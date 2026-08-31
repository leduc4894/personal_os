"""Closed-set mutation report for the exclusion-policy suites.

No external mutation tool supports the repository's Python 3.14 pin, so this
runner applies a small closed mutation set (comparison-operator swaps,
boolean-operator swaps, and integer-constant +/-1) to ``--source`` and runs
``--tests`` per mutant. A mutant is killed when the suite exits non-zero or
times out. Survivors are reported for hand review without captured test output.
"""

from __future__ import annotations

import argparse
import ast
import copy
import dataclasses
import enum
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

_COMPARISON_SWAPS: Final[dict[type[ast.cmpop], type[ast.cmpop]]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
}
_BOOL_SWAPS: Final[dict[type[ast.boolop], type[ast.boolop]]] = {
    ast.And: ast.Or,
    ast.Or: ast.And,
}


@dataclasses.dataclass(frozen=True, slots=True)
class Mutation:
    """One source mutation in the closed set."""

    path: Path
    line: int
    description: str
    source: str


class MutationOutcome(enum.Enum):
    """Closed result classes for one killing-suite execution."""

    KILLED = "killed"
    KILLED_BY_TIMEOUT = "killed_by_timeout"
    SURVIVED = "survived"


@dataclasses.dataclass(frozen=True, slots=True)
class MutationRun:
    """Aggregate results retained without child-process output."""

    mutant_count: int
    killed_count: int
    timeout_count: int
    survivors: tuple[Mutation, ...]


def _cloned_target(
    tree: ast.Module, target_index: int, expected_type: type[ast.AST]
) -> tuple[ast.Module, ast.AST]:
    cloned = copy.deepcopy(tree)
    target = list(ast.walk(cloned))[target_index]
    if not isinstance(target, expected_type):
        raise RuntimeError("mutation_target_mismatch")
    return cloned, target


def _render_mutation(tree: ast.Module) -> str:
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _mutations_of(tree: ast.Module, source: str, path: Path) -> list[Mutation]:
    """Enumerate the precise closed mutation set for one parsed module."""
    mutations: list[Mutation] = []
    for target_index, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            comparison_replacement = _COMPARISON_SWAPS.get(type(node.ops[0]))
            if comparison_replacement is not None:
                mutated, candidate_node = _cloned_target(tree, target_index, ast.Compare)
                candidate = candidate_node
                assert isinstance(candidate, ast.Compare)
                candidate.ops[0] = comparison_replacement()
                mutations.append(
                    Mutation(
                        path=path,
                        line=node.lineno,
                        description=(
                            f"{type(node.ops[0]).__name__}->{comparison_replacement.__name__}"
                        ),
                        source=_render_mutation(mutated),
                    )
                )
        elif isinstance(node, ast.BoolOp):
            bool_replacement = _BOOL_SWAPS.get(type(node.op))
            if bool_replacement is not None:
                mutated, candidate_node = _cloned_target(tree, target_index, ast.BoolOp)
                candidate = candidate_node
                assert isinstance(candidate, ast.BoolOp)
                candidate.op = bool_replacement()
                mutations.append(
                    Mutation(
                        path=path,
                        line=node.lineno,
                        description=f"{type(node.op).__name__}->{bool_replacement.__name__}",
                        source=_render_mutation(mutated),
                    )
                )
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            for delta in (1, -1):
                mutated, candidate_node = _cloned_target(tree, target_index, ast.Constant)
                candidate = candidate_node
                assert isinstance(candidate, ast.Constant)
                candidate.value = node.value + delta
                mutations.append(
                    Mutation(
                        path=path,
                        line=node.lineno,
                        description=f"int {node.value}->{node.value + delta}",
                        source=_render_mutation(mutated),
                    )
                )
    return [mutation for mutation in mutations if mutation.source != source]


def _run_suite(tests: Path, timeout_seconds: int) -> MutationOutcome:
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "pytest",
                str(tests),
                "-x",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            timeout=timeout_seconds,
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return MutationOutcome.KILLED_BY_TIMEOUT
    if result.returncode != 0:
        return MutationOutcome.KILLED
    return MutationOutcome.SURVIVED


def _display_path(path: Path, source_root: Path) -> str:
    resolved_path = path.resolve()
    for base in (Path.cwd().resolve(), source_root.resolve()):
        try:
            return resolved_path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.name


def _execute_mutations(
    mutations: list[Mutation],
    *,
    source_root: Path,
    tests: Path,
    timeout_seconds: int,
) -> MutationRun:
    killed_count = 0
    timeout_count = 0
    survivors: list[Mutation] = []
    for index, mutation in enumerate(mutations, start=1):
        original = mutation.path.read_bytes()
        original_stat = mutation.path.stat()
        try:
            mutation.path.write_bytes(mutation.source.encode("utf-8") + b"\n")
            # A unique second-level mtime invalidates timestamp-based bytecode;
            # ``-B`` then prevents a mutant cache from replacing the original.
            os.utime(
                mutation.path,
                ns=(
                    original_stat.st_atime_ns,
                    original_stat.st_mtime_ns + index * 1_000_000_000,
                ),
            )
            outcome = _run_suite(tests, timeout_seconds)
            if outcome is MutationOutcome.SURVIVED:
                survivors.append(mutation)
            else:
                killed_count += 1
                if outcome is MutationOutcome.KILLED_BY_TIMEOUT:
                    timeout_count += 1
        finally:
            mutation.path.write_bytes(original)
            os.utime(
                mutation.path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
        location = _display_path(mutation.path, source_root)
        print(
            f"[{index}/{len(mutations)}] "
            f"{location}:{mutation.line} {mutation.description} {outcome.value}",
            flush=True,
        )
    return MutationRun(
        mutant_count=len(mutations),
        killed_count=killed_count,
        timeout_count=timeout_count,
        survivors=tuple(survivors),
    )


def _write_report(output: Path, run: MutationRun, source_root: Path) -> None:
    score = f"{run.killed_count / run.mutant_count:.3f}" if run.mutant_count else "n/a"
    lines = [
        "# Exclusion-policy mutation report",
        "",
        f"- mutants: {run.mutant_count}",
        f"- killed: {run.killed_count}",
        f"- killed by timeout: {run.timeout_count}",
        f"- survived: {len(run.survivors)}",
        f"- score: {score}",
        "",
        "## Survivors",
        "",
    ]
    if run.survivors:
        lines.extend(
            f"- `{_display_path(mutation.path, source_root)}:{mutation.line}` "
            f"— {mutation.description}"
            for mutation in run.survivors
        )
    else:
        lines.append("- none")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-mutant-timeout-seconds", type=int, default=180)
    args = parser.parse_args()

    mutations: list[Mutation] = []
    tests_root = args.tests.resolve()
    for py_file in sorted(args.source.rglob("*.py")):
        if py_file.resolve().is_relative_to(tests_root):
            continue
        source = py_file.read_text(encoding="utf-8")
        mutations.extend(_mutations_of(ast.parse(source), source, py_file))

    run = _execute_mutations(
        mutations,
        source_root=args.source,
        tests=args.tests,
        timeout_seconds=args.per_mutant_timeout_seconds,
    )
    _write_report(args.output, run, args.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
