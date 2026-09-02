import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { browser } from "@wdio/globals";
import { onboardLiveDevice } from "../support/live-device-onboarding";
import { writeLiveAcceptanceDiagnostic } from "../support/live-acceptance-phase-status";
import { runFromE2eRepositoryRoot } from "../support/repository-subprocess";

/**
 * Diagnostics live smoke round (2026-08-31 plan, task 5): one real-Obsidian
 * journey that triggers every remediation failure class cheaply and reads
 * each sanitized surface back — the wrong-origin connection detail token
 * (A-class), the policy SYSTEM failure trail through a temporarily broken
 * active-snapshot signature (policy-observability criterion 4), the
 * lifecycle locator-conflict rejection (L1) and the terminal cleared-reason
 * line after a server-side device revoke (A3 terminal case).
 *
 * Logs and the phase diagnostic file carry closed tokens, counts and
 * sanitized settings lines only — never an origin, path, credential,
 * signature byte or free-form server text. The snapshot-signature backup
 * lives in one untracked file under the runtime-logs directory and is
 * deleted after the restore; its bytes are never printed.
 */
const serverOrigin = process.env.E2E_SERVER_ORIGIN ?? "http://127.0.0.1:8000";
const allowedOrigin = process.env.E2E_ALLOWED_ORIGIN ?? "https://app.ducinvest.com";
const realPluginOrigin = process.env.E2E_PLUGIN_ORIGIN ?? "https://app.ducinvest.com";
const webUsername = process.env.E2E_WEB_USERNAME ?? "duc";
const passwordFile = process.env.E2E_WEB_PASSWORD_FILE;
const totpHelper = process.env.E2E_TOTP_HELPER;
const livePhaseStatusFile = process.env.E2E_LIVE_PHASE_STATUS_FILE;
const pluginDataPathSuffix = "plugins/knowledge-workspace/data.json";
/** RFC 2606 reserved TLD: syntactically a valid HTTPS origin, never resolves. */
const wrongOrigin = "https://knowledge-wrong-origin-rejected.invalid";
const fixtureNonce = crypto.randomUUID();
const policyFailureNotePath = `controlled-policy-failure-${fixtureNonce}.md`;
const conflictNotePath = `controlled-locator-conflict-${fixtureNonce}.md`;
const policyFailureNoteContent = `# Policy failure probe\n\n${fixtureNonce}\n`;
const conflictNoteInitialContent = `# Locator conflict original\n\n${fixtureNonce}\n`;
const conflictNoteReplacementContent = `# Locator conflict successor\n\n${fixtureNonce}\n`;
const databaseEnvironmentKeys = [
  "KNOWLEDGE_SECRET_ROOT",
  "KNOWLEDGE_DATABASE_HOST",
  "KNOWLEDGE_DATABASE_PORT",
  "KNOWLEDGE_DATABASE_NAME",
  "KNOWLEDGE_DATABASE_USER",
  "KNOWLEDGE_DATABASE_PASSWORD_FILE",
] as const;
const smokeDeviceName = `e2e-diagnostics-smoke-${fixtureNonce.slice(0, 8)}`;
let journalDirectoryPath: string | null = null;

function recordDiagnostic(diagnostic: Record<string, number>): void {
  if (livePhaseStatusFile !== undefined) {
    writeLiveAcceptanceDiagnostic(livePhaseStatusFile, diagnostic);
  }
}

async function readSettingDescription(settingName: string): Promise<string> {
  return browser.execute((name: string) => {
    const app = (
      window as unknown as {
        app: {
          setting: { open: () => void; openTabById: (tabId: string) => void };
        };
      }
    ).app;
    app.setting.open();
    app.setting.openTabById("knowledge-workspace");
    const settingNames = Array.from(document.querySelectorAll(".setting-item-name"));
    const target = settingNames.find((element) => element.textContent === name);
    return (
      target?.closest(".setting-item")?.querySelector(".setting-item-description")?.textContent ??
      ""
    );
  }, settingName);
}

async function waitForSettingLine(
  settingName: string,
  accepts: (line: string) => boolean,
  failureMessage: string,
  maximumAttempts = 90,
): Promise<string> {
  let lastLine = "";
  for (let attempt = 0; attempt < maximumAttempts; attempt += 1) {
    lastLine = await readSettingDescription(settingName);
    if (accepts(lastLine)) {
      return lastLine;
    }
    await browser.pause(1_000);
  }
  throw new Error(`${failureMessage}: ${lastLine}`);
}

async function setPluginServerOrigin(origin: string): Promise<void> {
  await browser.execute(
    async (dataPathSuffix: string, nextOrigin: string) => {
      const app = (
        window as unknown as {
          app: {
            vault: {
              configDir: string;
              adapter: {
                read: (vaultPath: string) => Promise<string>;
                write: (vaultPath: string, data: string) => Promise<void>;
              };
            };
          };
        }
      ).app;
      const dataPath = `${app.vault.configDir}/${dataPathSuffix}`;
      const current = JSON.parse(await app.vault.adapter.read(dataPath)) as Record<
        string,
        unknown
      >;
      await app.vault.adapter.write(
        dataPath,
        JSON.stringify({ ...current, server_origin: nextOrigin }),
      );
    },
    pluginDataPathSuffix,
    origin,
  );
}

async function reloadKnowledgeWorkspacePlugin(): Promise<void> {
  await browser.execute(async () => {
    const app = (
      window as unknown as {
        app: {
          plugins: {
            disablePlugin: (pluginId: string) => Promise<void>;
            enablePlugin: (pluginId: string) => Promise<void>;
          };
        };
      }
    ).app;
    await app.plugins.disablePlugin("knowledge-workspace");
    await app.plugins.enablePlugin("knowledge-workspace");
  });
}

async function writeFixtureNote(notePath: string, content: string): Promise<void> {
  await browser.execute(async (normalizedPath: string, noteContent: string) => {
    const app = (
      window as unknown as {
        app: {
          vault: {
            getAbstractFileByPath: (vaultPath: string) => unknown;
            create: (vaultPath: string, text: string) => Promise<void>;
            modify: (file: unknown, text: string) => Promise<void>;
          };
        };
      }
    ).app;
    const file = app.vault.getAbstractFileByPath(normalizedPath);
    if (file === null) {
      await app.vault.create(normalizedPath, noteContent);
      return;
    }
    await app.vault.modify(file, noteContent);
  }, notePath, content);
}

async function deleteFixtureNote(notePath: string): Promise<void> {
  await browser.execute(async (normalizedPath: string) => {
    const app = (
      window as unknown as {
        app: {
          vault: {
            getAbstractFileByPath: (vaultPath: string) => unknown;
            delete: (file: unknown, force?: boolean) => Promise<void>;
          };
        };
      }
    ).app;
    const file = app.vault.getAbstractFileByPath(normalizedPath);
    if (file === null) {
      throw new Error("controlled fixture was unavailable for delete");
    }
    await app.vault.delete(file, true);
  }, notePath);
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
  await browser.pause(1_000);
}

async function resolveJournalDirectoryPath(): Promise<string> {
  return browser.execute(() => {
    const app = (
      window as unknown as {
        app: {
          vault: {
            configDir: string;
            adapter: { getFullPath: (vaultPath: string) => string };
          };
        };
      }
    ).app;
    return app.vault.adapter.getFullPath(
      `${app.vault.configDir}/plugins/knowledge-workspace`,
    );
  });
}

interface SanitizedJournalEvidence {
  readonly contentCommittedCount: number;
  readonly contentPendingCount: number;
  readonly deleteCommittedCount: number;
  readonly restoreBlockedConflictCount: number;
}

async function readSanitizedJournalEvidence(
  controlledNormalizedPath: string,
): Promise<SanitizedJournalEvidence> {
  if (journalDirectoryPath === null) {
    throw new Error("sanitized journal evidence was unavailable");
  }
  const manifest = JSON.parse(
    fs.readFileSync(path.join(journalDirectoryPath, "journal.manifest.json"), "utf8"),
  ) as { current?: { generationNumber?: unknown } };
  const generationNumber = manifest.current?.generationNumber;
  if (!Number.isSafeInteger(generationNumber) || Number(generationNumber) < 0) {
    throw new Error("sanitized journal evidence was unavailable");
  }
  const journalBytes = fs.readFileSync(
    path.join(journalDirectoryPath, `journal.sqlite.g${generationNumber}`),
  );
  const initSqlJs = (await import("sql.js")).default;
  const engine = await initSqlJs();
  const database = new engine.Database(journalBytes);
  try {
    const scalar = (statementText: string): number => {
      const statement = database.prepare(statementText);
      try {
        statement.bind([controlledNormalizedPath]);
        return statement.step() ? Number(statement.get()[0] ?? 0) : 0;
      } finally {
        statement.free();
      }
    };
    return {
      contentCommittedCount: scalar(
        `select count(*) from journal_events event
          join local_files file on file.local_file_id = event.local_file_id
         where file.normalized_path = ? and event.state = 'committed'
           and event.operation is null`,
      ),
      contentPendingCount: scalar(
        `select count(*) from journal_events event
          join local_files file on file.local_file_id = event.local_file_id
         where file.normalized_path = ?
           and event.state in ('queued', 'preflight', 'uploading', 'waiting_retry')
           and event.operation is null`,
      ),
      deleteCommittedCount: scalar(
        `select count(*) from journal_events event
          join local_files file on file.local_file_id = event.local_file_id
         where file.normalized_path = ? and event.state = 'committed'
           and event.operation = 'delete'`,
      ),
      restoreBlockedConflictCount: scalar(
        `select count(*) from journal_events event
          join local_files file on file.local_file_id = event.local_file_id
         where file.normalized_path = ?
           and event.state = 'blocked_conflict' and event.operation = 'restore'`,
      ),
    };
  } finally {
    database.close();
  }
}

async function waitForJournalEvidence(
  controlledNormalizedPath: string,
  accepts: (evidence: SanitizedJournalEvidence) => boolean,
  failureMessage: string,
  maximumAttempts = 60,
): Promise<SanitizedJournalEvidence> {
  let lastEvidence: SanitizedJournalEvidence | null = null;
  for (let attempt = 0; attempt < maximumAttempts; attempt += 1) {
    try {
      lastEvidence = await readSanitizedJournalEvidence(controlledNormalizedPath);
      if (accepts(lastEvidence)) {
        return lastEvidence;
      }
    } catch {
      // An atomic generation swap may briefly move the manifest ahead of
      // the file read; retry without exposing a path detail.
    }
    await browser.pause(1_000);
  }
  throw new Error(`${failureMessage}: ${JSON.stringify(lastEvidence)}`);
}

/**
 * Flip one byte of the active policy snapshot's stored signature inside the
 * disposable CI project (the live analog of the runbook's broken-signer
 * class: every content boundary then fails closed with the typed SYSTEM
 * code). The original bytes are parked in one untracked backup file and are
 * never printed. `mode` is `corrupt` or `restore`; both close by printing
 * exactly `OK`.
 */
const snapshotSignatureScript = String.raw`
import json
import os
import sys
import tempfile
from pathlib import Path

import psycopg

mode = sys.argv[1]
backup_path = Path(os.environ["DIAGNOSTICS_SMOKE_BACKUP_FILE"])
secret_root = Path(os.environ["KNOWLEDGE_SECRET_ROOT"]).resolve(strict=True)
password_path = (
    secret_root / os.environ["KNOWLEDGE_DATABASE_PASSWORD_FILE"]
).resolve(strict=True)
if not password_path.is_relative_to(secret_root):
    raise ValueError
password = password_path.read_text(encoding="ascii").strip()
with psycopg.connect(
    host=os.environ["KNOWLEDGE_DATABASE_HOST"],
    port=int(os.environ["KNOWLEDGE_DATABASE_PORT"]),
    dbname=os.environ["KNOWLEDGE_DATABASE_NAME"],
    user=os.environ["KNOWLEDGE_DATABASE_USER"],
    password=password,
    connect_timeout=5,
    options="-c statement_timeout=5000 -c lock_timeout=2000",
) as connection:
    row = connection.execute(
        """
        select pol.policy_revision_id, pol.signature_bytes
          from knowledge.workspace_policy_state state
          join knowledge.source_policies pol
            on pol.policy_revision_id = state.active_policy_revision_id
         where state.active_policy_revision_id is not null
         order by state.workspace_id
         limit 1
        """
    ).fetchone()
    if row is None:
        raise ValueError
    revision_id, signature = row
    signature = bytes(signature)
    if mode == "corrupt":
        if backup_path.exists():
            raise ValueError
        backup_path.write_bytes(signature)
        corrupted = signature[:-1] + bytes([signature[-1] ^ 0xFF])
        connection.execute(
            "update knowledge.source_policies set signature_bytes = %s"
            " where policy_revision_id = %s",
            (corrupted, revision_id),
        )
        connection.commit()
    elif mode == "restore":
        if not backup_path.exists():
            raise ValueError
        original = backup_path.read_bytes()
        connection.execute(
            "update knowledge.source_policies set signature_bytes = %s"
            " where policy_revision_id = %s",
            (original, revision_id),
        )
        connection.commit()
        confirmed = connection.execute(
            "select signature_bytes from knowledge.source_policies"
            " where policy_revision_id = %s",
            (revision_id,),
        ).fetchone()
        if confirmed is None or bytes(confirmed[0]) != original:
            raise ValueError
        backup_path.unlink()
    else:
        raise ValueError
print("OK")
`;

async function runSnapshotSignatureMode(mode: "corrupt" | "restore"): Promise<void> {
  const repositoryRoot = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "../../../..",
  );
  const backupFile = path.join(
    repositoryRoot,
    ".local/runtime-logs",
    "diagnostics-smoke-signature-backup.bin",
  );
  const environment = {
    ...process.env,
    DIAGNOSTICS_SMOKE_BACKUP_FILE: backupFile,
  };
  const { stdout } = await runFromE2eRepositoryRoot(
    "uv",
    ["run", "python", "-c", snapshotSignatureScript, mode],
    import.meta.url,
    environment,
    30_000,
  );
  if (stdout.trim() !== "OK") {
    throw new Error("snapshot signature mode did not close cleanly");
  }
}

async function clickVisibleText(selector: string, expectedText: string): Promise<void> {
  const didClick = await browser.execute(
    (candidateSelector: string, text: string) => {
      const element = Array.from(document.querySelectorAll(candidateSelector)).find(
        (candidate) => candidate.textContent?.trim() === text,
      );
      if (!(element instanceof HTMLElement)) {
        return false;
      }
      element.click();
      return true;
    },
    selector,
    expectedText,
  );
  if (!didClick) {
    throw new Error("explicit restore control was unavailable");
  }
}

/**
 * Drive the restore command to an occupied target path: the local
 * reservation accepts, the shipped restore event is rejected by the server
 * with the typed locator conflict, and the journal parks the outcome.
 */
async function requestRestoreToOccupiedPath(): Promise<void> {
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
  const entered = await browser.execute(
    (targetPath: string) => {
      const input = document.querySelector(".modal-container input");
      if (!(input instanceof HTMLInputElement)) {
        return false;
      }
      input.value = targetPath;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    },
    conflictNotePath,
  );
  if (!entered) {
    throw new Error("explicit restore target control was unavailable");
  }
  await clickVisibleText(".modal-container button", "Restore");
  await browser.pause(250);
  // The target stays occupied by the successor note, so no staging happens;
  // confirm the restore request so the lifecycle event ships and parks.
  await clickVisibleText(".modal-container button", "Restore");
  // Dismiss any leftover modal so later settings reads stay unobstructed.
  await browser.execute(() => {
    const closeButton = document.querySelector(".modal-container .modal-close-button");
    if (closeButton instanceof HTMLElement) {
      closeButton.click();
    }
  });
}

function adminHeaders(cookies: readonly string[], csrf: string): Record<string, string> {
  return {
    "content-type": "application/json",
    origin: allowedOrigin,
    cookie: cookies.join("; "),
    "x-csrf-token": csrf,
  };
}

interface AdminDeviceRow {
  readonly device_id: string;
  readonly device_name: string;
}

async function revokeSmokeDevice(
  cookies: readonly string[],
  csrf: string,
): Promise<void> {
  const listResponse = await fetch(`${serverOrigin}/api/admin/devices`, {
    headers: { origin: allowedOrigin, cookie: cookies.join("; ") },
  });
  if (!listResponse.ok) {
    throw new Error(`admin device list failed: ${listResponse.status}`);
  }
  const listed = ((await listResponse.json()) as { data: { devices: AdminDeviceRow[] } })
    .data.devices;
  const target = listed.find((device) => device.device_name === smokeDeviceName);
  if (target === undefined) {
    throw new Error("smoke device was not listed");
  }
  const revokeResponse = await fetch(
    `${serverOrigin}/api/admin/devices/${target.device_id}/revoke`,
    {
      method: "POST",
      headers: adminHeaders(cookies, csrf),
      body: JSON.stringify({ device_name_confirmation: smokeDeviceName }),
    },
  );
  if (!revokeResponse.ok) {
    throw new Error(`admin device revoke failed: ${revokeResponse.status}`);
  }
}

describe("diagnostics surface live smoke (wrong origin, policy system failure, lifecycle conflict)", () => {
  before(() => {
    if (passwordFile === undefined || totpHelper === undefined) {
      throw new Error(
        "live E2E environment loader did not provide the credential-file and TOTP-helper contracts",
      );
    }
    const missingDatabaseContracts = databaseEnvironmentKeys.filter(
      (key) => process.env[key] === undefined,
    );
    if (missingDatabaseContracts.length > 0) {
      throw new Error("live E2E environment loader did not provide server-evidence contracts");
    }
    if (!allowedOrigin.startsWith("https://")) {
      throw new Error("live E2E allowed origin must use public HTTPS");
    }
  });

  it("surfaces every closed failure token through the remediation surfaces", async function () {
    this.timeout(720_000);

    const onboarding = await onboardLiveDevice({
      serverOrigin,
      allowedOrigin,
      webUsername,
      passwordFile: passwordFile as string,
      totpHelper: totpHelper as string,
      pluginDataPathSuffix,
      deviceName: smokeDeviceName,
    });
    const sessionCookies = [...onboarding.adminSessionCookies];
    const sessionCsrf = onboarding.adminSessionCsrf;
    journalDirectoryPath = await resolveJournalDirectoryPath();
    recordDiagnostic({ onboardingCompleted: 1 });

    // A-class baseline: the connection line carries no failure detail.
    await waitForSettingLine(
      "Connection status",
      (line) => line === "Connected",
      "baseline connection line was not the clean connected state",
    );

    // A-class wrong origin: one startup refresh must fail and the settings
    // detail line must name the closed transport token.
    await setPluginServerOrigin(wrongOrigin);
    await reloadKnowledgeWorkspacePlugin();
    const wrongOriginLine = await waitForSettingLine(
      "Connection status",
      (line) => line.includes("network_unavailable"),
      "wrong-origin connection detail line did not name the closed token",
    );
    console.log("WRONG_ORIGIN_CONNECTION_LINE", wrongOriginLine);
    recordDiagnostic({ wrongOriginDetailTokenObserved: 1 });

    // Restore the real origin; the connection must converge back.
    await setPluginServerOrigin(realPluginOrigin);
    await reloadKnowledgeWorkspacePlugin();
    await waitForSettingLine(
      "Connection status",
      (line) => line === "Connected",
      "restored-origin connection line did not converge back to connected",
    );
    recordDiagnostic({ wrongOriginRestoredConnected: 1 });

    // Policy SYSTEM failure window: corrupt the active snapshot signature,
    // drive one small-file create, and read the plugin-side wire failure.
    await runSnapshotSignatureMode("corrupt");
    try {
      await writeFixtureNote(policyFailureNotePath, policyFailureNoteContent);
      await triggerSyncNow();
      const trailLine = await waitForSettingLine(
        "Sync diagnostics trail",
        (line) => line.includes("wire_failure") && line.includes("server_error"),
        "policy system failure did not surface in the diagnostics trail",
      );
      console.log("POLICY_SYSTEM_FAILURE_TRAIL_LINE", trailLine);
      recordDiagnostic({ policySystemFailureTrailObserved: 1 });
      const parkedEvidence = await waitForJournalEvidence(
        policyFailureNotePath,
        (evidence) => evidence.contentPendingCount >= 1,
        "policy system failure did not park the controlled note as pending",
      );
      recordDiagnostic({ policySystemFailureParkedCount: parkedEvidence.contentPendingCount });
    } finally {
      await runSnapshotSignatureMode("restore");
    }

    // Signature restored: the parked note must converge to committed.
    await triggerSyncNow();
    const convergedEvidence = await waitForJournalEvidence(
      policyFailureNotePath,
      (evidence) =>
        evidence.contentCommittedCount >= 1 && evidence.contentPendingCount === 0,
      "restored signer window did not converge the controlled note",
    );
    recordDiagnostic({
      policySystemFailureConvergedCount: convergedEvidence.contentCommittedCount,
    });

    // L1 locator conflict: commit one conflict note, delete it (tombstone),
    // let a successor claim the same path, then restore the tombstone to
    // the occupied target so the server rejects with the typed conflict.
    await writeFixtureNote(conflictNotePath, conflictNoteInitialContent);
    await triggerSyncNow();
    await waitForJournalEvidence(
      conflictNotePath,
      (evidence) => evidence.contentCommittedCount >= 1,
      "conflict fixture note did not commit",
    );
    await deleteFixtureNote(conflictNotePath);
    await triggerSyncNow();
    await waitForJournalEvidence(
      conflictNotePath,
      (evidence) => evidence.deleteCommittedCount >= 1,
      "conflict fixture delete did not commit",
    );
    await writeFixtureNote(conflictNotePath, conflictNoteReplacementContent);
    await triggerSyncNow();
    await waitForJournalEvidence(
      conflictNotePath,
      (evidence) => evidence.contentCommittedCount >= 1,
      "conflict successor note did not commit",
    );
    await requestRestoreToOccupiedPath();
    await triggerSyncNow();
    const conflictEvidence = await waitForJournalEvidence(
      conflictNotePath,
      (evidence) => evidence.restoreBlockedConflictCount >= 1,
      "typed restore rejection did not park as blocked_conflict",
    );
    recordDiagnostic({
      lifecycleConflictParkedCount: conflictEvidence.restoreBlockedConflictCount,
    });
    console.log("LIFECYCLE_CONFLICT_PARKED", "blocked_conflict");

    // A3 terminal case: revoke the device server-side, let one refresh
    // answer the closed terminal code, then reload so the durable
    // tombstone reason renders through the Last cleared reason line.
    await revokeSmokeDevice(sessionCookies, sessionCsrf);
    await reloadKnowledgeWorkspacePlugin();
    const revokedLine = await waitForSettingLine(
      "Connection status",
      (line) => line.includes("device_revoked"),
      "revoked refresh did not surface the terminal cleared token",
    );
    console.log("TERMINAL_REVOKED_CONNECTION_LINE", revokedLine);
    recordDiagnostic({ terminalRevokedDetailObserved: 1 });
    await reloadKnowledgeWorkspacePlugin();
    const clearedReasonLine = await waitForSettingLine(
      "Connection status",
      (line) => line.includes("Last cleared reason: device_revoked"),
      "durable cleared reason did not render after reload",
    );
    console.log("TERMINAL_CLEARED_REASON_LINE", clearedReasonLine);
    recordDiagnostic({ terminalClearedReasonLineObserved: 1 });
  });
});
