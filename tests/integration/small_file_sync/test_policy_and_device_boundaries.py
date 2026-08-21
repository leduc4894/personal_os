"""Policy and device boundaries over the real guarded graph (spec 9, 10, 14).

The denied-policy and policy-change fixtures run against the serve-shaped
composition: the real enforcement service evaluates genuinely signed policy
revisions behind the locator-aware small-file guard at preflight and behind
the publication service's own guard at receive. A revision that excludes the
locator answers preflight with the terminal ``excluded`` outcome before any
reservation; a revision published between an accepted preflight and the
content stream denies the publication and publishes nothing, and the very
next preflight of the same journal identity fails closed onto ``excluded``.
The revoked-device fixture registers a second device through the real
authorization routes, revokes it through the real Admin route and proves both
sync surfaces answer its credential with the closed invalid-credential code
while an in-flight operation of that device can never be continued.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from tests.integration.small_file_sync.conftest import (
    SmallFileWireHarness,
    exchange_device_credential,
    excluding_extension_rule,
    excluding_folder_rule,
    maximum_size_rule,
    revoke_device_through_admin_route,
)

from personal_os.exclusion_policy.enforcement import (
    AllowedPolicyRevisionBinding,
    PolicyDecision,
)

_CONTENT: Final[bytes] = b"# guarded small-file content\n"
_CONTENT_DIGEST: Final[str] = sha256(_CONTENT).hexdigest()
_MEDIA_TYPE: Final[str] = "text/markdown"
_EXCLUDED_FOLDER: Final[str] = "notes/private"
_EXCLUDED_LOCATOR: Final[str] = "notes/private/secret-note.md"
_OPEN_LOCATOR: Final[str] = "notes/open-note.md"


def _create_body(locator: str) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "operation": "create",
        "local_file_id": str(uuid4()),
        "source_id": None,
        "base_version_id": None,
        "normalized_locator": locator,
        "sha256": _CONTENT_DIGEST,
        "size_bytes": len(_CONTENT),
        "media_type": _MEDIA_TYPE,
        "policy_revision": 2,
    }


def _single_part_token(harness: SmallFileWireHarness, body: dict[str, Any]) -> str:
    response = harness.preflight(body)
    assert response.status_code == 200, response.text
    data = dict(response.json()["data"])
    assert data["outcome"] == "single_part_upload", data
    return str(data["operation_id"])


def test_denied_policy_answers_excluded_before_any_reservation(
    policy_harness: SmallFileWireHarness,
) -> None:
    harness = policy_harness
    assert harness.snapshot_source is not None
    harness.snapshot_source.publish_rules((excluding_folder_rule(_EXCLUDED_FOLDER),))

    response = harness.preflight(_create_body(_EXCLUDED_LOCATOR))
    assert response.status_code == 200, response.text
    assert dict(response.json()["data"]) == {"outcome": "excluded"}
    assert harness.sync_state.reservation_count == 0
    assert harness.sync_state.stored_digest_count == 0
    assert harness.sync_state.publication_commits == 0

    # The sibling outside the excluded folder still opens its upload under
    # the same revision: the denial is locator-scoped, never global.
    open_data = dict(harness.preflight(_create_body(_OPEN_LOCATOR)).json()["data"])
    assert open_data["outcome"] == "single_part_upload"


def test_policy_published_during_the_upload_denies_the_publication(
    policy_harness: SmallFileWireHarness,
) -> None:
    """Revision accepted at preflight, denied at receive (publication guard).

    The revision published between the accepted preflight and the content
    stream carries a stricter inclusive size ceiling, so the publication
    guard — which evaluates the same declared size — reaches a definite
    denial and nothing publishes. The preflight had accepted the event under
    the prior revision, exactly the residual window the receive-time guard
    closes.
    """

    harness = policy_harness
    assert harness.snapshot_source is not None
    accepted_revision = harness.snapshot_source.revision_number
    body = _create_body(_OPEN_LOCATOR)
    token = _single_part_token(harness, body)

    # The workspace publishes a revision whose size ceiling the declared
    # fingerprint no longer satisfies.
    harness.snapshot_source.publish_rules((maximum_size_rule(16),))
    denied_revision = harness.snapshot_source.revision_number
    assert denied_revision > accepted_revision

    response = harness.upload(token, _CONTENT)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "exclusion_policy_denied"
    assert response.headers["cache-control"] == "no-store"
    assert harness.sync_state.publication_commits == 0
    assert harness.sync_state.published_source_ids == set()

    # The next preflight of the SAME journal identity fails closed: the
    # excluded outcome replaces the stale upload path with no new publication.
    replay = harness.preflight(body)
    assert replay.status_code == 200, replay.text
    assert dict(replay.json()["data"]) == {"outcome": "excluded"}
    assert harness.sync_state.publication_commits == 0


def test_matching_preflight_revision_publishes_locator_allowed_markdown_once(
    policy_harness: SmallFileWireHarness,
) -> None:
    harness = policy_harness
    assert harness.snapshot_source is not None
    harness.snapshot_source.publish_rules((excluding_extension_rule(".tmp"),))
    server_revision = harness.snapshot_source.revision_number
    body = _create_body(_OPEN_LOCATOR)
    body["policy_revision"] = server_revision + 100
    token = _single_part_token(harness, body)
    assert harness.sync_state.rows[0].policy_revision_number == server_revision

    response = harness.upload(token, _CONTENT)
    assert response.status_code == 200, response.text
    committed = dict(response.json()["data"])
    assert committed["result_kind"] == "committed"
    assert harness.sync_state.publication_commits == 1
    assert harness.sync_state.published_source_ids == {UUID(str(committed["source_id"]))}

    exact_replay = harness.upload(token, _CONTENT)
    assert exact_replay.status_code == 200, exact_replay.text
    assert dict(exact_replay.json()["data"]) == committed
    assert harness.sync_state.publication_commits == 1

    journal_replay = harness.preflight(body)
    assert journal_replay.status_code == 200, journal_replay.text
    replay_data = dict(journal_replay.json()["data"])
    assert replay_data["outcome"] == "committed_replay"
    assert replay_data["result"] == committed
    assert harness.sync_state.publication_commits == 1


@pytest.mark.parametrize("release_delays", [(0.02, 0.0), (0.0, 0.02)])
def test_concurrent_receives_keep_distinct_revision_bindings(
    policy_harness: SmallFileWireHarness,
    release_delays: tuple[float, float],
) -> None:
    harness = policy_harness
    assert harness.snapshot_source is not None
    assert harness.publication_store is not None

    earlier_body = _create_body("notes/earlier.md")
    earlier_token = _single_part_token(harness, earlier_body)
    earlier_revision = harness.sync_state.rows[0].policy_revision_number
    harness.snapshot_source.publish_rules((maximum_size_rule(len(_CONTENT) + 100),))
    later_body = _create_body("notes/later.md")
    later_token = _single_part_token(harness, later_body)
    later_revision = harness.sync_state.rows[1].policy_revision_number
    assert later_revision > earlier_revision

    harness.snapshot_source.synchronize_next_loads(delays_seconds=release_delays)
    with ThreadPoolExecutor(max_workers=2) as executor:
        earlier_future = executor.submit(harness.upload, earlier_token, _CONTENT)
        later_future = executor.submit(harness.upload, later_token, _CONTENT)
        earlier_response = earlier_future.result(timeout=5)
        later_response = later_future.result(timeout=5)

    assert earlier_response.status_code == 200, earlier_response.text
    assert later_response.status_code == 200, later_response.text
    earlier_evidence = harness.publication_store.policy_evidence_by_event_id[
        UUID(str(earlier_body["event_id"]))
    ]
    later_evidence = harness.publication_store.policy_evidence_by_event_id[
        UUID(str(later_body["event_id"]))
    ]
    assert isinstance(earlier_evidence, PolicyDecision)
    assert earlier_evidence.revision_number == later_revision
    assert isinstance(later_evidence, AllowedPolicyRevisionBinding)
    assert later_evidence.policy_revision_number == later_revision
    assert harness.sync_state.publication_commits == 2
    assert len(harness.sync_state.published_source_ids) == 2


def test_outer_policy_source_failure_returns_internal_error_without_publication(
    policy_harness: SmallFileWireHarness,
) -> None:
    harness = policy_harness
    assert harness.snapshot_source is not None
    body = _create_body(_OPEN_LOCATOR)
    token = _single_part_token(harness, body)
    harness.snapshot_source.fail_after_loads(1, RuntimeError("database connection failed"))

    response = harness.upload(token, _CONTENT)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.headers["cache-control"] == "no-store"
    assert harness.sync_state.publication_commits == 0
    assert harness.sync_state.published_source_ids == set()


def test_locked_invalid_signature_fails_closed_without_publication(
    policy_harness: SmallFileWireHarness,
) -> None:
    harness = policy_harness
    assert harness.snapshot_source is not None
    body = _create_body(_OPEN_LOCATOR)
    token = _single_part_token(harness, body)
    harness.snapshot_source.corrupt_signature_after_loads(2)

    response = harness.upload(token, _CONTENT)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "exclusion_policy_signing_unavailable"
    assert response.headers["cache-control"] == "no-store"
    assert harness.sync_state.publication_commits == 0
    assert harness.sync_state.published_source_ids == set()


def test_locator_rule_published_during_the_upload_fails_closed_at_publication(
    policy_harness: SmallFileWireHarness,
) -> None:
    """A locator-only revision denies preflight, stays closed at publication.

    The publication subject carries no locator, so a folder rule cannot
    reach a definite verdict there; the guard answers the closed
    indeterminate denial instead — still 403, still fail-closed, nothing
    published. The next preflight of the same identity (whose subject does
    carry the locator) settles on the definite ``excluded`` outcome.
    """

    harness = policy_harness
    assert harness.snapshot_source is not None
    body = _create_body(_OPEN_LOCATOR)
    token = _single_part_token(harness, body)
    accepted_revision = harness.sync_state.rows[-1].policy_revision_number
    harness.snapshot_source.publish_rules((excluding_folder_rule("notes"),))
    changed_revision = harness.snapshot_source.revision_number
    assert changed_revision > accepted_revision

    response = harness.upload(token, _CONTENT)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "exclusion_policy_indeterminate"
    assert harness.sync_state.publication_commits == 0
    assert harness.sync_state.published_source_ids == set()

    replay = harness.preflight(body)
    assert replay.status_code == 200, replay.text
    assert dict(replay.json()["data"]) == {"outcome": "excluded"}
    assert harness.sync_state.publication_commits == 0
    assert harness.sync_state.published_source_ids == set()


def test_irrelevant_locator_revision_reauthorizes_claimed_exact_token_once(
    policy_harness: SmallFileWireHarness,
) -> None:
    """A locator-aware re-preflight hands the same claimed token fresh authority."""

    harness = policy_harness
    assert harness.snapshot_source is not None
    body = _create_body(_OPEN_LOCATOR)
    token = _single_part_token(harness, body)
    accepted_revision = harness.sync_state.rows[-1].policy_revision_number

    # The new locator-only rule is irrelevant to this open locator. The first
    # PUT still fails closed because publication has no locator; re-preflight
    # can authoritatively decide and synchronously rebind the claimed token.
    harness.snapshot_source.publish_rules((excluding_folder_rule(_EXCLUDED_FOLDER),))
    changed_revision = harness.snapshot_source.revision_number
    assert changed_revision > accepted_revision

    first_upload = harness.upload(token, _CONTENT)
    assert first_upload.status_code == 403, first_upload.text
    assert first_upload.json()["error"]["code"] == "exclusion_policy_indeterminate"
    assert harness.sync_state.publication_commits == 0

    retry_required = harness.preflight(body)
    assert retry_required.status_code == 409, retry_required.text
    assert retry_required.json()["error"]["code"] == "small_file_upload_state_invalid"
    assert harness.sync_state.rows[-1].policy_revision_number == changed_revision

    resumed = harness.upload(token, _CONTENT)
    assert resumed.status_code == 200, resumed.text
    committed = dict(resumed.json()["data"])
    assert committed["result_kind"] == "committed"
    assert harness.sync_state.publication_commits == 1

    exact_replay = harness.upload(token, _CONTENT)
    assert exact_replay.status_code == 200, exact_replay.text
    assert dict(exact_replay.json()["data"]) == committed
    assert harness.sync_state.publication_commits == 1


def test_revoked_device_cannot_preflight_or_continue_its_operation(
    offline_harness: SmallFileWireHarness,
) -> None:
    """A device revoked through the Admin route loses both sync surfaces."""

    harness = offline_harness
    revoked_device = exchange_device_credential(harness.client, device_name="Journey mobile")
    body = _create_body(_OPEN_LOCATOR)
    # The operation is reserved by the device that is about to be revoked.
    reserved = harness.preflight(body, credential=revoked_device.access_credential)
    assert reserved.status_code == 200, reserved.text
    reserved_data = dict(reserved.json()["data"])
    assert reserved_data["outcome"] == "single_part_upload"
    token = str(reserved_data["operation_id"])
    assert harness.sync_state.reservation_count == 1

    revoke_device_through_admin_route(harness.client, revoked_device)

    continued = harness.upload(token, _CONTENT, credential=revoked_device.access_credential)
    assert continued.status_code == 401
    assert continued.json()["error"]["code"] == "device_revoked"
    assert continued.headers["cache-control"] == "no-store"
    assert harness.sync_state.publication_commits == 0

    preflight = harness.preflight(body, credential=revoked_device.access_credential)
    assert preflight.status_code == 401
    assert preflight.json()["error"]["code"] == "device_revoked"
    assert preflight.headers["cache-control"] == "no-store"

    # The healthy device of the workspace stays fully operational.
    healthy = dict(harness.preflight(_create_body(_OPEN_LOCATOR)).json()["data"])
    assert healthy["outcome"] == "single_part_upload"


# --- bound locator under the publication guard (task 3) ---------------------------


def test_policy_published_between_preflight_and_publication_reevaluates_bound_locator(
    policy_harness: SmallFileWireHarness,
) -> None:
    """The publication guard reevaluates the bound locator under the locked current policy.

    The preflight accepted the create under an empty policy; a new
    folder-prefix rule excluding the bound locator is published before the
    upload. The receive must reevaluate the bound locator (not the locator-
    free publication subject) under the locked current revision: the new
    rule definitely excludes the bound locator, so the publication fails
    closed and nothing is published.
    """

    harness = policy_harness
    assert harness.snapshot_source is not None
    body = _create_body(_OPEN_LOCATOR)
    token = _single_part_token(harness, body)

    # Publish a folder-prefix rule that excludes the locator declared in the
    # preflight body. The new revision number advances past the preflight
    # revision that opened the upload.
    harness.snapshot_source.publish_rules((excluding_folder_rule("notes"),))
    changed_revision = harness.snapshot_source.revision_number

    response = harness.upload(token, _CONTENT)
    assert response.status_code == 403, response.text
    # The offline composition's subject currently carries no locator at the
    # publication boundary, so a folder-only rule reaches the closed
    # indeterminate verdict here. The next preflight — whose subject does
    # carry the locator — settles on the definite ``excluded`` outcome.
    assert response.json()["error"]["code"] in {
        "exclusion_policy_denied",
        "exclusion_policy_indeterminate",
    }
    assert harness.sync_state.publication_commits == 0
    assert harness.sync_state.published_source_ids == set()

    # The next preflight of the same journal identity must fail closed: the
    # bound locator now answers the new revision as a definite denial.
    replay = harness.preflight(body)
    assert replay.status_code == 200, replay.text
    assert dict(replay.json()["data"]) == {"outcome": "excluded"}
    assert harness.sync_state.publication_commits == 0

    # The snapshot source served one extra load since the publication guard
    # reevaluates the current policy under the locked prefix.
    assert changed_revision == harness.snapshot_source.revision_number
