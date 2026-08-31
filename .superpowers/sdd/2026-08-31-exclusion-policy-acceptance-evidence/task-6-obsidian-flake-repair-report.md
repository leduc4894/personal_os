# Task 6 Obsidian multipart chronology-flake repair

## Scope and root cause

This is a test-only remediation for GitHub Actions run `33399194078` attempts
1 and 2. The affected tests treated the receipt order of concurrent multipart
PUT workers as a contract. Workers re-fingerprint before acquiring the FIFO
semaphore, so a later-numbered part can reach the semaphore and transport
first. The production runner's durable behavior remains deterministic:
completed parts are persisted in ascending order and all workers are joined
before outcome evaluation.

No production scheduler or multipart behavior changed.

## Exact test contract

The resumed Mobile test now uses a controlled latch on the first re-read of
the frozen file. It holds part 2 before the semaphore, permits part 3 to reach
the PUT transport first, then releases part 2. The test asserts:

- exactly two PUTs, whose sorted membership is `[2, 3]`;
- a maximum of two active Mobile PUTs;
- a committed outcome;
- status before any resumed part URL, no create call; and
- durable completed progress `[1, 2, 3]` in ascending order.

The offline queue-driver test asserts four PUT attempts with sorted membership
`[1, 1, 2, 3]`, preserving the retry cardinality and eligible-part set without
requiring which concurrent worker was received first. It continues to assert
the retry-scheduled first pass and committed second-pass outcome.

## Red/green evidence

RED (before replacing the chronological assertion):

```text
pnpm --dir apps/obsidian-plugin exec vitest run src/journal/multipart-upload.test.ts -t "resumes only unfinished Mobile parts with maximum two active PUTs" --silent
FAIL expected [ 3, 2 ] to deeply equal [ 2, 3 ]
```

GREEN focused:

```text
multipart-upload focused test: 1 passed
queue-driver offline focused test: 1 passed
```

GREEN full plugin suite:

```text
pnpm --dir apps/obsidian-plugin test
Test Files 56 passed (56)
Tests 1245 passed (1245)
```

Quality gates:

```text
pnpm --dir apps/obsidian-plugin lint       # exit 0
pnpm --dir apps/obsidian-plugin type-check # exit 0
git diff --check                           # exit 0
```

Vitest emits the pre-existing Vite native-config deprecation warning; it did
not produce lint, type, or test failures.

## Changed files

- `apps/obsidian-plugin/src/journal/multipart-upload.test.ts`
- `apps/obsidian-plugin/src/journal/queue-driver.test.ts`

## Spec and quality self-review

The assertions now match the documented multipart contract: bounded Mobile
concurrency, unfinished-part membership/count, durable ascending progress,
and terminal/retry outcomes. The controlled latch proves the relevant
out-of-order arrival rather than relying on timing. Test harness changes are
private to the test file; no production code, public contract, dependency, or
architecture changed.

## Concerns

None. The targeted CI failure mode is deterministically covered. Test-only
repair commit: `e78253a31d83f9be56180d969566320c329d8ab9`.
