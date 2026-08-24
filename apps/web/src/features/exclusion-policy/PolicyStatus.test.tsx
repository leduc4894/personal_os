import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { components } from "@workspace/api-client";

import type { PolicyStatusData } from "./policy-models";
import { PolicyStatus } from "./PolicyStatus";

/**
 * The status card renders only closed, opaque metadata: revision identity,
 * reconciliation progress and the explicit empty-policy guidance. It never
 * renders rule operands, secrets or provider details.
 */

type PolicyPublicationData = components["schemas"]["PolicyPublicationData"];

function statusData(overrides: Partial<PolicyStatusData> = {}): PolicyStatusData {
  return {
    active_policy_revision_id: "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
    active_revision_number: 3,
    draft: {
      base_policy_revision_id: "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
      draft_id: "0f9e8d7c-6b5a-4c3d-2e1f-0a1b2c3d4e5f",
      draft_version: 7,
      rules: [],
    },
    reconciliation: {
      policy_revision_id: "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
      state: "running",
      updated_at: "2026-08-17T08:00:00Z",
      safe_error_code: null,
    },
    ...overrides,
  };
}

function publicationData(overrides: Partial<PolicyPublicationData> = {}): PolicyPublicationData {
  return {
    is_replay: false,
    parent_policy_revision_id: "1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e",
    payload_sha256: "b".repeat(64),
    policy_revision_id: "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
    published_at: "2026-08-17T08:05:00Z",
    reconciliation_status: "pending",
    revision_number: 3,
    rule_count: 2,
    signing_key_id: "key-2026-08",
    workspace_id: "9a8b7c6d-5e4f-4a3b-2c1d-0e9f8a7b6c5d",
    ...overrides,
  };
}

describe("PolicyStatus", () => {
  it("renders the active revision metadata and reconciliation progress", () => {
    render(<PolicyStatus status={statusData()} />);
    expect(screen.getByText("Active policy revision")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText(/2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f/)).toBeInTheDocument();
    expect(screen.getByText(/Reconciliation/)).toBeInTheDocument();
    expect(screen.getByText(/running/)).toBeInTheDocument();
    expect(
      screen.getByText(/Projection updates may still be in progress until reconciliation completes\./),
    ).toBeInTheDocument();
  });

  it("offers explicit first-publication guidance when no policy is active", () => {
    render(
      <PolicyStatus
        status={statusData({
          active_policy_revision_id: null,
          active_revision_number: 0,
          reconciliation: null,
        })}
      />,
    );
    expect(screen.getByText(/No exclusion policy is published yet/)).toBeInTheDocument();
    expect(
      screen.getByText(/Every content operation is denied until a first policy is published/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Publishing the empty policy allows all current sources/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Reconciliation/)).not.toBeInTheDocument();
  });

  it("replaces the summary with the exact committed publication result when provided", () => {
    render(
      <PolicyStatus status={statusData()} lastPublication={publicationData({ revision_number: 4 })} />,
    );
    expect(screen.getByText(/Published revision 4/)).toBeInTheDocument();
    expect(screen.getByText(/2 rules/)).toBeInTheDocument();
    expect(screen.getByText(/pending/)).toBeInTheDocument();
    expect(screen.queryByText(/exact replay/)).not.toBeInTheDocument();
  });

  it("marks a replayed publication result explicitly", () => {
    render(
      <PolicyStatus status={statusData()} lastPublication={publicationData({ is_replay: true })} />,
    );
    expect(screen.getByText(/Published revision 3/)).toBeInTheDocument();
    expect(screen.getByText(/exact replay of an already committed publication/)).toBeInTheDocument();
  });

  it("shows the signer of the committed revision as an opaque key identifier", () => {
    render(
      <PolicyStatus status={statusData()} lastPublication={publicationData({ signing_key_id: "key-2026-08" })} />,
    );
    expect(screen.getByText(/signed by key/i)).toBeInTheDocument();
    expect(screen.getByText("key-2026-08")).toBeInTheDocument();
  });

  it("renders no signer claim when no committed publication result exists", () => {
    render(<PolicyStatus status={statusData()} />);
    expect(screen.queryByText(/signed by key/i)).not.toBeInTheDocument();
  });

  it("renders only closed metadata and never rule or secret material", () => {
    const { container } = render(
      <PolicyStatus
        status={statusData({
          draft: {
            ...statusData().draft,
            rules: [
              {
                rule_id: "3b4c5d6e-7f8a-4b9c-0d1e-2f3a4b5c6d7e",
                rule_kind: "folder_prefix",
                folder_prefix: "private/journal",
                semantic_fingerprint: "f".repeat(64),
              },
            ],
          },
        })}
      />,
    );
    expect(container.textContent).not.toContain("private/journal");
    expect(container.textContent).not.toContain("f".repeat(64));
  });
});
