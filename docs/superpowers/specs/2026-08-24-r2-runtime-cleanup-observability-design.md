# R2 runtime cleanup observability — design spec

Date: 2026-08-24. Domain: R2 object-storage runtime check and local spool
cleanup. Governing rule: `AGENTS.md` (every closed error path surfaces a
reason token) and `docs/15-OBSERVABILITY_AND_ALERTING.md` §2, §3 and §7.

## Problem

The 2026-08-24 remaining-corners audit found two related omissions in the
one-shot `object-storage-check-runtime` path.

- `SpoolManager._cleanup_stale_spools_at()` returns a clean-looking
  `SpoolCleanupSummary(0, 0, 0, 0)` when `scandir()` fails and increments the
  ordinary `skipped_count` when `lstat()` or `unlink()` fails. The runtime
  check treats both as a clean cleanup. An operator cannot distinguish a
  clean spool from a spool root that could not be scanned or entries that
  could not be inspected/removed.
- `run_object_storage_runtime_check()` catches an injected janitor exception
  and emits `object_storage_spool_cleanup_degraded` without a reason token;
  it also suppresses every `client_source.close()` failure after the probe.
  The latter can leave a failed client teardown behind an otherwise-successful
  probe result with no readable record.

The affected locations at audit HEAD `60cb02a` are
`spool.py:356,374`, `runtime_check.py:282-290`, and `runtime_check.py:340-341`.

## Goals

1. Every degraded spool-cleanup outcome carries one closed reason token.
2. A client-close failure becomes a safe, structured degraded event.
3. The existing read-only HeadBucket probe still runs after janitor failure;
   its outcome and documented exit-code semantics remain independent from
   cleanup/teardown degradation.
4. No path, spool filename, exception text, credential, endpoint, bucket,
   digest or object bytes enters a summary, event or CLI output.

## Non-goals

- Do not add a provider fallback, retry, delete/list R2 operation, or a new
  public API/Admin route.
- Do not turn janitor or close degradation into a probe failure or change the
  `0`, `2`, `69`, `70`, `78` probe exit-code meanings.
- Do not expose operating-system errno, exception type/message, or a spool
  path as a reason.

## Contract

### C1. Closed cleanup reason vocabulary

Add one closed `SafeToken` vocabulary owned by `r2_object_storage.spool`:

| Token | Meaning | Count semantics |
| --- | --- | --- |
| `spool_cleanup_deferred` | Candidates exceeded the per-run bound. | Exact deferred candidates. |
| `spool_cleanup_scan_failed` | The spool root could not be scanned. | `0`; no candidate inventory is trustworthy. |
| `spool_cleanup_entry_failed` | One or more matching candidates could not be statted or removed. | Exact failed-entry count. |
| `spool_cleanup_janitor_failed` | The injected janitor raised before producing a valid summary. | `0`; no summary exists. |
| `object_storage_client_close_failed` | R2 client teardown raised after the probe path completed. | `0`. |

Tokens are constants, never derived from an exception, filesystem state or
provider response. Existing normal skips (non-regular, non-stale files) remain
counts only and must not be classified as failures.

### C2. Spool summary preserves failure evidence

`SpoolCleanupSummary` becomes an immutable closed result that preserves the
existing examined/removed/skipped/deferred counts and adds:

```text
reason: SafeToken | None
failed_count: non-negative integer
```

Exactly one result reason is allowed per invocation:

- clean: `reason=None`, `failed_count=0`, `deferred_count=0`;
- deferred-only: `reason=spool_cleanup_deferred`, `failed_count=0`;
- scan failure: `reason=spool_cleanup_scan_failed`, all inventory counts and
  `failed_count` are zero;
- entry failure: `reason=spool_cleanup_entry_failed`, `failed_count>0`.

When both deferred candidates and entry failures occur, `entry_failed` wins:
the summary's reason reports the failure and retains the exact
`deferred_count`; only one event is emitted. This prevents ambiguous multiple
events while preserving both safe counts. The scan-failure path must not
pretend the root was clean.

### C3. Janitor diagnostics

Extend the registered `object_storage_spool_cleanup_degraded` event with a
required closed `reason` field. It continues to require `operation` and
`count`; it is warning/degraded and retains its current stdout routing.

The runtime check emits exactly one cleanup-degraded event when:

- the summary has a reason, using its `reason` and `deferred_count`;
- the injected janitor throws a non-cancellation exception, using
  `spool_cleanup_janitor_failed` and count `0`.

Cancellation still propagates untouched. A clean summary emits no cleanup
event. The exception is not passed to `emit_internal_error`, because this
best-effort janitor contract deliberately continues to the probe; its closed
reason is the only operator-facing evidence.

### C4. Client teardown diagnostics and precedence

Add a registered `object_storage_client_close_degraded` event at
warning/degraded level. Its exact fields are:

```text
operation=object_storage_client_close
reason=object_storage_client_close_failed
error_code=internal_error
error_category=internal
is_retryable=false
```

`operation` is a new fixed, validated safe token
`object_storage_client_close`; it must not reuse `spool_cleanup`.

The runtime check calls `client_source.close()` exactly once in `finally` as
today. A non-cancellation exception emits this event and is suppressed only
after the event is successfully constructed. `CancelledError` remains
propagated. The close event never replaces an already-emitted probe event and
does not alter the probe's exit code. Thus a successful probe plus failed
close returns `0` with one success event and one close-degraded event; a failed
probe plus failed close returns `69` with both its failed probe event and the
close-degraded event. If the logger itself fails, the established diagnostic
fallback rules apply and no close exception may replace the original probe
outcome.

## Implementation boundaries

Expected touched files:

```text
packages/r2-object-storage/src/r2_object_storage/spool.py
packages/r2-object-storage/src/r2_object_storage/runtime_check.py
src/personal_os/diagnostics/events.py
tests/unit/object_storage/test_spool_manager.py
tests/unit/object_storage/test_runtime_check.py
tests/contract/object_storage/test_r2_runtime_contract.py
tests/unit/object_storage/test_error_diagnostics_contract.py
docs/operations/object-storage.md
```

No core `ErrorCode` is added: these are degraded operational conditions, and
the closed `reason` field is the established low-cardinality surface. Do not
alter the object-store adapter's put/get/integrity mappings.

## Test strategy and acceptance criteria

Write the failing tests before implementation. On Python 3.14 via `uv`:

1. A scan `OSError` returns a summary with
   `spool_cleanup_scan_failed`, and the runtime check emits exactly one
   cleanup-degraded event with that reason while still running one HeadBucket
   probe.
2. Per-entry `lstat` and `unlink` failures increase `failed_count`, use
   `spool_cleanup_entry_failed`, preserve unrelated skipped/deferred counts,
   and never reveal names or paths.
3. Deferred-only cleanup retains its exact count and carries
   `spool_cleanup_deferred`; a fully clean summary produces no cleanup event.
4. An injected janitor exception emits `spool_cleanup_janitor_failed`, never
   its exception text, and does not change the probe exit/result.
5. A close failure after both successful and unavailable probes emits exactly
   one `object_storage_client_close_degraded` event with the fixed closed
   fields; it neither suppresses nor duplicates the probe event or changes
   the exit code. Cancellation remains uncollapsed.
6. Event-registry and sensitive-diagnostics tests prove the new event names,
   fields, tokens and no-leak constraints are closed.
7. Focused object-storage unit/contract suites, strict type checking, lint
   and the repository verification command pass on the same commit.

## Documentation update

Update `docs/operations/object-storage.md` to say that janitor degradation
includes one closed reason plus safe counts, and that client-close degradation
is separately observable but does not alter the HeadBucket probe result or
exit code. The canonical observability document remains authoritative; no
architecture decision record is required.
