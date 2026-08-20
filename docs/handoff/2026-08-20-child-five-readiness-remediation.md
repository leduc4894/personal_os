# Child Five Readiness Remediation Handoff

## Status

Completed the final Child 5 readiness backlog closure after the implementation
commit `e15fb76` (`fix: redact small file value representations`). The completed
small-file-sync value-object redaction row has been removed from the living
backlog. No code, API, schema, policy, or architecture contract changed in this
documentation-closeout commit.

## Gate status

| Gate | Evidence |
| --- | --- |
| Scope check | `rg -n "Before Child 5" docs/handoff/BACKLOG.md` before the edit returned exactly one row: the 2026-08-20 `small-file-sync` value-object-redaction row at line 52. |
| Python lint | `uv run poe python-lint` exited 0: `All checks passed!` |
| Python type check | `uv run poe python-type-check` exited 0: `Success: no issues found in 161 source files` |
| Focused remediation suite | `uv run pytest tests/unit/sources tests/unit/identity tests/unit/recovery tests/unit/tools/test_postgresql_dump_process.py -q` exited 0: 376 passed, 4 skipped. |
| Focused remediation suite | `uv run pytest tests/unit/api_runtime tests/unit/small_file_sync -q` exited 0: 633 passed, 2 skipped, 1 existing FastAPI TestClient deprecation warning. |
| Focused remediation suite | `uv run pytest tests/contract/source_publication -q` exited 0: 19 passed. |
| Focused remediation suite | `uv run pytest tests/contract/api -q` exited 0: 135 passed, 1 existing FastAPI TestClient deprecation warning. |
| Documentation integrity | `git diff --check` exited 0; final `rg -n "Before Child 5" docs/handoff/BACKLOG.md` returned no rows. |

The required focused test selection was also launched as the single prescribed
command. The local command runner returned its bounded observation at 86% after
30 seconds without an exit status, so the same selected paths were rerun in the
four groups above to obtain conclusive exit-0 evidence. Their aggregate result
is 1,163 passed and 6 skipped.

Projection integration was not run: this closeout changes no source-dispatcher
transaction code. The preceding implementation commit changed small-file-sync
value-object representations only.

## Rulings and interpretations

- The binding design describes eleven original `Before Child 5` items, but the
  branch-base observation is authoritative for this closeout: ten were already
  removed by ancestor remediation commits. Removing them again would falsely
  portray already-closed dependencies as unresolved.
- Therefore this handoff removes only the remaining line observed at backlog
  line 52, for `SmallFileIdempotencyKey`, `NormalizedLocator`, and
  `UploadOperationToken` representation redaction.
- Later-gated rows, including Child 7, Child 9, production-activation,
  dependency-pin, and conditional gates, remain in `BACKLOG.md` unchanged.
- The existing deferred structural-enforcement row remains valid: do not add a
  shared redaction abstraction; reopen it only when a fourth sensitive-string
  value object is introduced without a redacted representation.

## Remaining linked deferred work

The deferred work indexed in [BACKLOG.md](BACKLOG.md) remains unchanged except
for the completed Child 5 row. In particular, the Child 4 reference-device
evidence remains gated before Child 9 acceptance closure, and the sensitive
value-object structural-enforcement item remains conditional on a fourth
value-object need.

## Next action

Begin Child 5: source-locator and tombstone lifecycle.

## Final implementation commit

`e15fb76` (`fix: redact small file value representations`) delivered the
redaction behavior whose completed backlog item this handoff closes.
