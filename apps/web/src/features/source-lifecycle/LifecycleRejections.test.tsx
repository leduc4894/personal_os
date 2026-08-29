import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { components } from "@workspace/api-client";

import type { SourceLifecycleReader } from "../../api/source-lifecycle-client";
import { LifecycleRejections } from "./LifecycleRejections";

/**
 * The lifecycle surface renders only closed, opaque tokens: the counter
 * operation/outcome labels, the bounded rejection ring's error codes and
 * timestamps. It never renders request payloads or provider detail, and a
 * failed read surfaces only the closed error code.
 */

type SourceLifecycleDiagnosticsData = components["schemas"]["SourceLifecycleDiagnosticsData"];
type RejectionRead = Awaited<ReturnType<SourceLifecycleReader["getRejectionDiagnostics"]>>;

function diagnosticsData(
  overrides: Partial<SourceLifecycleDiagnosticsData> = {},
): SourceLifecycleDiagnosticsData {
  return {
    commit_counters: [
      { count: 3, operation: "rename", outcome: "committed" },
      { count: 1, operation: "restore", outcome: "rejected" },
    ],
    recent_rejections: [
      {
        at_epoch_ms: 1_750_000_000_000,
        error_code: "source_locator_conflict",
        operation: "restore",
      },
    ],
    ...overrides,
  };
}

function readerReturning(result: RejectionRead): SourceLifecycleReader {
  return {
    async getRejectionDiagnostics() {
      return result;
    },
  };
}

describe("LifecycleRejections", () => {
  it("renders a loading status until the read resolves", () => {
    render(
      <LifecycleRejections
        client={{ getRejectionDiagnostics: () => new Promise<RejectionRead>(() => {}) }}
      />,
    );
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("renders the commit counters and the recent rejection ring", async () => {
    render(<LifecycleRejections client={readerReturning({ ok: true, data: diagnosticsData() })} />);
    expect(await screen.findByText("rename · committed")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("restore · rejected")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("source_locator_conflict")).toBeInTheDocument();
    expect(screen.getByText(/restore at/i)).toBeInTheDocument();
    expect(screen.getByText("2025-06-15T15:06:40.000Z")).toBeInTheDocument();
  });

  it("renders the explicit empty states when nothing was recorded", async () => {
    render(
      <LifecycleRejections
        client={readerReturning({
          ok: true,
          data: diagnosticsData({ commit_counters: [], recent_rejections: [] }),
        })}
      />,
    );
    expect(await screen.findByText("No lifecycle operations recorded yet.")).toBeInTheDocument();
    expect(screen.getByText("No rejections in the recent ring.")).toBeInTheDocument();
  });

  it("renders only the closed error code when the read fails", async () => {
    render(
      <LifecycleRejections
        client={readerReturning({
          ok: false,
          error: {
            code: "authentication_required",
            details: {},
            message: "Simulated authentication_required failure.",
            retryable: false,
          },
        })}
      />,
    );
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("authentication_required")).toBeInTheDocument();
    expect(screen.queryByText(/Simulated/)).not.toBeInTheDocument();
  });
});
