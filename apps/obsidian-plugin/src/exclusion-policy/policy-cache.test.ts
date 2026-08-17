import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import type { AcceptedPolicyState, SignedPolicySnapshot } from "./contracts";
import {
  POLICY_CACHE_RECORD_CONTRACT,
  buildPolicyCacheRecord,
  persistAcceptedPolicyState,
  readAcceptedPolicyStateFromRecord,
} from "./policy-cache";
import type { PolicyCacheAdapter } from "./policy-cache";
import { parseClosedJson } from "./strict-json";
import { validateKeysetEnvelope } from "./keyset";
import { validateSnapshotEnvelope } from "./snapshot";
import {
  KEYSET_CURRENT_SEED,
  SNAPSHOT_SIGNER_SEED,
  WORKSPACE_ID,
  buildKeysetEnvelope,
  buildSnapshotEnvelope,
  deriveTestSigningKey,
  keysetKeyPayload,
  keysetPayload,
  snapshotPayload,
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
};

const SNAPSHOT_FIXTURE = JSON.parse(
  readFileSync(
    new URL("../../../../tests/fixtures/exclusion_policy/snapshot-golden.json", import.meta.url),
    "utf8",
  ),
) as {
  readonly payload: string;
  readonly payload_sha256: string;
  readonly signature: { readonly algorithm: string; readonly key_id: string; readonly value: string };
};

function goldenSnapshotEnvelope(): SignedPolicySnapshot {
  return validateSnapshotEnvelope({
    payload: parseClosedJson(SNAPSHOT_FIXTURE.payload, { maximumBytes: 256 * 1024 }),
    payload_sha256: SNAPSHOT_FIXTURE.payload_sha256,
    signature: SNAPSHOT_FIXTURE.signature,
  });
}

/** A consistent accepted state: the golden snapshot under its signer's keyset. */
async function acceptedState(): Promise<AcceptedPolicyState> {
  const signer = await deriveTestSigningKey(SNAPSHOT_SIGNER_SEED);
  const keyset = await buildKeysetEnvelope(
    keysetPayload([keysetKeyPayload(signer, "current")], {
      keysetRevision: 1,
      parentKeysetRevision: null,
      createdAt: "2026-08-17T09:00:00.000000Z",
    }),
    [signer],
  );
  return {
    workspaceId: WORKSPACE_ID,
    revisionNumber: 1,
    keysetSequence: 1,
    keysetEnvelope: keyset,
    snapshotEnvelope: goldenSnapshotEnvelope(),
  };
}

function memoryAdapter(initial: unknown = null): PolicyCacheAdapter & {
  writtenRecords: unknown[];
} {
  let stored: unknown = initial;
  return {
    writtenRecords: [],
    async readPolicyCacheRecord() {
      return stored;
    },
    async writePolicyCacheRecord(record: unknown) {
      stored = record;
      this.writtenRecords.push(record);
    },
  };
}

describe("policy cache record", () => {
  it("round-trips one versioned record through the adapter", async () => {
    const state = await acceptedState();
    const record = buildPolicyCacheRecord(state);
    expect(record).toMatchObject({
      contract: POLICY_CACHE_RECORD_CONTRACT,
      workspace_id: WORKSPACE_ID,
      keyset_sequence: 1,
      revision_number: 1,
    });
    const restored = await readAcceptedPolicyStateFromRecord(record);
    expect(restored).not.toBeNull();
    expect(restored?.workspaceId).toBe(WORKSPACE_ID);
    expect(restored?.keysetEnvelope.payload.keyset_revision).toBe(1);
    expect(restored?.snapshotEnvelope.payload_sha256).toBe(SNAPSHOT_FIXTURE.payload_sha256);
    expect(restored?.revisionNumber).toBe(1);
    expect(restored?.keysetSequence).toBe(1);
  });

  it("persists the accepted state as exactly one record and verifies the readback", async () => {
    const adapter = memoryAdapter();
    await persistAcceptedPolicyState(await acceptedState(), adapter);
    expect(adapter.writtenRecords.length).toBe(1);
    expect(await readAcceptedPolicyStateFromRecord(adapter.writtenRecords[0])).not.toBeNull();
  });

  it("fails closed when the write or the readback is broken", async () => {
    const state = await acceptedState();
    const failingWrite: PolicyCacheAdapter = {
      async readPolicyCacheRecord() {
        return null;
      },
      async writePolicyCacheRecord() {
        throw new Error("disk full");
      },
    };
    await expect(persistAcceptedPolicyState(state, failingWrite)).rejects.toMatchObject({
      reason: "policy_cache_write_failed",
    });

    const mismatchingReadback: PolicyCacheAdapter = {
      async readPolicyCacheRecord() {
        return { anything: "else" };
      },
      async writePolicyCacheRecord() {
        return;
      },
    };
    await expect(persistAcceptedPolicyState(state, mismatchingReadback)).rejects.toMatchObject({
      reason: "policy_cache_readback_mismatch",
    });
  });

  it("treats a malformed persisted record as absent, never as trusted", async () => {
    const wellFormed = buildPolicyCacheRecord(await acceptedState());
    const malformedValues = [
      null,
      1,
      "text",
      {},
      [],
      { ...wellFormed, contract: "other/v9" },
      { ...wellFormed, keyset_sequence: "one" },
      { ...wellFormed, revision_number: 1.5 },
    ];
    for (const malformed of malformedValues) {
      expect(await readAcceptedPolicyStateFromRecord(malformed)).toBeNull();
    }
  });

  it("rejects envelopes inside the record whose digest was corrupted", async () => {
    const currentKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const keyset = await buildKeysetEnvelope(
      keysetPayload([keysetKeyPayload(currentKey, "current")], {
        keysetRevision: 1,
        parentKeysetRevision: null,
      }),
      [currentKey],
    );
    const snapshot = await buildSnapshotEnvelope(
      snapshotPayload([], { revisionNumber: 1 }),
      currentKey,
    );
    const good = buildPolicyCacheRecord({
      workspaceId: WORKSPACE_ID,
      revisionNumber: 1,
      keysetSequence: 1,
      keysetEnvelope: keyset,
      snapshotEnvelope: snapshot,
    });
    expect(await readAcceptedPolicyStateFromRecord(good)).not.toBeNull();
    const corrupted = JSON.parse(JSON.stringify(good)) as Record<string, unknown>;
    const keysetMember = corrupted["keyset_envelope"] as Record<string, unknown>;
    keysetMember["payload_sha256"] = "0".repeat(64);
    expect(await readAcceptedPolicyStateFromRecord(corrupted)).toBeNull();
  });

  it("carries no secret, credential or diagnostic material", async () => {
    const serialized = JSON.stringify(buildPolicyCacheRecord(await acceptedState()));
    for (const forbidden of [
      "secret",
      "credential",
      "refresh",
      "access_token",
      "private_key",
      "diagnostic",
    ]) {
      expect(serialized).not.toContain(forbidden);
    }
  });
});

describe("policy cache consistency", () => {
  it("rejects a record whose snapshot is not signed by a recorded key", async () => {
    const signer = await deriveTestSigningKey(SNAPSHOT_SIGNER_SEED);
    const otherKey = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const keyset = await buildKeysetEnvelope(
      keysetPayload([keysetKeyPayload(otherKey, "current")], {
        keysetRevision: 1,
        parentKeysetRevision: null,
      }),
      [otherKey],
    );
    const snapshot = await buildSnapshotEnvelope(snapshotPayload([], { revisionNumber: 1 }), signer);
    const record = buildPolicyCacheRecord({
      workspaceId: WORKSPACE_ID,
      revisionNumber: 1,
      keysetSequence: 1,
      keysetEnvelope: keyset,
      snapshotEnvelope: snapshot,
    });
    expect(await readAcceptedPolicyStateFromRecord(record)).toBeNull();
  });

  it("keeps the golden keyset fixture bytes intact inside the record path", async () => {
    const envelope = validateKeysetEnvelope({
      payload: parseClosedJson(KEYSET_FIXTURE.payload, { maximumBytes: 64 * 1024 }),
      payload_sha256: KEYSET_FIXTURE.payload_sha256,
      signatures: [KEYSET_FIXTURE.signature],
    });
    expect(envelope.payload_sha256).toBe(KEYSET_FIXTURE.payload_sha256);
    expect(envelope.payload.keyset_revision).toBe(2);
  });
});
