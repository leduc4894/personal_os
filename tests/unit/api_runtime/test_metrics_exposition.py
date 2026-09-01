"""Prometheus text renderer over the policy diagnostics snapshot.

The renderer is the production metrics sink of the policy observability
surface (sink plan 2026-08-31): it renders the closed spec-21 counter
families of one immutable :class:`ExclusionPolicyDiagnostics` snapshot in
the Prometheus text exposition format — counters and closed tokens only
(docs/15 §3 cardinality rule). Label values are exactly the registry's
closed boundary/decision/outcome members; ids, paths, hostnames and free
text never become labels, and the failure ring stays off the wire (its
closed codes surface through the Admin diagnostics route instead).
"""

from __future__ import annotations

from api_runtime.metrics_exposition import render_policy_diagnostics_prometheus

from personal_os.exclusion_policy.metrics import (
    EvaluationMetricOutcome,
    ExclusionPolicyDiagnostics,
    PolicyBoundary,
    PublicationMetricOutcome,
)


def test_renderer_emits_closed_prometheus_counters_only() -> None:
    snapshot = ExclusionPolicyDiagnostics(
        evaluation_counters={
            (PolicyBoundary.SOURCE_CREATE_UPDATE, EvaluationMetricOutcome.FAILED): 2,
            (PolicyBoundary.SOURCE_CREATE_UPDATE, EvaluationMetricOutcome.ALLOWED): 7,
        },
        publication_counters={PublicationMetricOutcome.REJECTED: 1},
        recent_failures=(),
    )

    text = render_policy_diagnostics_prometheus(snapshot)

    lines = text.strip().splitlines()
    assert (
        'exclusion_policy_evaluation_total{boundary="source_create_update",decision="failed"} 2'
        in lines
    )
    assert (
        'exclusion_policy_evaluation_total{boundary="source_create_update",decision="allowed"} 7'
        in lines
    )
    assert 'exclusion_policy_publication_total{outcome="rejected"} 1' in lines
    assert "# TYPE exclusion_policy_evaluation_total counter" in lines
    assert "# TYPE exclusion_policy_publication_total counter" in lines
    assert text.endswith("\n")


def test_renderer_orders_families_and_labels_deterministically() -> None:
    """One snapshot always renders byte-identical, sorted exposition text."""

    snapshot = ExclusionPolicyDiagnostics(
        evaluation_counters={
            (PolicyBoundary.RETRIEVAL, EvaluationMetricOutcome.ALLOWED): 1,
            (PolicyBoundary.CANONICAL_READ, EvaluationMetricOutcome.EXCLUDED): 4,
            (PolicyBoundary.CANONICAL_READ, EvaluationMetricOutcome.ALLOWED): 3,
        },
        publication_counters={
            PublicationMetricOutcome.REPLAYED: 2,
            PublicationMetricOutcome.PUBLISHED: 5,
        },
        recent_failures=(),
    )

    text = render_policy_diagnostics_prometheus(snapshot)

    assert text == (
        "# TYPE exclusion_policy_evaluation_total counter\n"
        'exclusion_policy_evaluation_total{boundary="canonical_read",decision="allowed"} 3\n'
        'exclusion_policy_evaluation_total{boundary="canonical_read",decision="excluded"} 4\n'
        'exclusion_policy_evaluation_total{boundary="retrieval",decision="allowed"} 1\n'
        "# TYPE exclusion_policy_publication_total counter\n"
        'exclusion_policy_publication_total{outcome="published"} 5\n'
        'exclusion_policy_publication_total{outcome="replayed"} 2\n'
    )


def test_renderer_serves_an_empty_snapshot_as_the_two_type_headers() -> None:
    """A fresh or fallback sink renders a valid scrape with zero samples."""

    snapshot = ExclusionPolicyDiagnostics(
        evaluation_counters={},
        publication_counters={},
        recent_failures=(),
    )

    text = render_policy_diagnostics_prometheus(snapshot)

    assert text == (
        "# TYPE exclusion_policy_evaluation_total counter\n"
        "# TYPE exclusion_policy_publication_total counter\n"
    )
