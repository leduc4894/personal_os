import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { browser } from "@wdio/globals";
import { onboardLiveDevice } from "../support/live-device-onboarding";
import {
  type LiveAcceptancePhaseResultCode,
  writeLiveAcceptancePhaseStatus,
} from "../support/live-acceptance-phase-status";
import { runFromE2eRepositoryRoot } from "../support/repository-subprocess";

/**
 * Mandatory Child 5 live Desktop journey.  The test onboards the fixture
 * Obsidian instance through the real device-authorization flow, publishes one
 * controlled note, then performs real Vault rename, move, delete and explicit
 * restore actions against the authenticated public HTTPS plugin origin.  The
 * final assertions read canonical PostgreSQL evidence and a sanitized journal
 * projection; no mock substitutes for either boundary and no locator, content,
 * digest or credential is printed.
 */
const serverOrigin = process.env.E2E_SERVER_ORIGIN ?? "http://127.0.0.1:8000";
const allowedOrigin = process.env.E2E_ALLOWED_ORIGIN ?? "https://app.ducinvest.com";
const webUsername = process.env.E2E_WEB_USERNAME ?? "duc";
const passwordFile = process.env.E2E_WEB_PASSWORD_FILE;
const totpHelper = process.env.E2E_TOTP_HELPER;
const livePhaseStatusFile = process.env.E2E_LIVE_PHASE_STATUS_FILE;
const pluginDataPathSuffix = "plugins/knowledge-workspace/data.json";
const fixtureIdentity = crypto.randomUUID();
const initialPath = `controlled-lifecycle-${fixtureIdentity}.md`;
const renamedPath = `controlled-lifecycle-renamed-${fixtureIdentity}.md`;
const movedFolder = `controlled-lifecycle-folder-${fixtureIdentity}`;
const movedPath = `${movedFolder}/controlled-lifecycle-renamed-${fixtureIdentity}.md`;
const restoredPath = `${movedFolder}/controlled-lifecycle-restored-${fixtureIdentity}.md`;
const fixtureContent = `# Controlled lifecycle fixture\n\n${fixtureIdentity}\n`;
const fixtureDigest = crypto.createHash("sha256").update(fixtureContent).digest("hex");
const databaseEnvironmentKeys = [
  "KNOWLEDGE_SECRET_ROOT",
  "KNOWLEDGE_DATABASE_HOST",
  "KNOWLEDGE_DATABASE_PORT",
  "KNOWLEDGE_DATABASE_NAME",
  "KNOWLEDGE_DATABASE_USER",
  "KNOWLEDGE_DATABASE_PASSWORD_FILE",
] as const;

interface CanonicalLifecycleEvidence {
  readonly sourceId: string | null;
  readonly currentVersionId: string | null;
  readonly syncState: string | null;
  readonly activeLocatorCount: number;
  readonly locatorHistoryCount: number;
  readonly openTombstoneCount: number;
  readonly lifecycleEventCount: number;
  readonly renameEventCount: number;
  readonly moveEventCount: number;
  readonly deleteEventCount: number;
  readonly restoreEventCount: number;
}

interface JournalLifecycleEvidence {
  readonly localFileId: string;
  readonly sourceId: string;
  readonly currentVersionId: string;
  readonly committedLifecycleCount: number;
  readonly pendingLifecycleCount: number;
  readonly blockedLifecycleCount: number;
}

function recordLivePhase(resultCode: LiveAcceptancePhaseResultCode): void {
  if (livePhaseStatusFile === undefined) {
    throw new Error("live E2E phase status contract was unavailable");
  }
  writeLiveAcceptancePhaseStatus(livePhaseStatusFile, resultCode);
}

const canonicalEvidenceScript = String.raw`
import json
import os
import re
from pathlib import Path

import psycopg

try:
    secret_root = Path(os.environ["KNOWLEDGE_SECRET_ROOT"]).resolve(strict=True)
    password_path = (
        secret_root / os.environ["KNOWLEDGE_DATABASE_PASSWORD_FILE"]
    ).resolve(strict=True)
    if not password_path.is_relative_to(secret_root):
        raise ValueError
    controlled_digest = os.environ["SOURCE_LIFECYCLE_EVIDENCE_SHA256"]
    if re.fullmatch(r"[0-9a-f]{64}", controlled_digest) is None:
        raise ValueError
    password = password_path.read_text(encoding="ascii").strip()
    with psycopg.connect(
        host=os.environ["KNOWLEDGE_DATABASE_HOST"],
        port=int(os.environ["KNOWLEDGE_DATABASE_PORT"]),
        dbname=os.environ["KNOWLEDGE_DATABASE_NAME"],
        user=os.environ["KNOWLEDGE_DATABASE_USER"],
        password=password,
        connect_timeout=5,
        options="-c statement_timeout=5000 -c lock_timeout=1000",
    ) as connection:
        row = connection.execute(
            """
            with controlled_source as (
              select operation.result_source_id as source_id
                from knowledge.small_file_upload_operations operation
               where operation.declared_sha256 = %s
                 and operation.state = 'committed'
               order by operation.created_at desc
               limit 1
            )
            select
              source.source_id,
              source.current_version_id,
              source.sync_state,
              (select count(*) from knowledge.source_locators locator
                where locator.source_id = source.source_id
                  and locator.closed_event_id is null),
              (select count(*) from knowledge.source_locators locator
                where locator.source_id = source.source_id),
              (select count(*) from knowledge.source_tombstones tombstone
                where tombstone.source_id = source.source_id
                  and tombstone.restore_event_id is null),
              (select count(*) from knowledge.sync_events event
                where event.source_id = source.source_id
                  and event.event_type in ('rename', 'move', 'delete', 'restore')),
              (select count(*) from knowledge.sync_events event
                where event.source_id = source.source_id and event.event_type = 'rename'),
              (select count(*) from knowledge.sync_events event
                where event.source_id = source.source_id and event.event_type = 'move'),
              (select count(*) from knowledge.sync_events event
                where event.source_id = source.source_id and event.event_type = 'delete'),
              (select count(*) from knowledge.sync_events event
                where event.source_id = source.source_id and event.event_type = 'restore')
              from controlled_source controlled
              join knowledge.sources source on source.source_id = controlled.source_id
            """,
            (controlled_digest,),
        ).fetchone()
    if row is None:
        print(json.dumps({"state": "not_found"}, separators=(",", ":")))
    else:
        print(json.dumps({
            "sourceId": str(row[0]),
            "currentVersionId": str(row[1]),
            "syncState": str(row[2]),
            "activeLocatorCount": int(row[3]),
            "locatorHistoryCount": int(row[4]),
            "openTombstoneCount": int(row[5]),
            "lifecycleEventCount": int(row[6]),
            "renameEventCount": int(row[7]),
            "moveEventCount": int(row[8]),
            "deleteEventCount": int(row[9]),
            "restoreEventCount": int(row[10]),
        }, separators=(",", ":")))
except BaseException:
    print(json.dumps({"state": "server_evidence_unavailable"}, separators=(",", ":")))
    raise SystemExit(1)
`;

async function onboardFixtureDevice(): Promise<void> {
  if (passwordFile === undefined || totpHelper === undefined) {
    throw new Error(
      "live E2E environment loader did not provide the credential-file and TOTP-helper contracts",
    );
  }
  await onboardLiveDevice({
    serverOrigin,
    allowedOrigin,
    webUsername,
    passwordFile,
    totpHelper,
    pluginDataPathSuffix,
    deviceName: "source-lifecycle-e2e",
  });
}

async function triggerSyncNow(): Promise<void> {
  await browser.execute(() => {
    const app = (
      window as unknown as {
        app: { commands: { executeCommandById: (commandId: string) => void } };
      }
    ).app;
    app.commands.executeCommandById("knowledge-workspace:sync-now");
  });
}

async function readCanonicalEvidence(): Promise<CanonicalLifecycleEvidence | null> {
  const { stdout } = await runFromE2eRepositoryRoot(
    "uv",
    ["run", "python", "-c", canonicalEvidenceScript],
    import.meta.url,
    { ...process.env, SOURCE_LIFECYCLE_EVIDENCE_SHA256: fixtureDigest },
  );
  const parsed = JSON.parse(stdout) as Record<string, unknown>;
  if (parsed["state"] === "not_found") {
    return null;
  }
  const countKeys = [
    "activeLocatorCount",
    "locatorHistoryCount",
    "openTombstoneCount",
    "lifecycleEventCount",
    "renameEventCount",
    "moveEventCount",
    "deleteEventCount",
    "restoreEventCount",
  ] as const;
  if (
    typeof parsed["sourceId"] !== "string" ||
    typeof parsed["currentVersionId"] !== "string" ||
    typeof parsed["syncState"] !== "string" ||
    countKeys.some((key) => !Number.isSafeInteger(parsed[key]) || Number(parsed[key]) < 0)
  ) {
    throw new Error("canonical lifecycle evidence was invalid");
  }
  return parsed as unknown as CanonicalLifecycleEvidence;
}

async function waitForCanonicalEvidence(
  accepts: (evidence: CanonicalLifecycleEvidence) => boolean,
  failureMessage: string,
): Promise<CanonicalLifecycleEvidence> {
  let lastSafeState: Record<string, unknown> | null = null;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const evidence = await readCanonicalEvidence();
    if (evidence !== null) {
      lastSafeState = {
        syncState: evidence.syncState,
        activeLocatorCount: evidence.activeLocatorCount,
        locatorHistoryCount: evidence.locatorHistoryCount,
        openTombstoneCount: evidence.openTombstoneCount,
        lifecycleEventCount: evidence.lifecycleEventCount,
      };
      if (accepts(evidence)) {
        return evidence;
      }
    }
    await browser.pause(1_000);
  }
  throw new Error(`${failureMessage}: ${JSON.stringify(lastSafeState)}`);
}

async function journalDirectoryPath(): Promise<string> {
  return browser.execute(() => {
    const app = (
      window as unknown as {
        app: {
          vault: {
            configDir: string;
            adapter: { getFullPath: (path: string) => string };
          };
        };
      }
    ).app;
    return app.vault.adapter.getFullPath(
      `${app.vault.configDir}/plugins/knowledge-workspace`,
    );
  });
}

async function readJournalEvidence(
  knownLocalFileId?: string,
): Promise<JournalLifecycleEvidence | null> {
  const directory = await journalDirectoryPath();
  const manifest = JSON.parse(
    fs.readFileSync(path.join(directory, "journal.manifest.json"), "utf8"),
  ) as { current?: { generationNumber?: unknown } };
  const generationNumber = manifest.current?.generationNumber;
  if (!Number.isSafeInteger(generationNumber) || Number(generationNumber) < 0) {
    throw new Error("journal lifecycle evidence was unavailable");
  }
  const journalBytes = fs.readFileSync(
    path.join(directory, `journal.sqlite.g${generationNumber}`),
  );
  const initSqlJs = (await import("sql.js")).default;
  const engine = await initSqlJs();
  const database = new engine.Database(journalBytes);
  try {
    const statement = database.prepare(
      `select file.local_file_id, file.source_id, file.base_version_id,
              sum(case when event.operation in ('rename', 'move', 'delete', 'restore')
                            and event.state = 'committed' then 1 else 0 end),
              sum(case when event.operation in ('rename', 'move', 'delete', 'restore')
                            and event.state in ('queued', 'preflight', 'uploading', 'waiting_retry')
                       then 1 else 0 end),
              sum(case when event.operation in ('rename', 'move', 'delete', 'restore')
                            and event.state in ('blocked_conflict', 'integrity_failed')
                       then 1 else 0 end)
         from local_files file
         left join journal_events event on event.local_file_id = file.local_file_id
        where (? is null or file.local_file_id = ?)
          and file.source_id is not null and file.base_version_id is not null
        group by file.local_file_id, file.source_id, file.base_version_id
        order by file.local_file_id`,
    );
    try {
      statement.bind([knownLocalFileId ?? null, knownLocalFileId ?? null]);
      if (!statement.step()) {
        return null;
      }
      const row = statement.get();
      const result: JournalLifecycleEvidence = {
        localFileId: String(row[0]),
        sourceId: String(row[1]),
        currentVersionId: String(row[2]),
        committedLifecycleCount: Number(row[3] ?? 0),
        pendingLifecycleCount: Number(row[4] ?? 0),
        blockedLifecycleCount: Number(row[5] ?? 0),
      };
      if (statement.step() && knownLocalFileId === undefined) {
        throw new Error("controlled journal evidence was not fixture-unique");
      }
      return result;
    } finally {
      statement.free();
    }
  } finally {
    database.close();
  }
}

async function waitForJournalEvidence(
  knownLocalFileId: string | undefined,
  accepts: (evidence: JournalLifecycleEvidence) => boolean,
  failureMessage: string,
): Promise<JournalLifecycleEvidence> {
  let lastSafeState: Record<string, number> | null = null;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const evidence = await readJournalEvidence(knownLocalFileId);
      if (evidence !== null) {
        lastSafeState = {
          committedLifecycleCount: evidence.committedLifecycleCount,
          pendingLifecycleCount: evidence.pendingLifecycleCount,
          blockedLifecycleCount: evidence.blockedLifecycleCount,
        };
        if (accepts(evidence)) {
          return evidence;
        }
      }
    } catch {
      // The verified-generation manifest may briefly lead the immutable image.
    }
    await browser.pause(1_000);
  }
  throw new Error(`${failureMessage}: ${JSON.stringify(lastSafeState)}`);
}

async function createFixtureNote(): Promise<void> {
  await browser.execute(async (notePath: string, content: string) => {
    const app = (
      window as unknown as {
        app: { vault: { create: (path: string, text: string) => Promise<void> } };
      }
    ).app;
    await app.vault.create(notePath, content);
  }, initialPath, fixtureContent);
}

async function renameFixtureNote(): Promise<void> {
  await browser.execute(async (priorPath: string, nextPath: string) => {
    const app = (
      window as unknown as {
        app: {
          vault: {
            getAbstractFileByPath: (path: string) => unknown;
            rename: (file: unknown, path: string) => Promise<void>;
          };
        };
      }
    ).app;
    const file = app.vault.getAbstractFileByPath(priorPath);
    if (file === null) {
      throw new Error("controlled fixture was unavailable for rename");
    }
    await app.vault.rename(file, nextPath);
  }, initialPath, renamedPath);
}

async function moveFixtureNote(): Promise<void> {
  await browser.execute(async (priorPath: string, folder: string, nextPath: string) => {
    const app = (
      window as unknown as {
        app: {
          vault: {
            createFolder: (path: string) => Promise<void>;
            getAbstractFileByPath: (path: string) => unknown;
            rename: (file: unknown, path: string) => Promise<void>;
          };
        };
      }
    ).app;
    if (app.vault.getAbstractFileByPath(folder) === null) {
      await app.vault.createFolder(folder);
    }
    const file = app.vault.getAbstractFileByPath(priorPath);
    if (file === null) {
      throw new Error("controlled fixture was unavailable for move");
    }
    await app.vault.rename(file, nextPath);
  }, renamedPath, movedFolder, movedPath);
}

async function deleteFixtureNote(): Promise<void> {
  await browser.execute(async (notePath: string) => {
    const app = (
      window as unknown as {
        app: {
          vault: {
            delete: (file: unknown, force?: boolean) => Promise<void>;
            getAbstractFileByPath: (path: string) => unknown;
          };
        };
      }
    ).app;
    const file = app.vault.getAbstractFileByPath(notePath);
    if (file === null) {
      throw new Error("controlled fixture was unavailable for delete");
    }
    await app.vault.delete(file, true);
  }, movedPath);
}

async function clickVisibleText(selector: string, expectedText: string): Promise<void> {
  const didClick = await browser.execute((candidateSelector: string, text: string) => {
    const element = Array.from(document.querySelectorAll(candidateSelector)).find(
      (candidate) => candidate.textContent?.trim() === text,
    );
    if (!(element instanceof HTMLElement)) {
      return false;
    }
    element.click();
    return true;
  }, selector, expectedText);
  if (!didClick) {
    throw new Error("explicit restore control was unavailable");
  }
}

async function explicitlyRestoreFixtureNote(): Promise<void> {
  // Reservation-first protocol (2026-08-25): the restore command reserves
  // the target locator the moment the target-path prompt is accepted, so
  // the staged bytes land on a path the convergence lane durably defers
  // — they can never converge as a fresh source before the restore event
  // ships. The staging therefore happens BETWEEN the prompt accept and
  // the confirm click, with the plugin enabled throughout (no
  // disable/enable dance).
  await browser.execute(() => {
    const app = (
      window as unknown as {
        app: { commands: { executeCommandById: (commandId: string) => void } };
      }
    ).app;
    app.commands.executeCommandById("knowledge-workspace:restore-selected-tombstone");
  });
  await browser.pause(250);
  const selected = await browser.execute(() => {
    const item = document.querySelector(".modal-container li");
    if (!(item instanceof HTMLElement)) {
      return false;
    }
    item.click();
    return true;
  });
  if (!selected) {
    throw new Error("explicit restore tombstone selection was unavailable");
  }
  await browser.pause(250);
  const entered = await browser.execute((targetPath: string) => {
    const input = document.querySelector(".modal-container input");
    if (!(input instanceof HTMLInputElement)) {
      return false;
    }
    input.value = targetPath;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  }, restoredPath);
  if (!entered) {
    throw new Error("explicit restore target control was unavailable");
  }
  // Accept the target path: the reservation is now durable.
  await clickVisibleText(".modal-container button", "Restore");
  // Stage the restored bytes on the reserved target while the confirm
  // modal is open.
  await browser.execute(async (notePath: string, content: string) => {
    const app = (
      window as unknown as {
        app: { vault: { create: (path: string, text: string) => Promise<void> } };
      }
    ).app;
    await app.vault.create(notePath, content);
  }, restoredPath, fixtureContent);
  await browser.pause(250);
  await clickVisibleText(".modal-container button", "Restore");
}

describe("source locator and tombstone lifecycle (live server)", () => {
  before(() => {
    const missingDatabaseContracts = databaseEnvironmentKeys.filter(
      (key) => process.env[key] === undefined,
    );
    if (
      passwordFile === undefined ||
      totpHelper === undefined ||
      livePhaseStatusFile === undefined
    ) {
      throw new Error(
        "live E2E environment loader did not provide the credential-file and TOTP-helper contracts",
      );
    }
    if (missingDatabaseContracts.length > 0) {
      throw new Error("live E2E environment loader did not provide server-evidence contracts");
    }
    if (!allowedOrigin.startsWith("https://")) {
      throw new Error("live E2E allowed origin must use public HTTPS");
    }
  });

  it("preserves source identity across rename move delete and explicit restore", async function () {
    this.timeout(600_000);
    recordLivePhase("source_lifecycle_scenario_started");
    await onboardFixtureDevice();
    recordLivePhase("source_lifecycle_onboarding_completed");
    await createFixtureNote();
    await triggerSyncNow();
    const before = await waitForCanonicalEvidence(
      (evidence) =>
        evidence.syncState === "active" &&
        evidence.activeLocatorCount === 1 &&
        evidence.lifecycleEventCount === 0,
      "initial canonical source did not settle",
    );
    const initialJournal = await waitForJournalEvidence(
      undefined,
      (evidence) =>
        evidence.sourceId === before.sourceId &&
        evidence.currentVersionId === before.currentVersionId &&
        evidence.pendingLifecycleCount === 0,
      "initial plugin source mapping did not settle",
    );
    recordLivePhase("source_lifecycle_initial_sync_completed");

    await renameFixtureNote();
    await browser.pause(300);
    await triggerSyncNow();
    await waitForCanonicalEvidence(
      (evidence) =>
        evidence.renameEventCount === 1 &&
        evidence.activeLocatorCount === 1 &&
        evidence.locatorHistoryCount === 2,
      "rename did not commit canonically",
    );
    recordLivePhase("source_lifecycle_rename_completed");

    await moveFixtureNote();
    await browser.pause(300);
    await triggerSyncNow();
    await waitForCanonicalEvidence(
      (evidence) =>
        evidence.moveEventCount === 1 &&
        evidence.activeLocatorCount === 1 &&
        evidence.locatorHistoryCount === 3,
      "move did not commit canonically",
    );
    recordLivePhase("source_lifecycle_move_completed");

    await deleteFixtureNote();
    await triggerSyncNow();
    await waitForCanonicalEvidence(
      (evidence) =>
        evidence.syncState === "deleted" &&
        evidence.deleteEventCount === 1 &&
        evidence.activeLocatorCount === 0 &&
        evidence.openTombstoneCount === 1,
      "delete did not commit canonically",
    );
    recordLivePhase("source_lifecycle_delete_completed");

    await explicitlyRestoreFixtureNote();
    await triggerSyncNow();
    const after = await waitForCanonicalEvidence(
      (evidence) =>
        evidence.syncState === "active" &&
        evidence.restoreEventCount === 1 &&
        evidence.lifecycleEventCount === 4 &&
        evidence.activeLocatorCount === 1 &&
        evidence.openTombstoneCount === 0,
      "explicit restore did not commit canonically",
    );
    recordLivePhase("source_lifecycle_restore_completed");
    const finalJournal = await waitForJournalEvidence(
      initialJournal.localFileId,
      (evidence) =>
        evidence.committedLifecycleCount === 4 &&
        evidence.pendingLifecycleCount === 0 &&
        evidence.blockedLifecycleCount === 0,
      "plugin lifecycle journal did not drain",
    );
    recordLivePhase("source_lifecycle_journal_drained");

    expect(after.sourceId).toBe(before.sourceId);
    expect(after.currentVersionId).toBe(before.currentVersionId);
    expect(finalJournal.sourceId).toBe(before.sourceId);
    expect(finalJournal.currentVersionId).toBe(before.currentVersionId);
    expect(finalJournal.pendingLifecycleCount).toBe(0);
    recordLivePhase("source_lifecycle_journey_completed");
    console.log(
      "SANITIZED_SOURCE_LIFECYCLE_EVIDENCE",
      JSON.stringify({
        stableSourceIdentity: after.sourceId === before.sourceId,
        stableVersionIdentity: after.currentVersionId === before.currentVersionId,
        lifecycleEventCount: after.lifecycleEventCount,
        locatorHistoryCount: after.locatorHistoryCount,
        pendingLifecycleCount: finalJournal.pendingLifecycleCount,
        blockedLifecycleCount: finalJournal.blockedLifecycleCount,
      }),
    );
  });
});
