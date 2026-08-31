import { expect, type Page, type Route } from "@playwright/test";
import { test } from "@playwright/test";
import type { components } from "@workspace/api-client";

import {
  E2E_ACCEPTED_LOGIN_PASSWORD,
  E2E_LOGIN_USERNAME,
} from "../authentication/e2e-credentials";

/**
 * The complete exclusion-policy publication journey across the Web Admin
 * boundary (spec 17/23.5): the operator signs in, sees the fail-closed
 * no-policy status, previews the empty draft, publishes revision 1 with the
 * exact typed confirmation, then adds a deny rule, previews the deny impact
 * and publishes revision 2, ending on the visible active status. The API is
 * intercepted with page.route at the exact OpenAPI contract paths and
 * payloads, so the journey also pins request fidelity — the CSRF double
 * submit on writes, the idempotency key header on publication and the exact
 * binding body the generated client emits — against the committed contract.
 */

const REQUEST_ID = "e2e-00000000-0000-4000-8000-000000000013";

const SESSION_COOKIES = ["admin_session_local=e2e-session-value; Path=/; SameSite=Lax"];
const CSRF_COOKIE = ["admin_csrf_local=e2e-csrf-value; Path=/; SameSite=Lax"];

const WORKSPACE_ID = "1a0b2c3d-0000-4000-8000-000000000001";
const DRAFT_ID = "2b1c3d4e-0000-4000-8000-000000000002";
const PREVIEW_ID_EMPTY = "4d3e5f6a-0000-4000-8000-000000000004";
const PREVIEW_ID_DENY = "5e4f6a7b-0000-4000-8000-000000000005";
const REVISION_ID_EMPTY = "6f5a7b8c-0000-4000-8000-000000000006";
const REVISION_ID_DENY = "7a6b8c9d-0000-4000-8000-000000000007";
const DENIED_SOURCE_ID = "9d8e7f6a-0000-4000-8000-000000000009";
const SIGNING_KEY_ID = "ed25519-sha256-43base64urlkeyidaaaaaaaaaaaaaaaaaa";

const FINGERPRINT = "a".repeat(64);
const DRAFT_SHA_EMPTY = "b".repeat(64);
const DRAFT_SHA_DENY = "c".repeat(64);
const IMPACT_DIGEST = "d".repeat(64);
const PUBLISH_CONFIRMATION_PHRASE = "PUBLISH EXCLUSION POLICY";

function jsonResponseHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return { "content-type": "application/json", "cache-control": "no-store", ...extra };
}

function envelopeBody(data: unknown): string {
  return JSON.stringify({ data, error: null, request_id: REQUEST_ID, warnings: [] });
}

function errorEnvelopeBody(code: string): string {
  return JSON.stringify({
    data: null,
    error: { code, details: {}, message: `Simulated ${code}.`, retryable: false },
    request_id: REQUEST_ID,
    warnings: [],
  });
}

function activeSession(): Record<string, unknown> {
  return {
    absolute_expires_at: "2026-08-18T00:00:00Z",
    authenticated: true,
    idle_expires_at: "2026-08-17T13:00:00Z",
    scopes: ["exclusion_policy_admin"],
    state: "active",
  };
}

function policyStatus(activeRevision: 0 | 1 | 2, denyRuleId: string) {
  const activeRevisionId = activeRevision === 1 ? REVISION_ID_EMPTY : REVISION_ID_DENY;
  const activePolicyRevisionId = activeRevision === 0 ? null : activeRevisionId;
  const rules: components["schemas"]["PolicyRuleData"][] =
    activeRevision === 2
      ? [
          {
            rule_id: denyRuleId,
            rule_kind: "folder_prefix",
            semantic_fingerprint: FINGERPRINT,
            folder_prefix: "notes/private",
          },
        ]
      : [];
  return {
    active_policy_revision_id: activePolicyRevisionId,
    active_revision_number: activeRevision,
    draft: {
      draft_id: DRAFT_ID,
      draft_version: activeRevision === 2 ? 2 : 1,
      base_policy_revision_id: activePolicyRevisionId,
      rules,
    },
    // The real composition returns null while no reconciliation intent exists
    // (an unpublished workspace) and always carries the safe-error verdict.
    reconciliation:
      activeRevision === 0
        ? null
        : {
            policy_revision_id: activeRevisionId,
            state: "running",
            updated_at: "2026-08-17T12:30:00Z",
            safe_error_code: null,
          },
    stale_running_previews: null,
  } satisfies components["schemas"]["ExclusionPolicyStatusData"];
}

function readyPreview(options: {
  policyPreviewId: string;
  draftVersion: number;
  draftSha256: string;
  basePolicyRevisionId: string | null;
  counters: Record<string, number>;
  results: Record<string, unknown>[];
  matchedRuleId?: string;
}): Record<string, unknown> {
  const firstResult: Record<string, unknown> = {
    source_id: DENIED_SOURCE_ID,
    previous_raw_decision: "allowed",
    previous_enforced_decision: "allowed",
    proposed_raw_decision: "excluded",
    proposed_enforced_decision: "excluded",
    proposed_match_state: "matched",
    impact_class: "newly_excluded",
    matched_rule_ids: options.matchedRuleId === undefined ? [] : [options.matchedRuleId],
    missing_fields: [],
  };
  return {
    policy_preview_id: options.policyPreviewId,
    status: "ready",
    policy_draft_id: DRAFT_ID,
    draft_version: options.draftVersion,
    draft_sha256: options.draftSha256,
    base_policy_revision_id: options.basePolicyRevisionId,
    source_checkpoint_event_sequence: 42,
    created_at: "2026-08-17T12:00:00Z",
    ready_at: "2026-08-17T12:00:05Z",
    expires_at: "2026-08-17T12:15:05Z",
    consumed_at: null,
    impact_digest: IMPACT_DIGEST,
    safe_error_code: null,
    counters: {
      newly_excluded_count: 0,
      still_excluded_count: 0,
      newly_allowed_count: 0,
      still_allowed_count: 0,
      indeterminate_count: 0,
      ...options.counters,
    },
    results: options.results.length > 0 ? [firstResult] : [],
    next_cursor: null,
  };
}

function publication(revision: 1 | 2): Record<string, unknown> {
  return {
    workspace_id: WORKSPACE_ID,
    policy_revision_id: revision === 1 ? REVISION_ID_EMPTY : REVISION_ID_DENY,
    revision_number: revision,
    parent_policy_revision_id: revision === 1 ? null : REVISION_ID_EMPTY,
    payload_sha256: revision === 1 ? DRAFT_SHA_EMPTY : DRAFT_SHA_DENY,
    signing_key_id: SIGNING_KEY_ID,
    published_at: "2026-08-17T12:01:00Z",
    rule_count: revision === 1 ? 0 : 1,
    reconciliation_status: "running",
    is_replay: false,
  };
}

interface CapturedApiCall {
  method: string;
  path: string;
  body: string | null;
  csrfToken: string | undefined;
  idempotencyKey: string | undefined;
}

type RouteFulfillment = (route: Route) => Promise<void>;

function fulfill(body: string, status = 200, headers: Record<string, string> = {}): RouteFulfillment {
  return async (route) => {
    await route.fulfill({ status, headers: jsonResponseHeaders(headers), body });
  };
}

/**
 * The publication journey with a request-capturing, stateful API surface.
 * The workspace walks the exact Admin state machine: uninitialized → active
 * revision 1 (empty policy) → active revision 2 (one folder_prefix deny).
 * The deny rule keeps the client-generated rule id end to end.
 */
class PolicyPublicationJourney {
  readonly calls: CapturedApiCall[] = [];
  denyRuleId = "3c2d4e5f-0000-4000-8000-000000000003";
  private activeRevision: 0 | 1 | 2 = 0;
  private isSignedIn = false;

  constructor(private readonly page: Page) {}

  async install(): Promise<void> {
    await this.page.route("**/api/auth/session", async (route) => {
      await route.fulfill({
        status: this.isSignedIn ? 200 : 401,
        headers: jsonResponseHeaders(),
        body: this.isSignedIn
          ? envelopeBody(activeSession())
          : errorEnvelopeBody("authentication_required"),
      });
    });
    await this.page.route("**/api/auth/login", async (route) => {
      this.calls.push({
        method: "POST",
        path: "/api/auth/login",
        body: route.request().postData(),
        csrfToken: undefined,
        idempotencyKey: undefined,
      });
      this.isSignedIn = true;
      await route.fulfill({
        status: 200,
        headers: jsonResponseHeaders({
          "set-cookie": [...SESSION_COOKIES, ...CSRF_COOKIE].join("\n"),
        }),
        body: envelopeBody(activeSession()),
      });
    });
    // No TOTP enrollment offer: the login completes straight to the redirect.
    await this.page.route(
      "**/api/auth/totp/enrollments",
      fulfill(errorEnvelopeBody("totp_not_enrolled"), 404),
    );
    await this.page.route("**/api/admin/devices", fulfill(envelopeBody({ devices: [] })));

    await this.page.route("**/api/admin/exclusion-policy", async (route) => {
      this.calls.push({
        method: "GET",
        path: "/api/admin/exclusion-policy",
        body: null,
        csrfToken: route.request().headers()["x-csrf-token"],
        idempotencyKey: undefined,
      });
      await route.fulfill({
        status: this.isSignedIn ? 200 : 401,
        headers: jsonResponseHeaders(),
        body: this.isSignedIn
          ? envelopeBody(policyStatus(this.activeRevision, this.denyRuleId))
          : errorEnvelopeBody("authentication_required"),
      });
    });
    await this.page.route("**/api/admin/exclusion-policy/draft", async (route) => {
      this.calls.push({
        method: "PUT",
        path: "/api/admin/exclusion-policy/draft",
        body: route.request().postData(),
        csrfToken: route.request().headers()["x-csrf-token"],
        idempotencyKey: undefined,
      });
      const requestBody = JSON.parse(route.request().postData() ?? "{}") as {
        rules?: { rule_id?: string; rule_kind?: string }[];
      };
      const denyRule = (requestBody.rules ?? []).find((rule) => rule.rule_kind === "folder_prefix");
      if (denyRule !== undefined && denyRule.rule_id !== undefined) {
        this.denyRuleId = denyRule.rule_id;
      }
      await route.fulfill({
        status: 200,
        headers: jsonResponseHeaders(),
        body: envelopeBody({
          draft_id: DRAFT_ID,
          draft_version: 2,
          base_policy_revision_id:
            this.activeRevision === 0 ? null : REVISION_ID_EMPTY,
          rules: [
            {
              rule_id: this.denyRuleId,
              rule_kind: "folder_prefix",
              semantic_fingerprint: FINGERPRINT,
              folder_prefix: "notes/private",
            },
          ],
        }),
      });
    });
    await this.page.route("**/api/admin/exclusion-policy/previews", async (route) => {
      this.calls.push({
        method: "POST",
        path: "/api/admin/exclusion-policy/previews",
        body: route.request().postData(),
        csrfToken: route.request().headers()["x-csrf-token"],
        idempotencyKey: undefined,
      });
      if (this.activeRevision === 0) {
        await route.fulfill({
          status: 200,
          headers: jsonResponseHeaders(),
          body: envelopeBody(
            readyPreview({
              policyPreviewId: PREVIEW_ID_EMPTY,
              draftVersion: 1,
              draftSha256: DRAFT_SHA_EMPTY,
              basePolicyRevisionId: null,
              counters: { still_allowed_count: 128 },
              results: [],
            }),
          ),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        headers: jsonResponseHeaders(),
        body: envelopeBody(
          readyPreview({
            policyPreviewId: PREVIEW_ID_DENY,
            draftVersion: 2,
            draftSha256: DRAFT_SHA_DENY,
            basePolicyRevisionId: REVISION_ID_EMPTY,
            counters: { newly_excluded_count: 3, still_allowed_count: 125 },
            results: [{ placeholder: true }],
            matchedRuleId: this.denyRuleId,
          }),
        ),
      });
    });
    await this.page.route("**/api/admin/exclusion-policy/publications", async (route) => {
      this.calls.push({
        method: "POST",
        path: "/api/admin/exclusion-policy/publications",
        body: route.request().postData(),
        csrfToken: route.request().headers()["x-csrf-token"],
        idempotencyKey: route.request().headers()["x-idempotency-key"],
      });
      const published = this.activeRevision === 0 ? 1 : 2;
      this.activeRevision = published;
      await route.fulfill({
        status: 201,
        headers: jsonResponseHeaders(),
        body: envelopeBody(publication(published)),
      });
    });
  }

  publicationCalls(): CapturedApiCall[] {
    return this.calls.filter((call) => call.path === "/api/admin/exclusion-policy/publications");
  }
}

test("the policy publication journey publishes the empty policy then a deny rule", async ({ page }) => {
  const journey = new PolicyPublicationJourney(page);
  await journey.install();

  // The unauthenticated operator is routed to the login page first.
  await page.goto("/admin/policy");
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
  await page.getByLabel("Username").fill(E2E_LOGIN_USERNAME);
  await page.getByLabel("Password").fill(E2E_ACCEPTED_LOGIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  // The login completes to the device list; the operator opens the policy
  // Admin page and sees the fail-closed no-policy status.
  await expect(page).toHaveURL(/\/admin\/devices/);
  await page.goto("/admin/policy");
  await expect(page.getByRole("heading", { name: "Exclusion policy" })).toBeVisible();
  await expect(page.getByText("No exclusion policy is published yet.")).toBeVisible();

  // Preview of the empty draft: the ready preview renders its counters and
  // the exact draft/checkpoint binding.
  await page.getByRole("button", { name: "Preview impact" }).click();
  const emptyPreview = page.locator(".policy-preview");
  await expect(emptyPreview).toContainText("Newly excluded");
  await expect(emptyPreview.getByText("Preview bound to draft version 1, checkpoint 42.")).toBeVisible();
  await expect(emptyPreview.locator("tbody tr")).toHaveCount(0);

  // Publication requires the exact typed confirmation.
  await emptyPreview.getByRole("button", { name: "Publish…" }).click();
  const dialog = page.getByRole("dialog", { name: "Publish exclusion policy" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("This publishes revision 0 → 1")).toBeVisible();
  await expect(dialog.getByText("Newly excluded 0 · Newly allowed 0 · Unchanged 128 · Indeterminate 0")).toBeVisible();
  const confirmInput = dialog.getByLabel("Type PUBLISH EXCLUSION POLICY to confirm");
  await confirmInput.fill("publish exclusion policy");
  await expect(dialog.getByRole("button", { name: "Publish policy" })).toBeDisabled();
  await confirmInput.fill(PUBLISH_CONFIRMATION_PHRASE);

  await dialog.getByRole("button", { name: "Publish policy" }).click();
  const statusCard = page.locator(".policy-status");
  await expect(statusCard).toContainText("Published revision 1 · 0 rules · reconciliation running.");
  await expect(statusCard).toContainText("Active policy revision");
  await expect(statusCard).toContainText("Reconciliation running — updated 2026-08-17T12:30:00Z");

  // The deny rule: one folder_prefix rule is added and explicitly saved.
  await page.getByLabel("Rule kind").selectOption("folder_prefix");
  await page.getByRole("button", { name: "Add rule" }).click();
  await page.getByLabel("Folder prefix").fill("notes/private");
  await expect(page.getByText(/You have unsaved changes: 1 added/)).toBeVisible();
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText("Draft saved.")).toBeVisible();

  // Preview of the deny rule shows the newly excluded impact and one row.
  await page.getByRole("button", { name: "Preview impact" }).click();
  const denyPreview = page.locator(".policy-preview");
  await expect(denyPreview.getByText("Preview bound to draft version 2, checkpoint 42.")).toBeVisible();
  const deniedRow = denyPreview.getByRole("row", { name: new RegExp(DENIED_SOURCE_ID) });
  await expect(deniedRow).toBeVisible();
  await expect(deniedRow.getByText("newly excluded")).toBeVisible();
  await expect(deniedRow.getByText("allowed → excluded")).toBeVisible();

  // The second publication binds revision 1 → 2 and the active status moves.
  await denyPreview.getByRole("button", { name: "Publish…" }).click();
  const denyDialog = page.getByRole("dialog", { name: "Publish exclusion policy" });
  await expect(denyDialog.getByText("This publishes revision 1 → 2")).toBeVisible();
  await denyDialog
    .getByLabel("Type PUBLISH EXCLUSION POLICY to confirm")
    .fill(PUBLISH_CONFIRMATION_PHRASE);
  await denyDialog.getByRole("button", { name: "Publish policy" }).click();
  await expect(statusCard).toContainText("Published revision 2 · 1 rule · reconciliation running.");
  const revisionEntry = statusCard.locator("dl div", { hasText: "Active policy revision" });
  await expect(revisionEntry).toContainText("2", { useInnerText: true });

  // Request fidelity: both publications carried the CSRF token, exactly one
  // printable idempotency key each, and the exact contract binding bodies.
  const publications = journey.publicationCalls();
  expect(publications).toHaveLength(2);
  for (const call of publications) {
    expect(call.csrfToken).toBe("e2e-csrf-value");
    expect(call.idempotencyKey).toMatch(/^[!-~]{1,200}$/);
  }
  expect(new Set(publications.map((call) => call.idempotencyKey)).size).toBe(2);
  expect(JSON.parse(publications[0]!.body ?? "{}")).toEqual({
    confirmation: PUBLISH_CONFIRMATION_PHRASE,
    expected_active_policy_revision_id: null,
    expected_active_revision_number: 0,
    expected_draft_sha256: DRAFT_SHA_EMPTY,
    expected_draft_version: 1,
    policy_draft_id: DRAFT_ID,
    policy_preview_id: PREVIEW_ID_EMPTY,
    preview_impact_digest: IMPACT_DIGEST,
  });
  expect(JSON.parse(publications[1]!.body ?? "{}")).toEqual({
    confirmation: PUBLISH_CONFIRMATION_PHRASE,
    expected_active_policy_revision_id: REVISION_ID_EMPTY,
    expected_active_revision_number: 1,
    expected_draft_sha256: DRAFT_SHA_DENY,
    expected_draft_version: 2,
    policy_draft_id: DRAFT_ID,
    policy_preview_id: PREVIEW_ID_DENY,
    preview_impact_digest: IMPACT_DIGEST,
  });
  const draftSave = journey.calls.filter((call) => call.path === "/api/admin/exclusion-policy/draft");
  expect(draftSave).toHaveLength(1);
  expect(draftSave[0]!.csrfToken).toBe("e2e-csrf-value");
  expect(JSON.parse(draftSave[0]!.body ?? "{}")).toEqual({
    expected_draft_version: 1,
    rules: [
      {
        rule_id: journey.denyRuleId,
        rule_kind: "folder_prefix",
        folder_prefix: "notes/private",
      },
    ],
  });

  // No rule operand or preview evidence persisted anywhere in the browser.
  const storageState = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(storageState).toEqual({ local: 0, session: 0 });
});
