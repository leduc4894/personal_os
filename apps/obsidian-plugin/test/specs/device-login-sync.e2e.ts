import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { browser } from "@wdio/globals";
import { onboardLiveDevice } from "../support/live-device-onboarding";
import {
  type LiveAcceptancePhaseResultCode,
  writeLiveAcceptanceDiagnostic,
  writeLiveAcceptancePhaseStatus,
} from "../support/live-acceptance-phase-status";
import { runFromE2eRepositoryRoot } from "../support/repository-subprocess";

/**
 * Live-server device login and small-file sync journey. The real grant is
 * created and operator-approved through the real admin routes (Origin gate
 * + CSRF double-submit), then injected as the plugin's persisted pending
 * state so a plugin reload resumes the real exchange: poll → credential →
 * onboarding trust → policy acceptance. An edit of the fixture note must
 * then run settle → policy gate → preflight/content against the live API.
 * The claimed-upload case interrupts a receiving operation with an irrelevant
 * locator-only policy revision, then proves exact-token resume to one server
 * publication and one terminal journal receipt.
 *
 * This protected gate is mandatory for release reviews that touch the live
 * claimed-resume contract. It needs the loader-provided live server and Web
 * credential, so the offline default remains separate. Logs carry closed
 * labels and fixture-scoped counts only — never codes, cookies or Vault
 * content.
 */
const serverOrigin = process.env.E2E_SERVER_ORIGIN ?? "http://127.0.0.1:8000";
const allowedOrigin = process.env.E2E_ALLOWED_ORIGIN ?? "https://app.ducinvest.com";
const webUsername = process.env.E2E_WEB_USERNAME ?? "duc";
const passwordFile = process.env.E2E_WEB_PASSWORD_FILE;
const totpHelper = process.env.E2E_TOTP_HELPER;
const livePhaseStatusFile = process.env.E2E_LIVE_PHASE_STATUS_FILE;
const pluginDataPathSuffix = "plugins/knowledge-workspace/data.json";
const fixtureNonce = crypto.randomUUID();
const existingFixtureNotePath = `controlled-existing-${crypto.randomUUID()}.md`;
const fixtureNotePath = `controlled-live-${crypto.randomUUID()}.md`;
const policyRecoveryNotePath = `controlled-policy-recovery-${crypto.randomUUID()}.md`;
const claimedResumeNotePath = `controlled-claimed-resume-${crypto.randomUUID()}.md`;
const irrelevantFolderPrefix = `irrelevant-policy-${crypto.randomUUID()}`;
const existingFixtureNoteContent =
  `# Existing note\n\nPresent before automatic startup convergence.\n${fixtureNonce}\n`;
const fixtureNoteContent = `# Test note\n\nUpdated by the live login journey.\n${fixtureNonce}\n`;
const policyRecoveryNoteContent =
  `# Policy recovery note\n\nRe-admitted automatically after policy authorization.\n${fixtureNonce}\n`;
const claimedResumeHeader = `# Controlled claimed resume\n\n${fixtureNonce}\n`;
const claimedResumeNoteContent =
  claimedResumeHeader +
  "x".repeat(1024 * 1024 - Buffer.byteLength(claimedResumeHeader));
const existingFixtureDeclaredSha256 = crypto
  .createHash("sha256")
  .update(existingFixtureNoteContent)
  .digest("hex");
const fixtureDeclaredSha256 = crypto.createHash("sha256").update(fixtureNoteContent).digest("hex");
const policyRecoveryDeclaredSha256 = crypto
  .createHash("sha256")
  .update(policyRecoveryNoteContent)
  .digest("hex");
const claimedResumeDeclaredSha256 = crypto
  .createHash("sha256")
  .update(claimedResumeNoteContent)
  .digest("hex");
const databaseEnvironmentKeys = [
  "KNOWLEDGE_SECRET_ROOT",
  "KNOWLEDGE_DATABASE_HOST",
  "KNOWLEDGE_DATABASE_PORT",
  "KNOWLEDGE_DATABASE_NAME",
  "KNOWLEDGE_DATABASE_USER",
  "KNOWLEDGE_DATABASE_PASSWORD_FILE",
] as const;
let adminSessionCookies: string[] | null = null;
let adminSessionCsrf: string | null = null;
let journalDirectoryPath: string | null = null;

function recordLivePhase(resultCode: LiveAcceptancePhaseResultCode): void {
  if (livePhaseStatusFile !== undefined) {
    writeLiveAcceptancePhaseStatus(livePhaseStatusFile, resultCode);
  }
}

function recordAutomaticJourneyStage(stageCode: number): void {
  if (livePhaseStatusFile !== undefined) {
    writeLiveAcceptanceDiagnostic(livePhaseStatusFile, { journeyStageCode: stageCode });
  }
}

interface PolicyStatus {
  readonly active_policy_revision_id: string | null;
  readonly active_revision_number: number;
  readonly draft: { readonly draft_version: number };
}

interface PolicyPreview {
  readonly policy_preview_id: string;
  readonly status: string;
  readonly policy_draft_id: string;
  readonly draft_version: number;
  readonly draft_sha256: string;
  readonly base_policy_revision_id: string | null;
  readonly impact_digest: string | null;
}

interface PreparedPolicyPublication {
  readonly preview: PolicyPreview;
  readonly expectedActivePolicyRevisionId: string | null;
  readonly expectedActiveRevisionNumber: number;
}

interface ServerPublicationEvidence {
  readonly sourceCount: number;
  readonly sourceVersionCount: number;
  readonly syncEventCount: number;
  readonly operationCount: number;
  readonly committedOperationCount: number;
  readonly exactOperationPublicationCount: number;
  readonly receivingUnpublishedOperationCount: number;
}

interface SanitizedJournalEvidence {
  readonly committedCount: number;
  readonly pendingCount: number;
  readonly mappedCount: number;
  readonly excludedPolicyCount: number;
  readonly queuedCount: number;
  readonly preflightCount: number;
  readonly uploadingCount: number;
  readonly waitingRetryCount: number;
}

const serverEvidenceScript = String.raw`
import json
import os
import re
import time
from pathlib import Path

import psycopg

try:
    secret_root = Path(os.environ["KNOWLEDGE_SECRET_ROOT"]).resolve(strict=True)
    password_path = (
        secret_root / os.environ["KNOWLEDGE_DATABASE_PASSWORD_FILE"]
    ).resolve(strict=True)
    if not password_path.is_relative_to(secret_root):
        raise ValueError
    controlled_digests = os.environ["SERVER_EVIDENCE_DECLARED_SHA256S"].split(",")
    if not controlled_digests or any(
        re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in controlled_digests
    ):
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
        statement = """
            with controlled_operations as (
              select * from knowledge.small_file_upload_operations
              where declared_sha256 = any(%s)
            )
            select
              (select count(distinct source.source_id)
                 from controlled_operations operation
                 join knowledge.sources source
                   on source.source_id = operation.result_source_id),
              (select count(distinct version.source_version_id)
                 from controlled_operations operation
                 join knowledge.source_versions version
                   on version.source_version_id = operation.result_source_version_id),
              (select count(distinct event.event_id)
                 from controlled_operations operation
                 join knowledge.sync_events event on event.event_id = operation.event_id),
              (select count(*) from controlled_operations),
              (select count(*) from controlled_operations where state = 'committed'),
              (select count(*)
                 from controlled_operations operation
                 join knowledge.sources source
                   on source.source_id = operation.result_source_id
                 join knowledge.source_versions version
                   on version.source_version_id = operation.result_source_version_id
                  and version.source_id = source.source_id
                 join knowledge.sync_events event
                   on event.event_id = operation.event_id
                 and event.source_id = source.source_id
                  and event.committed_version_id = version.source_version_id
                where operation.state = 'committed'),
              (select count(*) from controlled_operations
                where state = 'receiving'
                  and result_kind is null
                  and result_source_id is null
                  and result_source_version_id is null)
            """
        should_wait_for_receiving = os.environ.get("SERVER_EVIDENCE_WAIT_FOR_RECEIVING") == "1"
        deadline = time.monotonic() + 30
        while True:
            row = connection.execute(statement, (controlled_digests,)).fetchone()
            if (
                not should_wait_for_receiving
                or row is None
                or int(row[6]) == 1
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(0.01)
    if row is None:
        raise ValueError
    print(json.dumps({
        "sourceCount": int(row[0]),
        "sourceVersionCount": int(row[1]),
        "syncEventCount": int(row[2]),
        "operationCount": int(row[3]),
        "committedOperationCount": int(row[4]),
        "exactOperationPublicationCount": int(row[5]),
        "receivingUnpublishedOperationCount": int(row[6]),
    }, separators=(",", ":")))
except BaseException:
    print(json.dumps({"state": "server_evidence_unavailable"}))
    raise SystemExit(1)
`;

async function responseData<T>(response: Response, operation: string): Promise<T> {
  if (!response.ok) {
    throw new Error(`${operation} failed: ${response.status}`);
  }
  return ((await response.json()) as { data: T }).data;
}

function adminHeaders(cookies: string[], csrf: string): Record<string, string> {
  return {
    "content-type": "application/json",
    origin: allowedOrigin,
    cookie: cookies.join("; "),
    "x-csrf-token": csrf,
  };
}

type PreparedRule =
  | {
      readonly rule_id: string;
      readonly rule_kind: "extension";
      readonly extension: ".md" | ".tmp";
    }
  | {
      readonly rule_id: string;
      readonly rule_kind: "folder_prefix";
      readonly folder_prefix: string;
    };

async function prepareSingleExclusionRule(
  cookies: string[],
  csrf: string,
  rule: PreparedRule,
): Promise<PreparedPolicyPublication> {
  const status = await responseData<PolicyStatus>(
    await fetch(`${serverOrigin}/api/admin/exclusion-policy`, {
      headers: { origin: allowedOrigin, cookie: cookies.join("; ") },
    }),
    "policy status",
  );
  await responseData(
    await fetch(`${serverOrigin}/api/admin/exclusion-policy/draft`, {
      method: "PUT",
      headers: adminHeaders(cookies, csrf),
      body: JSON.stringify({
        expected_draft_version: status.draft.draft_version,
        rules: [rule],
      }),
    }),
    "policy draft replacement",
  );
  const requested = await responseData<PolicyPreview>(
    await fetch(`${serverOrigin}/api/admin/exclusion-policy/previews`, {
      method: "POST",
      headers: adminHeaders(cookies, csrf),
    }),
    "policy preview request",
  );
  let preview = requested;
  for (let attempt = 0; attempt < 60 && preview.status !== "ready"; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    preview = await responseData<PolicyPreview>(
      await fetch(
        `${serverOrigin}/api/admin/exclusion-policy/previews/${requested.policy_preview_id}`,
        { headers: { origin: allowedOrigin, cookie: cookies.join("; ") } },
      ),
      "policy preview poll",
    );
  }
  if (preview.status !== "ready" || preview.impact_digest === null) {
    throw new Error(`policy preview did not become ready: ${preview.status}`);
  }
  return {
    preview,
    expectedActivePolicyRevisionId: status.active_policy_revision_id,
    expectedActiveRevisionNumber: status.active_revision_number,
  };
}

async function prepareExtensionExclusionRule(
  cookies: string[],
  csrf: string,
  extension: ".md" | ".tmp",
): Promise<PreparedPolicyPublication> {
  return prepareSingleExclusionRule(cookies, csrf, {
    rule_id: crypto.randomUUID(),
    rule_kind: "extension",
    extension,
  });
}

async function prepareIrrelevantFolderExclusionRule(
  cookies: string[],
  csrf: string,
): Promise<PreparedPolicyPublication> {
  return prepareSingleExclusionRule(cookies, csrf, {
    rule_id: crypto.randomUUID(),
    rule_kind: "folder_prefix",
    folder_prefix: irrelevantFolderPrefix,
  });
}

async function publishPreparedPolicy(
  cookies: string[],
  csrf: string,
  prepared: PreparedPolicyPublication,
): Promise<number> {
  const publication = await responseData<{ revision_number: number; rule_count: number }>(
    await fetch(`${serverOrigin}/api/admin/exclusion-policy/publications`, {
      method: "POST",
      headers: {
        ...adminHeaders(cookies, csrf),
        "X-Idempotency-Key": `obsidian-e2e-${crypto.randomUUID()}`,
      },
      body: JSON.stringify({
        policy_preview_id: prepared.preview.policy_preview_id,
        policy_draft_id: prepared.preview.policy_draft_id,
        expected_draft_version: prepared.preview.draft_version,
        expected_draft_sha256: prepared.preview.draft_sha256,
        preview_impact_digest: prepared.preview.impact_digest,
        expected_active_policy_revision_id: prepared.expectedActivePolicyRevisionId,
        expected_active_revision_number: prepared.expectedActiveRevisionNumber,
        confirmation: "PUBLISH EXCLUSION POLICY",
      }),
    }),
    "policy publication",
  );
  if (publication.rule_count !== 1) {
    throw new Error(`policy publication rule count mismatch: ${publication.rule_count}`);
  }
  return publication.revision_number;
}

async function readServerPublicationEvidence(
  controlledDeclaredSha256: string,
  shouldWaitForReceiving = false,
): Promise<ServerPublicationEvidence> {
  const { stdout } = await runFromE2eRepositoryRoot(
    "uv",
    ["run", "python", "-c", serverEvidenceScript],
    import.meta.url,
    {
      ...process.env,
      SERVER_EVIDENCE_DECLARED_SHA256S: controlledDeclaredSha256,
      SERVER_EVIDENCE_WAIT_FOR_RECEIVING: shouldWaitForReceiving ? "1" : "0",
    },
    shouldWaitForReceiving ? 40_000 : 10_000,
  );
  const parsed = JSON.parse(stdout) as Record<string, unknown>;
  const evidenceKeys = [
    "sourceCount",
    "sourceVersionCount",
    "syncEventCount",
    "operationCount",
    "committedOperationCount",
    "exactOperationPublicationCount",
    "receivingUnpublishedOperationCount",
  ] as const;
  for (const key of evidenceKeys) {
    if (!Number.isSafeInteger(parsed[key]) || Number(parsed[key]) < 0) {
      throw new Error("sanitized server publication evidence was invalid");
    }
  }
  return parsed as unknown as ServerPublicationEvidence;
}

function hasExactlyOneCanonicalPublication(evidence: ServerPublicationEvidence): boolean {
  return (
    evidence.sourceCount !== 1 ||
    evidence.sourceVersionCount !== 1 ||
    evidence.syncEventCount !== 1 ||
    evidence.operationCount !== 1 ||
    evidence.committedOperationCount !== 1 ||
    evidence.exactOperationPublicationCount !== 1 ||
    evidence.receivingUnpublishedOperationCount !== 0
  ) === false;
}

function requireExactlyOneCanonicalPublication(
  evidence: ServerPublicationEvidence,
  failureMessage: string,
): void {
  if (!hasExactlyOneCanonicalPublication(evidence)) {
    throw new Error(failureMessage);
  }
}

async function readPluginData(): Promise<Record<string, unknown>> {
  return browser.execute(
    async (dataPathSuffix: string) => {
      const app = (
        window as unknown as {
          app: {
            vault: {
              configDir: string;
              adapter: { read: (path: string) => Promise<string> };
            };
          };
        }
      ).app;
      const raw = await app.vault.adapter.read(`${app.vault.configDir}/${dataPathSuffix}`);
      return JSON.parse(raw) as Record<string, unknown>;
    },
    pluginDataPathSuffix,
  );
}

async function readStatusBarText(): Promise<string> {
  return browser.execute(() =>
    Array.from(document.querySelectorAll(".status-bar-item"))
      .map((element) => element.textContent ?? "")
      .join("|"),
  );
}

async function readLocalNoteSyncStatusListText(): Promise<string> {
  return browser.execute(() => {
    const app = (
      window as unknown as {
        app: {
          setting: {
            open: () => void;
            openTabById: (tabId: string) => void;
          };
        };
      }
    ).app;
    app.setting.open();
    app.setting.openTabById("knowledge-workspace");
    const settingNames = Array.from(document.querySelectorAll(".setting-item-name"));
    const syncStatusName = settingNames.find(
      (element) => element.textContent === "Sync status by note",
    );
    return syncStatusName
      ?.closest(".setting-item")
      ?.querySelector(".setting-item-description")
      ?.textContent ?? "";
  });
}

async function writeFixtureNote(notePath: string, content: string): Promise<void> {
  await browser.execute(async (normalizedPath: string, noteContent: string) => {
    const app = (
      window as unknown as {
        app: {
          vault: {
            getAbstractFileByPath: (path: string) => unknown;
            create: (path: string, content: string) => Promise<void>;
            modify: (file: unknown, content: string) => Promise<void>;
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

async function editFixtureNoteForClaimedResume(): Promise<void> {
  await browser.execute(async (notePath: string, content: string) => {
    const app = (
      window as unknown as {
        app: {
          vault: {
            create: (path: string, content: string) => Promise<void>;
          };
        };
      }
    ).app;
    await app.vault.create(notePath, content);
  }, claimedResumeNotePath, claimedResumeNoteContent);
}

async function resolveJournalDirectoryPath(): Promise<string> {
  return await browser.execute(() => {
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
    const scalar = (statementText: string, parameters: readonly string[]): number => {
      const statement = database.prepare(statementText);
      try {
        statement.bind([...parameters]);
        return statement.step() ? Number(statement.get()[0] ?? 0) : 0;
      } finally {
        statement.free();
      }
    };
    const eventCount = (statePredicate: string): number =>
      scalar(
        `select count(*) from journal_events event
          join local_files file on file.local_file_id = event.local_file_id
          where file.normalized_path = ? and ${statePredicate}`,
        [controlledNormalizedPath],
      );
    const mappedCount = scalar(
      `select count(*) from local_files
        where normalized_path = ? and source_id is not null and base_version_id is not null`,
      [controlledNormalizedPath],
    );
    return {
      committedCount: eventCount("event.state = 'committed'"),
      pendingCount: eventCount(
        "event.state in ('queued', 'preflight', 'uploading', 'waiting_retry')",
      ),
      mappedCount,
      excludedPolicyCount: eventCount("event.state = 'excluded_policy'"),
      queuedCount: eventCount("event.state = 'queued'"),
      preflightCount: eventCount("event.state = 'preflight'"),
      uploadingCount: eventCount("event.state = 'uploading'"),
      waitingRetryCount: eventCount("event.state = 'waiting_retry'"),
    };
  } finally {
    database.close();
  }
}

async function readOpaqueJournalOperationIdentity(
  controlledNormalizedPath: string,
): Promise<string> {
  if (journalDirectoryPath === null) {
    throw new Error("opaque journal identity was unavailable");
  }
  const manifest = JSON.parse(
    fs.readFileSync(path.join(journalDirectoryPath, "journal.manifest.json"), "utf8"),
  ) as { current?: { generationNumber?: unknown } };
  const generationNumber = manifest.current?.generationNumber;
  if (!Number.isSafeInteger(generationNumber) || Number(generationNumber) < 0) {
    throw new Error("opaque journal identity was unavailable");
  }
  const journalBytes = fs.readFileSync(
    path.join(journalDirectoryPath, `journal.sqlite.g${generationNumber}`),
  );
  const initSqlJs = (await import("sql.js")).default;
  const engine = await initSqlJs();
  const database = new engine.Database(journalBytes);
  try {
    const statement = database.prepare(
      `select event.operation_id from journal_events event
        join local_files file on file.local_file_id = event.local_file_id
        where file.normalized_path = ? and event.operation_id is not null`,
    );
    try {
      statement.bind([controlledNormalizedPath]);
      if (!statement.step()) {
        throw new Error("opaque journal identity was unavailable");
      }
      const operationIdentity = statement.get()[0];
      if (typeof operationIdentity !== "string" || operationIdentity.length === 0) {
        throw new Error("opaque journal identity was unavailable");
      }
      if (statement.step()) {
        throw new Error("opaque journal identity was not fixture-unique");
      }
      return operationIdentity;
    } finally {
      statement.free();
    }
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
      const evidence = await readSanitizedJournalEvidence(controlledNormalizedPath);
      lastEvidence = evidence;
      if (accepts(evidence)) {
        return evidence;
      }
    } catch {
      // An atomic generation swap may briefly move the manifest ahead of the
      // file read. Retry without exposing a path or filesystem detail.
    }
    await browser.pause(1_000);
  }
  throw new Error(`${failureMessage}: ${JSON.stringify(lastEvidence)}`);
}

async function waitForAutomaticCommitWithOneSettledEvent(
  controlledNormalizedPath: string,
  retryContent: string,
  accepts: (evidence: SanitizedJournalEvidence) => boolean,
  failureMessage: string,
): Promise<SanitizedJournalEvidence> {
  try {
    return await waitForJournalEvidence(
      controlledNormalizedPath,
      accepts,
      failureMessage,
      30,
    );
  } catch {
    const pendingEvidence = await readSanitizedJournalEvidence(controlledNormalizedPath);
    if (
      pendingEvidence.committedCount !== 0 ||
      pendingEvidence.pendingCount !== 1
    ) {
      throw new Error(failureMessage);
    }
    if (livePhaseStatusFile !== undefined) {
      writeLiveAcceptanceDiagnostic(livePhaseStatusFile, {
        automaticRetryEventCount: 1,
        ...pendingEvidence,
      });
    }
    // A retained retry is eligible by now (the first bounded backoff is at
    // most two seconds). Rewriting the same bytes emits one ordinary settled
    // Vault event: capture preserves the frozen event identity and its
    // automatic queue trigger replays it without any command surface.
    await writeFixtureNote(controlledNormalizedPath, retryContent);
    return waitForJournalEvidence(
      controlledNormalizedPath,
      accepts,
      failureMessage,
    );
  }
}

async function waitForAutomaticServerPublicationWithOneSettledEvent(
  controlledDeclaredSha256: string,
  controlledNormalizedPath: string,
  retryContent: string,
  failureMessage: string,
): Promise<ServerPublicationEvidence> {
  let lastEvidence: ServerPublicationEvidence | null = null;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    lastEvidence = await readServerPublicationEvidence(controlledDeclaredSha256);
    if (hasExactlyOneCanonicalPublication(lastEvidence)) {
      return lastEvidence;
    }
    await browser.pause(1_000);
  }
  if (livePhaseStatusFile !== undefined && lastEvidence !== null) {
    writeLiveAcceptanceDiagnostic(livePhaseStatusFile, {
      automaticRetryEventCount: 1,
      operationCount: lastEvidence.operationCount,
      committedOperationCount: lastEvidence.committedOperationCount,
      exactOperationPublicationCount: lastEvidence.exactOperationPublicationCount,
    });
  }
  await writeFixtureNote(controlledNormalizedPath, retryContent);
  for (let attempt = 0; attempt < 60; attempt += 1) {
    lastEvidence = await readServerPublicationEvidence(controlledDeclaredSha256);
    if (hasExactlyOneCanonicalPublication(lastEvidence)) {
      return lastEvidence;
    }
    await browser.pause(1_000);
  }
  throw new Error(failureMessage);
}

async function waitForStatusText(
  accepts: (statusText: string) => boolean,
  failureMessage: string,
): Promise<string> {
  let lastStatusText = "";
  for (let attempt = 0; attempt < 60; attempt += 1) {
    lastStatusText = await readStatusBarText();
    if (accepts(lastStatusText)) {
      return lastStatusText;
    }
    await browser.pause(1_000);
  }
  throw new Error(`${failureMessage}: ${lastStatusText}`);
}

async function waitForPolicyRecoveryNoteToRenderSynced(): Promise<void> {
  let lastStatusListText = "";
  for (let attempt = 0; attempt < 60; attempt += 1) {
    lastStatusListText = await readLocalNoteSyncStatusListText();
    if (lastStatusListText.includes(`${policyRecoveryNotePath} — Synced`)) {
      return;
    }
    await browser.pause(1_000);
  }
  if (livePhaseStatusFile !== undefined) {
    writeLiveAcceptanceDiagnostic(livePhaseStatusFile, {
      journeyStageCode: 331,
      controlledStatusPresentCount: Number(lastStatusListText.includes(policyRecoveryNotePath)),
      syncedStatusPresentCount: Number(
        lastStatusListText.includes(`${policyRecoveryNotePath} — Synced`),
      ),
      policyBlockedStatusPresentCount: Number(
        lastStatusListText.includes(`${policyRecoveryNotePath} — Policy blocked`),
      ),
      retryingStatusPresentCount: Number(
        lastStatusListText.includes(`${policyRecoveryNotePath} — Retrying`),
      ),
      reconcileRequiredStatusPresentCount: Number(
        lastStatusListText.includes(`${policyRecoveryNotePath} — Reconciliation required`),
      ),
    });
  }
  throw new Error("local note status list did not report the controlled successor as synced");
}

async function disableKnowledgeWorkspacePlugin(): Promise<void> {
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
  });
}

async function enableKnowledgeWorkspacePlugin(): Promise<void> {
  await browser.execute(async () => {
    const app = (
      window as unknown as {
        app: {
          plugins: {
            enablePlugin: (pluginId: string) => Promise<void>;
          };
        };
      }
    ).app;
    await app.plugins.enablePlugin("knowledge-workspace");
  });
}

describe("device login and small-file sync (live server)", () => {
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
  });

  after(async function () {
    this.timeout(120_000);
    if (adminSessionCookies === null || adminSessionCsrf === null) {
      return;
    }
    const restored = await prepareExtensionExclusionRule(
      adminSessionCookies,
      adminSessionCsrf,
      ".tmp",
    );
    const restoredRevision = await publishPreparedPolicy(
      adminSessionCookies,
      adminSessionCsrf,
      restored,
    );
    console.log("TMP_POLICY_RESTORED", restoredRevision > 0);
  });

  it("proves automatic convergence for existing, new, and policy-authorized notes", async function () {
    this.timeout(720_000);
    recordAutomaticJourneyStage(10);

    const existingBaseline = await readServerPublicationEvidence(
      existingFixtureDeclaredSha256,
    );
    if (existingBaseline.operationCount !== 0) {
      throw new Error("existing-note publication identity was not unique before capture");
    }
    recordAutomaticJourneyStage(20);
    await disableKnowledgeWorkspacePlugin();
    recordAutomaticJourneyStage(30);
    await writeFixtureNote(existingFixtureNotePath, existingFixtureNoteContent);
    recordAutomaticJourneyStage(40);

    const onboarding = await onboardLiveDevice({
      serverOrigin,
      allowedOrigin,
      webUsername,
      passwordFile: passwordFile as string,
      totpHelper: totpHelper as string,
      pluginDataPathSuffix,
      deviceName: "e2e-automatic-existing",
    });
    recordAutomaticJourneyStage(50);
    const sessionCookies = [...onboarding.adminSessionCookies];
    const sessionCsrf = onboarding.adminSessionCsrf;
    adminSessionCookies = sessionCookies;
    adminSessionCsrf = sessionCsrf;
    journalDirectoryPath = await resolveJournalDirectoryPath();
    recordAutomaticJourneyStage(60);

    const existingJournalEvidence = await waitForAutomaticCommitWithOneSettledEvent(
      existingFixtureNotePath,
      existingFixtureNoteContent,
      (evidence) =>
        evidence.committedCount === 1 &&
        evidence.pendingCount === 0 &&
        evidence.mappedCount === 1,
      "automatic startup convergence did not commit the existing note",
    );
    recordAutomaticJourneyStage(70);
    const existingServerEvidence = await readServerPublicationEvidence(
      existingFixtureDeclaredSha256,
    );
    requireExactlyOneCanonicalPublication(
      existingServerEvidence,
      "existing note did not produce exactly one canonical publication",
    );
    recordLivePhase("automatic_existing_note_committed");
    recordAutomaticJourneyStage(100);

    const newNoteBaseline = await readServerPublicationEvidence(fixtureDeclaredSha256);
    if (newNoteBaseline.operationCount !== 0) {
      throw new Error("new-note publication identity was not unique before capture");
    }
    recordAutomaticJourneyStage(110);
    await writeFixtureNote(fixtureNotePath, fixtureNoteContent);
    recordAutomaticJourneyStage(120);
    let newNoteJournalEvidence: SanitizedJournalEvidence;
    try {
      newNoteJournalEvidence = await waitForAutomaticCommitWithOneSettledEvent(
        fixtureNotePath,
        fixtureNoteContent,
        (evidence) =>
          evidence.committedCount === 1 &&
          evidence.pendingCount === 0 &&
          evidence.mappedCount === 1,
        "new note did not converge automatically",
      );
    } catch {
      try {
        const evidence = await readSanitizedJournalEvidence(fixtureNotePath);
        if (livePhaseStatusFile !== undefined) {
          writeLiveAcceptanceDiagnostic(livePhaseStatusFile, {
            journeyStageCode: 121,
            ...evidence,
          });
        }
      } catch {
        recordAutomaticJourneyStage(122);
      }
      throw new Error("new note did not converge automatically");
    }
    recordAutomaticJourneyStage(130);
    const newNoteServerEvidence = await readServerPublicationEvidence(fixtureDeclaredSha256);
    requireExactlyOneCanonicalPublication(
      newNoteServerEvidence,
      "new note did not produce exactly one canonical publication",
    );
    recordAutomaticJourneyStage(140);
    recordLivePhase("automatic_new_note_committed");
    recordAutomaticJourneyStage(200);

    const markdownPolicy = await prepareExtensionExclusionRule(
      sessionCookies,
      sessionCsrf,
      ".md",
    );
    recordAutomaticJourneyStage(210);
    const markdownPolicyRevision = await publishPreparedPolicy(
      sessionCookies,
      sessionCsrf,
      markdownPolicy,
    );
    recordAutomaticJourneyStage(220);
    await onboardLiveDevice({
      serverOrigin,
      allowedOrigin,
      webUsername,
      passwordFile: passwordFile as string,
      totpHelper: totpHelper as string,
      pluginDataPathSuffix,
      deviceName: "e2e-policy-blocked",
      minimumPolicyRevision: markdownPolicyRevision,
    });
    recordAutomaticJourneyStage(230);
    await writeFixtureNote(policyRecoveryNotePath, policyRecoveryNoteContent);
    recordAutomaticJourneyStage(240);
    const blockedJournalEvidence = await waitForJournalEvidence(
      policyRecoveryNotePath,
      (evidence) =>
        evidence.excludedPolicyCount >= 1 &&
        evidence.committedCount === 0 &&
        evidence.pendingCount === 0,
      "controlled note did not retain terminal policy audit evidence",
    );
    recordAutomaticJourneyStage(250);
    await waitForStatusText(
      (statusText) => statusText.includes("Policy blocked"),
      "aggregate status did not report the active policy block",
    );
    recordAutomaticJourneyStage(260);

    const restoredTmpPolicy = await prepareExtensionExclusionRule(
      sessionCookies,
      sessionCsrf,
      ".tmp",
    );
    recordAutomaticJourneyStage(300);
    const restoredTmpPolicyRevision = await publishPreparedPolicy(
      sessionCookies,
      sessionCsrf,
      restoredTmpPolicy,
    );
    recordAutomaticJourneyStage(310);
    if (restoredTmpPolicyRevision <= markdownPolicyRevision) {
      throw new Error("allowing policy revision did not advance");
    }
    const recoveryBaseline = await readServerPublicationEvidence(policyRecoveryDeclaredSha256);
    if (recoveryBaseline.operationCount !== 0) {
      throw new Error("policy-successor publication identity was not unique before authorization");
    }
    await onboardLiveDevice({
      serverOrigin,
      allowedOrigin,
      webUsername,
      passwordFile: passwordFile as string,
      totpHelper: totpHelper as string,
      pluginDataPathSuffix,
      deviceName: "e2e-policy-allowed",
      minimumPolicyRevision: restoredTmpPolicyRevision,
    });
    recordAutomaticJourneyStage(320);
    const recoveredPolicyServerEvidence =
      await waitForAutomaticServerPublicationWithOneSettledEvent(
      policyRecoveryDeclaredSha256,
      policyRecoveryNotePath,
      policyRecoveryNoteContent,
      "automatic policy acceptance did not commit the allowed successor",
    );
    requireExactlyOneCanonicalPublication(
      recoveredPolicyServerEvidence,
      "policy-authorized successor did not produce exactly one canonical publication",
    );
    await waitForJournalEvidence(
      policyRecoveryNotePath,
      (evidence) =>
        evidence.committedCount === 1 &&
        evidence.pendingCount === 0 &&
        evidence.mappedCount === 1 &&
        evidence.excludedPolicyCount >= 1,
      "policy-authorized successor did not persist its terminal local receipt",
    );
    recordAutomaticJourneyStage(330);
    await waitForPolicyRecoveryNoteToRenderSynced();
    recordAutomaticJourneyStage(340);
    await waitForStatusText(
      (statusText) => !statusText.includes("Policy blocked"),
      "aggregate status retained a stale policy block after successor commit",
    );
    recordAutomaticJourneyStage(350);
    recordLivePhase("automatic_policy_successor_committed");

    const data = await readPluginData();
    if (data.pending_grant !== null) {
      throw new Error("final pending grant state was not cleared");
    }

    const irrelevantLocatorPolicy = await prepareIrrelevantFolderExclusionRule(
      sessionCookies,
      sessionCsrf,
    );
    const claimedResumeBaseline = await readServerPublicationEvidence(
      claimedResumeDeclaredSha256,
    );
    if (claimedResumeBaseline.operationCount !== 0) {
      throw new Error("claimed-resume publication identity was not unique before capture");
    }
    const operationObservation = readServerPublicationEvidence(
      claimedResumeDeclaredSha256,
      true,
    );
    await browser.pause(1_000);
    await editFixtureNoteForClaimedResume();
    const observedOperation = await operationObservation;
    if (
      observedOperation.operationCount !== 1 ||
      observedOperation.receivingUnpublishedOperationCount !== 1
    ) {
      throw new Error("server did not observe the controlled receiving operation");
    }
    const changedPolicyRevision = await publishPreparedPolicy(
      sessionCookies,
      sessionCsrf,
      irrelevantLocatorPolicy,
    );
    if (changedPolicyRevision <= restoredTmpPolicyRevision) {
      throw new Error("irrelevant policy revision did not advance");
    }
    await disableKnowledgeWorkspacePlugin();
    const retryableJournalEvidence = await waitForJournalEvidence(
      claimedResumeNotePath,
      (evidence) => evidence.pendingCount === 1,
      "interrupted claimed upload did not remain durable",
    );
    const interruptedOperationIdentity = await readOpaqueJournalOperationIdentity(
      claimedResumeNotePath,
    );
    const interruptedServerEvidence = await readServerPublicationEvidence(
      claimedResumeDeclaredSha256,
    );
    if (
      interruptedServerEvidence.sourceCount !== 0 ||
      interruptedServerEvidence.sourceVersionCount !== 0 ||
      interruptedServerEvidence.syncEventCount !== 0 ||
      interruptedServerEvidence.operationCount !== 1 ||
      interruptedServerEvidence.committedOperationCount !== 0 ||
      interruptedServerEvidence.exactOperationPublicationCount !== 0 ||
      interruptedServerEvidence.receivingUnpublishedOperationCount !== 1
    ) {
      throw new Error("irrelevant policy revision did not interrupt the claimed upload safely");
    }
    await enableKnowledgeWorkspacePlugin();
    const recoveredJournalEvidence = await waitForJournalEvidence(
      claimedResumeNotePath,
      (evidence) =>
        evidence.committedCount === 1 &&
        evidence.pendingCount === 0 &&
        evidence.mappedCount === 1 &&
        evidence.excludedPolicyCount === 0,
      "automatic restart did not settle the exact-token receipt",
    );
    const terminalOperationIdentity = await readOpaqueJournalOperationIdentity(
      claimedResumeNotePath,
    );
    const resumedPublicationEvidence = await readServerPublicationEvidence(
      claimedResumeDeclaredSha256,
    );
    if (
      interruptedOperationIdentity !== terminalOperationIdentity ||
      recoveredJournalEvidence.committedCount !== 1 ||
      recoveredJournalEvidence.mappedCount !== 1
    ) {
      throw new Error("claimed exact-token resume did not preserve one terminal receipt");
    }
    requireExactlyOneCanonicalPublication(
      resumedPublicationEvidence,
      "claimed exact-token resume did not commit exactly once",
    );

    if (livePhaseStatusFile !== undefined) {
      writeLiveAcceptanceDiagnostic(livePhaseStatusFile, {
        existingSourceCount: existingServerEvidence.sourceCount,
        existingSourceVersionCount: existingServerEvidence.sourceVersionCount,
        existingSyncEventCount: existingServerEvidence.syncEventCount,
        existingOperationCount: existingServerEvidence.operationCount,
        newSourceCount: newNoteServerEvidence.sourceCount,
        newSourceVersionCount: newNoteServerEvidence.sourceVersionCount,
        newSyncEventCount: newNoteServerEvidence.syncEventCount,
        newOperationCount: newNoteServerEvidence.operationCount,
        policySuccessorSourceCount: recoveredPolicyServerEvidence.sourceCount,
        policySuccessorVersionCount: recoveredPolicyServerEvidence.sourceVersionCount,
        policySuccessorSyncEventCount: recoveredPolicyServerEvidence.syncEventCount,
        policySuccessorOperationCount: recoveredPolicyServerEvidence.operationCount,
        policyAuditCount: blockedJournalEvidence.excludedPolicyCount,
        policySuccessorCommittedCount:
          recoveredPolicyServerEvidence.exactOperationPublicationCount,
        claimedResumeOperationCount: resumedPublicationEvidence.operationCount,
        claimedResumeCommittedCount: recoveredJournalEvidence.committedCount,
        claimedResumeInterruptedPendingCount: retryableJournalEvidence.pendingCount,
        existingJournalCommittedCount: existingJournalEvidence.committedCount,
        newJournalCommittedCount: newNoteJournalEvidence.committedCount,
      });
    }
    recordLivePhase("automatic_convergence_journey_completed");
  });
});
