from __future__ import annotations

import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from personal_os.package_metadata import distribution_version


def test_distribution_version_comes_from_installed_metadata() -> None:
    assert distribution_version() == version("knowledge-core") == "0.1.0"


def test_import_succeeds_outside_repository(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import personal_os; print(personal_os.__name__)"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "personal_os"
