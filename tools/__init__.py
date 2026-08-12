"""Marker module for the ``tools`` Python source root.

The workspace declares ``tools`` in ``[tool.mypy].files`` and ``[tool.ruff].src``
and the Poe ``python-type-check`` / ``python-format`` commands target it. Until
concrete infra scripts land here, this empty package keeps those gates resolvable
so ``uv run poe verify`` can run end to end. Replace it with real tooling modules
when they arrive.
"""
