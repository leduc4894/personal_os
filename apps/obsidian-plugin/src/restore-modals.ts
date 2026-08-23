/**
 * The explicit-restore modal adapters.
 *
 * These small adapters keep Obsidian's synchronous `Modal.close()` / `onClose`
 * behavior behind a behavior-tested boundary. Accepted values must be
 * published before closing; otherwise the dismissal fallback wins the
 * awaiting Promise and restore never dispatches.
 */

import { Modal, Setting } from "obsidian";

/** A narrow retained-tombstone picker with safe rendered labels only. */
export class SuggestModal<T> extends Modal {
  readonly #items: readonly T[];
  readonly #render: (item: T) => string;
  #placeholder = "Search…";
  onChooseItem: (item: T) => void = () => undefined;

  constructor(app: import("obsidian").App, items: readonly T[], render: (item: T) => string) {
    super(app);
    this.#items = items;
    this.#render = render;
  }

  setPlaceholder(text: string): void {
    this.#placeholder = text;
  }

  override onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl("p", { text: this.#placeholder });
    const list = contentEl.createEl("ul");
    for (const item of this.#items) {
      const row = list.createEl("li", { text: this.#render(item) });
      row.style.cursor = "pointer";
      row.addEventListener("click", () => {
        this.onChooseItem(item);
        this.close();
      });
    }
  }
}

/** A narrow text prompt for the restore target path. */
export class TextPromptModal extends Modal {
  readonly #title: string;
  readonly #description: string;
  readonly #accept: (value: string) => void;
  readonly #reject: () => void;
  #inputValue = "";

  constructor(
    app: import("obsidian").App,
    title: string,
    description: string,
    accept: (value: string) => void,
    reject: () => void,
  ) {
    super(app);
    this.#title = title;
    this.#description = description;
    this.#accept = accept;
    this.#reject = reject;
  }

  override onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    this.titleEl.setText(this.#title);
    contentEl.createEl("p", { text: this.#description });
    const input = contentEl.createEl("input");
    input.type = "text";
    input.style.width = "100%";
    input.addEventListener("input", () => {
      this.#inputValue = input.value;
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        this.#accept(this.#inputValue);
        this.close();
      }
    });
    new Setting(contentEl)
      .addButton((button) =>
        button
          .setButtonText("Restore")
          .setCta()
          .onClick(() => {
            this.#accept(this.#inputValue);
            this.close();
          }),
      )
      .addButton((button) =>
        button
          .setButtonText("Cancel")
          .onClick(() => {
            this.close();
            this.#reject();
          }),
      );
    this.onClose = () => this.#reject();
  }
}

/** A narrow two-button confirmation for one explicit restore request. */
export class ConfirmModal extends Modal {
  readonly #title: string;
  readonly #body: string;
  readonly #accept: () => void;
  readonly #reject: () => void;

  constructor(
    app: import("obsidian").App,
    title: string,
    body: string,
    accept: () => void,
    reject: () => void,
  ) {
    super(app);
    this.#title = title;
    this.#body = body;
    this.#accept = accept;
    this.#reject = reject;
  }

  override onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    this.titleEl.setText(this.#title);
    contentEl.createEl("p", { text: this.#body });
    new Setting(contentEl)
      .addButton((button) =>
        button
          .setButtonText("Restore")
          .setCta()
          .onClick(() => {
            this.#accept();
            this.close();
          }),
      )
      .addButton((button) =>
        button
          .setButtonText("Cancel")
          .onClick(() => {
            this.close();
            this.#reject();
          }),
      );
    this.onClose = () => this.#reject();
  }
}

/**
 * A read-only preformatted text modal (the clipboard-unavailable fallback
 * of the copy-sync-diagnostics command): one title, one verbatim
 * preformatted body and a close button. The body is the already-sanitized
 * closed-vocabulary block; the modal never alters, wraps or records it.
 */
export class PreformattedTextModal extends Modal {
  readonly #title: string;
  readonly #body: string;

  constructor(app: import("obsidian").App, title: string, body: string) {
    super(app);
    this.#title = title;
    this.#body = body;
  }

  override onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    this.titleEl.setText(this.#title);
    contentEl.createEl("pre", { text: this.#body });
    new Setting(contentEl).addButton((button) =>
      button.setButtonText("Close").setCta().onClick(() => this.close()),
    );
  }
}
