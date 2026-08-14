"""Concrete R2 content-addressable object-storage adapter public surface.

The provider package implements the core ``personal_os.object_storage``
contracts over Cloudflare R2. It imports core contracts freely; the core package
never imports this package. Only composition roots consume these exports: the
store for verified content-addressable reads (and later writes), and the
settings loader to build the frozen configuration snapshot from the bounded
environment mapping and secret files.
"""

from r2_object_storage.adapter import R2S3ObjectStore
from r2_object_storage.settings import (
    LoadedR2Credentials,
    ObjectStorageSettings,
    load_object_storage_settings,
)

__all__ = [
    "LoadedR2Credentials",
    "ObjectStorageSettings",
    "R2S3ObjectStore",
    "load_object_storage_settings",
]
