import { describe, expect, it } from "vitest";

import type {
  SyncDiagnosticsTrail,
  SyncDiagnosticsTrailAppendInput,
} from "./sync-diagnostics-trail";
import { createJournalFailureReporter } from "./diagnostic-reporter";

describe("journal failure reporter", () => {
  it("appends one journal_failure entry for a closed token", () => {
    const appended: SyncDiagnosticsTrailAppendInput[] = [];
    const trail: SyncDiagnosticsTrail = {
      load: async () => undefined,
      append: async (input) => {
        appended.push(input);
      },
      readEntries: () => [],
      readAppendFailureCount: () => 0,
    };

    const reporter = createJournalFailureReporter(trail);
    reporter.reportJournalFailure("snapshot_drain_failed");

    expect(appended).toEqual([
      {
        kind: "journal_failure",
        tokens: ["snapshot_drain_failed"],
      },
    ]);
  });

  it("does not require a trail", () => {
    expect(() => createJournalFailureReporter(null).reportJournalFailure("queue_drain_failed"))
      .not.toThrow();
  });
});
