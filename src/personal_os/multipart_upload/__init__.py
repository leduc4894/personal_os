"""Public multipart upload domain contracts (Child 7 spec 4-7).

Framework-neutral geometry, state machine, safe results, the typed error
bound to the closed registry and the application service port for the
resumable multipart staging transfer. The package imports no FastAPI,
SQLAlchemy, R2 SDK or request type; orchestration lands with the multipart
service in a later task. Provider identity value objects stay private to
:mod:`personal_os.multipart_upload.ports` and are deliberately absent from
this surface.
"""

from personal_os.multipart_upload.contracts import (
    MAX_MULTIPART_PART_COUNT,
    MULTIPART_PART_SIZE_BYTES,
    MULTIPART_PART_URL_LIFETIME,
    MULTIPART_SESSION_LIFETIME,
    MULTIPART_SESSION_TRANSITIONS,
    MultipartCompletionResult,
    MultipartPartGeometry,
    MultipartPartRange,
    MultipartPartUrl,
    MultipartSessionState,
    MultipartSessionStatus,
    MultipartUploadPlan,
    MultipartUploadSessionId,
    compute_multipart_session_expiry,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.ports import MultipartUploadApplicationService

__all__ = [
    "MAX_MULTIPART_PART_COUNT",
    "MULTIPART_PART_SIZE_BYTES",
    "MULTIPART_PART_URL_LIFETIME",
    "MULTIPART_SESSION_LIFETIME",
    "MULTIPART_SESSION_TRANSITIONS",
    "MultipartCompletionResult",
    "MultipartPartGeometry",
    "MultipartPartRange",
    "MultipartPartUrl",
    "MultipartSessionState",
    "MultipartSessionStatus",
    "MultipartUploadApplicationService",
    "MultipartUploadError",
    "MultipartUploadPlan",
    "MultipartUploadSessionId",
    "compute_multipart_session_expiry",
]
