"""Provider-neutral ports for the multipart upload orchestration (spec 4-6).

The seam the later tasks build on: the application-facing service protocol
(create-or-resume, one part-URL issuance, completion) plus the provider
identity value objects that stay private to this module — the durable store
and the staging provider adapters exchange them, but they are deliberately
absent from the package's public surface, never rendered outside a redacted
``repr`` and never carried by a plan, status, URL or error. The protocol
imports no FastAPI, SQLAlchemy, R2 SDK or request type; device and workspace
identity arrive only through the credential-derived
:class:`~personal_os.small_file_sync.contracts.SmallFileDeviceContext`, and
the server-owned
:class:`~personal_os.diagnostics.context.DiagnosticContext` travels with
every call for correlation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.multipart_upload.contracts import (
    MultipartCompletionResult,
    MultipartPartUrl,
    MultipartUploadPlan,
    MultipartUploadSessionId,
)
from personal_os.small_file_sync.contracts import (
    SmallFileDeviceContext,
    SmallFilePreflight,
)

#: Bounded length of one provider-assigned opaque identity value.
_MAX_PROVIDER_IDENTITY_LENGTH: Final[int] = 1024


@dataclass(frozen=True, slots=True)
class MultipartProviderUploadId:
    """The provider-assigned upload ID of one exact staging upload.

    Server-private database-sensitive material (spec 4.1): it exists only
    inside the store and staging provider boundary, is never returned to the
    plugin or public API and never renders outside a redacted ``repr``.
    """

    value: str

    def __repr__(self) -> str:
        return f"{type(self).__name__}(value=<redacted>)"

    def __post_init__(self) -> None:
        if not 1 <= len(self.value) <= _MAX_PROVIDER_IDENTITY_LENGTH:
            raise ValueError(
                f"provider upload ID must be 1 to {_MAX_PROVIDER_IDENTITY_LENGTH} characters long"
            )


@dataclass(frozen=True, slots=True)
class MultipartProviderPartETag:
    """The provider-observed ETag of one completed staging part.

    Part completion is proved by the provider (spec 3.6): the server obtains
    this value itself and stores it in PostgreSQL as database-sensitive
    material — the client never echoes one as trusted completion evidence,
    and it never renders outside a redacted ``repr``.
    """

    value: str

    def __repr__(self) -> str:
        return f"{type(self).__name__}(value=<redacted>)"

    def __post_init__(self) -> None:
        if not 1 <= len(self.value) <= _MAX_PROVIDER_IDENTITY_LENGTH:
            raise ValueError(
                f"provider part ETag must be 1 to {_MAX_PROVIDER_IDENTITY_LENGTH} characters long"
            )


class MultipartUploadApplicationService(Protocol):
    """The multipart upload use cases the API routes drive (spec 5/6).

    ``create_or_resume`` returns the single session bound to one frozen
    preflight operation — an exact replay resolves the existing session and
    never mints provider work twice. ``issue_part_url`` rechecks authority,
    policy, state and expiry, then authorizes exactly one numbered part's
    byte range for at most ten minutes. ``complete`` claims the serialized
    completion and returns either the persisted in-progress state or the
    frozen terminal source-event result. Implementations surface every
    closed failure through the typed
    :class:`~personal_os.multipart_upload.errors.MultipartUploadError`.
    """

    async def create_or_resume(
        self,
        *,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartUploadPlan: ...

    async def issue_part_url(
        self,
        *,
        session_id: MultipartUploadSessionId,
        part_number: int,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartPartUrl: ...

    async def complete(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartCompletionResult: ...
