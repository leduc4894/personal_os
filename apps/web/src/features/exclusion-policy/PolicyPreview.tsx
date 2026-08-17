"use client";

import type { ReactNode } from "react";

import {
  indeterminateMissingFields,
  type PolicyPreviewCursorData,
  type PolicyPreviewData,
  type PolicyPreviewResultRowData,
} from "./policy-models";

export type PolicyPreviewPanelState =
  | { kind: "in-flight" }
  | {
      kind: "ready";
      preview: PolicyPreviewData;
      rows: readonly PolicyPreviewResultRowData[];
      hasMore: boolean;
    }
  | { kind: "terminal"; message: string };

export interface PolicyPreviewProps {
  state: PolicyPreviewPanelState;
  /** True while the next result page is being fetched. */
  isLoadingMore?: boolean | undefined;
  /** Requests the next page through the preview&apos;s stable cursor. */
  onLoadMore?: ((cursor: PolicyPreviewCursorData) => void) | undefined;
  /** Opens the publication confirmation dialog. */
  onPublish?: (() => void) | undefined;
  /** The owning editor decides whether the preview still publishes. */
  isPublishEnabled?: boolean | undefined;
}

function formatImpactClass(impactClass: string): string {
  return impactClass.replaceAll("_", " ");
}

/**
 * The preview surface (spec 10/17): closed impact counters with unchanged
 * collapsed from the two still-* counters, a prominent indeterminate warning
 * naming the missing subject fields, and paginated result rows joined to
 * nothing but their opaque identifiers and closed states. Row text is plain
 * escaped React text; it never enters storage or telemetry from here.
 */
export function PolicyPreview({
  state,
  isLoadingMore = false,
  onLoadMore,
  onPublish,
  isPublishEnabled = false,
}: PolicyPreviewProps): ReactNode {
  if (state.kind === "in-flight") {
    return (
      <section aria-labelledby="policy-preview-heading" className="policy-preview">
        <h2 id="policy-preview-heading">Preview impact</h2>
        <p role="status" aria-label="Preview running">
          Preview running. The impact counts appear when the preview completes.
        </p>
      </section>
    );
  }
  if (state.kind === "terminal") {
    return (
      <section aria-labelledby="policy-preview-heading" className="policy-preview">
        <h2 id="policy-preview-heading">Preview impact</h2>
        <p role="alert" className="error-message">
          {state.message}
        </p>
      </section>
    );
  }
  const { preview, rows, hasMore } = state;
  const counters = preview.counters;
  const unchangedCount = counters.still_allowed_count + counters.still_excluded_count;
  const missingFields = indeterminateMissingFields(rows);
  return (
    <section aria-labelledby="policy-preview-heading" className="policy-preview">
      <h2 id="policy-preview-heading">Preview impact</h2>
      <dl>
        <div>
          <dt>Newly excluded</dt>
          <dd>{counters.newly_excluded_count}</dd>
        </div>
        <div>
          <dt>Newly allowed</dt>
          <dd>{counters.newly_allowed_count}</dd>
        </div>
        <div>
          <dt>Unchanged</dt>
          <dd>{unchangedCount}</dd>
        </div>
        <div>
          <dt>Indeterminate</dt>
          <dd>{counters.indeterminate_count}</dd>
        </div>
      </dl>
      {counters.indeterminate_count > 0 && (
        <p role="alert" className="warning-message">
          {counters.indeterminate_count}{" "}
          {counters.indeterminate_count === 1 ? "source" : "sources"} could not be classified
          because required fields are missing
          {missingFields.length > 0 ? ` (${missingFields.join(", ")})` : ""}. Indeterminate sources
          stay excluded from sync until the fields exist.
        </p>
      )}
      <table>
        <caption>Preview result details</caption>
        <thead>
          <tr>
            <th scope="col">Source</th>
            <th scope="col">Impact</th>
            <th scope="col">Match state</th>
            <th scope="col">Enforced decision</th>
            <th scope="col">Missing fields</th>
            <th scope="col">Matched rules</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.impact_class}/${row.source_id}`}>
              <th scope="row">
                <code>{row.source_id}</code>
              </th>
              <td>{formatImpactClass(row.impact_class)}</td>
              <td>{row.proposed_match_state}</td>
              <td>
                {row.previous_enforced_decision} → {row.proposed_enforced_decision}
              </td>
              <td>{row.missing_fields.length > 0 ? row.missing_fields.join(", ") : "—"}</td>
              <td>
                {row.matched_rule_ids.length === 0
                  ? "—"
                  : `${row.matched_rule_ids.length} ${
                      row.matched_rule_ids.length === 1 ? "rule" : "rules"
                    }`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {hasMore && preview.next_cursor !== null && preview.next_cursor !== undefined && (
        <button type="button" onClick={() => onLoadMore?.(preview.next_cursor!)} disabled={isLoadingMore}>
          Load more results
        </button>
      )}
      {onPublish !== undefined && (
        <button type="button" onClick={onPublish} disabled={!isPublishEnabled} className="publish-button">
          Publish…
        </button>
      )}
      <p>
        Preview bound to draft version {preview.draft_version}, checkpoint{" "}
        {preview.source_checkpoint_event_sequence}.
      </p>
    </section>
  );
}
