"""Generate the committed common-password SHA-256 blocklist artifact.

Downloads the pinned SecLists release file to an OS temporary file, verifies
it contains exactly the expected count of well-formed non-empty duplicate-free
lines, computes one lowercase SHA-256 digest per lowercased line (case
variants of one value intentionally collapse onto a single digest), sorts the
digests bytewise and writes only two artifacts into
``src/personal_os/authentication/data/``: the sorted digest file and a
provenance JSON recording the release, raw source URL, computed source
SHA-256, generated timestamp and generator version. The temporary raw list is
deleted in every exit path and its content is never printed or committed.

Pinned source (decision record 2026-08-16): SecLists release ``2025.2`` is
the last release containing the exact mandated file
``Passwords/Common-Credentials/10-million-password-list-top-10000.txt``;
release ``2026.1`` removed it (the successor file is reordered and contains
an empty line). Regeneration stays provenance-reproducible against this pin.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from urllib.error import URLError
from urllib.request import Request, urlopen

GENERATOR_VERSION: Final[str] = "1"
BLOCKLIST_VERSION: Final[str] = "common-password-sha256-v1"
SOURCE_RELEASE: Final[str] = "2025.2"
SOURCE_PATH: Final[str] = "Passwords/Common-Credentials/10-million-password-list-top-10000.txt"
SOURCE_URL: Final[str] = (
    f"https://raw.githubusercontent.com/danielmiessler/SecLists/{SOURCE_RELEASE}/{SOURCE_PATH}"
)
EXPECTED_SOURCE_LINE_COUNT: Final[int] = 10_000
DOWNLOAD_TIMEOUT_SECONDS: Final[float] = 60.0
DOWNLOAD_ATTEMPT_MAXIMUM: Final[int] = 3
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR: Final[Path] = REPO_ROOT / "src" / "personal_os" / "authentication" / "data"


class BlocklistGenerationError(RuntimeError):
    """Raised when the pinned source fails any verification step."""


def _download_source_bytes() -> bytes:
    """Download the pinned source into a temporary file and read it back."""
    last_failure: Exception | None = None
    for _attempt in range(DOWNLOAD_ATTEMPT_MAXIMUM):
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="seclists-source-",
                suffix=".txt",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                request = Request(SOURCE_URL, headers={"User-Agent": "blocklist-generator"})
                with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                    temporary_file.write(response.read())
            return temporary_path.read_bytes()
        except (URLError, OSError) as failure:
            last_failure = failure
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    raise BlocklistGenerationError(
        f"download failed after {DOWNLOAD_ATTEMPT_MAXIMUM} attempts: {last_failure!r}"
    )


def _validated_source_lines(raw_bytes: bytes) -> list[str]:
    """Decode and verify the exact, non-empty, well-formed source lines."""
    try:
        source_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as cause:
        raise BlocklistGenerationError("source is not valid UTF-8") from cause
    lines = source_text.splitlines()
    if len(lines) != EXPECTED_SOURCE_LINE_COUNT:
        raise BlocklistGenerationError(
            f"expected exactly {EXPECTED_SOURCE_LINE_COUNT} source lines, read {len(lines)}"
        )
    for line in lines:
        if not line:
            raise BlocklistGenerationError("source contains an empty line")
        if line != line.strip():
            raise BlocklistGenerationError("source line carries surrounding whitespace")
        if any(unicodedata.category(character) == "Cc" for character in line):
            raise BlocklistGenerationError("source line carries control characters")
    if len(set(lines)) != len(lines):
        raise BlocklistGenerationError("source contains duplicate lines")
    return lines


def _digest_lines(source_lines: list[str]) -> tuple[list[str], int]:
    """Compute sorted lowercase digests, collapsing case-variant duplicates.

    Digests are computed over lowercased lines so case variants of one common
    value share a single digest; the raw source is verified duplicate-free
    beforehand, so every collapse is a case variant, never a corruption.
    Returns the bytewise-sorted digest list and the count of collapsed lines.
    """
    digests = {hashlib.sha256(line.lower().encode("utf-8")).hexdigest() for line in source_lines}
    collapsed_line_count = len(source_lines) - len(digests)
    return sorted(digests), collapsed_line_count


def _write_artifacts(
    digests: list[str],
    source_line_count: int,
    collapsed_line_count: int,
    source_sha256: str,
    output_dir: Path,
) -> None:
    """Write only the digest file and provenance JSON; never the raw list."""
    output_dir.mkdir(parents=True, exist_ok=True)
    digest_file = output_dir / f"{BLOCKLIST_VERSION}.txt"
    provenance_file = output_dir / f"{BLOCKLIST_VERSION}.provenance.json"
    digest_file.write_text("\n".join(digests) + "\n", encoding="ascii", newline="\n")
    provenance = {
        "blocklist_version": BLOCKLIST_VERSION,
        "source_release": SOURCE_RELEASE,
        "source_url": SOURCE_URL,
        "source_sha256": source_sha256,
        "source_line_count": source_line_count,
        "lowercased_duplicate_line_count": collapsed_line_count,
        "digest_count": len(digests),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "generator_version": GENERATOR_VERSION,
    }
    provenance_file.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
        newline="\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory receiving the digest and provenance artifacts",
    )
    arguments = parser.parse_args(argv)
    raw_bytes = _download_source_bytes()
    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    source_lines = _validated_source_lines(raw_bytes)
    digests, collapsed_line_count = _digest_lines(source_lines)
    _write_artifacts(
        digests,
        len(source_lines),
        collapsed_line_count,
        source_sha256,
        arguments.output_dir,
    )
    print(
        f"generated {BLOCKLIST_VERSION}: "
        f"release={SOURCE_RELEASE} source_lines={len(source_lines)} "
        f"digests={len(digests)} collapsed={collapsed_line_count} "
        f"source_sha256={source_sha256}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BlocklistGenerationError as error:
        print(f"blocklist generation failed: {error}", file=sys.stderr)
        sys.exit(1)
