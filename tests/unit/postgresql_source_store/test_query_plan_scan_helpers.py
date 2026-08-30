"""Unit pins for the query-plan acceptance scan helpers.

The integration module ``tests.integration.source_publication.test_query_plans``
derives the approved index set from the baseline migration source and flags
sequential scans over populated relations. Both helpers are pure functions of
their input, so their contracts are pinned here without the disposable stack:
the seq-scan matcher must flag the parallel variants (PostgreSQL reports
``Parallel Seq Scan`` as its own node type, not ``Seq Scan``) while never
flagging index access, and the migration scanner must fail closed on any
index or constraint name it cannot statically resolve instead of silently
dropping it from the approved set.
"""

from __future__ import annotations

import pytest
from tests.integration.source_publication.test_query_plans import (
    _approved_index_names_from_source,
    _sequential_scan_relations,
)


def test_sequential_scan_matcher_flags_parallel_seq_scan_but_not_index_scan() -> None:
    payload = [
        {
            "Plan": {
                "Node Type": "Parallel Seq Scan",
                "Relation Name": "sources",
                "Plans": [
                    {
                        "Node Type": "Index Scan",
                        "Relation Name": "source_versions",
                        "Index Name": "uq_source_versions__source_ordinal",
                    },
                ],
            }
        }
    ]
    assert _sequential_scan_relations(payload) == ["sources"]


def test_migration_scanner_resolves_constant_index_and_constraint_names() -> None:
    source = (
        "op.create_index("
        "'ix_projection_intents__pending_dispatch', 'projection_intents', ['status'])\n"
        "sa.PrimaryKeyConstraint('source_id', name='pk_sources')\n"
        "sa.UniqueConstraint('workspace_id', name='uq_sources__workspace_source')\n"
    )
    assert _approved_index_names_from_source(source) == frozenset(
        {
            "ix_projection_intents__pending_dispatch",
            "pk_sources",
            "uq_sources__workspace_source",
        }
    )


def test_migration_scanner_fails_closed_on_a_variable_index_name() -> None:
    source = "op.create_index(index_name, 'sources', ['workspace_id'])\n"
    with pytest.raises(ValueError, match="unsupported index-name shape"):
        _approved_index_names_from_source(source)


def test_migration_scanner_fails_closed_on_an_fstring_index_name() -> None:
    source = 'op.create_index(f"ix_{table}", "sources", ["workspace_id"])\n'
    with pytest.raises(ValueError, match="unsupported index-name shape"):
        _approved_index_names_from_source(source)


def test_migration_scanner_fails_closed_on_a_dynamic_constraint_name_keyword() -> None:
    source = "sa.PrimaryKeyConstraint('source_id', name=constraint_name)\n"
    with pytest.raises(ValueError, match="unsupported index-name shape"):
        _approved_index_names_from_source(source)
