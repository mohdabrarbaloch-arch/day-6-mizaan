"""Drift classification and the parity matrix."""

from __future__ import annotations

from app.engine import digest, drift

BASE = {
    "version": "1.0.0",
    "components": {
        "weights": {"sha256": "a" * 64, "format": "safetensors"},
        "tokenizer": {"sha256": "b" * 64, "vocab_size": 32000},
        "template": {"sha256": "c" * 64, "name": "chatml"},
        "runtime": {"framework": "vllm", "max_seq_len": 8192},
    },
    "params": {"temperature": 0.7, "max_tokens": 2048},
}


def _digest(manifest):
    return digest.manifest_digest(manifest)


class TestIdentical:
    def test_same_manifest_is_identical(self):
        report = drift.compare(BASE, BASE)
        assert report["identical"] is True
        assert report["classes"] == []
        assert report["severity"] is None
        assert report["diffs"] == []

    def test_overrides_that_change_nothing_stay_identical(self):
        """Deploying twice with empty overrides must prove parity, not differ."""
        a = digest.merge_overrides(BASE, {})
        b = digest.merge_overrides(BASE, None)
        report = drift.compare(a, b, digest_a=_digest(a), digest_b=_digest(b))
        assert report["identical"] is True


class TestClassification:
    def test_weights_drift_is_the_most_severe(self):
        other = {
            **BASE,
            "components": {
                **BASE["components"],
                "weights": {"sha256": "z" * 64, "format": "safetensors"},
            },
        }
        report = drift.compare(BASE, other)
        assert report["severity"] == "weights"
        assert "weights" in report["classes"]

    def test_tokenizer_drift_detected(self):
        other = {
            **BASE,
            "components": {
                **BASE["components"],
                "tokenizer": {"sha256": "b" * 64, "vocab_size": 50000},
            },
        }
        report = drift.compare(BASE, other)
        assert "tokenizer" in report["classes"]

    def test_template_drift_detected(self):
        other = {
            **BASE,
            "components": {
                **BASE["components"],
                "template": {"sha256": "c" * 64, "name": "llama3"},
            },
        }
        report = drift.compare(BASE, other)
        assert report["severity"] == "template"

    def test_runtime_drift_detected(self):
        other = digest.merge_overrides(BASE, {"components.runtime.max_seq_len": 4096})
        report = drift.compare(BASE, other)
        assert "runtime" in report["classes"]

    def test_parameter_only_drift(self):
        other = digest.merge_overrides(BASE, {"params.temperature": 0.2})
        report = drift.compare(BASE, other)
        assert report["classes"] == ["parameters"]
        assert report["severity"] == "parameters"

    def test_version_drift_detected(self):
        other = {**BASE, "version": "1.0.1"}
        report = drift.compare(BASE, other)
        assert "version" in report["classes"]

    def test_multiple_classes_are_ordered_worst_first(self):
        """A pair can carry several problems; severity[0] must be the worst."""
        other = digest.merge_overrides(BASE, {"params.temperature": 0.2})
        other = {**other, "components": {**other["components"], "weights": {"sha256": "z" * 64}}}
        report = drift.compare(BASE, other)
        assert report["classes"][0] == "weights"
        assert "parameters" in report["classes"]
        assert report["severity"] == "weights"


class TestDiffs:
    def test_field_level_diff_names_the_change(self):
        other = digest.merge_overrides(BASE, {"components.runtime.max_seq_len": 4096})
        report = drift.compare(BASE, other)
        fields = {d["field"] for d in report["diffs"]}
        assert any("max_seq_len" in f for f in fields)
        diff = next(d for d in report["diffs"] if "max_seq_len" in d["field"])
        assert diff["a"] == 8192
        assert diff["b"] == 4096

    def test_absent_vs_present_is_reported(self):
        a = {"components": {"runtime": {"framework": "vllm"}}, "params": {}}
        b = {"components": {"runtime": {"framework": "vllm", "max_seq_len": 8192}}, "params": {}}
        report = drift.compare(a, b)
        assert not report["identical"]
        assert any(d["field"].endswith("max_seq_len") for d in report["diffs"])

    def test_nested_list_difference_captured(self):
        a = {"components": {}, "params": {"stop_sequences": ["<a>"]}}
        b = {"components": {}, "params": {"stop_sequences": ["<b>"]}}
        report = drift.compare(a, b)
        assert any(d["field"].endswith("stop_sequences") for d in report["diffs"])

    def test_summary_is_a_sentence_not_a_code(self):
        other = {**BASE, "components": {**BASE["components"], "weights": {"sha256": "z" * 64}}}
        report = drift.compare(BASE, other)
        assert report["summary"].endswith(".")
        assert "weights" in report["summary"].lower()


class TestMatrix:
    def _dep(self, dep_id, region, manifest, environment="production"):
        return {
            "id": dep_id,
            "region": region,
            "environment": environment,
            "effective_manifest": manifest,
            "digest": _digest(manifest),
        }

    def test_empty_set_is_trivially_identical(self):
        m = drift.build_matrix([])
        assert m["cells"] == []
        assert m["summary"]["all_identical"] is True

    def test_single_deployment_has_no_pairs(self):
        m = drift.build_matrix([self._dep(1, "us-east", BASE)])
        assert m["cells"] == []
        assert m["summary"]["all_identical"] is True

    def test_two_identical_regions_produce_a_passing_cell(self):
        """The central claim of the product, asserted directly."""
        m = drift.build_matrix(
            [
                self._dep(1, "us-east", BASE),
                self._dep(2, "ap-south", BASE),
            ]
        )
        assert len(m["cells"]) == 1
        assert m["cells"][0]["identical"] is True
        assert m["summary"]["all_identical"] is True

    def test_override_creates_exactly_one_drifting_pair(self):
        drifted = digest.merge_overrides(BASE, {"components.runtime.max_seq_len": 4096})
        m = drift.build_matrix(
            [
                self._dep(1, "us-east", BASE),
                self._dep(2, "ap-south", drifted),
            ]
        )
        assert m["summary"]["drifting_pairs"] == 1
        assert m["summary"]["all_identical"] is False
        assert m["summary"]["worst_severity"] == "runtime"

    def test_three_deployments_yield_three_pairs(self):
        drifted = digest.merge_overrides(BASE, {"params.temperature": 0.2})
        m = drift.build_matrix(
            [
                self._dep(1, "us-east", BASE),
                self._dep(2, "eu-west", BASE),
                self._dep(3, "ap-south", drifted),
            ]
        )
        assert len(m["cells"]) == 3
        assert m["summary"]["drifting_pairs"] == 2
        assert m["summary"]["deployment_count"] == 3

    def test_worst_severity_is_the_worst_across_pairs(self):
        template_drift = digest.merge_overrides(BASE, {"components.template.name": "llama3"})
        weight_drift = digest.merge_overrides(BASE, {"components.weights.sha256": "z" * 64})
        m = drift.build_matrix(
            [
                self._dep(1, "us-east", BASE),
                self._dep(2, "eu-west", template_drift),
                self._dep(3, "ap-south", weight_drift),
            ]
        )
        assert m["summary"]["worst_severity"] == "weights"

    def test_environments_are_distinguished(self):
        m = drift.build_matrix(
            [
                self._dep(1, "us-east", BASE, "production"),
                self._dep(2, "us-east", BASE, "staging"),
            ]
        )
        cell = m["cells"][0]
        assert cell["a_environment"] == "production"
        assert cell["b_environment"] == "staging"
        # Same manifest in both envs: identical, but the labels still differ.
        assert cell["identical"] is True


class TestErrors:
    def test_non_object_manifest_rejected(self):
        try:
            drift.compare("nope", BASE)  # type: ignore[arg-type]
        except drift.DriftError:
            assert True
        else:
            raise AssertionError("expected DriftError")
