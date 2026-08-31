# Web-auth Multi-worker Poll and Admin Client Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the four web-auth/admin-client BACKLOG rows (2026-08-16 §9 poll replay + pacing, 2026-08-16 §13 multi-worker pacing schema, 2026-08-30 stale journeys, 2026-08-30 envelope-helper duplicates) by making grant-poll replay survive keyring rotation and poll pacing durable.

**Architecture:** The replay digest becomes a per-key map (current + retained-previous master key) so a rotation mid-grant still matches the stored digest. Poll pacing moves from the in-memory `GrantPollPacer` to the existing `authentication_throttle_buckets` table under a new closed `bucket_kind` value `grant_poll`, paced BEFORE credential verification so unknown credentials throttle too. The two stale Playwright journeys are rewritten against the post-99fe1c3 flow; duplicated envelope helpers collapse onto `authentication-client.ts`.

**Tech Stack:** Python 3.14 (mypy strict, ruff), SQLAlchemy Core store, Alembic, pytest; TypeScript strict, Playwright + MSW, pnpm workspace.

**Spec:** `docs/superpowers/specs/backlog/2026-08-31-web-auth-multi-worker-poll-and-admin-client-hardening-design.md`

## Global Constraints

- No raw username, source address, credential or token in any bucket row, digest record, log line or metric label (spec 15.8 invariant).
- New closed paths surface only existing registry codes; no paths, hostnames or exception text.
- No new production dependency. Schema changes land as Alembic revision + upgrade/downgrade tests + `CANONICAL_POSTGRESQL_SCHEMA_REVISION` bump in one commit.
- Each BACKLOG row is removed in the same diff that closes it (living-index rule).
- Plan-review ratifications embedded here: the bucket token is `grant_poll`; `GrantPollPacer` is REMOVED (not kept as cache — one less cache-invalidation class; the DB roundtrip is negligible next to the poll handler's existing work).
- Plan-time reality note (already folded into the spec): the `quality.yml` `authentication-e2e` job has run the journeys since commit `0606cf7`; no CI wiring work remains.

---

### Task 1: Multi-key poll replay digest

**Files:**
- Modify: `src/personal_os/authentication/device_tokens.py` (constructor ~L823-832, `exchange_grant` command build ~L877-881)
- Modify: `src/personal_os/authentication/device_authorization.py` (no change expected — creation-side digest stays single/current-key)
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/device_authorization_store.py` (`poll_exchange` ~L425-469)
- Modify: `apps/api/src/api_runtime/authentication_composition.py` (offline double `poll_exchange` ~L1539-1571; serve composition passes `previous_master_key`)
- Test: `tests/unit/authentication/test_device_tokens.py`, `tests/integration/authentication/test_device_token_replay.py`

**Interfaces:**
- Consumes: `derive_grant_replay_hmac_key(crypto, master_key) -> bytes`, `polling_credential_hash_of(hmac_key, polling_credential) -> str` (both exist).
- Produces: `ExchangeGrantCommand.previous_polling_secret_hash: str | None` (new optional field); `DeviceTokenService.__init__(..., previous_master_key: bytes | None = None)`; store/double match on either digest.

- [ ] **Step 1: Write the failing unit test (service computes and passes the previous-key digest)**

In `tests/unit/authentication/test_device_tokens.py`, add (mirror the file's existing fake-exchange construction used by other `DeviceTokenService` tests; keep the fake minimal):

```python
def test_exchange_command_carries_the_previous_key_digest_after_rotation() -> None:
    """A rotation mid-grant must not break replay: the exchange command
    carries the digest under BOTH the current and the retained previous
    master key (BACKLOG 2026-08-16 §9)."""

    captured: dict[str, object] = {}

    class _CapturingExchange:
        async def poll_exchange(self, command: ExchangeGrantCommand) -> object:
            captured["command"] = command
            raise AuthenticationError(ErrorCode.DEVICE_AUTHORIZATION_PENDING,
                                      safe_details={"retry_after_seconds": 5})

    service = build_device_token_service(  # the file's existing builder helper
        exchange=_CapturingExchange(),
        master_key=b"\x01" * 32,
        previous_master_key=b"\x02" * 32,
    )
    with pytest.raises(AuthenticationError) as raised:
        anyio.run(service.exchange_grant, *_the_files_existing_argument_shape())
    assert raised.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_PENDING
    command = cast(ExchangeGrantCommand, captured["command"])
    current = polling_credential_hash_of(hmac_key=service._grant_hmac_key, polling_credential=_CREDENTIAL)
    previous = polling_credential_hash_of(hmac_key=service._previous_grant_hmac_key, polling_credential=_CREDENTIAL)
    assert command.polling_secret_hash == current
    assert command.previous_polling_secret_hash == previous
    assert current != previous
```

If the file has no builder helper, construct `DeviceTokenService` directly with the same dependencies its other tests use — the assertion contract above is the deliverable.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/authentication/test_device_tokens.py -k previous_key_digest -q`
Expected: FAIL — `DeviceTokenService.__init__()` got an unexpected keyword `previous_master_key` (or `previous_polling_secret_hash` attribute missing).

- [ ] **Step 3: Implement**

`device_tokens.py` constructor (next to the existing `self._grant_hmac_key = derive_grant_replay_hmac_key(...)` at ~L830-832):

```python
self._grant_hmac_key = derive_grant_replay_hmac_key(crypto=crypto, master_key=master_key)
self._previous_grant_hmac_key: bytes | None = (
    derive_grant_replay_hmac_key(crypto=crypto, master_key=previous_master_key)
    if previous_master_key is not None
    else None
)
```

In `exchange_grant`'s `ExchangeGrantCommand(...)` build (~L877-881) add:

```python
    previous_polling_secret_hash=(
        polling_credential_hash_of(
            hmac_key=self._previous_grant_hmac_key, polling_credential=polling_credential
        )
        if self._previous_grant_hmac_key is not None
        else None
    ),
```

Add the field to the `ExchangeGrantCommand` dataclass (`device_authorization.py`, next to `polling_secret_hash`) with default `None` and a docstring line: "digest under the retained previous replay key; set only while the two-key keyring retains the grant-issuing key".

Store `poll_exchange` (~L451) — replace the single-digest where-clause:

```python
_matchable_digests = [command.polling_secret_hash]
if command.previous_polling_secret_hash is not None:
    _matchable_digests.append(command.previous_polling_secret_hash)
...
.where(
    device_authorization_grants.c.polling_secret_hash.in_(_matchable_digests)
)
```

The existing `row.grant_id != command.grant_id` check (L456-457) already disambiguates a hash collision across grants. Mirror the same two-digest match in the offline double (`authentication_composition.py` ~L1539-1571) and pass `previous_master_key` from the serve composition where the two-key keyring exposes the retained key (grep the composition for the existing previous-key plumbing used by TOTP re-encryption; use the same source).

- [ ] **Step 4: Run the unit test to verify it passes**

Run: `uv run pytest tests/unit/authentication/test_device_tokens.py -q`
Expected: PASS (whole file).

- [ ] **Step 5: Write the failing integration test (real rotation mid-grant), then make it pass**

In `tests/integration/authentication/test_device_token_replay.py`, reuse the rotation harness of `test_exchange_replay_is_terminated_after_first_rotation` (L493) and the replay harness of `test_lost_exchange_acknowledgement_replays_identical_credentials` (L458). New test:

```python
@pytest.mark.asyncio
async def test_keyring_rotation_mid_grant_preserves_exact_poll_replay(...) -> None:
    """Rotation BETWEEN two polls of the same pending grant keeps the
    exchange an exact replay instead of an invalid credential (BACKLOG §9)."""
    # 1. create grant + first poll under key A (existing harness calls)
    # 2. rotate the keyring A -> B (the harness call used by the L493 test)
    # 3. poll again with the SAME credential
    # assertions:
    #   - the second poll does NOT raise DEVICE_CREDENTIAL_INVALID
    #   - after exchanging, a replayed exchange returns byte-identical credentials
    #     (same assertion block as the L458 test's replay section)
```

Run: `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan1-t1-* uv run pytest tests/integration/authentication/test_device_token_replay.py -m local_stack -q`
Expected before fix: the second poll raises `DEVICE_CREDENTIAL_INVALID`; after: PASS.

- [ ] **Step 6: Commit + retire the §9 digest half**

```bash
git add src/personal_os/authentication/device_tokens.py src/personal_os/authentication/device_authorization.py packages/postgresql-source-store/src/postgresql_source_store/device_authorization_store.py apps/api/src/api_runtime/authentication_composition.py tests/unit/authentication/test_device_tokens.py tests/integration/authentication/test_device_token_replay.py
git commit -m "fix: keep grant poll replay exact across keyring rotation"
```

Note in the commit body: the slow-down-hint / unknown-credential / pacer-scope halves of the 2026-08-16 §9 row close in Task 3 — the row stays indexed until then.

---

### Task 2: The `grant_poll` bucket kind (enum + migration + revision bump)

**Files:**
- Modify: `src/personal_os/authentication/sessions.py:107-115` (`ThrottleBucketKind`)
- Create: `migrations/versions/20260901_01_add_grant_poll_pacing_bucket_kind.py`
- Modify: `src/personal_os/database_schema.py:23` (`CANONICAL_POSTGRESQL_SCHEMA_REVISION`)
- Modify: `docs/superpowers/specs/2026-08-16-web-auth-and-device-authorization-design.md` §15.8 (bucket_kind list) and §11.4 (durable pacing under multi-worker)
- Test: `tests/unit/migrations/` (new file, following `test_device_sync_migration.py` structure), plus whichever existing tests pin the six-value set (grep first)

**Interfaces:**
- Produces: `ThrottleBucketKind.GRANT_POLL = "grant_poll"`; revision `20260901_01` (down_revision `20260829_01`); CHECK constraint `ck_authentication_throttle_buckets__bucket_kind` recreated with seven values.

- [ ] **Step 1: Find the existing six-value pins**

Run: `rg -n "recovery_verification" tests/ src/ migrations/ | grep -v sessions.py`
Every hit that pins the closed set (migration contract test, enum exhaustiveness test) gets the seventh value in this task.

- [ ] **Step 2: Write the failing migration test**

Create `tests/unit/migrations/test_grant_poll_pacing_bucket_kind_migration.py` (follow the import/graph-assertion style of `tests/unit/migrations/test_device_sync_migration.py`):

```python
def test_grant_poll_pacing_revision_extends_the_closed_bucket_kind_set() -> None:
    module = _load_revision_module("20260901_01_add_grant_poll_pacing_bucket_kind")
    assert module.revision == "20260901_01"
    assert module.down_revision == "20260829_01"
    assert "grant_poll" in module.UPGRADE_KIND_LIST
    assert "grant_poll" not in module.DOWNGRADE_KIND_LIST

def test_canonical_schema_revision_points_at_the_new_head() -> None:
    from personal_os.database_schema import CANONICAL_POSTGRESQL_SCHEMA_REVISION
    assert CANONICAL_POSTGRESQL_SCHEMA_REVISION == "20260901_01"
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/unit/migrations/test_grant_poll_pacing_bucket_kind_migration.py -q`
Expected: FAIL — module not found / revision mismatch.

- [ ] **Step 4: Implement enum + migration + bump**

`sessions.py`:

```python
    RECOVERY_VERIFICATION = "recovery_verification"
    GRANT_POLL = "grant_poll"
```

(docstring line: "closed throttle-bucket kinds (spec 8.3, binding decision 2; `grant_poll` added by the 2026-08-31 multi-worker pacing spec amendment)".)

Migration `migrations/versions/20260901_01_add_grant_poll_pacing_bucket_kind.py` (copy `SCHEMA_NAME` and header conventions from `20260816_01`):

```python
revision: str = "20260901_01"
down_revision: str | None = "20260829_01"

UPGRADE_KIND_LIST = (
    "login_username", "login_source", "grant_creation", "user_code_lookup",
    "totp_verification", "recovery_verification", "grant_poll",
)
DOWNGRADE_KIND_LIST = UPGRADE_KIND_LIST[:-1]
_CONSTRAINT_NAME = "ck_authentication_throttle_buckets__bucket_kind"

def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "authentication_throttle_buckets",
                       schema=SCHEMA_NAME, type_="check")
    op.create_check_constraint(
        _CONSTRAINT_NAME, "authentication_throttle_buckets",
        "bucket_kind IN (" + ", ".join(f"'{k}'" for k in UPGRADE_KIND_LIST) + ")",
        schema=SCHEMA_NAME,
    )

def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "authentication_throttle_buckets",
                       schema=SCHEMA_NAME, type_="check")
    op.create_check_constraint(
        _CONSTRAINT_NAME, "authentication_throttle_buckets",
        "bucket_kind IN (" + ", ".join(f"'{k}'" for k in DOWNGRADE_KIND_LIST) + ")",
        schema=SCHEMA_NAME,
    )
```

`database_schema.py`: `CANONICAL_POSTGRESQL_SCHEMA_REVISION: Final[str] = "20260901_01"`. Update every test found in Step 1. Amend spec §15.8's `bucket_kind` list (add `grant_poll` with a one-line note) and §11.4's no-durable-pacing sentence (point at the durable bucket; single-worker behavior unchanged).

- [ ] **Step 5: Run migration gates on a disposable stack**

```bash
CI=true bash .local/serve-live-ci.sh up knowledge-ci-plan1-t2-*
uv run alembic upgrade head        # -> 20260901_01
uv run alembic -x allow_destructive=true downgrade 20260829_01
uv run alembic upgrade head
bash .local/serve-live-ci.sh down
```
Expected: all three exit 0. Then: `uv run poe verify` (exit 0).

- [ ] **Step 6: Commit**

```bash
git add src/personal_os/authentication/sessions.py migrations/versions/20260901_01_add_grant_poll_pacing_bucket_kind.py src/personal_os/database_schema.py docs/superpowers/specs/2026-08-16-web-auth-and-device-authorization-design.md tests/unit/migrations/
git commit -m "feat: add the grant_poll throttle bucket kind for durable poll pacing"
```

---

### Task 3: Durable poll pacing (replace `GrantPollPacer`)

**Files:**
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/device_authorization_store.py` (new `pace_grant_poll` next to `resolve_throttle_bucket` ~L194)
- Modify: `src/personal_os/authentication/device_tokens.py` (delete `GrantPollPacer` L509-564 + constants L125-126 + `poll_pacer` ctor param L823/829 + `_raise_pending_or_slow_down` L899-917; restructure `exchange_grant`)
- Modify: `apps/api/src/api_runtime/authentication_composition.py` (offline double gains `pace_grant_poll`; serve composition drops pacer construction)
- Modify: `docs/operations/web-authentication-and-device-authorization.md` (reverse-proxy multi-worker note)
- Test: `tests/unit/authentication/test_device_tokens.py` (delete `test_poll_pacer_starts_at_five_seconds_and_backs_off` L373), `tests/unit/postgresql_source_store/` (new pacing tests), `tests/integration/authentication/test_device_token_replay.py` (slow-down + unknown-credential tests updated)

**Interfaces:**
- Produces (port method, implemented by store + offline double):
  `async def pace_grant_poll(self, *, polling_credential_hash: str, database_now: datetime) -> int | None` — returns the remaining too-fast seconds, or `None` when the poll is admissible.

- [ ] **Step 1: Write the failing store test (window + doubling + reset)**

In the device-authorization-store test module used by sibling throttle tests, add:

```python
@pytest.mark.asyncio
async def test_pace_grant_poll_windows_double_and_reset(store, database_now) -> None:
    digest = "a" * 64
    assert await store.pace_grant_poll(polling_credential_hash=digest, database_now=database_now) is None
    # too fast: 5s window anchored at the first poll
    soon = database_now + timedelta(seconds=1)
    assert await store.pace_grant_poll(polling_credential_hash=digest, database_now=soon) == 4
    # repeat violation doubles the anchored window (10s from window start)
    sooner = database_now + timedelta(seconds=2)
    assert await store.pace_grant_poll(polling_credential_hash=digest, database_now=sooner) in {8, 9}
    # after the window expires the poll is admissible again and the level resets
    late = database_now + timedelta(seconds=61)
    assert await store.pace_grant_poll(polling_credential_hash=digest, database_now=late) is None
    assert await store.pace_grant_poll(polling_credential_hash=digest,
                                       database_now=late + timedelta(seconds=1)) == 4
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/postgresql_source_store -k pace_grant_poll -q`
Expected: FAIL — attribute missing.

- [ ] **Step 3: Implement the store method**

In `device_authorization_store.py` (bucket upsert follows the store's existing guarded-insert convention; window math anchors at `window_started_at`):

```python
_GRANT_POLL_BASE_INTERVAL_SECONDS: Final[int] = 5      # POLL_INTERVAL_SECONDS
_GRANT_POLL_MAXIMUM_INTERVAL_SECONDS: Final[int] = 60  # spec 11.4 back-off cap

async def pace_grant_poll(self, *, polling_credential_hash: str, database_now: datetime) -> int | None:
    """Durable grant-poll pacing under ``bucket_kind='grant_poll'``.

    The window anchors at the first admissible poll; each too-fast poll
    doubles the anchored interval up to the cap. The bucket key is the
    HMAC digest of the presented credential, so unknown credentials pace
    identically without leaking existence. Returns the remaining
    too-fast seconds, or ``None`` when admissible.
    """
    async with self._engine.begin() as connection:
        state = await self._select_bucket(
            connection, bucket_kind=ThrottleBucketKind.GRANT_POLL, bucket_hash=polling_credential_hash
        )
        if state is None or state.locked_until is None or state.locked_until <= database_now:
            # (re)open the window: level 0, deadline = now + base interval
            ...guarded insert-or-reset using the uq_authentication_throttle_buckets__kind_hash
            # collision path (upsert guard) mirrors apply_throttle_bucket_failure
            return None
        level = state.failed_attempt_count + 1
        interval_seconds = min(
            _GRANT_POLL_BASE_INTERVAL_SECONDS * (2 ** level), _GRANT_POLL_MAXIMUM_INTERVAL_SECONDS
        )
        # deadline stays anchored: window_started_at + doubled interval
        ...guarded update failed_attempt_count=level,
           locked_until=state.window_started_at + timedelta(seconds=interval_seconds)
        return max(1, math.ceil((state.locked_until - database_now).total_seconds()))
```

Fill the two elided statements with the store's established guarded-insert/reset SQL (see `_record_bucket_attempt` L702 and `_reset_bucket` L722-723); the semantics above are the contract.

- [ ] **Step 4: Restructure the service and delete the pacer**

`exchange_grant` (device_tokens.py) — pace first, then exchange:

```python
        database_now = ...  # the clock read the method already performs
        retry_after_seconds = await self._exchange.pace_grant_poll(
            polling_credential_hash=polling_credential_hash_of(
                hmac_key=self._grant_hmac_key, polling_credential=polling_credential
            ),
            database_now=database_now,
        )
        if retry_after_seconds is not None:
            raise AuthenticationError(
                ErrorCode.DEVICE_AUTHORIZATION_SLOW_DOWN,
                safe_details={"retry_after_seconds": retry_after_seconds},
            )
```

Delete `GrantPollPacer`, `_MAXIMUM_POLL_INTERVAL_SECONDS`, `_POLL_PACE_WINDOW_MAXIMUM_GRANTS`, the `poll_pacer` constructor parameter, `_raise_pending_or_slow_down`, the `__all__` entry, and the pacer unit test. The `PENDING` outcome keeps its store-provided five-second hint untouched. Note in the `pace_grant_poll` docstring: a keyring rotation mid-grant re-keys the pacing digest once (new bucket) — replay correctness is Task 1's contract. Update the offline double + serve composition. Update the two §9 ride-along integration tests: the slow-down hint now reflects the durable window (no under-report after back-off), and `test_unknown_polling_credential_fails_closed` (L703) gains a companion asserting repeated unknown-credential polls receive `DEVICE_AUTHORIZATION_SLOW_DOWN` before `DEVICE_CREDENTIAL_INVALID`.

- [ ] **Step 5: Run gates**

```bash
uv run poe authentication-test
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan1-t3-* uv run pytest tests/integration/authentication -m local_stack -q
```
Expected: exit 0 both.

- [ ] **Step 6: Commit + retire both remaining rows of this plan's web-auth halves**

```bash
git add -A
git commit -m "feat: pace grant polls through the durable grant_poll bucket"
```

Then remove from `docs/handoff/BACKLOG.md` the 2026-08-16 web-auth §9 row and the §13 row in this same commit's follow-through (or amend before committing — one commit total):
lines: `| 2026-08-16 | web-auth | Poll replay digest single-key...` and `| 2026-08-16 | web-auth | Multi-worker poll pacing needs a poll bucket_kind...`.

---

### Task 4: Rewrite the two stale `web-security.spec.ts` journeys

**Files:**
- Modify: `tests/end_to_end/authentication/web-security.spec.ts` (journeys at L111 and L187; header comment L7-8)
- Test: the file itself, run through `pnpm run test:e2e:authentication`

**Interfaces:**
- Consumes: the file's existing MSW handler helpers (`enrollmentOffer()`, the verify/recovery-code handlers at L124-141) and its storage-emptiness assertion block (reused verbatim from the old journeys).

- [ ] **Step 1: Read the current security-page enrollment flow**

Run: `rg -ln "enroll" apps/web/src/features/ apps/web/src/app/` and read the security feature component + its vitest tests to get the current TOTP-enrollment control labels and routes. The journeys must drive THAT flow, not the removed first-login offer.

- [ ] **Step 2: Replace journey 1 (L111) — enrollment moves to the security page**

Keep the MSW handler stubs from the old body (L124-141) but trigger enrollment from the security page:

```ts
test("TOTP enrollment completes through the security page with a local QR and one-time codes", async ({ page }) => {
  // handlers: enrollmentOffer() on **/api/auth/totp/enrollments (POST start),
  // verify + recovery-code handlers verbatim from the old L124-141 block
  await performLogin(page);            // the login sequence of the L222 journey, copied verbatim
  await page.goto("/admin/security");
  await page.getByRole("button", { name: /enable two-factor/i }).click();  // adapt to the real label from Step 1
  await expect(page.getByText("E2EESUPERSECRET2345")).toBeVisible();
  await expect(page.locator("svg")).toBeVisible();
  await page.getByLabel("Verification code").fill("123456");
  await page.getByRole("button", { name: /activate/i }).click();
  await expect(page.getByText("AAAA-BBBB-CCCC")).toBeVisible();
  // storage-emptiness + cookie-scope assertion block copied verbatim from the old journey 1 tail
});
```

- [ ] **Step 3: Replace journey 2 (L187) — the interstitial is gone and must stay gone**

```ts
test("login lands on the devices page with no first-login TOTP interstitial", async ({ page }) => {
  const requested: string[] = [];
  // wrap the worker's request ledger the way the L307 root-redirect journey captures requests
  await performLogin(page, requested);
  await expect(page).toHaveURL(/\/admin\/devices/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  assert(!requested.some((url) => url.includes("/api/auth/totp/enrollments")),
    "the removed first-login offer must not reappear in the login path");
  // storage-emptiness assertion block copied verbatim
});
```

Update the header comment (L7-8) to describe the new pair.

- [ ] **Step 4: Run locally to green**

Run: `pnpm run test:e2e:authentication`
Expected: 5 passed (the whole `tests/end_to_end/authentication` directory). Before the rewrite, running it must show the two old journeys failing — record that RED output in the task report.

- [ ] **Step 5: Commit + retire the row**

Remove `| 2026-08-30 | web-auth acceptance | 2 web-security.spec.ts journeys stale...` from `docs/handoff/BACKLOG.md`, then:

```bash
git add tests/end_to_end/authentication/web-security.spec.ts docs/handoff/BACKLOG.md
git commit -m "test: pin the post-offer TOTP journeys against the security page"
```

---

### Task 5: Single envelope-helper source

**Files:**
- Modify: `apps/web/src/api/exclusion-policy-client.ts` (delete duplicate `REQUEST_UNAVAILABLE_ERROR` L94-99 and `unwrapEnvelope` L106-123; import both from `./authentication-client`)
- Modify: `apps/web/src/api/source-lifecycle-client.ts:9` (import from `./authentication-client` instead of `./exclusion-policy-client`)
- Test: existing vitest suites (no new tests — pure refactor)

**Interfaces:**
- Consumes: `authentication-client.ts` L80-102 exports (unchanged).
- Produces: exactly one definition of each symbol in the workspace.

- [ ] **Step 1: Enumerate every importer of the duplicates**

Run: `rg -n "REQUEST_UNAVAILABLE_ERROR|unwrapEnvelope" apps/web/src/`
Expected hits: authentication-client (definition), exclusion-policy-client (duplicate), source-lifecycle-client (re-import). Any OTHER importer of the exclusion-policy-client copies switches to `./authentication-client` too.

- [ ] **Step 2: Apply the edits**

`exclusion-policy-client.ts` — replace the duplicate block with:

```ts
import { REQUEST_UNAVAILABLE_ERROR, unwrapEnvelope } from "./authentication-client";
```

(keep any re-export ONLY if Step 1 found an external importer that cannot be switched — none expected). `source-lifecycle-client.ts:9` becomes:

```ts
import { REQUEST_UNAVAILABLE_ERROR, unwrapEnvelope } from "./authentication-client";
```

- [ ] **Step 3: Verify type-check + tests + no duplicates remain**

```bash
rg -n "export const REQUEST_UNAVAILABLE_ERROR|export function unwrapEnvelope" apps/web/src/   # exactly one hit each (authentication-client.ts)
pnpm --filter @workspace/web-runtime test
pnpm --filter @workspace/web-runtime exec tsc --noEmit
```
Expected: single-definition grep; tests and type-check exit 0.

- [ ] **Step 4: Commit + retire the row**

Remove `| 2026-08-30 | web admin api clients | Duplicate REQUEST_UNAVAILABLE_ERROR/unwrapEnvelope...` from BACKLOG, then:

```bash
git add apps/web/src/api/exclusion-policy-client.ts apps/web/src/api/source-lifecycle-client.ts docs/handoff/BACKLOG.md
git commit -m "refactor: single envelope helper source for the admin api clients"
```

---

### Task 6: Final verification

- [ ] **Step 1: Full offline gates from a clean tree**

```bash
uv run poe verify
uv run poe api-contract-check
uv run poe authentication-test
pnpm run test
pnpm run build
```
Expected: all exit 0; `packages/api-client` unchanged (no OpenAPI delta — wire behavior preserved).

- [ ] **Step 2: Live-stack migration + integration round**

```bash
CI=true bash .local/serve-live-ci.sh up knowledge-ci-plan1-final-*
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan1-final-* uv run poe authentication-test
bash .local/serve-live-ci.sh down
```
Expected: exit 0 (the 2026-08-30 documented `exclusion_policy_not_initialized` readiness caveat belongs to another plan's domain — if it appears, it is out of scope here; report it, do not chase it).

- [ ] **Step 3: BACKLOG check**

Run: `rg -n "2026-08-16 \| web-auth|2026-08-30 \| web-auth acceptance|2026-08-30 \| web admin api clients" docs/handoff/BACKLOG.md`
Expected: no hits — all four rows retired.

- [ ] **Step 4: Commit any residue**

```bash
git status --short   # only intended files
git diff --check
```

## Self-review notes

Spec coverage: C1→Task 1, C2+C3→Task 3 (unknown-credential pacing and honest hint are properties of the pre-exchange durable pace; pacer-scope accounting disappears with the pacer — the runbook line states the durable truth), schema/spec amendment→Task 2, C4→Task 4, C5→Task 5. Acceptance criteria 1–5 map to Tasks 1,2,3,4/6 and per-task row retirements. Type consistency: `pace_grant_poll` signature identical across store/double/port; `previous_polling_secret_hash` spelled identically in command, service and store.
