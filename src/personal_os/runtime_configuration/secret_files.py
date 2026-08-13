"""Bounded, confidential secret-file loading with safe error mapping."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import SecretFileError

DEFAULT_MAXIMUM_SECRET_SIZE_BYTES: Final[int] = 65_536


def _secret_error(code: ErrorCode, reason: str) -> SecretFileError:
    """Build a :class:`SecretFileError` carrying only a safe ``reason`` token.

    ``reason`` is one of the registered safe tokens (``missing``,
    ``outside_root``, ``invalid_type``, ``insecure_permissions``, ``too_large``,
    ``invalid_encoding``, ``empty``). The raw exception string, filesystem path
    and file content are never placed into ``safe_details``.
    """
    return SecretFileError(code, safe_details={"reason": SafeToken.parse(reason)})


def read_secret_file(
    secret_file: Path,
    secret_root: Path,
    maximum_size_bytes: int = DEFAULT_MAXIMUM_SECRET_SIZE_BYTES,
) -> SecretStr:
    """Read a bounded secret value from ``secret_file`` under ``secret_root``.

    The value is loaded under strict resolved-path and file-descriptor checks so
    a symlink escape, non-regular target, oversized file or insecure permission
    is rejected without disclosing the offending path or content. Every
    filesystem and decoder failure is mapped to a registered
    :class:`SecretFileError`.
    """
    if maximum_size_bytes <= 0:
        raise ValueError("maximum_size_bytes must be positive")

    if not secret_file.is_absolute() or not secret_root.is_absolute():
        raise ValueError("secret_file and secret_root must be absolute paths")

    try:
        resolved_root = secret_root.resolve(strict=True)
    except OSError as cause:
        raise _secret_error(ErrorCode.SECRET_FILE_MISSING, "missing") from cause

    try:
        resolved_candidate = secret_file.resolve(strict=True)
    except OSError as cause:
        raise _secret_error(ErrorCode.SECRET_FILE_MISSING, "missing") from cause

    if not resolved_candidate.is_relative_to(resolved_root):
        raise _secret_error(ErrorCode.SECRET_FILE_OUTSIDE_ROOT, "outside_root")

    try:
        candidate_stat = resolved_candidate.stat()
    except FileNotFoundError as cause:
        raise _secret_error(ErrorCode.SECRET_FILE_MISSING, "missing") from cause
    except OSError as cause:
        raise _secret_error(ErrorCode.SECRET_FILE_INVALID_TYPE, "invalid_type") from cause
    if not stat.S_ISREG(candidate_stat.st_mode):
        raise _secret_error(ErrorCode.SECRET_FILE_INVALID_TYPE, "invalid_type")

    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(str(resolved_candidate), open_flags)
    except FileNotFoundError as cause:
        raise _secret_error(ErrorCode.SECRET_FILE_MISSING, "missing") from cause
    except OSError as cause:
        raise _secret_error(ErrorCode.SECRET_FILE_INVALID_TYPE, "invalid_type") from cause

    raw: bytes = b""
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_stat.st_mode):
                raise _secret_error(ErrorCode.SECRET_FILE_INVALID_TYPE, "invalid_type")
            if descriptor_stat.st_size > maximum_size_bytes:
                raise _secret_error(ErrorCode.SECRET_FILE_TOO_LARGE, "too_large")
            insecure_bits = descriptor_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            if os.name == "posix" and insecure_bits:
                raise _secret_error(
                    ErrorCode.SECRET_FILE_INSECURE_PERMISSIONS,
                    "insecure_permissions",
                )
            raw = handle.read(maximum_size_bytes + 1)
    except SecretFileError:
        raise
    except OSError as cause:
        raise _secret_error(ErrorCode.SECRET_FILE_INVALID_TYPE, "invalid_type") from cause

    if len(raw) > maximum_size_bytes:
        raise _secret_error(ErrorCode.SECRET_FILE_TOO_LARGE, "too_large")

    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
        raise _secret_error(ErrorCode.SECRET_FILE_INVALID_ENCODING, "invalid_encoding")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as cause:
        raise _secret_error(
            ErrorCode.SECRET_FILE_INVALID_ENCODING,
            "invalid_encoding",
        ) from cause
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if value == "":
        raise _secret_error(ErrorCode.SECRET_FILE_EMPTY, "empty")
    return SecretStr(value)
