"""W3C Trace Context primitives: strict traceparent parsing and fresh ID generation."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Final

_TRACEPARENT_PATTERN: Final = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_TRACE_ID_PATTERN: Final = re.compile(r"[0-9a-f]{32}")
_SPAN_ID_PATTERN: Final = re.compile(r"[0-9a-f]{16}")
_ZERO_TRACE_ID: Final = "0" * 32
_ZERO_SPAN_ID: Final = "0" * 16


@dataclass(frozen=True, slots=True)
class TraceId:
    """Lowercase nonzero hexadecimal W3C trace identifier (32 chars)."""

    value: str

    def __post_init__(self) -> None:
        if _TRACE_ID_PATTERN.fullmatch(self.value) is None:
            raise ValueError("trace id must be 32 lowercase hex characters")
        if self.value == _ZERO_TRACE_ID:
            raise ValueError("trace id must not be all zero")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SpanId:
    """Lowercase nonzero hexadecimal W3C span identifier (16 chars)."""

    value: str

    def __post_init__(self) -> None:
        if _SPAN_ID_PATTERN.fullmatch(self.value) is None:
            raise ValueError("span id must be 16 lowercase hex characters")
        if self.value == _ZERO_SPAN_ID:
            raise ValueError("span id must not be all zero")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Resolved local trace view: remote parent, fresh local span and sampled flags."""

    trace_id: TraceId
    remote_parent_span_id: SpanId | None
    local_span_id: SpanId
    trace_flags: int


@dataclass(frozen=True, slots=True)
class TraceContextResolution:
    """Outcome of traceparent resolution: the local context plus replacement status."""

    context: TraceContext
    was_replaced: bool


def _generate_trace_id() -> TraceId:
    while True:
        candidate = secrets.token_hex(16)
        if candidate != _ZERO_TRACE_ID:
            return TraceId(candidate)


def _generate_span_id() -> SpanId:
    while True:
        candidate = secrets.token_hex(8)
        if candidate != _ZERO_SPAN_ID:
            return SpanId(candidate)


def _fresh_trace_context() -> TraceContext:
    return TraceContext(
        trace_id=_generate_trace_id(),
        remote_parent_span_id=None,
        local_span_id=_generate_span_id(),
        trace_flags=0,
    )


def resolve_trace_context(value: str | None) -> TraceContextResolution:
    """Resolve a W3C traceparent into a local trace context.

    An absent header yields a fresh context with ``was_replaced=False``. A present
    but malformed, uppercase or all-zero header yields a fresh context with
    ``was_replaced=True``. The rejected input value is never retained. A valid v00
    header keeps the remote trace id and parent span id and generates a new local
    span id, leaving ``was_replaced=False``.
    """
    if value is None:
        return TraceContextResolution(context=_fresh_trace_context(), was_replaced=False)
    match = _TRACEPARENT_PATTERN.fullmatch(value)
    if match is None:
        return TraceContextResolution(context=_fresh_trace_context(), was_replaced=True)
    trace_text, parent_span_text, flags_text = (
        match.group(1),
        match.group(2),
        match.group(3),
    )
    if trace_text == _ZERO_TRACE_ID or parent_span_text == _ZERO_SPAN_ID:
        return TraceContextResolution(context=_fresh_trace_context(), was_replaced=True)
    return TraceContextResolution(
        context=TraceContext(
            trace_id=TraceId(trace_text),
            remote_parent_span_id=SpanId(parent_span_text),
            local_span_id=_generate_span_id(),
            trace_flags=int(flags_text, 16),
        ),
        was_replaced=False,
    )


def format_traceparent(context: TraceContext) -> str:
    """Render a trace context as a W3C traceparent header value."""
    return f"00-{context.trace_id}-{context.local_span_id}-{context.trace_flags:02x}"
