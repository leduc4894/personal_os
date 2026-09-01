"""Prometheus text exposition of the closed policy diagnostics counters.

The renderer is the first production metrics sink of the policy
observability surface (sink plan 2026-08-31): a dependency-free Prometheus
text-format (version 0.0.4) renderer over one immutable
:class:`ExclusionPolicyDiagnostics` snapshot of the shared recorder the
serve graph already binds. It renders — it never records and never
mutates: the recorder stays the single recording source, and a render
failure can never touch an evaluation path.

Only the two closed spec-21 counter families of the snapshot render, and
only as counters and closed tokens (docs/15 §3 cardinality rule): label
values are exactly the registry's closed boundary/decision/outcome enum
members — never a workspace, source, rule, preview, revision, path, media
type or key identity and never free text. The bounded recent-failure ring
deliberately does not render: its closed codes and epoch timestamps
surface through the Admin diagnostics route instead, keeping the scrape
output a pure counter family.
"""

from __future__ import annotations

from typing import Final

from personal_os.exclusion_policy.metrics import ExclusionPolicyDiagnostics

#: The two counter family names of the exposition (spec 21 / docs/15 §3).
#: IDs, locators, operands and revision numbers are never labels.
EVALUATION_COUNTER_NAME: Final[str] = "exclusion_policy_evaluation_total"
PUBLICATION_COUNTER_NAME: Final[str] = "exclusion_policy_publication_total"


def render_policy_diagnostics_prometheus(snapshot: ExclusionPolicyDiagnostics) -> str:
    """Render the closed policy counters in Prometheus text format.

    Counters and closed tokens only (docs/15 §3 cardinality rule): label
    values are the registry's closed boundary/decision/outcome members —
    never ids, paths or free text. Both families render sorted by their
    closed label values, so one snapshot always renders byte-identical
    text, and an empty snapshot renders the two TYPE headers alone — a
    valid scrape of a fresh or fallback sink.
    """
    lines: list[str] = [f"# TYPE {EVALUATION_COUNTER_NAME} counter"]
    for (boundary, decision), count in sorted(
        snapshot.evaluation_counters.items(),
        key=lambda item: (item[0][0].value, item[0][1].value),
    ):
        lines.append(
            f'{EVALUATION_COUNTER_NAME}{{boundary="{boundary.value}",'
            f'decision="{decision.value}"}} {count}'
        )
    lines.append(f"# TYPE {PUBLICATION_COUNTER_NAME} counter")
    for outcome, count in sorted(
        snapshot.publication_counters.items(),
        key=lambda item: item[0].value,
    ):
        lines.append(f'{PUBLICATION_COUNTER_NAME}{{outcome="{outcome.value}"}} {count}')
    return "\n".join(lines) + "\n"


__all__ = [
    "EVALUATION_COUNTER_NAME",
    "PUBLICATION_COUNTER_NAME",
    "render_policy_diagnostics_prometheus",
]
