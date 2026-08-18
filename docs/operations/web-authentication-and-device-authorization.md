# Web Authentication and Device Authorization Operations Guide

Operator contract for the web credential, session, TOTP, device-authorization
and device-token surfaces (`src/personal_os/authentication`, the adapters in
`packages/postgresql-source-store`, the routes in `apps/api/src/api_runtime`,
the Web Admin in `apps/web` and the Obsidian plugin in
`apps/obsidian-plugin`). Design:
`docs/superpowers/specs/2026-08-16-web-auth-and-device-authorization-design.md`.
The shared HTTP boundary contract (envelopes, liveness, readiness, OpenAPI
snapshot) lives in `docs/operations/api-runtime-contract.md`.

There is no public signup: the only way a first credential exists is the
protected CLI enrollment below, run on a trusted host with the secret root
mounted. PostgreSQL holds every canonical authentication row; the Obsidian
plugin holds only its opaque device credentials inside Obsidian SecretStorage,
never in settings data or Vault files.

## Credential lifecycle commands

```bash
# Enroll the first password of one canonical username (idempotent-refusing).
# The password is read from the exact secret file, or prompted when omitted;
# it never travels in argv.
uv run --package api-runtime personal-api enroll-web-credential \
  --username <canonical-username> [--password-file-name <file-under-secret-root>]

# Report whether one username is enrolled and its current credential
# revision. Unknown usernames fail closed with authentication_failed.
uv run --package api-runtime personal-api web-credential-status --username <canonical-username>

# Emergency reset: replaces the password (file or prompt), revokes every web
# session, device token family and still-active token, disables TOTP and its
# unused recovery codes, denies every pending grant deployment-wide plus this
# user's approved-but-unexchanged grants, and appends one reset audit row.
# The typed confirmation must equal the canonical username exactly.
uv run --package api-runtime personal-api reset-web-authentication \
  --username <canonical-username> [--password-file-name <file-under-secret-root>]
```

Exit codes follow the shell contract: `0` success, `2` CLI syntax error,
`70` unexpected internal error, `78` configuration or secret error. An
emergency reset never deletes audit rows, device rows, Vault data or sync
queue data; terminal rows stay for evidence during Phase 2.

After an emergency reset the operator must: re-enroll nothing (the reset
already installed the new password), sign in with the new password, decide
whether TOTP is re-enabled through the Security page, and tell the device
owner to re-run plugin Login — old device credentials answer with the
terminal `device_revoked` rejection and the plugin tombstones them while
preserving all Vault data.

## Authentication keyring

`personal-api serve` loads a bounded keyring of versioned 32-byte master keys
from exact files under the bounded secret root. Non-secret configuration
names them; secret bytes never appear in environment values.

| Environment | Meaning |
| --- | --- |
| `KNOWLEDGE_AUTH_ALLOWED_ORIGIN` | The one exact browser origin the session routes accept (HTTPS in staging/production). |
| `KNOWLEDGE_AUTH_TRUSTED_PROXY_CIDRS` | Comma-separated CIDRs whose forwarded-address header is honored; empty (default) is fail-closed: the socket peer always wins. |
| `KNOWLEDGE_AUTH_CURRENT_KEY_ID` / `KNOWLEDGE_AUTH_CURRENT_KEY_FILE` | The key ID and exact file name of the current master key. |
| `KNOWLEDGE_AUTH_PREVIOUS_KEYS` | Bounded `key-id=file-name` pairs (max four keys total) still referenced by stored state. |
| `KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION` / `KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION` | The approved plugin version window of the device-grant endpoint. |

Key creation and permissions:

```bash
# One 32-byte key per key ID, created with a real random source and never
# committed to the repository. The keyring loader accepts hex-encoded key
# material only: 32 random bytes rendered as one 64-character hex line.
head -c 32 /dev/urandom | xxd -p -c 64 > "$KNOWLEDGE_SECRET_ROOT/auth-key-2026-08.key"
chmod 600 "$KNOWLEDGE_SECRET_ROOT/auth-key-2026-08.key"
```

Short, duplicate, missing or path-escaping key files fail `serve` during
startup with `configuration_secret_invalid` — before the listening socket is
exposed.

Rotation procedure:

1. Create the new key file under the secret root.
2. Set `KNOWLEDGE_AUTH_CURRENT_KEY_ID`/`_FILE` to the new key and move the
   old key into `KNOWLEDGE_AUTH_PREVIOUS_KEYS` (`old-id=old-file`).
3. Restart `serve`. Startup reads every key ID still referenced by
   PostgreSQL — active/pending TOTP ciphertext, active unexpired refresh
   tokens, and unexpired grants carrying replay state — and refuses with
   `configuration_secret_invalid` and the fixed reason
   `keyring_missing_referenced_key` when the keyring omits one. Previous-key
   TOTP secrets are re-encrypted under the current key inside the next
   verification transaction that opens them; device replay resolves through
   whichever key anchored the stored derivation.
4. Removal check: a key file may only leave the keyring after
   `required_key_ids` no longer returns its ID — practically, after every
   TOTP secret referencing it was re-encrypted and every grant/token family
   anchored to it expired (refresh families live up to 90 days absolute).
   Removing it earlier is refused by the same startup coverage check.

## Reverse proxy requirements

The reverse proxy owns TLS termination and HSTS. It must:

- Forward `Origin` unchanged: session and grant routes compare the exact
  configured origin, and an absent or mismatched origin closes with
  `csrf_validation_failed`.
- Set `X-Forwarded-For` only as the socket peer address chain; the API
  honors the forwarded value solely from CIDRs configured in
  `KNOWLEDGE_AUTH_TRUSTED_PROXY_CIDRS`.
- Never cache any `/api/auth/...` or `/api/admin/...` response: the
  application already sends `Cache-Control: no-store` on every
  authentication response and adds `Pragma: no-cache` on the provisioning
  and recovery surfaces (grant creation, poll exchange, refresh, TOTP
  enrollment start/verify, recovery entry, recovery-code regeneration). The
  proxy must not strip these headers or add its own auth-response caching.
- Log request lines with method, path, status and duration only. Never log
  request or response bodies, `Cookie`, `Authorization` or `X-CSRF-Token`
  values: those carry session secrets, polling credentials and device
  tokens. The API's own structured events carry closed safe fields only
  (`http_method`, `route`, `status_code`, `duration_ms`), and audit rows
  never carry credential material.
- Web responses are served with a per-response nonce CSP
  (`default-src 'self'; script-src 'self' 'nonce-...'; object-src 'none';
  frame-ancestors 'none'`), `Referrer-Policy: no-referrer` and
  `X-Content-Type-Options: nosniff`; the proxy must not loosen them.

Single-process note: the device poll pacer is deliberately in-memory (spec
11.4 mandates no durable pacing state). `serve` runs one process; a
multi-worker deployment needs a shared pacing store or a poll bucket kind
added to the closed schema set before it may front more than one worker.

## Device revoke and recovery

- Admin revoke: Web Admin → Devices → Revoke. The dialog demands the exact
  device name (server-verified; mismatch answers
  `device_revocation_confirmation_invalid`) and a password re-authentication
  within the last five minutes. The revocation denies the device's claimed
  pending and approved grants, revokes the token family and every usable
  token, and the next device request answers terminal `device_revoked`.
- Plugin self-revoke: Obsidian settings → device account → revoke. The
  plugin presents its current refresh credential to
  `POST /api/auth/device-tokens/revoke-current`, tombstones its local
  credential and keeps all Vault data.
- Lost device / recovery: revoke the device row from Web Admin, then re-run
  plugin Login on the device. The plugin's crash-safe record resumes a
  pending grant or replays an exchanged one exactly; a lost response never
  creates a second device or family (exact-replay contract, spec 12.2/13.4).
- Emergency reset (above) is the nuclear path: every device dies with the
  sessions and grants.

## Argon2id benchmark evidence

Parameters are pinned by the spec and the code: `memory_cost_kib=65536`,
`time_cost=3`, `parallelism=1`, `salt_length_bytes=16`,
`hash_length_bytes=32` (`Argon2PasswordHasher`, argon2-cffi 25.1.0). The
reviewed p95 band for 20 sequential hash runs on the smallest deployment
host is 150–750 ms; changing parameters or band requires a spec and runbook
update.

Evidence recorded 2026-08-16 on the development host
(Windows 11 10.0.26200, AMD64, CPython 3.14.6, argon2-cffi 25.1.0), 20
sequential `hash` runs with the pinned parameters:

```text
min 113.3 ms   median 117.7 ms   mean 119.4 ms   p95 127.1 ms   max 134.9 ms
```

Deviation note: this host sits below the reviewed band's floor — it is
faster than the slowest host the band was reviewed against. The band rule
still governs deployments: benchmark the smallest deployment host at
install time with the same 20-run method and confirm its p95 lies within
150–750 ms; a host below 150 ms means stronger-than-reviewed hardware (not a
policy violation), a host above 750 ms must not serve logins until
parameters are re-reviewed.

Reproduce with:

```bash
uv run python - <<'PY'
import statistics, time, argon2
hasher = argon2.PasswordHasher(
    type=argon2.Type.ID, memory_cost=65536, time_cost=3,
    parallelism=1, salt_len=16, hash_len=32,
)
runs = []
for index in range(20):
    started = time.perf_counter()
    hasher.hash(f"benchmark-password-{index:02d}")
    runs.append((time.perf_counter() - started) * 1000)
ordered = sorted(runs)
print("median", round(statistics.median(runs), 1),
      "p95", round(ordered[18] + 0.05 * (ordered[19] - ordered[18]), 1))
PY
```

## Database backup and restore implications

Authentication state is canonical PostgreSQL data and is covered by the
existing canonical backup/restore procedure (see
`docs/operations/canonical-core-recovery.md`). Operator notes:

- A restore replays credential, session, TOTP, grant, token, throttle and
  audit rows together; point-in-time consistency matters because a family's
  refresh lineage is a single logical unit — restoring mid-rotation is
  safe only if the whole lineage (predecessor and successor) comes from the
  same recovery point, which any consistent snapshot or PITR target
  provides.
- The authentication master keys are NOT in PostgreSQL. A database restore
  without the matching key files leaves TOTP ciphertext and device replay
  state undecryptable, and `serve` refuses startup with
  `keyring_missing_referenced_key` until the referenced key files return.
  Key files must therefore be backed up under the same (or stricter)
  retention as the database, and a key retired from the keyring must remain
  restorable until every row referencing it has expired out of every
  retained backup that can still be restored.
- Restoring to a point before an emergency reset resurrects the revoked
  surfaces; re-run the reset CLI after such a restore if the compromise
  window that motivated the reset includes the recovery point.
- Throttle buckets restore too: a restored lockout stays locked until its
  window passes, and login throttling keeps failing closed.

## Safe metrics and observability

- Structured request events carry only `http_method`, `route`, `status_code`
  and `duration_ms`; identity, address, device and credential values never
  become labels or fields (spec 21, 20.4; no telemetry of any kind).
- The append-only `audit_events` table records the closed action set:
  `authentication.login_succeeded` / `login_rejected`,
  `authentication.web_credential_enrolled`, `authentication.password_changed`,
  `authentication.web_authentication_reset`,
  `authentication.totp_enrollment_started` / `totp_activated` /
  `totp_recovery_code_used` / `totp_disabled`,
  `authentication.device_authorization_approved` / `device_authorization_denied`,
  `authentication.device_registered`,
  `authentication.device_token_family_created`,
  `authentication.device_token_reuse_detected`, and the device/family revoke
  rows. Audit rows carry workspace/user ids and result codes only — no
  credential, code, token, cookie or address material.
- Normal access-token checks and successful refresh rotations write no
  audit row; look at request events plus the token tables for volume.
- Unknown-account login attempts create no audit row (no trusted workspace);
  they surface only through bounded diagnostics and throttle state.

## Incident runbook: confirmed token reuse

1. Detect: the API revokes automatically on confirmed reuse — a second,
   distinct rotation identity presented on an already-rotated refresh
   predecessor. The plugin receives terminal `device_token_reuse_detected`
   and tombstones its local credential; the family, its tokens and the
   device row are revoked before the rejection surfaces, and one
   `authentication.device_token_reuse_detected` audit row names the family.
2. Scope: query the family's device row and audit history
   (`authentication.device_registered`,
   `authentication.device_token_family_created`, revoke rows) to see when
   the device registered, last rotated and which grants claimed it. Confirm
   whether other families of the same workspace show reuse.
3. Contain: if the operator suspects the password too, change it from the
   Security page (revokes other web sessions, keeps devices) or run the
   emergency reset CLI (kills sessions, devices and pending grants).
   Re-enroll TOTP if the reset path was used.
4. Recover: the device owner re-runs plugin Login; approval happens in the
   browser as usual. Because the old family is revoked, no stolen old
   credential can replay into the new one.
5. Evidence: keep the audit rows and structured events; do not copy token
   or cookie values into tickets — request ids and family ids are the safe
   correlation keys.
