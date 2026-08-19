import * as crypto from "node:crypto";
import { execFile } from "node:child_process";
import * as fs from "node:fs";
import { promisify } from "node:util";
import { browser } from "@wdio/globals";

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

const runFile = promisify(execFile);

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

async function publishTmpExclusionRule(cookies: string[], csrf: string): Promise<number> {
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
            extension: ".tmp",
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
  const publication = await responseData<{ revision_number: number; rule_count: number }>(
    await fetch(`${serverOrigin}/api/admin/exclusion-policy/publications`, {
      method: "POST",
      headers: {
        ...adminHeaders(cookies, csrf),
        "X-Idempotency-Key": `obsidian-e2e-${crypto.randomUUID()}`,
      },
      body: JSON.stringify({
        policy_preview_id: preview.policy_preview_id,
        policy_draft_id: preview.policy_draft_id,
        expected_draft_version: preview.draft_version,
        expected_draft_sha256: preview.draft_sha256,
        preview_impact_digest: preview.impact_digest,
        expected_active_policy_revision_id: preview.base_policy_revision_id,
        expected_active_revision_number: status.active_revision_number,
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
  await browser.execute(async () => {
    const app = (
      window as unknown as {
        app: {
          vault: {
            getAbstractFileByPath: (path: string) => unknown;
            modify: (file: unknown, content: string) => Promise<void>;
          };
        };
      }
    ).app;
    const file = app.vault.getAbstractFileByPath("hello.md");
    await app.vault.modify(file, "# Test note\n\nUpdated by the live login journey.\n");
  });
}

describe("device login and small-file sync (live server)", () => {
  before(() => {
    if (passwordFile === undefined || totpHelper === undefined) {
      throw new Error(
        "live E2E environment loader did not provide the credential-file and TOTP-helper contracts",
      );
    }
  });

  it("completes the device authorization flow and syncs an edited note", async function () {
    this.timeout(300_000);

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

    const { stdout: totpStdout } = await runFile("uv", ["run", "python", totpHelper], {
      cwd: "D:/App/personal_os",
    });
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

    const policyRevision = await publishTmpExclusionRule(sessionCookies, sessionCsrf ?? "");
    console.log("TMP_POLICY_PUBLISHED", policyRevision > 0);

    await injectPendingGrant(created);
    await browser.reloadObsidian();
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

    await editFixtureNote();
    for (let seconds = 0; seconds <= 30; seconds += 2) {
      await browser.pause(2_000);
      console.log(`STATUS_T_PLUS_${seconds}`, await readStatusBarText());
      if (seconds === 10) {
        await browser.execute(() => {
          const app = (
            window as unknown as {
              app: { commands: { executeCommandById: (id: string) => void } };
            }
          ).app;
          app.commands.executeCommandById("knowledge-workspace:sync-now");
        });
        console.log("SYNC_NOW_TRIGGERED");
      }
    }

    const data = await readPluginData();
    console.log("PENDING_GRANT_FINAL", data.pending_grant === null ? "cleared" : "still-pending");

    const journalDump = await browser.execute(
      async (dataPathSuffix: string) => {
        const app = (
          window as unknown as {
            app: {
              vault: {
                configDir: string;
                adapter: {
                  list: (path: string) => Promise<{ files: string[] }>;
                  readBinary: (path: string) => Promise<ArrayBuffer>;
                };
              };
            };
          }
        ).app;
        const pluginDir = `${app.vault.configDir}/plugins/knowledge-workspace`;
        void dataPathSuffix;
        const listing = await app.vault.adapter.list(pluginDir);
        const manifest = JSON.parse(
          await app.vault.adapter.read(`${pluginDir}/journal.manifest.json`),
        ) as { current?: { generationNumber?: unknown } };
        const generationNumber = manifest.current?.generationNumber;
        if (!Number.isSafeInteger(generationNumber) || generationNumber < 0) {
          return { error: "invalid journal manifest", files: listing.files };
        }
        const journalName = `journal.sqlite.g${generationNumber}`;
        if (!listing.files.some((file) => file.endsWith(`/${journalName}`))) {
          return { error: "no journal generation", files: listing.files };
        }
        const bytes = new Uint8Array(
          await app.vault.adapter.readBinary(`${pluginDir}/${journalName}`),
        );
        let binary = "";
        for (let offset = 0; offset < bytes.length; offset += 0x8000) {
          binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
        }
        return { base64: btoa(binary) };
      },
      pluginDataPathSuffix,
    );
    if ("error" in journalDump || journalDump.base64 === undefined) {
      console.log("JOURNAL_EVIDENCE_AVAILABLE", false);
      throw new Error("sanitized journal evidence was unavailable");
    } else {
      const initSqlJs = (await import("sql.js")).default;
      const engine = await initSqlJs();
      const database = new engine.Database(Buffer.from(journalDump.base64, "base64"));
      const committed = database.exec(
        "select count(*) from journal_events where state = 'committed'",
      );
      const pending = database.exec(
        "select count(*) from journal_events where state not in ('committed', 'no_change', 'excluded_policy', 'blocked_conflict')",
      );
      const mapped = database.exec(
        "select count(*) from local_files where source_id is not null and base_version_id is not null",
      );
      const committedCount = Number(committed[0]?.values[0]?.[0] ?? 0);
      const pendingCount = Number(pending[0]?.values[0]?.[0] ?? 0);
      const mappedCount = Number(mapped[0]?.values[0]?.[0] ?? 0);
      console.log(
        "SANITIZED_JOURNAL_EVIDENCE",
        JSON.stringify({ committedCount, pendingCount, mappedCount }),
      );
      if (committedCount !== 1 || pendingCount !== 0 || mappedCount !== 1) {
        throw new Error("journal did not converge to exactly one committed mapped publication");
      }
    }
  });
});
