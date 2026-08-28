"""Exact-cleanup execution contracts of the expiry scheduler (spec 6.4).

The durable claim/run state transitions themselves live in the session
store (``claim_cleanup_batch`` strikes the 24-hour deadline, leases a
bounded batch of obligations by rotating the lease token, and
``record_cleanup_result`` lands the lease-fenced outcome with its closed
reason and exact bounded next retry) and in the orchestration service's
``run_exact_cleanup`` batch executor. This module owns the remaining piece
the Temporal boundary needs: the deterministic workflow input, continuation
and identity of one cleanup sweep, and the pure bounded-drain rules the
workflow loop follows. Every value here is opaque by construction — the
contract tag, one opaque batch token and opaque counts — so no staging key,
provider upload ID, ETag, URL or reason text can ever cross into a workflow
history; the exact private resource identities stay behind the store and
provider boundary the executor alone drives.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

#: The closed input contract tag of the cleanup workflow.
MULTIPART_CLEANUP_CONTRACT: Final[str] = "multipart_cleanup/v1"

#: The deterministic workflow identity prefix: one opaque batch token per
#: dispatched sweep, so a re-driven start with the same token converges on
#: the same execution while no session, key or provider detail ever rides
#: the identity.
MULTIPART_CLEANUP_WORKFLOW_ID_PREFIX: Final[str] = "multipart_cleanup"

#: The bounded claim size one batch activity asks the executor to claim.
#: The value respects the durable store's hard one-batch ceiling
#: (``MULTIPART_CLEANUP_BATCH_MAXIMUM``) without importing it: the domain
#: rule pins the same bound the store enforces.
MULTIPART_CLEANUP_BATCH_LIMIT: Final[int] = 100

#: The per-run bound on batch executions before the workflow continues as
#: new with its cumulative counters, so one history never grows unboundedly
#: while draining an arbitrarily deep backlog.
MULTIPART_CLEANUP_CONTINUE_AS_NEW_BATCHES: Final[int] = 20


@dataclass(frozen=True, slots=True)
class MultipartCleanupBatchInput:
    """The closed deterministic workflow input of one cleanup sweep.

    Carries only the contract tag and the opaque batch token: the token is a
    fresh opaque UUID minted by the dispatcher, never derived from any
    session, staging key or provider identity, and the claimed rows
    themselves are resolved inside the activity through the store's
    skip-locked lease — so the input cannot leak what it never knew.
    """

    contract: str
    batch_token: UUID


@dataclass(frozen=True, slots=True)
class MultipartCleanupCounters:
    """The closed cumulative counters one workflow run folds per batch."""

    cleaned_count: int = 0
    failed_count: int = 0


@dataclass(frozen=True, slots=True)
class MultipartCleanupContinuation:
    """The closed continue-as-new payload: the input plus the run counters."""

    contract: str
    batch_token: UUID
    counters: MultipartCleanupCounters


class MultipartCleanupExecutionOutcome(StrEnum):
    """The closed workflow outcomes of one cleanup sweep."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"


def multipart_cleanup_workflow_id(batch_token: UUID) -> str:
    """Build the deterministic workflow identity of one cleanup sweep."""

    return f"{MULTIPART_CLEANUP_WORKFLOW_ID_PREFIX}/{batch_token}"


def accumulate_cleanup_counters(
    counters: MultipartCleanupCounters, *, cleaned_count: int, failed_count: int
) -> MultipartCleanupCounters:
    """Fold one committed batch's opaque counts into the run totals."""

    if cleaned_count < 0 or failed_count < 0:
        raise ValueError("batch counts must not be negative")
    return MultipartCleanupCounters(
        cleaned_count=counters.cleaned_count + cleaned_count,
        failed_count=counters.failed_count + failed_count,
    )


def is_drain_complete(*, cleaned_count: int, failed_count: int) -> bool:
    """Report whether one batch claimed nothing and the drain is complete.

    A batch that cleaned or failed at least one row leaves work or a
    durably-backoff obligation behind, so the sweep keeps claiming; a batch
    that claimed nothing found no due row and the sweep ends. A failed row
    cannot spin the loop: the store pushes its exact next retry into the
    future, so the following batch no longer sees it as due.
    """

    if cleaned_count < 0 or failed_count < 0:
        raise ValueError("batch counts must not be negative")
    return cleaned_count == 0 and failed_count == 0


def should_continue_as_new(*, run_batch_count: int) -> bool:
    """The pure continue-as-new rule: the pinned batch bound per run."""

    if run_batch_count < 0:
        raise ValueError("run_batch_count must not be negative")
    return run_batch_count >= MULTIPART_CLEANUP_CONTINUE_AS_NEW_BATCHES


__all__ = [
    "MULTIPART_CLEANUP_BATCH_LIMIT",
    "MULTIPART_CLEANUP_CONTINUE_AS_NEW_BATCHES",
    "MULTIPART_CLEANUP_CONTRACT",
    "MULTIPART_CLEANUP_WORKFLOW_ID_PREFIX",
    "MultipartCleanupBatchInput",
    "MultipartCleanupContinuation",
    "MultipartCleanupCounters",
    "MultipartCleanupExecutionOutcome",
    "accumulate_cleanup_counters",
    "is_drain_complete",
    "multipart_cleanup_workflow_id",
    "should_continue_as_new",
]
