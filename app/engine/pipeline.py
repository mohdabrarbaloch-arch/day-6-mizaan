"""Pipeline DAG validation and promotion gate evaluation.

Validation collects *all* errors rather than raising on the first, because an
operator fixing a graph wants the whole list, not a game of twenty questions.

Topological order is deterministic: ready nodes are sorted by
``(kind_rank, id)``, so the same graph always yields the same execution order.
A pipeline that shuffles its own run order cannot be reproduced, which makes
its results unfalsifiable.
"""

from __future__ import annotations

from typing import Any

# Allowed node kinds, in the order they must occur.
KIND_ORDER: tuple[str, ...] = ("ingest", "train", "eval", "register", "deploy")
KIND_RANK: dict[str, int] = {kind: i for i, kind in enumerate(KIND_ORDER)}


class PipelineError(ValueError):
    """Raised when a pipeline graph cannot be interpreted at all."""


def _ancestors(deps: dict[str, list[str]]) -> dict[str, set[str]]:
    """Transitive dependency set for every node.

    Iterative rather than recursive so a long chain cannot hit the recursion
    limit, and memoised per node so a wide graph stays linear-ish.
    """
    memo: dict[str, set[str]] = {}

    def collect(node_id: str) -> set[str]:
        if node_id in memo:
            return memo[node_id]
        out: set[str] = set()
        stack = list(deps.get(node_id, []))
        while stack:
            current = stack.pop()
            if current in out:
                continue
            out.add(current)
            stack.extend(deps.get(current, []))
        memo[node_id] = out
        return out

    for node_id in deps:
        collect(node_id)
    return memo


def validate(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate a pipeline graph.

    Returns:
        valid    — True when errors is empty
        errors   — list of {code, message, node?} — every problem found
        order    — deterministic topological order (empty when invalid)
        warnings — non-fatal observations
    """
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []

    if not isinstance(nodes, list) or not nodes:
        return {
            "valid": False,
            "errors": [
                {"code": "empty_pipeline", "message": "A pipeline needs at least one node."}
            ],
            "order": [],
            "warnings": [],
        }

    by_id: dict[str, dict[str, Any]] = {}

    for idx, node in enumerate(nodes):
        if not isinstance(node, dict):
            errors.append(
                {
                    "code": "malformed_node",
                    "message": f"Node at index {idx} is not an object.",
                }
            )
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append(
                {
                    "code": "missing_id",
                    "message": f"Node at index {idx} has no id.",
                }
            )
            continue
        if node_id in by_id:
            errors.append(
                {
                    "code": "duplicate_id",
                    "message": f"Duplicate node id {node_id!r}.",
                    "node": node_id,
                }
            )
            continue
        kind = node.get("kind")
        if kind not in KIND_RANK:
            errors.append(
                {
                    "code": "unknown_kind",
                    "message": (
                        f"Node {node_id!r} has unknown kind {kind!r}. "
                        f"Expected one of: {', '.join(KIND_ORDER)}."
                    ),
                    "node": node_id,
                }
            )
            continue
        by_id[node_id] = node

    # Dependencies: unknown reference, self-reference.
    deps: dict[str, list[str]] = {}
    for node_id, node in by_id.items():
        raw = node.get("depends_on") or []
        if not isinstance(raw, list):
            errors.append(
                {
                    "code": "malformed_depends_on",
                    "message": f"Node {node_id!r} has non-list depends_on.",
                    "node": node_id,
                }
            )
            deps[node_id] = []
            continue
        cleaned: list[str] = []
        for dep in raw:
            if not isinstance(dep, str):
                errors.append(
                    {
                        "code": "malformed_dependency",
                        "message": f"Node {node_id!r} depends on a non-string value.",
                        "node": node_id,
                    }
                )
                continue
            if dep == node_id:
                errors.append(
                    {
                        "code": "self_dependency",
                        "message": f"Node {node_id!r} depends on itself.",
                        "node": node_id,
                    }
                )
                continue
            if dep not in by_id:
                errors.append(
                    {
                        "code": "unknown_dependency",
                        "message": f"Node {node_id!r} depends on unknown node {dep!r}.",
                        "node": node_id,
                    }
                )
                continue
            if dep not in cleaned:
                cleaned.append(dep)
        deps[node_id] = cleaned

    if errors:
        return {"valid": False, "errors": errors, "order": [], "warnings": warnings}

    # Kahn's algorithm with deterministic tie-breaking.
    indegree = {node_id: len(deps[node_id]) for node_id in by_id}
    children: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    for node_id, node_deps in deps.items():
        for dep in node_deps:
            children[dep].append(node_id)

    ready = sorted(
        (n for n, d in indegree.items() if d == 0),
        key=lambda n: (KIND_RANK[by_id[n]["kind"]], n),
    )
    order: list[str] = []

    while ready:
        current = ready.pop(0)
        order.append(current)
        for child in sorted(children[current], key=lambda n: (KIND_RANK[by_id[n]["kind"]], n)):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort(key=lambda n: (KIND_RANK[by_id[n]["kind"]], n))

    if len(order) != len(by_id):
        cycle_nodes = sorted(set(by_id) - set(order))
        errors.append(
            {
                "code": "cycle",
                "message": (
                    "Dependency cycle among: "
                    + ", ".join(cycle_nodes)
                    + " — these nodes can never become ready."
                ),
                "nodes": cycle_nodes,
            }
        )
        return {"valid": False, "errors": errors, "order": [], "warnings": warnings}

    # Kind ordering: an earlier rank must not depend on a later rank.
    for node_id in order:
        node_rank = KIND_RANK[by_id[node_id]["kind"]]
        for dep in deps[node_id]:
            if KIND_RANK[by_id[dep]["kind"]] > node_rank:
                errors.append(
                    {
                        "code": "kind_order_violation",
                        "message": (
                            f"{by_id[node_id]['kind']} node {node_id!r} depends on later-stage "
                            f"{by_id[dep]['kind']} node {dep!r}."
                        ),
                        "node": node_id,
                    }
                )

    # A deploy must have a register somewhere upstream: you cannot ship
    # something the registry has never seen. This is an error, not a warning,
    # because the failure it prevents (unattributed artefact in production)
    # is much worse than the friction of declaring the stage.
    ancestors = _ancestors(deps)
    for node in by_id.values():
        if node["kind"] != "deploy":
            continue
        upstream_kinds = {by_id[a]["kind"] for a in ancestors.get(node["id"], set())}
        if "register" not in upstream_kinds:
            errors.append(
                {
                    "code": "kind_order_violation",
                    "message": (
                        f"deploy node {node['id']!r} has no register node upstream; "
                        "a deployment must reference a registered artefact."
                    ),
                    "node": node["id"],
                }
            )

    # A deploy with no eval upstream means nothing was gated.
    has_eval = any(n["kind"] == "eval" for n in by_id.values())
    for node in by_id.values():
        if node["kind"] == "deploy" and not has_eval:
            warnings.append(
                f"Deploy node {node['id']!r} exists but the pipeline has no eval stage, "
                "so nothing gates promotion."
            )

    return {
        "valid": not errors,
        "errors": errors,
        "order": order if not errors else [],
        "warnings": warnings,
    }


def evaluate_gates(
    gates: dict[str, dict[str, float]] | None,
    observed: dict[str, float] | None,
) -> dict[str, Any]:
    """Evaluate promotion gates against observed metrics.

    ``gates`` maps metric name to a bound object, either ``{"min": x}``,
    ``{"max": x}``, or both. A metric with no observed value fails its gate —
    silence is not a pass, and treating it as one is how unmeasured models
    reach production.
    """
    gates = gates or {}
    observed = observed or {}

    results: list[dict[str, Any]] = []
    passed = True

    for metric in sorted(gates):
        bound = gates[metric]
        if not isinstance(bound, dict):
            results.append(
                {
                    "metric": metric,
                    "passed": False,
                    "reason": "gate bound must be an object with min and/or max",
                    "observed": observed.get(metric),
                }
            )
            passed = False
            continue

        value = observed.get(metric)
        entry: dict[str, Any] = {
            "metric": metric,
            "observed": value,
            "min": bound.get("min"),
            "max": bound.get("max"),
        }

        if value is None:
            entry["passed"] = False
            entry["reason"] = "no observed value for a gated metric"
            passed = False
            results.append(entry)
            continue

        failures: list[str] = []
        if "min" in bound and value < bound["min"]:
            failures.append(f"{value} is below the minimum {bound['min']}")
        if "max" in bound and value > bound["max"]:
            failures.append(f"{value} is above the maximum {bound['max']}")

        entry["passed"] = not failures
        entry["reason"] = "; ".join(failures) if failures else "within bounds"
        if failures:
            passed = False
        results.append(entry)

    return {
        "passed": passed,
        "results": results,
        "gated_metric_count": len(gates),
        "failed_count": sum(1 for r in results if not r["passed"]),
    }


def can_promote(run_status: str, gate_result: dict[str, Any]) -> tuple[bool, str]:
    """Whether a pipeline run may move to ``promoted``.

    A run that has not completed cannot be promoted regardless of its gates —
    a partial evaluation passing is not a pass.
    """
    if run_status != "succeeded":
        return False, f"run is {run_status}, not succeeded; nothing to promote"
    if not gate_result.get("passed", False):
        return False, "promotion gates did not pass"
    return True, "gates passed and run succeeded"
