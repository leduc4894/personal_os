# R2 runtime cleanup observability task

Implement `docs/superpowers/plans/2026-08-24-r2-runtime-cleanup-observability.md`.

Definition of done: scan, entry, injected-janitor, and client-close failures
each emit a privacy-safe closed reason; HeadBucket remains read-only with
unchanged exits; focused tests, strict static checks, and the operator contract
pass under Python 3.14 through `uv`.
