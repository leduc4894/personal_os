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
}));

vi.mock("obsidian", () => {
  class MockElement {
    readonly style: Record<string, string> = {};
    value = "";
    type = "";
    readonly listeners = new Map<string, MockListener>();

    empty(): void {
      this.value = "";
    }

    createEl(tag: string, options?: { readonly text?: string }): MockElement {
      const element = new MockElement();
      mockState.elements.push({
        tag,
        text: options?.text,
        element,
        listeners: element.listeners,
      });
      return element;
    }

    addEventListener(name: string, listener: MockListener): void {
      this.listeners.set(name, listener);
    }
  }

  class Modal {
    readonly contentEl = new MockElement();
    readonly titleEl = {
      setText: (text: string): void => {
        void text;
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

import { ConfirmModal, PreformattedTextModal, SuggestModal, TextPromptModal } from "./restore-modals";

function settleOnce<T>(read: () => T, write: (value: T) => void, value: T): void {
  if (read() === "pending") {
    write(value);
  }
}

function clickButton(text: string): void {
  const button = mockState.buttons.find((candidate) => candidate.text === text);
  if (button === undefined) {
    throw new Error("button unavailable");
  }
  button.click();
}

describe("explicit restore modal settlement", () => {
  beforeEach(() => {
    mockState.elements.length = 0;
    mockState.buttons.length = 0;
  });

  it("keeps picker acceptance when close synchronously invokes onClose", () => {
    let outcome = "pending";
    const modal = new SuggestModal(
      {} as never,
      [{ label: "safe" }],
      (item) => item.label,
    );
    modal.onChooseItem = () => settleOnce(() => outcome, (value) => (outcome = value), "accepted");
    modal.onClose = () => settleOnce(() => outcome, (value) => (outcome = value), "dismissed");
    modal.open();

    const row = mockState.elements.find((element) => element.tag === "li");
    row?.listeners.get("click")?.({ preventDefault: () => undefined });

    expect(outcome).toBe("accepted");
  });

  it("keeps target acceptance when close synchronously invokes rejection", () => {
    let outcome = "pending";
    const modal = new TextPromptModal(
      {} as never,
      "Restore target path",
      "Safe description",
      () => settleOnce(() => outcome, (value) => (outcome = value), "accepted"),
      () => settleOnce(() => outcome, (value) => (outcome = value), "dismissed"),
    );
    modal.open();

    const input = mockState.elements.find((element) => element.tag === "input");
    if (input !== undefined) {
      input.element.value = "notes/restored.md";
      input.listeners.get("input")?.({ preventDefault: () => undefined });
    }
    clickButton("Restore");

    expect(outcome).toBe("accepted");
  });

  it("dispatches confirmed restore before close can dismiss it", () => {
    let outcome = "pending";
    let dispatchCount = 0;
    const modal = new ConfirmModal(
      {} as never,
      "Confirm restore",
      "Safe body",
      () => {
        settleOnce(() => outcome, (value) => (outcome = value), "accepted");
        dispatchCount += 1;
      },
      () => settleOnce(() => outcome, (value) => (outcome = value), "cancelled"),
      () => settleOnce(() => outcome, (value) => (outcome = value), "dismissed"),
    );
    modal.open();
    clickButton("Restore");

    expect(outcome).toBe("accepted");
    expect(dispatchCount).toBe(1);
  });

  it("keeps a passive dismissal distinct from an explicit cancel", () => {
    const outcomes: string[] = [];
    const modal = new ConfirmModal(
      {} as never,
      "Confirm restore",
      "Safe body",
      () => outcomes.push("accepted"),
      () => outcomes.push("cancelled"),
      () => outcomes.push("dismissed"),
    );
    modal.open();
    // A close without any button choice is the passive dismissal — the
    // durable restore reservation must survive it (a mobile app switch
    // closes modals without an explicit cancel).
    modal.close();
    expect(outcomes).toEqual(["dismissed"]);

    mockState.elements.length = 0;
    mockState.buttons.length = 0;
    const cancelledModal = new ConfirmModal(
      {} as never,
      "Confirm restore",
      "Safe body",
      () => outcomes.push("accepted"),
      () => outcomes.push("cancelled"),
      () => outcomes.push("dismissed"),
    );
    cancelledModal.open();
    clickButton("Cancel");
    expect(outcomes).toEqual(["dismissed", "cancelled"]);
  });
});

describe("preformatted text modal settlement (clipboard-unavailable fallback)", () => {
  beforeEach(() => {
    mockState.elements.length = 0;
    mockState.buttons.length = 0;
  });

  it("renders the sanitized block verbatim in one preformatted element with a close button", () => {
    // Sync error tracing task 2: the clipboard-unavailable fallback shows
    // the SAME sanitized block the clipboard path would have written —
    // closed tokens, counts and timestamps only, never altered or wrapped.
    const block = [
      "obsidian_sync_diagnostics_export/v1",
      "Status: Ready (3)",
      "Trail tail (last 5):",
      "2026-07-13T00:00:00.000Z · wire_failure · server_error",
    ].join("\n");
    const modal = new PreformattedTextModal({} as never, "Sync diagnostics", block);
    modal.open();

    const pre = mockState.elements.find((element) => element.tag === "pre");
    expect(pre?.text).toBe(block);
    expect(mockState.buttons.some((button) => button.text === "Close")).toBe(true);
  });
});
