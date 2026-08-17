/**
 * Keyset chain verification and the single Ed25519/WebCrypto configuration.
 *
 * `configureEd25519WebCryptoSha512` is the ONE place the pinned
 * `@noble/ed25519` library gets its SHA-512 implementation; it binds the
 * platform WebCrypto implementation exactly once and every verification goes
 * through `ed25519.verifyAsync`. Chain acceptance follows spec 13.3/13.4: a
 * later keyset is accepted only when its parent equals the highest trusted
 * revision, its canonical bytes and digest are valid, at least one signature
 * comes from an already trusted non-retired key, and the current key signed
 * (activation proof). Revision 1 is self-signed and trusted only through the
 * authenticated onboarding boundary.
 */

import * as ed25519 from "@noble/ed25519";

import type {
  PolicyKeysetEnvelope,
  PolicyKeysetKeyPayload,
  PolicyKeysetPayload,
  PolicySignatureMember,
} from "./contracts";
import {
  ED25519_PUBLIC_KEY_BYTES,
  ED25519_SIGNATURE_BYTES,
  KEYSET_MAXIMUM_NON_RETIRED_KEYS,
  KEYSET_PAYLOAD_CONTRACT,
  KEYSET_SIGNING_DOMAIN,
  KEY_ID_PREFIX,
  SIGNATURE_ALGORITHM,
} from "./contracts";
import { policyVerificationError } from "./contracts";
import { canonicalJsonBytes, sha256Hex } from "./canonical-json";
import type { ClosedJsonValue } from "./strict-json";

// --- base64url and key identifiers ----------------------------------------------------

const BASE64URL_PATTERN = /^[A-Za-z0-9_-]+$/;

/** Encode raw bytes as base64url with the padding characters stripped. */
export function encodeBase64UrlWithoutPadding(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Decode strict base64url text; every malformed form answers null. */
export function decodeBase64UrlWithoutPadding(text: string): Uint8Array | null {
  if (text.length === 0 || text.length % 4 === 1 || !BASE64URL_PATTERN.test(text)) {
    return null;
  }
  const padded = text + "=".repeat((4 - (text.length % 4)) % 4);
  const binary = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

/** Derive `ed25519-sha256-BASE64URL` from one raw 32-byte public key. */
export async function deriveEd25519KeyId(publicKey: Uint8Array): Promise<string> {
  return KEY_ID_PREFIX + encodeBase64UrlWithoutPadding(
    new Uint8Array(await crypto.subtle.digest("SHA-256", publicKey as unknown as ArrayBuffer)),
  );
}

/** The closed key-ID grammar: prefix plus 43 base64url characters. */
export function isWellFormedEd25519KeyId(keyId: string): boolean {
  return keyId.startsWith(KEY_ID_PREFIX) && keyId.length === KEY_ID_PREFIX.length + 43 &&
    BASE64URL_PATTERN.test(keyId.slice(KEY_ID_PREFIX.length));
}

/** Join the ASCII domain separator, one 0x00 byte and the payload bytes. */
export function buildSignedPolicyMessage(domain: string, payloadBytes: Uint8Array): Uint8Array {
  const domainBytes = new TextEncoder().encode(domain);
  const message = new Uint8Array(domainBytes.length + 1 + payloadBytes.length);
  message.set(domainBytes, 0);
  message[domainBytes.length] = 0;
  message.set(payloadBytes, domainBytes.length + 1);
  return message;
}

// --- the single Ed25519/WebCrypto configuration ----------------------------------------

let isSha512Configured = false;

/**
 * Configure platform WebCrypto SHA-512 for noble-ed25519 exactly once. The
 * library default already uses WebCrypto subtle; this site pins it explicitly
 * so no module depends on library defaults and no second configuration site
 * can ever exist.
 */
export function configureEd25519WebCryptoSha512(): void {
  if (isSha512Configured) {
    return;
  }
  ed25519.hashes.sha512Async = async (message: Uint8Array) =>
    new Uint8Array(await crypto.subtle.digest("SHA-512", message as unknown as ArrayBuffer));
  isSha512Configured = true;
}

/**
 * Verify one detached Ed25519 signature. Any malformed geometry or library
 * rejection answers a plain false — library exceptions are never surfaced.
 */
export async function verifyDetachedEd25519(
  message: Uint8Array,
  signature: Uint8Array,
  publicKey: Uint8Array,
): Promise<boolean> {
  if (signature.length !== ED25519_SIGNATURE_BYTES || publicKey.length !== ED25519_PUBLIC_KEY_BYTES) {
    return false;
  }
  configureEd25519WebCryptoSha512();
  try {
    return await ed25519.verifyAsync(signature, message, publicKey);
  } catch {
    return false;
  }
}

// --- closed schema validation ----------------------------------------------------------

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const TIMESTAMP_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/;
const HEX_SHA256_PATTERN = /^[0-9a-f]{64}$/;
const KEY_STATES: readonly string[] = ["current", "staged", "retired"];

function asObject(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value as Record<string, unknown>;
}

function requireExactMembers(
  value: Record<string, unknown>,
  expected: readonly string[],
): void {
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

function requireInteger(value: unknown, minimum: number): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value;
}

function validateSignatureMember(value: unknown): { keyId: string; value: string } {
  const member = asObject(value);
  requireExactMembers(member, ["algorithm", "key_id", "value"]);
  if (member["algorithm"] !== SIGNATURE_ALGORITHM) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const keyId = requireString(member["key_id"]);
  const signatureValue = requireString(member["value"]);
  if (!isWellFormedEd25519KeyId(keyId)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (signatureValue.length !== 86 || !BASE64URL_PATTERN.test(signatureValue)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return { keyId, value: signatureValue };
}

function validateKeyMember(value: unknown): PolicyKeysetKeyPayload {
  const member = asObject(value);
  requireExactMembers(member, ["algorithm", "key_id", "public_key", "state"]);
  if (member["algorithm"] !== SIGNATURE_ALGORITHM) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const keyId = requireString(member["key_id"]);
  if (!isWellFormedEd25519KeyId(keyId)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const publicKey = requireString(member["public_key"]);
  if (publicKey.length !== 43 || !BASE64URL_PATTERN.test(publicKey)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const state = requireString(member["state"]);
  if (!KEY_STATES.includes(state)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return {
    algorithm: SIGNATURE_ALGORITHM,
    key_id: keyId,
    public_key: publicKey,
    state: state as PolicyKeysetKeyPayload["state"],
  };
}

function validateKeysetPayload(value: unknown): PolicyKeysetPayload {
  const payload = asObject(value);
  requireExactMembers(payload, [
    "contract",
    "workspace_id",
    "keyset_revision",
    "parent_keyset_revision",
    "created_at",
    "keys",
  ]);
  if (payload["contract"] !== KEYSET_PAYLOAD_CONTRACT) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!UUID_PATTERN.test(requireString(payload["workspace_id"]))) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const keysetRevision = requireInteger(payload["keyset_revision"], 1);
  const parent = payload["parent_keyset_revision"];
  if (parent !== null && (typeof parent !== "number" || !Number.isInteger(parent) || parent < 1)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (keysetRevision === 1 && parent !== null) {
    throw policyVerificationError("policy_keyset_revision_invalid");
  }
  if (keysetRevision > 1 && parent !== keysetRevision - 1) {
    throw policyVerificationError("policy_keyset_revision_invalid");
  }
  if (!TIMESTAMP_PATTERN.test(requireString(payload["created_at"]))) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!Array.isArray(payload["keys"]) || payload["keys"].length === 0) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const keys = payload["keys"].map((key) => validateKeyMember(key));
  const seenKeyIds = new Set<string>();
  let currentCount = 0;
  let nonRetiredCount = 0;
  for (const key of keys) {
    if (seenKeyIds.has(key.key_id)) {
      throw policyVerificationError("policy_keyset_key_invalid");
    }
    seenKeyIds.add(key.key_id);
    if (key.state === "current") {
      currentCount += 1;
    }
    if (key.state !== "retired") {
      nonRetiredCount += 1;
    }
  }
  if (currentCount !== 1) {
    // Exactly one current key per revision: a keyset without a current key
    // could never prove activation and would strand the trust chain.
    throw policyVerificationError("policy_keyset_current_invalid");
  }
  if (nonRetiredCount > KEYSET_MAXIMUM_NON_RETIRED_KEYS) {
    throw policyVerificationError("policy_keyset_key_invalid");
  }
  return {
    contract: KEYSET_PAYLOAD_CONTRACT,
    workspace_id: requireString(payload["workspace_id"]),
    keyset_revision: keysetRevision,
    parent_keyset_revision: parent,
    created_at: requireString(payload["created_at"]),
    keys,
  };
}

/**
 * Validate one keyset envelope against the closed schema: exact member sets,
 * typed fields, identifier grammars and the structural ceilings, all before
 * any canonicalization or signature work.
 */
export function validateKeysetEnvelope(value: unknown): PolicyKeysetEnvelope {
  const envelope = asObject(value);
  requireExactMembers(envelope, ["payload", "payload_sha256", "signatures"]);
  const payload = validateKeysetPayload(envelope["payload"]);
  const payloadSha256 = requireString(envelope["payload_sha256"]);
  if (!HEX_SHA256_PATTERN.test(payloadSha256)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!Array.isArray(envelope["signatures"]) || envelope["signatures"].length === 0) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const signatures: PolicySignatureMember[] = envelope["signatures"].map((signature) => {
    const validated = validateSignatureMember(signature);
    return {
      algorithm: SIGNATURE_ALGORITHM,
      key_id: validated.keyId,
      value: validated.value,
    };
  });
  return { payload, payload_sha256: payloadSha256, signatures };
}

// --- chain verification ----------------------------------------------------------------

export interface KeysetChainInput {
  /** Envelopes in ascending revision order, as delivered by the API pages. */
  readonly envelopes: readonly PolicyKeysetEnvelope[];
  /** The currently trusted keyset, or null before initial trust exists. */
  readonly trustedKeyset: PolicyKeysetEnvelope | null;
  /** The pinned workspace binding, or null while establishing first trust. */
  readonly trustedWorkspaceId: string | null;
  /** True ONLY immediately after authenticated device onboarding. */
  readonly allowInitialTrust: boolean;
}

export interface KeysetChainOutcome {
  readonly acceptedKeyset: PolicyKeysetEnvelope;
  readonly workspaceId: string;
  readonly payloadSha256: string;
}

interface VerifiedKeyset {
  readonly envelope: PolicyKeysetEnvelope;
  readonly payloadBytes: Uint8Array;
  readonly payloadSha256: string;
}

/**
 * Verify one keyset envelope's intrinsic integrity: closed schema, the
 * key_id/public-key binding, canonical bytes and the exact digest. Signature
 * and chain checks happen on top of this in `verifyKeysetChain`.
 */
export async function verifyKeysetEnvelopeIntegrity(
  envelope: PolicyKeysetEnvelope,
): Promise<{ readonly payloadBytes: Uint8Array; readonly payloadSha256: string }> {
  const validated = validateKeysetEnvelope(envelope);
  for (const key of validated.payload.keys) {
    const publicKey = decodeBase64UrlWithoutPadding(key.public_key);
    if (publicKey === null || publicKey.length !== ED25519_PUBLIC_KEY_BYTES) {
      throw policyVerificationError("policy_keyset_key_invalid");
    }
    if ((await deriveEd25519KeyId(publicKey)) !== key.key_id) {
      throw policyVerificationError("policy_keyset_key_invalid");
    }
  }
  const payloadBytes = canonicalJsonBytes(validated.payload as unknown as ClosedJsonValue);
  const payloadSha256 = await sha256Hex(payloadBytes);
  if (payloadSha256 !== validated.payload_sha256) {
    throw policyVerificationError("policy_payload_hash_mismatch");
  }
  return { payloadBytes, payloadSha256 };
}

async function verifyKeysetEnvelope(envelope: PolicyKeysetEnvelope): Promise<VerifiedKeyset> {
  const integrity = await verifyKeysetEnvelopeIntegrity(envelope);
  return {
    envelope: validateKeysetEnvelope(envelope),
    payloadBytes: integrity.payloadBytes,
    payloadSha256: integrity.payloadSha256,
  };
}

async function verifyAnySignature(
  verified: VerifiedKeyset,
  keyId: string,
  signatureValue: string,
): Promise<boolean> {
  const signature = decodeBase64UrlWithoutPadding(signatureValue);
  if (signature === null) {
    return false;
  }
  const message = buildSignedPolicyMessage(KEYSET_SIGNING_DOMAIN, verified.payloadBytes);
  const keyEntry = verified.envelope.payload.keys.find((key) => key.key_id === keyId);
  if (keyEntry === undefined) {
    return false;
  }
  const publicKey = decodeBase64UrlWithoutPadding(keyEntry.public_key);
  if (publicKey === null) {
    return false;
  }
  return verifyDetachedEd25519(message, signature, publicKey);
}

function keyStateOf(envelope: PolicyKeysetEnvelope, keyId: string): string | null {
  return envelope.payload.keys.find((key) => key.key_id === keyId)?.state ?? null;
}

/** Verify and accept a keyset chain page by page against the trusted state. */
export async function verifyKeysetChain(input: KeysetChainInput): Promise<KeysetChainOutcome> {
  const firstEnvelope = input.envelopes[0];
  if (firstEnvelope === undefined) {
    throw policyVerificationError("policy_keyset_chain_gap");
  }
  if (input.trustedKeyset === null) {
    if (!input.allowInitialTrust) {
      throw policyVerificationError("policy_onboarding_boundary_violation");
    }
    const first = firstEnvelope;
    if (first.payload.keyset_revision !== 1 || first.payload.parent_keyset_revision !== null) {
      throw policyVerificationError("policy_keyset_revision_invalid");
    }
    if (input.trustedWorkspaceId !== null && first.payload.workspace_id !== input.trustedWorkspaceId) {
      throw policyVerificationError("policy_workspace_mismatch");
    }
    const verified = await verifyKeysetEnvelope(first);
    // Revision 1 is self-signed by its own current key.
    const currentKey = first.payload.keys.find((key) => key.state === "current");
    if (currentKey === undefined) {
      throw policyVerificationError("policy_keyset_current_invalid");
    }
    const selfSigned = first.signatures.find((signature) => signature.key_id === currentKey.key_id);
    if (selfSigned === undefined || !(await verifyAnySignature(verified, selfSigned.key_id, selfSigned.value))) {
      throw policyVerificationError("policy_signature_invalid");
    }
    for (const signature of first.signatures) {
      const known = keyStateOf(first, signature.key_id) !== null;
      if (!known) {
        throw policyVerificationError("policy_signature_untrusted_key");
      }
    }
    let accepted = verified;
    for (const envelope of input.envelopes.slice(1)) {
      accepted = await acceptRotation(accepted, envelope, input.trustedWorkspaceId);
    }
    return {
      acceptedKeyset: accepted.envelope,
      workspaceId: accepted.envelope.payload.workspace_id,
      payloadSha256: accepted.payloadSha256,
    };
  }
  let accepted = await verifyKeysetEnvelope(input.trustedKeyset);
  for (const envelope of input.envelopes) {
    accepted = await acceptRotation(accepted, envelope, input.trustedWorkspaceId);
  }
  return {
    acceptedKeyset: accepted.envelope,
    workspaceId: accepted.envelope.payload.workspace_id,
    payloadSha256: accepted.payloadSha256,
  };
}

async function acceptRotation(
  trusted: VerifiedKeyset,
  candidate: PolicyKeysetEnvelope,
  trustedWorkspaceId: string | null,
): Promise<VerifiedKeyset> {
  // The closed schema runs before any revision shortcut: even an identical
  // replay must first be schema-valid.
  const validatedCandidate = validateKeysetEnvelope(candidate);
  const candidateRevision = validatedCandidate.payload.keyset_revision;
  const trustedRevision = trusted.envelope.payload.keyset_revision;
  if (validatedCandidate.payload.workspace_id !== trusted.envelope.payload.workspace_id) {
    throw policyVerificationError("policy_workspace_mismatch");
  }
  if (trustedWorkspaceId !== null && validatedCandidate.payload.workspace_id !== trustedWorkspaceId) {
    throw policyVerificationError("policy_workspace_mismatch");
  }
  if (candidateRevision < trustedRevision) {
    throw policyVerificationError("policy_keyset_downgrade");
  }
  if (candidateRevision === trustedRevision) {
    if (validatedCandidate.payload_sha256 === trusted.envelope.payload_sha256) {
      return trusted;
    }
    throw policyVerificationError("policy_keyset_conflict");
  }
  if (validatedCandidate.payload.parent_keyset_revision !== trustedRevision) {
    throw policyVerificationError("policy_keyset_chain_gap");
  }
  const verified = await verifyKeysetEnvelope(validatedCandidate);
  // (a) chain continuity: at least one valid signature from a key that was
  //     trusted before AND stays non-retired in the new revision.
  let chainSignature = false;
  // (b) activation: the current key of the new revision must sign it.
  const currentKey = verified.envelope.payload.keys.find((key) => key.state === "current");
  let activationSignature = currentKey === undefined;
  for (const signature of verified.envelope.signatures) {
    const previousState = keyStateOf(trusted.envelope, signature.key_id);
    const nextState = keyStateOf(verified.envelope, signature.key_id);
    const valid = await verifyAnySignature(verified, signature.key_id, signature.value);
    if (!valid) {
      continue;
    }
    if (previousState !== null && previousState !== "retired" && nextState !== null && nextState !== "retired") {
      chainSignature = true;
    }
    if (currentKey !== undefined && signature.key_id === currentKey.key_id) {
      activationSignature = true;
    }
  }
  if (!chainSignature) {
    throw policyVerificationError("policy_signature_untrusted_key");
  }
  if (!activationSignature) {
    throw policyVerificationError("policy_signature_invalid");
  }
  return verified;
}

/**
 * Resolve the raw public key for a key ID from the trusted keyset (any
 * lifecycle state — trusted history stays resolvable for snapshots).
 */
export function resolveTrustedKey(
  trustedKeyset: PolicyKeysetEnvelope,
  keyId: string,
): Uint8Array | null {
  const keyEntry = trustedKeyset.payload.keys.find((key) => key.key_id === keyId);
  if (keyEntry === undefined) {
    return null;
  }
  return decodeBase64UrlWithoutPadding(keyEntry.public_key);
}
