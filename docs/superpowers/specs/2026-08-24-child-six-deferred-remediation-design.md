# Child 6 Deferred Remediation Design

**Status:** Draft for review

**Phase:** Phase 2 — Obsidian sync, Child 6 readiness and closure support

**Governing contracts:** `docs/15-OBSERVABILITY_AND_ALERTING.md` §7,
`docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md` §§7,
12, `AGENTS.md`, and `docs/handoff/BACKLOG.md` rows 53–58, 60–62.

## 1. Purpose

Resolve the six agreed deferred-work streams that are either a Child 6
acceptance gate or are deliberately brought forward because Child 6 changes
the same plugin journal, error-contract, and release surfaces:

1. physical Mobile source-lifecycle acceptance;
2. terminal journal state for typed create rejections;
3. PostgreSQL SQLSTATE `23xxx` error-classification boundaries;
4. shared wire-golden coverage for `source_locator_conflict`;
5. plugin diagnostics-trail cleanup; and
6. the Python 3.14/Ruff PEP 758 formatting decision.

This is a remediation slice, not an implementation of cursor pull, manifest
reconciliation, remote apply, or repair. The later Child 6 design owns those
features; this slice removes known blockers and makes the already-landed
small-file/lifecycle behavior safe to build upon.

## 2. Non-negotiable invariants

- PostgreSQL remains canonical; the plugin SQLite database remains rebuildable
  journal state and never mints a canonical source identity.
- No change may log or persist raw Vault paths, content, bytes, digests,
  credentials, URLs, provider details, SQL, or exception text in diagnostics.
- Every new closed error path introduced or made terminal by this remediation
  must surface its closed reason token to at least one readable established
  surface: durable diagnostics trail, status/settings, or structured closed
  diagnostics log. A catch that discards a reason token is forbidden.
- The plan implementing this spec must contain a dedicated diagnostics-surface
  task. It must prove, with RED then GREEN tests, that every new or changed
  closed outcome is observable at code-land time; no diagnostic work may be
  deferred to Phase 10.
- Trail diagnostics remain observe-only, bounded, fire-and-forget, and unable
  to block a queue pass or change sync semantics.
- Public API/schema changes, if any, update OpenAPI snapshots, generated
  TypeScript client, contract tests, and living docs in the same change.

## 3. Scope and decisions

### D1 — Typed create rejection becomes terminal journal state

When the small-file create content endpoint returns a typed non-retryable
4xx, including `source_locator_conflict`, the queue driver must persist a
terminal `failed` operation state before returning the existing terminal
queue outcome (`blocked_conflict`, `excluded_policy`, or the mapped terminal
state). A typed rejection must never leave its operation row at `receiving`.

The persisted record carries only the existing closed reason token and safe
correlation identifier. The queue driver appends the same closed outcome and,
where present, envelope request ID to the durable diagnostics trail. Thus an
operator can distinguish an honest terminal rejection from a retryable wire
failure without inspecting a database or raw HTTP response.

### D2 — Narrow database error classification

The PostgreSQL adapter will classify only the explicitly documented
constraint/SQLSTATE cases as typed terminal business outcomes. Other class
`23xxx` integrity violations must not fall into the generic retryable
`source_commit_outcome_unknown` branch merely because they share an SQLSTATE
class with a known locator conflict.

The classification remains narrow: it must not turn an unknown constraint
into `source_locator_conflict`, and it must continue to redact SQLSTATE,
constraint names, SQL and driver text from every public/diagnostic surface.
The resulting closed application reason token is emitted through the existing
source publication/lifecycle diagnostics and reaches the plugin trail through
the existing wire-error mapping when a request is involved.

### D3 — Cross-language wire golden

Add `source_locator_conflict` to `tests/fixtures/small_file_sync/wire-golden.json`
and the corresponding Python and TypeScript replay tests. The golden requires
HTTP 409 with this closed code to map to the non-retryable plugin
`blocked_conflict` outcome, a terminal operation write, and a trail-readable
closed token. Registry/hash generated artifacts are updated by the repository
contract command, never hand-edited.

### D4 — Diagnostics-trail cleanup is a single observable contract task

Resolve the three `sync-error-tracing` rows as one compatible cleanup:

- validate `envelopeRequestId()` at construction, not only during load;
- use one unambiguous request-ID token name across plugin modules;
- make `syncFailureKind` narrowing consistent and correct the queue-driver
  claim so it describes only failures actually observed by that hook;
- type settings/export stop reasons with the existing closed token union;
- make the copy command absorb/report its own promise rejection through the
  established bounded diagnostics pattern;
- document the 999 append-failure saturation behavior and maintain per-
  vocabulary narrowing in the self-check.

None of these changes may widen diagnostics to arbitrary strings. Invalid
request IDs are rejected before trail persistence and produce the existing
safe fallback/null behavior; no raw invalid value may be displayed or logged.
Each altered failure path has a focused trail/settings/self-check assertion
proving its closed token remains readable.

### D5 — Formatter decision

Adopt the pinned Python 3.14/Ruff output `except A, B:` as repository style.
Do not constrain Ruff or perform a broad formatter rewrite. The remediation
updates the relevant style documentation/test expectation if one exists,
runs the repository format gate, and removes the backlog row only when the
formatter is stable on the affected files.

### D6 — Mobile lifecycle acceptance

Run the existing eight-scenario source-lifecycle matrix on a physical Obsidian
Mobile device: tracked rename, tracked move, delete, automatic restore,
explicit restore, offline capture/reconnect, unload/reload, and policy denial.
Record only sanitized result metadata and evidence references in
`docs/operations/source-locator-tombstone-lifecycle.md`, then run the
reference-device-record contract test and remove the single matching BACKLOG
row. This is mandatory before Child 6 acceptance closes, not a substitute for
the Child 6 Desktop/live acceptance itself.

The Mobile run uses the existing approved live bootstrap/runbook paths. It is
not mocked, substituted by automated tests, or marked PASS without physical
evidence. Any closed failure encountered during the run must be read back from
the plugin trail/status or server's closed diagnostics surface and recorded
only as its safe token.

## 4. Out of scope

- Cursor schema/endpoints, incremental pull, gap detection, atomic remote
  apply, echo suppression, manifest pages, offline registration, SQLite-loss
  repair, multipart upload, and conflict resolution.
- Any deferred item whose trigger is Child 9, production activation,
  multi-worker serve, policy-worker operations, or a different domain.
- New observability infrastructure, raw diagnostic export, or a new metrics
  backend.

## 5. Error and diagnostics matrix

| Condition | Behavioral result | Required readable surface |
|---|---|---|
| Typed create 4xx | terminal operation state; no retry | trail records mapped closed token plus opaque request ID when supplied |
| Known locator conflict | `blocked_conflict`; no overwrite | queue terminal state and trail token `source_locator_conflict` |
| Unknown integrity violation | narrow typed/redacted application error; no false locator label | existing closed API/publication diagnostic route/log and wire-to-trail path |
| Invalid envelope request ID | not persisted as a token | bounded safe fallback; never echoes input |
| Trail append/copy persistence failure | original sync outcome unchanged | existing bounded append-failure counter/self-check token |
| Mobile lifecycle rejection | no silent lifecycle mutation | plugin trail/status or lifecycle rejection surface with closed token |

## 6. Acceptance criteria

1. Every behavior change begins with a failing test and reaches GREEN with
   focused Python/TypeScript tests.
2. A typed create 409 persists terminal operation state and a trail-visible
   closed token; a retryable failure remains retryable and cannot be falsely
   terminalized.
3. Classification tests cover known locator conflict, unrelated unique/
   integrity failures, safe public error mapping, and redaction.
4. The Python and TypeScript wire-golden suites replay
   `source_locator_conflict` identically; generated contract artifacts pass
   their freshness check.
5. Diagnostics cleanup tests prove constructor UUID validation, closed-token
   types/narrowing, bounded append/copy failure handling, and no newly silent
   closed path.
6. Plugin lint, typecheck, build and Vitest; Python lint, format, mypy strict,
   relevant unit/contract tests, and the repository composed verification gate
   pass on the final code commit.
7. The physical Mobile matrix has PASS evidence in the living runbook and its
   reference-device-record test passes before Child 6 acceptance closure.
8. Resolved BACKLOG rows are removed; any item genuinely not complete remains
   exactly once with its existing explicit implement-by trigger.

## 7. Documentation and handoff

The implementation updates the two living operations documents only for
changed operator behavior, keeps `BACKLOG.md` as the live deferred index, and
writes one Child 6 remediation handoff with final commit SHA, gate evidence,
diagnostics-surface evidence, decisions, and any remaining deferred rows.
