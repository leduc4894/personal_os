/**
 * The strict decoded Conflict Inbox contract surface (Child 8 spec 6,
 * Task 7).
 *
 * The generated workspace client is intentionally NOT bundled into
 * Obsidian, so this module owns the plugin-side mirror of exactly the
 * four Task 6 conflict wire shapes: every closed vocabulary is
 * compile-time bound to the generated server registry through
 * `satisfies`, and every wire record decodes onto a frozen camelCase
 * plugin shape that carries ONLY opaque identifiers, closed labels and
 * normalized timestamps — never a locator, object key, digest, provider
 * detail or any raw response text. A decoder rejects an unknown enum
 * value, an ill-typed member or a foreign payload shape with the single
 * closed {@link ConflictContractError} reason; raw response detail never
 * reaches the thrown failure.
 *
 * The same module owns the closed vocabularies of the durable no-byte
 * repair facts of journal schema v9 (`conflict_local_repairs`): the
 * target-action and safe-reason tokens the journal DDL renders its CHECK
 * constraints from (single source of truth with
 * `../journal/sqlite-database.ts`), and the {@link PendingLocalApply}
 * record whose members make byte or path storage unrepresentable — no
 * `Uint8Array`, blob, path or locator member exists on any input type
 * here.
 *
 * This module carries NO runtime dependency on the journal engine, Node,
 * Electron or the Obsidian adapter — only a type-level import of the
 * generated contract package — so the whole `src/conflicts` tree stays
 * loadable on mobile.
 */

import type { components } from "@workspace/api-client";

// --- the closed wire vocabularies (Task 6 registry binding) ----------------------------------------

/**
 * The closed conflict kind vocabulary: every member is a registered
 * `ConflictKind` of the generated server error registry, so an
 * unregistered snake_case kind fails `tsc --noEmit` here, not at a
 * diagnostics surface.
 */
export const CONFLICT_KINDS = [
  "stale_content",
  "edit_remote_delete",
  "delete_remote_edit",
  "locator_collision",
] as const satisfies readonly components["schemas"]["ConflictKind"][];

/** One conflict's closed kind label. */
export type ConflictKind = (typeof CONFLICT_KINDS)[number];

/** The closed conflict lifecycle status vocabulary. */
export const CONFLICT_STATUSES = [
  "open",
  "resolving",
  "resolved",
  "superseded",
] as const satisfies readonly components["schemas"]["ConflictStatus"][];

/** One conflict's closed lifecycle status. */
export type ConflictStatus = (typeof CONFLICT_STATUSES)[number];

/** The closed candidate shape vocabulary of one conflict. */
export const CONFLICT_CANDIDATE_KINDS = [
  "content",
  "delete",
] as const satisfies readonly components["schemas"]["ConflictCandidateKind"][];

/** One conflict candidate's closed shape (retained bytes or byteless intent). */
export type ConflictCandidateKind = (typeof CONFLICT_CANDIDATE_KINDS)[number];

/** The closed explicit-resolution choice vocabulary. */
export const CONFLICT_RESOLUTION_KINDS = [
  "keep_remote",
  "keep_local",
  "save_merged",
] as const satisfies readonly components["schemas"]["ConflictResolutionKind"][];

/** One explicit resolution's closed choice. */
export type ConflictResolutionKind = (typeof CONFLICT_RESOLUTION_KINDS)[number];

/** The closed resolution outcome vocabulary (`stale_successor` is a typed success). */
export const CONFLICT_RESOLUTION_OUTCOMES = [
  "resolved",
  "stale_successor",
] as const satisfies readonly components["schemas"]["ConflictResolutionOutcome"][];

/** One resolution attempt's closed outcome. */
export type ConflictResolutionOutcome = (typeof CONFLICT_RESOLUTION_OUTCOMES)[number];

/** The closed immutable evidence role vocabulary. */
export const CONFLICT_EVIDENCE_ROLES = [
  "base",
  "remote",
  "candidate",
] as const satisfies readonly components["schemas"]["ConflictEvidenceRole"][];

/** One immutable evidence role of the verified evidence download. */
export type ConflictEvidenceRole = (typeof CONFLICT_EVIDENCE_ROLES)[number];

/** Whether one value is a member of the closed evidence role vocabulary. */
export function isConflictEvidenceRole(value: unknown): value is ConflictEvidenceRole {
  return (
    typeof value === "string" && (CONFLICT_EVIDENCE_ROLES as readonly string[]).includes(value)
  );
}

// --- the decoded wire records -----------------------------------------------------------------------

/**
 * One conflict's safe metadata: the decoded mirror of the server's
 * `SourceConflictData`. Every member is an opaque identifier, a closed
 * label or a normalized UTC timestamp — the credential-derived workspace
 * is the caller's own and no locator, object key, digest or provider
 * detail ever crosses.
 */
export interface ConflictSummary {
  readonly conflictId: string;
  readonly sourceId: string | null;
  readonly conflictKind: ConflictKind;
  readonly status: ConflictStatus;
  readonly originatingEventId: string;
  readonly originatingDeviceId: string;
  readonly baseVersionId: string | null;
  readonly observedRemoteVersionId: string | null;
  readonly candidateKind: ConflictCandidateKind;
  readonly verifiedCandidateObjectId: string | null;
  readonly capturedAt: string;
  readonly resolutionKind: ConflictResolutionKind | null;
  readonly resolutionEventId: string | null;
  readonly resultingVersionId: string | null;
  readonly successorConflictId: string | null;
  readonly closedAt: string | null;
}

/**
 * One conflict's detail: the safe metadata plus exactly the offered
 * choices the server's kind/media-type matrix admits, so the Inbox can
 * never offer an unappliable choice.
 */
export interface ConflictDetail extends ConflictSummary {
  readonly choices: readonly ConflictResolutionKind[];
}

/**
 * One bounded page of the workspace's open conflicts with its stable
 * continuation cursor.
 */
export interface ConflictPage {
  readonly conflicts: readonly ConflictSummary[];
  readonly hasMore: boolean;
  readonly nextExclusiveStartConflictId: string | null;
}

/**
 * The frozen outcome of one explicit resolution attempt: `resolved`
 * commits the winner (with a resulting version only under a publishing
 * choice) and `stale_successor` binds the open successor created against
 * the newer observed remote; a same-identity replay receives this value
 * unchanged.
 */
export interface ConflictResolution {
  readonly outcome: ConflictResolutionOutcome;
  readonly conflictId: string;
  readonly resolutionEventId: string;
  readonly resolutionKind: ConflictResolutionKind;
  readonly resultingVersionId: string | null;
  readonly successorConflictId: string | null;
  readonly completedAt: string;
}

/**
 * One strict explicit-resolution request: the new resolution event
 * identity with its fresh canonical idempotency key, the closed choice,
 * the reviewed remote version and — only under `save_merged` — the
 * verified object reference of an already-uploaded merged result. Never
 * raw bytes, a digest, a locator or any content.
 */
export interface ConflictResolveInput {
  readonly conflictId: string;
  readonly resolutionEventId: string;
  readonly idempotencyKey: string;
  readonly resolutionKind: ConflictResolutionKind;
  readonly reviewedRemoteVersionId?: string | null | undefined;
  readonly verifiedCandidateObjectId?: string | null | undefined;
}

/**
 * The verified read of one immutable evidence role: the exact response
 * bytes after their declared length was verified, plus the declared
 * canonical media type and byte length. The evidence wire surface
 * carries no digest header, so the verification here is the exact
 * content-length; the controller (Task 8) decodes the bytes only for
 * supported text/Markdown media types.
 */
export interface VerifiedConflictEvidence {
  readonly bytes: Uint8Array;
  readonly mediaType: string;
  readonly sizeBytes: number;
}

// --- the closed contract failure -------------------------------------------------------------------

/**
 * The single closed reason of every decoded-contract rejection: an
 * unknown enum value, an ill-typed member, a malformed identity or
 * timestamp, or a foreign payload shape. Raw response detail never
 * reaches the thrown failure.
 */
export type ConflictContractErrorReason = "conflict_contract_invalid";

/** One decoded-contract violation: the closed reason and a static message. */
export class ConflictContractError extends Error {
  readonly reason: ConflictContractErrorReason;

  constructor() {
    super("conflict contract invalid");
    this.name = "ConflictContractError";
    this.reason = "conflict_contract_invalid";
  }
}

function contractInvalid(): ConflictContractError {
  return new ConflictContractError();
}

// --- decoding helpers --------------------------------------------------------------------------------

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function requireRecord(value: unknown): Record<string, unknown> {
  if (!isRecord(value) || Array.isArray(value)) {
    throw contractInvalid();
  }
  return value;
}

function decodeClosedToken<T extends string>(
  value: unknown,
  closedSet: readonly T[],
): T {
  if (typeof value !== "string" || !(closedSet as readonly string[]).includes(value)) {
    throw contractInvalid();
  }
  // Membership is proven against the closed set above; the cast only
  // refines the narrowed string to its literal union.
  return value as T;
}

function decodeUuid(value: unknown): string {
  if (typeof value !== "string" || !UUID_PATTERN.test(value)) {
    throw contractInvalid();
  }
  return value;
}

function decodeNullableUuid(value: unknown): string | null {
  if (value === null) {
    return null;
  }
  return decodeUuid(value);
}

function decodeTimestamp(value: unknown): string {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) {
    throw contractInvalid();
  }
  return value;
}

function decodeNullableTimestamp(value: unknown): string | null {
  if (value === null) {
    return null;
  }
  return decodeTimestamp(value);
}

function decodeResolutionKind(value: unknown): ConflictResolutionKind {
  return decodeClosedToken(value, CONFLICT_RESOLUTION_KINDS);
}

// --- the record decoders -------------------------------------------------------------------------------

/** Decode one wire `SourceConflictData` onto the frozen plugin shape. */
export function decodeConflictSummary(value: unknown): ConflictSummary {
  const data = requireRecord(value);
  const summary: ConflictSummary = {
    conflictId: decodeUuid(data["conflict_id"]),
    sourceId: decodeNullableUuid(data["source_id"]),
    conflictKind: decodeClosedToken(data["conflict_kind"], CONFLICT_KINDS),
    status: decodeClosedToken(data["status"], CONFLICT_STATUSES),
    originatingEventId: decodeUuid(data["originating_event_id"]),
    originatingDeviceId: decodeUuid(data["originating_device_id"]),
    baseVersionId: decodeNullableUuid(data["base_version_id"]),
    observedRemoteVersionId: decodeNullableUuid(data["observed_remote_version_id"]),
    candidateKind: decodeClosedToken(data["candidate_kind"], CONFLICT_CANDIDATE_KINDS),
    verifiedCandidateObjectId: decodeNullableUuid(data["verified_candidate_object_id"]),
    capturedAt: decodeTimestamp(data["captured_at"]),
    resolutionKind:
      data["resolution_kind"] === null ? null : decodeResolutionKind(data["resolution_kind"]),
    resolutionEventId: decodeNullableUuid(data["resolution_event_id"]),
    resultingVersionId: decodeNullableUuid(data["resulting_version_id"]),
    successorConflictId: decodeNullableUuid(data["successor_conflict_id"]),
    closedAt: decodeNullableTimestamp(data["closed_at"]),
  };
  return summary;
}

/** Decode one wire `SourceConflictDetailData` with its offered choices. */
export function decodeConflictDetail(value: unknown): ConflictDetail {
  const data = requireRecord(value);
  const choicesWire = data["choices"];
  if (!Array.isArray(choicesWire)) {
    throw contractInvalid();
  }
  const choices = choicesWire.map((choice) => decodeResolutionKind(choice));
  return { ...decodeConflictSummary(data), choices };
}

/** Decode one wire `SourceConflictPageData` with its continuation cursor. */
export function decodeConflictPage(value: unknown): ConflictPage {
  const data = requireRecord(value);
  const conflictsWire = data["conflicts"];
  if (!Array.isArray(conflictsWire)) {
    throw contractInvalid();
  }
  const hasMore = data["has_more"];
  if (typeof hasMore !== "boolean") {
    throw contractInvalid();
  }
  return {
    conflicts: conflictsWire.map((conflict) => decodeConflictSummary(conflict)),
    hasMore,
    nextExclusiveStartConflictId: decodeNullableUuid(data["next_exclusive_start_conflict_id"]),
  };
}

/** Decode one wire `SourceConflictResolutionData` outcome. */
export function decodeConflictResolution(value: unknown): ConflictResolution {
  const data = requireRecord(value);
  return {
    outcome: decodeClosedToken(data["outcome"], CONFLICT_RESOLUTION_OUTCOMES),
    conflictId: decodeUuid(data["conflict_id"]),
    resolutionEventId: decodeUuid(data["resolution_event_id"]),
    resolutionKind: decodeResolutionKind(data["resolution_kind"]),
    resultingVersionId: decodeNullableUuid(data["resulting_version_id"]),
    successorConflictId: decodeNullableUuid(data["successor_conflict_id"]),
    completedAt: decodeTimestamp(data["completed_at"]),
  };
}

// --- the resolve input grammar --------------------------------------------------------------------------

/**
 * The canonical lowercase hyphenated UUID text the server's idempotency
 * key grammar accepts — exactly the form the plugin mints with
 * `crypto.randomUUID`.
 */
const IDEMPOTENCY_KEY_PATTERN = UUID_PATTERN;

/**
 * Validate one explicit-resolution request against the server's own field
 * grammar BEFORE any transport contact: canonical UUID identities, the
 * canonical idempotency key, a closed choice, and the verified-object
 * shape per kind (`save_merged` requires the reference; every other kind
 * must not carry one). A violation throws the closed contract error; the
 * request never leaves the device.
 */
export function validateConflictResolveInput(input: ConflictResolveInput): void {
  const resolutionKind = decodeClosedToken(input.resolutionKind, CONFLICT_RESOLUTION_KINDS);
  const reviewedRemoteVersionId = input.reviewedRemoteVersionId ?? null;
  const verifiedCandidateObjectId = input.verifiedCandidateObjectId ?? null;
  if (
    !UUID_PATTERN.test(input.conflictId) ||
    !UUID_PATTERN.test(input.resolutionEventId) ||
    typeof input.idempotencyKey !== "string" ||
    !IDEMPOTENCY_KEY_PATTERN.test(input.idempotencyKey) ||
    (reviewedRemoteVersionId !== null && !UUID_PATTERN.test(reviewedRemoteVersionId)) ||
    (verifiedCandidateObjectId !== null && !UUID_PATTERN.test(verifiedCandidateObjectId))
  ) {
    throw contractInvalid();
  }
  if (resolutionKind === "save_merged") {
    if (verifiedCandidateObjectId === null) {
      throw contractInvalid();
    }
    return;
  }
  if (verifiedCandidateObjectId !== null) {
    throw contractInvalid();
  }
}

// --- the durable no-byte repair vocabularies (journal schema v9) ------------------------------------------

/**
 * The closed target-action vocabulary of one parked local apply: WHICH
 * canonical outcome the Vault still owes. The repair worker re-reads the
 * conflict detail over the wire for the winner identity, so no version
 * id, path or locator is durable here.
 */
export const CONFLICT_LOCAL_REPAIR_ACTIONS = [
  "apply_remote_version",
  "apply_resulting_version",
  "apply_remote_tombstone",
] as const;

/** One parked local apply's closed target action. */
export type ConflictLocalRepairAction = (typeof CONFLICT_LOCAL_REPAIR_ACTIONS)[number];

/**
 * The closed safe-reason vocabulary of one parked local apply: where the
 * local apply stalled. `resolution_committed` parks the fact the moment
 * the canonical resolution committed (the pre-apply crash window);
 * `winner_download_failed` and `vault_apply_failed` name the two bounded
 * retry stages of the local apply itself.
 */
export const CONFLICT_LOCAL_REPAIR_SAFE_REASONS = [
  "resolution_committed",
  "winner_download_failed",
  "vault_apply_failed",
] as const;

/** One parked local apply's closed safe reason. */
export type ConflictLocalRepairSafeReason = (typeof CONFLICT_LOCAL_REPAIR_SAFE_REASONS)[number];

/**
 * One durable pending-local-apply fact of journal schema v9: exactly the
 * conflict UUID, the resolution event identity, the closed target action,
 * the closed safe reason and the retry bookkeeping. The member set makes
 * byte, path and locator storage unrepresentable — evidence and merge
 * drafts live only in bounded ephemeral memory (Task 8).
 */
export interface PendingLocalApply {
  readonly conflictId: string;
  readonly resolutionEventId: string;
  readonly targetAction: ConflictLocalRepairAction;
  readonly safeReason: ConflictLocalRepairSafeReason;
  readonly attemptCount: number;
  readonly nextEligibleRetryEpochMs: number | null;
  readonly createdAtEpochMs: number;
  readonly updatedAtEpochMs: number;
}
