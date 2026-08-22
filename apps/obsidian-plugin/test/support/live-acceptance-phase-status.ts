import * as fs from "node:fs";

export const LIVE_ACCEPTANCE_PHASE_RESULT_CODES = [
  "source_lifecycle_scenario_started",
  "source_lifecycle_onboarding_completed",
  "source_lifecycle_initial_sync_completed",
  "source_lifecycle_rename_completed",
  "source_lifecycle_move_completed",
  "source_lifecycle_delete_completed",
  "source_lifecycle_restore_completed",
  "source_lifecycle_journal_drained",
  "source_lifecycle_journey_completed",
  "policy_recovery_scenario_started",
  "policy_recovery_block_observed",
  "policy_recovery_allowed_reauthorization_completed",
  "policy_recovery_existing_scan_started",
  "policy_recovery_journal_recovered",
  "policy_recovery_journey_completed",
] as const;

export type LiveAcceptancePhaseResultCode =
  (typeof LIVE_ACCEPTANCE_PHASE_RESULT_CODES)[number];

const phaseResultCodes = new Set<string>(LIVE_ACCEPTANCE_PHASE_RESULT_CODES);

export function writeLiveAcceptancePhaseStatus(
  statusFile: string,
  resultCode: LiveAcceptancePhaseResultCode,
): void {
  if (!phaseResultCodes.has(resultCode)) {
    throw new Error("live acceptance phase result code was invalid");
  }
  fs.writeFileSync(
    statusFile,
    JSON.stringify({ result_code: resultCode }),
    { encoding: "utf8", mode: 0o600 },
  );
}
