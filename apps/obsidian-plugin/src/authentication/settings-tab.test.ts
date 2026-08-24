import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";

vi.mock("obsidian", () => ({
  PluginSettingTab: class {
    display(): void {
      return undefined;
    }
  },
  Setting: class {
    setName(): this {
      return this;
    }
  },
}));

import { renderJournalStoreDiagnosticsLine, renderLocalNoteSyncStatusList } from "./settings-tab";
import {
  renderJournalStartupFailureLine,
  renderPolicyStateGuidanceLine,
} from "./settings-tab";
import { POLICY_INTEGRITY_STATES } from "../exclusion-policy/contracts";

const tabPath = new URL("./settings-tab.ts", import.meta.url);
const tabSource = readFileSync(tabPath, "utf8");

// The settings tab imports the Obsidian runtime module, so this suite pins its
// source contract statically (the same convention as plugin.test.ts): the
// closed control set of spec 19, the allowed Obsidian surface, and no
// forbidden load-time capability.
const ALLOWED_OBSIDIAN_IMPORT_NAMES = new Set([
  "PluginSettingTab",
  "Setting",
  "App",
  "Plugin",
  "Platform",
  "requestUrl",
  "RequestUrlParam",
  "RequestUrlResponse",
]);

function extractObsidianImportNames(source: string): string[] {
  const names: string[] = [];
  const importPattern = /import\s+(type\s+)?\{([^}]*)\}\s+from\s+"obsidian"/g;
  for (const match of source.matchAll(importPattern)) {
    for (const specifier of match[2]?.split(",") ?? []) {
      const name = specifier.trim().split(/\s+as\s+/)[0]?.trim();
      if (name) {
        names.push(name);
      }
    }
  }
  return names;
}

describe("DeviceAuthenticationSettingTab source contract", () => {
  it("imports only the closed Obsidian settings surface", () => {
    const names = extractObsidianImportNames(tabSource);
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) {
      expect(ALLOWED_OBSIDIAN_IMPORT_NAMES.has(name)).toBe(true);
    }
  });

  it("exposes the exact spec-19 control set", () => {
    expect(extractObsidianImportNames(tabSource)).toContain("PluginSettingTab");
    for (const requiredControl of [
      "Server origin",
      "Device name",
      "Connection status",
      "Login",
      "Open browser again",
      "Cancel pending login",
      "Disconnect",
    ]) {
      expect(tabSource).toContain(requiredControl);
    }
  });

  it("derives the status text and controls from the closed contracts", () => {
    expect(tabSource).toContain("CONNECTION_STATUS_TEXT");
    expect(tabSource).toContain("resolveAuthenticationControls");
    expect(tabSource).toContain("ConnectionState");
  });

  it("shows the closed sync status and its blocker guidance (spec 11)", () => {
    expect(tabSource).toContain("Sync status");
    expect(tabSource).toContain("syncStatusText");
    expect(tabSource).toContain("syncBlockerGuidance");
  });

  it("renders the redacted lifecycle state histogram (Task 10, fix round 1 I1)", () => {
    // Fix round 1 I1: the settings snapshot must accept the four new
    // lifecycle fields and the tab must render the histogram counts and
    // the closed blocked reason codes list. The render is a Setting
    // description only — no controls, no path, no source ID.
    expect(tabSource).toContain("lifecycleStateCounts");
    expect(tabSource).toContain("pendingLifecycleEventCount");
    expect(tabSource).toContain("failedAttemptCount");
    expect(tabSource).toContain("lifecycleBlockedReasonCodes");
    // The tab MUST render a Setting that names both the histogram and the
    // blocked reason codes so the operator can see them.
    expect(tabSource).toContain("Lifecycle state");
    expect(tabSource).toContain("Lifecycle blockers");
    // Reject any path-leaking pattern that the new render surfaces must
    // never include: the description is closed-enum counts and codes only.
    const descriptionSnippet = tabSource.match(/Lifecycle blockers[\s\S]*?setDesc\(([^)]+)\)/);
    if (descriptionSnippet !== null) {
      const descriptionBuilder = descriptionSnippet[1] ?? "";
      for (const forbidden of [".md", "notes/", "at1.", "secret", "https://"]) {
        expect(descriptionBuilder).not.toContain(forbidden);
      }
    }
  });

  it("renders the durable sync diagnostics trail section (sync error tracing task 2)", () => {
    // The settings snapshot must accept the four trail fields and the tab
    // must render them through the closed renderer: the derived stop-reason
    // tokens, the total entry count, the bounded append-failure counter and
    // the last five trail entries. Closed tokens and timestamps only.
    expect(tabSource).toContain("Sync diagnostics trail");
    expect(tabSource).toContain("renderSyncDiagnosticsTrailSection");
    expect(tabSource).toContain("syncStopReasonTokens");
    expect(tabSource).toContain("trailTailEntries");
    expect(tabSource).toContain("trailEntryCount");
    expect(tabSource).toContain("trailAppendFailureCount");
    // Reject any path-leaking pattern in the new section's description
    // builder: the render is closed tokens, counts and timestamps only.
    const sectionIndex = tabSource.indexOf("Sync diagnostics trail");
    expect(sectionIndex).toBeGreaterThanOrEqual(0);
    const sectionSnippet = tabSource.slice(sectionIndex, sectionIndex + 700);
    for (const forbidden of [".md", "notes/", "at1.", "secret", "https://"]) {
      expect(sectionSnippet).not.toContain(forbidden);
    }
  });

  it("carries the closed policy state and startup-failure tokens on the snapshot (closed-reason surfacing C1)", () => {
    // C1 P3: the closed `policyState` (including `policy_integrity_failed`)
    // joins the settings snapshot and renders one fixed guidance line per
    // closed value. C1 P1: the closed startup-failure tokens join the
    // snapshot and render beside the sync status — null before the first
    // failure, never a fake success token.
    expect(tabSource).toContain("policyState");
    expect(tabSource).toContain("lastStartupFailureTokens");
    expect(tabSource).toContain("Policy state");
    expect(tabSource).toContain("renderPolicyStateGuidanceLine");
    expect(tabSource).toContain("renderJournalStartupFailureLine");
    // Reject any path-leaking pattern in the new fixed guidance lines and
    // the startup-failure line: closed tokens and fixed English only.
    const guidanceIndex = tabSource.indexOf("POLICY_STATE_GUIDANCE_TEXT");
    expect(guidanceIndex).toBeGreaterThanOrEqual(0);
    const guidanceSnippet = tabSource.slice(guidanceIndex, guidanceIndex + 1_200);
    const startupLineIndex = tabSource.indexOf("export function renderJournalStartupFailureLine");
    const startupLineSnippet = tabSource.slice(startupLineIndex, startupLineIndex + 500);
    for (const forbidden of [".md", "notes/", "at1.", "secret", "https://", "Error:"]) {
      expect(guidanceSnippet).not.toContain(forbidden);
      expect(startupLineSnippet).not.toContain(forbidden);
    }
  });

  it("offers no control implying automatic full-Vault upload", () => {
    for (const forbiddenLabel of ["Sync all", "Upload all", "Sync everything", "Upload everything"]) {
      expect(tabSource).not.toContain(forbiddenLabel);
    }
  });

  it("touches no forbidden runtime capability", () => {
    for (const forbiddenText of [
      "node:",
      "electron",
      "FileSystemAdapter",
      ".vault",
      "fetch(",
      "process.env",
      "qrcode",
    ]) {
      expect(tabSource).not.toContain(forbiddenText);
    }
  });
});

describe("renderJournalStoreDiagnosticsLine (fix round 5)", () => {
  it("renders an observed empty state", () => {
    expect(
      renderJournalStoreDiagnosticsLine({
        lastJournalFailureReasons: [],
        generationPublishFailureCount: 0,
        lastGenerationPublishFailureReasons: [],
      }),
    ).toContain("No journal store failures observed.");
  });

  it("renders the closed reason tokens and the publish-failure count only", () => {
    const line = renderJournalStoreDiagnosticsLine({
      lastJournalFailureReasons: ["journal_mutation_failed", "journal_query_failed"],
      generationPublishFailureCount: 3,
      lastGenerationPublishFailureReasons: ["journal_generation_write_failed"],
    });
    expect(line).toContain("journal_mutation_failed, journal_query_failed");
    expect(line).toContain("3");
    expect(line).toContain("journal_generation_write_failed");
    // Closed vocabulary only: no raw error text, path, digest or content
    // ever reaches the line.
    expect(line).not.toContain("Error:");
    expect(line).not.toContain("notes/");
  });
});

describe("renderPolicyStateGuidanceLine (closed-reason surfacing C1 P3)", () => {
  it("maps every closed policy integrity state to one distinct fixed guidance line", () => {
    const lines = POLICY_INTEGRITY_STATES.map(renderPolicyStateGuidanceLine);
    for (const line of lines) {
      expect(line.length).toBeGreaterThan(0);
      for (const forbidden of [".md", "notes/", "at1.", "secret", "https://"]) {
        expect(line).not.toContain(forbidden);
      }
    }
    // One FIXED line per closed value: no two states share a line.
    expect(new Set(lines).size).toBe(POLICY_INTEGRITY_STATES.length);
    expect(POLICY_INTEGRITY_STATES).toContain("policy_integrity_failed");
  });

  it("explains that capture is stopped while policy integrity is failed", () => {
    const line = renderPolicyStateGuidanceLine("policy_integrity_failed");
    expect(line).toContain("integrity failed");
    expect(line).toContain("capture is stopped");
  });
});

describe("renderJournalStartupFailureLine (closed-reason surfacing C1 P1)", () => {
  it("renders nothing before the first startup failure (never a fake success token)", () => {
    expect(renderJournalStartupFailureLine(null)).toBeNull();
    expect(renderJournalStartupFailureLine([])).toBeNull();
  });

  it("renders the closed stage and store reason tokens only", () => {
    expect(renderJournalStartupFailureLine(["engine_load"])).toBe(
      "Journal startup failed: engine_load",
    );
    const line = renderJournalStartupFailureLine([
      "journal_recovery",
      "journal_schema_unsupported",
    ]);
    expect(line).toBe("Journal startup failed: journal_recovery, journal_schema_unsupported");
    for (const forbidden of [".md", "notes/", "at1.", "secret", "https://", "Error:"]) {
      expect(line).not.toContain(forbidden);
    }
  });
});

describe("renderLocalNoteSyncStatusList", () => {
  it("renders every closed current-note state with fixed labels", () => {
    const rendered = renderLocalNoteSyncStatusList([
      { normalizedPath: "g.md", state: "synced", policyRevisionNumber: 4, retryAtEpochMs: null, reason: null },
      { normalizedPath: "f.md", state: "queued", policyRevisionNumber: 4, retryAtEpochMs: null, reason: null },
      { normalizedPath: "e.md", state: "syncing", policyRevisionNumber: 4, retryAtEpochMs: null, reason: null },
      { normalizedPath: "d.md", state: "retrying", policyRevisionNumber: 4, retryAtEpochMs: 1_750_000_000_000, reason: "network_offline" },
      { normalizedPath: "c.md", state: "policy_blocked", policyRevisionNumber: 12, retryAtEpochMs: null, reason: "excluded_policy" },
      { normalizedPath: "b.md", state: "conflict", policyRevisionNumber: 4, retryAtEpochMs: null, reason: "blocked_conflict" },
      { normalizedPath: "a.md", state: "reconcile_required", policyRevisionNumber: 4, retryAtEpochMs: null, reason: "integrity_failed" },
    ]);

    expect(rendered).toContain("a.md — Reconciliation required");
    expect(rendered).toContain("b.md — Conflict");
    expect(rendered).toContain("c.md — Policy blocked · Policy revision: 12 · Reason: excluded_policy");
    expect(rendered).toContain("d.md — Retrying · Retry at: 1750000000000 · Reason: network_offline");
    expect(rendered).toContain("e.md — Syncing");
    expect(rendered).toContain("f.md — Queued");
    expect(rendered).toContain("g.md — Synced");
  });

  it("sorts note statuses by normalized path without mutating the supplied snapshot", () => {
    const statuses = [
      { normalizedPath: "zeta.md", state: "synced" as const, policyRevisionNumber: 1, retryAtEpochMs: null, reason: null },
      { normalizedPath: "alpha.md", state: "queued" as const, policyRevisionNumber: 1, retryAtEpochMs: null, reason: null },
    ];

    expect(renderLocalNoteSyncStatusList(statuses)).toBe("alpha.md — Queued\nzeta.md — Synced");
    expect(statuses.map((status) => status.normalizedPath)).toEqual(["zeta.md", "alpha.md"]);
  });

  it("keeps non-ASCII paths in fixed code-unit order regardless of host locale", () => {
    const rendered = renderLocalNoteSyncStatusList([
      { normalizedPath: "äther.md", state: "synced", policyRevisionNumber: 1, retryAtEpochMs: null, reason: null },
      { normalizedPath: "zeta.md", state: "queued", policyRevisionNumber: 1, retryAtEpochMs: null, reason: null },
    ]);

    expect(rendered).toBe("zeta.md — Queued\näther.md — Synced");
  });

  it("renders a local empty state when the current device tracks no notes", () => {
    expect(renderLocalNoteSyncStatusList([])).toBe("No note sync statuses are available on this device");
  });

  it("shows a policy block with only its revision and closed reason details", () => {
    const rendered = renderLocalNoteSyncStatusList([
      {
        normalizedPath: "notes/local-only.md",
        state: "policy_blocked",
        policyRevisionNumber: 12,
        retryAtEpochMs: null,
        reason: "excluded_policy",
      },
    ]);

    expect(rendered).toBe("notes/local-only.md — Policy blocked · Policy revision: 12 · Reason: excluded_policy");
    expect(rendered).not.toContain("Retry at");
  });
});
