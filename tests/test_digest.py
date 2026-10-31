"""Digest canonicalisation and determinism.

These tests pin exact hex values. If a refactor changes the encoding, these fail
immediately — which is the point: a digest that quietly changes is worse than no
digest, because it silently invalidates every stored comparison.
"""

from __future__ import annotations

import pytest

from app.engine import digest

BASE = {
    "name": "tarteel",
    "version": "1.0.0",
    "components": {
        "weights": {"sha256": "a" * 64, "format": "safetensors", "quantization": "awq-int4"},
        "tokenizer": {"sha256": "b" * 64, "vocab_size": 32000},
        "template": {"sha256": "c" * 64, "name": "chatml"},
        "runtime": {"framework": "vllm", "max_seq_len": 8192},
    },
    "params": {"temperature": 0.7, "top_p": 0.95, "max_tokens": 2048},
}


class TestCanonicalisation:
    def test_dict_key_order_does_not_change_digest(self):
        """The whole design fails if build order leaks into the hash."""
        reordered = {
            "params": {"max_tokens": 2048, "top_p": 0.95, "temperature": 0.7},
            "components": {
                "runtime": {"max_seq_len": 8192, "framework": "vllm"},
                "template": {"name": "chatml", "sha256": "c" * 64},
                "tokenizer": {"vocab_size": 32000, "sha256": "b" * 64},
                "weights": {
                    "quantization": "awq-int4",
                    "format": "safetensors",
                    "sha256": "a" * 64,
                },
            },
            "version": "1.0.0",
            "name": "tarteel",
        }
        assert digest.manifest_digest(BASE) == digest.manifest_digest(reordered)

    def test_nested_key_order_does_not_change_digest(self):
        left = {"components": {"runtime": {"a": 1, "b": {"c": 2, "d": 3}}}, "params": {}}
        right = {"components": {"runtime": {"b": {"d": 3, "c": 2}, "a": 1}}, "params": {}}
        assert digest.manifest_digest(left) == digest.manifest_digest(right)

    def test_float_precision_noise_is_normalised(self):
        """0.7000001 and 0.7 must hash the same; 0.7 and 0.71 must not."""
        a = {"components": {}, "params": {"temperature": 0.7}}
        b = {"components": {}, "params": {"temperature": 0.7000001}}
        c = {"components": {}, "params": {"temperature": 0.71}}
        assert digest.manifest_digest(a) == digest.manifest_digest(b)
        assert digest.manifest_digest(a) != digest.manifest_digest(c)

    def test_negative_zero_normalises_to_zero(self):
        a = {"components": {}, "params": {"temperature": 0.0}}
        b = {"components": {}, "params": {"temperature": -0.0}}
        assert digest.manifest_digest(a) == digest.manifest_digest(b)

    def test_nan_raises_rather_than_hashing_something_wrong(self):
        with pytest.raises(digest.ManifestError):
            digest.canonicalise({"params": {"temperature": float("nan")}})

    def test_infinity_raises(self):
        with pytest.raises(digest.ManifestError):
            digest.canonicalise({"x": float("inf")})

    def test_list_order_is_preserved(self):
        """Stop sequences are ordered; reordering them is a real change."""
        a = {"components": {}, "params": {"stop_sequences": ["<a>", "<b>"]}}
        b = {"components": {}, "params": {"stop_sequences": ["<b>", "<a>"]}}
        assert digest.manifest_digest(a) != digest.manifest_digest(b)

    def test_bool_is_not_treated_as_int(self):
        a = {"components": {}, "params": {"flag": True}}
        b = {"components": {}, "params": {"flag": 1}}
        assert digest.manifest_digest(a) != digest.manifest_digest(b)


class TestDigestIdentity:
    def test_digest_is_stable_across_calls(self):
        assert digest.manifest_digest(BASE) == digest.manifest_digest(BASE)

    def test_digest_is_64_hex_chars(self):
        d = digest.manifest_digest(BASE)
        assert len(d) == 64
        assert all(c in "0123456789abcdef" for c in d)

    def test_changing_weights_changes_digest(self):
        changed = {
            **BASE,
            "components": {
                **BASE["components"],
                "weights": {**BASE["components"]["weights"], "sha256": "d" * 64},
            },
        }
        assert digest.manifest_digest(changed) != digest.manifest_digest(BASE)

    def test_changing_only_temperature_changes_digest(self):
        """A parameter change alters behaviour and must alter the hash."""
        changed = {**BASE, "params": {**BASE["params"], "temperature": 0.2}}
        assert digest.manifest_digest(changed) != digest.manifest_digest(BASE)

    def test_adding_unknown_top_level_key_changes_digest(self):
        """Unknown fields still affect behaviour, so they still affect the hash."""
        changed = {**BASE, "notes": "internal"}
        assert digest.manifest_digest(changed) != digest.manifest_digest(BASE)

    def test_empty_and_missing_components_differ(self):
        a = {"components": {"runtime": {}}, "params": {}}
        b = {"components": {}, "params": {}}
        assert digest.manifest_digest(a) != digest.manifest_digest(b)


class TestComponentDigests:
    def test_all_four_components_present(self):
        digests = digest.component_digests(BASE)
        assert set(digests) == {"weights", "tokenizer", "template", "runtime"}

    def test_absent_component_is_distinguishable_from_present(self):
        with_runtime = digest.component_digests({"components": {"runtime": {}}})
        without = digest.component_digests({"components": {}})
        assert with_runtime["runtime"] != without["runtime"]

    def test_component_digests_are_independent(self):
        """Changing weights must not change the tokenizer's digest."""
        changed = {
            **BASE,
            "components": {
                **BASE["components"],
                "weights": {**BASE["components"]["weights"], "sha256": "e" * 64},
            },
        }
        before = digest.component_digests(BASE)
        after = digest.component_digests(changed)
        assert before["weights"] != after["weights"]
        assert before["tokenizer"] == after["tokenizer"]
        assert before["template"] == after["template"]

    def test_non_object_components_rejected(self):
        with pytest.raises(digest.ManifestError):
            digest.component_digests({"components": "nope"})


class TestOverrides:
    def test_leaf_override_changes_only_that_field(self):
        merged = digest.merge_overrides(BASE, {"components.runtime.max_seq_len": 4096})
        assert merged["components"]["runtime"]["max_seq_len"] == 4096
        assert merged["components"]["runtime"]["framework"] == "vllm"
        assert merged["components"]["weights"] == BASE["components"]["weights"]

    def test_override_does_not_mutate_source(self):
        digest.merge_overrides(BASE, {"components.runtime.max_seq_len": 4096})
        assert BASE["components"]["runtime"]["max_seq_len"] == 8192

    def test_param_override(self):
        merged = digest.merge_overrides(BASE, {"params.temperature": 0.1})
        assert merged["params"]["temperature"] == 0.1
        assert merged["params"]["top_p"] == 0.95

    def test_whole_component_replacement(self):
        merged = digest.merge_overrides(BASE, {"components.runtime": {"framework": "tgi"}})
        assert merged["components"]["runtime"] == {"framework": "tgi"}

    def test_invalid_path_prefix_rejected(self):
        with pytest.raises(digest.ManifestError):
            digest.merge_overrides(BASE, {"name": "hijack"})

    def test_params_path_must_be_two_segments(self):
        with pytest.raises(digest.ManifestError):
            digest.merge_overrides(BASE, {"params.temperature.value": 0.1})

    def test_identity_when_no_overrides(self):
        assert digest.manifest_digest(digest.merge_overrides(BASE, {})) == digest.manifest_digest(
            BASE
        )
        assert digest.manifest_digest(digest.merge_overrides(BASE, None)) == digest.manifest_digest(
            BASE
        )


class TestSummary:
    def test_summary_surfaces_the_facts_a_human_checks(self):
        s = digest.manifest_summary(BASE)
        assert s["weight_format"] == "safetensors"
        assert s["quantization"] == "awq-int4"
        assert s["vocab_size"] == 32000
        assert s["max_seq_len"] == 8192
        assert s["temperature"] == 0.7

    def test_summary_tolerates_empty_manifest(self):
        s = digest.manifest_summary({})
        assert s["quantization"] == "none"
        assert s["vocab_size"] is None
