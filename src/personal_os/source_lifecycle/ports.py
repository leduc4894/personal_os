"""Provider-neutral lifecycle persistence and policy ports."""

from __future__ import annotations

from typing import Protocol

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.source_lifecycle.commands import (
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.fingerprint import LifecycleRequestFingerprint


class SourceLifecycleStore(Protocol):
    """Durable replay lookup and atomic lifecycle transition boundary."""

    async def resolve_committed(
        self,
        command: SourceLifecycleCommand,
        request_fingerprint: LifecycleRequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult | None: ...


class SourceLifecyclePolicy(Protocol):
    """Reserved policy seam; implementation is intentionally owned by task 5."""

    async def evaluate(self, command: SourceLifecycleCommand) -> object: ...
