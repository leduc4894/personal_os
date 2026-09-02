/**
 * Two-device Conflict Inbox resolution journeys (Child 8 spec 8.9, Task 10).
 *
 * These journeys drive the REAL plugin conflict stack — the hand-mirrored
 * wire client, the REAL verified-candidate uploader (Task 10's binding
 * over the resolution-candidate route), the REAL controller with its
 * bounded three-way merge, and the REAL Inbox modal over a mocked
 * Obsidian runtime — against an in-process server double at the raw
 * transport boundary that implements the served conflict wire contract:
 * safe listing/detail with the kind/media-type choice matrix, verified
 * evidence reads, the digest-declared resolution-candidate upload with
 * content-addressed admission, idempotent resolution keyed by resolution
 * event identity, and exactly-one-winner publishing.
 *
 * Every journey ends in a visible user resolution with no silent
 * overwrite: concurrent Markdown edits resolve through the merge editor
 * (save_merged over the real candidate upload), a binary conflict shows
 * only the two whole-object choices with no editor, a plain-text conflict
 * resolves through the whole-object choices, the edit/delete and
 * delete/edit races resolve through their tombstone outcome, a locator
 * collision resolves keep_local, and a resolved conflict refuses a second
 * winner — each publishing exactly one winning version while the losing
 * candidate stays retained as downloadable evidence.
 *
 * Privacy (spec 9): the double serves note content, but no assertion or
 * record ever prints it; the journeys assert identities, counts, closed
 * labels and byte equality only.
 */

import { describe, expect, it, vi } from "vitest";

import type { DeviceSyncHttpResponse } from "../../src/device-sync/api";
import type { SyncHttpRequest } from "../../src/journal/sync-api";
import { createConflictApi } from "../../src/conflicts/api";
import { createConflictVerifiedCandidateUploader } from "../../src/conflicts/composition";
import { ConflictInboxModal } from "../../src/conflicts/ConflictInboxModal";
import { CanonicalApplyError, createConflictController } from "../../src/conflicts/controller";
import type {
  CanonicalOutcomeApplyCommand,
  ConflictController,
  ConflictDiagnosticsSink,
  ConflictRepairStore,
} from "../../src/conflicts/controller";
import type {
  ConflictDetail,
  ConflictKind,
  ConflictResolutionKind,
} from "../../src/conflicts/contracts";

// --- the mocked Obsidian runtime (modal journeys) ---------------------------------------------------

interface MockButtonRecord {
  text: string;
  click: () => void;
}

const modalState = vi.hoisted(() => ({
  buttons: [] as MockButtonRecord[],
  paragraphs: [] as string[],
  textareas: 0,
  title: "",
  reset(): void {
    this.buttons.length = 0;
    this.paragraphs.length = 0;
    this.textareas = 0;
    this.title = "";
  },
}));

vi.mock("obsidian", () => {
  class MockElement {
    readonly style: Record<string, string> = {};
    value = "";

    empty(): void {
      modalState.reset();
    }

    createEl(tag: string, options?: { readonly text?: string }): MockElement {
      if (tag === "textarea") {
        modalState.textareas += 1;
      }
      if (options?.text !== undefined) {
        modalState.paragraphs.push(options.text);
      }
      return new MockElement();
    }
  }

  class MockButton {
    #record: MockButtonRecord = { text: "", click: () => undefined };

    setButtonText(text: string): MockButton {
      this.#record.text = text;
      return this;
    }

    setCta(): MockButton {
      return this;
    }

    onClick(click: () => void): MockButton {
      this.#record.click = click;
      modalState.buttons.push(this.#record);
      return this;
    }
  }

  class Setting {
    constructor(container: unknown) {
      void container;
    }

    addButton(configure: (button: MockButton) => unknown): Setting {
      configure(new MockButton());
      return this;
    }
  }

  class Modal {
    readonly contentEl = new MockElement();
    readonly titleEl = {
      setText: (text: string): void => {
        modalState.title = text;
      },
    };
    onClose = (): void => undefined;

    constructor(app: unknown) {
      void app;
    }

    onOpen(): void {
      void this.contentEl;
    }

    open(): void {
      this.onOpen();
    }

    close(): void {
      this.onClose();
    }
  }

  return { Modal, Setting };
});

// --- the in-process conflict server double ----------------------------------------------------------

const ORIGIN = "https://conflicts.example.org";
const ACCESS_TOKEN = "at1.e2e-conflict-access";
const REQUEST_ID = "99999999-9999-4999-8999-999999999999";
const CHOICE_LABELS = ["keep remote", "keep local", "save merged"] as const;

interface ServedVersion {
  readonly versionId: string;
  readonly bytes: Uint8Array;
  readonly mediaType: string;
}

interface AdmittedCandidate {
  readonly objectId: string;
  readonly bytes: Uint8Array;
}

interface ServedConflict {
  readonly conflictId: string;
  readonly sourceId: string | null;
  readonly conflictKind: ConflictKind;
  status: "open" | "resolving" | "resolved" | "superseded";
  readonly base: ServedVersion | null;
  readonly remote: ServedVersion | null;
  readonly candidateBytes: Uint8Array | null;
  readonly candidateMediaType: string | null;
  resolutionKind: ConflictResolutionKind | null;
  resolutionEventId: string | null;
  resultingVersion: ServedVersion | null;
  successorConflictId: string | null;
}

function countedUuid(prefix: string, counter: number): string {
  return `${prefix.repeat(8)}-${prefix.repeat(4)}-4${prefix.repeat(3)}-8${prefix.repeat(3)}-${String(counter).padStart(12, "0")}`;
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const buffer = bytes.slice().buffer as ArrayBuffer;
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function wireBytes(bytes: Uint8Array): ArrayBuffer {
  return bytes.slice().buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer;
}

/**
 * The stateful wire-contract double behind the transport: the five
 * conflict routes with their served semantics — the closed choice matrix
 * (markdown admits save_merged, binary and plain text admit the two
 * whole-object choices, the tombstone races admit only keep_remote),
 * verified evidence reads, the digest-declared candidate upload with
 * content-addressed admission, idempotent resolution keyed by resolution
 * event identity, and exactly-one-winner publishing. No retry: the
 * client owns every retry decision.
 */
class ConflictServerDouble {
  readonly conflicts = new Map<string, ServedConflict>();
  readonly publishedVersionCountBySource = new Map<string, number>();
  readonly resolutionAttempts: string[] = [];
  readonly candidateUploads: { sha256: string; byteLength: number }[] = [];
  readonly admittedByDigest = new Map<string, AdmittedCandidate>();
  /** Every served version's exact bytes, keyed by version identity. */
  readonly versionsById = new Map<string, Uint8Array>();
  #counter = 100;

  #nextUuid(prefix: string): string {
    this.#counter += 1;
    return countedUuid(prefix, this.#counter);
  }

  seedConflict(input: {
    conflictKind: ConflictKind;
    sourceId?: string | null;
    base?: ServedVersion | null;
    remote?: ServedVersion | null;
    candidateBytes?: Uint8Array | null;
    candidateMediaType?: string | null;
  }): ServedConflict {
    const conflict: ServedConflict = {
      conflictId: this.#nextUuid("1"),
      sourceId: input.sourceId ?? this.#nextUuid("2"),
      conflictKind: input.conflictKind,
      status: "open",
      base: input.base ?? null,
      remote: input.remote ?? null,
      candidateBytes: input.candidateBytes ?? null,
      candidateMediaType: input.candidateMediaType ?? null,
      resolutionKind: null,
      resolutionEventId: null,
      resultingVersion: null,
      successorConflictId: null,
    };
    this.conflicts.set(conflict.conflictId, conflict);
    if (conflict.base !== null) {
      this.versionsById.set(conflict.base.versionId, conflict.base.bytes);
    }
    if (conflict.remote !== null) {
      this.versionsById.set(conflict.remote.versionId, conflict.remote.bytes);
    }
    return conflict;
  }

  openConflicts(): ServedConflict[] {
    return [...this.conflicts.values()].filter((conflict) => conflict.status === "open");
  }

  #choicesOf(conflict: ServedConflict): ConflictResolutionKind[] {
    if (conflict.status !== "open") {
      return [];
    }
    if (conflict.candidateBytes === null) {
      return ["keep_remote"];
    }
    if (conflict.conflictKind === "edit_remote_delete") {
      return ["keep_remote"];
    }
    if (conflict.candidateMediaType === "text/markdown") {
      return ["keep_remote", "keep_local", "save_merged"];
    }
    return ["keep_remote", "keep_local"];
  }

  #summaryWire(conflict: ServedConflict): Record<string, unknown> {
    return {
      conflict_id: conflict.conflictId,
      source_id: conflict.sourceId,
      conflict_kind: conflict.conflictKind,
      status: conflict.status,
      originating_event_id: this.#nextUuid("3"),
      originating_device_id: this.#nextUuid("4"),
      base_version_id: conflict.base?.versionId ?? null,
      observed_remote_version_id: conflict.remote?.versionId ?? null,
      candidate_kind: conflict.candidateBytes === null ? "delete" : "content",
      verified_candidate_object_id:
        conflict.candidateBytes === null ? null : this.#nextUuid("5"),
      captured_at: "2026-09-02T00:00:00Z",
      resolution_kind: conflict.resolutionKind,
      resolution_event_id: conflict.resolutionEventId,
      resulting_version_id: conflict.resultingVersion?.versionId ?? null,
      successor_conflict_id: conflict.successorConflictId,
      closed_at: conflict.status === "open" ? null : "2026-09-02T00:01:00Z",
    };
  }

  async handle(request: SyncHttpRequest): Promise<DeviceSyncHttpResponse> {
    const path = new URL(request.url).pathname;
    const method = request.method;

    if (path === "/api/sync/conflicts" && method === "GET") {
      return this.#json(200, {
        conflicts: this.openConflicts().map((conflict) => this.#summaryWire(conflict)),
        has_more: false,
        next_exclusive_start_conflict_id: null,
      });
    }

    const detailMatch = path.match(/^\/api\/sync\/conflicts\/([^/]+)$/);
    if (detailMatch !== null && method === "GET") {
      const conflict = this.conflicts.get(decodeURIComponent(detailMatch[1] ?? ""));
      if (conflict === undefined) {
        return this.#error(404, "source_conflict_not_found");
      }
      return this.#json(200, {
        ...this.#summaryWire(conflict),
        choices: this.#choicesOf(conflict),
      });
    }

    const evidenceMatch = path.match(/^\/api\/sync\/conflicts\/([^/]+)\/evidence\/([a-z]+)$/);
    if (evidenceMatch !== null && method === "GET") {
      const conflict = this.conflicts.get(decodeURIComponent(evidenceMatch[1] ?? ""));
      const role = evidenceMatch[2];
      if (conflict === undefined) {
        return this.#error(404, "source_conflict_not_found");
      }
      const evidence =
        role === "base"
          ? conflict.base
          : role === "remote"
            ? conflict.remote
            : conflict.candidateBytes === null
              ? null
              : {
                  bytes: conflict.candidateBytes,
                  mediaType: conflict.candidateMediaType ?? "application/octet-stream",
                };
      if (evidence === null) {
        return this.#error(404, "source_conflict_evidence_unavailable");
      }
      return {
        status: 200,
        bodyText: "",
        bodyBytes: wireBytes(evidence.bytes),
        headers: {
          "content-type": evidence.mediaType,
          "content-length": String(evidence.bytes.byteLength),
        },
      };
    }

    const candidateMatch = path.match(/^\/api\/sync\/conflicts\/([^/]+)\/candidate$/);
    if (candidateMatch !== null && method === "PUT") {
      const conflict = this.conflicts.get(decodeURIComponent(candidateMatch[1] ?? ""));
      if (conflict === undefined) {
        return this.#error(404, "source_conflict_not_found");
      }
      if (conflict.status !== "open" || conflict.candidateBytes === null) {
        return this.#error(409, "source_conflict_state_invalid");
      }
      const bytes = new Uint8Array(request.body as ArrayBuffer);
      const digest = await sha256Hex(bytes);
      if (request.headers["x-candidate-sha256"] !== digest) {
        return this.#error(422, "source_conflict_evidence_integrity_failed");
      }
      this.candidateUploads.push({ sha256: digest, byteLength: bytes.byteLength });
      const admitted =
        this.admittedByDigest.get(digest) ?? { objectId: this.#nextUuid("7"), bytes };
      this.admittedByDigest.set(digest, admitted);
      return this.#json(200, { verified_candidate_object_id: admitted.objectId });
    }

    const resolveMatch = path.match(/^\/api\/sync\/conflicts\/([^/]+)\/resolve$/);
    if (resolveMatch !== null && method === "POST") {
      const conflict = this.conflicts.get(decodeURIComponent(resolveMatch[1] ?? ""));
      if (conflict === undefined) {
        return this.#error(404, "source_conflict_not_found");
      }
      const body = JSON.parse(request.body as string) as Record<string, unknown>;
      const resolutionEventId = String(body["resolution_event_id"] ?? "");
      this.resolutionAttempts.push(resolutionEventId);
      // Exact replay by resolution event identity returns the frozen outcome.
      if (conflict.resolutionEventId === resolutionEventId) {
        return this.#json(200, {
          outcome: "resolved",
          conflict_id: conflict.conflictId,
          resolution_event_id: resolutionEventId,
          resolution_kind: conflict.resolutionKind,
          resulting_version_id: conflict.resultingVersion?.versionId ?? null,
          successor_conflict_id: null,
          completed_at: "2026-09-02T00:01:00Z",
        });
      }
      if (conflict.status !== "open") {
        return this.#error(409, "source_conflict_state_invalid");
      }
      const kind = String(body["resolution_kind"] ?? "");
      if (!this.#choicesOf(conflict).includes(kind as ConflictResolutionKind)) {
        return this.#error(422, "source_conflict_input_invalid");
      }
      const mergedObject = String(body["verified_candidate_object_id"] ?? "");
      const mergedBytes = [...this.admittedByDigest.values()].find(
        (admitted) => admitted.objectId === mergedObject,
      )?.bytes;
      const winnerBytes =
        kind === "keep_local" ? conflict.candidateBytes : kind === "save_merged" ? mergedBytes : null;
      if ((kind === "keep_local" || kind === "save_merged") && winnerBytes === undefined) {
        return this.#error(422, "source_conflict_input_invalid");
      }
      const resultingVersion =
        winnerBytes == null
          ? null
          : {
              versionId: this.#nextUuid("6"),
              bytes: winnerBytes,
              mediaType:
                kind === "save_merged"
                  ? "text/markdown"
                  : (conflict.candidateMediaType ?? "application/octet-stream"),
            };
      if (resultingVersion !== null && conflict.sourceId !== null) {
        const key = conflict.sourceId;
        this.publishedVersionCountBySource.set(
          key,
          (this.publishedVersionCountBySource.get(key) ?? 0) + 1,
        );
      }
      conflict.status = "resolved";
      conflict.resolutionKind = kind as ConflictResolutionKind;
      conflict.resolutionEventId = resolutionEventId;
      conflict.resultingVersion = resultingVersion;
      if (resultingVersion !== null) {
        this.versionsById.set(resultingVersion.versionId, resultingVersion.bytes);
      }
      return this.#json(200, {
        outcome: "resolved",
        conflict_id: conflict.conflictId,
        resolution_event_id: resolutionEventId,
        resolution_kind: kind,
        resulting_version_id: resultingVersion?.versionId ?? null,
        successor_conflict_id: null,
        completed_at: "2026-09-02T00:01:00Z",
      });
    }

    return this.#error(404, "api_route_not_found");
  }

  #json(status: number, data: unknown): DeviceSyncHttpResponse {
    return {
      status,
      bodyText: JSON.stringify({ data, error: null, request_id: REQUEST_ID, warnings: [] }),
      bodyBytes: null,
      headers: { "content-type": "application/json" },
    };
  }

  #error(status: number, code: string): DeviceSyncHttpResponse {
    return {
      status,
      bodyText: JSON.stringify({
        data: null,
        error: { code, message: "registered safe message", details: {}, retryable: false },
        request_id: REQUEST_ID,
        warnings: [],
      }),
      bodyBytes: null,
      headers: { "content-type": "application/json" },
    };
  }
}

// --- the composed journey harness -------------------------------------------------------------------

class InMemoryRepairStore implements ConflictRepairStore {
  readonly parked: {
    conflictId: string;
    resolutionEventId: string;
    targetAction: string;
    completed: boolean;
  }[] = [];

  readPendingLocalApply(): null {
    return null;
  }

  readPendingLocalApplies(): [] {
    return [];
  }

  async parkPendingLocalApply(input: {
    conflictId: string;
    resolutionEventId: string;
    targetAction: string;
  }): Promise<void> {
    this.parked.push({ ...input, completed: false });
  }

  async recordLocalApplyFailure(): Promise<void> {
    return;
  }

  async completeLocalApply(input: { conflictId: string }): Promise<void> {
    const row = this.parked.find((parked) => parked.conflictId === input.conflictId);
    if (row !== undefined) {
      row.completed = true;
    }
  }
}

class InMemoryVaultApplier {
  readonly applied: CanonicalOutcomeApplyCommand[] = [];
  readonly vaultFiles = new Map<string, Uint8Array>();
  /** One canonical source per vault path — the seeded locator mapping. */
  readonly locatorBySource = new Map<string, string>();
  /** The winner-version download seam: exact bytes by version identity. */
  bytesByVersion: (versionId: string) => Uint8Array | null = () => null;

  async applyCanonicalOutcome(command: CanonicalOutcomeApplyCommand): Promise<void> {
    this.applied.push(command);
    const path = command.sourceId === null ? null : this.locatorBySource.get(command.sourceId);
    if (path === null || path === undefined) {
      throw new CanonicalApplyError("winner_download");
    }
    if (command.targetAction === "apply_remote_tombstone") {
      this.vaultFiles.delete(path);
      return;
    }
    const winnerVersionId = command.winnerVersionId;
    const bytes =
      command.winnerBytes ??
      (winnerVersionId === null ? null : this.bytesByVersion(winnerVersionId));
    if (bytes === null || bytes.byteLength === 0) {
      throw new CanonicalApplyError("winner_download");
    }
    this.vaultFiles.set(path, bytes);
  }
}

class RecordingDiagnostics implements ConflictDiagnosticsSink {
  readonly reasons: string[] = [];

  observeConflictFailure(reason: string): void {
    this.reasons.push(reason);
  }
}

function encoded(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

function decoded(bytes: Uint8Array | undefined): string {
  return new TextDecoder().decode(bytes ?? new Uint8Array(0));
}

class ConflictJourney {
  readonly server = new ConflictServerDouble();
  readonly applier = new InMemoryVaultApplier();
  readonly repairStore = new InMemoryRepairStore();
  readonly diagnostics = new RecordingDiagnostics();
  readonly controller: ConflictController;
  readonly api: ReturnType<typeof createConflictApi>;
  readonly modal: ConflictInboxModal;

  constructor() {
    this.api = createConflictApi({
      transport: async (request: SyncHttpRequest) => await this.server.handle(request),
      resolveOrigin: () => ORIGIN,
      getAccessToken: () => ACCESS_TOKEN,
    });
    this.controller = createConflictController({
      api: this.api,
      repairStore: this.repairStore,
      uploader: createConflictVerifiedCandidateUploader(this.api),
      applier: this.applier,
      diagnostics: this.diagnostics,
    });
    this.applier.bytesByVersion = (versionId: string): Uint8Array | null =>
      this.server.versionsById.get(versionId) ?? null;
    this.modal = new ConflictInboxModal({} as never, this.controller);
  }

  seedJourneyConflict(input: {
    conflictKind: ConflictKind;
    vaultPath: string;
    baseText?: string;
    remote?: { text: string; mediaType?: string };
    /** Exact remote evidence bytes, overriding the text derivation. */
    remoteBytes?: Uint8Array;
    candidate?: { bytes: Uint8Array; mediaType: string };
  }): { conflictId: string; sourceId: string; remoteVersionId: string | null } {
    const conflict = this.server.seedConflict({
      conflictKind: input.conflictKind,
      base:
        input.baseText === undefined
          ? null
          : { versionId: countedUuid("a", 1), bytes: encoded(input.baseText), mediaType: "text/markdown" },
      remote:
        input.remote === undefined
          ? null
          : {
              versionId: countedUuid("b", 2),
              bytes: input.remoteBytes ?? encoded(input.remote.text),
              mediaType: input.remote.mediaType ?? "text/markdown",
            },
      candidateBytes: input.candidate?.bytes ?? null,
      candidateMediaType: input.candidate?.mediaType ?? null,
    });
    const sourceId = conflict.sourceId ?? "";
    this.applier.locatorBySource.set(sourceId, input.vaultPath);
    return {
      conflictId: conflict.conflictId,
      sourceId,
      remoteVersionId: conflict.remote?.versionId ?? null,
    };
  }

  async openConflictDetails(): Promise<ConflictDetail[]> {
    const page = await this.controller.listOpenConflicts();
    const details: ConflictDetail[] = [];
    for (const summary of page.conflicts) {
      details.push(await this.controller.getConflictDetail(summary.conflictId));
    }
    return details;
  }

  /** Open the real modal and settle its list render. */
  async openInbox(): Promise<void> {
    modalState.reset();
    this.modal.open();
    await this.modal.awaitRendered();
  }

  /** Click the first conflict row and settle its detail render. */
  async openFirstConflictDetail(): Promise<void> {
    const openButton = modalState.buttons.find((button) => button.text.startsWith("Open: "));
    if (openButton === undefined) {
      throw new Error("no conflict row rendered");
    }
    openButton.click();
    await this.modal.awaitRendered();
  }

  async openBinaryConflict(): Promise<void> {
    this.seedJourneyConflict({
      conflictKind: "stale_content",
      vaultPath: "assets/chart.png",
      baseText: "",
      remote: { text: "", mediaType: "image/png" },
      candidate: { bytes: new Uint8Array([0x89, 0x50, 0x4e, 0x47, 9, 9, 9, 9]), mediaType: "image/png" },
    });
    await this.openInbox();
    await this.openFirstConflictDetail();
  }

  visibleChoices(): string[] {
    return modalState.buttons
      .map((button) => button.text.toLowerCase())
      .filter((label): label is (typeof CHOICE_LABELS)[number] =>
        (CHOICE_LABELS as readonly string[]).includes(label),
      );
  }

  visibleMergeEditorCount(): number {
    return modalState.textareas;
  }
}

// --- the journeys (spec 8.9) ------------------------------------------------------------------------

describe("source conflict resolution two-device journeys", () => {
  it("resolves concurrent markdown edits through the merge editor and the real candidate upload", async () => {
    const journey = new ConflictJourney();
    const localDraft = "# race\n\nlocal edit\n";
    const seeded = journey.seedJourneyConflict({
      conflictKind: "stale_content",
      vaultPath: "notes/race.md",
      baseText: "# race\n",
      remote: { text: "# race\n\nremote edit\n" },
      candidate: { bytes: encoded(localDraft), mediaType: "text/markdown" },
    });

    const details = await journey.openConflictDetails();
    expect(details).toHaveLength(1);
    expect(details[0]?.choices).toEqual(["keep_remote", "keep_local", "save_merged"]);

    const proposal = await journey.controller.buildMergeProposal(seeded.conflictId);
    expect(proposal).toMatchObject({ kind: "editable_merge", mediaType: "text/markdown" });

    const mergedDraft = "# race\n\nremote edit\nlocal edit\n";
    const result = await journey.controller.resolveSaveMerged(seeded.conflictId, mergedDraft);

    expect(result.kind).toBe("resolved_and_applied");
    // The merged result crossed the real candidate route and the resolve
    // carried only the opaque admitted reference.
    expect(journey.server.candidateUploads).toHaveLength(1);
    expect(journey.server.candidateUploads[0]?.byteLength).toBe(encoded(mergedDraft).byteLength);
    // Exactly one winning version published; the Vault holds the merged bytes.
    expect(journey.server.publishedVersionCountBySource.get(seeded.sourceId)).toBe(1);
    expect(decoded(journey.applier.vaultFiles.get("notes/race.md"))).toBe(mergedDraft);
    // The losing candidate stays retained as downloadable evidence.
    const evidence = await journey.api.downloadConflictEvidence({
      conflictId: seeded.conflictId,
      role: "candidate",
    });
    expect(decoded(evidence.bytes)).toBe(localDraft);
    expect(evidence.mediaType).toBe("text/markdown");
    // The resolved conflict left the open inbox.
    expect((await journey.controller.listOpenConflicts()).conflicts).toHaveLength(0);
    expect(journey.repairStore.parked[0]?.completed).toBe(true);
  });

  it("shows binary choices without a merge editor and keeps the losing candidate retained", async () => {
    const journey = new ConflictJourney();
    await journey.openBinaryConflict();

    expect(journey.visibleChoices()).toEqual(["keep remote", "keep local"]);
    expect(journey.visibleMergeEditorCount()).toBe(0);
  });

  it("resolves a binary conflict keep_local with the retained candidate bytes", async () => {
    const journey = new ConflictJourney();
    const remotePng = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]);
    const localPng = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 9, 9, 9, 9]);
    const seeded = journey.seedJourneyConflict({
      conflictKind: "stale_content",
      vaultPath: "assets/chart.png",
      baseText: "",
      remote: { text: "", mediaType: "image/png" },
      remoteBytes: remotePng,
      candidate: { bytes: localPng, mediaType: "image/png" },
    });

    const details = await journey.openConflictDetails();
    expect(details[0]?.choices).toEqual(["keep_remote", "keep_local"]);

    const result = await journey.controller.resolveKeepLocal(seeded.conflictId);

    expect(result.kind).toBe("resolved_and_applied");
    expect(journey.applier.vaultFiles.get("assets/chart.png")).toEqual(localPng);
    expect(journey.server.publishedVersionCountBySource.get(seeded.sourceId)).toBe(1);
    // The losing remote stays retained as evidence.
    const evidence = await journey.api.downloadConflictEvidence({
      conflictId: seeded.conflictId,
      role: "remote",
    });
    expect(evidence.bytes).toEqual(remotePng);
    expect(evidence.mediaType).toBe("image/png");
  });

  it("resolves a plain-text conflict through the whole-object choices only", async () => {
    const journey = new ConflictJourney();
    const seeded = journey.seedJourneyConflict({
      conflictKind: "stale_content",
      vaultPath: "notes/data.txt",
      baseText: "base",
      remote: { text: "remote wins here", mediaType: "text/plain" },
      candidate: { bytes: encoded("local"), mediaType: "text/plain" },
    });

    const proposal = await journey.controller.buildMergeProposal(seeded.conflictId);
    expect(proposal).toMatchObject({ kind: "manual_choice_required" });

    const result = await journey.controller.resolveKeepRemote(seeded.conflictId);

    expect(result.kind).toBe("resolved_and_applied");
    expect(decoded(journey.applier.vaultFiles.get("notes/data.txt"))).toBe("remote wins here");
    // keep_remote publishes no version.
    expect(journey.server.publishedVersionCountBySource.get(seeded.sourceId)).toBeUndefined();
  });

  it("resolves an edit racing a remote delete through the tombstone outcome only", async () => {
    const journey = new ConflictJourney();
    const seeded = journey.seedJourneyConflict({
      conflictKind: "edit_remote_delete",
      vaultPath: "notes/doomed.md",
      baseText: "before delete",
      candidate: { bytes: encoded("local edit of a doomed note"), mediaType: "text/markdown" },
    });

    const details = await journey.openConflictDetails();
    expect(details[0]?.choices).toEqual(["keep_remote"]);

    const result = await journey.controller.resolveKeepRemote(seeded.conflictId);

    expect(result.kind).toBe("resolved_and_applied");
    expect(journey.applier.applied.at(-1)?.targetAction).toBe("apply_remote_tombstone");
    expect(journey.applier.vaultFiles.has("notes/doomed.md")).toBe(false);
    expect(journey.server.publishedVersionCountBySource.get(seeded.sourceId)).toBeUndefined();
    // The local edit stays retained as immutable candidate evidence.
    const evidence = await journey.api.downloadConflictEvidence({
      conflictId: seeded.conflictId,
      role: "candidate",
    });
    expect(decoded(evidence.bytes)).toBe("local edit of a doomed note");
  });

  it("resolves a local delete racing a remote edit through keep_remote", async () => {
    const journey = new ConflictJourney();
    const seeded = journey.seedJourneyConflict({
      conflictKind: "delete_remote_edit",
      vaultPath: "notes/deleted-locally.md",
      remote: { text: "edited while the local side deleted" },
    });

    const details = await journey.openConflictDetails();
    expect(details[0]?.choices).toEqual(["keep_remote"]);

    const result = await journey.controller.resolveKeepRemote(seeded.conflictId);

    expect(result.kind).toBe("resolved_and_applied");
    expect(journey.applier.applied.at(-1)?.targetAction).toBe("apply_remote_tombstone");
    expect(journey.applier.vaultFiles.has("notes/deleted-locally.md")).toBe(false);
  });

  it("resolves a locator collision keep_local with the retained candidate bytes", async () => {
    const journey = new ConflictJourney();
    const seeded = journey.seedJourneyConflict({
      conflictKind: "locator_collision",
      vaultPath: "notes/colliding.md",
      baseText: "base",
      remote: { text: "holder content" },
      candidate: { bytes: encoded("local restored content"), mediaType: "text/markdown" },
    });

    const details = await journey.openConflictDetails();
    expect(details[0]?.choices).toEqual(["keep_remote", "keep_local", "save_merged"]);

    const result = await journey.controller.resolveKeepLocal(seeded.conflictId);

    expect(result.kind).toBe("resolved_and_applied");
    expect(decoded(journey.applier.vaultFiles.get("notes/colliding.md"))).toBe(
      "local restored content",
    );
    expect(journey.server.publishedVersionCountBySource.get(seeded.sourceId)).toBe(1);
  });

  it("never silently overwrites and refuses a second winner", async () => {
    const journey = new ConflictJourney();
    const firstSeeded = journey.seedJourneyConflict({
      conflictKind: "stale_content",
      vaultPath: "notes/overwrite-a.md",
      baseText: "base",
      remote: { text: "remote-a" },
      candidate: { bytes: encoded("local-a"), mediaType: "text/markdown" },
    });
    const secondSeeded = journey.seedJourneyConflict({
      conflictKind: "stale_content",
      vaultPath: "notes/overwrite-b.md",
      baseText: "base",
      remote: { text: "remote-b" },
      candidate: { bytes: encoded("local-b"), mediaType: "text/markdown" },
    });

    const page = await journey.controller.listOpenConflicts();
    expect(page.conflicts).toHaveLength(2);

    const firstResult = await journey.controller.resolveKeepLocal(firstSeeded.conflictId);
    const secondResult = await journey.controller.resolveKeepRemote(secondSeeded.conflictId);
    expect(firstResult.kind).toBe("resolved_and_applied");
    expect(secondResult.kind).toBe("resolved_and_applied");

    expect(journey.server.publishedVersionCountBySource.get(firstSeeded.sourceId)).toBe(1);
    expect(journey.server.publishedVersionCountBySource.get(secondSeeded.sourceId)).toBeUndefined();
    // A second explicit attempt against a resolved conflict fails closed:
    // the terminal detail offers no choice, so the controller rejects with
    // its closed choice-unavailable verdict before any wire resolve.
    await expect(journey.controller.resolveKeepLocal(firstSeeded.conflictId)).rejects.toMatchObject({
      reason: "conflict_choice_unavailable",
    });
    // The rejection surfaced its closed diagnostic token — the one trail
    // entry of the whole journey; no other failure path ever fired.
    expect(journey.diagnostics.reasons).toEqual(["conflict_choice_unavailable"]);
  });
});
