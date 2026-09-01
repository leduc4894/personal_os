/**
 * Tests of the strict decoded Conflict Inbox contract surface (Child 8
 * spec 6, Task 7): every wire record decodes onto the closed camelCase
 * plugin shape, every vocabulary is compile-time bound to the generated
 * server registry, unknown enum values and ill-typed members fail closed
 * with the single closed contract reason, and the resolve input grammar
 * mirrors the server's own field rules (save_merged requires the verified
 * object reference; every other kind must not carry one).
 */

import { describe, expect, it } from "vitest";

import {
  CONFLICT_CANDIDATE_KINDS,
  CONFLICT_EVIDENCE_ROLES,
  CONFLICT_KINDS,
  CONFLICT_LOCAL_REPAIR_ACTIONS,
  CONFLICT_LOCAL_REPAIR_SAFE_REASONS,
  CONFLICT_RESOLUTION_KINDS,
  CONFLICT_RESOLUTION_OUTCOMES,
  CONFLICT_STATUSES,
  ConflictContractError,
  decodeConflictDetail,
  decodeConflictPage,
  decodeConflictResolution,
  decodeConflictSummary,
  isConflictEvidenceRole,
  validateConflictResolveInput,
} from "./contracts";

// --- shared wire fixtures -----------------------------------------------------------------------

const CONFLICT_ID = "11111111-1111-4111-8111-111111111111";
const SOURCE_ID = "22222222-2222-4222-8222-222222222222";
const ORIGINATING_EVENT_ID = "33333333-3333-4333-8333-333333333333";
const ORIGINATING_DEVICE_ID = "44444444-4444-4444-8444-444444444444";
const BASE_VERSION_ID = "55555555-5555-4555-8555-555555555555";
const OBSERVED_REMOTE_VERSION_ID = "66666666-6666-4666-8666-666666666666";
const VERIFIED_CANDIDATE_OBJECT_ID = "77777777-7777-4777-8777-777777777777";
const RESOLUTION_EVENT_ID = "88888888-8888-4888-8888-888888888888";
const RESULTING_VERSION_ID = "99999999-9999-4999-8999-999999999999";
const SUCCESSOR_CONFLICT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const IDEMPOTENCY_KEY = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const OPEN_CONTENT_SUMMARY_WIRE = {
  conflict_id: CONFLICT_ID,
  source_id: SOURCE_ID,
  conflict_kind: "stale_content",
  status: "open",
  originating_event_id: ORIGINATING_EVENT_ID,
  originating_device_id: ORIGINATING_DEVICE_ID,
  base_version_id: BASE_VERSION_ID,
  observed_remote_version_id: OBSERVED_REMOTE_VERSION_ID,
  candidate_kind: "content",
  verified_candidate_object_id: VERIFIED_CANDIDATE_OBJECT_ID,
  captured_at: "2026-09-01T00:00:00Z",
  resolution_kind: null,
  resolution_event_id: null,
  resulting_version_id: null,
  successor_conflict_id: null,
  closed_at: null,
};

const RESOLVED_SUMMARY_WIRE = {
  ...OPEN_CONTENT_SUMMARY_WIRE,
  status: "resolved",
  resolution_kind: "save_merged",
  resolution_event_id: RESOLUTION_EVENT_ID,
  resulting_version_id: RESULTING_VERSION_ID,
  closed_at: "2026-09-02T00:00:00Z",
};

describe("Conflict wire vocabulary constants (Task 6 registry binding)", () => {
  it("mirrors exactly the generated server conflict vocabularies", () => {
    expect([...CONFLICT_KINDS]).toEqual([
      "stale_content",
      "edit_remote_delete",
      "delete_remote_edit",
      "locator_collision",
    ]);
    expect([...CONFLICT_STATUSES]).toEqual(["open", "resolving", "resolved", "superseded"]);
    expect([...CONFLICT_CANDIDATE_KINDS]).toEqual(["content", "delete"]);
    expect([...CONFLICT_RESOLUTION_KINDS]).toEqual([
      "keep_remote",
      "keep_local",
      "save_merged",
    ]);
    expect([...CONFLICT_RESOLUTION_OUTCOMES]).toEqual(["resolved", "stale_successor"]);
    expect([...CONFLICT_EVIDENCE_ROLES]).toEqual(["base", "remote", "candidate"]);
  });

  it("keeps the evidence role guard closed", () => {
    for (const role of CONFLICT_EVIDENCE_ROLES) {
      expect(isConflictEvidenceRole(role)).toBe(true);
    }
    expect(isConflictEvidenceRole("local")).toBe(false);
    expect(isConflictEvidenceRole("BASE")).toBe(false);
    expect(isConflictEvidenceRole(null)).toBe(false);
    expect(isConflictEvidenceRole(1)).toBe(false);
  });

  it("keeps the durable repair vocabularies closed and action-named", () => {
    expect([...CONFLICT_LOCAL_REPAIR_ACTIONS]).toEqual([
      "apply_remote_version",
      "apply_resulting_version",
      "apply_remote_tombstone",
    ]);
    expect([...CONFLICT_LOCAL_REPAIR_SAFE_REASONS]).toEqual([
      "resolution_committed",
      "winner_download_failed",
      "vault_apply_failed",
    ]);
    for (const action of CONFLICT_LOCAL_REPAIR_ACTIONS) {
      expect(action.startsWith("apply_")).toBe(true);
    }
  });
});

describe("decodeConflictSummary", () => {
  it("decodes one open content conflict onto the closed plugin shape", () => {
    expect(decodeConflictSummary(OPEN_CONTENT_SUMMARY_WIRE)).toEqual({
      conflictId: CONFLICT_ID,
      sourceId: SOURCE_ID,
      conflictKind: "stale_content",
      status: "open",
      originatingEventId: ORIGINATING_EVENT_ID,
      originatingDeviceId: ORIGINATING_DEVICE_ID,
      baseVersionId: BASE_VERSION_ID,
      observedRemoteVersionId: OBSERVED_REMOTE_VERSION_ID,
      candidateKind: "content",
      verifiedCandidateObjectId: VERIFIED_CANDIDATE_OBJECT_ID,
      capturedAt: "2026-09-01T00:00:00Z",
      resolutionKind: null,
      resolutionEventId: null,
      resultingVersionId: null,
      successorConflictId: null,
      closedAt: null,
    });
  });

  it("decodes a resolved save_merged conflict with its resolution evidence", () => {
    const decoded = decodeConflictSummary(RESOLVED_SUMMARY_WIRE);
    expect(decoded.status).toBe("resolved");
    expect(decoded.resolutionKind).toBe("save_merged");
    expect(decoded.resolutionEventId).toBe(RESOLUTION_EVENT_ID);
    expect(decoded.resultingVersionId).toBe(RESULTING_VERSION_ID);
    expect(decoded.closedAt).toBe("2026-09-02T00:00:00Z");
  });

  it("decodes a byteless locator collision without a candidate object", () => {
    const decoded = decodeConflictSummary({
      ...OPEN_CONTENT_SUMMARY_WIRE,
      conflict_kind: "locator_collision",
      candidate_kind: "delete",
      verified_candidate_object_id: null,
    });
    expect(decoded.candidateKind).toBe("delete");
    expect(decoded.verifiedCandidateObjectId).toBeNull();
  });

  it("rejects an unknown status enum value", () => {
    expect(() =>
      decodeConflictSummary({ ...OPEN_CONTENT_SUMMARY_WIRE, status: "reopened" }),
    ).toThrow(ConflictContractError);
  });

  it("rejects an unknown conflict kind enum value", () => {
    expect(() =>
      decodeConflictSummary({ ...OPEN_CONTENT_SUMMARY_WIRE, conflict_kind: "edit_edit" }),
    ).toThrow(ConflictContractError);
  });

  it("rejects an unknown candidate kind enum value", () => {
    expect(() =>
      decodeConflictSummary({ ...OPEN_CONTENT_SUMMARY_WIRE, candidate_kind: "binary" }),
    ).toThrow(ConflictContractError);
  });

  it("rejects an unknown resolution kind enum value", () => {
    expect(() =>
      decodeConflictSummary({
        ...RESOLVED_SUMMARY_WIRE,
        resolution_kind: "keep_both",
      }),
    ).toThrow(ConflictContractError);
  });

  it("rejects a non-UUID identifier member", () => {
    expect(() =>
      decodeConflictSummary({ ...OPEN_CONTENT_SUMMARY_WIRE, conflict_id: "not-a-uuid" }),
    ).toThrow(ConflictContractError);
    expect(() =>
      decodeConflictSummary({
        ...OPEN_CONTENT_SUMMARY_WIRE,
        verified_candidate_object_id: 42,
      }),
    ).toThrow(ConflictContractError);
  });

  it("rejects a malformed timestamp member", () => {
    expect(() =>
      decodeConflictSummary({ ...OPEN_CONTENT_SUMMARY_WIRE, captured_at: "yesterday" }),
    ).toThrow(ConflictContractError);
    expect(() =>
      decodeConflictSummary({ ...OPEN_CONTENT_SUMMARY_WIRE, captured_at: 1783000000000 }),
    ).toThrow(ConflictContractError);
  });

  it("rejects a non-record payload and raw response detail", () => {
    expect(() => decodeConflictSummary(null)).toThrow(ConflictContractError);
    expect(() => decodeConflictSummary("conflict")).toThrow(ConflictContractError);
    expect(() => decodeConflictSummary([])).toThrow(ConflictContractError);
  });
});

describe("decodeConflictDetail", () => {
  it("decodes the safe metadata plus the offered choices", () => {
    const decoded = decodeConflictDetail({
      ...OPEN_CONTENT_SUMMARY_WIRE,
      choices: ["keep_remote", "keep_local", "save_merged"],
    });
    expect(decoded.conflictId).toBe(CONFLICT_ID);
    expect(decoded.choices).toEqual(["keep_remote", "keep_local", "save_merged"]);
  });

  it("decodes a byteless candidate's single offered choice", () => {
    const decoded = decodeConflictDetail({
      ...OPEN_CONTENT_SUMMARY_WIRE,
      conflict_kind: "delete_remote_edit",
      candidate_kind: "delete",
      verified_candidate_object_id: null,
      choices: ["keep_remote"],
    });
    expect(decoded.choices).toEqual(["keep_remote"]);
  });

  it("rejects an unknown offered choice", () => {
    expect(() =>
      decodeConflictDetail({
        ...OPEN_CONTENT_SUMMARY_WIRE,
        choices: ["keep_remote", "overwrite"],
      }),
    ).toThrow(ConflictContractError);
  });

  it("rejects a non-array choices member", () => {
    expect(() =>
      decodeConflictDetail({ ...OPEN_CONTENT_SUMMARY_WIRE, choices: "keep_remote" }),
    ).toThrow(ConflictContractError);
  });
});

describe("decodeConflictPage", () => {
  it("decodes one bounded page with its continuation cursor", () => {
    const decoded = decodeConflictPage({
      conflicts: [OPEN_CONTENT_SUMMARY_WIRE, RESOLVED_SUMMARY_WIRE],
      has_more: true,
      next_exclusive_start_conflict_id: SUCCESSOR_CONFLICT_ID,
    });
    expect(decoded.conflicts).toHaveLength(2);
    expect(decoded.conflicts[0]?.conflictId).toBe(CONFLICT_ID);
    expect(decoded.hasMore).toBe(true);
    expect(decoded.nextExclusiveStartConflictId).toBe(SUCCESSOR_CONFLICT_ID);
  });

  it("decodes a terminal page without a cursor", () => {
    const decoded = decodeConflictPage({
      conflicts: [],
      has_more: false,
      next_exclusive_start_conflict_id: null,
    });
    expect(decoded.conflicts).toEqual([]);
    expect(decoded.hasMore).toBe(false);
    expect(decoded.nextExclusiveStartConflictId).toBeNull();
  });

  it("rejects a page whose conflicts member is not an array of summaries", () => {
    expect(() => decodeConflictPage({ conflicts: OPEN_CONTENT_SUMMARY_WIRE, has_more: false })).toThrow(
      ConflictContractError,
    );
    expect(() =>
      decodeConflictPage({ conflicts: [{ ...OPEN_CONTENT_SUMMARY_WIRE, status: "?" }], has_more: false }),
    ).toThrow(ConflictContractError);
  });

  it("rejects a non-boolean has_more member", () => {
    expect(() =>
      decodeConflictPage({
        conflicts: [],
        has_more: 1,
        next_exclusive_start_conflict_id: null,
      }),
    ).toThrow(ConflictContractError);
  });
});

describe("decodeConflictResolution", () => {
  it("decodes one resolved outcome with its resulting version", () => {
    expect(
      decodeConflictResolution({
        outcome: "resolved",
        conflict_id: CONFLICT_ID,
        resolution_event_id: RESOLUTION_EVENT_ID,
        resolution_kind: "keep_local",
        resulting_version_id: RESULTING_VERSION_ID,
        successor_conflict_id: null,
        completed_at: "2026-09-02T00:00:00Z",
      }),
    ).toEqual({
      outcome: "resolved",
      conflictId: CONFLICT_ID,
      resolutionEventId: RESOLUTION_EVENT_ID,
      resolutionKind: "keep_local",
      resultingVersionId: RESULTING_VERSION_ID,
      successorConflictId: null,
      completedAt: "2026-09-02T00:00:00Z",
    });
  });

  it("decodes a stale successor outcome binding the open successor", () => {
    const decoded = decodeConflictResolution({
      outcome: "stale_successor",
      conflict_id: CONFLICT_ID,
      resolution_event_id: RESOLUTION_EVENT_ID,
      resolution_kind: "keep_remote",
      resulting_version_id: null,
      successor_conflict_id: SUCCESSOR_CONFLICT_ID,
      completed_at: "2026-09-02T00:00:00Z",
    });
    expect(decoded.outcome).toBe("stale_successor");
    expect(decoded.resultingVersionId).toBeNull();
    expect(decoded.successorConflictId).toBe(SUCCESSOR_CONFLICT_ID);
  });

  it("rejects an unknown outcome enum value", () => {
    expect(() =>
      decodeConflictResolution({
        outcome: "pending",
        conflict_id: CONFLICT_ID,
        resolution_event_id: RESOLUTION_EVENT_ID,
        resolution_kind: "keep_remote",
        resulting_version_id: null,
        successor_conflict_id: null,
        completed_at: "2026-09-02T00:00:00Z",
      }),
    ).toThrow(ConflictContractError);
  });
});

describe("validateConflictResolveInput", () => {
  const BASE_INPUT = {
    conflictId: CONFLICT_ID,
    resolutionEventId: RESOLUTION_EVENT_ID,
    idempotencyKey: IDEMPOTENCY_KEY,
    resolutionKind: "keep_remote" as const,
    reviewedRemoteVersionId: OBSERVED_REMOTE_VERSION_ID,
    verifiedCandidateObjectId: null,
  };

  it("accepts a whole-object resolution with its reviewed remote", () => {
    expect(() => validateConflictResolveInput(BASE_INPUT)).not.toThrow();
  });

  it("accepts a save_merged resolution carrying the verified object reference", () => {
    expect(() =>
      validateConflictResolveInput({
        ...BASE_INPUT,
        resolutionKind: "save_merged",
        verifiedCandidateObjectId: VERIFIED_CANDIDATE_OBJECT_ID,
      }),
    ).not.toThrow();
  });

  it("rejects a save_merged resolution without a verified object reference", () => {
    expect(() =>
      validateConflictResolveInput({
        ...BASE_INPUT,
        resolutionKind: "save_merged",
        verifiedCandidateObjectId: null,
      }),
    ).toThrow(ConflictContractError);
  });

  it("rejects a whole-object resolution that carries a verified object reference", () => {
    expect(() =>
      validateConflictResolveInput({
        ...BASE_INPUT,
        resolutionKind: "keep_local",
        verifiedCandidateObjectId: VERIFIED_CANDIDATE_OBJECT_ID,
      }),
    ).toThrow(ConflictContractError);
  });

  it("rejects a malformed idempotency key", () => {
    expect(() =>
      validateConflictResolveInput({
        ...BASE_INPUT,
        idempotencyKey: "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
      }),
    ).toThrow(ConflictContractError);
    expect(() =>
      validateConflictResolveInput({ ...BASE_INPUT, idempotencyKey: "idem-key" }),
    ).toThrow(ConflictContractError);
  });

  it("rejects non-UUID identities", () => {
    expect(() =>
      validateConflictResolveInput({ ...BASE_INPUT, conflictId: "conflict-1" }),
    ).toThrow(ConflictContractError);
    expect(() =>
      validateConflictResolveInput({ ...BASE_INPUT, resolutionEventId: "" }),
    ).toThrow(ConflictContractError);
    expect(() =>
      validateConflictResolveInput({ ...BASE_INPUT, reviewedRemoteVersionId: "v2" }),
    ).toThrow(ConflictContractError);
    expect(() =>
      validateConflictResolveInput({
        ...BASE_INPUT,
        resolutionKind: "save_merged",
        verifiedCandidateObjectId: "object",
      }),
    ).toThrow(ConflictContractError);
  });
});
