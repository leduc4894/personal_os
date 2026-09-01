/**
 * Tests of the explicit Conflict Inbox modal (Child 8 spec 5.2, Task 8).
 *
 * These tests pin the modal's choice matrix: the rendered choice buttons
 * come EXACTLY from the server-admitted `choices` array (a byteless
 * delete_remote_edit offers only keep_remote; a binary conflict offers no
 * merge editor and no save_merged action), the merge editor renders only
 * for an editable text/Markdown proposal, a manual-choice-required proposal
 * renders no editor, drafts are discarded and re-fetched instead of being
 * persisted, and every status/outcome paragraph renders closed labels only
 * — no raw note content ever reaches a rendered status text.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

type MockListener = (event: { readonly key?: string; preventDefault(): void }) => void;

interface MockElementRecord {
  readonly tag: string;
  readonly text: string | undefined;
  readonly element: { value: string };
  readonly listeners: Map<string, MockListener>;
}

interface MockButtonRecord {
  text: string;
  click: () => void;
}

const mockState = vi.hoisted(() => ({
  elements: [] as MockElementRecord[],
  buttons: [] as MockButtonRecord[],
  title: "",
}));

vi.mock("obsidian", () => {
  class MockElement {
    readonly style: Record<string, string> = {};
    value = "";
    type = "";
    readonly listeners = new Map<string, MockListener>();

    empty(): void {
      // Live-DOM semantics: emptying the content clears the recorded
      // elements and buttons, so the mock state tracks only live nodes.
      mockState.elements.length = 0;
      mockState.buttons.length = 0;
      this.value = "";
    }

    createEl(tag: string, options?: { readonly text?: string }): MockElement {
      const element = new MockElement();
      mockState.elements.push({ tag, text: options?.text, element, listeners: element.listeners });
      return element;
    }
  }

  class Modal {
    readonly contentEl = new MockElement();
    readonly titleEl = {
      setText: (text: string): void => {
        mockState.title = text;
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
      mockState.buttons.push(this.#record);
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

  return { Modal, Setting };
});

import { ConflictInboxModal } from "./ConflictInboxModal";
import { ConflictApiError } from "./api";
import type { ConflictController, ConflictMergeProposal, ConflictResolutionCommandResult } from "./controller";
import type { ConflictDetail, ConflictPage, ConflictResolution, ConflictSummary } from "./contracts";

// --- fixtures ------------------------------------------------------------------------------------------

const CONFLICT_ID = "11111111-1111-4111-8111-111111111111";
const SUCCESSOR_CONFLICT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const SECRET_CONTENT = "# secret note content that must never leak into a status text";
const MERGED_TEXT = `# merged draft\n${SECRET_CONTENT}`;

function buildSummary(overrides?: Partial<ConflictSummary>): ConflictSummary {
  return {
    conflictId: CONFLICT_ID,
    sourceId: "22222222-2222-4222-8222-222222222222",
    conflictKind: "stale_content",
    status: "open",
    originatingEventId: "33333333-3333-4333-8333-333333333333",
    originatingDeviceId: "44444444-4444-4444-8444-444444444444",
    baseVersionId: "55555555-5555-4555-8555-555555555555",
    observedRemoteVersionId: "66666666-6666-4666-8666-666666666666",
    candidateKind: "content",
    verifiedCandidateObjectId: "77777777-7777-4777-8777-777777777777",
    capturedAt: "2026-09-01T00:00:00Z",
    resolutionKind: null,
    resolutionEventId: null,
    resultingVersionId: null,
    successorConflictId: null,
    closedAt: null,
    ...overrides,
  };
}

function buildDetail(overrides?: Partial<ConflictDetail>): ConflictDetail {
  return {
    ...buildSummary(),
    choices: ["keep_remote", "keep_local", "save_merged"],
    ...overrides,
  };
}

function resolutionFixture(): ConflictResolution {
  return {
    outcome: "resolved",
    conflictId: CONFLICT_ID,
    resolutionEventId: "88888888-8888-4888-8888-888888888888",
    resolutionKind: "keep_remote",
    resultingVersionId: null,
    successorConflictId: null,
    completedAt: "2026-09-02T00:00:00Z",
  };
}

function editableProposal(): ConflictMergeProposal {
  return {
    kind: "editable_merge",
    mergedText: MERGED_TEXT,
    requiresUserReview: true,
    conflictingHunkCount: 1,
    mediaType: "text/markdown",
  };
}

function kindLabelOf(kind: ConflictSummary["conflictKind"]): string {
  switch (kind) {
    case "stale_content":
      return "Content conflict";
    case "edit_remote_delete":
      return "Edit vs remote delete";
    case "delete_remote_edit":
      return "Delete vs remote edit";
    case "locator_collision":
      return "Path collision";
  }
}

// --- the controller fake ---------------------------------------------------------------------------------

interface ModalControllerFake {
  readonly controller: ConflictController;
  readonly listCalls: number;
  readonly detailCalls: readonly string[];
  readonly proposalCalls: readonly string[];
  readonly keepRemoteCalls: readonly string[];
  readonly saveMergedCalls: readonly { conflictId: string; editedText: string }[];
  setPage(page: ConflictPage): void;
  setDetail(detail: ConflictDetail): void;
  setProposal(proposal: ConflictMergeProposal): void;
  setKeepRemoteResult(result: ConflictResolutionCommandResult | Error): void;
  setListFailure(failure: Error | null): void;
}

function createControllerFake(): ModalControllerFake {
  let page: ConflictPage = {
    conflicts: [buildSummary()],
    hasMore: false,
    nextExclusiveStartConflictId: null,
  };
  const details = new Map<string, ConflictDetail>();
  const proposals = new Map<string, ConflictMergeProposal>();
  let keepRemoteResult: ConflictResolutionCommandResult | Error = {
    kind: "resolved_and_applied",
    resolution: resolutionFixture(),
  };
  let listFailure: Error | null = null;
  let listCalls = 0;
  const detailCalls: string[] = [];
  const proposalCalls: string[] = [];
  const keepRemoteCalls: string[] = [];
  const saveMergedCalls: { conflictId: string; editedText: string }[] = [];
  const controller: ConflictController = {
    async listOpenConflicts() {
      listCalls += 1;
      if (listFailure !== null) {
        throw listFailure;
      }
      return page;
    },
    async getConflictDetail(conflictId) {
      detailCalls.push(conflictId);
      const detail = details.get(conflictId);
      if (detail === undefined) {
        throw new ConflictApiError("conflict_not_found", false);
      }
      return detail;
    },
    async buildMergeProposal(conflictId) {
      proposalCalls.push(conflictId);
      const proposal = proposals.get(conflictId);
      if (proposal === undefined) {
        throw new ConflictApiError("evidence_unavailable", false);
      }
      return proposal;
    },
    async resolveKeepRemote(conflictId) {
      keepRemoteCalls.push(conflictId);
      if (keepRemoteResult instanceof Error) {
        throw keepRemoteResult;
      }
      return keepRemoteResult;
    },
    async resolveKeepLocal() {
      throw new Error("keep local is not exercised by this fake");
    },
    async resolveSaveMerged(conflictId, editedText) {
      saveMergedCalls.push({ conflictId, editedText });
      return { kind: "resolved_and_applied", resolution: resolutionFixture() };
    },
    async retryPendingLocalApplies() {
      return;
    },
  };
  return {
    controller,
    get listCalls() {
      return listCalls;
    },
    get detailCalls() {
      return detailCalls;
    },
    get proposalCalls() {
      return proposalCalls;
    },
    get keepRemoteCalls() {
      return keepRemoteCalls;
    },
    get saveMergedCalls() {
      return saveMergedCalls;
    },
    setPage(next) {
      page = next;
    },
    setDetail(detail) {
      details.set(detail.conflictId, detail);
    },
    setProposal(proposal) {
      proposals.set(CONFLICT_ID, proposal);
    },
    setKeepRemoteResult(result) {
      keepRemoteResult = result;
    },
    setListFailure(failure) {
      listFailure = failure;
    },
  };
}

// --- the harness ------------------------------------------------------------------------------------------

function buttonLabels(): string[] {
  return mockState.buttons.map((button) => button.text);
}

function clickButton(label: string): void {
  const button = mockState.buttons.find((candidate) => candidate.text === label);
  if (button === undefined) {
    throw new Error(`button unavailable: ${label}`);
  }
  button.click();
}

function requireTextarea(): { value: string } {
  const editor = mockState.elements.find((element) => element.tag === "textarea");
  if (editor === undefined) {
    throw new Error("textarea unavailable");
  }
  return editor.element;
}

function paragraphTexts(): string[] {
  return mockState.elements
    .filter((element) => element.tag === "p")
    .map((element) => element.text ?? "");
}

async function openInbox(fake: ModalControllerFake): Promise<ConflictInboxModal> {
  const modal = new ConflictInboxModal({} as never, fake.controller);
  modal.open();
  await modal.awaitRendered();
  return modal;
}

async function openDetail(
  fake: ModalControllerFake,
  detail: ConflictDetail,
): Promise<ConflictInboxModal> {
  fake.setDetail(detail);
  fake.setPage({
    conflicts: [
      buildSummary({ conflictKind: detail.conflictKind, candidateKind: detail.candidateKind }),
    ],
    hasMore: false,
    nextExclusiveStartConflictId: null,
  });
  const modal = await openInbox(fake);
  clickButton(`Open: ${kindLabelOf(detail.conflictKind)}`);
  await modal.awaitRendered();
  return modal;
}

beforeEach(() => {
  mockState.elements.length = 0;
  mockState.buttons.length = 0;
  mockState.title = "";
});

// --- the inbox list ---------------------------------------------------------------------------------------

describe("conflict inbox list rendering", () => {
  it("renders one row per open conflict from the controller page", async () => {
    const fake = createControllerFake();
    fake.setPage({
      conflicts: [
        buildSummary(),
        buildSummary({
          conflictId: SUCCESSOR_CONFLICT_ID,
          conflictKind: "delete_remote_edit",
          candidateKind: "delete",
        }),
      ],
      hasMore: false,
      nextExclusiveStartConflictId: null,
    });

    await openInbox(fake);

    expect(mockState.title).toBe("Conflict Inbox");
    expect(buttonLabels()).toEqual(["Open: Content conflict", "Open: Delete vs remote edit"]);
  });

  it("renders the empty state when no conflicts are open", async () => {
    const fake = createControllerFake();
    fake.setPage({ conflicts: [], hasMore: false, nextExclusiveStartConflictId: null });

    await openInbox(fake);

    expect(paragraphTexts()).toContain("No open conflicts.");
    expect(buttonLabels()).toEqual([]);
  });

  it("renders the closed failure token of a failed list fetch, never a bare reason placeholder", async () => {
    const fake = createControllerFake();
    fake.setListFailure(new ConflictApiError("network_offline", true));

    await openInbox(fake);

    expect(fake.listCalls).toBe(1);
    const paragraphs = paragraphTexts().join("\n");
    expect(paragraphs).toContain("Inbox failed: network_offline");
    expect(paragraphs).not.toContain("reason_unavailable");
    expect(paragraphs).not.toContain(SECRET_CONTENT);
    expect(paragraphs).not.toContain(CONFLICT_ID);
    expect(buttonLabels()).toEqual(["Back to inbox"]);
  });
});

// --- the choice matrix --------------------------------------------------------------------------------------

describe("conflict inbox detail choice matrix (spec 5.2.2)", () => {
  it("renders exactly the server-admitted choices for a text conflict", async () => {
    const fake = createControllerFake();
    await openDetail(fake, buildDetail({ choices: ["keep_remote", "keep_local", "save_merged"] }));

    expect(buttonLabels()).toEqual(["Keep remote", "Keep local", "Edit merged result…", "Back"]);
  });

  it("renders only keep_remote for a byteless delete_remote_edit conflict", async () => {
    const fake = createControllerFake();
    await openDetail(
      fake,
      buildDetail({
        conflictKind: "delete_remote_edit",
        candidateKind: "delete",
        choices: ["keep_remote"],
      }),
    );

    expect(buttonLabels()).toEqual(["Keep remote", "Back"]);
  });

  it("renders no merge editor entry for a binary conflict without save_merged", async () => {
    const fake = createControllerFake();
    await openDetail(
      fake,
      buildDetail({ conflictKind: "locator_collision", choices: ["keep_remote", "keep_local"] }),
    );

    expect(buttonLabels()).toEqual(["Keep remote", "Keep local", "Back"]);
    expect(mockState.elements.some((element) => element.tag === "textarea")).toBe(false);
  });
});

// --- the merge editor -----------------------------------------------------------------------------------------

describe("conflict inbox merge editor (spec 5.2.2)", () => {
  it("renders the editable proposal text and saves the edited draft through the controller", async () => {
    const fake = createControllerFake();
    fake.setProposal(editableProposal());
    const modal = await openDetail(fake, buildDetail());

    clickButton("Edit merged result…");
    await modal.awaitRendered();

    expect(requireTextarea().value).toBe(MERGED_TEXT);

    const edited = `${MERGED_TEXT}\nresolved by hand`;
    requireTextarea().value = edited;
    clickButton("Save merged");
    await modal.awaitRendered();

    expect(fake.saveMergedCalls).toEqual([{ conflictId: CONFLICT_ID, editedText: edited }]);
    expect(paragraphTexts().some((text) => text.startsWith("Resolved"))).toBe(true);
  });

  it("renders no editor and no save action when the proposal demands a manual choice", async () => {
    const fake = createControllerFake();
    fake.setProposal({ kind: "manual_choice_required", reason: "merge_bound_exceeded" });
    const modal = await openDetail(fake, buildDetail());

    clickButton("Edit merged result…");
    await modal.awaitRendered();

    expect(mockState.elements.some((element) => element.tag === "textarea")).toBe(false);
    expect(buttonLabels()).not.toContain("Save merged");
    expect(paragraphTexts().some((text) => text.includes("Manual choice"))).toBe(true);
    expect(fake.saveMergedCalls).toEqual([]);
  });

  it("discards the draft and re-fetches a fresh proposal instead of persisting it", async () => {
    const fake = createControllerFake();
    fake.setProposal(editableProposal());
    const modal = await openDetail(fake, buildDetail());

    clickButton("Edit merged result…");
    await modal.awaitRendered();
    requireTextarea().value = `${MERGED_TEXT}\nephemeral draft edit`;

    clickButton("Discard draft");
    await modal.awaitRendered();
    expect(mockState.elements.some((element) => element.tag === "textarea")).toBe(false);

    clickButton("Edit merged result…");
    await modal.awaitRendered();
    expect(requireTextarea().value).toBe(MERGED_TEXT);
    expect(fake.proposalCalls).toEqual([CONFLICT_ID, CONFLICT_ID]);
  });
});

// --- the outcomes ----------------------------------------------------------------------------------------------

describe("conflict inbox outcomes (spec 5.2.2)", () => {
  it("surfaces the parked local apply state after a resolution", async () => {
    const fake = createControllerFake();
    fake.setKeepRemoteResult({ kind: "local_apply_pending" });
    const modal = await openDetail(fake, buildDetail({ choices: ["keep_remote"] }));

    clickButton("Keep remote");
    await modal.awaitRendered();

    expect(fake.keepRemoteCalls).toEqual([CONFLICT_ID]);
    expect(paragraphTexts().some((text) => text.includes("pending"))).toBe(true);
  });

  it("surfaces the successor conflict outcome after a stale resolution", async () => {
    const fake = createControllerFake();
    fake.setKeepRemoteResult({
      kind: "stale_successor",
      successorConflictId: SUCCESSOR_CONFLICT_ID,
    });
    const modal = await openDetail(fake, buildDetail({ choices: ["keep_remote"] }));

    clickButton("Keep remote");
    await modal.awaitRendered();

    expect(paragraphTexts().some((text) => text.includes("successor"))).toBe(true);
  });

  it("renders only the closed failure token, never raw content or paths", async () => {
    const fake = createControllerFake();
    fake.setKeepRemoteResult(new ConflictApiError("server_error", true));
    const modal = await openDetail(fake, buildDetail({ choices: ["keep_remote"] }));

    clickButton("Keep remote");
    await modal.awaitRendered();

    const paragraphs = paragraphTexts().join("\n");
    expect(paragraphs).toContain("server_error");
    expect(paragraphs).not.toContain(SECRET_CONTENT);
    expect(paragraphs).not.toContain(CONFLICT_ID);
  });
});
