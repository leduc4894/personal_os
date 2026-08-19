# Publication Policy Decision Binding Handoff

## Final commit

The final Task 6 implementation commit is `e883cbb`. The implementation commits are
`5509151`, `3ff47a0`, `580942a`, `3c8d515`, `0de40d1`, and `e883cbb`;
`c68d0db` preserves shared application secrets during stack-secret rotation.

## Gate evidence

- PostgreSQL acceptance: `tests/integration/source_publication/test_small_file_operations.py` plus `tests/integration/exclusion_policy/test_source_publication_enforcement.py -m local_stack -q` — 26 passed.
- Focused regression after fixture hardening: source-publication enforcement plus policy migration — 23 passed.
- Live Obsidian through `wdio-obsidian-service`, Cloudflare Tunnel, real device grant, admin login and TOTP — 3 specs passed; sanitized journal evidence was one committed, zero pending, one mapped row.
- `uv run poe canonical-core-test` — 976 passed, 11 skipped.
- API contract check — current; small-file migration and API contract suites — 32 passed.
- Local stack secret tests — 155 passed, 3 skipped; Ruff, ESLint, TypeScript and diff checks passed.
- `uv run poe exclusion-policy-test` — 1498 passed, 2 skipped, 1 deselected. PostgreSQL client tools 18.4 were installed and the backup/restore coverage passed after the manifest count contract was aligned with `small_file_upload_operations`.

## Spec interpretations and rationale

The server owns the allowed policy revision. Preflight stores an immutable
`AllowedPolicyRevisionBinding`; plugin revision claims are not authority. At
the transaction-final policy lock, a verified unchanged revision reuses the
binding, while a revision change forces a fail-closed authoritative evaluation.
The binding is invocation-local through the small-file publication gateway;
there is no request-global mutable policy state.

## Deferred items and verdicts

The locator-free publication gap is closed and its BACKLOG index line is
removed. The signing-key verifier-chain item remains open.

## Canonical documentation links

- `docs/operations/plugin-journal-small-file-sync.md`
- `docs/operations/exclusion-policy-publication.md`
- `.local/RESTART.md` (local operator runbook; ignored local artifact)

## Next actions

Keep the local stack stopped when no live test is running; use the existing
Cloudflare Tunnel only for required HTTPS live journeys.
