"""Typed multipart upload errors bound to the closed registry (spec 7).

``MultipartUploadError`` binds this domain to its closed ``multipart_*``
registry block. Staging keys, provider upload IDs, provider ETags,
presigned URLs, digests and raw payload bytes never enter the typed error:
the registry message and code are the only text rendered, and no code in
this block accepts a safe detail field — the readable closed reason travels
through the structured diagnostics and plugin trail surfaces instead. That
contract is enforced by
:class:`personal_os.error_contracts.exceptions.ApplicationError`.
"""

from __future__ import annotations

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError


class MultipartUploadError(ApplicationError):
    """Multipart upload failures across state, integrity, policy and dependency.

    The closed code set covers an unknown, expired or state-invalid session,
    an invalid part request, a rejected presigned part URL, provider-observed
    state inconsistent with the session, a concurrent completion already in
    progress, a staging integrity failure, a policy denial, an unfinished
    exact cleanup, locally changed content and a typed dependency outage.
    Input, state, integrity and policy codes are terminal for the triggering
    request — only the part-URL rejection, the in-progress completion, the
    cleanup obligation and the dependency outage retry with bounded backoff.
    """

    allowed_codes = frozenset(
        {
            ErrorCode.MULTIPART_SESSION_NOT_FOUND,
            ErrorCode.MULTIPART_SESSION_EXPIRED,
            ErrorCode.MULTIPART_SESSION_STATE_INVALID,
            ErrorCode.MULTIPART_PART_INVALID,
            ErrorCode.MULTIPART_PART_URL_REJECTED,
            ErrorCode.MULTIPART_PROVIDER_STATE_INVALID,
            ErrorCode.MULTIPART_COMPLETION_IN_PROGRESS,
            ErrorCode.MULTIPART_INTEGRITY_FAILED,
            ErrorCode.MULTIPART_POLICY_DENIED,
            ErrorCode.MULTIPART_CLEANUP_FAILED,
            ErrorCode.MULTIPART_LOCAL_CONTENT_CHANGED,
            ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE,
        }
    )
