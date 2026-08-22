# Existing-files sync drain design

## Problem

`Sync existing files` confirms and scans the Vault, but historically ended at
the journal admission boundary. A newly allowed file therefore became queued
without a guaranteed subsequent queue pass. If `Sync now` was invoked while
the scan was still running, its pass could finish before the scan recorded the
row; the row then remained stranded. This reproduces as a policy-blocked note
remaining uncommitted after a policy-changing reauthorization.

## Required behaviour

After the user confirms `Sync existing files`, the plugin must:

1. scan the bounded regular-file snapshot through the existing capture path;
2. preserve the prior terminal `excluded_policy` event as audit evidence;
3. append an allowed successor under the newly trusted policy when eligible;
4. drain the new queued work without requiring `Sync now`;
5. retry a bounded number of times when another pass is already running;
6. render a status other than `Policy blocked` once that successor exists.

Cancellation, unavailable policy trust, no processed files, and scan failure
must not start a queue pass. No path, content, credential, code, cookie or
token may be written to logs or acceptance artifacts.

## Design

`KnowledgeWorkspacePlugin.#runExistingFilesScan()` remains the only command
callback. After a completed scan with at least one processed file it starts
`#drainExistingFilesScanQueue()` asynchronously. That helper requests the
existing bounded queue pass. When the driver reports `pass_already_running`,
it waits a bounded 250 ms interval and retries at most 60 times. The helper
returns after one real pass or after the bounded retry window; it never creates
a parallel driver pass and never loops indefinitely.

`#runBoundedQueuePass()` returns its existing `QueuePassSummary`, allowing the
drain helper to distinguish a real result from `pass_already_running` while
retaining existing status projection behavior.

## Acceptance criteria

- A live WDIO journey creates a markdown fixture under a `.md` exclusion,
  observes `Policy blocked`, publishes `.tmp`, and reauthorizes.
- The real confirmation modal for `Sync existing files` is clicked.
- The fixture has one terminal excluded audit event and one committed mapped
  successor; canonical server evidence is exactly one source, version, sync
  event and committed operation.
- Status no longer contains `Policy blocked`.
- WDIO retains a closed phase artifact until the launcher records final pass
  or failure, so a background process cannot lose its verdict.
- The launcher atomically retains `.local/<project>.obsidian-live-result.json`
  for both pass and failure; cleanup is an explicit operator action.
