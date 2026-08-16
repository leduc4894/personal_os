"""Protected web-credential operator commands behind the API process shell.

This module implements the three protected subcommands declared by
:mod:`api_runtime.command` — ``enroll-web-credential``,
``web-credential-status`` and ``reset-web-authentication`` (spec 7.1, 7.2).
It is imported only inside those subcommand handlers, so every shell-only
invocation (``--help``, ``--version``, no arguments, invalid syntax, and each
subcommand's own ``--help`` or argument-validation failure) stays free of
settings, database and crypto imports.

The password boundary is closed: the password is read either through the
non-echoing :func:`getpass.getpass` prompt (twice with confirmation for
enrollment) or from exactly one validated relative file name beneath the
configured secret root through the shared secret-file boundary. It is never
accepted as an argument or environment value, and neither the prompts nor any
error path echo it. Passwords and the emergency-reset confirmation are read
only after full argument validation; the confirmation must equal the
canonical username, and Argon2id hashing happens outside the database
transaction (the one the store commits).

Exit codes follow the process-shell conventions: ``0`` on success, ``2`` for
operator-input validation failures (mirroring argparse syntax failures),
``78`` for typed closed :class:`ApplicationError` rejections and ``70`` for
any unexpected internal failure. stdout carries only the closed result line —
the enrollment flag with its credential revision, or the reset's closed
counts — never a hash, a password or any secret material.
"""

from __future__ import annotations

import asyncio
import getpass
import re
import sys
from argparse import Namespace
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.identity.contracts import IDENTITY_KEY_PATTERN
from personal_os.runtime_configuration.secret_files import read_secret_file

if TYPE_CHECKING:
    from postgresql_source_store.authentication_credentials import CredentialStore
    from postgresql_source_store.settings import DatabaseRuntimeSettings

#: Exit codes mirroring the shared shell and runtime-check conventions.
_EXIT_SUCCESS: Final[int] = 0
_EXIT_INPUT_INVALID: Final[int] = 2
_EXIT_APPLICATION_REJECTED: Final[int] = 78
_EXIT_INTERNAL: Final[int] = 70

#: Password file names are forward-slash-joined segments that each start with
#: an alphanumeric character, exactly like the authentication key file names,
#: so ``..`` segments, absolute paths and backslash escapes have no valid
#: spelling in this grammar.
_PASSWORD_FILE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"
)
_MAXIMUM_PASSWORD_FILE_NAME_LENGTH: Final[int] = 128


class CredentialCommandInputError(Exception):
    """Closed operator-input rejection carrying one fixed safe reason.

    The rejected value never travels with the error, so ``str`` and ``repr``
    can only ever expose the fixed reason text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- the closed input boundary --------------------------------------------------------


def validate_command_username(username: str) -> str:
    """Screen one username against the canonical identity grammar."""
    if IDENTITY_KEY_PATTERN.fullmatch(username) is None:
        raise CredentialCommandInputError("username does not match the canonical grammar")
    return username


def validate_password_file_name(file_name: str) -> str:
    """Screen one password file name against the closed relative grammar."""
    if (
        len(file_name) > _MAXIMUM_PASSWORD_FILE_NAME_LENGTH
        or _PASSWORD_FILE_NAME_PATTERN.fullmatch(file_name) is None
    ):
        raise CredentialCommandInputError(
            "password file name must be a relative name beneath the secret root"
        )
    return file_name


def read_password_from_file_name(secret_root: Path, file_name: str) -> str:
    """Read the password through the shared secret-file boundary."""
    validate_password_file_name(file_name)
    return read_secret_file(secret_root / file_name, secret_root).get_secret_value()


def read_password_interactively(*, should_confirm: bool) -> str:
    """Read the password through the non-echoing prompt, optionally confirmed.

    Enrollment reads the password twice and refuses a mismatch; the reset
    reads it once because the typed username confirmation is its guard.
    """
    password = getpass.getpass("password: ")
    if should_confirm:
        confirmed = getpass.getpass("confirm password: ")
        if confirmed != password:
            raise CredentialCommandInputError("password confirmation did not match")
    return password


def read_emergency_reset_confirmation(username: str) -> str:
    """Read the typed confirmation and require the canonical username.

    The confirmation repeats the canonical username — not secret material,
    unlike the password — so it is read as one echoed line: the typed guard
    works at a real terminal and through piped automation input alike.
    """
    confirmation = input("type the username to confirm the emergency reset: ")
    if confirmation != username:
        raise CredentialCommandInputError(
            "reset confirmation did not match the canonical username"
        )
    return confirmation


def _validated_optional_password_file_name(file_name: str | None) -> str | None:
    if file_name is None:
        return None
    return validate_password_file_name(file_name)


def _resolve_new_password(
    *, password_file_name: str | None, secret_root: Path, should_confirm: bool
) -> str:
    if password_file_name is not None:
        return read_password_from_file_name(secret_root, password_file_name)
    return read_password_interactively(should_confirm=should_confirm)


# --- the protected subcommand handlers --------------------------------------------------


def run_enroll_web_credential(arguments: Namespace) -> int:
    """Enroll the initial Web credential of one canonical username (spec 7.1)."""

    def operation() -> int:
        username = validate_command_username(arguments.username)
        password_file_name = _validated_optional_password_file_name(
            arguments.password_file_name
        )
        return _enroll_web_credential(username, password_file_name)

    return _run_protected_command(operation)


def run_web_credential_status(arguments: Namespace) -> int:
    """Report only the enrollment flag and credential revision (spec 7.1)."""

    def operation() -> int:
        username = validate_command_username(arguments.username)
        return _report_web_credential_status(username)

    return _run_protected_command(operation)


def run_reset_web_authentication(arguments: Namespace) -> int:
    """Emergency-reset every authentication surface (spec 7.2)."""

    def operation() -> int:
        username = validate_command_username(arguments.username)
        password_file_name = _validated_optional_password_file_name(
            arguments.password_file_name
        )
        return _reset_web_authentication(username, password_file_name)

    return _run_protected_command(operation)


def _run_protected_command(operation: Callable[[], int]) -> int:
    """Map one protected operation onto the closed exit-code contract."""
    try:
        return operation()
    except CredentialCommandInputError as error:
        print(f"personal-api: {error.reason}", file=sys.stderr)
        return _EXIT_INPUT_INVALID
    except ApplicationError as error:
        print(
            f"personal-api: {error.error_code.value}: {error.safe_message}",
            file=sys.stderr,
        )
        return _EXIT_APPLICATION_REJECTED
    except Exception:
        print("personal-api: internal_error", file=sys.stderr)
        return _EXIT_INTERNAL


def _enroll_web_credential(username: str, password_file_name: str | None) -> int:
    from api_runtime.authentication_crypto import Argon2PasswordHasher
    from personal_os.authentication.passwords import (
        load_common_password_blocklist,
        validate_new_password,
    )
    from personal_os.diagnostics.context import create_diagnostic_context
    from postgresql_source_store.authentication_credentials import (
        EnrollWebCredentialCommand,
    )
    from postgresql_source_store.settings import load_database_runtime_settings

    database_settings = load_database_runtime_settings()
    password = _resolve_new_password(
        password_file_name=password_file_name,
        secret_root=database_settings.secret_root,
        should_confirm=True,
    )
    validate_new_password(password, load_common_password_blocklist())
    password_hash = Argon2PasswordHasher().hash_password(password)
    enrolled = _run_credential_store_operation(
        database_settings,
        lambda store: store.enroll_web_credential(
            EnrollWebCredentialCommand(
                username=username,
                password_hash=password_hash,
                database_now=datetime.now(UTC),
                diagnostic_context=create_diagnostic_context().context,
            )
        ),
    )
    print(f"enrolled=true credential_revision={enrolled.credential_revision}")
    return _EXIT_SUCCESS


def _report_web_credential_status(username: str) -> int:
    from postgresql_source_store.settings import load_database_runtime_settings

    database_settings = load_database_runtime_settings()
    status = _run_credential_store_operation(
        database_settings,
        lambda store: store.resolve_web_credential_status(username=username),
    )
    is_enrolled = status.credential_revision is not None
    revision = status.credential_revision if is_enrolled else "none"
    print(f"enrolled={str(is_enrolled).lower()} credential_revision={revision}")
    return _EXIT_SUCCESS


def _reset_web_authentication(username: str, password_file_name: str | None) -> int:
    from api_runtime.authentication_crypto import Argon2PasswordHasher
    from personal_os.authentication.passwords import (
        load_common_password_blocklist,
        validate_new_password,
    )
    from personal_os.diagnostics.context import create_diagnostic_context
    from postgresql_source_store.authentication_credentials import (
        ResetWebAuthenticationCommand,
    )
    from postgresql_source_store.settings import load_database_runtime_settings

    database_settings = load_database_runtime_settings()
    new_password = _resolve_new_password(
        password_file_name=password_file_name,
        secret_root=database_settings.secret_root,
        should_confirm=False,
    )
    read_emergency_reset_confirmation(username)
    validate_new_password(new_password, load_common_password_blocklist())
    password_hash = Argon2PasswordHasher().hash_password(new_password)
    reset = _run_credential_store_operation(
        database_settings,
        lambda store: store.reset_web_authentication(
            ResetWebAuthenticationCommand(
                username=username,
                new_password_hash=password_hash,
                database_now=datetime.now(UTC),
                diagnostic_context=create_diagnostic_context().context,
            )
        ),
    )
    print(
        f"reset=true credential_revision={reset.credential_revision} "
        f"revoked_web_sessions={reset.revoked_web_session_count} "
        f"revoked_devices={reset.revoked_device_count} "
        f"revoked_token_families={reset.revoked_token_family_count} "
        f"revoked_device_tokens={reset.revoked_device_token_count} "
        f"replaced_totp_credentials={reset.replaced_totp_credential_count} "
        f"disabled_recovery_codes={reset.disabled_recovery_code_count} "
        f"denied_grants={reset.denied_grant_count}"
    )
    return _EXIT_SUCCESS


def _run_credential_store_operation[ResultT](
    database_settings: DatabaseRuntimeSettings,
    operation: Callable[[CredentialStore], Coroutine[Any, Any, ResultT]],
) -> ResultT:
    """Run one store operation on a short-lived engine, then dispose it."""
    from postgresql_source_store.authentication_credentials import CredentialStore
    from postgresql_source_store.engine import (
        create_source_store_engine,
        dispose_source_store_engine,
    )
    from postgresql_source_store.settings import read_database_runtime_password

    password = read_database_runtime_password(database_settings)
    engine = create_source_store_engine(database_settings, password)
    try:
        return _run_async(operation(CredentialStore(engine)))
    finally:
        _run_async(dispose_source_store_engine(engine))


def _run_async[ResultT](coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
    """Drive one coroutine on a selector event loop.

    The psycopg async driver needs a selector loop: the Windows default
    proactor loop cannot host it, and the selector loop is already the default
    on the Linux deployment hosts.
    """
    return asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(coroutine)
