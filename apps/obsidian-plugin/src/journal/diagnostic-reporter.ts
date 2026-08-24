import type {
  SyncDiagnosticClosedToken,
  SyncDiagnosticsTrail,
} from "./sync-diagnostics-trail";

export interface JournalFailureReporter {
  reportJournalFailure(token: SyncDiagnosticClosedToken): void;
}

export function createJournalFailureReporter(
  trail: SyncDiagnosticsTrail | null,
): JournalFailureReporter {
  return {
    reportJournalFailure(token: SyncDiagnosticClosedToken): void {
      void trail?.append({ kind: "journal_failure", tokens: [token] });
    },
  };
}
