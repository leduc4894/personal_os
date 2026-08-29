"use client";

import { useEffect, useState, type ReactNode } from "react";

import {
  createBrowserSourceLifecycleClient,
  type SourceLifecycleDiagnosticsData,
  type SourceLifecycleReader,
} from "../../api/source-lifecycle-client";

export interface LifecycleRejectionsProps {
  /** The lifecycle rejection reader; tests inject a fake. */
  client?: SourceLifecycleReader;
}

type LifecycleReading =
  | { kind: "loading" }
  | { kind: "ready"; data: SourceLifecycleDiagnosticsData }
  | { kind: "failed"; errorCode: string };

/** Renders the bounded ring's epoch-millisecond timestamps deterministically. */
function formatRejectionTime(atEpochMs: number): string {
  return new Date(atEpochMs).toISOString();
}

/**
 * The Admin lifecycle surface: the commit counters per closed
 * operation/outcome pair plus the bounded recent-rejection ring (spec 19.2
 * diagnostics). It renders only closed, opaque tokens — operation labels,
 * outcome labels, counts, error codes and timestamps — never request
 * payloads, locators or provider detail; a failed read surfaces only the
 * closed error code.
 */
export function LifecycleRejections({
  client = createBrowserSourceLifecycleClient(),
}: LifecycleRejectionsProps): ReactNode {
  const [reading, setReading] = useState<LifecycleReading>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    void client.getRejectionDiagnostics().then((result) => {
      if (cancelled) {
        return;
      }
      setReading(
        result.ok
          ? { kind: "ready", data: result.data }
          : { kind: "failed", errorCode: result.error.code },
      );
    });
    return () => {
      cancelled = true;
    };
  }, [client]);

  if (reading.kind === "loading") {
    return <p role="status">Loading lifecycle diagnostics…</p>;
  }

  return (
    <section aria-labelledby="lifecycle-rejections-heading" className="lifecycle-rejections">
      <h2 id="lifecycle-rejections-heading">Lifecycle operations</h2>
      {reading.kind === "failed" ? (
        <p role="alert" className="error-message">
          Lifecycle diagnostics could not be loaded (error code:{" "}
          <code>{reading.errorCode}</code>). Try again.
        </p>
      ) : (
        <>
          {reading.data.commit_counters.length === 0 ? (
            <p>No lifecycle operations recorded yet.</p>
          ) : (
            <dl>
              {reading.data.commit_counters.map((counter) => (
                <div key={`${counter.operation}-${counter.outcome}`}>
                  <dt>
                    {counter.operation} · {counter.outcome}
                  </dt>
                  <dd>{counter.count}</dd>
                </div>
              ))}
            </dl>
          )}
          {reading.data.recent_rejections.length === 0 ? (
            <p>No rejections in the recent ring.</p>
          ) : (
            <ul>
              {reading.data.recent_rejections.map((rejection) => (
                <li key={`${rejection.operation}-${rejection.at_epoch_ms}-${rejection.error_code}`}>
                  <code>{rejection.error_code}</code> — {rejection.operation} at{" "}
                  <time dateTime={formatRejectionTime(rejection.at_epoch_ms)}>
                    {formatRejectionTime(rejection.at_epoch_ms)}
                  </time>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
