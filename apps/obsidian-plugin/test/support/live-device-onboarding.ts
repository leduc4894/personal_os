import * as crypto from "node:crypto";
import * as fs from "node:fs";
import { browser } from "@wdio/globals";
import { runFromE2eRepositoryRoot } from "./repository-subprocess";

const TOTP_CODE_PATTERN = /^[0-9]{6}$/;
let previousVerifiedTotpCode: string | null = null;

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
  /** A reauthorization is usable only after this accepted policy revision is cached. */
  readonly minimumPolicyRevision?: number;
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

/**
 * The live server treats a TOTP value as one-time within its time step. A
 * regression journey may authorize more than one test device in succession,
 * so wait locally for the helper to advance rather than replaying the value
 * that the preceding authorization already consumed. Codes stay in memory and
 * are never logged.
 */
async function readFreshTotpCode(totpHelper: string): Promise<string> {
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
  let verifyResponse: Response | null = null;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const totpCode = await readFreshTotpCode(options.totpHelper);
    verifyResponse = await fetch(`${options.serverOrigin}/api/auth/totp/verify`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: options.allowedOrigin,
        cookie: loginCookies.join("; "),
        "x-csrf-token": loginCsrf,
      },
      body: JSON.stringify({ code: totpCode }),
    });
    if (verifyResponse.ok) {
      previousVerifiedTotpCode = totpCode;
      break;
    }
    // The guarded bootstrap may have consumed this time-step's code in a
    // separate process immediately before WDIO begins. Treat only its closed
    // replay response as an instruction to wait for the next helper value;
    // every other failure remains visible to the journey.
    if (verifyResponse.status !== 401) {
      throw new Error(`TOTP verification failed: ${verifyResponse.status}`);
    }
    previousVerifiedTotpCode = totpCode;
  }
  if (verifyResponse === null || !verifyResponse.ok) {
    throw new Error(`TOTP verification failed: ${verifyResponse?.status ?? 0}`);
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
    const onboardingState = await browser.execute(async (dataPathSuffix: string) => {
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
      const cachedPolicy = data["policy_cache"];
      const revisionNumber =
        typeof cachedPolicy === "object" && cachedPolicy !== null && !Array.isArray(cachedPolicy)
          ? (cachedPolicy as Record<string, unknown>)["revision_number"]
          : null;
      return {
        isPending: data["pending_grant"] !== null && data["pending_grant"] !== undefined,
        revisionNumber: typeof revisionNumber === "number" && Number.isInteger(revisionNumber)
          ? revisionNumber
          : null,
      };
    }, options.pluginDataPathSuffix);
    const hasRequiredPolicy =
      options.minimumPolicyRevision === undefined ||
      (onboardingState.revisionNumber !== null &&
        onboardingState.revisionNumber >= options.minimumPolicyRevision);
    if (!onboardingState.isPending && hasRequiredPolicy) {
      const isJournalReady = await browser.execute(async () => {
        const app = (
          window as unknown as {
            app: {
              vault: {
                configDir: string;
                adapter: { exists: (path: string) => Promise<boolean> };
              };
            };
          }
        ).app;
        return app.vault.adapter.exists(
          `${app.vault.configDir}/plugins/knowledge-workspace/journal.manifest.json`,
        );
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
