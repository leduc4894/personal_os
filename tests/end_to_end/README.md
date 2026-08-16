# End-to-end tests

## Status

This layer owns the repository's Playwright end-to-end suites. It left the
bootstrap-reserved state when the web-authentication child landed its first
real spec (`authentication/web-security.spec.ts`); that child's plan
(`docs/superpowers/plans/2026-08-16-web-auth-and-device-authorization.md`)
schedules the remaining authentication flows here as well.

## Layout

- `playwright.config.ts` at the repository root configures the web server and
  the browser project.
- `authentication/` holds the browser-flow specs. Each spec proves one
  user-facing journey with real assertions; none may silently pass with zero
  assertions.

## Conventions

- Specs run via `pnpm exec playwright test` from the repository root.
- A spec proves its UI flow (storage hygiene, redirects, one-time value
  handling) with `page.route` interception unless a task states otherwise.
- Later children add their own domain folders with real behavior; placeholder
  or empty specs stay forbidden.
