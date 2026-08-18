/**
 * Cross-language wire-contract replay of the shared small-file sync corpus.
 *
 * This suite replays the exact golden bytes of
 * `tests/fixtures/small_file_sync/wire-golden.json` — the identical corpus
 * the Python contract suite pins by hash and replays against the real route
 * stack — through the REAL hand-mirrored sync client over a raw transport
 * double. Every entry asserts the closed plugin-side landing: one of the
 * five typed preflight outcomes, the committed receipt, or one
 * {@link SyncApiFailureKind} from the spec-12 mapping. No retry, no
 * interpretation: a corpus change means changing both language replays and
 * the pinned registry hash together.
 */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { deriveFrozenFingerprint } from "./fingerprint";
import { createJournalSyncApi, SyncApiError } from "./sync-api";
import type { JournalEventPreflightInput, SyncHttpRequest } from "./sync-api";

interface WireGoldenEntry {
  readonly name: string;
  readonly surface: "preflight" | "content";
  readonly status: number;
  readonly body_text: string;
  readonly plugin_expectation: {
    readonly kind: "preflight_outcome" | "committed_receipt" | "failure";
    readonly outcome?: string;
    readonly sourceId?: string;
    readonly contentVersion?: number;
    readonly failureKind?: string;
  };
}

const FIXTURE = JSON.parse(
  readFileSync(
    new URL("../../../../tests/fixtures/small_file_sync/wire-golden.json", import.meta.url),
    "utf8",
  ),
) as { readonly entries: readonly WireGoldenEntry[] };

const ORIGIN = "https://sync.example.org";
const ACCESS_TOKEN = "at1.wire-contract-access";
const OPERATION_ID = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-Zz";
const CONTENT = new TextEncoder().encode("# shared wire-contract corpus content\n");

/** One preflight input carrying a real derived fingerprint. */
async function preflightInput(): Promise<JournalEventPreflightInput> {
  return {
    eventId: "11111111-1111-4111-8111-111111111112",
    idempotencyKey: "12121212-1212-4121-8121-121212121212",
    operation: "create",
    localFileId: "13131313-1313-4131-8131-131313131313",
    sourceId: null,
    baseVersionId: null,
    normalizedLocator: "notes/wire-contract.md",
    fingerprint: await deriveFrozenFingerprint(CONTENT),
    policyRevisionNumber: 2,
  };
}

/** Drive one entry through the real client and return its closed landing. */
async function replayEntry(entry: WireGoldenEntry): Promise<{
  outcome: string;
  receiptSourceId?: string | undefined;
  receiptContentVersion?: number | undefined;
  failureKind?: string | undefined;
}> {
  const syncApi = createJournalSyncApi({
    transport: async (request: SyncHttpRequest) => {
      void request;
      return { status: entry.status, bodyText: entry.body_text };
    },
    resolveOrigin: () => ORIGIN,
    getAccessToken: () => ACCESS_TOKEN,
  });
  const onFailure = (error: unknown): { outcome: "failure"; failureKind?: string } => {
    if (error instanceof SyncApiError) {
      return { outcome: "failure", failureKind: error.kind };
    }
    throw error;
  };
  if (entry.surface === "preflight") {
    try {
      const result = await syncApi.preflightJournalEvent(await preflightInput());
      return {
        outcome: result.outcome,
        receiptSourceId:
          "receipt" in result ? result.receipt.sourceId : undefined,
        receiptContentVersion:
          "receipt" in result ? result.receipt.contentVersion : undefined,
      };
    } catch (error) {
      return onFailure(error);
    }
  }
  try {
    const receipt = await syncApi.uploadSmallFileContent({
      operationId: OPERATION_ID,
      contentBytes: CONTENT,
    });
    return {
      outcome: "committed",
      receiptSourceId: receipt.sourceId,
      receiptContentVersion: receipt.contentVersion,
    };
  } catch (error) {
    return onFailure(error);
  }
}

describe("shared small-file sync wire-contract corpus (spec 10, 12)", () => {
  it("answers every golden entry with its pinned closed landing", async () => {
    expect(FIXTURE.entries.length).toBeGreaterThan(0);
    for (const entry of FIXTURE.entries) {
      const landing = await replayEntry(entry);
      const expectation = entry.plugin_expectation;
      switch (expectation.kind) {
        case "preflight_outcome":
          expect(landing.outcome, entry.name).toBe(expectation.outcome);
          break;
        case "committed_receipt":
          expect(landing.outcome, entry.name).toBe("committed");
          expect(landing.receiptSourceId, entry.name).toBe(expectation.sourceId);
          expect(landing.receiptContentVersion, entry.name).toBe(expectation.contentVersion);
          break;
        case "failure":
          expect(landing.outcome, entry.name).toBe("failure");
          expect(landing.failureKind, entry.name).toBe(expectation.failureKind);
          break;
      }
    }
  });

  it("keeps the failure vocabulary closed over the whole corpus", async () => {
    const failureKinds = new Set<string>();
    for (const entry of FIXTURE.entries) {
      const landing = await replayEntry(entry);
      if (landing.outcome === "failure") {
        failureKinds.add(landing.failureKind ?? "missing-kind");
      }
    }
    expect([...failureKinds].sort()).toEqual(
      [
        "access_expired",
        "blocked_size",
        "integrity_failed",
        "login_required",
        "operation_retry_required",
      ].sort(),
    );
  });
});
