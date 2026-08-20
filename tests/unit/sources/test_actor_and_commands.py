"""Source actor, idempotency-key, title and publication-command contract tests."""

from __future__ import annotations

import dataclasses
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.sources import (
    ActorKind,
    CreateSourceVersion,
    IdempotencyKey,
    SourceActor,
    SourceTitle,
    SourceType,
    UpdateSourceVersion,
)

NIL_UUID = UUID(int=0)
DIGEST = ContentDigest.parse("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
UTC_PLUS_TWO = timezone(timedelta(hours=2))


def _expected_object() -> ExpectedObject:
    return ExpectedObject(
        content_digest=DIGEST,
        size_bytes=0,
        media_type=CanonicalMediaType.parse("text/markdown"),
    )


def _user_actor() -> SourceActor:
    return SourceActor(actor_kind=ActorKind.USER, actor_id=uuid4())


def test_source_type_is_closed_over_seven_members() -> None:
    assert {(member.name, member.value) for member in SourceType} == {
        ("MARKDOWN", "markdown"),
        ("TEXT", "text"),
        ("PDF", "pdf"),
        ("IMAGE", "image"),
        ("AUDIO", "audio"),
        ("WEB", "web"),
        ("YOUTUBE", "youtube"),
    }


def test_actor_kind_is_closed_over_three_members() -> None:
    assert {member.value for member in ActorKind} == {"user", "device", "system"}


def test_user_and_device_actors_require_non_nil_actor_id() -> None:
    for kind in (ActorKind.USER, ActorKind.DEVICE):
        with pytest.raises(ValueError, match="actor_id"):
            SourceActor(actor_kind=kind, actor_id=None)
        with pytest.raises(ValueError, match="actor_id"):
            SourceActor(actor_kind=kind, actor_id=NIL_UUID)


def test_device_actor_requires_non_nil_actor_id() -> None:
    with pytest.raises(ValueError, match="actor_id"):
        SourceActor(actor_kind=ActorKind.DEVICE, actor_id=None)


def test_system_actor_requires_no_actor_id() -> None:
    with pytest.raises(ValueError, match="actor_id"):
        SourceActor(actor_kind=ActorKind.SYSTEM, actor_id=uuid4())
    assert SourceActor(actor_kind=ActorKind.SYSTEM, actor_id=None).actor_id is None


def test_source_actor_is_frozen() -> None:
    actor = _user_actor()
    with pytest.raises(FrozenInstanceError):
        actor.actor_id = uuid4()  # type: ignore[misc]


def test_idempotency_key_accepts_printable_ascii_boundaries() -> None:
    shortest = IdempotencyKey("a")
    longest = IdempotencyKey("!" + "~" * 199)
    assert shortest.value == "a"
    assert len(longest.value) == 200
    assert IdempotencyKey("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~0Az").value.isprintable()


def test_idempotency_key_repr_does_not_leak_value() -> None:
    """The repr must never include the raw idempotency key.

    The key is opaque and workspace-scoped, never logged. If any future task
    formats a command into a log line or traceback, the repr must not echo
    the key. The class name is preserved so debug output stays recognisable.
    """
    key = IdempotencyKey("super-secret-idempotency-key-do-not-leak")
    rendered = f"{key!r} {key}"
    assert "super-secret-idempotency-key-do-not-leak" not in rendered
    assert "IdempotencyKey" in repr(key)


def test_source_title_repr_does_not_leak_value() -> None:
    """The repr must never include the raw title text.

    The title is exact-trimmed user content; the repr must redact it so
    future logging paths cannot leak it through tracebacks or f-strings.
    The class name is preserved so debug output stays recognisable.
    """
    title = SourceTitle("A title the operator does not want in any log line")
    rendered = f"{title!r} {title}"
    assert "A title the operator does not want in any log line" not in rendered
    assert "SourceTitle" in repr(title)


def test_create_command_repr_does_not_leak_idempotency_key_or_title() -> None:
    """The composing command's repr must inherit the leaf redactions.

    The default dataclass repr for ``CreateSourceVersion`` calls
    ``repr(self.idempotency_key)`` and ``repr(self.title)``; both leaves
    redact, so the parent's repr must not leak either value either.
    """
    command = CreateSourceVersion(
        workspace_id=uuid4(),
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey("leak-me-not"),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Untitled secret"),
        actor=_user_actor(),
        expected_object=_expected_object(),
        client_timestamp=None,
    )
    rendered = f"{command!r}"
    assert "leak-me-not" not in rendered
    assert "Untitled secret" not in rendered


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "a" * 201,
        "key one",
        "key\tone",
        "key\none",
        "key\x7f",
        "clé",
        "キー",
    ],
)
def test_rejects_noncanonical_idempotency_key(value: str) -> None:
    with pytest.raises(ValueError, match="idempotency key"):
        IdempotencyKey(value)


def test_source_title_accepts_exact_trimmed_unicode_boundaries() -> None:
    shortest = SourceTitle("a")
    combining = SourceTitle("tiêu đế")
    longest = SourceTitle("é" * 500)
    assert len(longest.value) == 500
    assert shortest.value == "a"
    assert combining.value == "tiêu đế"


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        " leading",
        "trailing ",
        "\ttab",
        "é" * 501,
        "x" * 501,
        "line\nbreak",
        "null\x00char",
        "bell\x07char",
    ],
)
def test_rejects_noncanonical_source_title(value: str) -> None:
    with pytest.raises(ValueError, match="title"):
        SourceTitle(value)


def test_create_source_version_constructs_and_keeps_canonical_fields() -> None:
    workspace_id = uuid4()
    source_id = uuid4()
    event_id = uuid4()
    command = CreateSourceVersion(
        workspace_id=workspace_id,
        source_id=source_id,
        event_id=event_id,
        idempotency_key=IdempotencyKey("create-1"),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Canonical title"),
        actor=_user_actor(),
        expected_object=_expected_object(),
        client_timestamp=None,
    )
    assert command.workspace_id == workspace_id
    assert command.source_type is SourceType.MARKDOWN
    assert command.title.value == "Canonical title"
    assert command.client_timestamp is None
    assert command.expected_object.content_digest == DIGEST


def test_create_exposes_no_base_version_field() -> None:
    fields = {field.name for field in dataclasses.fields(CreateSourceVersion)}
    assert "base_version_id" not in fields
    assert "title" in fields
    assert "source_type" in fields


def test_update_exposes_no_title_or_source_type() -> None:
    fields = {field.name for field in dataclasses.fields(UpdateSourceVersion)}
    assert "title" not in fields
    assert "source_type" not in fields
    assert "base_version_id" in fields


@pytest.mark.parametrize("field_name", ["workspace_id", "source_id", "event_id"])
def test_create_rejects_nil_uuids(field_name: str) -> None:
    kwargs: dict[str, object] = {
        "workspace_id": uuid4(),
        "source_id": uuid4(),
        "event_id": uuid4(),
        "idempotency_key": IdempotencyKey("create-1"),
        "source_type": SourceType.TEXT,
        "title": SourceTitle("Canonical title"),
        "actor": _user_actor(),
        "expected_object": _expected_object(),
        "client_timestamp": None,
    }
    kwargs[field_name] = NIL_UUID
    with pytest.raises(ValueError, match=field_name):
        CreateSourceVersion(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("field_name", ["workspace_id", "source_id", "event_id", "base_version_id"])
def test_update_rejects_nil_uuids(field_name: str) -> None:
    kwargs: dict[str, object] = {
        "workspace_id": uuid4(),
        "source_id": uuid4(),
        "event_id": uuid4(),
        "idempotency_key": IdempotencyKey("update-1"),
        "base_version_id": uuid4(),
        "actor": _user_actor(),
        "expected_object": _expected_object(),
        "client_timestamp": None,
    }
    kwargs[field_name] = NIL_UUID
    with pytest.raises(ValueError, match=field_name):
        UpdateSourceVersion(**kwargs)  # type: ignore[arg-type]


def test_commands_normalize_aware_client_timestamp_to_utc() -> None:
    local_timestamp = datetime(2026, 8, 14, 14, 30, 0, tzinfo=UTC_PLUS_TWO)
    command = CreateSourceVersion(
        workspace_id=uuid4(),
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey("create-1"),
        source_type=SourceType.PDF,
        title=SourceTitle("Canonical title"),
        actor=_user_actor(),
        expected_object=_expected_object(),
        client_timestamp=local_timestamp,
    )
    assert command.client_timestamp == datetime(2026, 8, 14, 12, 30, 0, tzinfo=UTC)
    assert command.client_timestamp is not None
    assert command.client_timestamp.tzinfo is UTC


def test_commands_reject_naive_client_timestamp() -> None:
    with pytest.raises(ValueError, match="client_timestamp"):
        CreateSourceVersion(
            workspace_id=uuid4(),
            source_id=uuid4(),
            event_id=uuid4(),
            idempotency_key=IdempotencyKey("create-1"),
            source_type=SourceType.WEB,
            title=SourceTitle("Canonical title"),
            actor=_user_actor(),
            expected_object=_expected_object(),
            client_timestamp=datetime(2026, 8, 14, 12, 30, 0),
        )
    with pytest.raises(ValueError, match="client_timestamp"):
        UpdateSourceVersion(
            workspace_id=uuid4(),
            source_id=uuid4(),
            event_id=uuid4(),
            idempotency_key=IdempotencyKey("update-1"),
            base_version_id=uuid4(),
            actor=_user_actor(),
            expected_object=_expected_object(),
            client_timestamp=datetime(2026, 8, 14, 12, 30, 0),
        )


def test_commands_are_frozen() -> None:
    command = UpdateSourceVersion(
        workspace_id=uuid4(),
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey("update-1"),
        base_version_id=uuid4(),
        actor=_user_actor(),
        expected_object=_expected_object(),
        client_timestamp=None,
    )
    with pytest.raises(FrozenInstanceError):
        command.base_version_id = uuid4()  # type: ignore[misc]
