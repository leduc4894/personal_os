# Reserved: integration tests

**Status:** Reserved directory — no executable tests in this bootstrap.

## Owner

This directory is owned by the Phase One workspace **bootstrap** spec
(`docs/superpowers/specs/phase-one-workspace-bootstrap-design.md`). It exists
to preserve the canonical Python test hierarchy
(`unit`, `contract`, `integration`, `end_to_end`, `golden`, `performance`)
without populating layers that have no behavior to verify yet.

## Future acceptance source

Integration tests are added by a **later spec** that introduces the first
cross-module behavior worth exercising (for example: API route → repository →
database, or MCP tool → domain service). Until that spec lands, this
directory intentionally contains only this README.

## What is forbidden here during bootstrap

- No `test_*.py`, `*.test.ts`, `*.spec.ts` or any other executable test file.
- No `conftest.py` that autocollects placeholder tests.
- No fixture that silently passes with zero assertions.

A later spec is responsible for adding real tests with real assertions; until
then, pytest collects nothing from this directory.
