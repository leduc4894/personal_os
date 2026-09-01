import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";

import type { components } from "@workspace/api-client";

import type { AuthenticationCallResult } from "../../api/authentication-client";
import type { PolicyEditorClient } from "../../api/exclusion-policy-client";
import { PolicyEditor } from "./PolicyEditor";

/**
 * The editor owns the spec 17 state machine: local draft edits until an
 * explicit save, the server draft version as concurrency token, bounded
 * polling of pending previews only, publication gating against the saved
 * draft and current status, and closed generic failure copy that never
 * renders secret or provider detail.
 */

const routerMock = vi.hoisted(() => ({
  replace: vi.fn(),
  push: vi.fn(),
  refresh: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => routerMock,
}));
const replaceMock = routerMock.replace;

type PolicyStatusDataSchema = components["schemas"]["ExclusionPolicyStatusData"];
type PolicyPreviewDataSchema = components["schemas"]["PolicyPreviewData"];
type PolicyDraftDataSchema = components["schemas"]["PolicyDraftData"];
type PolicyPublicationDataSchema = components["schemas"]["PolicyPublicationData"];
type ApiErrorBody = components["schemas"]["ApiErrorBody"];

const DRAFT_ID = "0f9e8d7c-6b5a-4c3d-2e1f-0a1b2c3d4e5f";
const ACTIVE_REVISION_ID = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f";
const PREVIEW_ID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";

function errorResult(
  code: components["schemas"]["ErrorCode"],
  message = `Simulated ${code} failure.`,
): { ok: false; error: ApiErrorBody } {
  return { ok: false, error: { code, details: {}, message, retryable: false } };
}

function statusData(overrides: Partial<PolicyStatusDataSchema> = {}): PolicyStatusDataSchema {
  return {
    active_policy_revision_id: ACTIVE_REVISION_ID,
    active_revision_number: 3,
    draft: {
      base_policy_revision_id: ACTIVE_REVISION_ID,
      draft_id: DRAFT_ID,
      draft_version: 4,
      rules: [],
    },
    reconciliation: {
      policy_revision_id: ACTIVE_REVISION_ID,
      state: "running",
      updated_at: "2026-08-17T08:00:00Z",
      safe_error_code: null,
    },
    stale_running_previews: null,
    ...overrides,
  };
}

function savedDraftData(rules: PolicyDraftDataSchema["rules"] = [], version = 4): PolicyDraftDataSchema {
  return { base_policy_revision_id: ACTIVE_REVISION_ID, draft_id: DRAFT_ID, draft_version: version, rules };
}

function ruleData(overrides: Partial<components["schemas"]["PolicyRuleData"]> = {}): components["schemas"]["PolicyRuleData"] {
  return {
    rule_id: "3b4c5d6e-7f8a-4b9c-0d1e-2f3a4b5c6d7e",
    rule_kind: "folder_prefix",
    folder_prefix: "private",
    semantic_fingerprint: "f".repeat(64),
    ...overrides,
  };
}

function previewData(overrides: Partial<PolicyPreviewDataSchema> = {}): PolicyPreviewDataSchema {
  return {
    base_policy_revision_id: ACTIVE_REVISION_ID,
    consumed_at: null,
    counters: {
      indeterminate_count: 0,
      newly_allowed_count: 2,
      newly_excluded_count: 1,
      still_allowed_count: 7,
      still_excluded_count: 3,
    },
    created_at: "2026-08-17T08:00:00Z",
    draft_sha256: "a".repeat(64),
    draft_version: 4,
    expires_at: "2026-08-17T08:20:00Z",
    impact_digest: "c".repeat(64),
    policy_draft_id: DRAFT_ID,
    policy_preview_id: PREVIEW_ID,
    ready_at: null,
    results: null,
    safe_error_code: null,
    source_checkpoint_event_sequence: 12,
    status: "pending",
    ...overrides,
  };
}

function publicationData(overrides: Partial<PolicyPublicationDataSchema> = {}): PolicyPublicationDataSchema {
  return {
    is_replay: false,
    parent_policy_revision_id: ACTIVE_REVISION_ID,
    payload_sha256: "b".repeat(64),
    policy_revision_id: "6f7a8b9c-0d1e-4f2a-3b4c-5d6e7f8a9b0c",
    published_at: "2026-08-17T08:05:00Z",
    reconciliation_status: "pending",
    revision_number: 4,
    rule_count: 1,
    signing_key_id: "key-2026-08",
    workspace_id: "9a8b7c6d-5e4f-4a3b-2c1d-0e9f8a7b6c5d",
    ...overrides,
  };
}

interface FakeClientCalls {
  getStatus?: PolicyStatusDataSchema | { ok: false; error: ApiErrorBody };
  replaceDraft?: PolicyDraftDataSchema | { ok: false; error: ApiErrorBody };
  createPreview?: PolicyPreviewDataSchema | { ok: false; error: ApiErrorBody };
  getPreview?:
    | (PolicyPreviewDataSchema | { ok: false; error: ApiErrorBody })[]
    | { ok: false; error: ApiErrorBody };
  publish?: PolicyPublicationDataSchema | { ok: false; error: ApiErrorBody };
}

const OPERAND_FIELDS: Readonly<Record<string, string>> = {
  exact_source_id: "source_id",
  folder_prefix: "folder_prefix",
  path_glob: "path_glob",
  extension: "extension",
  media_type: "media_type",
  maximum_size: "maximum_size_bytes",
  source_type: "source_type",
};

function ruleDataFromRequest(
  rule: components["schemas"]["PolicyDraftRuleRequest"],
): components["schemas"]["PolicyRuleData"] {
  const operandField = OPERAND_FIELDS[rule.rule_kind]!;
  return {
    rule_id: rule.rule_id,
    rule_kind: rule.rule_kind,
    semantic_fingerprint: "0".repeat(64),
    [operandField]: (rule as unknown as Record<string, unknown>)[operandField],
  } as components["schemas"]["PolicyRuleData"];
}

function createFakeClient(calls: FakeClientCalls): {
  client: PolicyEditorClient;
  mocks: {
    getStatus: Mock<() => Promise<AuthenticationCallResult<PolicyStatusDataSchema>>>;
    replaceDraft: Mock<
      (
        input: {
          expectedDraftVersion: number;
          rules: readonly components["schemas"]["PolicyDraftRuleRequest"][];
        },
      ) => Promise<AuthenticationCallResult<PolicyDraftDataSchema>>
    >;
    createPreview: Mock<() => Promise<AuthenticationCallResult<PolicyPreviewDataSchema>>>;
    getPreview: Mock<() => Promise<AuthenticationCallResult<PolicyPreviewDataSchema>>>;
    publish: Mock<
      (
        input: {
          request: components["schemas"]["PolicyPublicationRequest"];
          idempotencyKey: string;
        },
      ) => Promise<AuthenticationCallResult<PolicyPublicationDataSchema>>
    >;
  };
} {
  const statusQueue = Array.isArray(calls.getStatus) ? [...calls.getStatus] : calls.getStatus ? [calls.getStatus] : [];
  const getStatusMock = vi.fn((): Promise<AuthenticationCallResult<PolicyStatusDataSchema>> => {
    const next = statusQueue.length > 1 ? statusQueue.shift() : (statusQueue[0] ?? statusData());
    return Promise.resolve(
      "draft" in next ? { ok: true, data: next as PolicyStatusDataSchema } : (next as { ok: false; error: ApiErrorBody }),
    );
  });
  const replaceDraftMock = vi.fn(
    (input: {
      expectedDraftVersion: number;
      rules: readonly components["schemas"]["PolicyDraftRuleRequest"][];
    }): Promise<AuthenticationCallResult<PolicyDraftDataSchema>> => {
      if (calls.replaceDraft) {
        const next = calls.replaceDraft;
        return Promise.resolve(
          "draft_id" in next
            ? { ok: true, data: next as PolicyDraftDataSchema }
            : (next as { ok: false; error: ApiErrorBody }),
        );
      }
      const rules = input.rules.map(ruleDataFromRequest);
      return Promise.resolve({ ok: true, data: savedDraftData(rules, input.expectedDraftVersion + 1) });
    },
  );
  const createPreviewMock = vi.fn((): Promise<AuthenticationCallResult<PolicyPreviewDataSchema>> => {
    const next = calls.createPreview ?? previewData();
    return Promise.resolve(
      "policy_preview_id" in next
        ? { ok: true, data: next as PolicyPreviewDataSchema }
        : (next as { ok: false; error: ApiErrorBody }),
    );
  });
  type PreviewPage = PolicyPreviewDataSchema | { ok: false; error: ApiErrorBody };
  const previewPages: PreviewPage[] = Array.isArray(calls.getPreview)
    ? [...calls.getPreview]
    : calls.getPreview
      ? [calls.getPreview]
      : [];
  const getPreviewMock = vi.fn((): Promise<AuthenticationCallResult<PolicyPreviewDataSchema>> => {
    const next =
      previewPages.length > 0 ? previewPages.shift()! : previewData({ status: "pending" });
    return Promise.resolve(
      "policy_preview_id" in next
        ? { ok: true, data: next as PolicyPreviewDataSchema }
        : (next as { ok: false; error: ApiErrorBody }),
    );
  });
  const publishMock = vi.fn((): Promise<AuthenticationCallResult<PolicyPublicationDataSchema>> => {
    const next = calls.publish ?? publicationData();
    return Promise.resolve(
      "policy_revision_id" in next
        ? { ok: true, data: next as PolicyPublicationDataSchema }
        : (next as { ok: false; error: ApiErrorBody }),
    );
  });
  const client: PolicyEditorClient = {
    getExclusionPolicyStatus: getStatusMock,
    replaceExclusionPolicyDraft: replaceDraftMock,
    createExclusionPolicyPreview: createPreviewMock,
    getExclusionPolicyPreview: getPreviewMock,
    publishExclusionPolicy: publishMock,
    reauthenticate: vi.fn(async () => ({
      ok: true as const,
      data: {
        absolute_expires_at: "2026-08-17T12:00:00Z",
        authenticated: true,
        idle_expires_at: "2026-08-17T10:00:00Z",
        scopes: [],
        state: "active" as const,
      },
    })),
  };
  return {
    client,
    mocks: {
      getStatus: getStatusMock,
      replaceDraft: replaceDraftMock,
      createPreview: createPreviewMock,
      getPreview: getPreviewMock,
      publish: publishMock,
    },
  };
}

function renderEditor(client: PolicyEditorClient, pollIntervalMs = 5) {
  return render(<PolicyEditor client={client} pollIntervalMs={pollIntervalMs} />);
}

async function addRule(kind: string): Promise<void> {
  await userEvent.selectOptions(screen.getByLabelText(/rule kind/i), kind);
  await userEvent.click(screen.getByRole("button", { name: /add rule/i }));
}

afterEach(() => {
  replaceMock.mockReset();
});

describe("PolicyEditor", () => {
  it("announces loading, then renders the saved status, draft version and reconciliation", async () => {
    const { client } = createFakeClient({ getStatus: statusData() });
    renderEditor(client);
    expect(screen.getByRole("status", { name: /loading policy/i })).toBeInTheDocument();
    expect(await screen.findByText(/Active policy revision/)).toBeInTheDocument();
    expect(screen.getByText(/Reconciliation/)).toBeInTheDocument();
    expect(screen.getByText(/running/)).toBeInTheDocument();
    expect(screen.getByText(/Draft version 4/)).toBeInTheDocument();
  });

  it("shows explicit first-publication guidance when no policy is active", async () => {
    const { client } = createFakeClient({
      getStatus: statusData({ active_policy_revision_id: null, active_revision_number: 0, reconciliation: null }),
    });
    renderEditor(client);
    expect(await screen.findByText(/No exclusion policy is published yet/)).toBeInTheDocument();
    expect(screen.getByText(/Publishing the empty policy allows all current sources/)).toBeInTheDocument();
  });

  it("redirects to the login page when the status read is unauthenticated", async () => {
    const { client } = createFakeClient({ getStatus: errorResult("authentication_required") });
    renderEditor(client);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/login"));
  });

  it("labels every rule-kind control and the kind selector for keyboard use", async () => {
    const { client } = createFakeClient({ getStatus: statusData() });
    renderEditor(client);
    await screen.findByText(/Draft version 4/);
    const kindSelect = screen.getByLabelText(/rule kind/i);
    const options = within(kindSelect).getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "Exact source ID",
      "Folder prefix",
      "Path glob",
      "Extension",
      "Media type",
      "Maximum size",
      "Source type",
    ]);
  });

  // Parallel-coverage headroom: the seven sequential addRule round-trips run
  // ~1 s isolated but can exceed Vitest's 5 s default under full-suite load;
  // this raises the wall-clock budget only, re-investigate before raising again.
  it("creates labeled closed controls for each of the seven rule kinds", async () => {
    const { client } = createFakeClient({ getStatus: statusData() });
    renderEditor(client);
    await screen.findByText(/Draft version 4/);
    await addRule("exact_source_id");
    await addRule("folder_prefix");
    await addRule("path_glob");
    await addRule("extension");
    await addRule("media_type");
    await addRule("maximum_size");
    await addRule("source_type");
    expect(screen.getByRole("group", { name: /Rule 1: Exact source ID/ })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /Rule 2: Folder prefix/ })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /Rule 3: Path glob/ })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /Rule 4: Extension/ })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /Rule 5: Media type/ })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /Rule 6: Maximum size/ })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /Rule 7: Source type/ })).toBeInTheDocument();
    const sourceTypeGroup = screen.getByRole("group", { name: /Rule 7: Source type/ });
    const sourceTypeSelect = within(sourceTypeGroup).getByLabelText(/source type/i);
    expect(
      within(sourceTypeSelect)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["markdown", "text", "pdf", "image", "audio", "web", "youtube"]);
  }, 15_000);

  it("validates each operand grammar with closed feedback and blocks saving", async () => {
    const { client, mocks } = createFakeClient({ getStatus: statusData() });
    renderEditor(client);
    await screen.findByText(/Draft version 4/);

    await addRule("folder_prefix");
    await userEvent.type(within(screen.getByRole("group", { name: /Rule 1: Folder prefix/ })).getByLabelText(/folder prefix/i), "/private");
    expect(screen.getByText(/must be relative/)).toBeInTheDocument();

    await addRule("path_glob");
    await userEvent.type(
      within(screen.getByRole("group", { name: /Rule 2: Path glob/ })).getByLabelText(/path glob/i),
      "notes/?.md",
    );
    expect(screen.getByText(/only \* wildcards/)).toBeInTheDocument();

    await addRule("extension");
    await userEvent.type(within(screen.getByRole("group", { name: /Rule 3: Extension/ })).getByLabelText(/extension/i), "xlsx");
    expect(screen.getByText(/starting with a dot/)).toBeInTheDocument();

    await addRule("media_type");
    await userEvent.type(within(screen.getByRole("group", { name: /Rule 4: Media type/ })).getByLabelText(/media type/i), "text/plain; charset=utf-8");
    expect(screen.getByText(/without parameters/)).toBeInTheDocument();

    await addRule("maximum_size");
    await userEvent.type(
      within(screen.getByRole("group", { name: /Rule 5: Maximum size/ })).getByLabelText(/maximum size/i),
      "104857601",
    );
    expect(screen.getByText(/between 0 and 104857600/)).toBeInTheDocument();

    await addRule("exact_source_id");
    await userEvent.type(
      within(screen.getByRole("group", { name: /Rule 6: Exact source ID/ })).getByLabelText(/source id/i),
      "not-a-uuid",
    );
    expect(screen.getByText(/UUID form/)).toBeInTheDocument();

    const saveButton = screen.getByRole("button", { name: /save draft/i });
    expect(saveButton).toBeDisabled();
    expect(mocks.replaceDraft).not.toHaveBeenCalled();
  });

  it("normalizes ASCII case in extension operands before saving", async () => {
    const { client, mocks } = createFakeClient({ getStatus: statusData(), replaceDraft: savedDraftData([], 5) });
    renderEditor(client);
    await screen.findByText(/Draft version 4/);
    await addRule("extension");
    await userEvent.type(
      within(screen.getByRole("group", { name: /Rule 1: Extension/ })).getByLabelText(/extension/i),
      ".XLSX",
    );
    await userEvent.click(screen.getByRole("button", { name: /save draft/i }));
    await waitFor(() => expect(mocks.replaceDraft).toHaveBeenCalledTimes(1));
    const request = mocks.replaceDraft.mock.calls[0]![0];
    expect(request.expectedDraftVersion).toBe(4);
    expect(request.rules).toEqual([
      {
        rule_id: expect.any(String),
        rule_kind: "extension",
        extension: ".xlsx",
      },
    ]);
    expect(screen.getByText(/Draft saved/)).toBeInTheDocument();
    expect(screen.getByText(/Draft version 5/)).toBeInTheDocument();
  });

  it("rejects duplicate semantic rules with closed feedback", async () => {
    const { client } = createFakeClient({ getStatus: statusData() });
    renderEditor(client);
    await screen.findByText(/Draft version 4/);
    await addRule("folder_prefix");
    await userEvent.type(
      within(screen.getByRole("group", { name: /Rule 1: Folder prefix/ })).getByLabelText(/folder prefix/i),
      "private",
    );
    await addRule("folder_prefix");
    await userEvent.type(
      within(screen.getByRole("group", { name: /Rule 2: Folder prefix/ })).getByLabelText(/folder prefix/i),
      "private",
    );
    expect(screen.getByText(/An identical rule already exists/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save draft/i })).toBeDisabled();
  });

  it("reorders and removes rules without changing their stable rule IDs", async () => {
    const { client, mocks } = createFakeClient({ getStatus: statusData() });
    renderEditor(client);
    await screen.findByText(/Draft version 4/);
    await addRule("folder_prefix");
    await userEvent.type(
      within(screen.getByRole("group", { name: /Rule 1: Folder prefix/ })).getByLabelText(/folder prefix/i),
      "alpha",
    );
    await addRule("extension");
    await userEvent.type(
      within(screen.getByRole("group", { name: /Rule 2: Extension/ })).getByLabelText(/extension/i),
      ".md",
    );
    await addRule("path_glob");
    await userEvent.type(
      within(screen.getByRole("group", { name: /Rule 3: Path glob/ })).getByLabelText(/path glob/i),
      "beta/**",
    );
    await userEvent.click(screen.getByRole("button", { name: /save draft/i }));
    await waitFor(() => expect(mocks.replaceDraft).toHaveBeenCalledTimes(1));
    const firstIds = (mocks.replaceDraft.mock.calls[0]![0].rules ?? []).map((rule) => rule.rule_id);

    await userEvent.click(screen.getByRole("button", { name: /move rule 3 up/i }));
    await userEvent.click(screen.getByRole("button", { name: /move rule 2 up/i }));
    await userEvent.click(screen.getByRole("button", { name: /remove rule 2/i }));
    await userEvent.click(screen.getByRole("button", { name: /save draft/i }));
    await waitFor(() => expect(mocks.replaceDraft).toHaveBeenCalledTimes(2));
    const secondRequest = mocks.replaceDraft.mock.calls[1]![0];
    expect(secondRequest.rules.map((rule) => rule.rule_kind)).toEqual(["path_glob", "extension"]);
    expect(secondRequest.rules.map((rule) => rule.rule_id)).toEqual([firstIds[2], firstIds[1]]);
    expect(secondRequest.expectedDraftVersion).toBe(5);
  });

  it("keeps unsaved edits local and disables preview until an explicit save", async () => {
    const { client, mocks } = createFakeClient({ getStatus: statusData() });
    renderEditor(client);
    await screen.findByText(/Draft version 4/);
    expect(screen.getByRole("button", { name: /preview impact/i })).toBeEnabled();
    await addRule("folder_prefix");
    await userEvent.type(
      within(screen.getByRole("group", { name: /Rule 1: Folder prefix/ })).getByLabelText(/folder prefix/i),
      "private",
    );
    expect(screen.getByText(/unsaved changes/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /preview impact/i })).toBeDisabled();
    expect(mocks.replaceDraft).not.toHaveBeenCalled();
  });

  it("offers reload, never last-write-wins, on a two-tab draft conflict", async () => {
    let conflict = true;
    const { client, mocks } = createFakeClient({
      getStatus: statusData(),
      replaceDraft: errorResult("exclusion_policy_draft_conflict"),
    });
    mocks.replaceDraft.mockImplementation(() =>
      Promise.resolve(
        conflict
          ? errorResult("exclusion_policy_draft_conflict")
          : { ok: true, data: savedDraftData([ruleData({ folder_prefix: "other-tab" })], 6) },
      ),
    );
    const statusQueue = [statusData(), statusData({ draft: savedDraftData([ruleData({ folder_prefix: "other-tab" })], 6) })];
    mocks.getStatus.mockImplementation(() => {
      const next = statusQueue.length > 1 ? statusQueue.shift()! : (statusQueue[0] ?? statusData());
      return Promise.resolve({ ok: true, data: next });
    });
    renderEditor(client);
    await screen.findByText(/Draft version 4/);
    await addRule("folder_prefix");
    await userEvent.type(
      within(screen.getByRole("group", { name: /Rule 1: Folder prefix/ })).getByLabelText(/folder prefix/i),
      "mine",
    );
    await userEvent.click(screen.getByRole("button", { name: /save draft/i }));
    expect(await screen.findByText(/changed in another tab or window/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reload draft/i })).toBeInTheDocument();
    expect(screen.getByDisplayValue("mine")).toBeInTheDocument();

    conflict = false;
    await userEvent.click(screen.getByRole("button", { name: /reload draft/i }));
    expect(await screen.findByDisplayValue("other-tab")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("mine")).not.toBeInTheDocument();
    expect(mocks.getStatus).toHaveBeenCalledTimes(2);  });

  it("announces the running preview and keeps polling only while pending", async () => {
    const { client, mocks } = createFakeClient({
      getStatus: statusData(),
      createPreview: previewData(),
      getPreview: [
        previewData({ status: "running" }),
        previewData({ status: "pending" }),
        previewData({ status: "leased" }),
      ],
    });
    renderEditor(client, 5);
    await screen.findByText(/Draft version 4/);
    await userEvent.click(screen.getByRole("button", { name: /preview impact/i }));
    expect(await screen.findByRole("status", { name: /preview running/i })).toBeInTheDocument();
    await waitFor(() => expect(mocks.getPreview.mock.calls.length).toBeGreaterThanOrEqual(3));
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(mocks.getPreview.mock.calls.length).toBeGreaterThan(3);
    expect(screen.getByRole("status", { name: /preview running/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^publish/i })).not.toBeInTheDocument();
  });

  it("stops polling when the preview turns ready and enables publishing", async () => {
    const { client, mocks } = createFakeClient({
      getStatus: statusData(),
      createPreview: previewData(),
      getPreview: [previewData({ status: "running" }), previewData({ status: "ready", results: [] })],
    });
    renderEditor(client, 5);
    await screen.findByText(/Draft version 4/);
    await userEvent.click(screen.getByRole("button", { name: /preview impact/i }));
    expect(await screen.findByText(/Newly excluded/)).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
    await waitFor(() => expect(mocks.getPreview.mock.calls.length).toBeGreaterThanOrEqual(2));
    const settledCalls = mocks.getPreview.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(mocks.getPreview.mock.calls.length).toBe(settledCalls);
    expect(screen.getByRole("button", { name: /^publish/i })).toBeEnabled();
    expect(screen.queryByRole("status", { name: /preview running/i })).not.toBeInTheDocument();
  });

  it("stops polling and reports a failed preview with closed copy", async () => {
    const { client, mocks } = createFakeClient({
      getStatus: statusData(),
      createPreview: previewData(),
      getPreview: [errorResult("exclusion_policy_preview_failed")],
    });
    renderEditor(client, 5);
    await screen.findByText(/Draft version 4/);
    await userEvent.click(screen.getByRole("button", { name: /preview impact/i }));
    expect(await screen.findByText(/The preview could not be completed/)).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(mocks.getPreview).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /preview impact/i })).toBeEnabled();
  });

  it("reports an expired preview with closed copy and returns to editing", async () => {
    const { client } = createFakeClient({
      getStatus: statusData(),
      createPreview: previewData(),
      getPreview: [previewData({ status: "expired" })],
    });
    renderEditor(client, 5);
    await screen.findByText(/Draft version 4/);
    await userEvent.click(screen.getByRole("button", { name: /preview impact/i }));
    expect(await screen.findByText(/The preview expired/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /preview impact/i })).toBeEnabled();
  });

  it("reports a stale preview with closed copy", async () => {
    const { client } = createFakeClient({
      getStatus: statusData(),
      createPreview: previewData(),
      getPreview: [errorResult("exclusion_policy_preview_stale")],
    });
    renderEditor(client, 5);
    await screen.findByText(/Draft version 4/);
    await userEvent.click(screen.getByRole("button", { name: /preview impact/i }));
    expect(await screen.findByText(/The preview is stale/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /preview impact/i })).toBeEnabled();
  });

  it("treats a ready preview bound to a different draft version as not publishable", async () => {
    const { client, mocks } = createFakeClient({
      getStatus: statusData(),
      createPreview: previewData(),
      getPreview: [previewData({ status: "ready", draft_version: 3, results: [] })],
    });
    renderEditor(client, 5);
    await screen.findByText(/Draft version 4/);
    await userEvent.click(screen.getByRole("button", { name: /preview impact/i }));
    expect(await screen.findByText(/no longer matches the saved draft/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^publish/i })).not.toBeInTheDocument();
    expect(mocks.publish).not.toHaveBeenCalled();
  });

  it("stops polling when unmounted mid-preview", async () => {
    const { client, mocks } = createFakeClient({
      getStatus: statusData(),
      createPreview: previewData(),
      getPreview: [previewData({ status: "pending" })],
    });
    const view = renderEditor(client, 5);
    await screen.findByText(/Draft version 4/);
    await userEvent.click(screen.getByRole("button", { name: /preview impact/i }));
    await screen.findByRole("status", { name: /preview running/i });
    await waitFor(() => expect(mocks.getPreview.mock.calls.length).toBeGreaterThanOrEqual(1));
    view.unmount();
    const settledCalls = mocks.getPreview.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(mocks.getPreview.mock.calls.length).toBe(settledCalls);
  });

  it("publishes through the exact confirmation dialog and refreshes the committed status", async () => {
    const { client, mocks } = createFakeClient({
      getStatus: statusData(),
      createPreview: previewData(),
      getPreview: [previewData({ status: "ready", results: [] })],
      publish: publicationData(),
    });
    const refreshedStatus = statusData({
      active_policy_revision_id: "6f7a8b9c-0d1e-4f2a-3b4c-5d6e7f8a9b0c",
      active_revision_number: 4,
      draft: savedDraftData([], 5),
      reconciliation: {
        policy_revision_id: "6f7a8b9c-0d1e-4f2a-3b4c-5d6e7f8a9b0c",
        state: "pending",
        updated_at: "2026-08-17T08:05:00Z",
        safe_error_code: null,
      },
    });
    const statusQueue = [statusData(), refreshedStatus];
    mocks.getStatus.mockImplementation(() => {
      const next = statusQueue.length > 1 ? statusQueue.shift()! : (statusQueue[0] ?? statusData());
      return Promise.resolve({ ok: true, data: next });
    });
    renderEditor(client, 5);
    await screen.findByText(/Draft version 4/);
    await userEvent.click(screen.getByRole("button", { name: /preview impact/i }));
    await screen.findByText(/Newly excluded/);
    await userEvent.click(screen.getByRole("button", { name: /^publish/i }));
    expect(screen.getByRole("dialog", { name: /publish exclusion policy/i })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/type publish exclusion policy to confirm/i), "PUBLISH EXCLUSION POLICY");
    await userEvent.click(screen.getByRole("button", { name: /publish policy/i }));
    await waitFor(() => expect(mocks.publish).toHaveBeenCalledTimes(1));
    const request = mocks.publish.mock.calls[0]?.[0].request;
    expect(request).toEqual({
      confirmation: "PUBLISH EXCLUSION POLICY",
      expected_active_policy_revision_id: ACTIVE_REVISION_ID,
      expected_active_revision_number: 3,
      expected_draft_sha256: "a".repeat(64),
      expected_draft_version: 4,
      policy_draft_id: DRAFT_ID,
      policy_preview_id: PREVIEW_ID,
      preview_impact_digest: "c".repeat(64),
    });
    expect(await screen.findByRole("status")).toBeInTheDocument();
    expect(screen.getByRole("status").textContent).toContain("Published revision 4");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await screen.findByText(/Draft version 5/)).toBeInTheDocument();
    expect(await screen.findByText(/Reconciliation pending/)).toBeInTheDocument();
    await waitFor(() => expect(mocks.getStatus.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("never renders provider or secret detail from any failure", async () => {
    const { client } = createFakeClient({
      getStatus: errorResult("internal_error", "psycopg2 failed: postgres://owner:hunter2@db:5432 secret-key"),
    });
    renderEditor(client);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("The policy could not be loaded");
    expect(document.body.textContent).not.toContain("hunter2");
    expect(document.body.textContent).not.toContain("psycopg2");
    expect(document.body.textContent).not.toContain("secret-key");
  });

  it("never persists draft or preview data in web storage", async () => {
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem");
    const { client } = createFakeClient({
      getStatus: statusData(),
      createPreview: previewData(),
      getPreview: [previewData({ status: "ready", results: [] })],
    });
    renderEditor(client, 5);
    await screen.findByText(/Draft version 4/);
    await userEvent.click(screen.getByRole("button", { name: /preview impact/i }));
    await screen.findByText(/Newly excluded/);
    expect(setItemSpy).not.toHaveBeenCalled();
    setItemSpy.mockRestore();
  });
});
