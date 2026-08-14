"""Repository-wide registry of approved ``KNOWLEDGE_*`` environment names.

Each adopted settings fragment owns a closed set of environment-variable names
that it parses. The union of those sets is the repository-wide registry: every
loader counts an unknown ``KNOWLEDGE_*`` key against the union so a composition
root can combine runtime, database and object-storage configuration without
false failures, while a typo or a plaintext secret name remains terminal
``configuration_unknown_key``.

This module stores names only, never values. It lives in the core package so
every loader (including the concrete R2 adapter) can consult one source of
truth without core importing a provider.
"""

from __future__ import annotations

from typing import Final

#: Runtime fragment: service identity, environment, log level and secret root.
RUNTIME_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "KNOWLEDGE_ENVIRONMENT",
        "KNOWLEDGE_LOG_LEVEL",
        "KNOWLEDGE_SECRET_ROOT",
    }
)

#: Database migration fragment: connection fields and the bounded password file.
DATABASE_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "KNOWLEDGE_ENVIRONMENT",
        "KNOWLEDGE_SECRET_ROOT",
        "KNOWLEDGE_DATABASE_HOST",
        "KNOWLEDGE_DATABASE_PORT",
        "KNOWLEDGE_DATABASE_NAME",
        "KNOWLEDGE_DATABASE_USER",
        "KNOWLEDGE_DATABASE_PASSWORD_FILE",
        "KNOWLEDGE_DATABASE_SSL_MODE",
    }
)

#: Object-storage fragment: the R2 endpoint, bucket, spool root and the two
#: bounded credential filenames resolved beneath ``KNOWLEDGE_SECRET_ROOT``.
#: Plaintext secret names (``KNOWLEDGE_R2_SECRET_ACCESS_KEY``,
#: ``KNOWLEDGE_R2_ACCESS_KEY_ID``) are deliberately absent: a value supplied as
#: an environment variable is a typo-class failure, not an approved setting.
OBJECT_STORAGE_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "KNOWLEDGE_ENVIRONMENT",
        "KNOWLEDGE_SECRET_ROOT",
        "KNOWLEDGE_R2_ENDPOINT",
        "KNOWLEDGE_R2_BUCKET_NAME",
        "KNOWLEDGE_R2_ACCESS_KEY_ID_FILE",
        "KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE",
        "KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT",
    }
)

#: Repository-wide union of every approved ``KNOWLEDGE_*`` name. A loader treats
#: any prefixed name outside this set as terminal ``configuration_unknown_key``.
KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    RUNTIME_ENVIRONMENT_NAMES | DATABASE_ENVIRONMENT_NAMES | OBJECT_STORAGE_ENVIRONMENT_NAMES
)
