import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { PolicyVerificationError } from "./contracts";
import type { PolicyKeysetEnvelope, SignedPolicySnapshot } from "./contracts";
import { parseClosedJson } from "./strict-json";
import {
  resolveSnapshotMonotonicity,
  validateSnapshotEnvelope,
  verifyPolicySnapshot,
} from "./snapshot";
import {
  KEYSET_CURRENT_SEED,
  KEYSET_STAGED_SEED,
  LATER_PUBLISHED_AT,
  OTHER_WORKSPACE_ID,
  POLICY_REVISION_ID,
  SECOND_POLICY_REVISION_ID,
  SNAPSHOT_SIGNER_SEED,
  WORKSPACE_ID,
  buildKeysetEnvelope,
  buildSnapshotEnvelope,
  deriveTestSigningKey,
  keysetKeyPayload,
  keysetPayload,
  snapshotPayload,
} from "./policy-signing-test-vectors";

const SNAPSHOT_FIXTURE = JSON.parse(
  readFileSync(
    new URL("../../../../tests/fixtures/exclusion_policy/snapshot-golden.json", import.meta.url),
    "utf8",
  ),
) as {
  readonly payload: string;
  readonly payload_sha256: string;
  readonly signature: { readonly algorithm: string; readonly key_id: string; readonly value: string };
  readonly signing_public_key: string;
};

const GOLDEN_RULES = [
  {
    rule_id: "018f47a0-7b00-7000-8000-000000000301",
    rule_kind: "exact_source_id",
    source_id: "018f47a0-7b00-7000-8000-000000000401",
  },
  {
    rule_id: "018f47a0-7b00-7000-8000-000000000303",
    rule_kind: "extension",
    extension: ".pdf",
  },
] as const;

function fixtureSnapshotEnvelope(): SignedPolicySnapshot {
  const payload = parseClosedJson(SNAPSHOT_FIXTURE.payload, { maximumBytes: 256 * 1024 });
  return validateSnapshotEnvelope({
    payload,
    payload_sha256: SNAPSHOT_FIXTURE.payload_sha256,
    signature: SNAPSHOT_FIXTURE.signature,
  });
}

/** Revision 1 keyset carrying the snapshot signer as its current key. */
async function snapshotSignerKeyset(): Promise<PolicyKeysetEnvelope> {
  const signer = await deriveTestSigningKey(SNAPSHOT_SIGNER_SEED);
  return buildKeysetEnvelope(
    keysetPayload([keysetKeyPayload(signer, "current")], {
      keysetRevision: 1,
      parentKeysetRevision: null,
      createdAt: "2026-08-17T09:00:00.000000Z",
    }),
    [signer],
  );
}

async function rotatedKeyset(): Promise<{
  trusted: PolicyKeysetEnvelope;
  currentSignerKey: Awaited<ReturnType<typeof deriveTestSigningKey>>;
}> {
  const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
  const stagedKey = await deriveTestSigningKey(KEYSET_STAGED_SEED);
  const trusted = await buildKeysetEnvelope(
    keysetPayload(
      [
        keysetKeyPayload(await deriveTestSigningKey(SNAPSHOT_SIGNER_SEED), "retired"),
        keysetKeyPayload(currentKey, "staged"),
        keysetKeyPayload(stagedKey, "current"),
      ],
      { keysetRevision: 2, parentKeysetRevision: 1, createdAt: "2026-08-17T11:00:00.000000Z" },
    ),
    [currentKey, stagedKey],
  );
  return { trusted, currentSignerKey: stagedKey };
}

async function verificationRejection(
  input: Parameters<typeof verifyPolicySnapshot>[0],
): Promise<string> {
  try {
    await verifyPolicySnapshot(input);
  } catch (error) {
    if (error instanceof PolicyVerificationError) {
      return error.reason;
    }
    throw error;
  }
  throw new Error("expected snapshot verification to reject the input");
}

function schemaRejection(value: unknown): string {
  try {
    validateSnapshotEnvelope(value);
  } catch (error) {
    if (error instanceof PolicyVerificationError) {
      return error.reason;
    }
    throw error;
  }
  throw new Error("expected snapshot envelope validation to reject the value");
}

describe("snapshot envelope schema validation", () => {
  it("accepts the golden fixture shape", () => {
    const envelope = fixtureSnapshotEnvelope();
    expect(envelope.payload.contract).toBe("exclusion_policy_snapshot/v1");
    expect(envelope.payload.revision_number).toBe(1);
    expect(envelope.payload.rules.length).toBe(7);
  });

  it("rejects unknown members, wrong operand kinds and oversized rule sets", () => {
    const envelope = fixtureSnapshotEnvelope();
    expect(
      schemaRejection({
        ...envelope,
        payload: { ...envelope.payload, extra: 1 },
      }),
    ).toBe("policy_payload_schema_invalid");
    expect(
      schemaRejection({
        ...envelope,
        payload: {
          ...envelope.payload,
          rules: [...envelope.payload.rules, { ...GOLDEN_RULES[0], folder_prefix: "x" }],
        },
      }),
    ).toBe("policy_payload_schema_invalid");
    expect(
      schemaRejection({
        ...envelope,
        payload: {
          ...envelope.payload,
          rules: Array.from({ length: 257 }, () => GOLDEN_RULES[0]),
        },
      }),
    ).toBe("policy_payload_schema_invalid");
    expect(
      schemaRejection({
        ...envelope,
        payload: { ...envelope.payload, default_decision: "denied" },
      }),
    ).toBe("policy_payload_schema_invalid");
  });
});

describe("verifyPolicySnapshot", () => {
  it("verifies the golden snapshot against its signing key's keyset", async () => {
    const outcome = await verifyPolicySnapshot({
      envelope: fixtureSnapshotEnvelope(),
      trustedKeyset: await snapshotSignerKeyset(),
      expectedWorkspaceId: WORKSPACE_ID,
    });
    expect(new TextDecoder().decode(outcome.payloadBytes)).toBe(SNAPSHOT_FIXTURE.payload);
    expect(outcome.payloadSha256).toBe(SNAPSHOT_FIXTURE.payload_sha256);
  });

  it("accepts a snapshot signed by the newly rotated current key", async () => {
    const { trusted, currentSignerKey } = await rotatedKeyset();
    const envelope = await buildSnapshotEnvelope(
      snapshotPayload([...GOLDEN_RULES], { revisionNumber: 2, policyRevisionId: SECOND_POLICY_REVISION_ID, parentPolicyRevisionId: POLICY_REVISION_ID, publishedAt: LATER_PUBLISHED_AT }),
      currentSignerKey,
    );
    const outcome = await verifyPolicySnapshot({
      envelope,
      trustedKeyset: trusted,
      expectedWorkspaceId: WORKSPACE_ID,
    });
    expect(outcome.envelope.payload.revision_number).toBe(2);
  });

  it("rejects an unknown signing key before touching the signature", async () => {
    // A keyset that does not carry the snapshot signer at all: the signer is
    // genuinely unknown, not merely retired trusted history.
    const unrelatedKey = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const trusted = await buildKeysetEnvelope(
      keysetPayload([keysetKeyPayload(unrelatedKey, "current")], {
        keysetRevision: 2,
        parentKeysetRevision: 1,
        createdAt: "2026-08-17T11:00:00.000000Z",
      }),
      [unrelatedKey],
    );
    expect(
      await verificationRejection({
        envelope: fixtureSnapshotEnvelope(),
        trustedKeyset: trusted,
        expectedWorkspaceId: WORKSPACE_ID,
      }),
    ).toBe("policy_signature_untrusted_key");
  });

  it("rejects a workspace mismatch, hash mismatch and modified byte", async () => {
    const trusted = await snapshotSignerKeyset();
    const signer = await deriveTestSigningKey(SNAPSHOT_SIGNER_SEED);
    const wrongWorkspace = await buildSnapshotEnvelope(
      snapshotPayload([...GOLDEN_RULES], {
        revisionNumber: 2,
        workspaceId: OTHER_WORKSPACE_ID,
      }),
      signer,
    );
    expect(
      await verificationRejection({
        envelope: wrongWorkspace,
        trustedKeyset: trusted,
        expectedWorkspaceId: WORKSPACE_ID,
      }),
    ).toBe("policy_workspace_mismatch");

    const genuine = await buildSnapshotEnvelope(
      snapshotPayload([...GOLDEN_RULES], { revisionNumber: 2 }),
      signer,
    );
    const hashTampered: SignedPolicySnapshot = {
      ...genuine,
      payload: { ...genuine.payload, revision_number: 3 },
    };
    expect(
      await verificationRejection({
        envelope: hashTampered,
        trustedKeyset: trusted,
        expectedWorkspaceId: WORKSPACE_ID,
      }),
    ).toBe("policy_payload_hash_mismatch");

    const signatureTampered: SignedPolicySnapshot = {
      ...genuine,
      signature: { ...genuine.signature, value: genuine.signature.value.slice(0, 85) + "A" },
    };
    expect(
      await verificationRejection({
        envelope: signatureTampered,
        trustedKeyset: trusted,
        expectedWorkspaceId: WORKSPACE_ID,
      }),
    ).toBe("policy_signature_invalid");
  });

  it("rejects a snapshot pinned to different evaluator semantics", async () => {
    const signer = await deriveTestSigningKey(SNAPSHOT_SIGNER_SEED);
    const envelope = await buildSnapshotEnvelope(
      {
        ...snapshotPayload([...GOLDEN_RULES], { revisionNumber: 2 }),
        evaluator_contract_sha256: "0".repeat(64),
      },
      signer,
    );
    expect(
      await verificationRejection({
        envelope,
        trustedKeyset: await snapshotSignerKeyset(),
        expectedWorkspaceId: WORKSPACE_ID,
      }),
    ).toBe("policy_evaluator_contract_mismatch");
  });
});

describe("resolveSnapshotMonotonicity", () => {
  const base: SignedPolicySnapshot = {
    payload: {
      ...snapshotPayload([...GOLDEN_RULES], { revisionNumber: 1 }),
    },
    payload_sha256: "a".repeat(64),
    signature: {
      algorithm: "Ed25519",
      key_id: "ed25519-sha256-" + "a".repeat(43),
      value: "b".repeat(86),
    },
  };

  it("accepts a greater revision and an identical replay", () => {
    expect(resolveSnapshotMonotonicity(base, null)).toBe("accept");
    expect(
      resolveSnapshotMonotonicity(
        { ...base, payload: { ...base.payload, revision_number: 2 } },
        base,
      ),
    ).toBe("accept");
    expect(resolveSnapshotMonotonicity(base, base)).toBe("identical");
  });

  it("conflicts on same revision with a different identity and downgrades", () => {
    expect(
      resolveSnapshotMonotonicity(
        {
          ...base,
          payload: { ...base.payload, policy_revision_id: SECOND_POLICY_REVISION_ID },
        },
        base,
      ),
    ).toBe("conflict");
    expect(
      resolveSnapshotMonotonicity(
        { ...base, payload_sha256: "c".repeat(64) },
        base,
      ),
    ).toBe("conflict");
    expect(
      resolveSnapshotMonotonicity(base, {
        ...base,
        payload: { ...base.payload, revision_number: 5 },
      }),
    ).toBe("downgrade");
  });
});
