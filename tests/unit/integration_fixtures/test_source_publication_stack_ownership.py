"""Ownership contract for the source-publication disposable-stack fixture."""

from __future__ import annotations

import json

import pytest
from tests.integration.source_publication import conftest as source_publication_conftest


def test_ready_disposable_stack_is_consumed_without_fixture_lifecycle_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A serve-live-ci-owned stack must never be reset by this fixture."""

    observed_commands: list[tuple[str, ...]] = []

    def stack_main(arguments: list[str]) -> int:
        observed_commands.append(tuple(arguments))
        print(json.dumps({"state": "ready"}))
        return 0

    monkeypatch.setattr(source_publication_conftest, "stack_main", stack_main)
    monkeypatch.setattr(
        source_publication_conftest,
        "_run_stack_steps",
        lambda project_name: pytest.fail(f"unexpected lifecycle mutation for {project_name}"),
    )

    owns_stack = source_publication_conftest._prepare_source_publication_stack(
        "knowledge-ci-fixture-ownership"
    )

    assert owns_stack is False
    assert observed_commands == [("status", "--project-name", "knowledge-ci-fixture-ownership")]


def test_absent_disposable_stack_is_owned_and_initialized_by_the_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The historical self-managed integration invocation remains supported."""

    observed_projects: list[str] = []

    def stack_main(arguments: list[str]) -> int:
        assert arguments == ["status", "--project-name", "knowledge-ci-fixture-ownership"]
        print(json.dumps({"state": "absent"}))
        return 2

    monkeypatch.setattr(source_publication_conftest, "stack_main", stack_main)
    monkeypatch.setattr(
        source_publication_conftest,
        "_run_stack_steps",
        lambda project_name: observed_projects.append(project_name),
    )

    owns_stack = source_publication_conftest._prepare_source_publication_stack(
        "knowledge-ci-fixture-ownership"
    )

    assert owns_stack is True
    assert observed_projects == ["knowledge-ci-fixture-ownership"]
