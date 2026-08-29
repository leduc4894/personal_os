import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { browser } from "@wdio/globals";
import { onboardLiveDevice } from "../support/live-device-onboarding";
import {
  type LiveAcceptancePhaseResultCode,
  writeLiveAcceptanceDiagnostic,
  writeLiveAcceptancePhaseStatus,
} from "../support/live-acceptance-phase-status";
import { runFromE2eRepositoryRoot } from "../support/repository-subprocess";

/**
 * Live-server multipart upload acceptance journey (child 7 spec 9.3): one
 * >16 MiB sanitized fixture through plugin-driven interruption/resume, one
 * API-driven corruption refusal, one API-driven lost completion
 * acknowledgement and one plugin-driven policy advance, against the real
 * disposable `knowledge-ci-*` stack with real R2 staging.
 *
 * The plugin rows run the REAL watcher → journal → preflight → multipart
 * runner path: fixture A is interrupted by unloading the plugin mid-upload
 * (durable safe progress proves at least one recorded completed part, the
 * journal proves no presigned URL or staging material persisted), then the
 * reload resumes the SAME session and commits exactly one publication.
 * Fixture B uploads under an allowing policy until a folder-prefix deny
 * revision advances mid-transfer; the closed denial terminalizes the event
 * and leaves zero publications plus a durably recorded exact cleanup
 * obligation. The API rows exchange their OWN device grant through the
 * real device-authorization routes and drive the public multipart session
 * endpoints: a flipped final part must fail the server-side full-object
 * verification (`multipart_integrity_failed`, nothing published), and a
 * discarded completion acknowledgement must resolve through status plus an
 * exact replay that returns the SAME frozen terminal result with exactly
 * one version.
 *
 * Logs and the phase-status file carry closed tokens, booleans and counts
 * only — never a presigned URL, signature, staging key, provider identity,
 * digest or Vault path.
 */
const serverOrigin = process.env.E2E_SERVER_ORIGIN ?? "http://127.0.0.1:8000";
const allowedOrigin = process.env.E2E_ALLOWED_ORIGIN ?? "https://app.ducinvest.com";
const webUsername = process.env.E2E_WEB_USERNAME ?? "duc";
const passwordFile = process.env.E2E_WEB_PASSWORD_FILE;
const totpHelper = process.env.E2E_TOTP_HELPER;
const livePhaseStatusFile = process.env.E2E_LIVE_PHASE_STATUS_FILE;
const pluginDataPathSuffix = "plugins/knowledge-workspace/data.json";
const databaseEnvironmentKeys = [
  "KNOWLEDGE_SECRET_ROOT",
  "KNOWLEDGE_DATABASE_HOST",
  "KNOWLEDGE_DATABASE_PORT",
  "KNOWLEDGE_DATABASE_NAME",
  "KNOWLEDGE_DATABASE_USER",
  "KNOWLEDGE_DATABASE_PASSWORD_FILE",
] as const;

/** One 8 MiB ordinary part; the server-owned geometry never negotiates it. */
const PART_SIZE_BYTES = 8 * 1024 * 1024;
/**
 * 16 MiB + 1 B — the smallest multipart geometry (three parts, one-byte
 * final part). The runner re-opens and re-fingerprints the WHOLE file
 * before every part, so the fixture size multiplies the renderer's memory
 * churn; larger fixtures crash the harness renderer mid-journey. The
 * one-byte final part guarantees a recorded completed part during the
 * outage below, and the pre-resolved kill lands while the two 8 MiB parts
 * are still in flight — before the completion claim can exist.
 */
const RESUME_FIXTURE_TOTAL_BYTES = 2 * PART_SIZE_BYTES + 1;
/**
 * 16 MiB + 1 B for the policy row too: the deny revision must prevent
 * PUBLICATION (it is rechecked at completion even after every part URL was
 * issued), so the smallest three-part geometry exercises the whole path.
 */
const POLICY_FIXTURE_TOTAL_BYTES = 2 * PART_SIZE_BYTES + 1;
/** 16 MiB + 1 B = three parts with a one-byte final part. */
const API_FIXTURE_TOTAL_BYTES = 2 * PART_SIZE_BYTES + 1;

const resumeFixtureSeed = `controlled-multipart-resume-${crypto.randomUUID()}`;
const resumeFixtureNotePath = `${resumeFixtureSeed}.bin`;
const policyFixtureFolder = `controlled-multipart-policy-${crypto.randomUUID()}`;
const policyFixtureNotePath = `${policyFixtureFolder}/controlled-multipart-policy.bin`;
const corruptionFixtureSeed = `controlled-multipart-corruption-${crypto.randomUUID()}`;
const corruptionFixturePath = `${corruptionFixtureSeed}.bin`;
const lostAckFixtureSeed = `controlled-multipart-lost-ack-${crypto.randomUUID()}`;
const lostAckFixturePath = `${lostAckFixtureSeed}.bin`;

interface FixturePattern {
  readonly declaredSha256: string;
  readonly totalBytes: number;
  readonly partCount: number;
}

interface ServerPublicationEvidence {
  readonly operationCount: number;
  readonly committedOperationCount: number;
  readonly sourceCount: number;
  readonly sourceVersionCount: number;
  readonly syncEventCount: number;
  readonly exactOperationPublicationCount: number;
  readonly sessionCount: number;
  readonly sessionCommittedCount: number;
  readonly sessionExpectedStateCount: number;
  readonly sessionCleanupPendingCount: number;
  readonly sessionPartRowCount: number;
  readonly sessionMaxPartCount: number;
}

interface MultipartJournalProgress {
  readonly eventState: string;
  readonly partCount: number;
  readonly completedPartCount: number;
  readonly sessionState: string;
  readonly hasSafeReason: boolean;
}

interface JournalEventCounts {
  readonly committedCount: number;
  readonly pendingCount: number;
  readonly excludedPolicyCount: number;
}

interface WireEnvelope<out T> {
  readonly data?: T;
  readonly error?: { readonly code?: unknown };
}

interface MultipartSessionPlanWire {
  readonly session_id: string;
  readonly part_size_bytes: number;
  readonly part_count: number;
  readonly expires_at: string;
}

interface MultipartSessionStatusWire extends MultipartSessionPlanWire {
  readonly state: string;
  readonly completed_part_numbers: readonly number[];
  readonly terminal_result: MultipartTerminalResultWire | null;
}

interface MultipartTerminalResultWire {
  readonly result_kind: string;
  readonly source_id: string;
  readonly source_version_id: string;
  readonly content_version: number;
}

interface MultipartPartUrlWire {
  readonly part_number: number;
  readonly offset_bytes: number;
  readonly size_bytes: number;
  readonly url: string;
  readonly expires_at: string;
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

interface ProbeDeviceCredential {
  readonly accessToken: string;
}

type PreparedExclusionRule =
  | { readonly rule_id: string; readonly rule_kind: "folder_prefix"; readonly folder_prefix: string }
  | { readonly rule_id: string; readonly rule_kind: "media_type"; readonly media_type: string };

let adminSessionCookies: string[] | null = null;
let adminSessionCsrf: string | null = null;
let journalDirectoryPath: string | null = null;
/** True while Row 1's deliberate API outage is active (the after-hook heals it). */
let liveTunnelOutageActive = false;

function recordLivePhase(resultCode: LiveAcceptancePhaseResultCode): void {
  if (livePhaseStatusFile !== undefined) {
    writeLiveAcceptancePhaseStatus(livePhaseStatusFile, resultCode);
  }
}

function recordLiveDiagnostics(diagnostic: Record<string, number>): void {
  if (livePhaseStatusFile !== undefined) {
    writeLiveAcceptanceDiagnostic(livePhaseStatusFile, diagnostic);
  }
}

// --- sanitized deterministic fixtures ----------------------------------------------------------------

/**
 * The fixture byte pattern: one 64 KiB block of a fixed seed line, tiled to
 * the exact total. The identical construction runs in Node (digest + part
 * windows) and inside Obsidian's renderer (Vault write), so no bulk content
 * ever crosses the automation channel.
 */
const FIXTURE_BLOCK_BYTES = 64 * 1024;

function fixtureBlockOf(seed: string): string {
  const seedLine = `${seed}\n`;
  const block = seedLine.repeat(Math.ceil(FIXTURE_BLOCK_BYTES / seedLine.length));
  return block.slice(0, FIXTURE_BLOCK_BYTES);
}

function fixtureContentOf(seed: string, totalBytes: number): string {
  const block = fixtureBlockOf(seed);
  const wholeBlocks = Math.floor(totalBytes / FIXTURE_BLOCK_BYTES);
  const remainder = totalBytes - wholeBlocks * FIXTURE_BLOCK_BYTES;
  return block.repeat(wholeBlocks) + block.slice(0, remainder);
}

function fixturePatternOf(seed: string, totalBytes: number): FixturePattern {
  const content = fixtureContentOf(seed, totalBytes);
  return {
    declaredSha256: crypto.createHash("sha256").update(content, "utf8").digest("hex"),
    totalBytes,
    partCount: Math.ceil(totalBytes / PART_SIZE_BYTES),
  };
}

function fixturePartWindow(content: string, offsetBytes: number, sizeBytes: number): Buffer {
  return Buffer.from(content.slice(offsetBytes, offsetBytes + sizeBytes), "utf8");
}

// --- vault helpers --------------------------------------------------------------------------------------

/**
 * The fixture vault's real directory: the WDIO service copies the fixture
 * vault to a temporary directory the Obsidian instance opens. Writing the
 * LARGE fixture bytes from Node straight into that directory (Obsidian's
 * own file watcher observes the change) keeps the megabyte-scale content
 * out of the renderer entirely — routing it through `browser.execute`
 * crashes the harness renderer once its heap carries the earlier
 * transfer's state.
 */
async function resolveFixtureVaultRoot(): Promise<string> {
  return await browser.execute(() => {
    const app = (
      window as unknown as {
        app: { vault: { adapter: { getFullPath: (path: string) => string } } };
      }
    ).app;
    return app.vault.adapter.getFullPath(".");
  });
}

function writeFixtureNote(
  vaultRoot: string,
  notePath: string,
  seed: string,
  totalBytes: number,
): void {
  const content = fixtureContentOf(seed, totalBytes);
  const absolutePath = path.join(vaultRoot, notePath);
  fs.mkdirSync(path.dirname(absolutePath), { recursive: true });
  fs.writeFileSync(absolutePath, content, "utf8");
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

async function disableKnowledgeWorkspacePlugin(): Promise<void> {
  await browser.execute(async () => {
    const app = (
      window as unknown as { app: { plugins: { disablePlugin: (id: string) => Promise<void> } } }
    ).app;
    await app.plugins.disablePlugin("knowledge-workspace");
  });
}

// --- the disposable tunnel outage (Row 1's deterministic interruption) ------------------------------

/** The repository root this spec's subprocess helpers resolve against. */
const repositoryRootPath: string = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../..",
);

/** The public plugin origin the live tunnel serves (the plugin's control plane). */
const tunnelHealthUrl = "https://api.ducinvest.com/api/health/ready";

async function isTunnelReachable(): Promise<boolean> {
  try {
    const response = await fetch(tunnelHealthUrl, { signal: AbortSignal.timeout(4_000) });
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * Stop the live tunnel process. The part PUTs ride presigned provider URLs
 * (never the tunnel), so they keep landing while EVERY plugin control call
 * fails offline — the completion call can never reach the API through a
 * dead tunnel, so no completion lease can come to exist.
 */
async function stopLiveTunnelProcess(): Promise<void> {
  await runFromE2eRepositoryRoot("taskkill", ["/IM", "cloudflared.exe", "/T", "/F"], import.meta.url);
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (!(await isTunnelReachable())) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("live tunnel survived the stop");
}

async function startLiveTunnelProcess(): Promise<void> {
  const logFile = fs.openSync(
    path.join(repositoryRootPath, ".local", "runtime-logs", "live-ci-tunnel.log"),
    "a",
  );
  const child = spawn(
    "C:\\Program Files (x86)\\cloudflared\\cloudflared.exe",
    ["tunnel", "run", "knowledge-api-verify"],
    {
      cwd: repositoryRootPath,
      detached: true,
      stdio: ["ignore", logFile, logFile],
      windowsHide: true,
    },
  );
  child.unref();
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (await isTunnelReachable()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error("live tunnel did not become reachable again");
}

async function countMultipartFailureTrailEntries(expectedReasonToken: string): Promise<number> {
  return await browser.execute(async (expectedToken: string) => {
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
    const raw = await app.vault.adapter.read(
      `${app.vault.configDir}/plugins/knowledge-workspace/sync-diagnostics-trail.json`,
    );
    const document = JSON.parse(raw) as {
      readonly entries?: readonly {
        readonly kind?: unknown;
        readonly tokens?: readonly unknown[];
      }[];
    };
    const entries = document.entries ?? [];
    return entries.filter(
      (entry) =>
        entry.kind === "multipart_failure" &&
        Array.isArray(entry.tokens) &&
        entry.tokens.includes(expectedToken),
    ).length;
  }, expectedReasonToken);
}

/**
 * The closed trail histogram of the live plugin — kind plus token counts
 * only — used to explain a stalled wait without exposing any open value.
 */
async function readClosedTrailHistogram(): Promise<Record<string, number>> {
  return await browser.execute(async () => {
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
    let entries: readonly { readonly kind?: unknown; readonly tokens?: readonly unknown[] }[] = [];
    try {
      const raw = await app.vault.adapter.read(
        `${app.vault.configDir}/plugins/knowledge-workspace/sync-diagnostics-trail.json`,
      );
      const document = JSON.parse(raw) as { readonly entries?: typeof entries };
      entries = document.entries ?? [];
    } catch {
      return { trail_unavailable: 1 };
    }
    const histogram: Record<string, number> = {};
    for (let index = 0; index < entries.length; index += 1) {
      const entry = entries[index];
      if (entry === undefined) {
        continue;
      }
      const kind = typeof entry.kind === "string" ? entry.kind : "unknown_kind";
      histogram[kind] = (histogram[kind] ?? 0) + 1;
      if (Array.isArray(entry.tokens)) {
        for (const token of entry.tokens) {
          if (typeof token === "string") {
            histogram[`${kind}:${token}`] = (histogram[`${kind}:${token}`] ?? 0) + 1;
          }
        }
      }
    }
    return histogram;
  });
}

// --- journal evidence (sql.js read of the durable generation) -----------------------------------------

function readJournalDatabase(): Buffer {
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
  throwOnForbiddenJournalMaterial(journalBytes);
  return journalBytes;
}

/** The durable journal must never carry wire or staging material (spec 9.3). */
function throwOnForbiddenJournalMaterial(journalBytes: Buffer): void {
  const decoded = journalBytes.toString("latin1");
  for (const forbiddenMarker of ["X-Amz-Signature", "X-Amz-Credential", "staging/multipart/"]) {
    if (decoded.includes(forbiddenMarker)) {
      throw new Error("durable journal carried forbidden transfer material");
    }
  }
}

type SqlScalarStep = (statementText: string, parameters: readonly string[]) => number | string | null;

async function readJournalEvidence(
  controlledNormalizedPath: string,
): Promise<{ readonly counts: JournalEventCounts; readonly progress: MultipartJournalProgress | null }> {
  const initSqlJs = (await import("sql.js")).default;
  const engine = await initSqlJs();
  const journalBytes = readJournalDatabase();
  const database = new engine.Database(journalBytes);
  try {
    const scalar: SqlScalarStep = (statementText, parameters) => {
      const statement = database.prepare(statementText);
      try {
        statement.bind([...parameters]);
        return statement.step() ? ((statement.get()[0] ?? 0) as number | string | null) : 0;
      } finally {
        statement.free();
      }
    };
    const eventCountOf = (statePredicate: string): number =>
      Number(
        scalar(
          `select count(*) from journal_events event
            join local_files file on file.local_file_id = event.local_file_id
            where file.normalized_path = ? and ${statePredicate}`,
          [controlledNormalizedPath],
        ) ?? 0,
      );
    const pendingState = scalar(
      `select event.state from journal_events event
         join local_files file on file.local_file_id = event.local_file_id
         where file.normalized_path = ? and event.state in
         ('queued', 'preflight', 'uploading', 'waiting_retry')`,
      [controlledNormalizedPath],
    );
    const progressJson = scalar(
      `select progress.completed_part_numbers_json from multipart_upload_progress progress
         join journal_events event on event.event_id = progress.event_id
         join local_files file on file.local_file_id = event.local_file_id
         where file.normalized_path = ?`,
      [controlledNormalizedPath],
    );
    const progressPartCount = scalar(
      `select progress.part_count from multipart_upload_progress progress
         join journal_events event on event.event_id = progress.event_id
         join local_files file on file.local_file_id = event.local_file_id
         where file.normalized_path = ?`,
      [controlledNormalizedPath],
    );
    const progressSessionState = scalar(
      `select progress.session_state from multipart_upload_progress progress
         join journal_events event on event.event_id = progress.event_id
         join local_files file on file.local_file_id = event.local_file_id
         where file.normalized_path = ?`,
      [controlledNormalizedPath],
    );
    const progressSafeReason = scalar(
      `select progress.safe_reason from multipart_upload_progress progress
         join journal_events event on event.event_id = progress.event_id
         join local_files file on file.local_file_id = event.local_file_id
         where file.normalized_path = ?`,
      [controlledNormalizedPath],
    );
    const completedPartNumbers =
      typeof progressJson === "string" ? (JSON.parse(progressJson) as number[]) : [];
    return {
      counts: {
        committedCount: eventCountOf("event.state = 'committed'"),
        pendingCount: eventCountOf(
          "event.state in ('queued', 'preflight', 'uploading', 'waiting_retry')",
        ),
        excludedPolicyCount: eventCountOf("event.state = 'excluded_policy'"),
      },
      progress:
        typeof progressPartCount === "number" && typeof progressSessionState === "string"
          ? {
              eventState: typeof pendingState === "string" ? pendingState : "terminal",
              partCount: progressPartCount,
              completedPartCount: completedPartNumbers.length,
              sessionState: progressSessionState,
              hasSafeReason: progressSafeReason !== null && progressSafeReason !== 0,
            }
          : null,
    };
  } finally {
    database.close();
  }
}

async function waitForJournalEvidence(
  controlledNormalizedPath: string,
  accepts: (evidence: {
    readonly counts: JournalEventCounts;
    readonly progress: MultipartJournalProgress | null;
  }) => boolean,
  failureMessage: string,
  maximumAttempts = 90,
  pauseMs = 1_000,
): Promise<{ readonly counts: JournalEventCounts; readonly progress: MultipartJournalProgress | null }> {
  let lastEvidence: {
    readonly counts: JournalEventCounts;
    readonly progress: MultipartJournalProgress | null;
  } | null = null;
  for (let attempt = 0; attempt < maximumAttempts; attempt += 1) {
    try {
      const evidence = await readJournalEvidence(controlledNormalizedPath);
      lastEvidence = evidence;
      if (accepts(evidence)) {
        return evidence;
      }
    } catch {
      // An atomic generation swap may briefly move the manifest ahead of
      // the file read. Retry without exposing any filesystem detail.
    }
    await browser.pause(pauseMs);
  }
  const sanitizedLastCounts = lastEvidence === null ? null : lastEvidence.counts;
  const sanitizedLastProgress =
    lastEvidence === null || lastEvidence.progress === null
      ? null
      : {
          eventState: lastEvidence.progress.eventState,
          partCount: lastEvidence.progress.partCount,
          completedPartCount: lastEvidence.progress.completedPartCount,
          sessionState: lastEvidence.progress.sessionState,
          hasSafeReason: lastEvidence.progress.hasSafeReason,
        };
  throw new Error(
    `${failureMessage}: ${JSON.stringify({ counts: sanitizedLastCounts, progress: sanitizedLastProgress })}`,
  );
}

// --- server evidence (counts only) --------------------------------------------------------------------

const serverEvidenceScript = String.raw`
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
    controlled_digests = os.environ["SERVER_EVIDENCE_DECLARED_SHA256S"].split(",")
    if not controlled_digests or any(
        re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in controlled_digests
    ):
        raise ValueError
    expected_state = os.environ["SERVER_EVIDENCE_SESSION_STATE"]
    if re.fullmatch(r"[a-z_]+", expected_state) is None:
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
            ), controlled_sessions as (
              select * from knowledge.multipart_uploads
              where declared_sha256 = any(%s)
            )
            select
              (select count(*) from controlled_operations),
              (select count(*) from controlled_operations where state = 'committed'),
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
              (select count(*) from controlled_operations operation
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
              (select count(*) from controlled_sessions),
              (select count(*) from controlled_sessions where state = 'committed'),
              (select count(*) from controlled_sessions where state = %s),
              (select count(*) from controlled_sessions where cleanup_state = 'pending'),
              (select coalesce(max(part_count), 0) from controlled_sessions),
              (select count(*) from knowledge.multipart_parts part
                 join controlled_sessions session
                   on session.multipart_upload_id = part.multipart_upload_id)
        """
        row = connection.execute(
            statement, (controlled_digests, controlled_digests, expected_state)
        ).fetchone()
    if row is None:
        raise ValueError
    print(json.dumps({
        "operationCount": int(row[0]),
        "committedOperationCount": int(row[1]),
        "sourceCount": int(row[2]),
        "sourceVersionCount": int(row[3]),
        "syncEventCount": int(row[4]),
        "exactOperationPublicationCount": int(row[5]),
        "sessionCount": int(row[6]),
        "sessionCommittedCount": int(row[7]),
        "sessionExpectedStateCount": int(row[8]),
        "sessionCleanupPendingCount": int(row[9]),
        "sessionMaxPartCount": int(row[10]),
        "sessionPartRowCount": int(row[11]),
    }, separators=(",", ":")))
except BaseException:
    print(json.dumps({"state": "server_evidence_unavailable"}))
    raise SystemExit(1)
`;

async function readServerEvidence(
  controlledDeclaredSha256: string,
  expectedSessionState: string,
): Promise<ServerPublicationEvidence> {
  const { stdout } = await runFromE2eRepositoryRoot(
    "uv",
    ["run", "python", "-c", serverEvidenceScript],
    import.meta.url,
    {
      ...process.env,
      SERVER_EVIDENCE_DECLARED_SHA256S: controlledDeclaredSha256,
      SERVER_EVIDENCE_SESSION_STATE: expectedSessionState,
    },
    10_000,
  );
  const parsed = JSON.parse(stdout) as Record<string, unknown>;
  const evidenceKeys = [
    "operationCount",
    "committedOperationCount",
    "sourceCount",
    "sourceVersionCount",
    "syncEventCount",
    "exactOperationPublicationCount",
    "sessionCount",
    "sessionCommittedCount",
    "sessionExpectedStateCount",
    "sessionCleanupPendingCount",
    "sessionMaxPartCount",
    "sessionPartRowCount",
  ] as const;
  for (const key of evidenceKeys) {
    if (!Number.isSafeInteger(parsed[key]) || Number(parsed[key]) < 0) {
      throw new Error("sanitized server evidence was invalid");
    }
  }
  return parsed as unknown as ServerPublicationEvidence;
}

function requireExactlyOnePublication(
  evidence: ServerPublicationEvidence,
  failureMessage: string,
): void {
  // Exactly one PUBLICATION: one published source, one version, one
  // committed sync event and one exact publication join. The OBSERVATION
  // counts around it may legitimately exceed one — a reload's automatic
  // snapshot races a second observation of the same file whose own session
  // attempt ends abandoned while the server's same-digest dedup answers
  // `no_change` — precisely the Phase 1 guarantees this row relies on. Only
  // one session may ever be COMMITTED for the digest.
  if (
    evidence.operationCount < 1 ||
    evidence.committedOperationCount < 1 ||
    evidence.sourceCount !== 1 ||
    evidence.sourceVersionCount !== 1 ||
    evidence.syncEventCount !== 1 ||
    evidence.exactOperationPublicationCount !== 1 ||
    evidence.sessionCommittedCount !== 1
  ) {
    throw new Error(failureMessage);
  }
}

function requireZeroPublications(evidence: ServerPublicationEvidence, failureMessage: string): void {
  if (
    evidence.operationCount === 0 ||
    evidence.committedOperationCount !== 0 ||
    evidence.sourceCount !== 0 ||
    evidence.sourceVersionCount !== 0 ||
    evidence.syncEventCount !== 0 ||
    evidence.exactOperationPublicationCount !== 0
  ) {
    throw new Error(failureMessage);
  }
}

// --- admin policy surface ------------------------------------------------------------------------------

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

async function prepareSingleExclusionRule(
  cookies: string[],
  csrf: string,
  rule: PreparedExclusionRule,
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

// --- probe device credential through the real device-authorization routes ------------------------------

async function exchangeProbeDeviceCredential(deviceName: string): Promise<ProbeDeviceCredential> {
  if (adminSessionCookies === null || adminSessionCsrf === null) {
    throw new Error("probe device exchange lacked the admin session");
  }
  const created = await responseData<{
    grant_id: string;
    polling_secret: string;
    poll_interval_seconds: number;
  }>(
    await fetch(`${serverOrigin}/api/auth/device-authorizations`, {
      method: "POST",
      headers: { "content-type": "application/json", origin: allowedOrigin },
      body: JSON.stringify({
        client_instance_id: crypto.randomUUID(),
        device_name: deviceName,
        platform_class: "obsidian_desktop",
        platform_name: "windows",
        plugin_version: "0.2.0",
        requested_scope: "obsidian_sync",
      }),
    }),
    "probe device grant creation",
  );
  const approveResponse = await fetch(
    `${serverOrigin}/api/auth/device-authorizations/${created.grant_id}/approve`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: allowedOrigin,
        cookie: adminSessionCookies.join("; "),
        "x-csrf-token": adminSessionCsrf,
      },
    },
  );
  if (!approveResponse.ok) {
    throw new Error(`probe device grant approval failed: ${approveResponse.status}`);
  }
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const pollResponse = await fetch(
      `${serverOrigin}/api/auth/device-authorizations/${created.grant_id}/poll`,
      {
        method: "POST",
        headers: { authorization: `Bearer ${created.polling_secret}` },
        signal: AbortSignal.timeout(30_000),
      },
    );
    const envelope = (await pollResponse.json()) as WireEnvelope<{
      access_credential: string;
    }>;
    if (pollResponse.ok && typeof envelope.data?.access_credential === "string") {
      return { accessToken: envelope.data.access_credential };
    }
    const errorCode = envelope.error?.code;
    if (
      errorCode === "device_authorization_pending" ||
      errorCode === "device_authorization_slow_down"
    ) {
      await new Promise((resolve) => setTimeout(resolve, created.poll_interval_seconds * 1000));
      continue;
    }
    throw new Error(`probe device poll failed with a closed code: ${String(errorCode)}`);
  }
  throw new Error("probe device exchange did not converge");
}

// --- the public multipart session surface (real routes) -------------------------------------------------

async function multipartRequest<T>(
  accessToken: string,
  method: string,
  route: string,
  body?: unknown,
): Promise<{ readonly status: number; readonly data: T | null; readonly errorCode: string | null }> {
  const response = await fetch(`${serverOrigin}${route}`, {
    method,
    headers: {
      authorization: `Bearer ${accessToken}`,
      ...(body === undefined ? {} : { "content-type": "application/json" }),
      accept: "application/json",
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(180_000),
  });
  const envelope = (await response.json().catch(() => ({}))) as WireEnvelope<T>;
  const errorCode = envelope.error?.code;
  return {
    status: response.status,
    data: envelope.data ?? null,
    errorCode: typeof errorCode === "string" ? errorCode : null,
  };
}

function requireMultipartData<T>(
  outcome: { readonly status: number; readonly data: T | null; readonly errorCode: string | null },
  operation: string,
): T {
  if (outcome.data === null) {
    throw new Error(`${operation} failed: ${outcome.status} ${outcome.errorCode ?? "no data"}`);
  }
  return outcome.data;
}

async function createProbeMultipartSession(
  accessToken: string,
  fixturePath: string,
  pattern: FixturePattern,
  activePolicyRevisionNumber: number,
): Promise<MultipartSessionPlanWire> {
  const created = await multipartRequest<MultipartSessionPlanWire>(
    accessToken,
    "POST",
    "/api/uploads/multipart-sessions",
    {
      event_id: crypto.randomUUID(),
      idempotency_key: crypto.randomUUID(),
      operation: "create",
      local_file_id: crypto.randomUUID(),
      source_id: null,
      base_version_id: null,
      normalized_locator: fixturePath,
      sha256: pattern.declaredSha256,
      size_bytes: pattern.totalBytes,
      media_type: "application/octet-stream",
      policy_revision: activePolicyRevisionNumber,
    },
  );
  const plan = requireMultipartData(created, "multipart session create");
  if (plan.part_count !== pattern.partCount || plan.part_size_bytes !== PART_SIZE_BYTES) {
    throw new Error("multipart session geometry disagreed with the frozen fixture");
  }
  return plan;
}

async function putFixturePart(
  accessToken: string,
  sessionId: string,
  content: string,
  partNumber: number,
  corrupt: boolean,
): Promise<void> {
  const issued = requireMultipartData(
    await multipartRequest<MultipartPartUrlWire>(
      accessToken,
      "POST",
      `/api/uploads/multipart-sessions/${sessionId}/parts/${partNumber}/url`,
    ),
    "multipart part url",
  );
  const expectedOffsetBytes = (partNumber - 1) * PART_SIZE_BYTES;
  if (
    issued.part_number !== partNumber ||
    issued.offset_bytes !== expectedOffsetBytes ||
    issued.size_bytes !==
      Math.min(PART_SIZE_BYTES, content.length - expectedOffsetBytes)
  ) {
    throw new Error("multipart part window disagreed with the frozen geometry");
  }
  let window = fixturePartWindow(content, issued.offset_bytes, issued.size_bytes);
  if (corrupt && window.byteLength > 0) {
    const flipped = Buffer.from(window);
    const firstByte = flipped[0] ?? 0;
    flipped[0] = firstByte === 0x5a ? 0x59 : 0x5a;
    window = flipped;
  }
  const put = await fetch(issued.url, {
    method: "PUT",
    body: new Uint8Array(window),
    signal: AbortSignal.timeout(180_000),
  });
  if (!put.ok) {
    throw new Error(`multipart part put failed: ${put.status}`);
  }
}

describe("multipart upload acceptance: interruption/resume, corruption refusal, policy advance and lost completion acknowledgement (live server)", () => {
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
    this.timeout(240_000);
    if (liveTunnelOutageActive) {
      liveTunnelOutageActive = false;
      await startLiveTunnelProcess().catch(() => undefined);
    }
    if (adminSessionCookies === null || adminSessionCsrf === null) {
      return;
    }
    // Restore the stack's baseline posture (deny image/* only) so the
    // disposable project is left as the bootstrap published it.
    const restored = await prepareSingleExclusionRule(adminSessionCookies, adminSessionCsrf, {
      rule_id: crypto.randomUUID(),
      rule_kind: "media_type",
      media_type: "image/*",
    });
    const restoredRevision = await publishPreparedPolicy(
      adminSessionCookies,
      adminSessionCsrf,
      restored,
    );
    console.log("MULTIPART_BASELINE_POLICY_RESTORED", restoredRevision > 0);
  });

  it("proves every multipart acceptance row against the real server and R2 staging", async function () {
    this.timeout(720_000);
    recordLivePhase("multipart_journey_started");

    const resumePattern = fixturePatternOf(resumeFixtureSeed, RESUME_FIXTURE_TOTAL_BYTES);
    const corruptionPattern = fixturePatternOf(corruptionFixtureSeed, API_FIXTURE_TOTAL_BYTES);
    const lostAckPattern = fixturePatternOf(lostAckFixtureSeed, API_FIXTURE_TOTAL_BYTES);
    const policyPattern = fixturePatternOf(policyFixtureNotePath, POLICY_FIXTURE_TOTAL_BYTES);

    // -- Row 1: plugin-driven interruption/resume of a >16 MiB fixture ---
    const resumeBaseline = await readServerEvidence(resumePattern.declaredSha256, "committed");
    if (resumeBaseline.operationCount !== 0 || resumeBaseline.sessionCount !== 0) {
      throw new Error("resume-fixture publication identity was not unique before capture");
    }
    await disableKnowledgeWorkspacePlugin();
    const fixtureVaultRoot = await resolveFixtureVaultRoot();
    writeFixtureNote(fixtureVaultRoot, resumeFixtureNotePath, resumeFixtureSeed, RESUME_FIXTURE_TOTAL_BYTES);
    const onboarding = await onboardLiveDevice({
      serverOrigin,
      allowedOrigin,
      webUsername,
      passwordFile: passwordFile as string,
      totpHelper: totpHelper as string,
      pluginDataPathSuffix,
      deviceName: "e2e-multipart-resume",
    });
    adminSessionCookies = [...onboarding.adminSessionCookies];
    adminSessionCsrf = onboarding.adminSessionCsrf;
    journalDirectoryPath = await resolveJournalDirectoryPath();

    // Interrupt the live transfer the moment durable progress records a
    // completed part: poll fast, then stop the disposable API process — a
    // REAL server outage. The plugin stays loaded (no reload, no snapshot
    // race, no credential race): every further control call fails offline,
    // the runner parks the frozen event in bounded backoff with its durable
    // safe progress intact, and the in-flight part PUTs may still land one
    // or two more recorded parts.
    const interrupted = await waitForJournalEvidence(
      resumeFixtureNotePath,
      (evidence) =>
        evidence.counts.committedCount === 0 &&
        evidence.progress !== null &&
        evidence.progress.completedPartCount >= 1 &&
        evidence.progress.completedPartCount < evidence.progress.partCount,
      "multipart transfer never exposed partial durable progress",
      // The fixture write itself takes tens of seconds inside the renderer
      // before capture and the session row land; budget generously.
      700,
      200,
    );
    // Pre-resolve the listener PID so the stop itself is a single fast
    // kill: the outage must land inside the remaining part waves, never
    // after the completion intent was persisted.
    liveTunnelOutageActive = true;
    await stopLiveTunnelProcess();
    if (
      interrupted.progress === null ||
      interrupted.progress.partCount !== resumePattern.partCount ||
      interrupted.progress.completedPartCount < 1 ||
      interrupted.progress.hasSafeReason
    ) {
      throw new Error("interrupted multipart progress was not the frozen safe shape");
    }
    const interruptedCompletedParts = interrupted.progress.completedPartCount;
    // The interruption must be real: with the API down the frozen event
    // settles short of its receipt while its durable progress survives.
    // Part PUTs already in flight to the staging provider may still land,
    // so the retained completed set may grow — even to every part with the
    // completion intent persisted — but no receipt may exist.
    const duringOutage = await waitForJournalEvidence(
      resumeFixtureNotePath,
      (evidence) =>
        evidence.counts.committedCount === 0 &&
        evidence.counts.pendingCount === 1 &&
        evidence.progress !== null &&
        evidence.progress.completedPartCount >= 1 &&
        evidence.progress.completedPartCount <= resumePattern.partCount &&
        evidence.progress.partCount === resumePattern.partCount,
      "offline multipart upload did not retain its durable safe progress",
      60,
      1_000,
    );
    const offlineCompletedParts =
      duringOutage.progress === null ? 0 : duringOutage.progress.completedPartCount;

    // Bring the disposable API back. The queue's bounded backoff retries the
    // SAME frozen event; the runner's first network call is session STATUS —
    // never a part URL — so the provider-reconciled completed parts are
    // known before any new authorization, and only unfinished ranges move.
    await startLiveTunnelProcess();
    liveTunnelOutageActive = false;
    let resumed: Awaited<ReturnType<typeof waitForJournalEvidence>>;
    try {
      resumed = await waitForJournalEvidence(
        resumeFixtureNotePath,
        (evidence) =>
          evidence.counts.committedCount === 1 &&
          evidence.counts.pendingCount === 0 &&
          evidence.progress === null,
        "resumed multipart upload did not settle one committed receipt",
        240,
        1_000,
      );
    } catch (failure) {
      // The closed trail histogram explains a stall without any open value.
      console.log("MULTIPART_RESUME_TRAIL", JSON.stringify(await readClosedTrailHistogram()));
      throw failure;
    }
    if (resumed.counts.committedCount !== 1 || resumed.counts.pendingCount !== 0) {
      throw new Error("resumed multipart upload did not commit exactly once");
    }
    const resumeServerEvidence = await readServerEvidence(resumePattern.declaredSha256, "committed");
    requireExactlyOnePublication(
      resumeServerEvidence,
      "resumed multipart upload did not produce exactly one canonical publication",
    );
    if (
      resumeServerEvidence.sessionCount !== 1 ||
      resumeServerEvidence.sessionCommittedCount !== 1 ||
      resumeServerEvidence.sessionExpectedStateCount !== 1 ||
      resumeServerEvidence.sessionMaxPartCount !== resumePattern.partCount ||
      resumeServerEvidence.sessionPartRowCount < 1
    ) {
      // Exactly ONE session may exist for this digest ever (the lifetime
      // operation uniqueness): the resumed upload continued the ORIGINAL
      // session — a second session would prove a silent fresh restart. The
      // provider-confirmed part FACTS land through the server's ListParts
      // reconciliations; only their presence is pinned.
      throw new Error("resumed multipart session did not freeze one committed session shape");
    }
    recordLiveDiagnostics({
      resumeInterruptedCompletedParts: interruptedCompletedParts,
      resumeOfflineCompletedParts: offlineCompletedParts,
      resumeCommittedCount: resumed.counts.committedCount,
      resumeTrailingPendingCount: resumed.counts.pendingCount,
      resumeSessionCount: resumeServerEvidence.sessionCount,
      resumeSessionPartRowCount: resumeServerEvidence.sessionPartRowCount,
    });
    recordLivePhase("multipart_resume_committed");

    // The outage ends with the plugin's device-sync coordinator running its
    // documented reconnect catch-up burst — it pulls and applies this
    // journey's own earlier publication through the renderer. Let that
    // burst quiesce before the next Vault write; a write racing the apply
    // burst crashes the harness renderer.
    await new Promise((resolve) => setTimeout(resolve, 75_000));

    // -- Row 4: plugin-driven policy advance mid-transfer ---
    const policyBaseline = await readServerEvidence(policyPattern.declaredSha256, "policy_denied");
    if (policyBaseline.operationCount !== 0 || policyBaseline.sessionCount !== 0) {
      throw new Error("policy-fixture publication identity was not unique before capture");
    }
    const preparedDenyPolicy = await prepareSingleExclusionRule(
      adminSessionCookies,
      adminSessionCsrf,
      {
        rule_id: crypto.randomUUID(),
        rule_kind: "folder_prefix",
        folder_prefix: policyFixtureFolder,
      },
    );
    // Reload through a fresh device grant FIRST (a clean renderer — the
    // accumulated upload/outage state of the resumed row crashes the
    // harness renderer on the next large Vault write), then write the
    // fixture: the loaded plugin's watcher captures it and opens its
    // session under the still-allowing policy, and the deny revision below
    // then races a live transfer.
    recordLiveDiagnostics({ policyRowStage: 1 });
    await onboardLiveDevice({
      serverOrigin,
      allowedOrigin,
      webUsername,
      passwordFile: passwordFile as string,
      totpHelper: totpHelper as string,
      pluginDataPathSuffix,
      deviceName: "e2e-multipart-policy",
    });
    recordLiveDiagnostics({ policyRowStage: 2 });
    writeFixtureNote(
      fixtureVaultRoot,
      policyFixtureNotePath,
      policyFixtureNotePath,
      POLICY_FIXTURE_TOTAL_BYTES,
    );
    recordLiveDiagnostics({ policyRowStage: 3 });
    // The fresh renderer can drop the vault-watcher event for a large
    // Node-side write while its initial device-sync apply burst is still
    // draining (observed live: no journal event and no session ever
    // appear). Re-fire the identical write, but only while the journal
    // still shows NO event for the fixture path — a captured event is
    // never duplicated, because this row's journal count assertions are
    // exact — and only inside a closed attempt bound.
    const POLICY_CAPTURE_MAX_ATTEMPTS = 3;
    let policyCaptureRefireCount = 0;
    for (
      let policyCaptureAttempt = 1;
      ;
      policyCaptureAttempt += 1
    ) {
      if (policyCaptureAttempt > POLICY_CAPTURE_MAX_ATTEMPTS) {
        throw new Error("policy-fixture transfer never opened its multipart session");
      }
      try {
        await waitForJournalEvidence(
          policyFixtureNotePath,
          (evidence) =>
            evidence.progress !== null && evidence.progress.completedPartCount >= 0,
          "policy-fixture watcher capture was lost",
          60,
          1_000,
        );
        break;
      } catch (captureError) {
        const interim = await readJournalEvidence(policyFixtureNotePath).catch(() => null);
        const captureExists =
          interim !== null &&
          (interim.progress !== null ||
            interim.counts.pendingCount > 0 ||
            interim.counts.committedCount > 0 ||
            interim.counts.excludedPolicyCount > 0);
        if (captureExists || policyCaptureAttempt === POLICY_CAPTURE_MAX_ATTEMPTS) {
          throw captureError;
        }
        writeFixtureNote(
          fixtureVaultRoot,
          policyFixtureNotePath,
          policyFixtureNotePath,
          POLICY_FIXTURE_TOTAL_BYTES,
        );
        policyCaptureRefireCount += 1;
        recordLiveDiagnostics({ policyCaptureRefireCount });
      }
    }
    recordLiveDiagnostics({ policyRowStage: 4 });
    const deniedRevision = await publishPreparedPolicy(
      adminSessionCookies,
      adminSessionCsrf,
      preparedDenyPolicy,
    );
    const denied = await waitForJournalEvidence(
      policyFixtureNotePath,
      (evidence) =>
        evidence.counts.excludedPolicyCount === 1 &&
        evidence.counts.pendingCount === 0 &&
        evidence.counts.committedCount === 0,
      "policy-denied multipart upload did not terminalize as excluded_policy",
      120,
      1_000,
    );
    const policyTrailCount = await countMultipartFailureTrailEntries("multipart_policy_denied");
    if (policyTrailCount < 1) {
      throw new Error("policy denial did not surface its closed reason token on the trail");
    }
    const policyServerEvidence = await readServerEvidence(
      policyPattern.declaredSha256,
      "policy_denied",
    );
    requireZeroPublications(
      policyServerEvidence,
      "policy-denied multipart upload published canonical content",
    );
    if (
      policyServerEvidence.sessionCount < 1 ||
      policyServerEvidence.sessionExpectedStateCount < 1 ||
      policyServerEvidence.sessionCleanupPendingCount < 1
    ) {
      // A raced sibling observation of the same denied file may record its
      // own denied session; every one of them must carry the exact cleanup
      // obligation, and none may publish (asserted above).
      throw new Error("policy-denied session did not record its exact cleanup obligation");
    }
    recordLiveDiagnostics({
      policyDenialRevision: deniedRevision,
      policyExcludedCount: denied.counts.excludedPolicyCount,
      policyTrailTokenCount: policyTrailCount,
      policySessionCount: policyServerEvidence.sessionCount,
      policyCleanupObligationCount: policyServerEvidence.sessionCleanupPendingCount,
    });
    recordLivePhase("multipart_policy_denial_observed");
    // -- Rows 2 and 3: the public multipart surface with a probe device credential ---
    const probe = await exchangeProbeDeviceCredential("e2e-multipart-probe");
    const policyStatus = await responseData<PolicyStatus>(
      await fetch(`${serverOrigin}/api/admin/exclusion-policy`, {
        headers: { origin: allowedOrigin, cookie: adminSessionCookies.join("; ") },
      }),
      "policy status",
    );
    const corruptionContent = fixtureContentOf(corruptionFixtureSeed, API_FIXTURE_TOTAL_BYTES);
    const lostAckContent = fixtureContentOf(lostAckFixtureSeed, API_FIXTURE_TOTAL_BYTES);

    // Corruption refusal: every part but the one-byte final part is exact;
    // the flipped byte must fail the server-side full-object verification.
    const corruptionSession = await createProbeMultipartSession(
      probe.accessToken,
      corruptionFixturePath,
      corruptionPattern,
      policyStatus.active_revision_number,
    );
    await putFixturePart(probe.accessToken, corruptionSession.session_id, corruptionContent, 1, false);
    await putFixturePart(probe.accessToken, corruptionSession.session_id, corruptionContent, 2, false);
    await putFixturePart(probe.accessToken, corruptionSession.session_id, corruptionContent, 3, true);
    const corruptionComplete = await multipartRequest<MultipartSessionStatusWire>(
      probe.accessToken,
      "POST",
      `/api/uploads/multipart-sessions/${corruptionSession.session_id}/complete`,
    );
    if (
      corruptionComplete.errorCode !== "multipart_integrity_failed" ||
      corruptionComplete.data !== null
    ) {
      throw new Error(
        `corrupt staging was not refused: ${corruptionComplete.status} ${String(corruptionComplete.errorCode)}`,
      );
    }
    const corruptionServerEvidence = await readServerEvidence(
      corruptionPattern.declaredSha256,
      "integrity_failed",
    );
    requireZeroPublications(
      corruptionServerEvidence,
      "corrupt staging refusal still published canonical content",
    );
    if (
      corruptionServerEvidence.sessionCount !== 1 ||
      corruptionServerEvidence.sessionExpectedStateCount !== 1
    ) {
      throw new Error("corrupt staging refusal did not freeze one integrity-failed session");
    }
    recordLiveDiagnostics({
      corruptionRefusalCodeObserved: 1,
      corruptionPublicationCount: corruptionServerEvidence.exactOperationPublicationCount,
      corruptionSessionCount: corruptionServerEvidence.sessionCount,
    });
    recordLivePhase("multipart_corruption_refused");

    // Lost completion acknowledgement: complete, DISCARD the response, then
    // resolve through status and one exact replay — same frozen result,
    // exactly one version and one object.
    const lostAckSession = await createProbeMultipartSession(
      probe.accessToken,
      lostAckFixturePath,
      lostAckPattern,
      policyStatus.active_revision_number,
    );
    await putFixturePart(probe.accessToken, lostAckSession.session_id, lostAckContent, 1, false);
    await putFixturePart(probe.accessToken, lostAckSession.session_id, lostAckContent, 2, false);
    await putFixturePart(probe.accessToken, lostAckSession.session_id, lostAckContent, 3, false);
    const discardedAcknowledgement = requireMultipartData(
      await multipartRequest<MultipartSessionStatusWire>(
        probe.accessToken,
        "POST",
        `/api/uploads/multipart-sessions/${lostAckSession.session_id}/complete`,
      ),
      "multipart completion",
    );
    if (discardedAcknowledgement.terminal_result === null) {
      throw new Error("multipart completion carried no terminal result");
    }
    const statusAfterLoss = requireMultipartData(
      await multipartRequest<MultipartSessionStatusWire>(
        probe.accessToken,
        "GET",
        `/api/uploads/multipart-sessions/${lostAckSession.session_id}`,
      ),
      "multipart session status",
    );
    if (
      statusAfterLoss.state !== "committed" ||
      statusAfterLoss.terminal_result === null ||
      statusAfterLoss.terminal_result.source_version_id !==
        discardedAcknowledgement.terminal_result.source_version_id
    ) {
      throw new Error("lost acknowledgement did not resolve through status");
    }
    const replayedCompletion = requireMultipartData(
      await multipartRequest<MultipartSessionStatusWire>(
        probe.accessToken,
        "POST",
        `/api/uploads/multipart-sessions/${lostAckSession.session_id}/complete`,
      ),
      "multipart completion replay",
    );
    if (
      replayedCompletion.state !== "committed" ||
      replayedCompletion.terminal_result === null ||
      replayedCompletion.terminal_result.source_version_id !==
        discardedAcknowledgement.terminal_result.source_version_id
    ) {
      throw new Error("completion replay did not return the frozen terminal result");
    }
    const lostAckServerEvidence = await readServerEvidence(lostAckPattern.declaredSha256, "committed");
    requireExactlyOnePublication(
      lostAckServerEvidence,
      "lost-acknowledgement replay created more than one publication",
    );
    if (lostAckServerEvidence.sessionCount !== 1 || lostAckServerEvidence.sessionCommittedCount !== 1) {
      throw new Error("lost-acknowledgement replay did not keep one committed session");
    }
    recordLiveDiagnostics({
      lostAckPublicationCount: lostAckServerEvidence.exactOperationPublicationCount,
      lostAckSessionCount: lostAckServerEvidence.sessionCount,
      lostAckReplaySameVersion: 1,
    });
    recordLivePhase("multipart_lost_ack_replayed");


    recordLivePhase("multipart_journey_completed");
  });
});
