from __future__ import annotations

import asyncio
import contextvars
from uuid import UUID

import pytest

from personal_os.diagnostics.context import (
    bind_diagnostic_context,
    copy_diagnostic_context,
    create_diagnostic_context,
    current_diagnostic_context,
    detached_diagnostic_context,
)
from personal_os.diagnostics.events import SafeToken


def test_server_request_id_is_uuid7_and_client_id_is_separate() -> None:
    client_id = "123e4567-e89b-12d3-a456-426614174000"
    resolved = create_diagnostic_context(client_request_id=client_id)
    assert resolved.context.request_id.version == 7
    assert resolved.context.client_request_id == UUID(client_id)
    assert resolved.context.request_id != resolved.context.client_request_id


def test_nested_binding_restores_parent() -> None:
    parent = create_diagnostic_context().context
    child = create_diagnostic_context().context
    with bind_diagnostic_context(parent):
        with bind_diagnostic_context(child):
            assert current_diagnostic_context() is child
        assert current_diagnostic_context() is parent
    assert current_diagnostic_context() is None


def test_concurrent_operations_do_not_leak_context() -> None:
    async def observe() -> UUID:
        context = create_diagnostic_context().context
        with bind_diagnostic_context(context):
            await asyncio.sleep(0)
            assert current_diagnostic_context() is context
            return context.request_id

    async def run() -> tuple[UUID, UUID]:
        first, second = await asyncio.gather(observe(), observe())
        return first, second

    first, second = asyncio.run(run())
    assert first != second


def test_noncanonical_client_request_id_is_rejected_without_storing_raw() -> None:
    noncanonical = "123e4567e89b12d3a456426614174000"
    resolved = create_diagnostic_context(client_request_id=noncanonical)
    assert resolved.was_client_request_id_rejected is True
    assert resolved.context.client_request_id is None
    assert noncanonical not in repr(resolved)
    assert noncanonical not in repr(resolved.context)


def test_nested_binding_restores_parent_when_body_raises() -> None:
    parent = create_diagnostic_context().context
    with bind_diagnostic_context(parent):
        with (
            pytest.raises(RuntimeError),
            bind_diagnostic_context(create_diagnostic_context().context),
        ):
            raise RuntimeError("boom")
        assert current_diagnostic_context() is parent
    assert current_diagnostic_context() is None


def test_detached_context_hides_parent_from_inner_work() -> None:
    parent = create_diagnostic_context().context
    with bind_diagnostic_context(parent):
        assert current_diagnostic_context() is parent
        with detached_diagnostic_context():
            assert current_diagnostic_context() is None
        assert current_diagnostic_context() is parent
    assert current_diagnostic_context() is None


def _capture_request_id() -> UUID:
    context = current_diagnostic_context()
    assert context is not None
    return context.request_id


def test_copied_context_preserves_request_id_when_invoked() -> None:
    context = create_diagnostic_context().context
    with bind_diagnostic_context(context):
        copied = copy_diagnostic_context()
    assert current_diagnostic_context() is None
    captured = copied.run(_capture_request_id)
    assert captured == context.request_id


def test_rejected_client_and_trace_sentinels_appear_in_no_repr() -> None:
    malicious_client = "'; DROP TABLE--"
    zero_trace = "00-00000000000000000000000000000000-00f067aa0ba902b7-01"
    resolved = create_diagnostic_context(
        client_request_id=malicious_client,
        traceparent=zero_trace,
    )
    assert resolved.was_client_request_id_rejected is True
    assert resolved.was_traceparent_replaced is True
    assert resolved.context.client_request_id is None
    assert malicious_client not in repr(resolved)
    assert malicious_client not in repr(resolved.context)
    assert malicious_client not in repr(resolved.context.trace)
    assert zero_trace not in repr(resolved)
    assert zero_trace not in repr(resolved.context)


def test_workflow_id_is_threaded_through_resolution() -> None:
    token = SafeToken.parse("ingest-source-commit")
    resolved = create_diagnostic_context(workflow_id=token)
    assert resolved.context.workflow_id == token
    assert str(resolved.context.workflow_id) == "ingest-source-commit"


def test_copy_diagnostic_context_returns_contextvars_context() -> None:
    copied = copy_diagnostic_context()
    assert isinstance(copied, contextvars.Context)
