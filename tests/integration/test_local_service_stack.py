import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))

from tools.local_service_stack import main

pytestmark = pytest.mark.local_stack


def test_disposable_stack_persists_restarts_and_recovers() -> None:
    project_name = os.environ["LOCAL_STACK_TEST_PROJECT"]
    assert project_name.startswith("knowledge-ci-")
    assert (
        main(
            [
                "smoke",
                "--project-name",
                project_name,
                "--confirm-project",
                project_name,
            ]
        )
        == 0
    )
