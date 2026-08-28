"""Public small-file sync domain contracts (spec 10 and 12).

Framework-neutral preflight and upload-operation values, the closed outcome
vocabularies, the exact-replay terminal result, the typed error bound to the
closed registry, the durable operation-store port with the injectable aware
UTC clock, and the low-cardinality metric contracts. The package imports no
FastAPI, SQLAlchemy, R2 or request type; orchestration lands with the sync
service in a later task.
"""

from personal_os.small_file_sync.contracts import (
    MAX_SINGLE_PART_FILE_SIZE_BYTES,
    MAX_UPLOAD_FILE_SIZE_BYTES,
    TERMINAL_PREFLIGHT_OUTCOMES,
    NormalizedLocator,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFilePreflightOutcome,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
    SmallFileUploadOperation,
    UploadOperationToken,
)
from personal_os.small_file_sync.errors import (
    PREFLIGHT_INVALID_REASONS,
    SmallFileSyncError,
)
from personal_os.small_file_sync.metrics import (
    SMALL_FILE_METRIC_CONTRACTS,
    InMemorySmallFileSyncMetrics,
    SmallFileMetricOutcome,
    SmallFileRejectionReason,
    SmallFileSyncMetrics,
)
from personal_os.small_file_sync.ports import (
    AwareUtcClock,
    SmallFileUploadOperationStore,
)

__all__ = [
    "MAX_SINGLE_PART_FILE_SIZE_BYTES",
    "MAX_UPLOAD_FILE_SIZE_BYTES",
    "PREFLIGHT_INVALID_REASONS",
    "SMALL_FILE_METRIC_CONTRACTS",
    "TERMINAL_PREFLIGHT_OUTCOMES",
    "AwareUtcClock",
    "InMemorySmallFileSyncMetrics",
    "NormalizedLocator",
    "SmallFileDeviceContext",
    "SmallFileIdempotencyKey",
    "SmallFileMetricOutcome",
    "SmallFileOperation",
    "SmallFilePreflight",
    "SmallFilePreflightOutcome",
    "SmallFileRejectionReason",
    "SmallFileSyncError",
    "SmallFileSyncMetrics",
    "SmallFileTerminalResult",
    "SmallFileTerminalResultKind",
    "SmallFileUploadOperation",
    "SmallFileUploadOperationStore",
    "UploadOperationToken",
]
