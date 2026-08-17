/**
 * Signed snapshot verification (spec 12 and 13.4).
 *
 * Validation order: closed envelope/payload schema and workspace binding,
 * canonical RFC 8785 payload bytes, exact SHA-256 digest, evaluator contract
 * hash, key resolution from the trusted keyset, then the detached Ed25519
 * signature. Monotonic revision rules are a pure function over revision
 * identity so the session applies them before any cache replacement.
 */

import type {
  PolicyKeysetEnvelope,
  PolicySnapshotPayload,
  PolicySnapshotRulePayload,
  RuleKindName,
  SignedPolicySnapshot,
} from "./contracts";
import {
  EVALUATOR_CONTRACT,
  MAXIMUM_RULES_PER_REVISION,
  SIGNATURE_ALGORITHM,
  SNAPSHOT_PAYLOAD_CONTRACT,
  SNAPSHOT_SIGNING_DOMAIN,
} from "./contracts";
import { policyVerificationError } from "./contracts";
import { canonicalJsonBytes, sha256Hex } from "./canonical-json";
import { buildSignedPolicyMessage, resolveTrustedKey, verifyDetachedEd25519 } from "./keyset";
import { decodeBase64UrlWithoutPadding, isWellFormedEd25519KeyId } from "./keyset";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const TIMESTAMP_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/;
const HEX_SHA256_PATTERN = /^[0-9a-f]{64}$/;

/** The closed kind-to-operand mapping (spec 6.2, 12). */
const OPERAND_BY_KIND: Readonly<Record<RuleKindName, string>> = {
  exact_source_id: "source_id",
  folder_prefix: "folder_prefix",
  path_glob: "path_glob",
  extension: "extension",
  media_type: "media_type",
  maximum_size: "maximum_size_bytes",
  source_type: "source_type",
};

function asObject(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value as Record<string, unknown>;
}

function requireExactMembers(value: Record<string, unknown>, expected: readonly string[]): void {
  for (const name of Object.keys(value)) {
    if (!expected.includes(name)) {
      throw policyVerificationError("policy_payload_schema_invalid");
    }
  }
  for (const name of expected) {
    if (!Object.prototype.hasOwnProperty.call(value, name)) {
      throw policyVerificationError("policy_payload_schema_invalid");
    }
  }
}

function requireString(value: unknown): string {
  if (typeof value !== "string") {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value;
}

function validateRule(value: unknown): PolicySnapshotRulePayload {
  const rule = asObject(value);
  if (Object.keys(rule).length !== 3) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!("rule_id" in rule) || !("rule_kind" in rule)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!UUID_PATTERN.test(requireString(rule["rule_id"]))) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const ruleKind = requireString(rule["rule_kind"]) as RuleKindName;
  const operandName = OPERAND_BY_KIND[ruleKind];
  if (operandName === undefined) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const expected = ["rule_id", "rule_kind", operandName];
  requireExactMembers(rule, expected);
  if (operandName === "maximum_size_bytes") {
    const sizeBytes = rule[operandName];
    if (typeof sizeBytes !== "number" || !Number.isInteger(sizeBytes) || sizeBytes < 0) {
      throw policyVerificationError("policy_payload_schema_invalid");
    }
  } else if (typeof rule[operandName] !== "string") {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return rule as unknown as PolicySnapshotRulePayload;
}

function validateSnapshotPayload(value: unknown): PolicySnapshotPayload {
  const payload = asObject(value);
  requireExactMembers(payload, [
    "contract",
    "workspace_id",
    "policy_revision_id",
    "revision_number",
    "parent_policy_revision_id",
    "published_at",
    "default_decision",
    "evaluator_contract_sha256",
    "rules",
  ]);
  if (payload["contract"] !== SNAPSHOT_PAYLOAD_CONTRACT) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (payload["default_decision"] !== "allowed") {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  for (const uuidMember of ["workspace_id", "policy_revision_id"]) {
    if (!UUID_PATTERN.test(requireString(payload[uuidMember]))) {
      throw policyVerificationError("policy_payload_schema_invalid");
    }
  }
  const parent = payload["parent_policy_revision_id"];
  if (parent !== null && (typeof parent !== "string" || !UUID_PATTERN.test(parent))) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const revisionNumber = payload["revision_number"];
  if (typeof revisionNumber !== "number" || !Number.isInteger(revisionNumber) || revisionNumber < 1) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!TIMESTAMP_PATTERN.test(requireString(payload["published_at"]))) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!HEX_SHA256_PATTERN.test(requireString(payload["evaluator_contract_sha256"]))) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!Array.isArray(payload["rules"]) || payload["rules"].length > MAXIMUM_RULES_PER_REVISION) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const rules = payload["rules"].map((rule) => validateRule(rule));
  return {
    contract: SNAPSHOT_PAYLOAD_CONTRACT,
    workspace_id: requireString(payload["workspace_id"]),
    policy_revision_id: requireString(payload["policy_revision_id"]),
    revision_number: revisionNumber,
    parent_policy_revision_id: parent,
    published_at: requireString(payload["published_at"]),
    default_decision: "allowed",
    evaluator_contract_sha256: requireString(payload["evaluator_contract_sha256"]),
    rules,
  };
}

/**
 * Validate one signed snapshot envelope against the closed schema: exact
 * member sets, the closed kind-to-operand mapping, identifier grammars and
 * the rule ceiling, before any canonicalization.
 */
export function validateSnapshotEnvelope(value: unknown): SignedPolicySnapshot {
  const envelope = asObject(value);
  requireExactMembers(envelope, ["payload", "payload_sha256", "signature"]);
  const payload = validateSnapshotPayload(envelope["payload"]);
  const payloadSha256 = requireString(envelope["payload_sha256"]);
  if (!HEX_SHA256_PATTERN.test(payloadSha256)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const signature = asObject(envelope["signature"]);
  requireExactMembers(signature, ["algorithm", "key_id", "value"]);
  if (signature["algorithm"] !== SIGNATURE_ALGORITHM) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const keyId = requireString(signature["key_id"]);
  const signatureValue = requireString(signature["value"]);
  if (!isWellFormedEd25519KeyId(keyId)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (signatureValue.length !== 86 || !/^[A-Za-z0-9_-]+$/.test(signatureValue)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return {
    payload,
    payload_sha256: payloadSha256,
    signature: { algorithm: SIGNATURE_ALGORITHM, key_id: keyId, value: signatureValue },
  };
}

export interface SnapshotVerificationInput {
  readonly envelope: SignedPolicySnapshot;
  readonly trustedKeyset: PolicyKeysetEnvelope;
  readonly expectedWorkspaceId: string;
}

export interface VerifiedSnapshot {
  readonly envelope: SignedPolicySnapshot;
  readonly payloadBytes: Uint8Array;
  readonly payloadSha256: string;
}

/** Verify one snapshot envelope against the trusted keyset (spec 13.4). */
export async function verifyPolicySnapshot(
  input: SnapshotVerificationInput,
): Promise<VerifiedSnapshot> {
  const envelope = validateSnapshotEnvelope(input.envelope);
  if (envelope.payload.workspace_id !== input.expectedWorkspaceId) {
    throw policyVerificationError("policy_workspace_mismatch");
  }
  const payloadBytes = canonicalJsonBytes(envelope.payload as unknown as never);
  const payloadSha256 = await sha256Hex(payloadBytes);
  if (payloadSha256 !== envelope.payload_sha256) {
    throw policyVerificationError("policy_payload_hash_mismatch");
  }
  const evaluatorContractHash = await sha256Hex(new TextEncoder().encode(EVALUATOR_CONTRACT));
  if (evaluatorContractHash !== envelope.payload.evaluator_contract_sha256) {
    throw policyVerificationError("policy_evaluator_contract_mismatch");
  }
  const publicKey = resolveTrustedKey(input.trustedKeyset, envelope.signature.key_id);
  if (publicKey === null) {
    throw policyVerificationError("policy_signature_untrusted_key");
  }
  const signature = decodeBase64UrlWithoutPadding(envelope.signature.value);
  if (signature === null) {
    throw policyVerificationError("policy_signature_malformed");
  }
  const message = buildSignedPolicyMessage(SNAPSHOT_SIGNING_DOMAIN, payloadBytes);
  const isValid = await verifyDetachedEd25519(message, signature, publicKey);
  if (!isValid) {
    throw policyVerificationError("policy_signature_invalid");
  }
  return { envelope, payloadBytes, payloadSha256 };
}

export type SnapshotMonotonicity = "accept" | "identical" | "conflict" | "downgrade";

/**
 * Apply the monotonic revision rules of spec 13.4: a greater revision is
 * accepted, the same revision only when revision ID and digest are identical,
 * and anything lower or conflicting is an integrity failure.
 */
export function resolveSnapshotMonotonicity(
  candidate: SignedPolicySnapshot,
  accepted: SignedPolicySnapshot | null,
): SnapshotMonotonicity {
  if (accepted === null) {
    return "accept";
  }
  if (candidate.payload.revision_number > accepted.payload.revision_number) {
    return "accept";
  }
  if (candidate.payload.revision_number < accepted.payload.revision_number) {
    return "downgrade";
  }
  const identical =
    candidate.payload.policy_revision_id === accepted.payload.policy_revision_id &&
    candidate.payload_sha256 === accepted.payload_sha256;
  return identical ? "identical" : "conflict";
}
