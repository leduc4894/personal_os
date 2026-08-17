/**
 * Test-only synthetic signing vectors for the exclusion-policy suites.
 *
 * The seeds mirror the documented synthetic fixtures of
 * `tests/unit/api_runtime/test_exclusion_policy_crypto.py` (never real
 * secrets): deriving their public keys through `@noble/ed25519` and comparing
 * against the committed Python-side golden fixtures is itself cross-language
 * parity evidence. Nothing in this module may be imported from production
 * code; it exists so tests can build real signed keyset/snapshot chains.
 */

import * as ed25519 from "@noble/ed25519";

import { canonicalJsonBytes, sha256Hex } from "./canonical-json";
import {
  buildSignedPolicyMessage,
  configureEd25519WebCryptoSha512,
  deriveEd25519KeyId,
  encodeBase64UrlWithoutPadding,
} from "./keyset";
import type {
  ClosedJsonValue,
} from "./strict-json";
import type {
  PolicyKeysetEnvelope,
  PolicyKeysetKeyPayload,
  PolicyKeysetPayload,
  PolicySignatureMember,
  PolicySnapshotPayload,
  SignedPolicySnapshot,
} from "./contracts";

export const WORKSPACE_ID = "018f47a0-7b00-7000-8000-000000000101";
export const OTHER_WORKSPACE_ID = "018f47a0-7b00-7000-8000-000000000102";
export const POLICY_REVISION_ID = "018f47a0-7b00-7000-8000-000000000201";
export const SECOND_POLICY_REVISION_ID = "018f47a0-7b00-7000-8000-000000000202";
export const PUBLISHED_AT = "2026-08-17T09:30:12.123456Z";
export const CREATED_AT = "2026-08-17T10:00:00.000000Z";
export const LATER_CREATED_AT = "2026-08-17T11:00:00.000000Z";
export const LATER_PUBLISHED_AT = "2026-08-17T12:00:00.000000Z";

/** SHA-256 of ASCII("exclusion_policy_evaluator/v1"), pinned by the golden snapshot fixture. */
export const EVALUATOR_CONTRACT_SHA256 = "8f174f9aa9a7a1580b377fa469a65c6e76801db66421404703b7aab38f50fbe1";

export const SNAPSHOT_SIGNING_DOMAIN = "exclusion-policy-snapshot/v1";
export const KEYSET_SIGNING_DOMAIN = "exclusion-policy-keyset/v1";

function seedBytes(first: number, lastExclusive: number): Uint8Array {
  return Uint8Array.from(
    Array.from({ length: lastExclusive - first }, (_, index) => first + index),
  );
}

/** The synthetic seeds shared with the Python-side golden fixtures. */
export const SNAPSHOT_SIGNER_SEED = seedBytes(0, 32);
export const KEYSET_CURRENT_SEED = seedBytes(32, 64);
export const KEYSET_STAGED_SEED = seedBytes(64, 96);
export const ROTATION_THIRD_SEED = seedBytes(96, 128);

export interface TestSigningKey {
  readonly seed: Uint8Array;
  readonly publicKey: Uint8Array;
  readonly keyId: string;
}

export async function deriveTestSigningKey(seed: Uint8Array): Promise<TestSigningKey> {
  configureEd25519WebCryptoSha512();
  const publicKey = await ed25519.getPublicKeyAsync(seed);
  return { seed, publicKey, keyId: await deriveEd25519KeyId(publicKey) };
}

export async function signCanonicalPayload(
  domain: string,
  payload: object,
  key: TestSigningKey,
): Promise<{ payloadBytes: Uint8Array; payloadSha256: string; signatureValue: string }> {
  configureEd25519WebCryptoSha512();
  const payloadBytes = canonicalJsonBytes(payload as unknown as ClosedJsonValue);
  const message = buildSignedPolicyMessage(domain, payloadBytes);
  const signature = await ed25519.signAsync(message, key.seed);
  return {
    payloadBytes,
    payloadSha256: await sha256Hex(payloadBytes),
    signatureValue: encodeBase64UrlWithoutPadding(signature),
  };
}

export function keysetKeyPayload(
  key: TestSigningKey,
  state: "current" | "staged" | "retired",
): PolicyKeysetKeyPayload {
  return {
    algorithm: "Ed25519",
    key_id: key.keyId,
    public_key: encodeBase64UrlWithoutPadding(key.publicKey),
    state,
  };
}

export function keysetPayload(
  keys: readonly PolicyKeysetKeyPayload[],
  options: {
    keysetRevision: number;
    parentKeysetRevision: number | null;
    workspaceId?: string;
    createdAt?: string;
  },
): PolicyKeysetPayload {
  return {
    contract: "exclusion_policy_keyset/v1",
    workspace_id: options.workspaceId ?? WORKSPACE_ID,
    keyset_revision: options.keysetRevision,
    parent_keyset_revision: options.parentKeysetRevision,
    created_at: options.createdAt ?? CREATED_AT,
    keys: [...keys].sort((left, right) => (left.key_id < right.key_id ? -1 : 1)),
  };
}

export async function buildKeysetEnvelope(
  payload: PolicyKeysetPayload,
  signers: readonly TestSigningKey[],
): Promise<PolicyKeysetEnvelope> {
  const primarySigner = signers[0];
  if (primarySigner === undefined) {
    throw new Error("keyset envelopes require at least one signer");
  }
  const reference = await signCanonicalPayload(KEYSET_SIGNING_DOMAIN, payload, primarySigner);
  const signatures: PolicySignatureMember[] = [];
  for (const signer of signers) {
    const signed = await signCanonicalPayload(KEYSET_SIGNING_DOMAIN, payload, signer);
    signatures.push({
      algorithm: "Ed25519",
      key_id: signer.keyId,
      value: signed.signatureValue,
    });
  }
  return { payload, payload_sha256: reference.payloadSha256, signatures };
}

export function snapshotPayload(
  rules: readonly object[],
  options: {
    revisionNumber: number;
    policyRevisionId?: string;
    parentPolicyRevisionId?: string | null;
    publishedAt?: string;
    workspaceId?: string;
  },
): PolicySnapshotPayload {
  return {
    contract: "exclusion_policy_snapshot/v1",
    workspace_id: options.workspaceId ?? WORKSPACE_ID,
    policy_revision_id: options.policyRevisionId ?? POLICY_REVISION_ID,
    revision_number: options.revisionNumber,
    parent_policy_revision_id: options.parentPolicyRevisionId ?? null,
    published_at: options.publishedAt ?? PUBLISHED_AT,
    default_decision: "allowed",
    evaluator_contract_sha256: EVALUATOR_CONTRACT_SHA256,
    rules: rules as unknown as PolicySnapshotPayload["rules"],
  };
}

export async function buildSnapshotEnvelope(
  payload: PolicySnapshotPayload,
  signer: TestSigningKey,
): Promise<SignedPolicySnapshot> {
  const signed = await signCanonicalPayload(SNAPSHOT_SIGNING_DOMAIN, payload, signer);
  return {
    payload,
    payload_sha256: signed.payloadSha256,
    signature: { algorithm: "Ed25519", key_id: signer.keyId, value: signed.signatureValue },
  };
}

/** Render one API success envelope body exactly like the server does. */
export function wireBody(data: unknown): string {
  return JSON.stringify({
    data,
    error: null,
    request_id: "018f47a0-7b00-7000-8000-000000000901",
    warnings: [],
  });
}
