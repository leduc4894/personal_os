# Object Storage Backlog Retirement Design

## Objective

Retire the 2026-08-14 `object-storage` rows that alter canonical correctness,
availability, observability, or live-test safety. A row leaves the index only
after a regression test proves it or this document records a precise ruling.

## Binding behavior

1. `CanonicalObjectKey.parse(value: str)` accepts only
   `objects/sha256/{digest[0:2]}/{digest[2:4]}/{digest}` for an existing
   lowercase 64-hex `ContentDigest`.
2. Free-space probing must not block the asyncio event loop. Admission keeps
   its lock, permit and reservation atomicity and maps lack of space or timeout
   to `OBJECT_STORAGE_BUSY`.
3. A stalled stream is bounded by the real-time receive backstop, returns the
   existing `OBJECT_STORAGE_INPUT_INVALID` / `stream_invalid` error, and
   removes its spool and reservation.
4. Single-flight waiters receive an equivalent fresh typed failure rather than
   the owner exception instance, report zero provider attempts, and cannot
   cancel the owner or leak registry state.
5. Failure metrics include `InternalApplicationError`; the reserved-size gauge
   is emitted after every reservation mutation.
6. Runtime duration starts immediately before the probe. Operator text names
   only actually emitted events.
7. The live workflow cannot cancel an in-progress cleanup, and live setup
   cleans local spool roots if settings loading fails. Hosted R2 evidence is
   external and cannot be faked by offline tests.

## Terminal rulings

- `PutObjectRequest` already exposes `content_md5_base64` plus compatible
  `content_md5` with contract coverage: retire its row as implemented.
- The two shielded-cleanup helpers have distinct ownership and only two
  callers: do not invent a shared abstraction before a real third caller.
- Keep the defensive unretrieved-future guard; tests prove owner failure is
  retrieved before registry removal instead of refactoring it speculatively.

## Non-goals

- No schema, API/OpenAPI, provider, source-of-truth, or dependency change.
- The pre-existing diagnostics/error-contract circular-import row belongs to
  the separate Wave 1 diagnostics slice and remains indexed.
- Credentials, bucket mutation, workflow dispatch and hosted evidence remain
  external actions.

## Acceptance

- Every changed behavior has observed RED then GREEN evidence.
- Relevant object-storage tests and `uv run poe verify` pass.
- The handoff records terminal rows/rulings and external live-gate status
  without secrets or endpoint values.
