"""Typed application error contracts: registry, error codes and typed exceptions."""

from personal_os.diagnostics.events import SafeToken  # noqa: F401

# Pre-load the diagnostics events module before any submodule import so the
# registry exceptions can import safe-scalar types without a partially
# initialized diagnostics package (same guard as postgresql_source_store).
