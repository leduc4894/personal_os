/**
 * The explicit Conflict Inbox modal (Child 8 spec 5.2, Task 8).
 *
 * The modal owns NO domain logic: every fetch, merge and resolution flows
 * through the injected {@link ConflictController}. It renders exactly the
 * choices the server's kind/media-type matrix admits (the detail's
 * `choices` array is the single source of truth — a byteless
 * delete_remote_edit conflict renders only Keep remote), offers the merge
 * editor only for an editable text/Markdown proposal, renders no editor
 * and no save action for the safe manual-choice state, and keeps merged
 * drafts strictly ephemeral: the draft lives only in this modal's memory
 * while the editor is open and is discarded on back, discard or close —
 * never persisted anywhere.
 *
 * Privacy (spec 9): every status and outcome paragraph renders closed
 * labels only. A failed command renders its closed failure token; the
 * message of a foreign error is never rendered. The editor text area is
 * the user's own working copy and the only surface that ever shows
 * content.
 */

import { Modal, Setting } from "obsidian";

import { ConflictApiError } from "./api";
import type {
  ConflictController,
  ConflictResolutionCommandResult,
} from "./controller";
import { ConflictControllerError } from "./controller";
import type { ConflictKind } from "./contracts";

/** The closed display label of one conflict kind. */
function conflictKindLabel(kind: ConflictKind): string {
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

/** The closed outcome sentence of one resolution command. */
function outcomeText(result: ConflictResolutionCommandResult): string {
  switch (result.kind) {
    case "resolved_and_applied":
      return "Resolved — canonical outcome applied.";
    case "local_apply_pending":
      return "Canonical outcome pending local apply — safe retry scheduled.";
    case "stale_successor":
      return "Remote changed — a successor conflict requires review.";
  }
}

/** The closed failure sentence of one thrown command rejection; a foreign error never leaks its message. */
function failureText(error: unknown): string {
  if (error instanceof ConflictApiError) {
    return `Resolution failed: ${error.kind}`;
  }
  if (error instanceof ConflictControllerError) {
    return `Resolution failed: ${error.reason}`;
  }
  return "Resolution failed: reason_unavailable";
}

/**
 * The explicit Conflict Inbox modal: a list of open conflicts, a detail
 * view with exactly the admitted choices, and — only for an editable
 * text/Markdown proposal — the merge editor whose draft stays ephemeral.
 */
export class ConflictInboxModal extends Modal {
  readonly #controller: ConflictController;
  /**
   * The live merge editor element — the modal's ONLY draft storage. The
   * draft lives in this bounded ephemeral memory while the editor is open
   * and is cleared on discard or close; nothing is persisted anywhere.
   */
  #mergeEditor: { readonly value: string } | null = null;
  #renderTask: Promise<void> = Promise.resolve();

  constructor(app: import("obsidian").App, controller: ConflictController) {
    super(app);
    this.#controller = controller;
  }

  /** Await the current render/command task (the composition and test seam). */
  awaitRendered(): Promise<void> {
    return this.#renderTask;
  }

  override onOpen(): void {
    this.#run(() => this.#renderList());
  }

  override onClose(): void {
    this.#discardDraft();
  }

  /** Run one async UI task under the render tracking; failures render the closed sentence. */
  #run(task: () => Promise<void>): void {
    this.#renderTask = task().catch(() => {
      this.#renderOutcome("Inbox failed: reason_unavailable");
    });
  }

  #resetContent(title: string): void {
    this.#discardDraft();
    const { contentEl } = this;
    contentEl.empty();
    this.titleEl.setText(title);
  }

  #discardDraft(): void {
    this.#mergeEditor = null;
  }

  async #renderList(): Promise<void> {
    this.#discardDraft();
    const page = await this.#controller.listOpenConflicts();
    this.#resetContent("Conflict Inbox");
    if (page.conflicts.length === 0) {
      this.contentEl.createEl("p", { text: "No open conflicts." });
      return;
    }
    for (const conflict of page.conflicts) {
      const label = `Open: ${conflictKindLabel(conflict.conflictKind)}`;
      new Setting(this.contentEl).addButton((button) =>
        button.setButtonText(label).setCta().onClick(() => {
          this.#run(() => this.#renderDetail(conflict.conflictId));
        }),
      );
    }
  }

  async #renderDetail(conflictId: string): Promise<void> {
    this.#discardDraft();
    const detail = await this.#controller.getConflictDetail(conflictId);
    this.#resetContent(`Conflict: ${conflictKindLabel(detail.conflictKind)}`);
    if (detail.choices.includes("keep_remote")) {
      new Setting(this.contentEl).addButton((button) =>
        button
          .setButtonText("Keep remote")
          .setCta()
          .onClick(() => {
            this.#run(async () => {
              await this.#executeCommand(() => this.#controller.resolveKeepRemote(conflictId));
            });
          }),
      );
    }
    if (detail.choices.includes("keep_local")) {
      new Setting(this.contentEl).addButton((button) =>
        button
          .setButtonText("Keep local")
          .setCta()
          .onClick(() => {
            this.#run(async () => {
              await this.#executeCommand(() => this.#controller.resolveKeepLocal(conflictId));
            });
          }),
      );
    }
    if (detail.choices.includes("save_merged")) {
      new Setting(this.contentEl).addButton((button) =>
        button
          .setButtonText("Edit merged result…")
          .setCta()
          .onClick(() => {
            this.#run(() => this.#renderMergeEditor(conflictId));
          }),
      );
    }
    new Setting(this.contentEl).addButton((button) =>
      button.setButtonText("Back").onClick(() => {
        this.#run(() => this.#renderList());
      }),
    );
  }

  async #renderMergeEditor(conflictId: string): Promise<void> {
    const proposal = await this.#controller.buildMergeProposal(conflictId);
    if (proposal.kind !== "editable_merge") {
      // The safe manual-choice state: no editor, no save action — only
      // the explicit keep buttons of the detail view remain available.
      this.#resetContent(`Conflict: manual choice required`);
      this.contentEl.createEl("p", {
        text: `Manual choice required — keep remote or keep local (${proposal.reason}).`,
      });
      new Setting(this.contentEl).addButton((button) =>
        button.setButtonText("Back").onClick(() => {
          this.#run(() => this.#renderDetail(conflictId));
        }),
      );
      return;
    }
    this.#resetContent("Resolve by merged result");
    if (proposal.requiresUserReview) {
      this.contentEl.createEl("p", {
        text: "Conflicting regions need your review — edit the marked hunks before saving.",
      });
    }
    const editor = this.contentEl.createEl("textarea");
    editor.value = proposal.mergedText;
    editor.style.width = "100%";
    editor.style.minHeight = "16rem";
    this.#mergeEditor = editor;
    new Setting(this.contentEl).addButton((button) =>
      button
        .setButtonText("Save merged")
        .setCta()
        .onClick(() => {
          // The live editor value IS the ephemeral draft.
          const editedText = editor.value;
          this.#run(async () => {
            await this.#executeCommand(() =>
              this.#controller.resolveSaveMerged(conflictId, editedText),
            );
          });
        }),
    );
    new Setting(this.contentEl).addButton((button) =>
      button.setButtonText("Discard draft").onClick(() => {
        this.#discardDraft();
        this.#run(() => this.#renderDetail(conflictId));
      }),
    );
  }

  /** Execute one resolution command and render its closed outcome sentence. */
  async #executeCommand(
    command: () => Promise<ConflictResolutionCommandResult>,
  ): Promise<void> {
    try {
      const result = await command();
      this.#renderOutcome(outcomeText(result));
    } catch (error) {
      this.#renderOutcome(failureText(error));
    }
  }

  #renderOutcome(text: string): void {
    this.#discardDraft();
    this.#resetContent("Conflict Inbox");
    this.contentEl.createEl("p", { text });
    new Setting(this.contentEl).addButton((button) =>
      button.setButtonText("Back to inbox").onClick(() => {
        this.#run(() => this.#renderList());
      }),
    );
  }
}
