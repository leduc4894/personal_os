# Linux CI Stack Activation Handoff

**Date:** 2026-08-15
**Scope:** Bước 1 của đánh giá Phase 1 — push 47 commits chờ trên local
`master` lên origin và hoàn tất vòng xác minh CI từ xa lần đầu (live-R2 gate,
baseline, local-stack, quality).
**Final code commit:** `ca5ea8d` (diagnosis reproduction) — all fixes landed
in the range `b643751..ca5ea8d`; the documentation commit (this handoff, ops
doc, diagnosis-workflow removal) follows.

Living operational status: `docs/operations/canonical-core-recovery.md`
(Acceptance status).

## Gate status (with evidence)

| Gate | Status and evidence (2026-08-15) |
|---|---|
| `canonical core acceptance` (protected live R2) | **green** — 6/6 live-R2 drills passed; runs [31886915923](https://github.com/leduc4894/personal_os/actions/runs/31886915923), 31886972234, 31887267161 |
| `canonical PostgreSQL baseline` | **green** — full lifecycle in 23m9s, run 31885979945 |
| `local service stack` | **green** — first ever green CI run (4m38s), run 31885979934 |
| `quality` (Ubuntu + Windows portability) | **green** — run 31887267157 |
| `object storage live R2` | **green** — runs 31887267153 et al. |
| Local offline suite after all fixes | green — `uv run pytest -q` 1424 passed, 19 skipped, 112 deselected; lint/mypy/format/boundary all green |

## What was found and fixed (each with a failing test first)

1. **`.local` created 0755 by workflow steps** (`b643751`): `mkdir -p
   .local/test-results` before the stack tool created the secret parent at
   the default umask; bootstrap requires 0700 and failed closed with
   `unsafe_secret_set` (exit 65). Windows never sees this (mode checks skip
   on win32). Fix: `install -d -m 700 .local` in the two affected workflows,
   pinned by `test_ci_security.py`.
2. **Non-root services cannot read 0600 host-owned bind-mount secrets on
   real Linux** (`6e2a3b9`, `747cbb4`, `ffb823d`): Compose bind-mounts file
   secrets with host ownership (uid 1001, mode 0600). Docker Desktop on
   Windows presents mounts as world-readable, which is why every local gate
   passed while Linux CI failed closed.
   - temporal (uid 1000): service starts as root; the entrypoint drops back
     via `su temporal -c` before exec'ing the server.
   - neo4j (uid 7474): its entrypoint hand-rolls readability from mode bits
     and the effective user, so even root fails the check; a wrapper
     materializes a neo4j-owned 0400 copy under `/run/neo4j-secrets` and
     repoints `NEO4J_AUTH_FILE`.
   - redis (uid 999): the image entrypoint drops to redis before the server
     opens the ACL file; a wrapper materializes a redis-owned 0400 copy
     under `/run/redis`.
   - Because the neo4j entrypoint is overridden, Compose no longer appends
     the image CMD — `command: ["neo4j"]` must stay explicit.
   All pinned in `test_local_service_stack_contract.py`.
3. **Port availability probe without `SO_REUSEADDR`** (`0a61394`): Docker
   publishes stack ports with SO_REUSEADDR; the probe's plain bind read
   TIME_WAIT sockets left by the previous per-test up/down cycle as
   occupied (`port_unavailable`, exit 64) in cyclic suites.
4. **Claim-mismatch drill payload off by one byte** (`0a61394`): the two
   headlines ("claimed"/"supplied") differed in length, violating the
   helper's same-length contract; first live run failed `assert 64 == 63`.
5. **Missing pinned pg client on the runner** (`4904168`, `1b97fa6`):
   recovery drills require pg_dump/pg_restore exactly 18.4 on PATH; the
   runner ships an older major and Debian keeps the binaries outside PATH —
   the workflow now installs `postgresql-client-18=18.4*` from PGDG and
   symlinks both binaries into `/usr/local/bin`.
6. **Windows-path bundle test** (`de2a971`): expected path built from the
   unresolved mkdtemp root (8.3 `RUNNER~1` form on the Windows runner) no
   longer matched the store's resolved root.

## Interpretive decisions (with rationale)

1. **Wrapper materialization over file-mode changes.** Making secret files
   group-readable or re-owning them would break the tool's owner/mode
   validation contract (0600, current-user owner) pinned across many tests;
   a non-root runner also cannot chgrp to an arbitrary gid. Root-entrypoint
   wrappers keep the host contract intact and the server processes
   non-root; healthchecks keep reading the original secret paths as root.
2. **`su` in the temporal entrypoint** preserves exported environment for
   the dropped-privilege child (verified against the real image; busybox su
   without `-` keeps env).
3. **SO_REUSEADDR probe parity**: the probe now binds exactly the way
   Docker publishes; an active listener still fails the probe (no
   SO_REUSEPORT), so the check keeps its meaning.
4. **Temporary diagnosis workflow** (`stack-startup-diagnosis.yml`,
   workflow_dispatch-only) was used for four rounds of raw-output
   diagnosis and removed in the documentation commit; it reproduced the
   production `up` path, the acceptance ports, the apt install and the
   exact pytest fixture — all green — which localised the remaining
   transient.

## Deferred items (verdicts)

- **Image pulls inside the startup deadline (was: suspected Docker Hub rate
  limiting).** Two acceptance runs failed `up` within seconds; after the
  green runs, manual dispatches of the two scheduled workflows on HEAD
  failed again — one stuck for the full 180s with zero containers created,
  which localised the cause: `docker compose up` was pulling gigabytes of
  pinned images inside the 180s startup deadline meant for health waiting.
  Verdict: **mitigated in `685696e`** — the three stack workflows now
  prefetch the pinned images (`docker compose pull --quiet`) before their
  live steps, keeping pull latency out of the startup deadline; pinned by
  `test_stack_workflows_prefetch_images_before_live_gates`. No retry loop
  was added; if the transient recurs despite prefetch, add a single
  bounded retry of the live-suite step or a Docker Hub token secret.
- **STACK failures hide compose stderr by design** (privacy contract);
   diagnosis required a temporary workflow. Verdict: deferred — a bounded
   sanitized compose-output tail in the startup failure payload would be a
   contract change; revisit only if startup failures become frequent.
- **No BACKLOG lines added** — the two items above are transient/diagnostic
   observations, not accepted-but-undone work; this handoff records them.

## Next actions

1. Continue Phase 2 (Obsidian sync) per `docs/20-IMPLEMENTATION_PLAN.md`;
   the identity graph, canonical publication/read and recovery paths from
   Phase 1 are now CI-verified end to end on Linux.
2. Watch the daily scheduled runs (`41 3 * * *`) for the deferred
   rate-limit transient; act per the verdict above if it fires.
