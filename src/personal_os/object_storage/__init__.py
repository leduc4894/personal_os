"""Public canonical object-storage contracts.

Provider-neutral value objects, the verified-object reader protocol and the
:class:`CanonicalObjectStore` port. The concrete R2 adapter imports these names;
this module imports no infrastructure SDK.
"""

from personal_os.object_storage.contracts import (
    CanonicalObjectStore,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReader,
    VerifiedObjectReceipt,
)
from personal_os.object_storage.keys import (
    CanonicalMediaType,
    CanonicalObjectKey,
    ContentDigest,
    derive_canonical_object_key,
)

__all__ = [
    "CanonicalMediaType",
    "CanonicalObjectKey",
    "CanonicalObjectStore",
    "ContentDigest",
    "ExpectedObject",
    "VerificationMethod",
    "VerifiedObjectReader",
    "VerifiedObjectReceipt",
    "derive_canonical_object_key",
]
