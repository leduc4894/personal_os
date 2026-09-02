"""Canonical PostgreSQL schema revision authority shared by core and adapters.

This module is the single source of truth for the pinned Alembic head every
runtime must observe before serving. It imports nothing and carries only the
closed revision constant, so provider-neutral core packages and SQL adapters
share one authority without bridging architectural boundaries.
"""

from __future__ import annotations

from typing import Final

#: Canonical PostgreSQL schema revision pinned by the acceptance/recovery contract;
#: readiness accepts exactly this head and nothing else. The dismissal-retirement
#: revision ``20260902_02`` (dropping the obsolete nullable
#: ``user_credentials.totp_prompt_dismissed_at`` and rebuilding the reduced
#: timestamp CHECK) stacks on the source-conflict revision ``20260902_01``
#: (the ``source_conflicts`` aggregate and the two conflict sync-event tokens),
#: which stacks on the terminal locator remediation revision ``20260901_03``
#: (the one-shot guarded UPDATE clearing the raw locator on already-terminal
#: small-file rows), which stacks on the device-sync scale-index revision
#: ``20260901_02`` (the workspace-scoped pull composite and the partial
#: tombstone-restore index), which stacks on the grant-poll pacing bucket kind
#: revision ``20260901_01`` (the seventh closed ``grant_poll`` member of the
#: throttle bucket-kind CHECK), which stacks on the revision ``20260829_01``
#: (the append-time submitted policy verdict column), which stacks on the
#: multipart revision ``20260828_04`` (the sealed raw multipart operation
#: token), which stacks on ``20260828_01..03`` (multipart sessions/parts,
#: widened operation size bound, deferred provider identity), which stack on
#: the device sync revision ``20260826_02`` (the catch-up download entry echo
#: amendment), which stacks on ``20260826_01``, the ``20260820_01``
#: source-lifecycle revision, and the small-file, exclusion-policy,
#: authentication and baseline revisions beneath them.
CANONICAL_POSTGRESQL_SCHEMA_REVISION: Final[str] = "20260902_02"
