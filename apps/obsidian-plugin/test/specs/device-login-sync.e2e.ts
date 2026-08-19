import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { browser } from "@wdio/globals";
import { runFromE2eRepositoryRoot } from "../support/repository-subprocess";

/**
 * Live-server device login and small-file sync journey. The real grant is
 * created and operator-approved through the real admin routes (Origin gate
 * + CSRF double-submit), then injected as the plugin's persisted pending
 * state so a plugin reload resumes the real exchange: poll → credential →
 * onboarding trust → policy acceptance. An edit of the fixture note must
 * then run settle → policy gate → preflight/content against the live API.
 *
 * Not part of the default gate: it needs a live server and the operator's
 * web credential, so it runs only when E2E_WEB_PASSWORD_FILE is set. Logs
 * carry closed labels only — never codes, cookies or vault content.
 */
const serverOrigin = process.env.E2E_SERVER_ORIGIN ?? "http://127.0.0.1:8000";
const allowedOrigin = process.env.E2E_ALLOWED_ORIGIN ?? "https://app.ducinvest.com";
const webUsername = process.env.E2E_WEB_USERNAME ?? "duc";
const passwordFile = process.env.E2E_WEB_PASSWORD_FILE;
const totpHelper = process.env.E2E_TOTP_HELPER;
const pluginDataPathSuffix = "plugins/knowledge-workspace/data.json";
const fixtureNonce = crypto.randomUUID();
const fixtureNotePath = `controlled-live-${crypto.randomUUID()}.md`;
const policyRaceNotePath = `controlled-policy-race-${crypto.randomUUID()}.md`;
const fixtureNoteContent = `# Test note\n\nUpdated by the live login journey.\n${fixtureNonce}\n`;
const policyRaceHeader = `# Controlled policy race\n\n${fixtureNonce}\n`;
const policyRaceNoteContent =
  policyRaceHeader + "x".repeat(1024 * 1024 - Buffer.byteLength(policyRaceHeader));
const fixtureDeclaredSha256 = crypto.createHash("sha256").update(fixtureNoteContent).digest("hex");
const policyRaceDeclaredSha256 = crypto
  .createHash("sha256")
  .update(policyRaceNoteContent)
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

function cookiePairsOf(response: Response): string[] {
  return (response.headers.getSetCookie() ?? []).map((cookie) => cookie.split(";")[0]);
}

function csrfValueOf(pairs: string[]): string | undefined {
  return pairs
    .find((pair) => pair.toLowerCase().includes("csrf"))
    ?.split("=")
    .slice(1)
    .join("=");
}

interface CreatedGrant {
  readonly grant_id: string;
  readonly user_code: string;
  readonly polling_secret: string;
  readonly verification_uri: string;
  readonly expires_in_seconds: number;
  readonly poll_interval_seconds: number;
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

async function prepareExtensionExclusionRule(
  cookies: string[],
  csrf: string,
  extension: ".md" | ".tmp",
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
        rules: [
          {
            rule_id: crypto.randomUUID(),
            rule_kind: "extension",
            extension,
          },
        ],
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

async function injectPendingGrant(grant: CreatedGrant): Promise<void> {
  await browser.execute(
    async (dataPathSuffix: string, pendingGrant: unknown, secretRecord: string) => {
      const app = (
        window as unknown as {
          app: {
            vault: {
              configDir: string;
              adapter: {
                read: (path: string) => Promise<string>;
                write: (path: string, data: string) => Promise<void>;
              };
            };
            secretStorage: {
              setSecret: (key: string, value: string) => Promise<void>;
            };
          };
        }
      ).app;
      const dataPath = `${app.vault.configDir}/${dataPathSuffix}`;
      const current = JSON.parse(await app.vault.adapter.read(dataPath)) as Record<string, unknown>;
      await app.secretStorage.setSecret("knowledge-workspace-device-credential", secretRecord);
      await app.vault.adapter.write(
        dataPath,
        JSON.stringify({ ...current, pending_grant: pendingGrant }),
      );
    },
    pluginDataPathSuffix,
    {
      grant_id: grant.grant_id,
      user_code: grant.user_code,
      verification_uri: grant.verification_uri,
      expires_at_epoch_seconds: Math.floor(Date.now() / 1000) + grant.expires_in_seconds,
      poll_interval_seconds: grant.poll_interval_seconds,
    },
    JSON.stringify({
      record_version: 1,
      state: "pending_grant",
      polling_secret: grant.polling_secret,
    }),
  );
}

async function editFixtureNote(): Promise<void> {
  await browser.execute(async (notePath: string, content: string) => {
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
    const file = app.vault.getAbstractFileByPath(notePath);
    if (file === null) {
      await app.vault.create(notePath, content);
      return;
    }
    await app.vault.modify(file, content);
  }, fixtureNotePath, fixtureNoteContent);
}

async function editFixtureNoteForPolicyRace(): Promise<void> {
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
  }, policyRaceNotePath, policyRaceNoteContent);
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
      waitingRetryCount: eventCount("event.state = 'waiting_retry'"),
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

async function triggerSyncNow(): Promise<void> {
  await browser.execute(() => {
    const app = (
      window as unknown as {
        app: { commands: { executeCommandById: (id: string) => void } };
      }
    ).app;
    app.commands.executeCommandById("knowledge-workspace:sync-now");
  });
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

  it("completes the device authorization flow and syncs an edited note", async function () {
    this.timeout(480_000);

    const createResponse = await fetch(`${serverOrigin}/api/auth/device-authorizations`, {
      method: "POST",
      headers: { "content-type": "application/json", origin: allowedOrigin },
      body: JSON.stringify({
        client_instance_id: crypto.randomUUID(),
        device_name: "e2e-harness",
        platform_class: "obsidian_desktop",
        platform_name: "windows",
        plugin_version: "0.1.0",
        requested_scope: "obsidian_sync",
      }),
    });
    if (createResponse.status !== 200 && createResponse.status !== 201) {
      throw new Error(`grant creation failed: ${createResponse.status}`);
    }
    const created = ((await createResponse.json()) as { data: CreatedGrant }).data;
    console.log("GRANT_CREATED");

    const password = fs.readFileSync(passwordFile as string, "utf8").trim();
    const loginResponse = await fetch(`${serverOrigin}/api/auth/login`, {
      method: "POST",
      headers: { "content-type": "application/json", origin: allowedOrigin },
      body: JSON.stringify({ username: webUsername, password }),
    });
    if (loginResponse.status !== 200) {
      throw new Error(`admin login failed: ${loginResponse.status}`);
    }
    const loginCookies = cookiePairsOf(loginResponse);
    const loginCsrf = csrfValueOf(loginCookies);

    const { stdout: totpStdout } = await runFromE2eRepositoryRoot(
      "uv",
      ["run", "python", totpHelper],
      import.meta.url,
    );
    const verifyResponse = await fetch(`${serverOrigin}/api/auth/totp/verify`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: allowedOrigin,
        cookie: loginCookies.join("; "),
        "x-csrf-token": loginCsrf ?? "",
      },
      body: JSON.stringify({ code: totpStdout.trim() }),
    });
    console.log("TOTP_VERIFY_STATUS", verifyResponse.status);
    if (verifyResponse.status !== 200) {
      throw new Error(`totp verification failed: ${verifyResponse.status}`);
    }
    const sessionCookies = cookiePairsOf(verifyResponse);
    const sessionCsrf = csrfValueOf(sessionCookies);
    adminSessionCookies = sessionCookies;
    adminSessionCsrf = sessionCsrf ?? "";
    const approveResponse = await fetch(
      `${serverOrigin}/api/auth/device-authorizations/${created.grant_id}/approve`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          origin: allowedOrigin,
          cookie: sessionCookies.join("; "),
          "x-csrf-token": sessionCsrf ?? "",
        },
      },
    );
    console.log("APPROVE_STATUS", approveResponse.status);
    if (approveResponse.status !== 200) {
      throw new Error(`grant approval failed: ${approveResponse.status}`);
    }

    const tmpPolicy = await prepareExtensionExclusionRule(
      sessionCookies,
      sessionCsrf ?? "",
      ".tmp",
    );
    const policyRevision = await publishPreparedPolicy(
      sessionCookies,
      sessionCsrf ?? "",
      tmpPolicy,
    );
    console.log("TMP_POLICY_PUBLISHED", policyRevision > 0);

    await injectPendingGrant(created);
    await reloadKnowledgeWorkspacePlugin();
    await browser.pause(5_000);

    let pendingGrantCleared = false;
    for (let attempt = 0; attempt < 30 && !pendingGrantCleared; attempt += 1) {
      await browser.pause(1_000);
      const data = await readPluginData();
      pendingGrantCleared = data.pending_grant === null || data.pending_grant === undefined;
    }
    console.log("PENDING_GRANT_CLEARED", pendingGrantCleared);
    await browser.pause(3_000);
    console.log("STATUS_AFTER_LOGIN", await readStatusBarText());
    journalDirectoryPath = await resolveJournalDirectoryPath();

    const initialServerEvidence = await readServerPublicationEvidence(fixtureDeclaredSha256);
    if (initialServerEvidence.operationCount !== 0) {
      throw new Error("controlled publication identity was not unique before capture");
    }
    await editFixtureNote();
    const initialJournalEvidence = await waitForJournalEvidence(
      fixtureNotePath,
      (evidence) =>
        evidence.committedCount === 1 &&
        evidence.pendingCount === 0 &&
        evidence.mappedCount === 1,
      "journal did not converge to exactly one committed mapped publication",
    );
    const publishedServerEvidence = await readServerPublicationEvidence(fixtureDeclaredSha256);
    console.log(
      "SANITIZED_SERVER_PUBLICATION_EVIDENCE",
      JSON.stringify(publishedServerEvidence),
    );
    if (
      publishedServerEvidence.sourceCount !== 1 ||
      publishedServerEvidence.sourceVersionCount !== 1 ||
      publishedServerEvidence.syncEventCount !== 1 ||
      publishedServerEvidence.operationCount !== 1 ||
      publishedServerEvidence.committedOperationCount !== 1 ||
      publishedServerEvidence.exactOperationPublicationCount !== 1 ||
      publishedServerEvidence.receivingUnpublishedOperationCount !== 0
    ) {
      throw new Error("server did not commit exactly one canonical publication");
    }
    console.log("SANITIZED_JOURNAL_EVIDENCE", JSON.stringify(initialJournalEvidence));

    const data = await readPluginData();
    console.log("PENDING_GRANT_FINAL", data.pending_grant === null ? "cleared" : "still-pending");

    const denyingMarkdownPolicy = await prepareExtensionExclusionRule(
      sessionCookies,
      sessionCsrf ?? "",
      ".md",
    );
    const policyRaceBaseline = await readServerPublicationEvidence(policyRaceDeclaredSha256);
    if (policyRaceBaseline.operationCount !== 0) {
      throw new Error("controlled policy-race identity was not unique before capture");
    }
    const operationObservation = readServerPublicationEvidence(policyRaceDeclaredSha256, true);
    await browser.pause(1_000);
    await editFixtureNoteForPolicyRace();
    await triggerSyncNow();
    const observedOperation = await operationObservation;
    if (
      observedOperation.operationCount !== 1 ||
      observedOperation.receivingUnpublishedOperationCount !== 1
    ) {
      throw new Error("server did not observe the controlled receiving operation");
    }
    const changedPolicyRevision = await publishPreparedPolicy(
      sessionCookies,
      sessionCsrf ?? "",
      denyingMarkdownPolicy,
    );
    console.log("MID_UPLOAD_POLICY_PUBLISHED", changedPolicyRevision > policyRevision);
    await new Promise((resolve) => setTimeout(resolve, 45_000));
    const retryableJournalEvidence = await readSanitizedJournalEvidence(policyRaceNotePath);
    console.log(
      "SANITIZED_POLICY_DENIAL_JOURNAL_EVIDENCE",
      JSON.stringify(retryableJournalEvidence),
    );
    const deniedBeforeRecovery = await readServerPublicationEvidence(policyRaceDeclaredSha256);
    console.log(
      "SANITIZED_POLICY_DENIAL_SERVER_EVIDENCE",
      JSON.stringify(deniedBeforeRecovery),
    );
    if (
      retryableJournalEvidence.pendingCount !== 1 ||
      deniedBeforeRecovery.sourceCount !== 0 ||
      deniedBeforeRecovery.sourceVersionCount !== 0 ||
      deniedBeforeRecovery.syncEventCount !== 0 ||
      deniedBeforeRecovery.operationCount !== 1 ||
      deniedBeforeRecovery.committedOperationCount !== 0 ||
      deniedBeforeRecovery.exactOperationPublicationCount !== 0 ||
      deniedBeforeRecovery.receivingUnpublishedOperationCount !== 1
    ) {
      throw new Error("mid-upload policy change did not fail closed into retryable recovery");
    }
    await reloadKnowledgeWorkspacePlugin();
    await browser.pause(5_000);
    await triggerSyncNow();
    const recoveredJournalEvidence = await waitForJournalEvidence(
      policyRaceNotePath,
      (evidence) => evidence.excludedPolicyCount === 1 && evidence.pendingCount === 0,
      "next-preflight recovery did not settle the event as excluded_policy",
    );
    const deniedPublicationEvidence = await readServerPublicationEvidence(
      policyRaceDeclaredSha256,
    );
    const preservedPublicationEvidence = await readSanitizedJournalEvidence(fixtureNotePath);
    console.log(
      "SANITIZED_MID_UPLOAD_POLICY_EVIDENCE",
      JSON.stringify({ ...deniedPublicationEvidence, recoveredExcludedPolicyCount: 1 }),
    );
    if (
      deniedPublicationEvidence.sourceCount !== 0 ||
      deniedPublicationEvidence.sourceVersionCount !== 0 ||
      deniedPublicationEvidence.syncEventCount !== 0 ||
      deniedPublicationEvidence.operationCount !== 1 ||
      deniedPublicationEvidence.committedOperationCount !== 0 ||
      deniedPublicationEvidence.exactOperationPublicationCount !== 0 ||
      deniedPublicationEvidence.receivingUnpublishedOperationCount !== 1 ||
      recoveredJournalEvidence.committedCount !== 0 ||
      recoveredJournalEvidence.mappedCount !== 0 ||
      preservedPublicationEvidence.committedCount !== 1 ||
      preservedPublicationEvidence.mappedCount !== 1
    ) {
      throw new Error("mid-upload policy change published canonical state or broke recovery");
    }
  });
});
