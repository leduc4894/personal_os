# Retire Initial TOTP Offer Specification

## Decision

TOTP is enabled voluntarily from Web Admin Security. A recovery-limited
session must enroll a replacement authenticator and cannot skip. The former
post-login initial-offer and its `dismiss_initial_offer` action are removed.

## Removed Contract

- `TotpEnrollmentAction` contains only `start`.
- `POST /api/auth/totp/enrollments` no longer accepts or returns a dismissal
  action or `dismissed_at` field.
- `TotpService`, its transaction port, offline composition, PostgreSQL store,
  and Web client expose no prompt-dismissal operation.
- `knowledge.user_credentials.totp_prompt_dismissed_at` and its timestamp
  constraint clause are removed by a reversible Alembic migration.
- `TotpEnrollmentOffer` has no skip callback or Skip button. Security
  enrollment remains optional because users enter it intentionally; recovery
  replacement remains mandatory through `requireCompletion`.

## Preserved Behavior

- Active password login goes directly to Admin without creating a TOTP
  enrollment.
- Security enrollment still requires recent authentication and returns exactly
  one provisioning offer.
- Recovery-code login still requires replacement enrollment and yields
  recovery codes only after activation.
- Existing Origin/CSRF, no-store, secret redaction, API envelope, and TOTP
  credential behavior are unchanged.

## Acceptance Criteria

1. Source and generated OpenAPI contain no dismissal vocabulary or timestamp.
2. The route rejects `{"action":"dismiss_initial_offer"}` as validation
   failure without mutating credential state.
3. Upgrade drops the column and replaces the check constraint; downgrade
   restores both exact prior structures.
4. Web tests prove Security enrollment has no Skip control and recovery
   replacement remains mandatory; no UI can call a dismissal method.
