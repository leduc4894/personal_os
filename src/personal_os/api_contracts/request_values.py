"""Closed HTTP method and route-template values used by safe access diagnostics.

These closed enum values are the only method and route scalars access
observations may carry; raw paths, query strings and headers never enter
diagnostics. Route values contain ``/`` but remain safe closed enum values.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ApiHttpMethod(StrEnum):
    """Closed HTTP method vocabulary; ``OTHER`` buckets every non-GET method."""

    GET = "GET"
    OTHER = "OTHER"


class ApiRouteTemplate(StrEnum):
    """Closed route-template vocabulary; ``UNMATCHED`` marks unknown routes."""

    HEALTH_LIVE = "/api/health/live"
    HEALTH_READY = "/api/health/ready"
    AUTH_LOGIN = "/api/auth/login"
    AUTH_SESSION = "/api/auth/session"
    AUTH_LOGOUT = "/api/auth/logout"
    AUTH_REAUTHENTICATE = "/api/auth/reauthenticate"
    AUTH_PASSWORD = "/api/auth/password"
    AUTH_TOTP_VERIFY = "/api/auth/totp/verify"
    AUTH_TOTP_ENROLLMENTS = "/api/auth/totp/enrollments"
    AUTH_TOTP_ENROLLMENT_VERIFY = "/api/auth/totp/enrollments/{enrollment_id}/verify"
    AUTH_TOTP_RECOVERY = "/api/auth/totp/recovery"
    AUTH_TOTP_RECOVERY_CODES_REGENERATE = "/api/auth/totp/recovery-codes/regenerate"
    AUTH_TOTP_DISABLE = "/api/auth/totp"
    AUTH_DEVICE_AUTHORIZATIONS = "/api/auth/device-authorizations"
    AUTH_DEVICE_AUTHORIZATION_LOOKUP = "/api/auth/device-authorizations/lookup"
    AUTH_DEVICE_AUTHORIZATION_APPROVE = "/api/auth/device-authorizations/{grant_id}/approve"
    AUTH_DEVICE_AUTHORIZATION_DENY = "/api/auth/device-authorizations/{grant_id}/deny"
    AUTH_DEVICE_AUTHORIZATION_POLL = "/api/auth/device-authorizations/{grant_id}/poll"
    AUTH_DEVICE_TOKENS_REFRESH = "/api/auth/device-tokens/refresh"
    AUTH_DEVICE_TOKENS_REVOKE_CURRENT = "/api/auth/device-tokens/revoke-current"
    ADMIN_DEVICES = "/api/admin/devices"
    ADMIN_DEVICE_REVOKE = "/api/admin/devices/{device_id}/revoke"
    ADMIN_SYNC_REJECTIONS = "/api/admin/sync/rejections"
    ADMIN_SOURCE_LIFECYCLE_REJECTIONS = "/api/admin/source-lifecycle/rejections"
    ADMIN_EXCLUSION_POLICY = "/api/admin/exclusion-policy"
    ADMIN_EXCLUSION_POLICY_DRAFT = "/api/admin/exclusion-policy/draft"
    ADMIN_EXCLUSION_POLICY_PREVIEWS = "/api/admin/exclusion-policy/previews"
    ADMIN_EXCLUSION_POLICY_PREVIEW = "/api/admin/exclusion-policy/previews/{policy_preview_id}"
    ADMIN_EXCLUSION_POLICY_PUBLICATIONS = "/api/admin/exclusion-policy/publications"
    ADMIN_EXCLUSION_POLICY_DIAGNOSTICS = "/api/admin/exclusion-policy/diagnostics"
    SYNC_EXCLUSION_POLICY_KEYSETS = "/api/sync/exclusion-policy/keysets"
    SYNC_EXCLUSION_POLICY_SNAPSHOT = "/api/sync/exclusion-policy/snapshot"
    SYNC_JOURNAL_EVENTS_PREFLIGHT = "/api/sync/journal-events/preflight"
    UPLOAD_CONTENT = "/api/uploads/{operation_id}/content"
    UPLOAD_MULTIPART_SESSIONS = "/api/uploads/multipart-sessions"
    UPLOAD_MULTIPART_SESSION = "/api/uploads/multipart-sessions/{session_id}"
    UPLOAD_MULTIPART_SESSION_PART_URL = (
        "/api/uploads/multipart-sessions/{session_id}/parts/{part_number}/url"
    )
    UPLOAD_MULTIPART_SESSION_COMPLETE = "/api/uploads/multipart-sessions/{session_id}/complete"
    UPLOAD_MULTIPART_SESSION_ABORT = "/api/uploads/multipart-sessions/{session_id}/abort"
    SYNC_SOURCE_LIFECYCLE_EVENTS = "/api/sources/lifecycle-events"
    SYNC_EVENTS = "/api/sync/events"
    SYNC_CURSOR_ACKNOWLEDGEMENTS = "/api/sync/cursor-acknowledgements"
    SYNC_MANIFESTS = "/api/sync/manifests"
    SYNC_MANIFEST_PAGES = "/api/sync/manifests/{manifest_run_id}/pages/{page_number}"
    SYNC_MANIFEST_FINALIZE = "/api/sync/manifests/{manifest_run_id}/finalize"
    SYNC_MANIFEST_ACTIONS = "/api/sync/manifests/{manifest_run_id}/actions"
    SYNC_MANIFEST_COMPLETE = "/api/sync/manifests/{manifest_run_id}/complete"
    SYNC_SOURCE_VERSION_CONTENT = "/api/sources/{source_id}/versions/{source_version_id}/content"
    OPENAPI_DOCUMENT = "/api/openapi.json"
    UNMATCHED = "unmatched"


#: Every authentication-bound route template of the closed session/password,
#: TOTP/recovery, device-authorization, device-token and Admin device route
#: sets (spec 16.1-16.4).
#: Responses on these routes carry the authentication cache-suppression and
#: privacy posture, so the error handlers need the same closed membership the
#: correlation middleware uses.
AUTHENTICATION_ROUTE_TEMPLATES: Final[frozenset[ApiRouteTemplate]] = frozenset(
    {
        ApiRouteTemplate.AUTH_LOGIN,
        ApiRouteTemplate.AUTH_SESSION,
        ApiRouteTemplate.AUTH_LOGOUT,
        ApiRouteTemplate.AUTH_REAUTHENTICATE,
        ApiRouteTemplate.AUTH_PASSWORD,
        ApiRouteTemplate.AUTH_TOTP_VERIFY,
        ApiRouteTemplate.AUTH_TOTP_ENROLLMENTS,
        ApiRouteTemplate.AUTH_TOTP_ENROLLMENT_VERIFY,
        ApiRouteTemplate.AUTH_TOTP_RECOVERY,
        ApiRouteTemplate.AUTH_TOTP_RECOVERY_CODES_REGENERATE,
        ApiRouteTemplate.AUTH_TOTP_DISABLE,
        ApiRouteTemplate.AUTH_DEVICE_AUTHORIZATIONS,
        ApiRouteTemplate.AUTH_DEVICE_AUTHORIZATION_LOOKUP,
        ApiRouteTemplate.AUTH_DEVICE_AUTHORIZATION_APPROVE,
        ApiRouteTemplate.AUTH_DEVICE_AUTHORIZATION_DENY,
        ApiRouteTemplate.AUTH_DEVICE_AUTHORIZATION_POLL,
        ApiRouteTemplate.AUTH_DEVICE_TOKENS_REFRESH,
        ApiRouteTemplate.AUTH_DEVICE_TOKENS_REVOKE_CURRENT,
        ApiRouteTemplate.ADMIN_DEVICES,
        ApiRouteTemplate.ADMIN_DEVICE_REVOKE,
    }
)

#: Immutable view of the template values themselves, for classifying a raw
#: matched route path without retaining it.
AUTHENTICATION_ROUTE_TEMPLATE_VALUES: Final[frozenset[str]] = frozenset(
    template.value for template in AUTHENTICATION_ROUTE_TEMPLATES
)


def is_authentication_route_template(template: ApiRouteTemplate) -> bool:
    """Return whether one route template belongs to the authentication set."""
    return template in AUTHENTICATION_ROUTE_TEMPLATES


#: The closed exclusion-policy route set of spec 16.1/16.2 — the Admin policy
#: control surface behind the Web session contract and the plugin keyset/
#: snapshot reads behind the access Bearer credential.
EXCLUSION_POLICY_ROUTE_TEMPLATES: Final[frozenset[ApiRouteTemplate]] = frozenset(
    {
        ApiRouteTemplate.ADMIN_EXCLUSION_POLICY,
        ApiRouteTemplate.ADMIN_EXCLUSION_POLICY_DRAFT,
        ApiRouteTemplate.ADMIN_EXCLUSION_POLICY_PREVIEWS,
        ApiRouteTemplate.ADMIN_EXCLUSION_POLICY_PREVIEW,
        ApiRouteTemplate.ADMIN_EXCLUSION_POLICY_PUBLICATIONS,
        ApiRouteTemplate.SYNC_EXCLUSION_POLICY_KEYSETS,
        ApiRouteTemplate.SYNC_EXCLUSION_POLICY_SNAPSHOT,
    }
)

#: The closed small-file sync route set of the plugin journal design (spec
#: 10): the journal-event preflight and the operation-bound content stream,
#: both behind the ``obsidian_sync`` access Bearer credential.
SMALL_FILE_SYNC_ROUTE_TEMPLATES: Final[frozenset[ApiRouteTemplate]] = frozenset(
    {
        ApiRouteTemplate.SYNC_JOURNAL_EVENTS_PREFLIGHT,
        ApiRouteTemplate.UPLOAD_CONTENT,
    }
)

#: The closed multipart upload route set of the resumable multipart mobile
#: upload design (Child 7 spec 5): session create-or-resume, safe status, one
#: short-lived part-URL issuance, completion and user cancellation, all behind
#: the ``obsidian_sync`` access Bearer credential with workspace and device
#: derived from the resolved token context — never a request field. The
#: part-URL response is the sole surface a signed URL may appear on, so the
#: whole set carries the strictest cache-suppression posture.
MULTIPART_UPLOAD_ROUTE_TEMPLATES: Final[frozenset[ApiRouteTemplate]] = frozenset(
    {
        ApiRouteTemplate.UPLOAD_MULTIPART_SESSIONS,
        ApiRouteTemplate.UPLOAD_MULTIPART_SESSION,
        ApiRouteTemplate.UPLOAD_MULTIPART_SESSION_PART_URL,
        ApiRouteTemplate.UPLOAD_MULTIPART_SESSION_COMPLETE,
        ApiRouteTemplate.UPLOAD_MULTIPART_SESSION_ABORT,
    }
)

#: The closed source lifecycle route set of the lifecycle API (spec 19.2):
#: the lifecycle-events commit behind the ``obsidian_sync`` access Bearer
#: credential. The route never carries a workspace or device selector; both
#: derive from the resolved token context.
SOURCE_LIFECYCLE_ROUTE_TEMPLATES: Final[frozenset[ApiRouteTemplate]] = frozenset(
    {
        ApiRouteTemplate.SYNC_SOURCE_LIFECYCLE_EVENTS,
    }
)

#: The closed device sync route set of the device cursor and manifest
#: reconciliation design (spec 7): event pull, cursor acknowledgement, the
#: manifest run lifecycle and the verified binary download, all behind the
#: ``obsidian_sync`` access Bearer credential with workspace, device and user
#: derived from the resolved token context — never a request field.
DEVICE_SYNC_ROUTE_TEMPLATES: Final[frozenset[ApiRouteTemplate]] = frozenset(
    {
        ApiRouteTemplate.SYNC_EVENTS,
        ApiRouteTemplate.SYNC_CURSOR_ACKNOWLEDGEMENTS,
        ApiRouteTemplate.SYNC_MANIFESTS,
        ApiRouteTemplate.SYNC_MANIFEST_PAGES,
        ApiRouteTemplate.SYNC_MANIFEST_FINALIZE,
        ApiRouteTemplate.SYNC_MANIFEST_ACTIONS,
        ApiRouteTemplate.SYNC_MANIFEST_COMPLETE,
        ApiRouteTemplate.SYNC_SOURCE_VERSION_CONTENT,
    }
)

#: The closed sync diagnostics admin route set: the read-only rejection
#: evidence surface behind the Web session contract. Its payloads are
#: per-process counters and ring snapshots that must never come from a shared
#: cache.
SYNC_DIAGNOSTICS_ROUTE_TEMPLATES: Final[frozenset[ApiRouteTemplate]] = frozenset(
    {
        ApiRouteTemplate.ADMIN_SYNC_REJECTIONS,
    }
)

#: The closed source lifecycle diagnostics admin route set: the read-only
#: commit-counter and rejection-ring evidence surface behind the Web session
#: contract. Its payloads are per-process counters and ring snapshots that
#: must never come from a shared cache.
SOURCE_LIFECYCLE_DIAGNOSTICS_ROUTE_TEMPLATES: Final[frozenset[ApiRouteTemplate]] = frozenset(
    {
        ApiRouteTemplate.ADMIN_SOURCE_LIFECYCLE_REJECTIONS,
    }
)

#: The closed exclusion-policy diagnostics admin route set (spec 2026-08-24
#: C2): the read-only evaluation-counter, publication-counter and
#: recent-failure-ring evidence surface behind the Web session contract. Its
#: payloads are per-process counters and ring snapshots that must never come
#: from a shared cache.
EXCLUSION_POLICY_DIAGNOSTICS_ROUTE_TEMPLATES: Final[frozenset[ApiRouteTemplate]] = frozenset(
    {
        ApiRouteTemplate.ADMIN_EXCLUSION_POLICY_DIAGNOSTICS,
    }
)

#: Every route whose responses — success, service rejection and dependency
#: failure alike — carry ``Cache-Control: no-store`` (spec 16): the
#: authentication-bound sets plus the exclusion-policy, small-file sync,
#: multipart upload, source lifecycle, device sync and the three diagnostics
#: admin route sets, whose payloads are per-request policy state, signed
#: envelopes, device-derived sync results, verified private bytes, one
#: short-lived presigned URL and per-process rejection evidence that must
#: never come from a shared cache.
NO_STORE_ROUTE_TEMPLATES: Final[frozenset[ApiRouteTemplate]] = (
    AUTHENTICATION_ROUTE_TEMPLATES
    | EXCLUSION_POLICY_ROUTE_TEMPLATES
    | SMALL_FILE_SYNC_ROUTE_TEMPLATES
    | MULTIPART_UPLOAD_ROUTE_TEMPLATES
    | SOURCE_LIFECYCLE_ROUTE_TEMPLATES
    | DEVICE_SYNC_ROUTE_TEMPLATES
    | SYNC_DIAGNOSTICS_ROUTE_TEMPLATES
    | SOURCE_LIFECYCLE_DIAGNOSTICS_ROUTE_TEMPLATES
    | EXCLUSION_POLICY_DIAGNOSTICS_ROUTE_TEMPLATES
)

#: Immutable view of the no-store template values, for classifying a raw
#: matched route path without retaining it.
NO_STORE_ROUTE_TEMPLATE_VALUES: Final[frozenset[str]] = frozenset(
    template.value for template in NO_STORE_ROUTE_TEMPLATES
)


def is_no_store_route_template(template: ApiRouteTemplate) -> bool:
    """Return whether one route template carries the no-store posture."""
    return template in NO_STORE_ROUTE_TEMPLATES
