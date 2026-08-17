import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { parseClosedJson } from "./strict-json";
import {
  decodeBase64UrlWithoutPadding,
  deriveEd25519KeyId,
  encodeBase64UrlWithoutPadding,
  isWellFormedEd25519KeyId,
  validateKeysetEnvelope,
  verifyDetachedEd25519,
  verifyKeysetChain,
} from "./keyset";
import type { PolicyKeysetEnvelope } from "./contracts";
import { PolicyVerificationError } from "./contracts";
import {
  CREATED_AT,
  KEYSET_CURRENT_SEED,
  KEYSET_STAGED_SEED,
  LATER_CREATED_AT,
  OTHER_WORKSPACE_ID,
  ROTATION_THIRD_SEED,
  SNAPSHOT_SIGNER_SEED,
  WORKSPACE_ID,
  deriveTestSigningKey,
  keysetPayload,
  buildKeysetEnvelope,
  keysetKeyPayload,
} from "./policy-signing-test-vectors";

const KEYSET_FIXTURE = JSON.parse(
  readFileSync(
    new URL("../../../../tests/fixtures/exclusion_policy/keyset-golden.json", import.meta.url),
    "utf8",
  ),
) as {
  readonly payload: string;
  readonly payload_sha256: string;
  readonly signature: { readonly algorithm: string; readonly key_id: string; readonly value: string };
  readonly signing_public_key: string;
};

function fixtureKeysetEnvelope(): PolicyKeysetEnvelope {
  const payload = parseClosedJson(KEYSET_FIXTURE.payload, {
    maximumBytes: 64 * 1024,
  }) as unknown;
  return validateKeysetEnvelope({
    payload,
    payload_sha256: KEYSET_FIXTURE.payload_sha256,
    signatures: [KEYSET_FIXTURE.signature],
  });
}

function schemaRejection(value: unknown): string {
  try {
    validateKeysetEnvelope(value);
  } catch (error) {
    if (error instanceof PolicyVerificationError) {
      return error.reason;
    }
    throw error;
  }
  throw new Error("expected keyset envelope validation to reject the value");
}

async function chainRejection(
  input: Parameters<typeof verifyKeysetChain>[0],
): Promise<string> {
  try {
    await verifyKeysetChain(input);
  } catch (error) {
    if (error instanceof PolicyVerificationError) {
      return error.reason;
    }
    throw error;
  }
  throw new Error("expected the keyset chain verification to reject the input");
}

describe("base64url and key-id derivation", () => {
  it("round-trips raw bytes without padding", () => {
    expect(encodeBase64UrlWithoutPadding(new Uint8Array([0, 1, 2, 250]))).toBe("AAEC-g");
    expect(decodeBase64UrlWithoutPadding("AAEC-g")).toEqual(Uint8Array.from([0, 1, 2, 250]));
    expect(decodeBase64UrlWithoutPadding("AAEC-g")).toEqual(Uint8Array.from([0, 1, 2, 250]));
  });

  it("rejects non-url alphabets, padding and impossible lengths", () => {
    for (const malformed of ["", "A", "AAEC+g", "AAEC/g", "AAEC=g", "AAEC-g=", "AAEC gg", "AAEC-g\n"]) {
      expect(decodeBase64UrlWithoutPadding(malformed)).toBeNull();
    }
  });

  it("derives the fixture key id from the fixture public key", async () => {
    const publicKey = decodeBase64UrlWithoutPadding(KEYSET_FIXTURE.signing_public_key);
    expect(publicKey).not.toBeNull();
    expect(await deriveEd25519KeyId(publicKey as Uint8Array)).toBe(
      KEYSET_FIXTURE.signature.key_id,
    );
    expect(isWellFormedEd25519KeyId(KEYSET_FIXTURE.signature.key_id)).toBe(true);
    expect(isWellFormedEd25519KeyId("ed25519-sha256-short")).toBe(false);
  });
});

describe("WebCrypto Ed25519 verification", () => {
  it("accepts the golden keyset signature over the Python canonical bytes", async () => {
    const envelope = fixtureKeysetEnvelope();
    const outcome = await verifyKeysetChain({
      envelopes: [envelope],
      trustedKeyset: await initialKeysetOfCurrentKey(),
      trustedWorkspaceId: WORKSPACE_ID,
      allowInitialTrust: false,
    });
    expect(outcome.acceptedKeyset.payload.keyset_revision).toBe(2);
  });

  it("rejects a modified payload byte and the wrong key", async () => {
    const publicKey = decodeBase64UrlWithoutPadding(
      KEYSET_FIXTURE.signing_public_key,
    ) as Uint8Array;
    const signature = decodeBase64UrlWithoutPadding(
      KEYSET_FIXTURE.signature.value,
    ) as Uint8Array;
    const domain = new TextEncoder().encode("exclusion-policy-keyset/v1");
    const separator = Uint8Array.from([0]);
    const payloadBytes = new TextEncoder().encode(KEYSET_FIXTURE.payload);
    const message = new Uint8Array(domain.length + 1 + payloadBytes.length);
    message.set(domain, 0);
    message.set(separator, domain.length);
    message.set(payloadBytes, domain.length + 1);

    expect(await verifyDetachedEd25519(message, signature, publicKey)).toBe(true);
    const modified = message.slice();
    const flipIndex = modified.length - 2;
    modified[flipIndex] = (modified[flipIndex] ?? 0) ^ 0x01;
    expect(await verifyDetachedEd25519(modified, signature, publicKey)).toBe(false);
    const wrongKey = decodeBase64UrlWithoutPadding(
      KEYSET_FIXTURE.signing_public_key.slice(0, 42) + "A",
    );
    expect(wrongKey).not.toBeNull();
    expect(await verifyDetachedEd25519(message, signature, wrongKey as Uint8Array)).toBe(false);
    expect(await verifyDetachedEd25519(message, signature.slice(0, 63), publicKey)).toBe(false);
    expect(
      await verifyDetachedEd25519(message, signature, Uint8Array.from(new Array(32).fill(1))),
    ).toBe(false);
  });
});

async function initialKeysetOfCurrentKey(): Promise<PolicyKeysetEnvelope> {
  const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
  const payload = keysetPayload([keysetKeyPayload(currentKey, "current")], {
    keysetRevision: 1,
    parentKeysetRevision: null,
    createdAt: "2026-08-17T09:00:00.000000Z",
  });
  return buildKeysetEnvelope(payload, [currentKey]);
}

describe("keyset envelope schema validation", () => {
  it("rejects unknown members before any canonicalization", () => {
    const payload = parseClosedJson(KEYSET_FIXTURE.payload, { maximumBytes: 64 * 1024 });
    expect(
      schemaRejection({
        payload,
        payload_sha256: KEYSET_FIXTURE.payload_sha256,
        signatures: [KEYSET_FIXTURE.signature],
        extra_member: 1,
      }),
    ).toBe("policy_payload_schema_invalid");
  });

  it("rejects floats and malformed UUIDs inside the payload", () => {
    expect(
      schemaRejection({
        payload: {
          ...fixtureKeysetEnvelope().payload,
          keyset_revision: 1.5,
        },
        payload_sha256: KEYSET_FIXTURE.payload_sha256,
        signatures: [KEYSET_FIXTURE.signature],
      }),
    ).toBe("policy_payload_schema_invalid");
    expect(
      schemaRejection({
        payload: {
          ...fixtureKeysetEnvelope().payload,
          workspace_id: "not-a-uuid",
        },
        payload_sha256: KEYSET_FIXTURE.payload_sha256,
        signatures: [KEYSET_FIXTURE.signature],
      }),
    ).toBe("policy_payload_schema_invalid");
  });
});

describe("verifyKeysetChain", () => {
  it("accepts self-signed revision 1 inside the onboarding boundary", async () => {
    const initial = await initialKeysetOfCurrentKey();
    const outcome = await verifyKeysetChain({
      envelopes: [initial],
      trustedKeyset: null,
      trustedWorkspaceId: null,
      allowInitialTrust: true,
    });
    expect(outcome.acceptedKeyset.payload.keyset_revision).toBe(1);
    expect(outcome.workspaceId).toBe(WORKSPACE_ID);
  });

  it("rejects the same revision 1 bytes outside the onboarding boundary", async () => {
    const initial = await initialKeysetOfCurrentKey();
    expect(
      await chainRejection({
        envelopes: [initial],
        trustedKeyset: null,
        trustedWorkspaceId: null,
        allowInitialTrust: false,
      }),
    ).toBe("policy_onboarding_boundary_violation");
  });

  it("accepts one rotation through a cross-signed keyset", async () => {
    const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const stagedKey = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const initial = await initialKeysetOfCurrentKey();
    const rotated = await buildKeysetEnvelope(
      keysetPayload(
        [keysetKeyPayload(currentKey, "staged"), keysetKeyPayload(stagedKey, "current")],
        { keysetRevision: 2, parentKeysetRevision: 1, createdAt: LATER_CREATED_AT },
      ),
      [currentKey, stagedKey],
    );
    const outcome = await verifyKeysetChain({
      envelopes: [rotated],
      trustedKeyset: initial,
      trustedWorkspaceId: WORKSPACE_ID,
      allowInitialTrust: false,
    });
    expect(outcome.acceptedKeyset.payload.keyset_revision).toBe(2);
    expect(
      outcome.acceptedKeyset.payload.keys.find((key) => key.state === "current")?.key_id,
    ).toBe(stagedKey.keyId);
  });

  it("accepts multiple rotations applied in sequence", async () => {
    const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const stagedKey = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const thirdKey = await deriveTestSigningKey(ROTATION_THIRD_SEED);
    const initial = await initialKeysetOfCurrentKey();
    const second = await buildKeysetEnvelope(
      keysetPayload(
        [keysetKeyPayload(currentKey, "staged"), keysetKeyPayload(stagedKey, "current")],
        { keysetRevision: 2, parentKeysetRevision: 1, createdAt: LATER_CREATED_AT },
      ),
      [currentKey, stagedKey],
    );
    const third = await buildKeysetEnvelope(
      keysetPayload(
        [
          keysetKeyPayload(currentKey, "retired"),
          keysetKeyPayload(stagedKey, "staged"),
          keysetKeyPayload(thirdKey, "current"),
        ],
        { keysetRevision: 3, parentKeysetRevision: 2, createdAt: LATER_CREATED_AT },
      ),
      [stagedKey, thirdKey],
    );
    const outcome = await verifyKeysetChain({
      envelopes: [second, third],
      trustedKeyset: initial,
      trustedWorkspaceId: WORKSPACE_ID,
      allowInitialTrust: false,
    });
    expect(outcome.acceptedKeyset.payload.keyset_revision).toBe(3);
  });

  it("rejects a rotation keyset that carries no current key", async () => {
    const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const stagedKey = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const initial = await initialKeysetOfCurrentKey();
    const noCurrent = await buildKeysetEnvelope(
      keysetPayload(
        [keysetKeyPayload(currentKey, "staged"), keysetKeyPayload(stagedKey, "staged")],
        { keysetRevision: 2, parentKeysetRevision: 1, createdAt: LATER_CREATED_AT },
      ),
      [currentKey],
    );
    expect(
      await chainRejection({
        envelopes: [noCurrent],
        trustedKeyset: initial,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_keyset_current_invalid");
  });

  it("stops rotation on an unknown parent gap", async () => {
    const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const stagedKey = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const initial = await initialKeysetOfCurrentKey();
    const skipped = await buildKeysetEnvelope(
      keysetPayload(
        [keysetKeyPayload(currentKey, "retired"), keysetKeyPayload(stagedKey, "current")],
        { keysetRevision: 3, parentKeysetRevision: 2, createdAt: LATER_CREATED_AT },
      ),
      [currentKey, stagedKey],
    );
    expect(
      await chainRejection({
        envelopes: [skipped],
        trustedKeyset: initial,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_keyset_chain_gap");
  });

  it("rejects a keyset downgrade and same-revision different bytes", async () => {
    const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const stagedKey = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const initial = await initialKeysetOfCurrentKey();
    const second = await buildKeysetEnvelope(
      keysetPayload(
        [keysetKeyPayload(currentKey, "staged"), keysetKeyPayload(stagedKey, "current")],
        { keysetRevision: 2, parentKeysetRevision: 1, createdAt: LATER_CREATED_AT },
      ),
      [currentKey, stagedKey],
    );
    // Trusted at revision 2; a later fetch replays revision 1.
    expect(
      await chainRejection({
        envelopes: [initial],
        trustedKeyset: second,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_keyset_downgrade");
    // Same revision number, different bytes.
    const conflicting = await buildKeysetEnvelope(
      keysetPayload([keysetKeyPayload(currentKey, "current")], {
        keysetRevision: 2,
        parentKeysetRevision: 1,
        createdAt: LATER_CREATED_AT,
      }),
      [currentKey],
    );
    expect(
      await chainRejection({
        envelopes: [conflicting],
        trustedKeyset: second,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_keyset_conflict");
  });

  it("skips an identical replay of the trusted revision", async () => {
    const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const stagedKey = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const initial = await initialKeysetOfCurrentKey();
    const identical = await initialKeysetOfCurrentKey();
    const second = await buildKeysetEnvelope(
      keysetPayload(
        [keysetKeyPayload(currentKey, "staged"), keysetKeyPayload(stagedKey, "current")],
        { keysetRevision: 2, parentKeysetRevision: 1, createdAt: LATER_CREATED_AT },
      ),
      [currentKey, stagedKey],
    );
    const outcome = await verifyKeysetChain({
      envelopes: [identical, second],
      trustedKeyset: initial,
      trustedWorkspaceId: WORKSPACE_ID,
      allowInitialTrust: false,
    });
    expect(outcome.acceptedKeyset.payload.keyset_revision).toBe(2);
  });

  it("rejects an unknown signing key, wrong workspace and retired-only signers", async () => {
    const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const stagedKey = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const snapshotSigner = await deriveTestSigningKey(SNAPSHOT_SIGNER_SEED);
    const initial = await initialKeysetOfCurrentKey();

    const signedByUnknownKey = await buildKeysetEnvelope(
      keysetPayload([keysetKeyPayload(currentKey, "staged"), keysetKeyPayload(stagedKey, "current")], {
        keysetRevision: 2,
        parentKeysetRevision: 1,
        createdAt: LATER_CREATED_AT,
      }),
      [snapshotSigner, stagedKey],
    );
    expect(
      await chainRejection({
        envelopes: [signedByUnknownKey],
        trustedKeyset: initial,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_signature_untrusted_key");

    const foreignWorkspace = await buildKeysetEnvelope(
      keysetPayload([keysetKeyPayload(currentKey, "current")], {
        keysetRevision: 2,
        parentKeysetRevision: 1,
        workspaceId: OTHER_WORKSPACE_ID,
        createdAt: LATER_CREATED_AT,
      }),
      [currentKey],
    );
    expect(
      await chainRejection({
        envelopes: [foreignWorkspace],
        trustedKeyset: initial,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_workspace_mismatch");

    // A keyset signed only by a key the new revision retires cannot rotate.
    const retiredOnly = await buildKeysetEnvelope(
      keysetPayload(
        [keysetKeyPayload(currentKey, "retired"), keysetKeyPayload(stagedKey, "current")],
        { keysetRevision: 2, parentKeysetRevision: 1, createdAt: LATER_CREATED_AT },
      ),
      [currentKey],
    );
    expect(
      await chainRejection({
        envelopes: [retiredOnly],
        trustedKeyset: initial,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_signature_untrusted_key");
  });

  it("requires a signature from the newly current key", async () => {
    const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const stagedKey = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const initial = await initialKeysetOfCurrentKey();
    const activation = await buildKeysetEnvelope(
      keysetPayload([keysetKeyPayload(currentKey, "staged"), keysetKeyPayload(stagedKey, "current")], {
        keysetRevision: 2,
        parentKeysetRevision: 1,
        createdAt: LATER_CREATED_AT,
      }),
      [currentKey],
    );
    expect(
      await chainRejection({
        envelopes: [activation],
        trustedKeyset: initial,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_signature_invalid");
  });

  it("rejects structural ceiling violations", async () => {
    const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const stagedKey = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const thirdKey = await deriveTestSigningKey(ROTATION_THIRD_SEED);
    const snapshotSigner = await deriveTestSigningKey(SNAPSHOT_SIGNER_SEED);
    const initial = await initialKeysetOfCurrentKey();

    const twoCurrent = await buildKeysetEnvelope(
      keysetPayload(
        [keysetKeyPayload(currentKey, "current"), keysetKeyPayload(stagedKey, "current")],
        { keysetRevision: 2, parentKeysetRevision: 1, createdAt: LATER_CREATED_AT },
      ),
      [currentKey],
    );
    expect(
      await chainRejection({
        envelopes: [twoCurrent],
        trustedKeyset: initial,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_keyset_current_invalid");

    const fiveNonRetired = await buildKeysetEnvelope(
      keysetPayload(
        [
          keysetKeyPayload(currentKey, "staged"),
          keysetKeyPayload(stagedKey, "staged"),
          keysetKeyPayload(thirdKey, "staged"),
          keysetKeyPayload(snapshotSigner, "staged"),
          keysetKeyPayload(await deriveTestSigningKey(Uint8Array.from(new Array(32).fill(7))), "current"),
        ],
        { keysetRevision: 2, parentKeysetRevision: 1, createdAt: LATER_CREATED_AT },
      ),
      [currentKey],
    );
    expect(
      await chainRejection({
        envelopes: [fiveNonRetired],
        trustedKeyset: initial,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_keyset_key_invalid");

    const mismatchedKeyId = await buildKeysetEnvelope(
      {
        ...keysetPayload([keysetKeyPayload(currentKey, "current")], {
          keysetRevision: 2,
          parentKeysetRevision: 1,
          createdAt: LATER_CREATED_AT,
        }),
        keys: [
          {
            algorithm: "Ed25519",
            key_id: stagedKey.keyId,
            public_key: keysetKeyPayload(currentKey, "current").public_key,
            state: "current",
          },
        ],
      },
      [currentKey],
    );
    expect(
      await chainRejection({
        envelopes: [mismatchedKeyId],
        trustedKeyset: initial,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_keyset_key_invalid");
  });

  it("rejects a payload whose digest does not match the canonical bytes", async () => {
    const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const stagedKey = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const initial = await initialKeysetOfCurrentKey();
    const rotated = await buildKeysetEnvelope(
      keysetPayload([keysetKeyPayload(currentKey, "staged"), keysetKeyPayload(stagedKey, "current")], {
        keysetRevision: 2,
        parentKeysetRevision: 1,
        createdAt: LATER_CREATED_AT,
      }),
      [currentKey, stagedKey],
    );
    const tampered: PolicyKeysetEnvelope = {
      ...rotated,
      payload: { ...rotated.payload, created_at: CREATED_AT },
    };
    expect(
      await chainRejection({
        envelopes: [tampered],
        trustedKeyset: initial,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_payload_hash_mismatch");
  });

  it("validates the envelope schema before verifying anything", async () => {
    const initial = await initialKeysetOfCurrentKey();
    const malformed = {
      ...initial,
      payload: { ...initial.payload, unexpected: true },
    } as unknown as PolicyKeysetEnvelope;
    expect(
      await chainRejection({
        envelopes: [malformed],
        trustedKeyset: initial,
        trustedWorkspaceId: WORKSPACE_ID,
        allowInitialTrust: false,
      }),
    ).toBe("policy_payload_schema_invalid");
  });
});
