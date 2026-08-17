/**
 * Closed contracts of the plugin-side exclusion-policy trust and evaluation
 * (spec 12, 13, 18, 19).
 *
 * The wire shapes hand-mirror `packages/api-client/src/generated/schema.ts`
 * (operations `listExclusionPolicyKeysets` and `getExclusionPolicySnapshot`);
 * the generated client itself stays out of the Obsidian bundle. Every
 * plugin-local verification failure names exactly one closed reason token —
 * never an HTTP code, library exception or rejected value (spec 19).
 */

// --- signed payload contract tags and signing domains (spec 12, 13) -------------------

export const SNAPSHOT_PAYLOAD_CONTRACT = "exclusion_policy_snapshot/v1";
export const KEYSET_PAYLOAD_CONTRACT = "exclusion_policy_keyset/v1";
export const SNAPSHOT_SIGNING_DOMAIN = "exclusion-policy-snapshot/v1";
export const KEYSET_SIGNING_DOMAIN = "exclusion-policy-keyset/v1";
export const SIGNING_DOMAINS: readonly string[] = [SNAPSHOT_SIGNING_DOMAIN, KEYSET_SIGNING_DOMAIN];

/** Hard ceiling on the complete encoded signed-snapshot response (spec 12). */
export const SIGNED_SNAPSHOT_MAXIMUM_BYTES = 256 * 1024;

/** Keyset chain ceilings (spec 13.3). */
export const KEYSET_MAXIMUM_NON_RETIRED_KEYS = 4;
export const KEYSET_PAGE_MAXIMUM_ENVELOPES = 16;
export const KEYSET_PAGE_MAXIMUM_FETCHES = 8;

/** Maximum rules in one published revision (spec 6.1). */
export const MAXIMUM_RULES_PER_REVISION = 256;

/** Evaluator semantics tag hashed into `evaluator_contract_sha256` (spec 7). */
export const EVALUATOR_CONTRACT = "exclusion_policy_evaluator/v1";

/** Ed25519 material geometry and identifier grammar (spec 12, 13). */
export const ED25519_PUBLIC_KEY_BYTES = 32;
export const ED25519_SIGNATURE_BYTES = 64;
export const SIGNATURE_ALGORITHM = "Ed25519";
export const KEY_ID_PREFIX = "ed25519-sha256-";

// --- closed verification reason tokens (spec 19) -------------------------------------

export const POLICY_VERIFICATION_REASONS = [
  "policy_response_oversized",
  "policy_response_malformed",
  "policy_envelope_invalid",
  "policy_payload_schema_invalid",
  "policy_payload_hash_mismatch",
  "policy_workspace_mismatch",
  "policy_signature_malformed",
  "policy_signature_untrusted_key",
  "policy_signature_invalid",
  "policy_evaluator_contract_mismatch",
  "policy_keyset_revision_invalid",
  "policy_keyset_chain_gap",
  "policy_keyset_downgrade",
  "policy_keyset_conflict",
  "policy_keyset_current_invalid",
  "policy_keyset_key_invalid",
  "policy_keyset_page_overflow",
  "policy_snapshot_downgrade",
  "policy_snapshot_conflict",
  "policy_onboarding_boundary_violation",
  "policy_network_unavailable",
  "policy_not_initialized_on_server",
  "policy_cache_write_failed",
  "policy_cache_readback_mismatch",
  "policy_value_unsupported",
] as const;

export type PolicyVerificationReason = (typeof POLICY_VERIFICATION_REASONS)[number];

/**
 * One plugin-local policy failure: a single closed reason token and a static
 * safe message. Library exceptions, rejected values, paths, signatures and
 * key material never enter this error (spec 19, 20).
 */
export class PolicyVerificationError extends Error {
  readonly reason: PolicyVerificationReason;

  constructor(reason: PolicyVerificationReason, message: string) {
    super(message);
    this.name = "PolicyVerificationError";
    this.reason = reason;
  }
}

export function policyVerificationError(reason: PolicyVerificationReason): PolicyVerificationError {
  return new PolicyVerificationError(reason, `exclusion policy verification failed: ${reason}`);
}

// --- wire shapes (mirror schema.ts) ----------------------------------------------------

export type PolicyKeysetState = "current" | "staged" | "retired";

export interface PolicyKeysetKeyPayload {
  readonly algorithm: "Ed25519";
  readonly key_id: string;
  readonly public_key: string;
  readonly state: PolicyKeysetState;
}

export interface PolicyKeysetPayload {
  readonly contract: "exclusion_policy_keyset/v1";
  readonly workspace_id: string;
  readonly keyset_revision: number;
  readonly parent_keyset_revision: number | null;
  readonly created_at: string;
  readonly keys: readonly PolicyKeysetKeyPayload[];
}

export interface PolicySignatureMember {
  readonly algorithm: "Ed25519";
  readonly key_id: string;
  readonly value: string;
}

/** One persisted keyset envelope: payload, digest and cross-signatures. */
export interface PolicyKeysetEnvelope {
  readonly payload: PolicyKeysetPayload;
  readonly payload_sha256: string;
  readonly signatures: readonly PolicySignatureMember[];
}

export interface PolicyKeysetPage {
  readonly has_more: boolean;
  readonly keysets: readonly PolicyKeysetEnvelope[];
}

export type RuleKindName =
  | "exact_source_id"
  | "folder_prefix"
  | "path_glob"
  | "extension"
  | "media_type"
  | "maximum_size"
  | "source_type";

export const RULE_KINDS: readonly RuleKindName[] = [
  "exact_source_id",
  "folder_prefix",
  "path_glob",
  "extension",
  "media_type",
  "maximum_size",
  "source_type",
];

/** The closed source-type vocabulary mirrored from the backend. */
export const SOURCE_TYPES: readonly string[] = [
  "markdown",
  "text",
  "pdf",
  "image",
  "audio",
  "web",
  "youtube",
];

/** One rule of a signed snapshot payload: exactly one typed operand member. */
export interface PolicySnapshotRulePayload {
  readonly rule_id: string;
  readonly rule_kind: RuleKindName;
  readonly source_id?: string;
  readonly folder_prefix?: string;
  readonly path_glob?: string;
  readonly extension?: string;
  readonly media_type?: string;
  readonly maximum_size_bytes?: number;
  readonly source_type?: string;
}

export interface PolicySnapshotPayload {
  readonly contract: "exclusion_policy_snapshot/v1";
  readonly workspace_id: string;
  readonly policy_revision_id: string;
  readonly revision_number: number;
  readonly parent_policy_revision_id: string | null;
  readonly published_at: string;
  readonly default_decision: "allowed";
  readonly evaluator_contract_sha256: string;
  readonly rules: readonly PolicySnapshotRulePayload[];
}

/** The persisted signed snapshot envelope (spec 12). */
export interface SignedPolicySnapshot {
  readonly payload: PolicySnapshotPayload;
  readonly payload_sha256: string;
  readonly signature: PolicySignatureMember;
}

// --- accepted state, decisions and integrity states ------------------------------------

/** The last accepted policy material (brief task 10, verbatim). */
export interface AcceptedPolicyState {
  readonly workspaceId: string;
  readonly revisionNumber: number;
  readonly keysetSequence: number;
  readonly keysetEnvelope: PolicyKeysetEnvelope;
  readonly snapshotEnvelope: SignedPolicySnapshot;
}

/** Local deny-only decision; indeterminate raw enforces as excluded. */
export type LocalPolicyDecision =
  | { readonly raw: "allowed"; readonly enforced: "allowed" }
  | { readonly raw: "excluded" | "indeterminate"; readonly enforced: "excluded" };

/** The closed policy integrity states of spec 18. */
export const POLICY_INTEGRITY_STATES = [
  "policy_not_initialized",
  "policy_ready",
  "policy_refresh_required",
  "policy_offline_cached",
  "policy_integrity_failed",
] as const;

export type PolicyIntegrityState = (typeof POLICY_INTEGRITY_STATES)[number];

// --- HTTP transport port ----------------------------------------------------------------

export interface PolicyHttpRequest {
  readonly url: string;
  readonly headers: Readonly<Record<string, string>>;
}

export interface PolicyHttpResponse {
  readonly status: number;
  readonly bodyText: string;
  readonly etag: string | null;
}

export type PolicyHttpTransport = (request: PolicyHttpRequest) => Promise<PolicyHttpResponse>;
