/**
 * The policy session: verified acquisition, the persistent cache and local
 * fail-closed evaluation (spec 13.4, 18).
 *
 * Initial trust exists only inside `adoptOnboardingTrust`, called immediately
 * after authenticated device onboarding over the configured HTTPS origin;
 * every later refresh fetches the active snapshot, fetches the keyset chain
 * first when the snapshot key is unknown, verifies everything into temporary
 * memory and swaps the ONE persisted record (write + readback) before the
 * in-memory pointer moves. Network failure keeps the last valid cache and
 * reports offline; any verification failure preserves the cache, denies the
 * candidate and blocks network sync.
 */

import type {
  AcceptedPolicyState,
  LocalPolicyDecision,
  PolicyHttpRequest,
  PolicyHttpResponse,
  PolicyHttpTransport,
  PolicyIntegrityState,
  PolicyKeysetEnvelope,
  PolicyKeysetPage,
  SignedPolicySnapshot,
} from "./contracts";
import {
  KEYSET_PAGE_MAXIMUM_FETCHES,
  SIGNED_SNAPSHOT_MAXIMUM_BYTES,
} from "./contracts";
import { PolicyVerificationError, policyVerificationError } from "./contracts";
import { parseClosedJson } from "./strict-json";
import type { ClosedJsonValue } from "./strict-json";
import { validateKeysetEnvelope, verifyKeysetChain } from "./keyset";
import {
  resolveSnapshotMonotonicity,
  validateSnapshotEnvelope,
  verifyPolicySnapshot,
} from "./snapshot";
import {
  persistAcceptedPolicyState,
  readAcceptedPolicyStateFromRecord,
} from "./policy-cache";
import type { PolicyCacheAdapter } from "./policy-cache";
import { evaluatePolicy, normalizePolicyRule } from "./evaluator";
import type {
  NormalizedPolicyRule,
  PolicyEvaluationSubject,
} from "./evaluator";

const KEYSET_PAGE_MAXIMUM_BYTES = 1024 * 1024;
const SNAPSHOT_RESPONSE_MAXIMUM_BYTES = SIGNED_SNAPSHOT_MAXIMUM_BYTES + 1024;

export interface PolicySessionDeps {
  readonly http: PolicyHttpTransport;
  readonly resolveOrigin: () => string;
  readonly getAccessToken: () => string | null;
  readonly cache: PolicyCacheAdapter;
  readonly onStateChange?: (state: PolicyIntegrityState) => void;
}

/**
 * Transient failure statuses: rate limiting and server-side faults never
 * poison the integrity state — they behave exactly like an unreachable
 * server, preserving the last valid cache and allowing later retries.
 */
function isTransientStatus(status: number): boolean {
  return status === 429 || status >= 500;
}

function parseWireEnvelope(
  response: PolicyHttpResponse,
  maximumBytes: number,
): unknown {
  let parsed: ClosedJsonValue;
  try {
    parsed = parseClosedJson(response.bodyText, { maximumBytes });
  } catch (error) {
    if (isTransientStatus(response.status)) {
      throw policyVerificationError("policy_network_unavailable");
    }
    throw error;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw policyVerificationError("policy_envelope_invalid");
  }
  const envelope = parsed as Record<string, unknown>;
  for (const name of Object.keys(envelope)) {
    if (name !== "data" && name !== "error" && name !== "request_id" && name !== "warnings") {
      throw policyVerificationError("policy_envelope_invalid");
    }
  }
  for (const required of ["data", "error", "request_id", "warnings"]) {
    if (!Object.prototype.hasOwnProperty.call(envelope, required)) {
      throw policyVerificationError("policy_envelope_invalid");
    }
  }
  const error = envelope["error"];
  if (error !== null) {
    if (typeof error !== "object" || error === null || Array.isArray(error)) {
      throw policyVerificationError("policy_envelope_invalid");
    }
    const code = (error as Record<string, unknown>)["code"];
    if (code === "exclusion_policy_not_initialized") {
      throw policyVerificationError("policy_not_initialized_on_server");
    }
    if (isTransientStatus(response.status)) {
      throw policyVerificationError("policy_network_unavailable");
    }
    throw policyVerificationError("policy_envelope_invalid");
  }
  if (envelope["data"] === null || envelope["data"] === undefined) {
    throw policyVerificationError("policy_envelope_invalid");
  }
  return envelope["data"];
}

function validateKeysetPage(value: unknown): PolicyKeysetPage {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const page = value as Record<string, unknown>;
  for (const name of Object.keys(page)) {
    if (name !== "has_more" && name !== "keysets") {
      throw policyVerificationError("policy_payload_schema_invalid");
    }
  }
  if (typeof page["has_more"] !== "boolean" || !Array.isArray(page["keysets"])) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return {
    has_more: page["has_more"],
    keysets: page["keysets"].map((envelope) => validateKeysetEnvelope(envelope)),
  };
}

async function normalizeSnapshotRules(
  snapshot: SignedPolicySnapshot,
): Promise<readonly NormalizedPolicyRule[]> {
  const rules = [];
  for (const rule of snapshot.payload.rules) {
    rules.push(
      await normalizePolicyRule({
        ruleId: rule.rule_id,
        ruleKind: rule.rule_kind,
        sourceIdOperand: rule.source_id ?? null,
        textOperand:
          rule.folder_prefix ?? rule.path_glob ?? rule.extension ?? rule.media_type ?? rule.source_type ?? null,
        sizeBytesOperand: rule.maximum_size_bytes ?? null,
      }),
    );
  }
  return rules;
}

export class PolicySession {
  readonly #deps: PolicySessionDeps;
  #state: PolicyIntegrityState = "policy_not_initialized";
  #accepted: AcceptedPolicyState | null = null;
  #normalizedRules: readonly NormalizedPolicyRule[] | null = null;

  constructor(deps: PolicySessionDeps) {
    this.#deps = deps;
  }

  get state(): PolicyIntegrityState {
    return this.#state;
  }

  get acceptedState(): AcceptedPolicyState | null {
    return this.#accepted;
  }

  #setState(state: PolicyIntegrityState): void {
    this.#state = state;
    this.#deps.onStateChange?.(state);
  }

  /** Load and re-verify the persisted cache into memory (offline startup). */
  async restoreFromCache(): Promise<void> {
    const record = await this.#deps.cache.readPolicyCacheRecord();
    const restored = await readAcceptedPolicyStateFromRecord(record);
    if (restored === null) {
      this.#accepted = null;
      this.#normalizedRules = null;
      this.#setState("policy_not_initialized");
      return;
    }
    try {
      this.#normalizedRules = await normalizeSnapshotRules(restored.snapshotEnvelope);
    } catch {
      this.#accepted = null;
      this.#normalizedRules = null;
      this.#setState("policy_not_initialized");
      return;
    }
    this.#accepted = restored;
    this.#setState("policy_offline_cached");
  }

  async #request(path: string, headers: Record<string, string>): Promise<PolicyHttpResponse> {
    const token = this.#deps.getAccessToken();
    if (token === null) {
      throw policyVerificationError("policy_network_unavailable");
    }
    const origin = this.#deps.resolveOrigin();
    if (origin === "") {
      throw policyVerificationError("policy_network_unavailable");
    }
    const request: PolicyHttpRequest = {
      url: `${origin}${path}`,
      headers: { accept: "application/json", authorization: `Bearer ${token}`, ...headers },
    };
    try {
      return await this.#deps.http(request);
    } catch {
      throw policyVerificationError("policy_network_unavailable");
    }
  }

  async #fetchKeysetEnvelopes(afterRevision: number): Promise<PolicyKeysetEnvelope[]> {
    const collected: PolicyKeysetEnvelope[] = [];
    let cursor = afterRevision;
    for (let fetchIndex = 0; fetchIndex < KEYSET_PAGE_MAXIMUM_FETCHES; fetchIndex += 1) {
      const response = await this.#request(
        `/api/sync/exclusion-policy/keysets?after_keyset_revision=${cursor}`,
        {},
      );
      const page = validateKeysetPage(
        parseWireEnvelope(response, KEYSET_PAGE_MAXIMUM_BYTES),
      );
      collected.push(...page.keysets);
      if (!page.has_more) {
        return collected;
      }
      const lastRevision = page.keysets[page.keysets.length - 1]?.payload.keyset_revision;
      if (lastRevision === undefined || lastRevision <= cursor) {
        throw policyVerificationError("policy_keyset_page_overflow");
      }
      cursor = lastRevision;
    }
    throw policyVerificationError("policy_keyset_page_overflow");
  }

  async #fetchSnapshot(etag: string | null): Promise<
    { readonly unchanged: true } | { readonly unchanged: false; readonly envelope: SignedPolicySnapshot }
  > {
    const headers: Record<string, string> = {};
    if (etag !== null) {
      headers["if-none-match"] = etag;
    }
    const response = await this.#request("/api/sync/exclusion-policy/snapshot", headers);
    if (response.status === 304) {
      return { unchanged: true };
    }
    const envelope = validateSnapshotEnvelope(
      parseWireEnvelope(response, SNAPSHOT_RESPONSE_MAXIMUM_BYTES),
    );
    return { unchanged: false, envelope };
  }

  /**
   * Initial trust: accept the self-signed keyset revision 1 and the active
   * snapshot ONLY immediately after authenticated device onboarding. A
   * completed re-onboarding is the one boundary that may REPLACE an existing
   * anchor (e.g. after disconnecting and pointing at another workspace); the
   * replacement is fully verified before the single record is rewritten, and
   * any failure of the new candidate preserves the prior anchor and cache.
   */
  async adoptOnboardingTrust(): Promise<void> {
    try {
      const envelopes = await this.#fetchKeysetEnvelopes(0);
      const chain = await verifyKeysetChain({
        envelopes,
        trustedKeyset: null,
        trustedWorkspaceId: null,
        allowInitialTrust: true,
      });
      const snapshotResponse = await this.#fetchSnapshot(null);
      if (snapshotResponse.unchanged) {
        throw policyVerificationError("policy_envelope_invalid");
      }
      const verifiedSnapshot = await verifyPolicySnapshot({
        envelope: snapshotResponse.envelope,
        trustedKeyset: chain.acceptedKeyset,
        expectedWorkspaceId: chain.workspaceId,
      });
      const candidate: AcceptedPolicyState = {
        workspaceId: chain.workspaceId,
        revisionNumber: verifiedSnapshot.envelope.payload.revision_number,
        keysetSequence: chain.acceptedKeyset.payload.keyset_revision,
        keysetEnvelope: chain.acceptedKeyset,
        snapshotEnvelope: verifiedSnapshot.envelope,
      };
      const normalizedRules = await normalizeSnapshotRules(verifiedSnapshot.envelope);
      await persistAcceptedPolicyState(candidate, this.#deps.cache);
      // Switch the in-memory pointer only after the verified readback.
      this.#accepted = candidate;
      this.#normalizedRules = normalizedRules;
      this.#setState("policy_ready");
    } catch (error) {
      this.#handleAcquisitionFailure(error);
      throw error;
    }
  }

  /**
   * Session refresh: check the server snapshot (conditional GET), fetch and
   * verify the keyset chain first when the snapshot key is unknown, verify
   * into temporary memory and atomically replace the cache only after every
   * check passes. Never clears a good cache on failure.
   */
  async refresh(): Promise<void> {
    if (this.#state === "policy_integrity_failed") {
      return;
    }
    const etag = this.#accepted === null
      ? null
      : `"${this.#accepted.snapshotEnvelope.payload_sha256}"`;
    try {
      const snapshotResponse = await this.#fetchSnapshot(etag);
      if (snapshotResponse.unchanged) {
        if (this.#accepted !== null) {
          this.#setState("policy_ready");
          return;
        }
        throw policyVerificationError("policy_envelope_invalid");
      }
      const snapshotEnvelope = validateSnapshotEnvelope(snapshotResponse.envelope);
      let trustedKeyset = this.#accepted?.keysetEnvelope ?? null;
      let trustedWorkspaceId = this.#accepted?.workspaceId ?? null;
      if (trustedKeyset === null) {
        // No trusted material exists: only the onboarding boundary may create
        // initial trust, so a chain fetch here always fails closed.
        const envelopes = await this.#fetchKeysetEnvelopes(0);
        const chain = await verifyKeysetChain({
          envelopes,
          trustedKeyset: null,
          trustedWorkspaceId: null,
          allowInitialTrust: false,
        });
        trustedKeyset = chain.acceptedKeyset;
        trustedWorkspaceId = chain.workspaceId;
      } else {
        const knownKey = trustedKeyset.payload.keys.some(
          (key) => key.key_id === snapshotEnvelope.signature.key_id,
        );
        if (!knownKey) {
          // Fetch the keyset chain BEFORE verifying a snapshot signed by an
          // unknown key.
          const envelopes = await this.#fetchKeysetEnvelopes(
            trustedKeyset.payload.keyset_revision,
          );
          const chain = await verifyKeysetChain({
            envelopes,
            trustedKeyset,
            trustedWorkspaceId,
            allowInitialTrust: false,
          });
          trustedKeyset = chain.acceptedKeyset;
        }
      }
      const verifiedSnapshot = await verifyPolicySnapshot({
        envelope: snapshotEnvelope,
        trustedKeyset,
        expectedWorkspaceId: trustedWorkspaceId ?? snapshotEnvelope.payload.workspace_id,
      });
      const monotonicity = resolveSnapshotMonotonicity(
        verifiedSnapshot.envelope,
        this.#accepted?.snapshotEnvelope ?? null,
      );
      if (monotonicity === "downgrade") {
        throw policyVerificationError("policy_snapshot_downgrade");
      }
      if (monotonicity === "conflict") {
        throw policyVerificationError("policy_snapshot_conflict");
      }
      if (monotonicity === "identical") {
        if (this.#accepted !== null) {
          this.#setState("policy_ready");
          return;
        }
      }
      const candidate: AcceptedPolicyState = {
        workspaceId: verifiedSnapshot.envelope.payload.workspace_id,
        revisionNumber: verifiedSnapshot.envelope.payload.revision_number,
        keysetSequence: trustedKeyset.payload.keyset_revision,
        keysetEnvelope: trustedKeyset,
        snapshotEnvelope: verifiedSnapshot.envelope,
      };
      const normalizedRules = await normalizeSnapshotRules(verifiedSnapshot.envelope);
      await persistAcceptedPolicyState(candidate, this.#deps.cache);
      this.#accepted = candidate;
      this.#normalizedRules = normalizedRules;
      this.#setState("policy_ready");
    } catch (error) {
      this.#handleAcquisitionFailure(error);
      throw error;
    }
  }

  #handleAcquisitionFailure(error: unknown): void {
    if (!(error instanceof PolicyVerificationError)) {
      // Only closed reasons drive state transitions; anything else behaves
      // like an unreachable server (the cache is never cleared).
      this.#setState(this.#accepted === null ? "policy_not_initialized" : "policy_offline_cached");
      return;
    }
    switch (error.reason) {
      case "policy_network_unavailable":
        // Never clear a good cache because the network failed.
        this.#setState(this.#accepted === null ? "policy_not_initialized" : "policy_offline_cached");
        return;
      case "policy_not_initialized_on_server":
        this.#setState(this.#accepted === null ? "policy_not_initialized" : "policy_refresh_required");
        return;
      case "policy_cache_write_failed":
      case "policy_cache_readback_mismatch":
        // Retain the prior in-memory and persisted record; retry later.
        this.#setState(this.#accepted === null ? "policy_not_initialized" : "policy_refresh_required");
        return;
      default:
        // Tampering or contract failure: preserve the previous valid cache,
        // deny the candidate and block network sync.
        this.#setState("policy_integrity_failed");
    }
  }

  /**
   * Local deny-only evaluation against the last accepted snapshot. Any absent,
   * untrusted or unnormalizable policy, and any invalid subject evidence,
   * fails closed to the enforced excluded decision.
   */
  evaluate(subject: Omit<PolicyEvaluationSubject, "workspaceId">): LocalPolicyDecision {
    if (this.#accepted === null || this.#normalizedRules === null) {
      return { raw: "indeterminate", enforced: "excluded" };
    }
    try {
      const outcome = evaluatePolicy(this.#normalizedRules, {
        ...subject,
        workspaceId: this.#accepted.workspaceId,
      }, { workspaceId: this.#accepted.workspaceId });
      if (outcome.raw === "allowed") {
        return { raw: "allowed", enforced: "allowed" };
      }
      return { raw: outcome.raw, enforced: "excluded" };
    } catch {
      return { raw: "indeterminate", enforced: "excluded" };
    }
  }
}
