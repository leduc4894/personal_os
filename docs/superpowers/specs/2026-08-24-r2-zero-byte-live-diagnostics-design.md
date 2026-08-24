# R2 Zero-Byte Live Diagnostics Design

**Status:** Proposed
**Date:** 2026-08-24
**Scope owner:** Live Cloudflare R2 contract observability
**Depends on:** `docs/superpowers/specs/phase 1/content-addressable-object-storage-design.md`, `docs/superpowers/specs/2026-08-24-closed-reason-surfacing-remediation-design.md`

## 1. Objective

Expose a safe, actionable reason token when the protected live R2
zero-byte round-trip fails, without disclosing endpoints, bucket names,
object keys, payload bytes, request headers or provider exception text.

## 2. Evidence and current boundary

The protected R2 workflow fails only `test_zero_byte_round_trip`; its other
eight live cases pass. The same failure repeats across protected runs from
2026-08-22 through 2026-08-24, including runs at an unchanged R2 source
revision. Credentials, ordinary object writes, reads, corruption detection
and exact-key cleanup therefore have positive evidence.

Published JUnit deliberately replaces every failure with
`r2_live_failure_details_redacted`. That privacy control is correct, but it
leaves no readable reason token for the closed failure path and prevents an
evidence-based decision about the adapter or provider boundary.

This spec does not assume that R2 rejects zero-byte objects. The cause remains
unconfirmed until the new diagnostics identify the failing stage and safe
classification.

## 3. Scope

In scope:

- Add a closed, non-sensitive live-test diagnostic record for the zero-byte
  case only.
- Distinguish the stages `store`, `resolve` and `read`.
- Classify the caught failure into a closed reason token derived from existing
  application errors or a provider-safe category.
- Preserve exact-key cleanup and sanitize every artifact before upload.

Out of scope:

- Removing, skipping, retry-looping around, or weakening the zero-byte
  contract.
- Logging raw provider exceptions, HTTP headers/status bodies, endpoint,
  bucket, canonical key, digest, payload or credentials.
- Changing the R2 adapter's zero-byte behavior before diagnostic evidence
  proves the violated component contract.
- Production bucket access, bucket listing, prefix deletion or a new tunnel.

## 4. Diagnostic contract

On any failure in the zero-byte test body, the test emits exactly one safe
diagnostic record before re-raising. It has this closed shape:

```json
{
  "event": "r2_live_zero_byte_failed",
  "stage": "store | resolve | read",
  "reason": "<closed reason token>"
}
```

`reason` uses an existing `ApplicationError.error_code` when one is available.
For a non-application provider exception, the only permitted categories are
`provider_client_error`, `provider_timeout`, `provider_transport_error` and
`provider_unclassified_error`. Classification must inspect exception type and
safe SDK metadata only; arbitrary exception strings and chained causes are
forbidden.

The safe record may appear in the protected job log and in a sanitized JUnit
`system-out` field for this case. The sanitizer must strip all other captured
streams, properties, traceback text and failure/error content. The artifact
must retain only the test identity, pass/fail state, this fixed-schema record
when available, and the existing redacted failure marker.

If diagnostic serialization itself fails, the original test failure remains
primary and the emitted reason is `diagnostic_emission_failed`; no exception
text is added.

## 5. Investigation flow

1. Run the focused protected workflow after this diagnostic change.
2. Read the sanitized result and classify the failure by stage/reason.
3. Compare with the eight passing cases and the adapter's offline zero-byte
   contracts.
4. Only then write a separate adapter or provider-configuration fix spec.

No semantic adapter modification is part of this spec. A repeatable provider
failure with a safe token is a valid completed outcome for this diagnostic
remediation, even if the zero-byte acceptance gate remains red.

## 6. Required tests

- Unit test each exception-classification branch and prove no arbitrary
  exception message can enter the record.
- Unit test a `store`, `resolve` and `read` failure maps to its exact stage.
- Unit test diagnostic-emission failure preserves the original exception and
  emits only `diagnostic_emission_failed`.
- Extend JUnit sanitizer tests to prove the one permitted zero-byte diagnostic
  is retained while a sensitive `system-out`, traceback, property or ordinary
  test output is removed.
- Run existing offline object-storage contract tests unchanged.

## 7. Acceptance criteria

- The live zero-byte case remains selected and fails closed if the underlying
  boundary remains faulty.
- A failed protected run yields exactly one readable fixed-schema diagnostic
  with an allowed stage and reason token.
- No endpoint, bucket, key, digest, payload, secret, provider exception text
  or arbitrary captured stream appears in the uploaded JUnit artifact.
- All eight non-zero-byte live cases continue to pass.
- `uv run poe verify`, focused sanitizer/classifier tests and `git diff --check`
  pass before the protected workflow is dispatched.

