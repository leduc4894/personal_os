"""Public device sync domain contracts (spec 5.1, 6, 7 and 12).

Framework-neutral cursor, event, manifest and content values, the closed
vocabularies, the typed error bound to the closed central registry, the
low-cardinality metric contracts and the store ports. The package imports no
FastAPI, SQLAlchemy, database driver, R2 SDK or Obsidian type; operation
orchestration lands with the service module.
"""

from personal_os.device_sync.contracts import (
    MANIFEST_RUN_LIFETIME,
    MAX_MANIFEST_PAGE_ENTRIES,
    MAX_MANIFEST_RUN_ENTRIES,
    MAX_PULL_EVENTS,
    AppendManifestPageCommand,
    CompleteManifestCommand,
    DeviceContentDescriptor,
    DeviceCursorReceipt,
    DeviceEventPage,
    DeviceEventType,
    DeviceSyncContext,
    DeviceSyncEvent,
    FinalizeManifestCommand,
    ManifestAction,
    ManifestActionKind,
    ManifestActionPage,
    ManifestActionReason,
    ManifestActionsQuery,
    ManifestEntry,
    ManifestPageReceipt,
    ManifestRunReceipt,
    ManifestRunState,
    NormalizedLocator,
    SourceFingerprint,
    StartManifestCommand,
    compute_manifest_run_expiry,
)
from personal_os.device_sync.errors import (
    CENTRAL_ERROR_CODE_BY_DEVICE_CODE,
    DeviceSyncError,
    DeviceSyncErrorCode,
)
from personal_os.device_sync.metrics import (
    DEVICE_SYNC_METRIC_CONTRACTS,
    DeviceSyncMetrics,
    DeviceSyncOperation,
    DeviceSyncOutcome,
    InMemoryDeviceSyncMetrics,
)
from personal_os.device_sync.ports import (
    DeviceEventStore,
    DeviceManifestStore,
)

__all__ = [
    "CENTRAL_ERROR_CODE_BY_DEVICE_CODE",
    "DEVICE_SYNC_METRIC_CONTRACTS",
    "MANIFEST_RUN_LIFETIME",
    "MAX_MANIFEST_PAGE_ENTRIES",
    "MAX_MANIFEST_RUN_ENTRIES",
    "MAX_PULL_EVENTS",
    "AppendManifestPageCommand",
    "CompleteManifestCommand",
    "DeviceContentDescriptor",
    "DeviceCursorReceipt",
    "DeviceEventPage",
    "DeviceEventStore",
    "DeviceEventType",
    "DeviceEventType",
    "DeviceManifestStore",
    "DeviceSyncContext",
    "DeviceSyncError",
    "DeviceSyncErrorCode",
    "DeviceSyncEvent",
    "DeviceSyncMetrics",
    "DeviceSyncOperation",
    "DeviceSyncOutcome",
    "FinalizeManifestCommand",
    "InMemoryDeviceSyncMetrics",
    "ManifestAction",
    "ManifestActionKind",
    "ManifestActionPage",
    "ManifestActionReason",
    "ManifestActionsQuery",
    "ManifestEntry",
    "ManifestPageReceipt",
    "ManifestRunReceipt",
    "ManifestRunState",
    "NormalizedLocator",
    "SourceFingerprint",
    "StartManifestCommand",
    "compute_manifest_run_expiry",
]
