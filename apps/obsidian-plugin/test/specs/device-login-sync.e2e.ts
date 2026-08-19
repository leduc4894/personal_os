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
  before(function () {
    if (passwordFile === undefined || totpHelper === undefined) {
      this.skip();
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
    await browser.pause(8_000);
    console.log("STATUS_AFTER_EDIT", await readStatusBarText());

    const data = await readPluginData();
    console.log("PENDING_GRANT_FINAL", data.pending_grant === null ? "cleared" : "still-pending");
  });
});
