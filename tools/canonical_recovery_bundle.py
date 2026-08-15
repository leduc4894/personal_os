"""Bundle-store composition helper for the canonical recovery backup root.

The canonical operations CLI (``tools/canonical_core_operations.py``) resolves
its :class:`~personal_os.recovery.bundle.FilesystemRecoveryBundleStore` through
this single helper so the backup root is always validated before any bundle
path is opened. Nothing else lives here: the filesystem bundle store and the
closed ``validate_backup_root`` contract already implement the boundary.
"""

from __future__ import annotations

from personal_os.recovery.bundle import FilesystemRecoveryBundleStore, validate_backup_root
from personal_os.runtime_configuration.models import CanonicalRecoverySettings

__all__ = ["build_bundle_store"]


def build_bundle_store(settings: CanonicalRecoverySettings) -> FilesystemRecoveryBundleStore:
    """Validate the configured backup root, then return the bundle store."""
    root = validate_backup_root(settings.backup_root)
    return FilesystemRecoveryBundleStore(root)
