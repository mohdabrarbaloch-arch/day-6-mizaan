"""Pipeline DAG validation and promotion gates."""

from __future__ import annotations

from app.engine import pipeline

VALID = [
    {"id": "ingest", "kind": "ingest", "depends_on": []},
    {"id": "train", "kind": "train", "depends_on": ["ingest"]},
    {"id": "eval", "kind": "eval", "depends_on": ["train"]},
    {"id": "register", "kind": "register", "depends_on": ["eval"]},
    {"id": "deploy", "kind": "deploy", "depends_on": ["register"]},
]


class TestValidGraphs:
    def test_standard_five_stage_graph_validates(self):
        result = pipeline.validate(VALID)
        assert result["valid"] is True
        assert result["errors"] == []
        assert result["order"] == ["ingest", "train", "eval", "register", "deploy"]

    def test_order_is_deterministic(self):
        first = pipeline.validate(VALID)["order"]
        second = pipeline.validate(VALID)["order"]
        assert first == second

    def test_independent_nodes_break_ties_by_kind_then_id(self):
        nodes = [
            {"id": "b", "kind": "ingest", "depends_on": []},
            {"id": "a", "kind": "ingest", "depends_on": []},
        ]
        order = pipeline.validate(nodes)["order"]
        assert order == ["a", "b"]

    def test_single_node_is_valid(self):
        result = pipeline.validate([{"id": "ingest", "kind": "ingest", "depends_on": []}])
        assert result["valid"] is True

    def test_diamond_graph_validates(self):
        nodes = [
            {"id": "ingest", "kind": "ingest", "depends_on": []},
            {"id": "a", "kind": "train", "depends_on": ["ingest"]},
            {"id": "b", "kind": "train", "depends_on": ["ingest"]},
            {"id": "eval", "kind": "eval", "depends_on": ["a", "b"]},
        ]
        result = pipeline.validate(nodes)
        assert result["valid"] is True
        assert result["order"][0] == "ingest"
        assert result["order"][-1] == "eval"


class TestStructuralErrors:
    def test_empty_pipeline_rejected(self):
        result = pipeline.validate([])
        assert result["valid"] is False
        assert result["errors"][0]["code"] == "empty_pipeline"

    def test_duplicate_id_rejected(self):
        nodes = [
            {"id": "a", "kind": "ingest", "depends_on": []},
            {"id": "a", "kind": "train", "depends_on": []},
        ]
        codes = {e["code"] for e in pipeline.validate(nodes)["errors"]}
        assert "duplicate_id" in codes

    def test_unknown_kind_rejected(self):
        nodes = [{"id": "a", "kind": "teleport", "depends_on": []}]
        codes = {e["code"] for e in pipeline.validate(nodes)["errors"]}
        assert "unknown_kind" in codes

    def test_unknown_dependency_rejected(self):
        nodes = [{"id": "train", "kind": "train", "depends_on": ["ghost"]}]
        codes = {e["code"] for e in pipeline.validate(nodes)["errors"]}
        assert "unknown_dependency" in codes

    def test_self_dependency_rejected(self):
        nodes = [{"id": "train", "kind": "train", "depends_on": ["train"]}]
        codes = {e["code"] for e in pipeline.validate(nodes)["errors"]}
        assert "self_dependency" in codes

    def test_missing_id_rejected(self):
        nodes = [{"kind": "ingest", "depends_on": []}]
        codes = {e["code"] for e in pipeline.validate(nodes)["errors"]}
        assert "missing_id" in codes

    def test_all_errors_reported_at_once(self):
        """An operator fixing a graph wants the full list, not twenty questions."""
        nodes = [
            {"id": "a", "kind": "bogus", "depends_on": []},
            {"id": "a", "kind": "ingest", "depends_on": []},
            {"kind": "ingest", "depends_on": []},
        ]
        result = pipeline.validate(nodes)
        assert len(result["errors"]) >= 2


class TestCycles:
    def test_two_node_cycle_detected(self):
        nodes = [
            {"id": "a", "kind": "train", "depends_on": ["b"]},
            {"id": "b", "kind": "eval", "depends_on": ["a"]},
        ]
        result = pipeline.validate(nodes)
        assert result["valid"] is False
        assert any(e["code"] == "cycle" for e in result["errors"])

    def test_cycle_reports_the_involved_nodes(self):
        nodes = [
            {"id": "train", "kind": "train", "depends_on": ["eval"]},
            {"id": "eval", "kind": "eval", "depends_on": ["train"]},
        ]
        err = next(e for e in pipeline.validate(nodes)["errors"] if e["code"] == "cycle")
        assert set(err["nodes"]) == {"train", "eval"}

    def test_three_node_cycle_detected(self):
        nodes = [
            {"id": "a", "kind": "ingest", "depends_on": ["c"]},
            {"id": "b", "kind": "train", "depends_on": ["a"]},
            {"id": "c", "kind": "eval", "depends_on": ["b"]},
        ]
        assert any(e["code"] == "cycle" for e in pipeline.validate(nodes)["errors"])

    def test_cycle_returns_empty_order(self):
        nodes = [
            {"id": "a", "kind": "train", "depends_on": ["b"]},
            {"id": "b", "kind": "eval", "depends_on": ["a"]},
        ]
        assert pipeline.validate(nodes)["order"] == []


class TestKindOrdering:
    def test_register_before_eval_rejected(self):
        """Registration must not happen before anything has been evaluated."""
        nodes = [
            {"id": "ingest", "kind": "ingest", "depends_on": []},
            {"id": "register", "kind": "register", "depends_on": ["ingest"]},
            {"id": "eval", "kind": "eval", "depends_on": ["register"]},
        ]
        codes = {e["code"] for e in pipeline.validate(nodes)["errors"]}
        assert "kind_order_violation" in codes

    def test_deploy_before_register_rejected(self):
        nodes = [
            {"id": "ingest", "kind": "ingest", "depends_on": []},
            {"id": "deploy", "kind": "deploy", "depends_on": ["ingest"]},
        ]
        codes = {e["code"] for e in pipeline.validate(nodes)["errors"]}
        assert "kind_order_violation" in codes

    def test_deploy_without_eval_warns(self):
        nodes = [
            {"id": "ingest", "kind": "ingest", "depends_on": []},
            {"id": "register", "kind": "register", "depends_on": ["ingest"]},
            {"id": "deploy", "kind": "deploy", "depends_on": ["register"]},
        ]
        result = pipeline.validate(nodes)
        assert result["valid"] is True
        assert any("no eval stage" in w for w in result["warnings"])


class TestGates:
    def test_passing_gate(self):
        result = pipeline.evaluate_gates({"accuracy": {"min": 0.9}}, {"accuracy": 0.95})
        assert result["passed"] is True
        assert result["failed_count"] == 0

    def test_failing_min_gate(self):
        result = pipeline.evaluate_gates({"accuracy": {"min": 0.9}}, {"accuracy": 0.8})
        assert result["passed"] is False
        assert "below the minimum" in result["results"][0]["reason"]

    def test_failing_max_gate(self):
        result = pipeline.evaluate_gates({"latency_p95_ms": {"max": 800}}, {"latency_p95_ms": 950})
        assert result["passed"] is False
        assert "above the maximum" in result["results"][0]["reason"]

    def test_missing_observation_fails_the_gate(self):
        """Silence is not a pass — this is the whole point of gating."""
        result = pipeline.evaluate_gates({"accuracy": {"min": 0.9}}, {})
        assert result["passed"] is False
        assert "no observed value" in result["results"][0]["reason"]

    def test_boundary_value_passes(self):
        result = pipeline.evaluate_gates({"accuracy": {"min": 0.9}}, {"accuracy": 0.9})
        assert result["passed"] is True

    def test_two_sided_bound(self):
        gates = {"score": {"min": 0.5, "max": 0.9}}
        assert pipeline.evaluate_gates(gates, {"score": 0.7})["passed"] is True
        assert pipeline.evaluate_gates(gates, {"score": 0.95})["passed"] is False
        assert pipeline.evaluate_gates(gates, {"score": 0.4})["passed"] is False

    def test_no_gates_means_promotable(self):
        result = pipeline.evaluate_gates({}, {})
        assert result["passed"] is True
        assert result["gated_metric_count"] == 0

    def test_one_failing_gate_fails_the_run(self):
        gates = {"accuracy": {"min": 0.9}, "latency": {"max": 800}}
        result = pipeline.evaluate_gates(gates, {"accuracy": 0.95, "latency": 2000})
        assert result["passed"] is False
        assert result["failed_count"] == 1
        assert result["gated_metric_count"] == 2


class TestPromotion:
    def test_succeeded_with_passing_gates_is_promotable(self):
        ok, reason = pipeline.can_promote("succeeded", {"passed": True})
        assert ok is True
        assert "gates passed" in reason

    def test_failed_run_is_not_promotable(self):
        ok, reason = pipeline.can_promote("failed", {"passed": True})
        assert ok is False
        assert "failed" in reason

    def test_running_run_is_not_promotable(self):
        """A partial evaluation passing is not a pass."""
        ok, _ = pipeline.can_promote("running", {"passed": True})
        assert ok is False

    def test_passing_run_with_failing_gates_is_not_promotable(self):
        ok, reason = pipeline.can_promote("succeeded", {"passed": False})
        assert ok is False
        assert "gates" in reason
