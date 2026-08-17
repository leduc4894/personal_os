"""Policy signing-key rotation chain against the real migrated PostgreSQL.

The module-scoped stack fixture of the sibling conftest provisions the
disposable ``knowledge-ci-*`` project and the migrated schema, so these tests
prove the offline key lifecycle end to end with real Ed25519 keys:
``initialize`` publishes the self-signed keyset revision 1 (current key, one
signature, one audit row), ``stage`` publishes the cross-signed revision 2
(old-current signature plus proof-of-possession from the staged key),
``activate`` publishes the cross-signed revision 3 making the staged key
current while the old key remains trusted, and ``retire`` publishes the
retirement revision signed by the current key alone. Replayed CLI invocations
acknowledge the already-committed transition without appending rows, guards
refuse staging under a non-current signer, activating a key that was never
staged and retiring the current (last trusted) key, and the fail-before-bind
startup proof accepts exactly the current key of the latest canonical keyset.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from api_runtime.database_lifecycle import DatabaseRuntimeLifecycle
from api_runtime.exclusion_policy_commands import (
    create_or_load_policy_signing_key,
    execute_policy_key_activate,
    execute_policy_key_initialize,
    execute_policy_key_retire,
    execute_policy_key_stage,
)
from api_runtime.exclusion_policy_crypto import Ed25519PolicyVerifier
from tests.integration.exclusion_policy.conftest import PolicyMigrationHarness

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.trace_context import SpanId, TraceContext, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.signatures import (
    KEYSET_SIGNING_DOMAIN,
    build_signed_message,
    derive_ed25519_key_id,
)

pytestmark = pytest.mark.local_stack

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)

INITIAL_KEY_FILE_NAME = "policy_signing_initial.pem"
STAGED_KEY_FILE_NAME = "policy_signing_staged.pem"
THIRD_KEY_FILE_NAME = "policy_signing_third.pem"


def _context() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


@pytest.fixture(scope="module")
def policy_secret_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("policy-signing-secrets")


# --- the staged rotation chain --------------------------------------------------


@pytest.mark.asyncio
async def test_startup_proof_refuses_an_uninitialized_workspace(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    lifecycle = DatabaseRuntimeLifecycle(
        settings=policy_migration_harness.stack.settings,
        password=policy_migration_harness.stack.password,
    )
    await lifecycle.start()
    try:
        with pytest.raises(ExclusionPolicyError) as raised:
            await lifecycle.verify_exclusion_policy_signer(
                signing_key_id="ed25519-sha256-" + "A" * 43
            )
        assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED
    finally:
        await lifecycle.stop()


@pytest.mark.asyncio
async def test_initialize_publishes_self_signed_keyset_revision_one(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> None:
    outcome = await execute_policy_key_initialize(
        engine=policy_migration_harness.engine,
        workspace_id=policy_migration_harness.stack.workspace_id,
        key_file_name=INITIAL_KEY_FILE_NAME,
        secret_root=policy_secret_root,
        context=_context(),
    )
    assert outcome.action == "initialized"
    assert outcome.keyset_revision == 1
    assert outcome.is_replay is False

    rows = await policy_migration_harness.fetch_all(
        "SELECT keyset_revision, parent_keyset_revision, payload_sha256,"
        " canonical_payload_bytes FROM knowledge.policy_keysets"
        " WHERE workspace_id = :workspace_id",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert len(rows) == 1
    revision, parent, payload_sha256, payload_bytes = rows[0]
    assert (int(revision), parent) == (1, None)
    assert payload_sha256 == hashlib.sha256(payload_bytes).hexdigest()
    assert b'"keyset_revision":1' in payload_bytes
    assert b'"state":"current"' in payload_bytes
    assert outcome.key_id.encode("ascii") in payload_bytes

    signature_count = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.policy_keyset_signatures s"
        " JOIN knowledge.policy_keysets k ON k.policy_keyset_id = s.policy_keyset_id"
        " WHERE k.workspace_id = :workspace_id",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert signature_count == 1
    audit_count = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.audit_events"
        " WHERE workspace_id = :workspace_id AND action = 'exclusion_policy.key_initialized'",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert audit_count == 1


@pytest.mark.asyncio
async def test_replayed_initialize_acknowledges_without_appending_rows(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> None:
    keyset_count_before = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.policy_keysets WHERE workspace_id = :workspace_id",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    outcome = await execute_policy_key_initialize(
        engine=policy_migration_harness.engine,
        workspace_id=policy_migration_harness.stack.workspace_id,
        key_file_name=INITIAL_KEY_FILE_NAME,
        secret_root=policy_secret_root,
        context=_context(),
    )
    assert outcome.is_replay is True
    assert outcome.keyset_revision == 1
    keyset_count_after = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.policy_keysets WHERE workspace_id = :workspace_id",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert keyset_count_after == keyset_count_before


@pytest.mark.asyncio
async def test_stage_publishes_cross_signed_keyset_revision_two(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> None:
    current_signer = create_or_load_policy_signing_key(policy_secret_root, INITIAL_KEY_FILE_NAME)
    outcome = await execute_policy_key_stage(
        engine=policy_migration_harness.engine,
        workspace_id=policy_migration_harness.stack.workspace_id,
        key_file_name=STAGED_KEY_FILE_NAME,
        secret_root=policy_secret_root,
        signer=current_signer,
        context=_context(),
    )
    assert outcome.action == "staged"
    assert outcome.keyset_revision == 2
    assert outcome.is_replay is False

    payload_bytes = await policy_migration_harness.fetch_scalar(
        "SELECT canonical_payload_bytes FROM knowledge.policy_keysets"
        " WHERE workspace_id = :workspace_id AND keyset_revision = 2",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    staged_signer = create_or_load_policy_signing_key(policy_secret_root, STAGED_KEY_FILE_NAME)
    assert staged_signer.key_id == outcome.key_id
    assert b'"state":"staged"' in payload_bytes
    assert b'"state":"current"' in payload_bytes

    signature_rows = await policy_migration_harness.fetch_all(
        "SELECT k.public_key_bytes, s.signature_bytes FROM knowledge.policy_keyset_signatures s"
        " JOIN knowledge.policy_signing_keys k ON k.signing_key_id = s.signing_key_id"
        " JOIN knowledge.policy_keysets ks ON ks.policy_keyset_id = s.policy_keyset_id"
        " WHERE ks.workspace_id = :workspace_id AND ks.keyset_revision = 2",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert len(signature_rows) == 2
    message = build_signed_message(KEYSET_SIGNING_DOMAIN, payload_bytes)
    verifier = Ed25519PolicyVerifier(
        {derive_ed25519_key_id(bytes(row[0])): bytes(row[0]) for row in signature_rows}
    )
    for row in signature_rows:
        assert verifier.verify(derive_ed25519_key_id(bytes(row[0])), bytes(row[1]), message)
    audit_count = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.audit_events"
        " WHERE workspace_id = :workspace_id AND action = 'exclusion_policy.key_staged'",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert audit_count == 1


@pytest.mark.asyncio
async def test_stage_rejects_a_signer_that_is_not_the_current_key(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> None:
    staged_signer = create_or_load_policy_signing_key(policy_secret_root, STAGED_KEY_FILE_NAME)
    with pytest.raises(ExclusionPolicyError) as raised:
        await execute_policy_key_stage(
            engine=policy_migration_harness.engine,
            workspace_id=policy_migration_harness.stack.workspace_id,
            key_file_name=THIRD_KEY_FILE_NAME,
            secret_root=policy_secret_root,
            signer=staged_signer,
            context=_context(),
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
    assert str(raised.value.safe_details["reason"]) == "signer_not_current"


@pytest.mark.asyncio
async def test_activate_publishes_cross_signed_keyset_revision_three(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> None:
    old_current_signer = create_or_load_policy_signing_key(
        policy_secret_root, INITIAL_KEY_FILE_NAME
    )
    outcome = await execute_policy_key_activate(
        engine=policy_migration_harness.engine,
        workspace_id=policy_migration_harness.stack.workspace_id,
        staged_key_file_name=STAGED_KEY_FILE_NAME,
        secret_root=policy_secret_root,
        signer=old_current_signer,
        context=_context(),
    )
    assert outcome.action == "activated"
    assert outcome.keyset_revision == 3

    staged_signer = create_or_load_policy_signing_key(policy_secret_root, STAGED_KEY_FILE_NAME)
    assert staged_signer.key_id == outcome.key_id
    signature_count = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.policy_keyset_signatures s"
        " JOIN knowledge.policy_keysets k ON k.policy_keyset_id = s.policy_keyset_id"
        " WHERE k.workspace_id = :workspace_id AND k.keyset_revision = 3",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert signature_count == 2
    audit_count = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.audit_events"
        " WHERE workspace_id = :workspace_id AND action = 'exclusion_policy.key_activated'",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert audit_count == 1


@pytest.mark.asyncio
async def test_replayed_activate_acknowledges_without_appending_rows(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> None:
    count_before = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.policy_keysets WHERE workspace_id = :workspace_id",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    old_current_signer = create_or_load_policy_signing_key(
        policy_secret_root, INITIAL_KEY_FILE_NAME
    )
    outcome = await execute_policy_key_activate(
        engine=policy_migration_harness.engine,
        workspace_id=policy_migration_harness.stack.workspace_id,
        staged_key_file_name=STAGED_KEY_FILE_NAME,
        secret_root=policy_secret_root,
        signer=old_current_signer,
        context=_context(),
    )
    assert outcome.is_replay is True
    assert outcome.keyset_revision == 3
    count_after = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.policy_keysets WHERE workspace_id = :workspace_id",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert count_after == count_before


@pytest.mark.asyncio
async def test_activate_refuses_a_key_that_was_never_staged(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> None:
    current_signer = create_or_load_policy_signing_key(policy_secret_root, STAGED_KEY_FILE_NAME)
    with pytest.raises(ExclusionPolicyError) as raised:
        await execute_policy_key_activate(
            engine=policy_migration_harness.engine,
            workspace_id=policy_migration_harness.stack.workspace_id,
            staged_key_file_name=THIRD_KEY_FILE_NAME,
            secret_root=policy_secret_root,
            signer=current_signer,
            context=_context(),
        )
    assert str(raised.value.safe_details["reason"]) == "key_not_staged"


@pytest.mark.asyncio
async def test_retire_publishes_the_retirement_keyset(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> None:
    initial_signer = create_or_load_policy_signing_key(policy_secret_root, INITIAL_KEY_FILE_NAME)
    current_signer = create_or_load_policy_signing_key(policy_secret_root, STAGED_KEY_FILE_NAME)
    outcome = await execute_policy_key_retire(
        engine=policy_migration_harness.engine,
        workspace_id=policy_migration_harness.stack.workspace_id,
        retiring_key_id=initial_signer.key_id,
        signer=current_signer,
        context=_context(),
    )
    assert outcome.action == "retired"
    assert outcome.keyset_revision == 4
    payload_bytes = await policy_migration_harness.fetch_scalar(
        "SELECT canonical_payload_bytes FROM knowledge.policy_keysets"
        " WHERE workspace_id = :workspace_id AND keyset_revision = 4",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert b'"state":"retired"' in payload_bytes
    audit_count = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.audit_events"
        " WHERE workspace_id = :workspace_id AND action = 'exclusion_policy.key_retired'",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert audit_count == 1


@pytest.mark.asyncio
async def test_retire_refuses_the_current_key_as_the_last_trusted_key(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> None:
    current_signer = create_or_load_policy_signing_key(policy_secret_root, STAGED_KEY_FILE_NAME)
    with pytest.raises(ExclusionPolicyError) as raised:
        await execute_policy_key_retire(
            engine=policy_migration_harness.engine,
            workspace_id=policy_migration_harness.stack.workspace_id,
            retiring_key_id=current_signer.key_id,
            signer=current_signer,
            context=_context(),
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
    assert str(raised.value.safe_details["reason"]) == "cannot_retire_current_key"


@pytest.mark.asyncio
async def test_replayed_retire_acknowledges_without_appending_rows(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> None:
    initial_signer = create_or_load_policy_signing_key(policy_secret_root, INITIAL_KEY_FILE_NAME)
    current_signer = create_or_load_policy_signing_key(policy_secret_root, STAGED_KEY_FILE_NAME)
    count_before = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.policy_keysets WHERE workspace_id = :workspace_id",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    outcome = await execute_policy_key_retire(
        engine=policy_migration_harness.engine,
        workspace_id=policy_migration_harness.stack.workspace_id,
        retiring_key_id=initial_signer.key_id,
        signer=current_signer,
        context=_context(),
    )
    assert outcome.is_replay is True
    count_after = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.policy_keysets WHERE workspace_id = :workspace_id",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert count_after == count_before


# --- the persisted chain and the fail-before-bind proof --------------------------


@pytest.mark.asyncio
async def test_persisted_keyset_chain_stays_canonical_and_ordered(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    rows = await policy_migration_harness.fetch_all(
        "SELECT keyset_revision, parent_keyset_revision, payload_sha256,"
        " canonical_payload_bytes FROM knowledge.policy_keysets"
        " WHERE workspace_id = :workspace_id ORDER BY keyset_revision",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert [int(row[0]) for row in rows] == [1, 2, 3, 4]
    for index, (revision, parent, payload_sha256, payload_bytes) in enumerate(rows):
        assert payload_sha256 == hashlib.sha256(payload_bytes).hexdigest()
        expected_parent = None if revision == 1 else int(revision) - 1
        assert parent == expected_parent
        assert int(revision) == index + 1
    audit_actions = await policy_migration_harness.fetch_all(
        "SELECT action FROM knowledge.audit_events"
        " WHERE workspace_id = :workspace_id AND action LIKE 'exclusion_policy.key_%'"
        " ORDER BY action",
        {"workspace_id": policy_migration_harness.stack.workspace_id},
    )
    assert [row[0] for row in audit_actions] == [
        "exclusion_policy.key_activated",
        "exclusion_policy.key_initialized",
        "exclusion_policy.key_retired",
        "exclusion_policy.key_staged",
    ]


@pytest.mark.asyncio
async def test_startup_proof_accepts_only_the_current_key_of_the_latest_keyset(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> None:
    lifecycle = DatabaseRuntimeLifecycle(
        settings=policy_migration_harness.stack.settings,
        password=policy_migration_harness.stack.password,
    )
    await lifecycle.start()
    try:
        current_signer = create_or_load_policy_signing_key(policy_secret_root, STAGED_KEY_FILE_NAME)
        initial_signer = create_or_load_policy_signing_key(
            policy_secret_root, INITIAL_KEY_FILE_NAME
        )
        await lifecycle.verify_exclusion_policy_signer(signing_key_id=current_signer.key_id)
        with pytest.raises(ConfigurationError):
            await lifecycle.verify_exclusion_policy_signer(signing_key_id=initial_signer.key_id)
    finally:
        await lifecycle.stop()
