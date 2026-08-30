# Child Nine hygiene retirement — handoff cuối (Tasks 1–20 hoàn tất)

Branch `child-nine-hygiene-retirement` (from `master` @ `85fb784`). Design spec:
`docs/superpowers/specs/backlog/2026-08-30-child-nine-and-phase-two-closure-hygiene-retirement-design.md`;
implementation plan `docs/superpowers/plans/2026-08-30-child-nine-hygiene-retirement.md`.
Đủ 20/20 task hoàn thành; 20/20 per-task review clean, final whole-branch
review đã hoàn tất với 4 minor findings (0 merge blocker) được đóng trong đúng
một fix wave — commit mang bản cập nhật này. File này thay thế snapshot gián
đoạn trước đó (sau Task 12) làm handoff cuối duy nhất của wave.

## Trạng thái

- Checkpoint 1 (Tasks 1–13, "before Child 9 acceptance"): final SHA `fd934b2`
  (Task 13).
- Checkpoint 2 (Tasks 14–19, code): final SHA `1b13c8b` (Task 19). Task 20 —
  commit mang file này ("docs: retire the child-nine hygiene backlog rows") —
  chỉ thay đổi docs; toàn bộ verification block dưới đây chạy tại `1b13c8b`
  cùng các edits docs (không đổi behavior).
- Backlog retirement: đủ 22 row trong phạm vi design spec đã được remove. 7 row
  được task sở hữu remove in-commit theo quy tắc living-index của AGENTS (T9
  2026-08-16 §7; T12 §11; T13 §12; T15 2026-08-14 object-storage ruling-2; T18
  2026-08-14 §7; T19 2026-08-15 §4 + §14); commit này remove 15 row còn lại
  (2026-08-14 object-storage ×1 + source-publication ×2; 2026-08-15 canonical-core
  ×7; 2026-08-16 web-auth ×4 + acceptance-tests ×1) và index 5 row mới (xem
  [Deferred items](#deferred-items)). Các row out-of-scope (conditional/live/
  mobile/mutation-testing/CI-first-run) giữ nguyên.
- Evidence chi tiết theo từng task: 22 commit trên branch (mỗi task một commit
  kèm test của nó); per-task reports là artifact session (gitignored, đã dọn
  sau final review) — kết luận của mỗi report nằm trong các section dưới đây.

## Gate evidence (plan Step 1 — final verification)

Chạy tại code head `1b13c8b`. Kết quả verbatim:

- `uv run poe verify`: exit 0 (full gate: lint, type checks, Python unit/contract
  suites, package builds, web/plugin builds — final build phase ends "Done",
  `VERIFY_EXIT=0`).
- `uv run poe api-contract-check`: exit 0 (`api_contract_current` + generated
  client immutable check pass).
- `pnpm --filter @workspace/web-runtime test`: exit 0 — 21 test files, 163
  tests passed.
- `pnpm --dir apps/obsidian-plugin exec vitest run`: exit 0 — 56 test files,
  1245 tests passed.
- `pnpm --dir apps/obsidian-plugin run type-check`: exit 0 (`tsc --noEmit`).
- `pnpm --dir apps/obsidian-plugin run build`: exit 0 (`build-plugin.mjs`).
- `CI=true bash .local/serve-live-ci.sh up knowledge-ci-plan-final-20260830`:
  exit 1 tại API-readiness sub-gate với `exclusion_policy_not_initialized` —
  KỲ VỌNG TÀI LIỆU SẴN (pre-existing từ 2026-08-27, log
  `.local/runtime-logs/live-ci-api-restart.log`); containers vẫn lên healthy,
  hai suite dưới chạy và pass/xếp loại đúng chống chúng.
- `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan-final-20260830 uv run poe authentication-test`:
  exit 0 — 1773 passed, 2 skipped.
- `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan-final-20260830 uv run pytest tests/integration/source_publication tests/integration/canonical_core -m "local_stack and not r2_live" -q`:
  2 failed, 98 passed, 6 deselected — hai failure chính xác là hai test đã tài liệu
  hóa ở trên (source_publication một mình: 79 passed / 2 failed như kỳ vọng;
  canonical_core thêm 19 passed); không có regression nào khác.
- `bash .local/serve-live-ci.sh down`: stack_down_complete (project CI removed,
  `knowledge-local` giữ trạng thái down).
- `git diff --check && git status --short`: clean ngoài các file docs của task này.

Hai kỳ vọng đã tài liệu hóa (không phải defect mới của wave này):

1. `serve-live-ci.sh up` exit 1 tại sub-gate API-readiness
   (`exclusion_policy_not_initialized`) — pre-existing trên mọi fresh CI project
   từ 2026-08-27, không thuộc branch này.
2. Hai test đỏ đã tài liệu hóa trong source_publication integration, chủ sở hữu
   là domain small-file/migrations (BACKLOG rows mới số 4 và 5 dưới đây):
   `test_terminal_transition_clears_raw_locator_and_keeps_digest` và
   `test_gated_downgrade_drops_the_operation_table_and_reapplies_head`.

Per-task evidence còn lại (OpenAPI delta duy nhất của wave = một ErrorCode enum
member `canonical_recovery_admission_refused` tại T2; các suite domain xanh tại
mỗi commit) nằm trong SDD ledger và task reports.

## Quyết định diễn giải spec (kèm lý do)

### Các phán quyết plan-mandated (a)–(e)

- **(a) Alembic version-table location — documented, not switched (T4).**
  `env.py` không set `version_table_schema`, nên version table thật sự nằm ở
  `public` ngay cả khi schema cấu hình là schema knowledge. Đổi vị trí là một
  migration vận hành (rủi ro lớn, ngoài hygiene scope). Constant được đổi tên
  `_ALEMBIC_VERSION_TABLE_REFERENCE` kèm provenance comment, pin bằng test.
- **(b) Whole-batch drain — pinned, not changed (T1).** Hành vi "shutdown đợi
  cả batch in-flight trong shutdown bound" đã tồn tại sẵn khi trace lại code;
  task chỉ thêm test pin hành vi đó thay vì sửa code.
- **(c) Policy-worker runtimes chia sẻ dispose/close gap của dispatcher —
  observed, out of scope, KHÔNG re-index (T1/T15).** Hai policy worker dùng
  pattern engine/client lifecycle tương tự dispatcher. Đây là cùng lớp vấn đề
  với các row 2026-08-24 policy-workers đã index (staleness verdict, live
  smoke) — việc index thêm row trùng domain sẽ tạo deferred trùng lặp, trái
  quy tắc một-dòng-một-item của BACKLOG.
- **(d) Third-digest-type condition fired → dedupe intra-module + từ chối
  extraction cross-domain (T18).** Row điều kiện "khi có digest value object
  thứ ba" đã kích hoạt: ngoài `RequestFingerprint`/`SafeDiffHash` còn có
  `ContentDigest` (object_storage). Ruling triển khai: chỉ gộp parser hex64
  trong `sources/fingerprint.py` (`_parse_hex64`); từ chối gộp với
  `object_storage.ContentDigest` theo tiền lệ row-51 (repetition over
  cross-domain abstraction khi các domain giữ closed vocabulary; tránh mở import
  boundary cross-domain cho một predicate 5 dòng). Predicate hex64 giống hệt tồn
  tại ở bảy module (thêm `exclusion_policy/publication.py`,
  `exclusion_policy/contracts.py`/enforcement, `source_lifecycle/fingerprint.py`,
  `object_storage/keys.py`, `recovery/manifest.py`, `recovery/bundle.py`) — tất cả
  cross-domain, tất cả bị từ chối bởi cùng ruling.
- **(e) Code-stands rulings.**
  - T6: compose-time lazy-engine no-dispose (engine lazy, không mở connection ở
    compose time — dispose là no-op) và Poe task `canonical-core-test` đứng độc
    lập ngoài `verify` (compose vào verify sẽ làm chậm mọi gate run) — ghi thành
    documented ruling tại chỗ (composition comment; Poe task comment), không đổi
    code.
  - T5: failed-restore metrics giữ quy ước 0/0 closed-sink (đã có từ trước;
    giờ được tài liệu hóa trong metrics docstring + runbook) và per-object
    buffered-copy bound giữ 100 MiB (không stream-to-file được vì source reader
    là async port; fallback buffer verify từng chunk rồi một lần write off-loop).

### Các phán quyết tích lũy từ ledger theo task

- **T2:** `RecoveryError.allowed_codes` phải nhận member mới (bắt buộc cấu trúc
  — `ApplicationError` từ chối code chưa đăng ký); service tests đặt ở
  `test_service_restore.py` (không có `test_service.py`); runbook exit table
  tách 3 dòng thay vì 2 — giữ lại dòng config-refusal residual (bảo toàn thông
  tin thay vì gộp mất).
- **T3:** RED dự đoán của brief cho deterministic empty-dir/extra-object không
  chính xác — các probe hiện có đã từ chối các shape đó từ trước; RED thật là
  probe-to-publish race, move-rollback, và fake-store media-conflict; kết quả
  vẫn pin đủ outcome yêu cầu.
- **T5 (fix round 1):** đóng thêm một dir-create thread race MỚI phát hiện khi
  làm việc (`_create_directories_private` chấp nhận `FileExistsError` cho dir
  do mình sở hữu, non-symlink) — sửa trong cùng commit (amend 872168c).
- **T7:** credential transaction port nằm ở `sessions.py`, không phải `ports.py`
  (đó là dependency thật của `LoginService`); cả ba implementer được mở rộng.
- **T8:** `status` không bao giờ fail cho username tồn tại (đặc tính cố ý —
  không join workspaces), nên pin archived-workspace dời sang path enroll/reset
  (nơi exit 78 tồn tại); reset-before-enrollment pin truthful closed refusal.
  Code-stands: EOF tại interactive credential password prompt map sang
  `internal_error:eoferror`/70 — pre-existing, đã surface closed class token;
  confirmation-prompt scope được pin bằng message token (ruling: code stands).
- **T9:** `.rowcount == 0` không dùng được trên stack này (SQLAlchemy 2.0.51 +
  psycopg 3.3.4 trả -1 cho guarded insert) — tín hiệu win/loss là sự hiện diện
  `.returning()` (server-guaranteed); fix dedupe thành shared
  `apply_throttle_bucket_failure`; năm TOTP outcome dataclass thêm `limited_at`
  (cần bởi `_rate_limited_json`).
- **T10:** chấp nhận đổi ordering locked+invalid — source locked với request
  INVALID giờ nhận validation rejection thay vì 429 (lock chạy cùng transaction
  insert; request invalid không bao giờ tạo grant; request hợp lệ vẫn 429).
- **T11:** dòng clear `setChallengePassword("")` trong `closeAsTerminal` là
  React-unobservable (subtree unmount trong cùng commit) — thay bằng end-to-end
  MSW replacement test.
- **T12:** focus trap cố ý tối thiểu (Tab wrap + opener restore, không `inert` —
  không thêm dependency); Retry control gate vào đúng path rate-limited qua
  `isLookupRetryable` (retry code expired/denied là dead end).
- **T13:** (1) rate-limited creation close là state `not_connected` + closed
  code + retry-seconds detail (state set đóng của spec-19 không có state
  rate-limit; giữ `canLogin` để Login button tự là retry affordance); (2) catch
  trong `reconcileCrashWindow` route qua `#recordStartupChainFailure` (buffered
  `startup_failure` trail, flush khi trail load) — `#journalFailureReporter`
  chỉ tồn tại sau khi trail load; stage token `other` là catch-all đã khai báo
  của vocabulary (không mint token mới); (3) login-refusal tái dùng
  `device_credential_invalid` + `isLocal: true` theo tiền lệ `#rotateOnce`, kèm
  emit `refresh_required` trên state seam.
- **T14:** pin `reauthenticate-rejected` (401) KHÔNG set cookie — probe
  TestClient với composition thật cho thấy path `_error_json` của
  `session_routes.py` không set cookie; brief nói "journey observes both" là
  không chính xác, test pin sự thật được quan sát.
- **T17:** `expected_row_deltas(**nonzero_deltas)` — helper chia sẻ tại conftest
  trả `{table: 0 cho cả 39-table registry} | nonzero_deltas`: expectation luôn
  exhaustive, không stale khi migration thêm table, và key override gõ sai sẽ
  fail lớn. Zero-mutation pin áp cho CẢ HAI exact update-replay tests (no-change
  và changed) — "the update-replay test" số ít của brief mơ hồ, cả hai đều là
  exact replay, coverage tăng, không suy yếu assertion.
- **T18 (fixture provenance):** entry `create_v2` trong `fingerprint_golden.json`
  KHÔNG spec-hash-pinned — "regenerate from spec" cho entry đó nghĩa là recompute
  từ specced v2 envelope + pinned inputs (locator spec 2026-08-20 yêu cầu edit
  mà không pin hash).
- **Retirement reconciliation:** 22 row của spec → 7 removed in-branch + 15
  removed bởi commit này; đối chiếu theo NỘI DUNG từng row với section rows
  của design spec (không đếm suông); các row out-of-scope giữ nguyên.

## Deferred items

Sáu row mới index tại `docs/handoff/BACKLOG.md` (2026-08-30), trỏ về file này.
Năm row đầu được index khi đóng wave; row thứ sáu (6) được thêm khi đóng final
whole-branch review — finding out-of-scope của Task 12 được triage tại đó
(brief của Task 12 chỉ named hai file, nên bản sao còn lại thuộc domain
web admin api clients, ngoài scope của mọi task trong wave). Ngoài row đó,
không có item nào khác mới được defer trong wave này.

1. **web-auth acceptance** — 2 journey `web-security.spec.ts` stale so với
   first-login TOTP offer đã bị remove (commit 99fe1c3 ngày 2026-08-22, là
   ancestor của merge-base 85fb784 — pre-branch). Auth Playwright journeys
   không được wire vào bất kỳ CI gate nào (`authentication-acceptance.yml` chạy
   pytest-only `poe authentication-test`; Poe task `authentication-e2e` mồ côi)
   — đó là lý do rot không bị phát hiện. *Implement by: Before Child 9
   operations acceptance.*
2. **exclusion-policy acceptance** — `policy-publication.spec.ts`: trang
   `/admin/policy` 500, root cause chưa chẩn đoán (cần fresh-build
   reproduction; không có green CI baseline từ ≥2026-08-24 vì
   `database_schema_contract_invalid` fail sớm hơn). *Implement by: Before
   Phase 2 closure (after Child 9).*
3. **object-storage** — `spool.py:148-149` `_run_shielded_cleanup` vẫn giữ
   pattern cleanup-raises-masks-cancellation (T15 chỉ fix adapter
   `_run_shielded`; cùng invariants, chủ sở hữu domain object-storage). Ruling:
   deferred, không code-stands — cùng lớp defect đã được sửa ở adapter, phần
   spool còn lại thuộc scope domain. *Implement by: Before production
   activation.*
4. **small-file** — raw locator KHÔNG BAO GIỜ được clear khi terminal transition
   (`small_file_sync_operations.py` thứ tự clear-statement; raw note paths còn
   sống; docstring claimed clearing; defect từ 6b9fab7 ngày 2026-08-21; test
   `test_terminal_transition_clears_raw_locator_and_keeps_digest` đỏ). Privacy
   invariant — không code-stands. *Implement by: Before production activation.*
5. **migrations/small-file** — in-process downgrade partial-commit gap:
   `transaction_per_migration` commit column drops của `20260820_01` TRƯỚC khi
   `20260818_01` từ chối row-gate, để lại schema nửa đường; test
   `test_gated_downgrade_drops_the_operation_table_and_reapplies_head` đỏ.
   *Implement by: Before production activation.*
6. **web admin api clients** — bản sao `REQUEST_UNAVAILABLE_ERROR`/
   `unwrapEnvelope` trùng lặp tại `apps/web/src/api/exclusion-policy-client.ts`
   (và `source-lifecycle-client.ts` import lại từ nó) thay vì dùng shared
   exports của `authentication-client.ts`; Task-12 out-of-scope finding
   (brief chỉ named hai file) được triage thành defer tại final whole-branch
   review — nội dung thuộc domain web admin api clients, không thuộc scope của
   task nào trong wave. *Implement by: Before Phase 2 closure (after Child 9).*

## Next actions

1. SDD final whole-branch review trên `85fb784..HEAD` — đã hoàn tất: 4 minor
   findings (0 merge blocker) được đóng trong đúng một fix wave và một scoped
   re-review sạch; xem diff của fix wave tại commit "fix: close the
   final-review findings on tokens, invariants, and docs" (`4bf0cb5`).
2. `superpowers:finishing-a-development-branch` — quyết định merge. Living
   docs (runbooks, metrics, operations) đã cập nhật tại chỗ trong các task;
   tra hiện trạng qua `docs/operations/`, không qua file này.
