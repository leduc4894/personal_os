"use client";

import type { ReactNode } from "react";

import type { PolicyPublicationData, PolicyStatusData } from "./policy-models";

export interface PolicyStatusProps {
  status: PolicyStatusData;
  /** The exact committed publication result, when one just completed. */
  lastPublication?: PolicyPublicationData | null;
}

/**
 * The Admin policy status card (spec 17): active revision identity,
 * reconciliation progress and the explicit first-publication guidance when
 * no policy exists. It renders only closed, opaque metadata — never rule
 * operands, fingerprints, signatures or key material, which the Admin read
 * does not carry.
 */
export function PolicyStatus({ status, lastPublication = null }: PolicyStatusProps): ReactNode {
  const hasActivePolicy = status.active_policy_revision_id !== null;
  return (
    <section aria-labelledby="policy-status-heading" className="policy-status">
      <h2 id="policy-status-heading">Policy status</h2>
      {hasActivePolicy ? (
        <>
          <dl>
            <div>
              <dt>Active policy revision</dt>
              <dd>{status.active_revision_number}</dd>
            </div>
            <div>
              <dt>Revision ID</dt>
              <dd>
                <code>{status.active_policy_revision_id}</code>
              </dd>
            </div>
          </dl>
          {status.reconciliation !== null ? (
            <p>
              Reconciliation {status.reconciliation.state} — updated{" "}
              <time dateTime={status.reconciliation.updated_at}>
                {status.reconciliation.updated_at}
              </time>
              . Projection updates may still be in progress until reconciliation completes.
            </p>
          ) : (
            <p>No reconciliation has been recorded for the active revision yet.</p>
          )}
        </>
      ) : (
        <p>
          No exclusion policy is published yet. Every content operation is denied until a first
          policy is published. Publishing the empty policy allows all current sources.
        </p>
      )}
      {status.stale_running_previews !== null && (
        <div role="alert" className="warning-message">
          <h3>Preview worker health</h3>
          <ul>
            {status.stale_running_previews.map((preview) => (
              <li key={preview.policy_preview_id}>
                Preview <code>{preview.policy_preview_id}</code> — worker stale running for{" "}
                {Math.round(preview.age_seconds / 60)} min
              </li>
            ))}
          </ul>
          <p>No live policy worker has swept these previews. Restart the policy workers.</p>
        </div>
      )}
      {lastPublication !== null && (
        <>
          <p>
            Published revision {lastPublication.revision_number} · {lastPublication.rule_count}{" "}
            {lastPublication.rule_count === 1 ? "rule" : "rules"} · reconciliation{" "}
            {lastPublication.reconciliation_status}
            {lastPublication.is_replay
              ? " · exact replay of an already committed publication"
              : ""}
            .
          </p>
          <p>
            Signed by key <code>{lastPublication.signing_key_id}</code>. The key identifier is an
            opaque public id; no key material is ever displayed.
          </p>
        </>
      )}
    </section>
  );
}
