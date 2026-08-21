import * as crypto from "node:crypto";
import * as fs from "node:fs";
import { browser } from "@wdio/globals";
import { runFromE2eRepositoryRoot } from "./repository-subprocess";

interface CreatedGrant {
  readonly grant_id: string;
  readonly user_code: string;
  readonly polling_secret: string;
  readonly verification_uri: string;
  readonly expires_in_seconds: number;
  readonly poll_interval_seconds: number;
}

export interface LiveDeviceOnboardingOptions {
  readonly serverOrigin: string;
  readonly allowedOrigin: string;
  readonly webUsername: string;
  readonly passwordFile: string;
  readonly totpHelper: string;
  readonly pluginDataPathSuffix: string;
  readonly deviceName: string;
}

export interface LiveDeviceOnboardingResult {
  readonly adminSessionCookies: readonly string[];
  readonly adminSessionCsrf: string;
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

async function injectPendingGrant(
  grant: CreatedGrant,
  pluginDataPathSuffix: string,
): Promise<void> {
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
            secretStorage: { setSecret: (key: string, value: string) => Promise<void> };
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

export async function onboardLiveDevice(
  options: LiveDeviceOnboardingOptions,
): Promise<LiveDeviceOnboardingResult> {
  const createResponse = await fetch(`${options.serverOrigin}/api/auth/device-authorizations`, {
    method: "POST",
    headers: { "content-type": "application/json", origin: options.allowedOrigin },
    body: JSON.stringify({
      client_instance_id: crypto.randomUUID(),
      device_name: options.deviceName,
      platform_class: "obsidian_desktop",
      platform_name: "windows",
      plugin_version: "0.1.0",
      requested_scope: "obsidian_sync",
    }),
  });
  if (!createResponse.ok) {
    throw new Error(`device grant creation failed: ${createResponse.status}`);
  }
  const grant = ((await createResponse.json()) as { data: CreatedGrant }).data;

  const password = fs.readFileSync(options.passwordFile, "utf8").trim();
  const loginResponse = await fetch(`${options.serverOrigin}/api/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json", origin: options.allowedOrigin },
    body: JSON.stringify({ username: options.webUsername, password }),
  });
  if (!loginResponse.ok) {
    throw new Error(`admin login failed: ${loginResponse.status}`);
  }
  const loginCookies = cookiePairsOf(loginResponse);
  const loginCsrf = csrfValueOf(loginCookies) ?? "";
  const { stdout: totpStdout } = await runFromE2eRepositoryRoot(
    "uv",
    ["run", "python", options.totpHelper],
    import.meta.url,
  );
  const verifyResponse = await fetch(`${options.serverOrigin}/api/auth/totp/verify`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      origin: options.allowedOrigin,
      cookie: loginCookies.join("; "),
      "x-csrf-token": loginCsrf,
    },
    body: JSON.stringify({ code: totpStdout.trim() }),
  });
  if (!verifyResponse.ok) {
    throw new Error(`TOTP verification failed: ${verifyResponse.status}`);
  }
  const sessionCookies = cookiePairsOf(verifyResponse);
  const sessionCsrf = csrfValueOf(sessionCookies) ?? "";
  const approveResponse = await fetch(
    `${options.serverOrigin}/api/auth/device-authorizations/${grant.grant_id}/approve`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: options.allowedOrigin,
        cookie: sessionCookies.join("; "),
        "x-csrf-token": sessionCsrf,
      },
    },
  );
  if (!approveResponse.ok) {
    throw new Error(`device grant approval failed: ${approveResponse.status}`);
  }

  await injectPendingGrant(grant, options.pluginDataPathSuffix);
  await reloadKnowledgeWorkspacePlugin();
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await browser.pause(1_000);
    const isPending = await browser.execute(async (dataPathSuffix: string) => {
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
      const data = JSON.parse(raw) as Record<string, unknown>;
      return data["pending_grant"] !== null && data["pending_grant"] !== undefined;
    }, options.pluginDataPathSuffix);
    if (!isPending) {
      const isJournalReady = await browser.execute(() => {
        const app = (
          window as unknown as {
            app: {
              commands: { listCommands: () => Array<{ id: string }> };
            };
          }
        ).app;
        return app.commands
          .listCommands()
          .some((command) => command.id === "knowledge-workspace:sync-now");
      });
      if (isJournalReady) {
        return {
          adminSessionCookies: sessionCookies,
          adminSessionCsrf: sessionCsrf,
        };
      }
    }
  }
  throw new Error("device onboarding did not converge");
}
