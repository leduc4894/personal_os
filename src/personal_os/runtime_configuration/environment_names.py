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

#: Runtime fragment: service identity, environment, log level, secret root and
#: the optional local diagnostics log directory (blank/unset disables the
#: rotating file sink; the value is a directory path, never a secret).
RUNTIME_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "KNOWLEDGE_ENVIRONMENT",
        "KNOWLEDGE_LOG_LEVEL",
        "KNOWLEDGE_SECRET_ROOT",
        "KNOWLEDGE_DIAGNOSTICS_LOG_DIR",
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

#: Temporal workflow fragment: the target address, namespace and task queue
#: the worker registers against. The durable workflow engine is reachable only
#: through these names; no plaintext credentials belong to this fragment.
TEMPORAL_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "KNOWLEDGE_TEMPORAL_TARGET",
        "KNOWLEDGE_TEMPORAL_NAMESPACE",
        "KNOWLEDGE_TEMPORAL_TASK_QUEUE",
    }
)

#: Canonical recovery fragment: the operator-owned private backup root.
CANONICAL_RECOVERY_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "KNOWLEDGE_ENVIRONMENT",
        "KNOWLEDGE_CANONICAL_BACKUP_ROOT",
    }
)

#: API server fragment: the bind address the API process listens on. Staging
#: and production have no loopback default, so both names must be supplied
#: explicitly there.
API_SERVER_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "KNOWLEDGE_ENVIRONMENT",
        "KNOWLEDGE_API_HOST",
        "KNOWLEDGE_API_PORT",
    }
)

#: Authentication fragment: the non-secret Web/session configuration surface.
#: It names the allowed origin, trusted-proxy CIDRs, plugin version bounds and
#: the versioned key IDs/file names resolved beneath ``KNOWLEDGE_SECRET_ROOT``;
#: key material itself never appears as an environment value, only in exact
#: secret files, so no plaintext key variable belongs to this fragment.
AUTHENTICATION_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "KNOWLEDGE_ENVIRONMENT",
        "KNOWLEDGE_SECRET_ROOT",
        "KNOWLEDGE_AUTH_ALLOWED_ORIGIN",
        "KNOWLEDGE_AUTH_TRUSTED_PROXY_CIDRS",
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE",
        "KNOWLEDGE_AUTH_PREVIOUS_KEYS",
        "KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION",
        "KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION",
    }
)

#: Exclusion-policy signing fragment: the versioned policy signer identity and
#: the exact key file name resolved beneath ``KNOWLEDGE_SECRET_ROOT``. Like the
#: authentication key fragment, key material itself never appears as an
#: environment value — only the derived key ID and the exact file name — so no
#: plaintext private-key variable belongs to this fragment.
EXCLUSION_POLICY_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "KNOWLEDGE_ENVIRONMENT",
        "KNOWLEDGE_SECRET_ROOT",
        "KNOWLEDGE_POLICY_SIGNING_KEY_ID",
        "KNOWLEDGE_POLICY_SIGNING_KEY_FILE",
    }
)

#: Repository-wide union of every approved ``KNOWLEDGE_*`` name. A loader treats
#: any prefixed name outside this set as terminal ``configuration_unknown_key``.
KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    RUNTIME_ENVIRONMENT_NAMES
    | DATABASE_ENVIRONMENT_NAMES
    | OBJECT_STORAGE_ENVIRONMENT_NAMES
    | TEMPORAL_ENVIRONMENT_NAMES
    | CANONICAL_RECOVERY_ENVIRONMENT_NAMES
    | API_SERVER_ENVIRONMENT_NAMES
    | AUTHENTICATION_ENVIRONMENT_NAMES
    | EXCLUSION_POLICY_ENVIRONMENT_NAMES
)
