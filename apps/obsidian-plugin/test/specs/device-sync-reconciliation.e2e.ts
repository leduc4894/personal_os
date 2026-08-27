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
 * Mandatory Child 6 live Desktop journey (design 18.1).  The test drives the
 * four reconciliation scenarios against the authenticated public HTTPS plugin
 * origin through a real second device actor: a remote edit with exact
 * no-echo, a server-side cursor gap healed by manifest repair, SQLite
 * journal loss rebuilt without a duplicate canonical source and a remote
 * tombstone applied to Obsidian local trash.  Every assertion reads closed
 * canonical PostgreSQL evidence, the sanitized plugin journal/trail surfaces
 * or the settings device-sync projection; no mock substitutes for any
 * boundary and no locator, content, digest or credential is ever printed.
 */
const serverOrigin = process.env.E2E_SERVER_ORIGIN ?? "http://127.0.0.1:8000";
const allowedOrigin = process.env.E2E_ALLOWED_ORIGIN ?? "https://app.ducinvest.com";
const webUsername = process.env.E2E_WEB_USERNAME ?? "duc";
const passwordFile = process.env.E2E_WEB_PASSWORD_FILE;
const totpHelper = process.env.E2E_TOTP_HELPER;
const livePhaseStatusFile = process.env.E2E_LIVE_PHASE_STATUS_FILE;
const pluginDataPathSuffix = "plugins/knowledge-workspace/data.json";
const fixtureIdentity = crypto.randomUUID();
const fixturePath = `device-reconciliation-${fixtureIdentity}.md`;
const trashedPath = `.trash/${fixturePath}`;
const localContent = `# Device reconciliation fixture\n\nlocal ${fixtureIdentity}\n`;
const remoteEditContent = `# Device reconciliation fixture\n\nremote ${fixtureIdentity}\n`;
const remoteEditContentBytes = Buffer.from(remoteEditContent, "utf8");
const databaseEnvironmentKeys = [
  "KNOWLEDGE_SECRET_ROOT",
  "KNOWLEDGE_DATABASE_HOST",
  "KNOWLEDGE_DATABASE_PORT",
  "KNOWLEDGE_DATABASE_NAME",
  "KNOWLEDGE_DATABASE_USER",
  "KNOWLEDGE_DATABASE_PASSWORD_FILE",
] as const;

interface CanonicalReconciliationEvidence {
  readonly state: "not_found" | "present";
  readonly sourceId: string | null;
  readonly currentVersionId: string | null;
  readonly syncState: string | null;
  readonly versionCount: number;
  readonly canonicalSourceCount: number;
  readonly committedUploadCount: number;
  readonly createEventCount: number;
  readonly updateEventCount: number;
  readonly deleteEventCount: number;
  readonly openTombstoneCount: number;
  readonly activeLocatorCount: number;
  readonly retainedEventCount: number;
  readonly completedManifestRunCount: number;
  readonly acknowledgedSequence: number;
  readonly deliveredSequence: number;
}

interface JournalReconciliationEvidence {
  readonly mappedSourceId: string | null;
  readonly mappedVersionId: string | null;
  readonly appliedSequence: number;
  readonly acknowledgedSequence: number;
}

interface RemoteDeviceActor {
  readonly deviceId: string;
  accessToken: string;
  refreshCredential: string;
  policyRevision: number;
}

/**
 * One authorized device request with exactly one transparent refresh: an
 * expired access credential (401) is refreshed through the device token
 * rotation wire with a fresh client-minted rotation id and the request is
 * replayed once. Every other failure surfaces unchanged.
 */
async function remoteAuthorizedFetch(
  actor: RemoteDeviceActor,
  path: string,
  init: RequestInit,
): Promise<Response> {
  const perform = (): Promise<Response> =>
    fetch(`${serverOrigin}${path}`, {
      ...init,
      headers: { ...init.headers, authorization: `Bearer ${actor.accessToken}` },
    });
  let response = await perform();
  if (response.status !== 401) {
    return response;
  }
  const refreshResponse = await fetch(`${serverOrigin}/api/auth/device-tokens/refresh`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${actor.refreshCredential}`,
      accept: "application/json",
    },
    body: JSON.stringify({ rotation_id: crypto.randomUUID() }),
  });
  if (!refreshResponse.ok) {
    return response;
  }
  const refreshed = ((await refreshResponse.json()) as {
    data: { access_credential?: unknown; refresh_credential?: unknown };
  }).data;
  if (
    typeof refreshed.access_credential !== "string" ||
    typeof refreshed.refresh_credential !== "string"
  ) {
    throw new Error("the remote device token refresh was malformed");
  }
  actor.accessToken = refreshed.access_credential;
  actor.refreshCredential = refreshed.refresh_credential;
  response = await perform();
  return response;
}

function sha256Hex(bytes: Buffer): string {
  return crypto.createHash("sha256").update(bytes).digest("hex");
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
    locator = os.environ["DEVICE_SYNC_EVIDENCE_LOCATOR"]
    device_id = os.environ["DEVICE_SYNC_EVIDENCE_DEVICE_ID"]
    if re.fullmatch(r"[^\s]+", locator) is None or re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", device_id
    ) is None:
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
            with fixture_operation as (
              select operation.result_source_id as source_id
                from knowledge.small_file_upload_operations operation
               where operation.normalized_locator = %(locator)s
                 and operation.state = 'committed'
               order by operation.created_at desc
               limit 1
            )
            select
              source.source_id,
              source.current_version_id,
              source.sync_state,
              (select count(*) from knowledge.source_versions version
                where version.source_id = source.source_id),
              (select count(distinct operation.result_source_id)
                 from knowledge.small_file_upload_operations operation
                where operation.normalized_locator = %(locator)s
                  and operation.state = 'committed'),
              (select count(*) from knowledge.small_file_upload_operations operation
                where operation.normalized_locator = %(locator)s
                  and operation.state = 'committed'),
              (select count(*) from knowledge.sync_events event
                where event.source_id = source.source_id and event.event_type = 'create'),
              (select count(*) from knowledge.sync_events event
                where event.source_id = source.source_id and event.event_type = 'update'),
              (select count(*) from knowledge.sync_events event
                where event.source_id = source.source_id and event.event_type = 'delete'),
              (select count(*) from knowledge.source_tombstones tombstone
                where tombstone.source_id = source.source_id
                  and tombstone.restore_event_id is null),
              (select count(*) from knowledge.source_locators locator_row
                where locator_row.source_id = source.source_id
                  and locator_row.closed_event_id is null),
              (select count(*) from knowledge.sync_events event
                where event.workspace_id = source.workspace_id),
              (select count(*) from knowledge.manifest_runs run
                where run.workspace_id = source.workspace_id and run.state = 'completed'),
              (select coalesce(max(cursor_row.acknowledged_sequence), 0)
                 from knowledge.device_cursors cursor_row
                where cursor_row.workspace_id = source.workspace_id
                  and cursor_row.device_id = %(device_id)s),
              (select coalesce(max(cursor_row.delivered_through_sequence), 0)
                 from knowledge.device_cursors cursor_row
                where cursor_row.workspace_id = source.workspace_id
                  and cursor_row.device_id = %(device_id)s)
              from fixture_operation controlled
              join knowledge.sources source on source.source_id = controlled.source_id
            """,
            {"locator": locator, "device_id": device_id},
        ).fetchone()
    if row is None:
        print(json.dumps({"state": "not_found"}, separators=(",", ":")))
    else:
        print(json.dumps({
            "state": "present",
            "sourceId": str(row[0]),
            "currentVersionId": str(row[1]),
            "syncState": str(row[2]),
            "versionCount": int(row[3]),
            "canonicalSourceCount": int(row[4]),
            "committedUploadCount": int(row[5]),
            "createEventCount": int(row[6]),
            "updateEventCount": int(row[7]),
            "deleteEventCount": int(row[8]),
            "openTombstoneCount": int(row[9]),
            "activeLocatorCount": int(row[10]),
            "retainedEventCount": int(row[11]),
            "completedManifestRunCount": int(row[12]),
            "acknowledgedSequence": int(row[13]),
            "deliveredSequence": int(row[14]),
        }, separators=(",", ":")))
except BaseException:
    print(json.dumps({"state": "server_evidence_unavailable"}, separators=(",", ":")))
    raise SystemExit(1)
`;

const deleteRetainedEventsScript = String.raw`
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
    locator = os.environ["DEVICE_SYNC_EVIDENCE_LOCATOR"]
    if re.fullmatch(r"[^\s]+", locator) is None:
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
        deleted = connection.execute(
            """
            delete from knowledge.sync_events event
             using knowledge.sources source
             join knowledge.small_file_upload_operations operation
               on operation.result_source_id = source.source_id
              and operation.normalized_locator = %(locator)s
              and operation.state = 'committed'
             where event.workspace_id = source.workspace_id
            """,
            {"locator": locator},
        ).rowcount
        connection.commit()
    print(json.dumps({"deletedEventCount": int(deleted)}, separators=(",", ":")))
except BaseException:
    print(json.dumps({"deletedEventCount": -1}, separators=(",", ":")))
    raise SystemExit(1)
`;

async function readDesktopDeviceId(): Promise<string> {
  const directory = await journalDirectoryPath();
  const data = JSON.parse(
    fs.readFileSync(path.join(directory, "data.json"), "utf8"),
  ) as Record<string, unknown>;
  const deviceId = data["device_id"];
  if (typeof deviceId !== "string" || deviceId.length === 0) {
    throw new Error("the onboarded device identity was unavailable");
  }
  return deviceId;
}

async function readCanonicalEvidence(): Promise<CanonicalReconciliationEvidence | null> {
  const deviceId = await readDesktopDeviceId();
  const { stdout } = await runFromE2eRepositoryRoot(
    "uv",
    ["run", "python", "-c", canonicalEvidenceScript],
    import.meta.url,
    {
      ...process.env,
      DEVICE_SYNC_EVIDENCE_LOCATOR: fixturePath,
      DEVICE_SYNC_EVIDENCE_DEVICE_ID: deviceId,
    },
  );
  const parsed = JSON.parse(stdout) as Record<string, unknown>;
  if (parsed["state"] === "not_found") {
    return null;
  }
  if (parsed["state"] !== "present") {
    throw new Error("canonical reconciliation evidence was unavailable");
  }
  const countKeys = [
    "versionCount",
    "canonicalSourceCount",
    "committedUploadCount",
    "createEventCount",
    "updateEventCount",
    "deleteEventCount",
    "openTombstoneCount",
    "activeLocatorCount",
    "retainedEventCount",
    "completedManifestRunCount",
    "acknowledgedSequence",
    "deliveredSequence",
  ] as const;
  if (
    typeof parsed["sourceId"] !== "string" ||
    typeof parsed["currentVersionId"] !== "string" ||
    typeof parsed["syncState"] !== "string" ||
    countKeys.some((key) => !Number.isSafeInteger(parsed[key]) || Number(parsed[key]) < 0)
  ) {
    throw new Error("canonical reconciliation evidence was invalid");
  }
  return parsed as unknown as CanonicalReconciliationEvidence;
}

async function waitForCanonicalEvidence(
  accepts: (evidence: CanonicalReconciliationEvidence) => boolean,
  failureMessage: string,
): Promise<CanonicalReconciliationEvidence> {
  let lastSafeState: Record<string, unknown> | null = null;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const evidence = await readCanonicalEvidence();
    if (evidence !== null) {
      lastSafeState = {
        syncState: evidence.syncState,
        versionCount: evidence.versionCount,
        canonicalSourceCount: evidence.canonicalSourceCount,
        committedUploadCount: evidence.committedUploadCount,
        updateEventCount: evidence.updateEventCount,
        openTombstoneCount: evidence.openTombstoneCount,
        completedManifestRunCount: evidence.completedManifestRunCount,
        acknowledgedSequence: evidence.acknowledgedSequence,
      };
      if (accepts(evidence)) {
        return evidence;
      }
    }
    await browser.pause(1_000);
  }
  throw new Error(`${failureMessage}: ${JSON.stringify(lastSafeState)}`);
}

async function deleteRetainedWorkspaceEvents(): Promise<number> {
  const { stdout } = await runFromE2eRepositoryRoot(
    "uv",
    ["run", "python", "-c", deleteRetainedEventsScript],
    import.meta.url,
    { ...process.env, DEVICE_SYNC_EVIDENCE_LOCATOR: fixturePath },
  );
  const parsed = JSON.parse(stdout) as Record<string, unknown>;
  if (!Number.isSafeInteger(parsed["deletedEventCount"]) || Number(parsed["deletedEventCount"]) < 0) {
    throw new Error("retained event deletion evidence was unavailable");
  }
  return Number(parsed["deletedEventCount"]);
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

interface TrailSidecarEntry {
  readonly kind?: unknown;
  readonly tokens?: unknown;
}

async function readTrailTokens(): Promise<string[]> {
  const directory = await journalDirectoryPath();
  let document: unknown;
  try {
    document = JSON.parse(
      fs.readFileSync(path.join(directory, "sync-diagnostics-trail.json"), "utf8"),
    );
  } catch {
    return [];
  }
  if (typeof document !== "object" || document === null) {
    return [];
  }
  const entries = (document as Record<string, unknown>)["entries"];
  if (!Array.isArray(entries)) {
    return [];
  }
  const tokens: string[] = [];
  for (const entry of entries as TrailSidecarEntry[]) {
    if (
      typeof entry === "object" &&
      entry !== null &&
      typeof entry.kind === "string" &&
      Array.isArray(entry.tokens)
    ) {
      for (const token of entry.tokens) {
        if (typeof token === "string") {
          tokens.push(`${entry.kind}:${token}`);
        }
      }
    }
  }
  return tokens;
}

async function waitForTrailToken(token: string, failureMessage: string): Promise<void> {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if ((await readTrailTokens()).includes(token)) {
      return;
    }
    await browser.pause(1_000);
  }
  throw new Error(failureMessage);
}

async function readJournalEvidence(): Promise<JournalReconciliationEvidence | null> {
  const directory = await journalDirectoryPath();
  const manifest = JSON.parse(
    fs.readFileSync(path.join(directory, "journal.manifest.json"), "utf8"),
  ) as { current?: { generationNumber?: unknown } };
  const generationNumber = manifest.current?.generationNumber;
  if (!Number.isSafeInteger(generationNumber) || Number(generationNumber) < 0) {
    throw new Error("journal reconciliation evidence was unavailable");
  }
  const journalBytes = fs.readFileSync(
    path.join(directory, `journal.sqlite.g${generationNumber}`),
  );
  const initSqlJs = (await import("sql.js")).default;
  const engine = await initSqlJs();
  const database = new engine.Database(journalBytes);
  try {
    const mappingStatement = database.prepare(
      "select source_id, base_version_id from local_files where normalized_path = ?",
    );
    try {
      mappingStatement.bind([fixturePath]);
      let mappedSourceId: string | null = null;
      let mappedVersionId: string | null = null;
      if (mappingStatement.step()) {
        const row = mappingStatement.get();
        if (typeof row[0] === "string") {
          mappedSourceId = row[0];
        }
        if (typeof row[1] === "string") {
          mappedVersionId = row[1];
        }
      }
      const stateStatement = database.prepare(
        "select applied_sequence, acknowledged_sequence from device_sync_state where singleton_key = 1",
      );
      try {
        if (!stateStatement.step()) {
          return null;
        }
        const stateRow = stateStatement.get();
        return {
          mappedSourceId,
          mappedVersionId,
          appliedSequence: Number(stateRow[0]),
          acknowledgedSequence: Number(stateRow[1]),
        };
      } finally {
        stateStatement.free();
      }
    } finally {
      mappingStatement.free();
    }
  } finally {
    database.close();
  }
}

async function waitForJournalEvidence(
  accepts: (evidence: JournalReconciliationEvidence) => boolean,
  failureMessage: string,
): Promise<JournalReconciliationEvidence> {
  let lastSafeState: Record<string, number | string | null> | null = null;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const evidence = await readJournalEvidence();
      if (evidence !== null) {
        lastSafeState = {
          appliedSequence: evidence.appliedSequence,
          acknowledgedSequence: evidence.acknowledgedSequence,
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

// --- the remote second-device actor (Node-side wire client) -----------------------------------------

const TOTP_CODE_PATTERN = /^[0-9]{6}$/;
let previousVerifiedTotpCode: string | null = null;

async function readFreshTotpCode(): Promise<string> {
  if (totpHelper === undefined) {
    throw new Error("live E2E environment loader did not provide the TOTP-helper contract");
  }
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const { stdout } = await runFromE2eRepositoryRoot(
      "uv",
      ["run", "python", totpHelper],
      import.meta.url,
    );
    const candidate = stdout.trim();
    if (!TOTP_CODE_PATTERN.test(candidate)) {
      throw new Error("TOTP helper produced an invalid code");
    }
    if (candidate !== previousVerifiedTotpCode) {
      return candidate;
    }
    await new Promise<void>((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error("TOTP helper did not advance to an unused code");
}

function cookiePairsOf(response: Response): string[] {
  return (response.headers.getSetCookie() ?? []).map((cookie) => cookie.split(";")[0]);
}

function csrfValueOf(pairs: readonly string[]): string | undefined {
  return pairs
    .find((pair) => pair.toLowerCase().includes("csrf"))
    ?.split("=")
    .slice(1)
    .join("=");
}

interface CreatedGrant {
  readonly grant_id: string;
  readonly polling_secret: string;
  readonly poll_interval_seconds: number;
}

async function authorizeRemoteDeviceActor(): Promise<RemoteDeviceActor> {
  if (passwordFile === undefined) {
    throw new Error("live E2E environment loader did not provide the credential-file contract");
  }
  const createResponse = await fetch(`${serverOrigin}/api/auth/device-authorizations`, {
    method: "POST",
    headers: { "content-type": "application/json", origin: allowedOrigin },
    body: JSON.stringify({
      client_instance_id: crypto.randomUUID(),
      device_name: "device-reconciliation-remote",
      platform_class: "obsidian_desktop",
      platform_name: "windows",
      // The launcher's auth gate pins the accepted plugin version range
      // (0.1.0 today); the desktop onboarding helper sends the same value.
      plugin_version: "0.1.0",
      requested_scope: "obsidian_sync",
    }),
  });
  if (!createResponse.ok) {
    throw new Error(`remote device grant creation failed: ${createResponse.status}`);
  }
  const grant = ((await createResponse.json()) as { data: CreatedGrant }).data;

  const password = fs.readFileSync(passwordFile, "utf8").trim();
  const loginResponse = await fetch(`${serverOrigin}/api/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json", origin: allowedOrigin },
    body: JSON.stringify({ username: webUsername, password }),
  });
  if (!loginResponse.ok) {
    throw new Error(`remote actor admin login failed: ${loginResponse.status}`);
  }
  const loginCookies = cookiePairsOf(loginResponse);
  const loginCsrf = csrfValueOf(loginCookies) ?? "";
  // The desktop onboarding inside this same journey may have consumed the
  // current time-step's code seconds ago: only the closed replay rejection
  // (401) retries with the next helper value, exactly like the onboarding
  // helper; every other failure surfaces.
  let verifyResponse: Response | null = null;
  for (let attempt = 0; attempt < 3 && (verifyResponse === null || !verifyResponse.ok); attempt += 1) {
    const totpCode = await readFreshTotpCode();
    verifyResponse = await fetch(`${serverOrigin}/api/auth/totp/verify`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: allowedOrigin,
        cookie: loginCookies.join("; "),
        "x-csrf-token": loginCsrf,
      },
      body: JSON.stringify({ code: totpCode }),
    });
    previousVerifiedTotpCode = totpCode;
    if (!verifyResponse.ok && verifyResponse.status !== 401) {
      throw new Error(`remote actor TOTP verification failed: ${verifyResponse.status}`);
    }
  }
  if (verifyResponse === null || !verifyResponse.ok) {
    throw new Error(`remote actor TOTP verification failed: ${verifyResponse?.status ?? 0}`);
  }
  const sessionCookies = cookiePairsOf(verifyResponse);
  const sessionCsrf = csrfValueOf(sessionCookies) ?? "";
  const approveResponse = await fetch(
    `${serverOrigin}/api/auth/device-authorizations/${grant.grant_id}/approve`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: allowedOrigin,
        cookie: sessionCookies.join("; "),
        "x-csrf-token": sessionCsrf,
      },
    },
  );
  if (!approveResponse.ok) {
    throw new Error(`remote device grant approval failed: ${approveResponse.status}`);
  }

  let accessToken: string | null = null;
  let refreshCredential: string | null = null;
  let deviceId: string | null = null;
  for (let attempt = 0; attempt < 30 && accessToken === null; attempt += 1) {
    const pollResponse = await fetch(
      `${serverOrigin}/api/auth/device-authorizations/${grant.grant_id}/poll`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${grant.polling_secret}`,
        },
        body: "{}",
      },
    );
    if (pollResponse.ok) {
      const exchange = ((await pollResponse.json()) as {
        data: { access_credential: unknown; refresh_credential: unknown; device_id: unknown };
      }).data;
      if (
        typeof exchange.access_credential !== "string" ||
        typeof exchange.refresh_credential !== "string" ||
        typeof exchange.device_id !== "string"
      ) {
        throw new Error("remote device exchange was malformed");
      }
      accessToken = exchange.access_credential;
      refreshCredential = exchange.refresh_credential;
      deviceId = exchange.device_id;
      break;
    }
    const error = ((await pollResponse.json().catch(() => null)) as {
      error?: { code?: unknown } | null;
    } | null) ?? null;
    const code = error?.error?.code;
    if (code !== "authorization_pending" && code !== "slow_down") {
      throw new Error(`remote device poll failed: ${pollResponse.status}`);
    }
    await new Promise<void>((resolve) =>
      setTimeout(resolve, Math.max(1, grant.poll_interval_seconds) * 1_000),
    );
  }
  if (accessToken === null || refreshCredential === null || deviceId === null) {
    throw new Error("remote device authorization did not converge");
  }
  const actor: RemoteDeviceActor = {
    deviceId,
    accessToken,
    refreshCredential,
    policyRevision: 0,
  };
  actor.policyRevision = await fetchRemotePolicyRevision(actor);
  return actor;
}

async function fetchRemotePolicyRevision(actor: RemoteDeviceActor): Promise<number> {
  const response = await remoteAuthorizedFetch(actor, "/api/sync/exclusion-policy/snapshot", {
    headers: { accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`remote policy snapshot failed: ${response.status}`);
  }
  const payload = ((await response.json()) as {
    data?: { payload?: { revision_number?: unknown } | null };
  }).data?.payload;
  const revision = payload?.revision_number;
  if (!Number.isSafeInteger(revision) || Number(revision) < 0) {
    throw new Error("remote policy revision was malformed");
  }
  return Number(revision);
}

interface RemoteUploadReceipt {
  readonly sourceId: string;
  readonly sourceVersionId: string;
}

async function remoteCommitSourceUpdate(
  actor: RemoteDeviceActor,
  sourceId: string,
  baseVersionId: string,
  contentBytes: Buffer,
): Promise<RemoteUploadReceipt> {
  const preflightResponse = await remoteAuthorizedFetch(
    actor,
    "/api/sync/journal-events/preflight",
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify({
        event_id: crypto.randomUUID(),
        idempotency_key: crypto.randomUUID(),
        operation: "update",
        local_file_id: crypto.randomUUID(),
        source_id: sourceId,
        base_version_id: baseVersionId,
        normalized_locator: fixturePath,
        sha256: sha256Hex(contentBytes),
        size_bytes: contentBytes.byteLength,
        media_type: "text/plain",
        policy_revision: actor.policyRevision,
      }),
    },
  );
  if (!preflightResponse.ok) {
    throw new Error(`remote update preflight failed: ${preflightResponse.status}`);
  }
  const preflight = ((await preflightResponse.json()) as {
    data: { outcome?: unknown; operation_id?: unknown };
  }).data;
  if (preflight.outcome !== "single_part_upload" || typeof preflight.operation_id !== "string") {
    throw new Error("remote update preflight did not continue the upload");
  }
  const uploadResponse = await remoteAuthorizedFetch(
    actor,
    `/api/uploads/${encodeURIComponent(preflight.operation_id)}/content`,
    {
      method: "PUT",
      headers: {
        "content-type": "application/octet-stream",
        accept: "application/json",
      },
      body: new Uint8Array(contentBytes),
    },
  );
  if (!uploadResponse.ok) {
    throw new Error(`remote update upload failed: ${uploadResponse.status}`);
  }
  const receipt = ((await uploadResponse.json()) as {
    data: { result_kind?: unknown; source_id?: unknown; source_version_id?: unknown };
  }).data;
  if (
    receipt.result_kind !== "committed" ||
    typeof receipt.source_id !== "string" ||
    typeof receipt.source_version_id !== "string"
  ) {
    throw new Error("remote update upload did not commit");
  }
  return { sourceId: receipt.source_id, sourceVersionId: receipt.source_version_id };
}

async function remoteCommitSourceTombstone(
  actor: RemoteDeviceActor,
  sourceId: string,
  currentVersionId: string,
): Promise<void> {
  const response = await remoteAuthorizedFetch(actor, "/api/sources/lifecycle-events", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify({
      event_id: crypto.randomUUID(),
      idempotency_key: crypto.randomUUID(),
      source_id: sourceId,
      operation: "delete",
      expected_version_id: currentVersionId,
      expected_locator: fixturePath,
      target_locator: null,
      tombstone_id: null,
      policy_revision: actor.policyRevision,
      client_timestamp: new Date().toISOString(),
    }),
  });
  if (!response.ok) {
    throw new Error(`remote tombstone commit failed: ${response.status}`);
  }
}

// --- the Obsidian-side journey helpers ---------------------------------------------------------------

/**
 * Trigger one foreground sync cycle. The plugin exposes no standalone
 * "sync now" command — the coordinator's real trigger surfaces are Vault
 * events, the explicit repair command and the 30-second cadence — so the
 * journey rewrites the fixture note with its own unchanged bytes through
 * the public Vault API: the modify event always fires, the capture admits
 * nothing (the fingerprint is unchanged) and the `local_commit` trigger
 * runs one bounded cycle with zero canonical side effects.
 */
async function triggerSyncNow(): Promise<void> {
  await browser.execute(async (notePath: string) => {
    const app = (
      window as unknown as {
        app: {
          vault: {
            getAbstractFileByPath: (path: string) => unknown;
            read: (file: unknown) => Promise<string>;
            modify: (file: unknown, data: string) => Promise<void>;
          };
        };
      }
    ).app;
    const file = app.vault.getAbstractFileByPath(notePath);
    if (file === null) {
      return;
    }
    await app.vault.modify(file, await app.vault.read(file));
  }, fixturePath);
}

async function triggerExplicitRepair(): Promise<void> {
  await browser.execute(() => {
    const app = (
      window as unknown as {
        app: { commands: { executeCommandById: (commandId: string) => void } };
      }
    ).app;
    app.commands.executeCommandById("knowledge-workspace:repair-sync");
  });
}

async function createFixtureNote(): Promise<void> {
  await browser.execute(async (notePath: string, content: string) => {
    const app = (
      window as unknown as {
        app: { vault: { create: (path: string, text: string) => Promise<void> } };
      }
    ).app;
    await app.vault.create(notePath, content);
  }, fixturePath, localContent);
}

async function readVaultFileText(notePath: string): Promise<string | null> {
  return browser.execute(async (targetPath: string) => {
    const app = (
      window as unknown as {
        app: {
          vault: {
            adapter: {
              exists: (path: string) => Promise<boolean>;
              read: (path: string) => Promise<string>;
            };
          };
        };
      }
    ).app;
    if (!(await app.vault.adapter.exists(targetPath))) {
      return null;
    }
    return app.vault.adapter.read(targetPath);
  }, notePath);
}

/**
 * SQLite loss: every immutable journal generation image is removed while the
 * verified manifest stays behind pointing at nothing, so the next open
 * rebuilds an empty `reconcile_required` journal (the recovery contract's
 * "nothing verified but artifacts exist" branch).  The Vault file and every
 * canonical row stay untouched.
 */
async function removePluginJournalGenerations(): Promise<void> {
  const directory = await journalDirectoryPath();
  await browser.execute(async () => {
    const app = (
      window as unknown as {
        app: {
          plugins: { disablePlugin: (pluginId: string) => Promise<void> };
        };
      }
    ).app;
    await app.plugins.disablePlugin("knowledge-workspace");
  });
  await browser.pause(1_500);
  const removedGenerations = fs
    .readdirSync(directory)
    .filter((name) => /^journal\.sqlite\.g\d+$/.test(name))
    .map((name) => path.join(directory, name));
  for (const generationPath of removedGenerations) {
    fs.rmSync(generationPath);
  }
  if (
    removedGenerations.length === 0 ||
    !fs.existsSync(path.join(directory, "journal.manifest.json"))
  ) {
    throw new Error("the SQLite-loss seam did not find the expected journal artifacts");
  }
  await browser.execute(async () => {
    const app = (
      window as unknown as {
        app: {
          plugins: { enablePlugin: (pluginId: string) => Promise<void> };
        };
      }
    ).app;
    await app.plugins.enablePlugin("knowledge-workspace");
  });
}

async function readDeviceSyncStatusText(): Promise<string | null> {
  await browser.execute(() => {
    const app = (
      window as unknown as {
        app: { commands: { executeCommandById: (commandId: string) => void } };
      }
    ).app;
    app.commands.executeCommandById("app:open-settings");
  });
  await browser.pause(400);
  const didSwitch = await browser.execute(() => {
    const candidate =
      document.querySelector('.vertical-tab-header-item[data-id="knowledge-workspace"]') ??
      Array.from(document.querySelectorAll(".vertical-tab-header-item")).find((element) =>
        element.textContent?.includes("Knowledge Workspace"),
      );
    if (!(candidate instanceof HTMLElement)) {
      return false;
    }
    candidate.click();
    return true;
  });
  if (!didSwitch) {
    throw new Error("the knowledge settings tab was unavailable");
  }
  await browser.pause(400);
  const statusText = await browser.execute(() => {
    const setting = Array.from(document.querySelectorAll(".setting-item")).find(
      (element) => element.querySelector(".setting-item-name")?.textContent?.trim() === "Device sync",
    );
    return setting?.querySelector(".setting-item-description")?.textContent?.trim() ?? null;
  });
  await browser.execute(() => {
    const closeButton = document.querySelector(".modal-close-button");
    if (closeButton instanceof HTMLElement) {
      closeButton.click();
    }
  });
  return statusText;
}

async function waitForDeviceSyncStatus(
  accepts: (statusText: string) => boolean,
  failureMessage: string,
): Promise<string> {
  let lastStatusText: string | null = null;
  for (let attempt = 0; attempt < 45; attempt += 1) {
    const statusText = await readDeviceSyncStatusText();
    if (statusText !== null && accepts(statusText)) {
      return statusText;
    }
    lastStatusText = statusText;
    await browser.pause(1_000);
  }
  throw new Error(`${failureMessage}: ${JSON.stringify(lastStatusText)}`);
}

describe("device cursor and manifest reconciliation (live server)", () => {
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

  it("reconciles remote edits, cursor gaps, lost SQLite journals and remote tombstones", async function () {
    this.timeout(780_000);
    recordLivePhase("device_sync_scenario_started");
    try {
      await runDeviceReconciliationJourney();
    } catch (error) {
      // Sanitized closed-token diagnostics of the exact stuck state — trail
      // tokens, cursor watermarks and the settings projection only — so a
      // failed gate names its layer without any content or locator. Each
      // piece logs independently; a failing read never masks the others.
      try {
        console.log(
          "SANITIZED_DEVICE_RECONCILIATION_FAILURE_TRAIL",
          JSON.stringify((await readTrailTokens()).slice(-30)),
        );
      } catch {
        // Diagnostics never mask the original failure.
      }
      try {
        const journalState = await readJournalEvidence();
        console.log(
          "SANITIZED_DEVICE_RECONCILIATION_FAILURE_JOURNAL",
          JSON.stringify({
            journalAppliedSequence: journalState?.appliedSequence ?? null,
            journalAcknowledgedSequence: journalState?.acknowledgedSequence ?? null,
            journalMappedSourcePresent: journalState?.mappedSourceId !== null,
          }),
        );
      } catch {
        // Diagnostics never mask the original failure.
      }
      try {
        console.log(
          "SANITIZED_DEVICE_RECONCILIATION_FAILURE_STATUS",
          JSON.stringify(await readDeviceSyncStatusText()),
        );
      } catch {
        // Diagnostics never mask the original failure.
      }
      throw error;
    }
  });

  async function runDeviceReconciliationJourney(): Promise<void> {

    // --- shared setup: the desktop device and the remote second device ----
    await onboardLiveDevice({
      serverOrigin,
      allowedOrigin,
      webUsername,
      passwordFile,
      totpHelper,
      pluginDataPathSuffix,
      deviceName: "device-reconciliation-desktop",
    });
    recordLivePhase("device_sync_onboarding_completed");
    const remoteActor = await authorizeRemoteDeviceActor();

    // --- scenario 1: remote edit plus exact no-echo ------------------------
    await createFixtureNote();
    await triggerSyncNow();
    const created = await waitForCanonicalEvidence(
      (evidence) =>
        evidence.syncState === "active" &&
        evidence.createEventCount === 1 &&
        evidence.versionCount === 1,
      "the fixture source did not settle canonically after the local create",
    );
    const remoteEdit = await remoteCommitSourceUpdate(
      remoteActor,
      created.sourceId as string,
      created.currentVersionId as string,
      remoteEditContentBytes,
    );
    await triggerSyncNow();
    const afterRemoteEdit = await waitForCanonicalEvidence(
      (evidence) =>
        evidence.syncState === "active" &&
        evidence.currentVersionId === remoteEdit.sourceVersionId &&
        evidence.versionCount === 2,
      "the remote edit did not advance the canonical source",
    );
    // Exact no-echo: the applied edit never echoes back as a third version
    // and no second locator-bound upload operation exists for the fixture
    // (the remote actor's update operation carries no locator — only the
    // desktop's original create does).
    const settledNoEcho = await waitForCanonicalEvidence(
      (evidence) =>
        evidence.versionCount === 2 &&
        evidence.committedUploadCount === 1 &&
        evidence.currentVersionId === remoteEdit.sourceVersionId,
      "the desktop echoed the applied remote edit back as a new version",
    );
    await waitForJournalEvidence(
      (evidence) =>
        evidence.acknowledgedSequence >= afterRemoteEdit.deliveredSequence &&
        evidence.acknowledgedSequence === evidence.appliedSequence,
      "the desktop cursor did not settle after the remote edit",
    );
    expect(await readVaultFileText(fixturePath)).toBe(remoteEditContent);
    expect(settledNoEcho.canonicalSourceCount).toBe(1);
    await waitForDeviceSyncStatus(
      (text) => text.includes("Repair: Ready"),
      "the device-sync status did not settle to Ready after the remote edit",
    );
    recordLivePhase("device_sync_remote_edit_no_echo_completed");

    // --- scenario 2: cursor gap to manifest repair -------------------------
    const deletedEventCount = await deleteRetainedWorkspaceEvents();
    if (deletedEventCount <= 0) {
      throw new Error("the retained-history deletion did not remove any event");
    }
    await triggerSyncNow();
    await waitForTrailToken(
      "cursor_failure:pull:device_cursor_gap",
      "the server cursor gap never surfaced on the closed diagnostics trail",
    );
    await triggerExplicitRepair();
    const afterGapRepair = await waitForCanonicalEvidence(
      (evidence) =>
        evidence.completedManifestRunCount >= 1 &&
        evidence.acknowledgedSequence === evidence.deliveredSequence &&
        evidence.versionCount === 2 &&
        evidence.canonicalSourceCount === 1,
      "the manifest repair did not converge the cursor gap",
    );
    await waitForDeviceSyncStatus(
      (text) => text.includes("Repair: Ready"),
      "the device-sync status did not return to Ready after the gap repair",
    );
    expect(await readVaultFileText(fixturePath)).toBe(remoteEditContent);
    recordLivePhase("device_sync_cursor_gap_repair_completed");

    // --- scenario 3: SQLite loss without a duplicate canonical source ------
    await removePluginJournalGenerations();
    await triggerExplicitRepair();
    const afterSqliteLoss = await waitForCanonicalEvidence(
      (evidence) =>
        evidence.completedManifestRunCount >= Number(afterGapRepair.completedManifestRunCount) + 1 &&
        evidence.canonicalSourceCount === 1 &&
        evidence.versionCount === 2 &&
        evidence.syncState === "active",
      "the lost-SQLite repair did not rebind to the single canonical source",
    );
    const rebuiltJournal = await waitForJournalEvidence(
      (evidence) =>
        evidence.acknowledgedSequence === evidence.appliedSequence &&
        evidence.acknowledgedSequence >= afterGapRepair.acknowledgedSequence,
      "the rebuilt journal cursor did not settle after the repair",
    );
    await waitForDeviceSyncStatus(
      (text) => text.includes("Repair: Ready"),
      "the device-sync status did not return to Ready after the SQLite-loss repair",
    );
    expect(await readVaultFileText(fixturePath)).toBe(remoteEditContent);
    expect(afterSqliteLoss.canonicalSourceCount).toBe(1);
    expect(rebuiltJournal.mappedSourceId === null || rebuiltJournal.mappedSourceId === created.sourceId).toBe(true);
    recordLivePhase("device_sync_lost_sqlite_repair_completed");

    // --- scenario 4: remote tombstone to Obsidian local trash --------------
    await remoteCommitSourceTombstone(
      remoteActor,
      created.sourceId as string,
      remoteEdit.sourceVersionId,
    );
    await triggerSyncNow();
    const afterTombstone = await waitForCanonicalEvidence(
      (evidence) =>
        evidence.syncState === "deleted" &&
        evidence.deleteEventCount === 1 &&
        evidence.openTombstoneCount === 1 &&
        evidence.activeLocatorCount === 0,
      "the remote tombstone did not commit canonically",
    );
    const trashedContent = await readVaultFileText(trashedPath);
    expect(await readVaultFileText(fixturePath)).toBeNull();
    expect(trashedContent).toBe(remoteEditContent);
    expect(afterTombstone.canonicalSourceCount).toBe(1);
    await waitForDeviceSyncStatus(
      (text) => text.includes("Repair: Ready"),
      "the device-sync status did not settle to Ready after the tombstone apply",
    );
    recordLivePhase("device_sync_remote_tombstone_completed");

    // --- the closed final verdict ------------------------------------------
    const finalJournal = await readJournalEvidence();
    expect(finalJournal !== null).toBe(true);
    expect(finalJournal?.acknowledgedSequence).toBe(finalJournal?.appliedSequence);
    recordLivePhase("device_sync_journey_completed");
    console.log(
      "SANITIZED_DEVICE_RECONCILIATION_EVIDENCE",
      JSON.stringify({
        remoteEditAppliedWithoutEcho:
          settledNoEcho.versionCount === 2 && settledNoEcho.committedUploadCount === 1,
        cursorGapRepaired: afterGapRepair.acknowledgedSequence === afterGapRepair.deliveredSequence,
        lostSqliteRepairedWithoutDuplicate:
          afterSqliteLoss.canonicalSourceCount === 1 && afterSqliteLoss.versionCount === 2,
        remoteTombstoneInLocalTrash:
          afterTombstone.syncState === "deleted" && trashedContent === remoteEditContent,
        completedManifestRunCount: afterSqliteLoss.completedManifestRunCount,
      }),
    );
  }
});
