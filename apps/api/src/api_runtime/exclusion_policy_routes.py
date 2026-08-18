"""Exclusion-policy Admin and plugin endpoints (spec 16.1/16.2).

The seven endpoints are created per composed runtime: each closure binds the
exclusion-policy services, the session dependencies of the composed web
authentication runtime and the device-token service, so the application
factory only registers semantic operation ids and response models. The Admin
surface answers only behind the exact-origin session/CSRF contract and
derives workspace and actor from the resolved session — publication
additionally proves the recent re-authentication window through the session
service's own clock. The plugin surface accepts exactly the ``obsidian_sync``
access Bearer credential — session cookies, refresh and polling credentials
close with the registered invalid-credential code — and derives workspace
and device from the resolved token context. Wire models convert to domain
values at the boundary through the shared normalization gate; no response
ever carries a database or provider object, a subject fingerprint, a
rejected operand or key material. Every response carries the canonical
envelope and ``Cache-Control: no-store``; the keyset page keeps the spec
13.3 bound of 16 ordered envelopes, and the snapshot answers conditional
GETs with the quoted payload SHA-256 — ``304`` only after the caller
authenticated.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Final
from uuid import UUID

from fastapi import Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials

from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.authentication_dependencies import (
    ACCESS_BEARER_SCHEME,
    AuthenticatedWebRequest,
    create_session_route_dependencies,
    extract_bearer_credential,
)
from api_runtime.exclusion_policy_composition import ExclusionPolicyRuntime
from api_runtime.exclusion_policy_models import (
    ExclusionPolicyStatusData,
    PolicyDraftData,
    PolicyDraftReplaceRequest,
    PolicyKeysetPageData,
    PolicyPreviewData,
    PolicyPublicationData,
    PolicyPublicationRequest,
    PolicyReconciliationSummaryData,
    SignedPolicySnapshotData,
    policy_draft_data,
    policy_keyset_page_data,
    policy_preview_data,
    policy_publication_data,
    signed_snapshot_data,
    to_domain_rule,
)
from personal_os.api_contracts import ApiRouteTemplate, success_envelope
from personal_os.authentication.contracts import AuthenticatedDeviceContext, DeviceScope
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import is_recently_authenticated
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.ports import PolicyActor, PolicyActorKind
from personal_os.exclusion_policy.previews import (
    PREVIEW_RESULT_PAGE_MAXIMUM,
    PreviewImpactClass,
    PreviewResultCursor,
)
from personal_os.exclusion_policy.publication import PublishPolicyCommand
from personal_os.sources.commands import IdempotencyKey

#: Response headers every policy response carries (spec 16.2).
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}

#: The dedicated opaque idempotency header of the publication route; the
#: closed grammar is the printable non-whitespace ASCII of the sources
#: idempotency-key contract (1-200 characters).
PUBLISH_IDEMPOTENCY_HEADER_NAME: Final[str] = "X-Idempotency-Key"
_PUBLISH_IDEMPOTENCY_PATTERN: Final[str] = r"^[!-~]{1,200}$"

#: Closed safe reason tokens of the route-level input rejections.
_IDEMPOTENCY_KEY_INVALID: Final[SafeToken] = SafeToken.parse("idempotency_key_invalid")
_PUBLICATION_BINDING_INVALID: Final[SafeToken] = SafeToken.parse("publication_binding_invalid")
_PREVIEW_CURSOR_INVALID: Final[SafeToken] = SafeToken.parse("preview_cursor_invalid")


@dataclass(frozen=True, slots=True)
class ExclusionPolicyRouteEndpoints:
    """The seven endpoint callables of the closed policy route set."""

    get_policy_status: Callable[..., Awaitable[JSONResponse]]
    replace_draft: Callable[..., Awaitable[JSONResponse]]
    create_preview: Callable[..., Awaitable[JSONResponse]]
    get_preview: Callable[..., Awaitable[JSONResponse]]
    publish: Callable[..., Awaitable[JSONResponse]]
    list_keysets: Callable[..., Awaitable[JSONResponse]]
    get_snapshot: Callable[..., Awaitable[Response]]


def snapshot_etag(payload_sha256: str) -> str:
    """Render the snapshot entity tag: the quoted payload SHA-256."""

    return f'"{payload_sha256}"'


def if_none_match_satisfied(header_value: str, etag: str) -> bool:
    """Return whether one ``If-None-Match`` header satisfies the entity tag.

    The closed comparison accepts the exact quoted entity tag, its unquoted
    digest spelling and the any-representation ``*`` marker; weak-validator
    prefixes and list separators follow RFC 9110 without any cache lookup —
    the value is compared against the freshly loaded persisted digest only.
    """

    for candidate in header_value.split(","):
        trimmed = candidate.strip()
        if trimmed == "*":
            return True
        if trimmed.startswith("W/"):
            trimmed = trimmed[2:].strip()
        if trimmed == etag or trimmed == etag.strip('"'):
            return True
    return False


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""

    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("exclusion policy routes require a bound request correlation context")
    return context


def _request_id() -> UUID:
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("exclusion policy routes require a bound request correlation context")
    return context.request_id


def _input_invalid(reason: SafeToken) -> ExclusionPolicyError:
    return ExclusionPolicyError(
        ErrorCode.EXCLUSION_POLICY_INPUT_INVALID, safe_details={"reason": reason}
    )


def _user_actor(authentication: AuthenticatedWebRequest) -> PolicyActor:
    return PolicyActor(PolicyActorKind.USER, user_id=authentication.context.user_id)


def create_exclusion_policy_route_endpoints(
    *,
    web_authentication: WebAuthenticationRuntime,
    exclusion_policy: ExclusionPolicyRuntime,
) -> ExclusionPolicyRouteEndpoints:
    """Build the seven policy endpoints over the two composed runtimes."""

    dependencies = create_session_route_dependencies(web_authentication)
    session_service = web_authentication.session_service

    def _success_json(
        data: (
            ExclusionPolicyStatusData
            | PolicyDraftData
            | PolicyPreviewData
            | PolicyPublicationData
            | PolicyKeysetPageData
            | SignedPolicySnapshotData
        ),
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        envelope = success_envelope(request_id=_request_id(), data=data)
        # ``exclude_unset`` keeps the one-named-operand rule grammar exact —
        # only the populated operand member of each rule renders — while every
        # other field of these renderer-constructed models is passed
        # explicitly, so no meaningful member is dropped.
        return JSONResponse(
            content=envelope.model_dump(mode="json", exclude_unset=True),
            status_code=status_code,
            headers={**_NO_STORE_HEADERS, **(headers or {})},
        )

    async def require_policy_read_access(
        request: Request,
        authorization: HTTPAuthorizationCredentials | None = Depends(  # noqa: B008
            ACCESS_BEARER_SCHEME
        ),
    ) -> AuthenticatedDeviceContext:
        """Resolve the access Bearer credential and require the sync scope.

        The dedicated access scheme of spec 16 is the only authority these
        routes accept: cookies and every other credential are never read, so
        presenting them changes nothing. The resolved context carries the
        workspace and device identity — never a request input.
        """
        del authorization  # the closed registry answers bad presentations
        credential = extract_bearer_credential(request)
        token = await web_authentication.device_token_service.authenticate_access(
            access_credential=credential
        )
        if token.context.scope is not DeviceScope.OBSIDIAN_SYNC:
            raise AuthenticationError(ErrorCode.AUTHORIZATION_SCOPE_DENIED)
        return token.context

    async def require_csrf_protected_recent_request(
        request: Request,
    ) -> AuthenticatedWebRequest:
        """The CSRF triple check plus the recent re-authentication window."""

        authentication = await dependencies.require_csrf_protected_request(request)
        database_now = await session_service.database_now()
        if not is_recently_authenticated(
            authentication.session,
            database_now=database_now,
            policy=session_service.session_policy,
        ):
            raise AuthenticationError(ErrorCode.RECENT_AUTHENTICATION_REQUIRED)
        return authentication

    async def get_policy_status(
        request: Request,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_session_request
        ),
    ) -> JSONResponse:
        """Return revision metadata, the exact draft and reconciliation."""
        request.scope["route_template"] = ApiRouteTemplate.ADMIN_EXCLUSION_POLICY
        context = _bound_diagnostic_context()
        workspace_id = authentication.context.workspace_id
        status = await exclusion_policy.queries.get_policy_status(workspace_id, context)
        summary = await exclusion_policy.queries.get_reconciliation_summary(workspace_id, context)
        return _success_json(
            ExclusionPolicyStatusData(
                active_policy_revision_id=status.active_policy_revision_id,
                active_revision_number=status.active_revision_number,
                draft=policy_draft_data(status.draft),
                reconciliation=(
                    None
                    if summary is None
                    else PolicyReconciliationSummaryData(
                        policy_revision_id=summary.policy_revision_id,
                        state=summary.state,
                        updated_at=summary.updated_at,
                    )
                ),
            )
        )

    async def replace_draft(
        request: Request,
        draft_request: PolicyDraftReplaceRequest,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_request
        ),
    ) -> JSONResponse:
        """Validate and atomically replace the complete desired rule list."""
        request.scope["route_template"] = ApiRouteTemplate.ADMIN_EXCLUSION_POLICY_DRAFT
        context = _bound_diagnostic_context()
        rules = tuple(to_domain_rule(rule, index) for index, rule in enumerate(draft_request.rules))
        draft = await exclusion_policy.drafts.load_draft(
            authentication.context.workspace_id, context
        )
        replaced = await exclusion_policy.drafts.replace_draft_rules(
            draft.draft_id,
            draft_request.expected_draft_version,
            rules,
            _user_actor(authentication),
            context,
        )
        return _success_json(policy_draft_data(replaced))

    async def create_preview(
        request: Request,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_request
        ),
    ) -> JSONResponse:
        """Bind one asynchronous preview to the workspace's current draft."""
        request.scope["route_template"] = ApiRouteTemplate.ADMIN_EXCLUSION_POLICY_PREVIEWS
        record = await exclusion_policy.previews.request_preview(
            authentication.context.workspace_id,
            _user_actor(authentication),
            _bound_diagnostic_context(),
        )
        return _success_json(policy_preview_data(record), status_code=202)

    async def get_preview(
        request: Request,
        policy_preview_id: UUID,
        cursor_impact_class: str | None = None,
        cursor_source_id: UUID | None = None,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_session_request
        ),
    ) -> JSONResponse:
        """Answer 202 while pending/running and 200 once ready (spec 16.1)."""
        request.scope["route_template"] = ApiRouteTemplate.ADMIN_EXCLUSION_POLICY_PREVIEW
        del authentication
        context = _bound_diagnostic_context()
        if (cursor_impact_class is None) != (cursor_source_id is None):
            raise _input_invalid(_PREVIEW_CURSOR_INVALID)
        cursor: PreviewResultCursor | None = None
        if cursor_impact_class is not None and cursor_source_id is not None:
            try:
                cursor = PreviewResultCursor(
                    impact_class=PreviewImpactClass(cursor_impact_class),
                    source_id=cursor_source_id,
                )
            except ValueError as cause:
                raise _input_invalid(_PREVIEW_CURSOR_INVALID) from cause
        record = await exclusion_policy.previews.get_preview(policy_preview_id, context)
        if record.status.value in ("failed", "expired", "consumed"):
            if record.status.value == "failed":
                raise ExclusionPolicyError(
                    ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED,
                    safe_details={
                        "reason": SafeToken.parse(str(record.safe_error_code or "preview_missing"))
                    },
                )
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED)
        if record.status.value != "ready":
            return _success_json(policy_preview_data(record), status_code=202)
        page = await exclusion_policy.previews.list_preview_results(
            policy_preview_id, context, cursor, PREVIEW_RESULT_PAGE_MAXIMUM
        )
        return _success_json(policy_preview_data(record, page))

    async def publish(
        request: Request,
        publication_request: PolicyPublicationRequest,
        idempotency_key: Annotated[
            str,
            Header(
                alias=PUBLISH_IDEMPOTENCY_HEADER_NAME,
                pattern=_PUBLISH_IDEMPOTENCY_PATTERN,
            ),
        ],
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            require_csrf_protected_recent_request
        ),
    ) -> JSONResponse:
        """Validate, replay-resolve and atomically publish (spec 11/16.1)."""
        request.scope["route_template"] = ApiRouteTemplate.ADMIN_EXCLUSION_POLICY_PUBLICATIONS
        try:
            key = IdempotencyKey(idempotency_key)
        except ValueError as cause:
            raise _input_invalid(_IDEMPOTENCY_KEY_INVALID) from cause
        try:
            command = PublishPolicyCommand(
                workspace_id=authentication.context.workspace_id,
                actor=_user_actor(authentication),
                policy_preview_id=publication_request.policy_preview_id,
                policy_draft_id=publication_request.policy_draft_id,
                expected_draft_version=publication_request.expected_draft_version,
                expected_draft_sha256=publication_request.expected_draft_sha256,
                preview_impact_digest=publication_request.preview_impact_digest,
                expected_active_policy_revision_id=(
                    publication_request.expected_active_policy_revision_id
                ),
                expected_active_revision_number=(
                    publication_request.expected_active_revision_number
                ),
                idempotency_key=key,
                confirmation=publication_request.confirmation,
            )
        except ValueError as cause:
            raise _input_invalid(_PUBLICATION_BINDING_INVALID) from cause
        result = await exclusion_policy.publication.publish(command, _bound_diagnostic_context())
        return _success_json(
            policy_publication_data(result),
            status_code=200 if result.is_replay else 201,
        )

    async def list_keysets(
        request: Request,
        after_keyset_revision: Annotated[int, Query(ge=0)] = 0,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_policy_read_access
        ),
    ) -> JSONResponse:
        """Return the next bounded ordered keyset chain page (spec 13.3)."""
        request.scope["route_template"] = ApiRouteTemplate.SYNC_EXCLUSION_POLICY_KEYSETS
        page = await exclusion_policy.queries.list_keyset_page(
            device.workspace_id, after_keyset_revision, _bound_diagnostic_context()
        )
        if not page.keysets and after_keyset_revision == 0:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
        return _success_json(policy_keyset_page_data(page.keysets, has_more=page.has_more))

    async def get_snapshot(
        request: Request,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_policy_read_access
        ),
    ) -> Response:
        """Serve the active signed envelope with conditional GET (spec 12/16.2)."""
        request.scope["route_template"] = ApiRouteTemplate.SYNC_EXCLUSION_POLICY_SNAPSHOT
        snapshot = await exclusion_policy.queries.load_active_snapshot(
            device.workspace_id, _bound_diagnostic_context()
        )
        if snapshot is None:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
        etag = snapshot_etag(snapshot.payload_sha256)
        presented = request.headers.get("if-none-match")
        if presented is not None and if_none_match_satisfied(presented, etag):
            return Response(status_code=304, headers={**_NO_STORE_HEADERS, "etag": etag})
        return _success_json(signed_snapshot_data(snapshot), headers={"etag": etag})

    return ExclusionPolicyRouteEndpoints(
        get_policy_status=get_policy_status,
        replace_draft=replace_draft,
        create_preview=create_preview,
        get_preview=get_preview,
        publish=publish,
        list_keysets=list_keysets,
        get_snapshot=get_snapshot,
    )
