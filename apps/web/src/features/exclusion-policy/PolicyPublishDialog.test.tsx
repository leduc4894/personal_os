import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  AuthenticationCallResult,
} from "../../api/authentication-client";
import type { PolicyPublicationTriggerClient } from "../../api/exclusion-policy-client";
import type {
  PolicyDraft,
  PolicyPreviewData,
  PolicyPublicationData,
  PolicyStatusData,
} from "./policy-models";
import { PolicyPublishDialog } from "./PolicyPublishDialog";

/**
 * The publication dialog is the exact-typed confirmation guard (spec 11/17):
 * it binds the ready preview identity, expected draft version/hash, expected
 * active revision and the source checkpoint, requires the exact phrase,
 * prevents double submits, retries with the same idempotency key after a
 * recent re-authentication and renders only closed, generic failure copy.
 */

type PublicationResult = AuthenticationCallResult<PolicyPublicationData>;

const DRAFT_SHA = "a".repeat(64);
const IMPACT_DIGEST = "c".repeat(64);
const PREVIEW_ID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
const DRAFT_ID = "0f9e8d7c-6b5a-4c3d-2e1f-0a1b2c3d4e5f";

function savedDraft(): PolicyDraft {
  return {
    base_policy_revision_id: "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
    draft_id: DRAFT_ID,
    draft_version: 4,
    rules: [
      { rule_id: "3b4c5d6e-7f8a-4b9c-0d1e-2f3a4b5c6d7e", rule_kind: "folder_prefix", operandText: "private" },
    ],
  };
}

function readyPreview(): PolicyPreviewData {
  return {
    base_policy_revision_id: "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
    consumed_at: null,
    counters: {
      indeterminate_count: 0,
      newly_allowed_count: 2,
      newly_excluded_count: 1,
      still_allowed_count: 7,
      still_excluded_count: 3,
    },
    created_at: "2026-08-17T08:00:00Z",
    draft_sha256: DRAFT_SHA,
    draft_version: 4,
    expires_at: "2026-08-17T08:20:00Z",
    impact_digest: IMPACT_DIGEST,
    policy_draft_id: DRAFT_ID,
    policy_preview_id: PREVIEW_ID,
    ready_at: "2026-08-17T08:01:00Z",
    results: [],
    safe_error_code: null,
    source_checkpoint_event_sequence: 12,
    status: "ready",
  };
}

function statusData(): PolicyStatusData {
  return {
    active_policy_revision_id: "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
    active_revision_number: 3,
    draft: {
      base_policy_revision_id: "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
      draft_id: DRAFT_ID,
      draft_version: 4,
      rules: [],
    },
    reconciliation: null,
    stale_running_previews: null,
  };
}

function publicationData(overrides: Partial<PolicyPublicationData> = {}): PolicyPublicationData {
  return {
    is_replay: false,
    parent_policy_revision_id: "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
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

function createDialogClient(options: {
  publishResults?: PublicationResult[];
  reauthenticateResult?: AuthenticationCallResult<{ state: string }>;
} = {}): { client: PolicyPublicationTriggerClient; publishMock: ReturnType<typeof vi.fn>; reauthMock: ReturnType<typeof vi.fn> } {
  const publishResults = options.publishResults ?? [
    {
      ok: true as const,
      data: publicationData(),
    },
  ];
  const publishMock = vi.fn();
  let nextResultIndex = 0;
  publishMock.mockImplementation(() => {
    const result = publishResults[Math.min(nextResultIndex, publishResults.length - 1)];
    nextResultIndex += 1;
    return Promise.resolve(result);
  });
  const reauthMock = vi.fn().mockResolvedValue(
    options.reauthenticateResult ?? { ok: true as const, data: { state: "active" } },
  );
  const client: PolicyPublicationTriggerClient = {
    publishExclusionPolicy: publishMock,
    reauthenticate: reauthMock,
  };
  return { client, publishMock, reauthMock };
}

function renderDialog(
  client: PolicyPublicationTriggerClient,
  listeners: { onClosed?: () => void; onPublished?: (result: PolicyPublicationData) => void } = {},
) {
  return render(
    <PolicyPublishDialog
      client={client}
      draft={savedDraft()}
      preview={readyPreview()}
      status={statusData()}
      onClosed={listeners.onClosed ?? vi.fn()}
      onPublished={listeners.onPublished ?? vi.fn()}
    />,
  );
}

async function confirmExactPhrase(): Promise<void> {
  await userEvent.type(
    screen.getByLabelText(/type publish exclusion policy to confirm/i),
    "PUBLISH EXCLUSION POLICY",
  );
}

describe("PolicyPublishDialog", () => {
  it("renders the exact publication binding before any confirmation", () => {
    renderDialog(createDialogClient().client);
    expect(screen.getByRole("dialog", { name: /publish exclusion policy/i })).toBeInTheDocument();
    expect(screen.getByText(/revision 3 → 4/i)).toBeInTheDocument();
    expect(screen.getByText(/Draft version 4/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(DRAFT_SHA))).toBeInTheDocument();
    expect(screen.getByText(new RegExp(PREVIEW_ID))).toBeInTheDocument();
    expect(screen.getByText(/checkpoint 12/)).toBeInTheDocument();
    expect(screen.getByText(/Newly excluded 1/)).toBeInTheDocument();
    expect(screen.getByText(/Newly allowed 2/)).toBeInTheDocument();
    expect(screen.getByText(/Unchanged 10/)).toBeInTheDocument();
  });

  it("keeps the confirmation input labeled and the submit disabled until the phrase matches exactly", async () => {
    const { client } = createDialogClient();
    renderDialog(client);
    const input = screen.getByLabelText(/type publish exclusion policy to confirm/i);
    expect(input).toHaveAccessibleDescription(/PUBLISH EXCLUSION POLICY/);
    const submit = screen.getByRole("button", { name: /publish policy/i });
    expect(submit).toBeDisabled();
    await userEvent.type(input, "publish exclusion policy");
    expect(submit).toBeDisabled();
    await userEvent.clear(input);
    await userEvent.type(input, "PUBLISH EXCLUSION POLICY ");
    expect(submit).toBeDisabled();
    await userEvent.clear(input);
    await confirmExactPhrase();
    expect(submit).toBeEnabled();
  });

  it("sends the exact binding and a printable idempotency key exactly once per submit", async () => {
    const { client, publishMock } = createDialogClient();
    const onPublished = vi.fn();
    renderDialog(client, { onPublished });
    await confirmExactPhrase();
    await userEvent.click(screen.getByRole("button", { name: /publish policy/i }));
    await waitFor(() => expect(onPublished).toHaveBeenCalledTimes(1));
    expect(publishMock).toHaveBeenCalledTimes(1);
    const call = publishMock.mock.calls[0]?.[0];
    expect(call.request).toEqual({
      confirmation: "PUBLISH EXCLUSION POLICY",
      expected_active_policy_revision_id: "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
      expected_active_revision_number: 3,
      expected_draft_sha256: DRAFT_SHA,
      expected_draft_version: 4,
      policy_draft_id: DRAFT_ID,
      policy_preview_id: PREVIEW_ID,
      preview_impact_digest: IMPACT_DIGEST,
    });
    expect(call.idempotencyKey).toMatch(/^[ -~]{1,200}$/);
    if (onPublished.mock.calls[0]) {
      expect((onPublished.mock.calls[0] as unknown[])[0]).toMatchObject({ revision_number: 4, is_replay: false });
    }
  });

  it("blocks double submits while the publication request is in flight", async () => {
    let resolvePublish: (value: PublicationResult) => void = () => {};
    const publishMock = vi.fn(
      () =>
        new Promise<PublicationResult>((resolve) => {
          resolvePublish = resolve;
        }),
    );
    const client: PolicyPublicationTriggerClient = {
      publishExclusionPolicy: publishMock,
      reauthenticate: vi.fn(),
    };
    renderDialog(client);
    await confirmExactPhrase();
    const submit = screen.getByRole("button", { name: /publish policy/i });
    await userEvent.click(submit);
    await userEvent.click(submit);
    await userEvent.click(submit);
    expect(publishMock).toHaveBeenCalledTimes(1);
    resolvePublish({ ok: true, data: publicationData() });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("reports an exact replay through the same committed-result callback", async () => {
    const { client } = createDialogClient({
      publishResults: [{ ok: true, data: publicationData({ is_replay: true }) }],
    });
    const onPublished = vi.fn();
    renderDialog(client, { onPublished });
    await confirmExactPhrase();
    await userEvent.click(screen.getByRole("button", { name: /publish policy/i }));
    await waitFor(() => expect(onPublished).toHaveBeenCalledTimes(1));
    expect((onPublished.mock.calls[0] as unknown[])[0]).toMatchObject({ is_replay: true });
  });

  it("prompts for recent re-authentication and retries with the same idempotency key", async () => {
    const { client, publishMock, reauthMock } = createDialogClient({
      publishResults: [
        {
          ok: false,
          error: {
            code: "recent_authentication_required",
            details: {},
            message: "Simulated recent_authentication_required failure.",
            retryable: false,
          },
        },
      ],
    });
    renderDialog(client);
    await confirmExactPhrase();
    await userEvent.click(screen.getByRole("button", { name: /publish policy/i }));
    const passwordInput = await screen.findByLabelText(/current password/i);
    expect(screen.getByText(/Confirm your password again to publish/)).toBeInTheDocument();
    await userEvent.type(passwordInput, "correct horse battery staple");
    await userEvent.click(screen.getByRole("button", { name: /confirm password/i }));
    await waitFor(() => expect(publishMock).toHaveBeenCalledTimes(2));
    expect(reauthMock).toHaveBeenCalledWith({ password: "correct horse battery staple", totpCode: undefined });
    const firstKey = publishMock.mock.calls[0]?.[0].idempotencyKey;
    const secondKey = publishMock.mock.calls[1]?.[0].idempotencyKey;
    expect(secondKey).toBe(firstKey);
  });

  it("renders only safe generic failure copy, never provider or secret detail", async () => {
    const { client } = createDialogClient({
      publishResults: [
        {
          ok: false,
          error: {
            code: "internal_error",
            details: { driver: "psycopg2 secret-key private/path.pem" },
            message: "psycopg2 failed: postgres://owner:hunter2@db:5432 with secret-key",
            retryable: true,
          },
        },
      ],
    });
    renderDialog(client);
    await confirmExactPhrase();
    await userEvent.click(screen.getByRole("button", { name: /publish policy/i }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Publishing the policy failed. Nothing was changed.");
    expect(document.body.textContent).not.toContain("hunter2");
    expect(document.body.textContent).not.toContain("psycopg2");
    expect(document.body.textContent).not.toContain("private/path.pem");
  });

  it("closes on Escape and on the cancel button without publishing", async () => {
    const { client, publishMock } = createDialogClient();
    const onClosed = vi.fn();
    const first = renderDialog(client, { onClosed });
    await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onClosed).toHaveBeenCalledTimes(1);
    first.unmount();
    const second = renderDialog(client, { onClosed });
    await userEvent.keyboard("{Escape}");
    expect(onClosed).toHaveBeenCalledTimes(2);
    second.unmount();
    expect(publishMock).not.toHaveBeenCalled();
  });
});
