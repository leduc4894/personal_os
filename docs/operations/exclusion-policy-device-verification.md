# Exclusion Policy Reference-Device Verification

Operator-observed evidence for the exclusion-policy publication child (spec
23.5/25): Desktop and Mobile Obsidian reference devices verified initial
trust, snapshot verification, rotation, offline cache and Vault preservation
against the live local stack (knowledge-local, PostgreSQL/Temporal compose)
exposed through a Cloudflare Tunnel at `https://app.ducinvest.com`
(single public origin for the Web Admin and `/api/*`; the plugin uses the
same origin). Server-side timestamps cited below come from the API
diagnostic log recorded during the session.

The journey itself surfaced and fixed three real cross-surface defects
before any scenario below could pass — recorded here because that is the
primary purpose of reference-device evidence:

1. The plugin sent no `Origin` header on device-grant creation while the
   server's exact-origin gate requires it (fixed in commit `5c6670f`).
2. The exact-origin gate rejected every same-origin browser GET because
   browsers omit `Origin` on safe-method fetches (fixed in `050387c`).
3. The deployment must use one public origin for the Web app and `/api/*`
   (spec decision 3); a two-hostname split failed the gate until aligned.

## Desktop reference device

Device name `Ultra Gear` (Obsidian desktop on Windows, plugin 0.1.0),
registered 2026-08-18 14:35 UTC against policy revision 1.

- Initial trust: after browser device-authorization approval the plugin fetched keyset revision 1 and the signed empty-policy snapshot immediately (server log 14:35:09, snapshot 200) and reported Connected with the policy active.
- Snapshot verification: after revision 2 (extension `.tmp` deny rule) was published, an Obsidian restart fetched the new snapshot bytes (server log 14:57:03, snapshot 200), the Ed25519 signature verified and the device stayed Connected with revision 2 enforced.
- Rotation: through keyset revisions 2-4 (stage, activate, retire; signer key A retired, key B current) the device paged and verified the cross-signed chain (server log 15:01:41, keysets 200) and stayed Connected without any manual trust action.
- Offline cache: with networking disabled the plugin preserved its credentials and the last verified snapshot and reported the offline state; after reconnecting and restarting Obsidian it returned to Connected (the plugin runs no background refresh loop by design).
- Vault preservation: after two policy publications, a full signing-key rotation and the offline test, every Vault file on the device was unchanged — the policy never alters Vault content.

Recorded by Duc on 2026-08-18.

## Mobile reference device

Device name `Iphone` (Obsidian mobile on iOS, plugin 0.1.0, installed into
the vault's plugin folder over the Files app), registered 2026-08-18 14:48
UTC against policy revision 1.

- Initial trust: onboarding opened the approval page in the on-device browser over the tunneled origin, and immediately after approval the plugin fetched keyset revision 1 and the signed snapshot (server log 14:48:28, snapshot 200) and reported Connected.
- Snapshot verification: after revision 2 was published, an Obsidian restart fetched the new snapshot bytes (server log 14:57:17, snapshot 200), verified the signature and stayed Connected.
- Rotation: after the keyset chain advanced to revision 4 the device's refresh verified against its cached trust with an unchanged snapshot digest (server log 15:01:47, snapshot 304) and stayed Connected.
- Offline cache: airplane mode moved the plugin to the offline state with credentials and the cached verified snapshot preserved; after disabling airplane mode and restarting Obsidian it returned to Connected (no background refresh loop by design).
- Vault preservation: every Vault file on the device was unchanged after the publication, rotation and offline testing — the policy never alters Vault content.

Recorded by Duc on 2026-08-18.

## Pending: child-4 reference-device evidence

The plugin journal and small-file sync child has its own operator evidence
procedure (same sanitized-labels-only discipline) in
`docs/operations/plugin-journal-small-file-sync.md`; its Desktop and Mobile
records are pending and have not been recorded here.
