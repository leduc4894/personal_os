import { describe, expect, it } from "vitest";

import type { PolicyHttpRequest, PolicyHttpResponse } from "./contracts";
import { PolicySession } from "./policy-session";
import type { PolicyCacheAdapter } from "./policy-cache";
import { buildPolicyCacheRecord } from "./policy-cache";
import { parseClosedJson } from "./strict-json";
import { validateKeysetEnvelope } from "./keyset";
import {
  KEYSET_CURRENT_SEED,
  KEYSET_STAGED_SEED,
  LATER_CREATED_AT,
  OTHER_WORKSPACE_ID,
  LATER_PUBLISHED_AT,
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
  wireBody,
} from "./policy-signing-test-vectors";
import type { PolicyKeysetEnvelope } from "./contracts";
import type { TestSigningKey } from "./policy-signing-test-vectors";

const RULES = [
  {
    rule_id: "018f47a0-7b00-7000-8000-000000000303",
    rule_kind: "extension",
    extension: ".pdf",
  },
] as const;

interface Harness {
  readonly session: PolicySession;
  readonly requests: PolicyHttpRequest[];
  readonly served: Map<string, () => PolicyHttpResponse | Error>;
  readonly cacheStore: { record: unknown; writes: number };
  failWrites: () => void;
  states: string[];
}

function jsonResponse(bodyText: string, etag: string | null = null): PolicyHttpResponse {
  return { status: 200, bodyText, etag };
}

function createHarness(options: {
  initialRecord?: unknown;
  token?: string | null;
}): Harness {
  const requests: PolicyHttpRequest[] = [];
  const served = new Map<string, () => PolicyHttpResponse | Error>();
  const cacheStore: { record: unknown; writes: number } = {
    record: options.initialRecord ?? null,
    writes: 0,
  };
  let failWrites = false;
  const states: string[] = [];
  const adapter: PolicyCacheAdapter = {
    async readPolicyCacheRecord() {
      return cacheStore.record;
    },
    async writePolicyCacheRecord(record: unknown) {
      if (failWrites) {
        throw new Error("persist failed");
      }
      cacheStore.record = record;
      cacheStore.writes += 1;
    },
  };
  const session = new PolicySession({
    http: async (request) => {
      requests.push(request);
      const handler = served.get(request.url);
      if (handler === undefined) {
        throw new Error(`unexpected request ${request.url}`);
      }
      const outcome = handler();
      if (outcome instanceof Error) {
        throw outcome;
      }
      return outcome;
    },
    resolveOrigin: () => "https://vault.example.com",
    getAccessToken: () => options.token ?? "at1-fixed-access-credential",
    cache: adapter,
    onStateChange: (state) => states.push(state),
  });
  return {
    session,
    requests,
    served,
    cacheStore,
    failWrites: () => {
      failWrites = true;
    },
    states,
  };
}

function keysetsUrl(afterRevision: number): string {
  return `https://vault.example.com/api/sync/exclusion-policy/keysets?after_keyset_revision=${afterRevision}`;
}

const SNAPSHOT_URL = "https://vault.example.com/api/sync/exclusion-policy/snapshot";

async function initialTrustMaterial(): Promise<{
  keyset: PolicyKeysetEnvelope;
  signer: TestSigningKey;
  snapshotBody: string;
  keysetsBody: string;
}> {
  const signer = await deriveTestSigningKey(SNAPSHOT_SIGNER_SEED);
  const keyset = await buildKeysetEnvelope(
    keysetPayload([keysetKeyPayload(signer, "current")], {
      keysetRevision: 1,
      parentKeysetRevision: null,
      createdAt: "2026-08-17T09:00:00.000000Z",
    }),
    [signer],
  );
  const snapshot = await buildSnapshotEnvelope(
    snapshotPayload([...RULES], { revisionNumber: 1 }),
    signer,
  );
  return {
    keyset,
    signer,
    snapshotBody: wireBody({
      payload: snapshot.payload,
      payload_sha256: snapshot.payload_sha256,
      signature: snapshot.signature,
    }),
    keysetsBody: wireBody({ has_more: false, keysets: [keyset] }),
  };
}

async function rotationMaterial(previousSigner: TestSigningKey): Promise<{
  keyset: PolicyKeysetEnvelope;
  newSigner: TestSigningKey;
  snapshotBody: string;
  keysetsBody: string;
}> {
  const newSigner = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
  const keyset = await buildKeysetEnvelope(
    keysetPayload(
      [keysetKeyPayload(previousSigner, "staged"), keysetKeyPayload(newSigner, "current")],
      { keysetRevision: 2, parentKeysetRevision: 1, createdAt: LATER_CREATED_AT },
    ),
    [previousSigner, newSigner],
  );
  const snapshot = await buildSnapshotEnvelope(
    snapshotPayload([...RULES], {
      revisionNumber: 2,
      policyRevisionId: SECOND_POLICY_REVISION_ID,
      parentPolicyRevisionId: POLICY_REVISION_ID,
      publishedAt: LATER_PUBLISHED_AT,
    }),
    newSigner,
  );
  return {
    keyset,
    newSigner,
    snapshotBody: wireBody({
      payload: snapshot.payload,
      payload_sha256: snapshot.payload_sha256,
      signature: snapshot.signature,
    }),
    keysetsBody: wireBody({ has_more: false, keysets: [keyset] }),
  };
}

describe("PolicySession initial onboarding trust", () => {
  it("accepts self-signed keyset revision 1 immediately after authenticated onboarding", async () => {
    const material = await initialTrustMaterial();
    const harness = createHarness({});
    harness.served.set(keysetsUrl(0), () => jsonResponse(material.keysetsBody));
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(material.snapshotBody, '"etag-one"'));

    await harness.session.restoreFromCache();
    expect(harness.session.state).toBe("policy_not_initialized");
    await harness.session.adoptOnboardingTrust();

    expect(harness.session.state).toBe("policy_ready");
    expect(harness.session.acceptedState?.workspaceId).toBe(WORKSPACE_ID);
    expect(harness.session.acceptedState?.keysetSequence).toBe(1);
    expect(harness.cacheStore.writes).toBe(1);
    expect(harness.requests[0]?.headers["authorization"]).toBe(
      "Bearer at1-fixed-access-credential",
    );
  });

  it("denies the same revision 1 bytes through the regular refresh boundary", async () => {
    const material = await initialTrustMaterial();
    const harness = createHarness({});
    harness.served.set(keysetsUrl(0), () => jsonResponse(material.keysetsBody));
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(material.snapshotBody, '"etag-one"'));
    await harness.session.restoreFromCache();
    await expect(harness.session.refresh()).rejects.toMatchObject({
      reason: "policy_onboarding_boundary_violation",
    });
    expect(harness.session.state).toBe("policy_integrity_failed");
    expect(harness.session.acceptedState).toBeNull();
    expect(harness.cacheStore.writes).toBe(0);
  });

  it("first-run offline with no cache denies and keeps the deny decision", async () => {
    const harness = createHarness({});
    await harness.session.restoreFromCache();
    expect(harness.session.state).toBe("policy_not_initialized");
    harness.served.set(SNAPSHOT_URL, () => new Error("network down"));
    await expect(harness.session.refresh()).rejects.toMatchObject({
      reason: "policy_network_unavailable",
    });
    expect(harness.session.state).toBe("policy_not_initialized");
    expect(
      harness.session.evaluate({ normalizedLocator: "a/b.md" }),
    ).toEqual({ raw: "indeterminate", enforced: "excluded" });
  });
});

describe("PolicySession refresh", () => {
  async function onboardedHarness(): Promise<{
    harness: Harness;
    material: Awaited<ReturnType<typeof initialTrustMaterial>>;
  }> {
    const material = await initialTrustMaterial();
    const harness = createHarness({});
    harness.served.set(keysetsUrl(0), () => jsonResponse(material.keysetsBody));
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(material.snapshotBody, '"etag-one"'));
    await harness.session.restoreFromCache();
    await harness.session.adoptOnboardingTrust();
    return { harness, material };
  }

  it("answers 304 with the cached snapshot and writes nothing", async () => {
    const { harness } = await onboardedHarness();
    const writesBefore = harness.cacheStore.writes;
    const requestsBefore = harness.requests.length;
    harness.served.set(SNAPSHOT_URL, () => ({ status: 304, bodyText: "", etag: '"etag-one"' }));
    await harness.session.refresh();
    expect(harness.session.state).toBe("policy_ready");
    expect(harness.cacheStore.writes).toBe(writesBefore);
    const snapshotRequest = harness.requests
      .slice(requestsBefore)
      .find((request) => request.url === SNAPSHOT_URL);
    expect(snapshotRequest?.headers["if-none-match"]).toBe(
      `"${harness.session.acceptedState?.snapshotEnvelope.payload_sha256}"`,
    );
  });

  it("fetches the keyset chain before a snapshot signed by an unknown key", async () => {
    const { harness, material } = await onboardedHarness();
    const rotation = await rotationMaterial(material.signer);
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(rotation.snapshotBody, '"etag-two"'));
    harness.served.set(keysetsUrl(1), () => jsonResponse(rotation.keysetsBody));
    await harness.session.refresh();

    expect(harness.session.state).toBe("policy_ready");
    expect(harness.session.acceptedState?.keysetSequence).toBe(2);
    expect(harness.session.acceptedState?.revisionNumber).toBe(2);
    // The refresh-phase keyset fetch happens only AFTER the snapshot request
    // revealed the unknown signing key.
    const refreshKeysetIndex = harness.requests.findIndex(
      (request) => request.url === keysetsUrl(1),
    );
    expect(refreshKeysetIndex).toBeGreaterThan(-1);
    const precedingSnapshot = harness.requests
      .slice(0, refreshKeysetIndex)
      .some((request) => request.url === SNAPSHOT_URL);
    expect(precedingSnapshot).toBe(true);
  });

  it("maps a transient keyset-chain fetch failure to a network failure", async () => {
    const { harness, material } = await onboardedHarness();
    const recordBefore = harness.cacheStore.record;
    const rotation = await rotationMaterial(material.signer);
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(rotation.snapshotBody, '"etag-two"'));
    harness.served.set(keysetsUrl(1), () => ({ status: 503, bodyText: "", etag: null }));
    await expect(harness.session.refresh()).rejects.toMatchObject({
      reason: "policy_network_unavailable",
    });
    expect(harness.session.state).toBe("policy_offline_cached");
    expect(harness.cacheStore.record).toBe(recordBefore);
  });

  it("replays an identical snapshot without rewriting the cache", async () => {
    const { harness, material } = await onboardedHarness();
    const writesBefore = harness.cacheStore.writes;
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(material.snapshotBody, '"etag-one"'));
    await harness.session.refresh();
    expect(harness.session.state).toBe("policy_ready");
    expect(harness.cacheStore.writes).toBe(writesBefore);
  });

  it("preserves the previous valid cache when the new snapshot is tampered", async () => {
    const { harness, material } = await onboardedHarness();
    const recordBefore = harness.cacheStore.record;
    const tampered = JSON.parse(material.snapshotBody) as {
      data: { payload: { revision_number: number } };
    };
    tampered.data.payload.revision_number = 9;
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(JSON.stringify(tampered), '"etag-bad"'));
    await expect(harness.session.refresh()).rejects.toMatchObject({
      reason: "policy_payload_hash_mismatch",
    });
    expect(harness.session.state).toBe("policy_integrity_failed");
    expect(harness.cacheStore.record).toBe(recordBefore);
    expect(harness.session.acceptedState?.revisionNumber).toBe(1);
    // The last trusted snapshot still classifies locally.
    expect(
      harness.session.evaluate({ normalizedLocator: "x/note.pdf" }),
    ).toEqual({ raw: "excluded", enforced: "excluded" });
  });

  it("treats a same-revision different-bytes snapshot as an integrity failure", async () => {
    const { harness, material } = await onboardedHarness();
    const recordBefore = harness.cacheStore.record;
    const conflicting = await buildSnapshotEnvelope(
      snapshotPayload([...RULES], {
        revisionNumber: 1,
        policyRevisionId: SECOND_POLICY_REVISION_ID,
        publishedAt: LATER_PUBLISHED_AT,
      }),
      material.signer,
    );
    harness.served.set(SNAPSHOT_URL, () =>
      jsonResponse(
        wireBody({
          payload: conflicting.payload,
          payload_sha256: conflicting.payload_sha256,
          signature: conflicting.signature,
        }),
        '"etag-conflict"',
      ),
    );
    await expect(harness.session.refresh()).rejects.toMatchObject({
      reason: "policy_snapshot_conflict",
    });
    expect(harness.session.state).toBe("policy_integrity_failed");
    expect(harness.cacheStore.record).toBe(recordBefore);
  });

  it("treats a lower snapshot revision as a downgrade", async () => {
    const { harness, material } = await onboardedHarness();
    const recordBefore = harness.cacheStore.record;
    // Advance to revision 2 through a rotation first.
    const rotation = await rotationMaterial(material.signer);
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(rotation.snapshotBody, '"etag-two"'));
    harness.served.set(keysetsUrl(1), () => jsonResponse(rotation.keysetsBody));
    await harness.session.refresh();
    expect(harness.session.acceptedState?.revisionNumber).toBe(2);
    // Then the server replays the old revision-1 snapshot.
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(material.snapshotBody, '"etag-one"'));
    await expect(harness.session.refresh()).rejects.toMatchObject({
      reason: "policy_snapshot_downgrade",
    });
    expect(harness.session.state).toBe("policy_integrity_failed");
    expect(harness.cacheStore.record).not.toBe(recordBefore);
    expect(harness.session.acceptedState?.revisionNumber).toBe(2);
  });

  it("keeps the valid cache and reports offline when the network fails", async () => {
    const { harness } = await onboardedHarness();
    const recordBefore = harness.cacheStore.record;
    harness.served.set(SNAPSHOT_URL, () => new Error("network down"));
    await expect(harness.session.refresh()).rejects.toMatchObject({
      reason: "policy_network_unavailable",
    });
    expect(harness.session.state).toBe("policy_offline_cached");
    expect(harness.cacheStore.record).toBe(recordBefore);
  });

  it("retains the prior record when persisting the accepted update fails", async () => {
    const { harness, material } = await onboardedHarness();
    const rotation = await rotationMaterial(material.signer);
    const recordBefore = harness.cacheStore.record;
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(rotation.snapshotBody, '"etag-two"'));
    harness.served.set(keysetsUrl(1), () => jsonResponse(rotation.keysetsBody));
    harness.failWrites();
    await expect(harness.session.refresh()).rejects.toMatchObject({
      reason: "policy_cache_write_failed",
    });
    expect(harness.cacheStore.record).toBe(recordBefore);
    expect(harness.session.acceptedState?.revisionNumber).toBe(1);
    expect(harness.session.state).toBe("policy_refresh_required");
  });
});

describe("PolicySession re-onboarding", () => {
  async function foreignWorkspaceMaterial() {
    const signer = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const keyset = await buildKeysetEnvelope(
      keysetPayload([keysetKeyPayload(signer, "current")], {
        keysetRevision: 1,
        parentKeysetRevision: null,
        createdAt: "2026-08-17T09:00:00.000000Z",
        workspaceId: OTHER_WORKSPACE_ID,
      }),
      [signer],
    );
    const snapshot = await buildSnapshotEnvelope(
      snapshotPayload([...RULES], { revisionNumber: 1, workspaceId: OTHER_WORKSPACE_ID }),
      signer,
    );
    return {
      keysetsBody: wireBody({ has_more: false, keysets: [keyset] }),
      snapshotBody: wireBody({
        payload: snapshot.payload,
        payload_sha256: snapshot.payload_sha256,
        signature: snapshot.signature,
      }),
    };
  }

  async function onboardedHarness(): Promise<Harness> {
    const material = await initialTrustMaterial();
    const harness = createHarness({});
    harness.served.set(keysetsUrl(0), () => jsonResponse(material.keysetsBody));
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(material.snapshotBody, '"etag-one"'));
    await harness.session.restoreFromCache();
    await harness.session.adoptOnboardingTrust();
    return harness;
  }

  it("replaces the trust anchor when a completed onboarding binds another workspace", async () => {
    const harness = await onboardedHarness();
    expect(harness.session.acceptedState?.workspaceId).toBe(WORKSPACE_ID);
    const foreign = await foreignWorkspaceMaterial();
    harness.served.set(keysetsUrl(0), () => jsonResponse(foreign.keysetsBody));
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(foreign.snapshotBody, '"etag-foreign"'));
    await harness.session.adoptOnboardingTrust();
    expect(harness.session.state).toBe("policy_ready");
    expect(harness.session.acceptedState?.workspaceId).toBe(OTHER_WORKSPACE_ID);
    expect(harness.session.acceptedState?.keysetSequence).toBe(1);
    expect(harness.cacheStore.writes).toBe(2);
    const persisted = JSON.stringify(harness.cacheStore.record);
    expect(persisted).toContain(OTHER_WORKSPACE_ID);
    expect(persisted).not.toContain(WORKSPACE_ID);
  });

  it("preserves the prior anchor when re-onboarding verification fails", async () => {
    const harness = await onboardedHarness();
    const recordBefore = harness.cacheStore.record;
    const foreign = await foreignWorkspaceMaterial();
    const tampered = JSON.parse(foreign.keysetsBody) as {
      data: { keysets: { payload_sha256: string }[] };
    };
    const tamperedEnvelope = tampered.data.keysets[0];
    if (tamperedEnvelope === undefined) {
      throw new Error("foreign keyset page must carry one envelope");
    }
    tamperedEnvelope.payload_sha256 = "0".repeat(64);
    harness.served.set(keysetsUrl(0), () => jsonResponse(JSON.stringify(tampered)));
    await expect(harness.session.adoptOnboardingTrust()).rejects.toMatchObject({
      reason: "policy_payload_hash_mismatch",
    });
    expect(harness.cacheStore.record).toBe(recordBefore);
    expect(harness.session.acceptedState?.workspaceId).toBe(WORKSPACE_ID);
    expect(harness.session.state).toBe("policy_integrity_failed");
  });

  it("preserves the prior anchor when re-onboarding cannot reach the server", async () => {
    const harness = await onboardedHarness();
    const recordBefore = harness.cacheStore.record;
    harness.served.set(keysetsUrl(0), () => new Error("network down"));
    await expect(harness.session.adoptOnboardingTrust()).rejects.toMatchObject({
      reason: "policy_network_unavailable",
    });
    expect(harness.cacheStore.record).toBe(recordBefore);
    expect(harness.session.acceptedState?.workspaceId).toBe(WORKSPACE_ID);
    expect(harness.session.state).toBe("policy_offline_cached");
  });
});

describe("PolicySession offline startup", () => {
  it("classifies from the last trusted cache without any network claim", async () => {
    const material = await initialTrustMaterial();
    const keyset = material.keyset;
    const snapshot = await buildSnapshotEnvelope(
      snapshotPayload([...RULES], { revisionNumber: 1 }),
      material.signer,
    );
    const record = buildPolicyCacheRecord({
      workspaceId: WORKSPACE_ID,
      revisionNumber: 1,
      keysetSequence: 1,
      keysetEnvelope: keyset,
      snapshotEnvelope: snapshot,
    });
    const harness = createHarness({ initialRecord: record });
    await harness.session.restoreFromCache();
    expect(harness.session.state).toBe("policy_offline_cached");
    expect(harness.requests.length).toBe(0);
    expect(
      harness.session.evaluate({ normalizedLocator: "notes/a.pdf" }),
    ).toEqual({ raw: "excluded", enforced: "excluded" });
    expect(
      harness.session.evaluate({ normalizedLocator: "notes/a.md" }),
    ).toEqual({ raw: "allowed", enforced: "allowed" });
  });

  it("denies when the persisted record is corrupt", async () => {
    const harness = createHarness({ initialRecord: { tampered: true } });
    await harness.session.restoreFromCache();
    expect(harness.session.state).toBe("policy_not_initialized");
    expect(
      harness.session.evaluate({ normalizedLocator: "a.md" }),
    ).toEqual({ raw: "indeterminate", enforced: "excluded" });
  });
});

describe("PolicySession strict response handling", () => {
  async function onboardedHarness(): Promise<Harness> {
    const material = await initialTrustMaterial();
    const harness = createHarness({});
    harness.served.set(keysetsUrl(0), () => jsonResponse(material.keysetsBody));
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(material.snapshotBody, '"etag-one"'));
    await harness.session.restoreFromCache();
    await harness.session.adoptOnboardingTrust();
    return harness;
  }

  it("rejects duplicate JSON members in the response body", async () => {
    const harness = await onboardedHarness();
    const duplicated = `{"data": null, "data": null, "error": null, "request_id": "x", "warnings": []}`;
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(duplicated));
    await expect(harness.session.refresh()).rejects.toMatchObject({
      reason: "policy_response_malformed",
    });
    expect(harness.session.state).toBe("policy_integrity_failed");
  });

  it("rejects unknown envelope members and empty envelopes", async () => {
    const extraMemberHarness = await onboardedHarness();
    extraMemberHarness.served.set(SNAPSHOT_URL, () =>
      jsonResponse(`{"data": null, "error": null, "request_id": "x", "warnings": [], "extra": 1}`),
    );
    await expect(extraMemberHarness.session.refresh()).rejects.toMatchObject({
      reason: "policy_envelope_invalid",
    });
    expect(extraMemberHarness.session.state).toBe("policy_integrity_failed");

    const emptyEnvelopeHarness = await onboardedHarness();
    emptyEnvelopeHarness.served.set(SNAPSHOT_URL, () =>
      jsonResponse(`{"data": null, "error": null, "request_id": "x", "warnings": []}`),
    );
    await expect(emptyEnvelopeHarness.session.refresh()).rejects.toMatchObject({
      reason: "policy_envelope_invalid",
    });
    expect(emptyEnvelopeHarness.session.state).toBe("policy_integrity_failed");
  });

  it("surfaces the closed 409 error code for an uninitialized policy", async () => {
    // The backend serves exclusion_policy_not_initialized as HTTP 409 with an
    // error envelope (spec 19); the closed parser must see that body even
    // though the status is not 200, and a fresh server must never read as
    // tampering.
    const fresh = createHarness({});
    fresh.served.set(SNAPSHOT_URL, () => ({
      status: 409,
      bodyText: JSON.stringify({
        data: null,
        error: {
          code: "exclusion_policy_not_initialized",
          message: "safe",
          details: {},
          retryable: false,
        },
        request_id: "018f47a0-7b00-7000-8000-000000000902",
        warnings: [],
      }),
      etag: null,
    }));
    await fresh.session.restoreFromCache();
    await expect(fresh.session.refresh()).rejects.toMatchObject({
      reason: "policy_not_initialized_on_server",
    });
    expect(fresh.session.state).toBe("policy_not_initialized");
  });

  it("maps transient 5xx and 429 snapshot responses to a network failure without poisoning integrity", async () => {
    for (const transientStatus of [429, 500, 503]) {
      const harness = await onboardedHarness();
      const recordBefore = harness.cacheStore.record;
      harness.served.set(SNAPSHOT_URL, () => ({
        status: transientStatus,
        bodyText: "",
        etag: null,
      }));
      await expect(harness.session.refresh()).rejects.toMatchObject({
        reason: "policy_network_unavailable",
      });
      expect(harness.session.state, `status ${transientStatus}`).toBe("policy_offline_cached");
      expect(harness.cacheStore.record, `status ${transientStatus}`).toBe(recordBefore);
      // A transient failure must not lock out later refresh attempts.
      harness.served.set(SNAPSHOT_URL, () => ({ status: 304, bodyText: "", etag: null }));
      await harness.session.refresh();
      expect(harness.session.state, `status ${transientStatus}`).toBe("policy_ready");
    }
  });

});

describe("PolicySession keyset page handling", () => {
  it("verifies a chain delivered across multiple ordered pages", async () => {
    const firstSigner = await deriveTestSigningKey(SNAPSHOT_SIGNER_SEED);
    const secondSigner = await deriveTestSigningKey(KEYSET_CURRENT_SEED);
    const thirdSigner = await deriveTestSigningKey(KEYSET_STAGED_SEED);
    const first = await buildKeysetEnvelope(
      keysetPayload([keysetKeyPayload(firstSigner, "current")], {
        keysetRevision: 1,
        parentKeysetRevision: null,
        createdAt: "2026-08-17T09:00:00.000000Z",
      }),
      [firstSigner],
    );
    const second = await buildKeysetEnvelope(
      keysetPayload(
        [keysetKeyPayload(firstSigner, "staged"), keysetKeyPayload(secondSigner, "current")],
        { keysetRevision: 2, parentKeysetRevision: 1, createdAt: LATER_CREATED_AT },
      ),
      [firstSigner, secondSigner],
    );
    const third = await buildKeysetEnvelope(
      keysetPayload(
        [keysetKeyPayload(secondSigner, "staged"), keysetKeyPayload(thirdSigner, "current")],
        { keysetRevision: 3, parentKeysetRevision: 2, createdAt: LATER_CREATED_AT },
      ),
      [secondSigner, thirdSigner],
    );
    const snapshot = await buildSnapshotEnvelope(
      snapshotPayload([...RULES], { revisionNumber: 4 }),
      thirdSigner,
    );
    const harness = createHarness({});
    harness.served.set(keysetsUrl(0), () => jsonResponse(wireBody({ has_more: true, keysets: [first, second] })));
    harness.served.set(keysetsUrl(2), () => jsonResponse(wireBody({ has_more: false, keysets: [third] })));
    harness.served.set(SNAPSHOT_URL, () =>
      jsonResponse(
        wireBody({
          payload: snapshot.payload,
          payload_sha256: snapshot.payload_sha256,
          signature: snapshot.signature,
        }),
        '"etag-three"',
      ),
    );
    await harness.session.restoreFromCache();
    await harness.session.adoptOnboardingTrust();
    expect(harness.session.state).toBe("policy_ready");
    expect(harness.session.acceptedState?.keysetSequence).toBe(3);
    expect(harness.session.acceptedState?.revisionNumber).toBe(4);
  });

  it("stops an unbounded keyset pagination loop", async () => {
    const signer = await deriveTestSigningKey(SNAPSHOT_SIGNER_SEED);
    const envelope = await buildKeysetEnvelope(
      keysetPayload([keysetKeyPayload(signer, "current")], {
        keysetRevision: 1,
        parentKeysetRevision: null,
      }),
      [signer],
    );
    const harness = createHarness({});
    for (let revision = 0; revision < 24; revision += 1) {
      harness.served.set(keysetsUrl(revision), () =>
        jsonResponse(wireBody({ has_more: true, keysets: [envelope] })),
      );
    }
    harness.served.set(SNAPSHOT_URL, () => new Error("should not be reached"));
    await harness.session.restoreFromCache();
    await expect(harness.session.adoptOnboardingTrust()).rejects.toMatchObject({
      reason: "policy_keyset_page_overflow",
    });
  });

  it("revalidates the persisted keyset envelope with the closed parser on restore", async () => {
    const material = await initialTrustMaterial();
    const record = buildPolicyCacheRecord({
      workspaceId: WORKSPACE_ID,
      revisionNumber: 1,
      keysetSequence: 1,
      keysetEnvelope: material.keyset,
      snapshotEnvelope: await buildSnapshotEnvelope(
        snapshotPayload([...RULES], { revisionNumber: 1 }),
        material.signer,
      ),
    });
    const parsed = parseClosedJson(JSON.stringify(record), { maximumBytes: 512 * 1024 });
    expect(parsed).not.toBeNull();
    const restoredKeyset = validateKeysetEnvelope(
      (parsed as { keyset_envelope: unknown }).keyset_envelope,
    );
    expect(restoredKeyset.payload.keyset_revision).toBe(1);
  });
});

describe("PolicySession capture evaluation seam (journal design 7.1, 9)", () => {
  it("answers with the accepted revision for an onboarded session", async () => {
    const material = await initialTrustMaterial();
    const harness = createHarness({});
    harness.served.set(keysetsUrl(0), () => jsonResponse(material.keysetsBody));
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(material.snapshotBody, '"etag-capture"'));

    await harness.session.adoptOnboardingTrust();
    const evaluation = harness.session.evaluateForCapture({
      sourceId: null,
      normalizedLocator: "notes/report.md",
      mediaType: "text/plain",
      sizeBytes: 128,
    });
    expect(evaluation.decision).toEqual({ raw: "allowed", enforced: "allowed" });
    expect(evaluation.revisionNumber).toBe(1);
  });

  it("denies a rule-matched subject under the same accepted revision", async () => {
    const material = await initialTrustMaterial();
    const harness = createHarness({});
    harness.served.set(keysetsUrl(0), () => jsonResponse(material.keysetsBody));
    harness.served.set(SNAPSHOT_URL, () => jsonResponse(material.snapshotBody, '"etag-capture"'));

    await harness.session.adoptOnboardingTrust();
    const evaluation = harness.session.evaluateForCapture({
      sourceId: null,
      normalizedLocator: "docs/brochure.pdf",
      mediaType: "application/pdf",
      sizeBytes: 256,
    });
    expect(evaluation.decision).toEqual({ raw: "excluded", enforced: "excluded" });
    expect(evaluation.revisionNumber).toBe(1);
  });

  it("fails closed with revision 0 when no snapshot is accepted", async () => {
    const harness = createHarness({});
    await harness.session.restoreFromCache();

    const evaluation = harness.session.evaluateForCapture({
      sourceId: null,
      normalizedLocator: "notes/report.md",
      mediaType: "text/plain",
      sizeBytes: 128,
    });
    expect(evaluation.decision).toEqual({ raw: "indeterminate", enforced: "excluded" });
    expect(evaluation.revisionNumber).toBe(0);
  });
});
