"""Provider-neutral port for the atomic identity bootstrap store."""

from __future__ import annotations

from typing import Protocol

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.identity.contracts import BootstrapIdentityCommand, BootstrapIdentityResult


class IdentityBootstrapStore(Protocol):
    """One atomic bootstrap or exact-replay read (design spec 4.4, 5.3, 5.4).

    ``bootstrap`` either creates the canonical user/workspace/device identity
    rows in one transaction or returns the previously committed result for an
    exact replay of the same command; it exposes no driver row, SQL statement
    or provider payload, and raises :class:`IdentityBootstrapError` for the
    closed identity code set only.
    """

    async def bootstrap(
        self, command: BootstrapIdentityCommand, diagnostic_context: DiagnosticContext
    ) -> BootstrapIdentityResult: ...
