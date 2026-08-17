/**
 * The persistent policy cache: ONE versioned plugin-data record holding only
 * the last accepted signed keyset/snapshot envelopes plus the monotonic
 * revision metadata (spec 18). No private key, Vault content, credential or
 * raw excluded path is ever part of the record.
 *
 * Persistence is verify-then-switch: the accepted state is written through a
 * narrow adapter as one record, read back and compared byte-exactly before
 * the caller may switch any in-memory pointer; a failed write or mismatched
 * readback retains the prior record and fails closed.
 */

import type { AcceptedPolicyState } from "./contracts";
import { policyVerificationError } from "./contracts";
import { canonicalizeClosedJson } from "./canonical-json";
import { validateKeysetEnvelope, verifyKeysetEnvelopeIntegrity } from "./keyset";
import { validateSnapshotEnvelope, verifyPolicySnapshot } from "./snapshot";

export const POLICY_CACHE_RECORD_CONTRACT = "obsidian_exclusion_policy_cache/v1";

/**
 * The narrow persistence surface: read the stored record value and replace it
 * atomically. The plugin binds this to its single plugin-data document.
 */
export interface PolicyCacheAdapter {
  readPolicyCacheRecord(): Promise<unknown>;
  writePolicyCacheRecord(record: unknown): Promise<void>;
}

/** Build the single versioned record for one accepted state. */
export function buildPolicyCacheRecord(state: AcceptedPolicyState): Record<string, unknown> {
  return {
    contract: POLICY_CACHE_RECORD_CONTRACT,
    workspace_id: state.workspaceId,
    keyset_sequence: state.keysetSequence,
    revision_number: state.revisionNumber,
    policy_revision_id: state.snapshotEnvelope.payload.policy_revision_id,
    payload_sha256: state.snapshotEnvelope.payload_sha256,
    keyset_envelope: state.keysetEnvelope,
    snapshot_envelope: state.snapshotEnvelope,
  };
}

function requireMember(record: Record<string, unknown>, name: string): unknown {
  if (!Object.prototype.hasOwnProperty.call(record, name)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return record[name];
}

function requireStringMember(record: Record<string, unknown>, name: string): string {
  const value = requireMember(record, name);
  if (typeof value !== "string") {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value;
}

function requireIntegerMember(record: Record<string, unknown>, name: string): number {
  const value = requireMember(record, name);
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value;
}

/**
 * Read one persisted record back into the accepted state, re-verifying the
 * envelopes (closed schema, canonical bytes, digest, and the snapshot
 * signature under a recorded key). Any malformed or tampered record answers
 * null — treated as an absent cache, never as trusted material.
 */
export async function readAcceptedPolicyStateFromRecord(
  record: unknown,
): Promise<AcceptedPolicyState | null> {
  if (typeof record !== "object" || record === null || Array.isArray(record)) {
    return null;
  }
  const candidate = record as Record<string, unknown>;
  try {
    if (requireStringMember(candidate, "contract") !== POLICY_CACHE_RECORD_CONTRACT) {
      return null;
    }
    const workspaceId = requireStringMember(candidate, "workspace_id");
    const keysetSequence = requireIntegerMember(candidate, "keyset_sequence");
    const revisionNumber = requireIntegerMember(candidate, "revision_number");
    const keysetEnvelope = validateKeysetEnvelope(requireMember(candidate, "keyset_envelope"));
    const snapshotEnvelope = validateSnapshotEnvelope(requireMember(candidate, "snapshot_envelope"));
    // Re-verify the persisted keyset's own canonical bytes and digest; a
    // tampered record is absent cache material, never trusted.
    await verifyKeysetEnvelopeIntegrity(keysetEnvelope);
    if (snapshotEnvelope.payload.workspace_id !== workspaceId) {
      return null;
    }
    if (keysetEnvelope.payload.workspace_id !== workspaceId) {
      return null;
    }
    await verifyPolicySnapshot({
      envelope: snapshotEnvelope,
      trustedKeyset: keysetEnvelope,
      expectedWorkspaceId: workspaceId,
    });
    return {
      workspaceId,
      revisionNumber,
      keysetSequence,
      keysetEnvelope,
      snapshotEnvelope,
    };
  } catch {
    return null;
  }
}

/**
 * Persist one accepted state as exactly one record and verify the readback:
 * the write plus a canonical byte comparison of what the adapter returns.
 * Both failures retain the prior persisted record and throw closed reasons.
 */
export async function persistAcceptedPolicyState(
  state: AcceptedPolicyState,
  adapter: PolicyCacheAdapter,
): Promise<void> {
  const record = buildPolicyCacheRecord(state);
  let canonicalForm: string;
  try {
    canonicalForm = canonicalizeClosedJson(record as never);
  } catch {
    throw policyVerificationError("policy_cache_write_failed");
  }
  try {
    await adapter.writePolicyCacheRecord(record);
  } catch {
    throw policyVerificationError("policy_cache_write_failed");
  }
  let readBack: unknown;
  try {
    readBack = await adapter.readPolicyCacheRecord();
  } catch {
    throw policyVerificationError("policy_cache_readback_mismatch");
  }
  let readBackForm: string;
  try {
    readBackForm = canonicalizeClosedJson(readBack as never);
  } catch {
    throw policyVerificationError("policy_cache_readback_mismatch");
  }
  if (readBackForm !== canonicalForm) {
    throw policyVerificationError("policy_cache_readback_mismatch");
  }
}
