"""Diagnostic correlation context: request identity, trace binding and ContextVar scoping."""

from __future__ import annotations

import contextlib
import contextvars
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

from personal_os.diagnostics.events import SafeToken
from personal_os.diagnostics.trace_context import TraceContext, resolve_trace_context


@dataclass(frozen=True, slots=True)
class DiagnosticContext:
    """Frozen correlation identity for one request-bound unit of work."""

    request_id: UUID
    client_request_id: UUID | None
    trace: TraceContext
    workflow_id: SafeToken | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticContextResolution:
    """Outcome of diagnostic context creation: the context plus rejection flags."""

    context: DiagnosticContext
    was_client_request_id_rejected: bool
    was_traceparent_replaced: bool


_diagnostic_context_var: contextvars.ContextVar[DiagnosticContext | None] = contextvars.ContextVar[
    DiagnosticContext | None
]("diagnostic_context", default=None)


def _resolve_client_request_id(value: str | None) -> tuple[UUID | None, bool]:
    """Return (canonical UUID, was_rejected) without retaining any rejected text."""
    if value is None:
        return None, False
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None, True
    if str(parsed) != value:
        return None, True
    return parsed, False


def create_diagnostic_context(
    *,
    client_request_id: str | None = None,
    traceparent: str | None = None,
    workflow_id: SafeToken | None = None,
) -> DiagnosticContextResolution:
    """Build a server-owned diagnostic context from client headers and workflow id.

    The server request id is always a freshly generated UUIDv7. A client request id
    is accepted only when it is already in canonical UUID form; otherwise it is
    dropped and the rejection flag is set without storing the raw input. The
    traceparent is resolved via ``resolve_trace_context``.
    """
    client_id, was_client_rejected = _resolve_client_request_id(client_request_id)
    trace_resolution = resolve_trace_context(traceparent)
    return DiagnosticContextResolution(
        context=DiagnosticContext(
            request_id=uuid.uuid7(),
            client_request_id=client_id,
            trace=trace_resolution.context,
            workflow_id=workflow_id,
        ),
        was_client_request_id_rejected=was_client_rejected,
        was_traceparent_replaced=trace_resolution.was_replaced,
    )


@contextlib.contextmanager
def bind_diagnostic_context(context: DiagnosticContext) -> Iterator[None]:
    """Bind a diagnostic context for the duration of the ``with`` block."""
    token = _diagnostic_context_var.set(context)
    try:
        yield
    finally:
        _diagnostic_context_var.reset(token)


def current_diagnostic_context() -> DiagnosticContext | None:
    """Return the diagnostic context bound to the current ContextVar scope."""
    return _diagnostic_context_var.get()


@contextlib.contextmanager
def detached_diagnostic_context() -> Iterator[None]:
    """Temporarily bind no diagnostic context, masking any parent binding."""
    token = _diagnostic_context_var.set(None)
    try:
        yield
    finally:
        _diagnostic_context_var.reset(token)


def copy_diagnostic_context() -> contextvars.Context:
    """Return a snapshot of the current contextvars context without submitting it."""
    return contextvars.copy_context()
