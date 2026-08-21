# Obsidian Live Acceptance Bootstrap Spec

## Goal

Provide one repository entrypoint that prepares a fresh disposable
`knowledge-ci-*` identity for live Obsidian acceptance and never treats a
missing active TOTP credential as a deferrable prerequisite.

## Scope

The entrypoint runs after the services have been started in the exact order in
`.local/RESTART.md`. It validates `CI=true` and the disposable project name,
applies the current migration, creates or replays the canonical Web identity,
creates the initial Web password credential when absent, initializes or replays
the policy signing key, publishes a policy through
`.local/publish-policy-revision.py`, and runs the focused WDIO journey.

The entrypoint uses `.local/e2e-totp-code.py` as a mandatory preflight. If that
helper cannot produce a code, the entrypoint logs in with the protected
file-backed Web credential, starts a TOTP enrollment through
`POST /api/auth/totp/enrollments`, computes one verification code in memory,
and activates the enrollment through
`POST /api/auth/totp/enrollments/{enrollment_id}/verify`. It reruns the helper
and refuses to publish policy or launch WDIO unless the helper succeeds.

## Contracts

- The project name must match `knowledge-ci-[a-z0-9][a-z0-9-]{0,40}` and the
  process environment must contain `CI=true`.
- Runtime setting names are loaded from the local launcher contract; secret
  values are read only through the repository secret-file boundary.
- Identity bootstrap and Web password enrollment use the existing protected
  CLIs. TOTP activation uses only the real HTTP routes; no SQL mutation is
  permitted.
- Child stdout and stderr are always captured or discarded. The entrypoint
  emits only closed JSON status documents and never emits a password, TOTP
  secret, TOTP code, recovery code, cookie, token, path, locator, or child
  exception text.
- `.local/e2e-totp-code.py` is a post-activation code producer. Its missing
  active-credential result selects the enrollment branch; it is not a BLOCKED
  or deferred outcome.
- Policy publication and WDIO are unreachable until the post-enrollment helper
  preflight succeeds.
- Existing `.local/RESTART.md`, `.local/serve-local.sh`,
  `.local/run-worker.sh`, `.local/e2e-totp-code.py`, and
  `.local/publish-policy-revision.py` remain the canonical local contracts.

## Failure behavior

Invalid project/environment input, malformed safe CLI output, HTTP rejection,
failed post-enrollment preflight, policy publication failure, and WDIO failure
all return nonzero with one closed result code. Raw child or provider output is
never forwarded.

## Acceptance criteria

1. A simulated fresh disposable identity begins without active TOTP, completes
   the real enrollment route choreography, proves the helper succeeds, and
   launches policy publication and WDIO in that order.
2. A rerun with active TOTP skips enrollment and still performs the mandatory
   preflight before policy publication and WDIO.
3. Sensitive sentinels returned by HTTP and child processes never appear in
   the entrypoint output.
4. AGENTS.md explicitly states that missing active TOTP requires bootstrap and
   cannot be labeled blocked or deferred by itself.
