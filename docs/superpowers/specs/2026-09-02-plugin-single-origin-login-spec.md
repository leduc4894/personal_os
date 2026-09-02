# Plugin Single-Origin Login Specification

## Goal

Make the public workspace origin the single configuration value used by the
Web Admin, browser device approval, and the Obsidian plugin's API requests.
The deployment configures that origin through `KNOWLEDGE_AUTH_ALLOWED_ORIGIN`;
the plugin never derives or embeds a second browser hostname.

## Contract

- The public workspace origin serves the Web Admin at `/*` and proxies API
  routes at `/api/*` to FastAPI.
- `KNOWLEDGE_AUTH_ALLOWED_ORIGIN` is an exact, scheme/host/port-only public
  workspace origin. It is the browser session/CSRF origin and the base used
  by `DeviceAuthorizationService` to mint `verification_uri` and
  `verification_uri_complete`.
- Plugin `server_origin` means that same public workspace origin. It is not a
  direct API hostname. The plugin sends its API traffic to
  `${server_origin}/api/*` and opens only the server-returned
  `verification_uri_complete`.
- Live-test variables `E2E_ALLOWED_ORIGIN` and `E2E_PLUGIN_ORIGIN` must carry
  the same public workspace origin. `E2E_SERVER_ORIGIN` remains a local,
  server-evidence-only endpoint and is never written to plugin settings.
- A direct `api.<host>` tunnel route is out of the supported plugin onboarding
  topology. It may not be documented or emitted as a plugin origin.

## Failure and Security Behavior

- The existing exact Origin and CSRF checks remain unchanged. A browser page
  from an unconfigured origin still receives the registered closed rejection.
- URL fragments continue to contain only the one-time user code; polling
  secrets, device credentials, API origins, and raw paths never appear in
  browser URLs, diagnostics, or evidence.
- The API contract and generated client are unchanged: this is deployment and
  plugin configuration alignment, not a new endpoint or response field.

## Acceptance Criteria

1. Plugin unit tests prove grant creation targets the configured public origin
   and that Login/Open browser again opens the verification URI supplied by
   the server, rather than an API hostname assembled by the plugin.
2. Live bootstrap and WDIO configuration write the same public origin to the
   plugin fixture and use it for browser approval; their local API endpoint is
   retained only for server-side setup/evidence.
3. The restart and operator runbooks state the one-origin topology and name
   `KNOWLEDGE_AUTH_ALLOWED_ORIGIN` as its deployment setting.
4. The corresponding BACKLOG row is removed only with a handoff recording
   the focused test evidence and the sanitized live configuration check.
