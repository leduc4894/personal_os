# Task 7 device-sync timeout repair report

## Status

Complete. The reported failure was a Vitest wall-clock flake in the
production-stack device-sync journey, not a production behavior failure. The
implementation commit is `5d61e20916e6b6b4f5bcdc981568265e63b21664`.

## Evidence and scope

- Read diagnosis: `device-sync-timeout-diagnosis.md`.
- Added `vi.setConfig({ testTimeout: 30_000 })` to
  `apps/obsidian-plugin/src/device-sync/device-sync-journey.test.ts`.
- Added the existing repository rationale: real-timer settling under
  parallel V8 coverage can exceed Vitest's 5 s default; the change supplies
  wall-clock headroom only.
- Changed no production code, global Vitest configuration, 80-turn settling
  semantics, API, dependency, or assertion.
- No new assertion was practical: this repair changes only the test harness
  deadline and the existing journey assertions already reproduce the target
  behavior.

## Verification

Commands run from `apps/obsidian-plugin`:

| Command | Result |
| --- | --- |
| `pnpm exec vitest run src/device-sync/device-sync-journey.test.ts --coverage=false` | 1 file, 9 tests passed |
| `pnpm exec vitest run src/device-sync/device-sync-journey.test.ts` | 1 file, 9 tests passed with V8 coverage |
| `pnpm lint` | passed, zero warnings/errors |
| `pnpm type-check` | passed (`tsc --noEmit`) |
| `pnpm test` | 56 files, 1,245 tests passed with V8 coverage |

Vitest emitted its existing Vite native-config deprecation warning during test
runs; it did not affect the result and was outside this scoped repair.

## Self-review

`git diff 5d61e20^ 5d61e20 --stat` shows one test file only (5 insertions, 1
deletion). The timeout is localized, follows the accepted `vi.setConfig`
precedent, and has no runtime or contract impact.
