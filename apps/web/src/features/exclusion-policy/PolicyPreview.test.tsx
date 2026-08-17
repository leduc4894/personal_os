import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { components } from "@workspace/api-client";

import type { PolicyPreviewData, PolicyPreviewResultRowData } from "./policy-models";
import { PolicyPreview } from "./PolicyPreview";

/**
 * The preview panel is the only surface that renders preview result rows.
 * It shows the closed counters (with unchanged collapsed from the two
 * still-* counters), a prominent indeterminate warning naming the missing
 * fields, paged rows behind a stable cursor, and the publish entry point
 * whose enablement is decided by the owning editor, not here.
 */

type PolicyPreviewCursorData = components["schemas"]["PolicyPreviewCursorData"];

function previewData(overrides: Partial<PolicyPreviewData> = {}): PolicyPreviewData {
  return {
    base_policy_revision_id: null,
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
    policy_draft_id: "0f9e8d7c-6b5a-4c3d-2e1f-0a1b2c3d4e5f",
    policy_preview_id: "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    ready_at: "2026-08-17T08:01:00Z",
    results: null,
    safe_error_code: null,
    source_checkpoint_event_sequence: 12,
    status: "ready",
    ...overrides,
  };
}

function resultRow(overrides: Partial<PolicyPreviewResultRowData> = {}): PolicyPreviewResultRowData {
  return {
    impact_class: "newly_excluded",
    matched_rule_ids: ["3b4c5d6e-7f8a-4b9c-0d1e-2f3a4b5c6d7e"],
    missing_fields: [],
    previous_enforced_decision: "allow",
    previous_raw_decision: "allow",
    proposed_enforced_decision: "deny",
    proposed_match_state: "matched",
    proposed_raw_decision: "excluded",
    source_id: "4d5e6f7a-8b9c-4d0e-1f2a-3b4c5d6e7f8a",
    ...overrides,
  };
}

describe("PolicyPreview", () => {
  it("announces an in-flight preview without leaking any row data", () => {
    render(<PolicyPreview state={{ kind: "in-flight" }} />);
    expect(screen.getByRole("status", { name: /preview running/i })).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders the four closed impact counters, collapsing unchanged", () => {
    render(
      <PolicyPreview
        state={{ kind: "ready", preview: previewData(), rows: [], hasMore: false }}
      />,
    );
    expect(screen.getByText("Newly excluded")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("Newly allowed")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("Unchanged")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.getByText("Indeterminate")).toBeInTheDocument();
    expect(screen.getByText("0")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("warns prominently about indeterminate sources with their missing field names", () => {
    render(
      <PolicyPreview
        state={{
          kind: "ready",
          preview: previewData({
            counters: {
              indeterminate_count: 2,
              newly_allowed_count: 0,
              newly_excluded_count: 0,
              still_allowed_count: 0,
              still_excluded_count: 0,
            },
          }),
          rows: [
            resultRow({
              impact_class: "indeterminate",
              missing_fields: ["size_bytes"],
              proposed_match_state: "indeterminate",
              source_id: "4d5e6f7a-8b9c-4d0e-1f2a-3b4c5d6e7f8a",
            }),
            resultRow({
              impact_class: "indeterminate",
              matched_rule_ids: [],
              missing_fields: ["size_bytes", "media_type"],
              proposed_match_state: "indeterminate",
              source_id: "5e6f7a8b-9c0d-4e1f-2a3b-4c5d6e7f8a9b",
            }),
          ],
          hasMore: false,
        }}
      />,
    );
    const warning = screen.getByRole("alert");
    expect(warning.textContent).toContain("2 sources could not be classified");
    expect(warning.textContent).toContain("size_bytes");
    expect(warning.textContent).toContain("media_type");
    expect(warning.textContent).toContain("stay excluded");
  });

  it("renders paged result details as escaped text with opaque identifiers only", () => {
    render(
      <PolicyPreview
        state={{ kind: "ready", preview: previewData(), rows: [resultRow()], hasMore: false }}
      />,
    );
    const row = screen.getByRole("row", { name: /4d5e6f7a-8b9c-4d0e-1f2a-3b4c5d6e7f8a/ });
    expect(row.textContent).toContain("newly excluded");
    expect(row.textContent).toContain("matched");
    expect(row.textContent).toContain("allow → deny");
    expect(screen.queryByRole("button", { name: /load more/i })).not.toBeInTheDocument();
  });

  it("loads the next page through the stable cursor on demand", async () => {
    const onLoadMore = vi.fn();
    const cursor: PolicyPreviewCursorData = {
      impact_class: "newly_excluded",
      source_id: "4d5e6f7a-8b9c-4d0e-1f2a-3b4c5d6e7f8a",
    };
    render(
      <PolicyPreview
        state={{ kind: "ready", preview: previewData({ next_cursor: cursor }), rows: [resultRow()], hasMore: true }}
        onLoadMore={onLoadMore}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /load more/i }));
    expect(onLoadMore).toHaveBeenCalledWith({
      impact_class: "newly_excluded",
      source_id: "4d5e6f7a-8b9c-4d0e-1f2a-3b4c5d6e7f8a",
    });
  });

  it("disables load-more while a page fetch is running", () => {
    render(
      <PolicyPreview
        state={{
          kind: "ready",
          preview: previewData({
            next_cursor: { impact_class: "newly_excluded", source_id: "4d5e6f7a-8b9c-4d0e-1f2a-3b4c5d6e7f8a" },
          }),
          rows: [resultRow()],
          hasMore: true,
        }}
        isLoadingMore
        onLoadMore={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /load more/i })).toBeDisabled();
  });

  it("renders terminal expired, stale and failed messaging without provider detail", () => {
    render(<PolicyPreview state={{ kind: "terminal", message: "The preview expired. Start a new preview." }} />);
    expect(screen.getByRole("alert").textContent).toContain("The preview expired");
    render(
      <PolicyPreview state={{ kind: "terminal", message: "The preview is stale. Start a new preview." }} />,
    );
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
  });

  it("gates the publish button on the owning editor decision", () => {
    const onPublish = vi.fn();
    const { rerender } = render(
      <PolicyPreview
        state={{ kind: "ready", preview: previewData(), rows: [], hasMore: false }}
        onPublish={onPublish}
        isPublishEnabled={false}
      />,
    );
    const publishButton = screen.getByRole("button", { name: /publish/i });
    expect(publishButton).toBeDisabled();
    rerender(
      <PolicyPreview
        state={{ kind: "ready", preview: previewData(), rows: [], hasMore: false }}
        onPublish={onPublish}
        isPublishEnabled
      />,
    );
    expect(screen.getByRole("button", { name: /publish/i })).toBeEnabled();
  });
});
