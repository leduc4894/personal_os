from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import SecretFileError
from personal_os.runtime_configuration.secret_files import read_secret_file


def _skip_without_symlink_support(target_dir: Path) -> None:
    """Skip the calling test when this platform cannot create symlinks.

    Some Windows accounts lack ``SeCreateSymbolicLinkPrivilege``, so
    ``Path.symlink_to`` raises ``OSError [WinError 1314]``. The symlink contract
    is exercised on POSIX CI; on such Windows hosts the test skips cleanly
    rather than erroring. The probe is created inside the per-test ``target_dir``
    because symlink support can be filesystem-specific.
    """
    probe_target = target_dir / "symlink_probe_target"
    probe_target.write_text("probe", encoding="utf-8")
    probe_link = target_dir / "symlink_probe_link"
    try:
        probe_link.symlink_to(probe_target)
    except (OSError, NotImplementedError) as cause:
        pytest.skip(f"symlinks are not supported on this platform: {cause}")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"value", "value"),
        (b"value\n", "value"),
        (b"value\r\n", "value"),
        (b"value\r", "value"),
        (b"value\n\n", "value"),
        (b"value\r\n\r\n", "value"),
        (b"value  \n", "value  "),
    ],
)
def test_reads_utf8_and_strips_every_trailing_line_ending(
    tmp_path: Path, raw: bytes, expected: str
) -> None:
    secret_file = tmp_path / "credential"
    secret_file.write_bytes(raw)
    secret = read_secret_file(secret_file, tmp_path)
    assert secret.get_secret_value() == expected


def test_preserves_interior_whitespace(tmp_path: Path) -> None:
    secret_file = tmp_path / "credential"
    secret_file.write_bytes(b"  value  ")
    assert read_secret_file(secret_file, tmp_path).get_secret_value() == "  value  "


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        (b"", ErrorCode.SECRET_FILE_EMPTY),
        (b"\n", ErrorCode.SECRET_FILE_EMPTY),
        (b"\xef\xbb\xbf", ErrorCode.SECRET_FILE_INVALID_ENCODING),
        (b"value\x00", ErrorCode.SECRET_FILE_INVALID_ENCODING),
        (b"\xff\xfe", ErrorCode.SECRET_FILE_INVALID_ENCODING),
    ],
)
def test_rejects_invalid_content(tmp_path: Path, raw: bytes, expected_code: ErrorCode) -> None:
    secret_file = tmp_path / "credential"
    secret_file.write_bytes(raw)
    with pytest.raises(SecretFileError) as raised:
        read_secret_file(secret_file, tmp_path)
    assert raised.value.error_code is expected_code


def test_rejects_missing_path_as_unavailable(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    with pytest.raises(SecretFileError) as raised:
        read_secret_file(missing, tmp_path)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_MISSING


def test_rejects_directory_as_invalid_type(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(SecretFileError) as raised:
        read_secret_file(directory, tmp_path)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_INVALID_TYPE


def test_rejects_file_exceeding_maximum_size(tmp_path: Path) -> None:
    secret_file = tmp_path / "credential"
    secret_file.write_bytes(b"x" * 65_537)
    with pytest.raises(SecretFileError) as raised:
        read_secret_file(secret_file, tmp_path)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_TOO_LARGE


def test_accepts_file_at_exactly_maximum_size(tmp_path: Path) -> None:
    secret_file = tmp_path / "credential"
    secret_file.write_bytes(b"x" * 65_536)
    assert read_secret_file(secret_file, tmp_path).get_secret_value() == "x" * 65_536


def test_rejects_secrets_backup_prefix_escape_without_disclosing_value(
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    sibling = tmp_path / "secrets-backup"
    sibling.mkdir()
    escaped = sibling / "credential"
    escaped.write_text("DO_NOT_LEAK_SECRET_VALUE", encoding="utf-8")
    secret_file = secret_root / ".." / "secrets-backup" / "credential"
    with pytest.raises(SecretFileError) as raised:
        read_secret_file(secret_file, secret_root)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_OUTSIDE_ROOT
    rendered = f"{raised.value} {raised.value.to_safe_dict()}"
    assert "DO_NOT_LEAK_SECRET_VALUE" not in rendered
    assert str(escaped) not in rendered


def test_rejects_symlink_escape_without_disclosing_paths(tmp_path: Path) -> None:
    _skip_without_symlink_support(tmp_path)
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    outside = tmp_path / "DO_NOT_LEAK_SECRET_PATH"
    outside.write_text("DO_NOT_LEAK_SECRET_VALUE", encoding="utf-8")
    link = secret_root / "credential"
    link.symlink_to(outside)
    with pytest.raises(SecretFileError) as raised:
        read_secret_file(link, secret_root)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_OUTSIDE_ROOT
    rendered = f"{raised.value} {raised.value.to_safe_dict()}"
    assert str(outside) not in rendered
    assert "DO_NOT_LEAK_SECRET_VALUE" not in rendered


def test_accepts_in_root_symlink(tmp_path: Path) -> None:
    _skip_without_symlink_support(tmp_path)
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    target = secret_root / "target"
    target.write_text("value", encoding="utf-8")
    link = secret_root / "credential"
    link.symlink_to(target)
    assert read_secret_file(link, secret_root).get_secret_value() == "value"


def test_post_open_fstat_rejects_non_regular_without_disclosing_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "credential"
    secret_file.write_text("DO_NOT_LEAK_SECRET_VALUE", encoding="utf-8")
    real_fstat = os.fstat

    def fake_fstat(descriptor: int) -> os.stat_result:
        real = real_fstat(descriptor)
        return os.stat_result(
            (
                stat.S_IFDIR | 0o755,
                real.st_ino,
                real.st_dev,
                real.st_nlink,
                real.st_uid,
                real.st_gid,
                real.st_size,
                real.st_atime,
                real.st_mtime,
                real.st_ctime,
            )
        )

    monkeypatch.setattr("os.fstat", fake_fstat)
    with pytest.raises(SecretFileError) as raised:
        read_secret_file(secret_file, tmp_path)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_INVALID_TYPE
    rendered = f"{raised.value} {raised.value.to_safe_dict()}"
    assert "DO_NOT_LEAK_SECRET_VALUE" not in rendered


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_rejects_insecure_write_permissions_on_posix(tmp_path: Path) -> None:
    secret_file = tmp_path / "credential"
    secret_file.write_text("value", encoding="utf-8")
    os.chmod(secret_file, 0o666)
    with pytest.raises(SecretFileError) as raised:
        read_secret_file(secret_file, tmp_path)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_INSECURE_PERMISSIONS


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_accepts_read_only_permissions_on_posix(tmp_path: Path) -> None:
    secret_file = tmp_path / "credential"
    secret_file.write_text("value", encoding="utf-8")
    os.chmod(secret_file, 0o444)
    assert read_secret_file(secret_file, tmp_path).get_secret_value() == "value"
