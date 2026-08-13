"""Temporal worker composition-root binding for the shared runtime check."""

from personal_os.diagnostics.runtime_check import run_runtime_check
from personal_os.runtime_configuration.models import ServiceName


def run() -> int:
    return run_runtime_check(ServiceName.WORKER)
