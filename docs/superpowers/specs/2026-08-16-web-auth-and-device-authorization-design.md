# Web Authentication and Device Authorization Design

**Status:** Approved in brainstorming on 2026-08-16

**Phase:** Phase 2 — Obsidian Sync, child 2 of 9

**Depends on:** `2026-08-15-api-runtime-and-contract-foundation-design.md`
**Next child:** `exclusion-policy-publication-design.md`

## 1. Purpose

Phase 1 created one canonical user, workspace and bootstrap device but no
password, Web session, public authentication API or plugin credential. Phase 2
child 1 then created the runnable FastAPI contract spine, canonical response
envelopes, deterministic OpenAPI snapshot and shared generated TypeScript
client.

This child makes that boundary usable by a real person and an Obsidian plugin.
It provides:

- protected enrollment of the first Web password;
- password login, optional TOTP and bounded recovery;
- opaque server-side Web sessions with CSRF protection;
- browser-mediated Obsidian device authorization;
- short-lived access tokens and retry-safe rotating refresh credentials;
- immediate device and token-family revocation;
- a minimal Web Admin for security and registered devices; and
- the plugin Login/onboarding and SecretStorage boundary.

It does not sync a Vault or source. Later children consume the authenticated
device context established here.

## 2. Canonical context

The design inherits these non-negotiable rules:

- the initial product serves one user and one logical workspace;
- PostgreSQL is authoritative for identity, authorization, session, token and
  audit state;
- Web and Obsidian are clients, not backend authorities;
- every request derives user, workspace, device and scope from its credential;
- request bodies never select an arbitrary workspace;
- Web uses a secure HTTP-only session and Obsidian uses a per-device rotating
  credential;
- domain modules do not import FastAPI, SQLAlchemy, database drivers or
  provider SDKs;
- public API responses use the child 1 envelope and closed error vocabulary;
- OpenAPI is deterministic and the generated client is the only shared
  Web/plugin workspace dependency;
- raw content, paths, credentials and attacker-provided values never enter
  telemetry; and
- no AI write, source publication, R2, Temporal or projection behavior belongs
  in an authentication transaction.

Existing canonical tables reused by this child are `users`, `workspaces`,
`devices` and `audit_events`. The Phase 1 bootstrap device remains an internal
system record; it is not an Obsidian credential and is not revocable from the
Web device list.

## 3. Scope

### 3.1 Included

- A protected internal CLI for initial password enrollment and emergency reset.
- Argon2id password hashing and offline password blocklist validation.
- Password login with persisted throttling and generic failure behavior.
- Optional RFC 6238 TOTP, one-use recovery codes and replay protection.
- Opaque PostgreSQL-backed Web sessions.
- Exact-origin and per-session CSRF enforcement.
- Five-minute recent re-authentication for sensitive operations.
- RFC 8628-inspired browser device authorization with plugin polling.
- SecretStorage-backed pending grant and refresh state.
- Fifteen-minute opaque access credentials.
- Refresh rotation with 30-day inactivity and 90-day absolute expiry.
- Exact replay after lost grant-exchange or refresh responses.
- Confirmed refresh reuse detection and whole-family revocation.
- Device list, device revoke, password and TOTP Web Admin pages.
- Plugin Login, resume, offline, revoked and disconnect states.
- Alembic, OpenAPI, generated-client, Web, plugin and security tests.
- An authentication operations runbook.

### 3.2 Excluded

- Public signup, email recovery, invitations and multi-user roles.
- Passkeys/WebAuthn and external OIDC.
- Personal access tokens and MCP credentials.
- Web session-list or per-browser-session administration.
- Exclusion policy publication, source APIs or sync routes.
- Vault access, SQLite journal, watcher, queue or file synchronization.
- Locator/tombstone lifecycle, cursor/reconciliation, multipart and conflicts.
- Cloudflare Worker or hybrid-edge routing.
- Physical pruning of terminal auth rows; child 9 owns operations retention.
- Mutation testing.
- Final real Obsidian Desktop/Mobile Phase 2 acceptance; child 9 owns that gate.

## 4. Approved decisions

1. Use opaque stateful credentials; do not use JWT.
2. Keep PostgreSQL on every authorization correctness path so revoke is
   effective on the next request.
3. Serve Web and `/api/*` from one HTTPS origin behind one reverse proxy.
4. Keep Next.js as an API client; it does not become an auth authority or BFF.
5. Do not expose public signup. The existing canonical username identifies the
   one account.
6. Require recent password re-authentication for device approval; require TOTP
   too when TOTP is active.
7. Make TOTP optional and opt-in from Security; password-only login proceeds
   without an enrollment prompt.
8. Support both one-use recovery codes and an emergency internal CLI.
9. Let a normal Web password change revoke other Web sessions while keeping
   approved plugin devices.
10. Let an emergency CLI reset revoke every Web session, Obsidian device token
    family and pending grant.
11. Open the exact browser approval page from the plugin. No access or refresh
    credential travels in a browser URL or callback.
12. Store pending polling and refresh credentials only through Obsidian
    SecretStorage; access credentials remain in memory.
13. Make grant exchange and refresh response loss exactly replayable without
    persisting plaintext token material.
14. Treat a different refresh rotation identity or use after a successor has
    rotated as confirmed reuse and revoke the family.
15. Keep Redis off the auth correctness path. Persist throttling in PostgreSQL.

## 5. System architecture and boundaries

```text
Browser / Next.js                         Obsidian plugin
opaque session cookie                     requestUrl transport
session-bound CSRF token                  SecretStorage refresh record
           \                                  /
            same-origin HTTPS /api/* adapters
                           |
       framework-neutral authentication services
          password / TOTP / session / grant / token
                           |
      repository + password + crypto + clock ports
                           |
         PostgreSQL adapters and reviewed crypto adapters
                           |
    users / workspaces / devices / auth tables / audit
```

The reverse proxy serves Next.js under `/` and FastAPI under `/api/*`. CORS is
absent. Production and staging accept browser credentials only over HTTPS.

FastAPI adapters own:

- cookie and dedicated Bearer extraction;
- exact-origin and CSRF request checks;
- request/response Pydantic models;
- canonical envelopes and HTTP mappings;
- OpenAPI security schemes; and
- binding the authenticated context to a request.

Framework-neutral services own:

- password, session, TOTP and recovery transitions;
- device grant approval and exchange;
- token rotation, exact replay, reuse detection and revoke;
- domain authorization scopes; and
- append-only audit commands.

Infrastructure adapters own PostgreSQL statements, Argon2id, authenticated
encryption, HKDF/HMAC, secure randomness and exact secret-file reads.

## 6. Identity and authorization scopes

### 6.1 Web identity

A successful Web credential resolves one active `user_id` and its one active
`workspace_id`. A Web session grants the fixed Phase 2 administration surface:

```text
web_security_manage
device_authorization_approve
device_administration_manage
```

The client never chooses these values. Disabled users or archived workspaces
fail closed.

### 6.2 Obsidian identity

An approved device token resolves:

```text
user_id
workspace_id
device_id
scope = obsidian_sync
```

`obsidian_sync` is a closed fixed scope in Phase 2. The approval page displays
it but cannot widen or customize it. Later sync children authorize their
routes against this scope.

Device credentials cannot access Web security or Admin routes.

## 7. Initial credential enrollment and emergency reset

### 7.1 Enrollment CLI

The API process gains a protected internal command for initial Web credential
enrollment. It accepts the canonical username and reads the password either:

- interactively through a non-echoing prompt; or
- from one exact file name beneath the configured secret root.

It never accepts the password as an argument, environment value, URL or
committed configuration field. Help/version/invalid-syntax paths do not read
configuration, a secret file or PostgreSQL.

The command:

1. resolves exactly one active canonical user/workspace;
2. refuses if a Web credential already exists;
3. validates the new password;
4. computes Argon2id outside the database transaction;
5. locks the user credential identity;
6. inserts `user_credentials` revision 1;
7. writes `authentication.web_credential_enrolled`; and
8. commits once.

The command is intentionally not a create-or-return operation: once a
credential exists, every later enrollment attempt refuses without changing it.
An operator who loses the CLI acknowledgement checks the non-secret credential
revision through a separate status command before deciding whether an emergency
reset is required.

### 7.2 Emergency reset CLI

Emergency reset requires an explicit typed confirmation. It reads a new
password through the same protected input boundary and atomically:

- replaces the password hash and increments credential revision;
- replaces/disables TOTP and recovery credentials;
- revokes every Web session;
- revokes every Obsidian device and token family;
- denies every pending or approved-but-unexchanged grant; and
- writes one append-only reset audit event with closed counts.

It never deletes Vault data, a device audit row or future sync queue data.

## 8. Password login and throttling

### 8.1 Password policy

Passwords are 15–128 Unicode characters. Spaces are allowed. There is no
uppercase/lowercase/number/symbol composition rule and no periodic reset.

The service rejects locally known common or compromised values through a
versioned offline blocklist. Passwords are never sent to a remote reputation
service.

Argon2id uses a pinned, reviewed implementation. The initial parameters may not
be weaker than OWASP's documented 19 MiB, two iterations and one lane baseline.
The implementation plan must benchmark the chosen parameters on the smallest
deployment host and pin both parameters and expected latency bounds.

The encoded PHC string carries its algorithm and work parameters. A successful
login upgrades an obsolete hash after verifying the submitted password.

### 8.2 Login sequence

`POST /api/auth/login` accepts a strict username/password body over the same
origin.

1. Normalize and validate the canonical username grammar.
2. Resolve the username and source throttle buckets.
3. Reject a locked bucket with the generic rate-limit response.
4. Load the credential or select a fixed dummy Argon2id hash.
5. Verify the supplied password outside database locks.
6. On failure, transactionally update the applicable throttle buckets and
   audit a rejected attempt only when a trusted user/workspace exists.
7. On success, lock and recheck user, workspace and credential revision.
8. Reset the credential failure streak.
9. Create an active or `pending_totp` Web session.

Unknown user and wrong password use the same public status, error, message and
safe details. Tests prove both invoke the same hasher contract; they do not use
flaky wall-clock equality assertions.

### 8.3 Throttle contract

- Five failures are allowed per username/source bucket in 15 minutes.
- Reaching the bound locks the affected bucket for 15 minutes.
- Username and resolved source material are HMACed before persistence.
- Raw username and source address are absent from throttle rows and telemetry.
- Successful authentication resets the credential failure streak.
- Grant creation, user-code lookup, TOTP and recovery verification have their
  own closed throttle kinds and bounded attempts.

PostgreSQL is authoritative for these transitions. Redis may not be required
to make a brute-force decision.

## 9. Web session and CSRF contract

### 9.1 Session cookie

Production uses:

```text
Name       __Host-admin_session
Secure     true
HttpOnly   true
SameSite   Lax
Path       /
Domain     absent
Lifetime   browser session cookie
```

The cookie contains an opaque random secret with at least 256 bits of entropy.
PostgreSQL stores only its hash. Closing the browser is expected to require a
new login; the database expiry remains authoritative even if a browser restores
a session cookie.

An explicit loopback local-development mode may use a different non-production
cookie name without `Secure`. Tests prove that mode cannot activate in staging
or production.

### 9.2 Session states and expiry

Closed states are:

```text
pending_totp
active
recovery_limited
revoked
```

- `pending_totp` expires after five minutes and can call only TOTP/recovery
  verification and logout.
- `active` has 12-hour idle and seven-day absolute expiry.
- `recovery_limited` can replace TOTP or logout only.
- `revoked` authenticates no route.

Activation, recent re-authentication, password change and completed recovery
rotate the session secret and CSRF binding. Logout revokes the PostgreSQL row
before clearing browser cookies.

### 9.3 CSRF

After the password step, the server sets a second Secure, SameSite=Lax,
non-HttpOnly CSRF cookie and returns no token in a URL. PostgreSQL stores only
the token hash bound to the session revision.

Every state-changing session request requires:

- an exact allowed `Origin`;
- the session cookie;
- the CSRF cookie;
- an equal `X-CSRF-Token` header; and
- a hash match against the current session binding.

Login and initial device-grant creation have no authenticated session, so they
require exact-origin enforcement and their own throttles. CORS preflight does
not become an authorization mechanism.

### 9.4 Recent re-authentication

Device approval/revoke, password change, TOTP enroll/replace/disable and
recovery-code regeneration require authentication within the last five
minutes.

Recent re-auth always verifies the password and also verifies TOTP when active.
Success rotates the session and records `reauthenticated_at`. A stale session
gets `recent_authentication_required`, not an automatic redirect or silent
approval.

### 9.5 Password change

Web password change requires recent re-auth and the new password. It:

- stores a new Argon2id hash;
- increments credential revision;
- revokes every other Web session;
- rotates and rebinds the current session;
- keeps approved Obsidian devices active; and
- writes an append-only password-change audit event.

## 10. TOTP and recovery

### 10.1 Enrollment

TOTP is optional. After a successful password-only login, the Web client
continues directly to the authenticated application without starting an
enrollment. Security exposes the optional enrollment control when the user
chooses to enable it.

`POST /api/auth/totp/enrollments` accepts one strict discriminated action.
`start` follows the enrollment flow below and requires recent
re-authentication. `dismiss_initial_offer` requires an active session, exact
Origin and CSRF proof, records `totp_prompt_dismissed_at`, returns no secret and
does not create a pending credential. No additional dismissal endpoint exists.

Enrollment requires recent re-auth and:

1. creates a 160-bit random secret;
2. stores an AEAD-encrypted pending credential with a ten-minute expiry;
3. returns the provisioning URI once under `Cache-Control: no-store`;
4. renders the QR locally in Web code, never through a remote service;
5. verifies one submitted code;
6. atomically activates the credential and replay marker; and
7. creates ten recovery codes returned exactly once.

The interoperable contract is RFC 6238 HMAC-SHA-1, six digits and a 30-second
period. The verifier accepts only the previous, current or next time step.

The label contains the canonical username and a fixed product issuer. It does
not contain a path, workspace identifier, secret or arbitrary user text.

### 10.2 Replay prevention

`totp_credentials.last_accepted_time_step` is updated while holding the active
credential row lock. A valid code is accepted only when its time step is newer
than the stored marker. Concurrent requests using the same code cannot both
succeed.

Clock drift outside the ±1 window fails safely. The UI recommends correcting
the device clock; the server does not silently widen the window.

### 10.3 Recovery codes

The service generates ten one-use codes, each twelve Base32 characters grouped
for readability. A domain-separated keyed hash is stored per code; plaintext
is shown only in the activation response.

Regeneration requires password plus current TOTP, invalidates every unused code
in the prior revision and displays a fresh revision once.

Password plus a recovery code:

1. verifies and consumes exactly one code under a row lock;
2. creates a `recovery_limited` session;
3. permits only TOTP replacement or logout; and
4. requires successful replacement before normal Admin access.

Recovery may not downgrade the account to password-only. Losing TOTP and every
recovery code requires the emergency CLI.

Ordinary TOTP disable requires password plus current TOTP, revokes all recovery
codes, increments credential revision, revokes other Web sessions and rotates
the current session to password-only authentication.

## 11. Browser device authorization

### 11.1 Grant creation

`POST /api/auth/device-authorizations` is an unauthenticated plugin endpoint
with exact schema:

```text
client_instance_id  UUID generated once by the plugin; non-secret
device_name        1–80 display characters
platform_class     obsidian_desktop | obsidian_mobile
platform_name      closed supported platform token
plugin_version     validated semantic version
requested_scope    fixed obsidian_sync
claimed_device_id  optional non-secret prior device ID
```

The server rejects unsupported plugin versions before issuing a grant. It caps
live grants per source bucket and client instance.

The response contains:

```text
grant_id
user_code
polling_secret
verification_uri
verification_uri_complete
expires_in_seconds = 600
poll_interval_seconds = 5
```

The user code is short-lived, human-readable and checksum-validated. The
polling secret has at least 256 bits of entropy. PostgreSQL stores hashes of
both.

The plugin stores the polling secret in SecretStorage before opening a browser.
Non-secret grant ID, user code, expiry and SecretStorage name may be stored in
plugin data so Desktop/Mobile restart can resume onboarding.

### 11.2 Browser continuity

`verification_uri_complete` places only the user code in a URL fragment. It
never contains the polling secret, access token or refresh token.

`/device/approve` reads the fragment into memory, immediately removes it with
`history.replaceState` and performs login inline when required. It does not
persist the user code to localStorage or sessionStorage. Reloading a lost page
requires the user to press **Open browser again** in the plugin.

### 11.3 Approval

The page resolves a pending grant only after an active Web session exists and
displays:

- the same user code shown by the plugin;
- device name as escaped text;
- Desktop/Mobile and platform;
- validated plugin version;
- fixed `obsidian_sync` scope; and
- remaining expiry.

Approval requires recent re-authentication and a deliberate **Approve** action.
Denial is explicit and terminal. There is no automatic approval, callback token
delivery or arbitrary scope selection.

The approve/deny transaction locks the grant, rechecks expiry/state, records
the authenticated user/session and writes an audit event. It does not register
a device or mint a token before the plugin exchanges the grant.

### 11.4 Polling

The plugin polls no faster than every five seconds with the polling secret in a
dedicated Bearer header. Polling faster returns
`device_authorization_slow_down` and increases the minimum interval. The
minimum-interval state is durable: it lives in the grant-poll pacing bucket
(`authentication_throttle_buckets` under `bucket_kind = grant_poll`, added by
the 2026-08-31 multi-worker pacing amendment) so every worker observes one
PostgreSQL-authoritative pacing decision; the single-worker observable
behavior is unchanged. Closed outcomes are pending, approved exchange, denied,
expired and invalid.

The plugin stops on deny/expiry, overwrites the pending SecretStorage value with
a non-credential tombstone and removes its non-secret settings reference. It
never deletes Vault data.

## 12. Device registration and initial token exchange

An approved poll locks the grant and atomically:

1. verifies the polling-secret hash, state and expiry;
2. rechecks user/workspace state;
3. creates one new active Obsidian `devices` row;
4. creates one token family;
5. creates initial access and refresh token rows;
6. records the device/family/token lookup IDs on the grant;
7. marks the grant exchanged;
8. writes registration and family-creation audit events; and
9. commits once.

Reauthorization creates a new device identity. A revoked device record remains
revoked for audit lineage; it is not silently reactivated.

### 12.1 Token form

Credentials are opaque and versioned:

```text
access   at1.<lookup_id>.<secret>
refresh  rt1.<lookup_id>.<secret>
```

`lookup_id` is non-secret and selects one indexed row. PostgreSQL stores only a
hash of `secret`. Parsing rejects unknown versions, wrong segment counts,
invalid identifiers and size violations without echoing the rejected value.

### 12.2 Exact exchange replay

The server derives the initial access and refresh secret through a
domain-separated keyed PRF over the presented polling secret, grant identity,
token lookup IDs and stored key ID. It persists only hashes.

If the response is lost after commit, the same polling secret can re-derive and
return the exact credentials with the original issued/expiry timestamps while
the initial refresh generation remains current. It creates no new device,
family, token or audit row.

After the initial refresh generation has rotated, grant polling is terminally
consumed and may not resurrect the old generation.

## 13. Access and refresh tokens

### 13.1 Access token

Access expires 15 minutes after its anchored issue time and carries only the
fixed `obsidian_sync` authority resolved from PostgreSQL.

Every access-authenticated request verifies:

- secret hash and expiry;
- token state;
- family state and absolute expiry;
- device state;
- user/workspace state; and
- required route scope.

An Admin revoke therefore invalidates an unexpired access token on the next
request. `devices.last_seen_at` is conditionally updated at most once per five
minutes; it is not a write on every request.

### 13.2 Refresh lifetime

The family has:

- 30-day inactivity expiry from the last successful rotation; and
- 90-day absolute expiry anchored at family creation.

Rotation may shorten but never extend the absolute expiry.

### 13.3 Plugin crash-safe record

The plugin stores one versioned JSON value in SecretStorage. An active record
contains:

```text
state = active
refresh_credential
refresh_generation
pending_rotation_id | null
```

A cleared record is a tombstone containing only `record_version`,
`state = cleared` and a closed `cleared_reason`; it contains no polling secret,
refresh credential or rotation identity.

Before a refresh, it creates a UUID `rotation_id` and writes the complete active
record with the current refresh credential. It reads that record back and sends
the request only after the value matches. After a successful response it writes
the complete successor record in one `setSecret` call, reads it back and clears
the pending identity. The design does not claim undocumented atomic-durability
semantics from the Obsidian API; server-side exact replay makes either retained
predecessor state or persisted successor state recoverable after a crash.

The plugin access token is memory-only.

### 13.4 Refresh transaction and exact replay

The refresh endpoint receives the refresh credential in a dedicated Bearer
header and `rotation_id` in the strict body.

For an active current refresh token plus a new rotation identity, it locks the
family/token and atomically:

- creates the successor refresh generation;
- creates a new 15-minute access token;
- marks the predecessor rotated and links the successor;
- stores the rotation identity and derivation key ID;
- advances the family current generation;
- advances inactivity expiry without exceeding absolute expiry; and
- commits once.

The successor material is derived from the presented predecessor secret,
rotation identity, family/generation and key ID. PostgreSQL stores its hash.

If the response is lost, retrying the same predecessor with the same
`rotation_id` returns the exact successor and anchored timestamps while that
successor remains the current generation. No extra token rows or expiry
extension occur.

### 13.5 Confirmed reuse

The whole family is revoked when:

- a rotated predecessor is presented with a different `rotation_id`;
- a predecessor is presented after its successor has rotated again;
- a terminal, expired or revoked credential is used as current; or
- stored lineage/replay evidence violates its invariant.

Reuse revocation locks the family, marks every usable token revoked, marks the
device revoked when compromise is confirmed, writes an audit event and returns
the terminal `device_token_reuse_detected` response. The plugin overwrites the
credential record with a non-secret tombstone, clears its settings reference
and requires browser authorization again.

## 14. Device revoke

### 14.1 Admin revoke

`POST /api/admin/devices/{device_id}/revoke` requires an active Web session,
recent re-authentication, CSRF proof and an exact device-name confirmation.

The transaction locks the device/family rows and atomically:

- changes the device to revoked;
- revokes all active token families and token generations;
- denies any grant explicitly linked to that existing device identity;
- records a closed revocation reason; and
- writes an audit event.

The system bootstrap device is excluded from this route.

### 14.2 Plugin self-revoke

`POST /api/auth/device-tokens/revoke-current` authenticates the current refresh
credential and revokes its device family atomically. After a confirmed response,
the plugin overwrites its SecretStorage value with a non-credential tombstone,
verifies the tombstone by reading it back, clears the non-secret settings
reference and clears the in-memory access token.

When offline, **Disconnect** reports that secure revoke cannot complete. The
user may retry online or revoke the device through Web Admin. It does not
pretend that clearing local credential material revoked the server credential.

## 15. PostgreSQL evolution

One forward Alembic revision adds the following normalized tables. Every
constraint/index has a semantic name, every quantity field carries a unit and
every state uses a closed check constraint.

### 15.1 `user_credentials`

```text
user_id PK/FK
workspace_id FK
password_hash
credential_revision
totp_prompt_dismissed_at nullable
password_changed_at
created_at
updated_at
```

One row exists per enrolled user. The PHC hash is treated as secret-bearing
material in repr/log boundaries even though it is one-way.

### 15.2 `web_sessions`

```text
web_session_id PK
user_id / workspace_id
session_secret_hash unique
csrf_secret_hash
state
credential_revision
authentication_method
created_at / authenticated_at / reauthenticated_at
last_seen_at / idle_expires_at / absolute_expires_at
revoked_at / revocation_reason nullable
```

Checks bind state to required timestamps and ensure idle expiry never exceeds
absolute expiry.

### 15.3 `totp_credentials`

```text
totp_credential_id PK
user_id / workspace_id
state
secret_ciphertext / secret_nonce / key_id
algorithm / digits / period_seconds
last_accepted_time_step nullable
enrollment_expires_at nullable
revision
created_at / activated_at / replaced_at
```

Partial unique indexes allow one active and at most one pending credential per
user.

### 15.4 `totp_recovery_codes`

```text
recovery_code_id PK
totp_credential_id / user_id / workspace_id
revision
code_hash
created_at
used_at nullable
```

The hash is unique within a credential revision. `used_at` is immutable once
set.

### 15.5 `device_authorization_grants`

```text
grant_id PK
user_code_hash unique
polling_secret_hash unique
client_instance_id
claimed_device_id nullable
device_name / platform_class / platform_name / plugin_version
requested_scope
state
created_at / expires_at
approved_at / denied_at / exchanged_at nullable
approved_by_user_id / approved_web_session_id nullable
device_id / token_family_id nullable
initial_access_token_id / initial_refresh_token_id nullable
derivation_key_id nullable
```

Checks enforce the pending/approved/denied/exchanged timestamp and reference
matrix.

### 15.6 `device_token_families`

```text
token_family_id PK
user_id / workspace_id / device_id
state
current_refresh_generation
created_at / last_refreshed_at
inactivity_expires_at / absolute_expires_at
revoked_at / revocation_reason nullable
```

One family is active per registered device in this child.

### 15.7 `device_tokens`

```text
device_token_id PK
token_family_id / user_id / workspace_id / device_id
token_kind = access | refresh
generation
secret_hash unique
state
predecessor_token_id nullable
successor_token_id nullable
rotation_id nullable
derivation_key_id
issued_at / expires_at
rotated_at / revoked_at nullable
```

Unique and partial indexes enforce one current refresh generation and prevent
two successors for one predecessor/rotation transition.

### 15.8 `authentication_throttle_buckets`

```text
throttle_bucket_id PK
bucket_kind = login_username | login_source | grant_creation | user_code_lookup | totp_verification | recovery_verification | grant_poll
bucket_hash
window_started_at
failed_attempt_count
locked_until nullable
updated_at
```

`(bucket_kind, bucket_hash)` is unique. No raw username or source address is
stored. `grant_poll` paces device-grant polling (2026-08-31 multi-worker
amendment, revision `20260901_01`); the schema admits it before any behavior
writes it.

### 15.9 Migration gates

The revision must pass:

- empty database upgrade;
- Phase 1 fixture upgrade;
- exact-head application smoke;
- schema reflection for tables, columns, FKs, checks, uniques and indexes; and
- deterministic downgrade back to the Phase 1 head.

Downgrade is refused outside an explicit destructive test/operator gate when
auth rows exist.

## 16. HTTP API contract

### 16.1 Session/password routes

```text
POST /api/auth/login
GET  /api/auth/session
POST /api/auth/logout
POST /api/auth/reauthenticate
PUT  /api/auth/password
```

### 16.2 TOTP/recovery routes

```text
POST   /api/auth/totp/verify
POST   /api/auth/totp/enrollments  # action = start | dismiss_initial_offer
POST   /api/auth/totp/enrollments/{enrollment_id}/verify
POST   /api/auth/totp/recovery
POST   /api/auth/totp/recovery-codes/regenerate
DELETE /api/auth/totp
```

### 16.3 Device routes

```text
POST /api/auth/device-authorizations
POST /api/auth/device-authorizations/lookup
POST /api/auth/device-authorizations/{grant_id}/approve
POST /api/auth/device-authorizations/{grant_id}/deny
POST /api/auth/device-authorizations/{grant_id}/poll
POST /api/auth/device-tokens/refresh
POST /api/auth/device-tokens/revoke-current
```

### 16.4 Admin routes

```text
GET  /api/admin/devices
POST /api/admin/devices/{device_id}/revoke
```

Every route has a strict Pydantic body/data model, manually assigned semantic
`operationId`, explicit auth scheme and exact closed response set. There is no
trailing-slash redirect.

Auth responses include `Cache-Control: no-store`; provisioning and recovery
responses also include `Pragma: no-cache`. Polling, refresh and self-revoke
credentials use dedicated Bearer schemes and never appear in URL paths or query
parameters.

The route/model change, normalized OpenAPI snapshot, generated TypeScript and
contract tests land together. The generated client performs no automatic retry;
feature code owns the stable grant/rotation retry identities.

## 17. Error contract

The child extends the closed registry and HTTP mapping:

| Error code | HTTP | Retryable | Safe details |
|---|---:|---:|---|
| `authentication_required` | 401 | no | none |
| `authentication_failed` | 401 | no | none |
| `authentication_rate_limited` | 429 | yes | `retry_after_seconds` |
| `recent_authentication_required` | 403 | no | none |
| `csrf_validation_failed` | 403 | no | none |
| `authorization_scope_denied` | 403 | no | none |
| `totp_enrollment_state_invalid` | 409 | no | none |
| `device_authorization_pending` | 409 | yes | `retry_after_seconds` |
| `device_authorization_slow_down` | 429 | yes | `retry_after_seconds` |
| `device_authorization_denied` | 403 | no | none |
| `device_authorization_expired` | 410 | no | none |
| `device_authorization_state_invalid` | 409 | no | none |
| `device_revocation_confirmation_invalid` | 409 | no | none |
| `device_credential_invalid` | 401 | no | none |
| `device_revoked` | 401 | no | none |
| `device_token_reuse_detected` | 401 | no | none |
| `plugin_version_unsupported` | 426 | no | approved version bounds only |

`device_revocation_confirmation_invalid` answers the section 14.1 exact
display-name confirmation mismatch; it was added at implementation because the
table had no expression for it.

No error contains username, user code, device name, token/lookup ID, source
address, rejected field value, SQL/driver text or crypto exception.

Database unavailability reuses the child 1 `database_connection_unavailable`
503 mapping. Corrupt internal auth state returns the safe `internal_error`
public envelope and a closed internal diagnostic event; it is not auto-repaired.

## 18. Minimal Web Admin

Child 2 creates only:

```text
/login
/device/approve
/admin/devices
/admin/security
```

The authenticated root redirects to `/admin/devices`; unauthenticated access
redirects to `/login` without preserving attacker-controlled arbitrary return
URLs.

### 18.1 Login

The page supports password, TOTP and recovery-code steps, generic failure,
and bounded retry guidance. Optional TOTP enrollment is initiated from
Security, not presented during password-only login.

### 18.2 Device approval

The page keeps grant context in memory, performs inline login when necessary,
renders plugin metadata as escaped text and presents Approve/Deny with keyboard
and screen-reader semantics.

### 18.3 Devices

The list shows device name, Desktop/Mobile, platform, validated plugin version,
active/revoked status, registered/last-seen/revoked time and family expiry.
Revoke requires recent re-auth and exact-name confirmation. Revoked rows remain
read-only.

### 18.4 Security

The page owns password change, TOTP enroll/replace/disable, recovery-code
regeneration and logout. It does not list browser sessions.

There are no placeholder policy, source, search or workflow pages. Later
children add navigation only with real behavior.

## 19. Obsidian plugin boundary

The plugin adds one settings tab with:

- exact server origin;
- editable device name;
- closed connection status;
- **Login**;
- **Open browser again**;
- **Cancel pending login**; and
- **Disconnect**.

The server origin must be HTTPS with no path, query, fragment or embedded
credential. Explicit local-development builds may allow loopback HTTP.

Closed states are:

```text
not_connected
requesting_authorization
waiting_for_approval
connected
offline
refresh_required
revoked
configuration_invalid
```

On load the plugin reads non-secret settings, resolves its SecretStorage record
and performs at most one bounded action:

- resume an unexpired pending grant; or
- refresh an existing device credential.

It does not create a long-running background sync loop in this child.

Offline/timeout preserves SecretStorage and reports a recoverable state.
Terminal revoked/reuse responses clear access memory, replace pending/refresh
records with non-secret tombstones, clear their settings references and reset
connection state while preserving every local Vault file and future queue.

The plugin targets Obsidian 1.13.1, which is above the SecretStorage API's
documented 1.11.4 introduction. Plugin data stores only a SecretStorage record
name, never secret material. Record names use lowercase ASCII letters, digits
and dashes so they satisfy the API identifier grammar. Because SecretStorage
offers `setSecret`, `getSecret` and `listSecrets` but no delete operation, this
child never claims to remove a SecretStorage key. It relies only on the
documented vault-local key/value boundary and makes no claim that Obsidian uses
an operating-system keychain or provides filesystem-level encryption.

The plugin uses only `requestUrl`, `Platform`, `SecretStorage`, settings,
modal and notice UI, and browser-opening APIs through narrow adapters
(notice UI added 2026-08-23 as the closed surface of the sync diagnostics
commands). Static tests prohibit Node.js, Electron and `FileSystemAdapter`
imports at module load time.

## 20. Security and privacy

### 20.1 Authentication keyring

`personal-api serve` loads a bounded ordered keyring of versioned 32-byte
authentication master keys from exact secret files beneath the existing secret
root. Non-secret runtime configuration names the current key ID/file and any
previous key IDs/files; secret values never appear in environment values.

HKDF domain separation derives subkeys for:

- grant/token exact replay;
- TOTP authenticated encryption;
- CSRF hashing;
- throttle-bucket HMAC; and
- recovery-code hashing.

New state uses the current key. Previous keys remain only until every referenced
grant/token replay state expires and all TOTP ciphertext is re-encrypted. A key
cannot be removed while PostgreSQL still references its ID.

Missing, duplicate, short, malformed or permission-unsafe key files fail
`serve` before socket exposure. Offline OpenAPI export injects a deterministic
non-secret crypto port and reads no runtime secret.

The crypto adapter rejects any subkey derivation whose label is not in the
closed `CRYPTO_DOMAIN_LABELS` vocabulary, failing closed as `INTERNAL_ERROR`
without echoing the rejected label. The offline composition enforces the
same membership check, so a subkey domain cannot be mixed through either
adapter.

### 20.2 Web headers

Web responses enforce a nonce-based CSP with at least:

```text
default-src 'self'
script-src 'self' 'nonce-<per-response>'
connect-src 'self'
img-src 'self' data:
object-src 'none'
base-uri 'none'
frame-ancestors 'none'
form-action 'self'
```

They also set `Referrer-Policy: no-referrer` and
`X-Content-Type-Options: nosniff`. No third-party script, analytics, remote QR
renderer or auth widget is allowed. HSTS and TLS termination belong to the
reverse-proxy deployment contract.

### 20.3 Trusted proxy

Forwarded client-address headers are accepted only when the immediate socket
peer belongs to an exact configured trusted-proxy CIDR. The resolver selects
the rightmost untrusted hop with a bounded chain length. Otherwise it uses the
socket peer. The result is HMACed immediately for throttling and never logged.

### 20.4 Telemetry prohibition

Never log, metric, trace, audit-detail or error-report:

- password, TOTP secret/code or recovery code;
- session, CSRF, polling, access or refresh credential;
- user code or provisioning URI;
- cookie, Authorization header or request/response body;
- username, source address or device name;
- token lookup ID, grant ID or full crypto hash; or
- raw path/query and provider/framework exception text.

Child 1's Uvicorn access log remains disabled. The deployment proxy must not
log credential headers or query values.

## 21. Audit and observability

Append-only audit actions cover:

- known-account login success/rejection and lockout transition;
- password enrollment/change/emergency reset;
- TOTP enroll/replace/disable;
- recovery-code use/regeneration;
- device authorization approve/deny;
- device registration and revoke; and
- token-family creation, confirmed reuse and terminal revoke.

Unknown-account attempts have no trusted workspace; they create only bounded
diagnostics and throttle state.

Normal access-token checks and successful refresh rotations use token rows and
metrics rather than one audit row per request. Metrics use closed method,
operation, outcome and error labels plus counts/durations. No identity,
address, device or credential becomes a metric label.

Every emitted event uses the existing server-owned request/trace context.

## 22. Failure and recovery behavior

| Failure | Server behavior | Client behavior |
|---|---|---|
| PostgreSQL unavailable | 503, no auth claim | Web retries manually; plugin preserves secrets |
| Auth key unavailable | refuse startup | no endpoint exposed |
| Timeout or lost response | no assumed rollback; exact replay stays available | retry stable grant/rotation identity |
| Device revoked | terminal 401 | tombstone local credential, preserve Vault, login again |
| Confirmed token reuse | revoke family/device, terminal 401 | tombstone local credential and reauthorize |
| Corrupt auth invariant | safe internal failure, no auto-repair | preserve local data and show request ID |
| TOTP clock drift | reject outside ±1 | correct clock; never widen silently |
| Unsupported plugin | 426 with safe version bounds | require plugin update |

Persisted timestamps and expiry use PostgreSQL time. Application monotonic time
is used only for deadlines and duration metrics.

Terminal/expired auth rows remain during Phase 2 for evidence and audit.
Request paths perform no opportunistic physical deletion. Child 9 owns bounded
retention and pruning operations.

## 23. Dependencies and configuration

The implementation plan may propose and pin only these new production roles:

- one reviewed Argon2id implementation;
- one reviewed authenticated-encryption/HKDF implementation; and
- one local client-side QR renderer.

It must document exact pins, transitive impact, license, mobile/Web build impact,
host benchmark and failure mapping. An auth framework, JWT library, external QR
service or hosted identity provider is not authorized.

Non-secret configuration includes password/TOTP limits, session/grant/token
durations, plugin version bounds, trusted proxy CIDRs and key IDs/file names.
Secrets remain exact files under the existing bounded secret root.

## 24. Testing

### 24.1 Domain/unit

- Password validation, blocklist and Argon2id upgrade decisions.
- Session state/expiry/revision and recent re-auth transitions.
- TOTP enrollment, ±1 window and replay marker.
- Recovery generation, consumption and limited-session authorization.
- Grant code/secret validation and state transitions.
- Token parsing, hash and deterministic derivation vectors.
- Refresh rotation, exact replay and confirmed reuse classification.
- Scope, CSRF, throttle and trusted-proxy resolution.

### 24.2 Property/race

- The same TOTP code succeeds at most once.
- Approve races deny and expiry deterministically.
- Duplicate exchange produces one device/family and one exact result.
- Same predecessor/same rotation ID produces one successor and exact replay.
- Same predecessor/different rotation ID revokes the family.
- Refresh races Admin revoke and self-revoke safely.
- Password change/reset races active session use safely.
- Emergency reset races pending approval without leaving usable credentials.

### 24.3 Migration/integration

- Empty upgrade, Phase 1 fixture upgrade, reflection and downgrade.
- PostgreSQL login/session/TOTP/grant/refresh/revoke transactions.
- Injected ambiguous commit acknowledgements for grant exchange and refresh.
- Keyring current/previous-key transition and removal refusal.

### 24.4 HTTP/contract

- Canonical envelopes and every closed HTTP mapping.
- Cookie flags, exact Origin, CSRF, `no-store`, CSP and absent CORS.
- OpenAPI security schemes, semantic operation IDs and deterministic snapshot.
- Generated TypeScript compile and no stale artifact.
- No unexpected auth/Admin route or trailing-slash redirect.
- Malformed token/header/body never echoes a rejected value.

### 24.5 Web

- Component behavior with Testing Library and MSW.
- Playwright login, TOTP enrollment/recovery, password change, approval/deny,
  device list/revoke and session expiry.
- Keyboard/focus/screen-reader smoke on every critical route.
- No credential persistence in localStorage/sessionStorage, analytics or error
  breadcrumbs.

### 24.6 Plugin

- SecretStorage adapter and settings-data separation.
- Pending grant resume and expiry.
- Startup refresh and one-attempt bound.
- Crash after response but before SecretStorage replacement.
- Offline preservation and terminal revoked/reuse cleanup.
- Self-revoke success/failure.
- Mobile import boundary and production bundle inspection.

### 24.7 Leak tests

Sentinel passwords, TOTP secrets/codes, recovery codes, user codes, device names
and all token forms are scanned across HTTP, diagnostics, audit, traces,
generated files and Web/plugin production builds.

## 25. Acceptance criteria

Child 2 is complete only when one final commit proves:

1. Internal CLI enrolls the first password without public signup.
2. Password login enforces 12-hour idle and seven-day absolute session limits.
3. Optional TOTP, replay defense, recovery codes and recovery-limited sessions
   work.
4. Plugin Login opens the exact approval page and stores the refresh credential
   only in SecretStorage.
5. Device grant exchange registers exactly one device and family.
6. Lost grant/refresh responses exact-replay without duplicate or false reuse.
7. Confirmed refresh reuse revokes the whole family/device.
8. Admin revoke and plugin self-revoke immediately disable access.
9. Normal password change revokes other Web sessions but keeps approved devices.
10. Emergency reset revokes sessions, devices and pending grants.
11. Migration, OpenAPI, generated client, Web/plugin builds and leak gates pass.
12. The auth operations runbook and exactly one implementation-plan handoff
    contain final gate evidence and deferred rulings.

## 26. Implementation boundary for the next plan

The implementation plan should preserve these dependency-ordered deliverables:

1. Domain contracts, registries, deterministic crypto/password ports and tests.
2. Alembic schema and PostgreSQL repositories.
3. Credential CLI, password login, session, CSRF and recent re-auth.
4. TOTP/recovery.
5. Device grant, registration, token rotation/revoke and exact replay.
6. FastAPI routes, errors, OpenAPI and generated client.
7. Minimal Web Admin.
8. Plugin SecretStorage/onboarding state machine.
9. Cross-boundary, leak, integration and operations gates.

This is ordering guidance, not authorization to implement before the written
plan is approved.

## 27. Visual companions

- [System boundary](html/2.%20web-auth-and-device-authorization-design/2026-08-16-web-auth-and-device-authorization-system-boundary.html)
- [Login and Web session](html/2.%20web-auth-and-device-authorization-design/2026-08-16-web-auth-and-device-authorization-login-session.html)
- [TOTP and recovery](html/2.%20web-auth-and-device-authorization-design/2026-08-16-web-auth-and-device-authorization-totp-recovery.html)
- [Device and token flow](html/2.%20web-auth-and-device-authorization-design/2026-08-16-web-auth-and-device-authorization-device-token-flow.html)
- [Schema and API](html/2.%20web-auth-and-device-authorization-design/2026-08-16-web-auth-and-device-authorization-schema-api.html)
- [Minimal Admin and plugin UI](html/2.%20web-auth-and-device-authorization-design/2026-08-16-web-auth-and-device-authorization-admin-plugin-ui.html)
- [Security and operations](html/2.%20web-auth-and-device-authorization-design/2026-08-16-web-auth-and-device-authorization-security-operations.html)
- [Testing and acceptance](html/2.%20web-auth-and-device-authorization-design/2026-08-16-web-auth-and-device-authorization-testing-acceptance.html)

## 28. References

- `docs/00-PRODUCT_VISION_AND_PRD.md`
- `docs/01-CANONICAL_ARCHITECTURE.md`
- `docs/02-TECH_STACK.md`
- `docs/04-OBSIDIAN_SYNC_AND_SOURCES.md`
- `docs/07-POSTGRESQL_DATA_MODEL.md`
- `docs/12-API_MCP_AND_AGENT_INTEGRATION.md`
- `docs/13-WEB_APP_AND_ADMIN_DASHBOARD.md`
- `docs/14-SECURITY_PRIVACY_AND_POLICY.md`
- `docs/19-ARCHITECTURE_DECISIONS.md`
- `docs/20-IMPLEMENTATION_PLAN.md`
- `docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md`
- `docs/superpowers/specs/2026-08-15-api-runtime-and-contract-foundation-design.md`
- [RFC 8628 — OAuth 2.0 Device Authorization Grant](https://www.rfc-editor.org/info/rfc8628/)
- [RFC 6238 — Time-Based One-Time Password Algorithm](https://www.rfc-editor.org/rfc/rfc6238)
- [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
- [NIST SP 800-63B](https://pages.nist.gov/800-63-4/sp800-63b.html)
- [Obsidian SecretStorage](https://docs.obsidian.md/plugins/guides/secret-storage)
