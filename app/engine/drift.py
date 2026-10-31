"""Drift detection: compare two effective manifests and classify the difference.

Severity is ordered because "the weights are different" and "the temperature is
different" are not the same emergency, and a report that lumps them together
teaches operators to ignore it.

The comparison is over *effective* manifests — the manifest after region
overrides are applied — because that is what is actually serving traffic.
Comparing source manifests would hide the overrides you are hunting for.
"""

from __future__ import annotations

from typing import Any

# Ordered most-severe first. The order is the contract: callers rely on
# severity[0] being the worst thing they have to worry about.
SEVERITY_ORDER: tuple[str, ...] = (
    "weights",
    "tokenizer",
    "template",
    "runtime",
    "parameters",
    "version",
)

SEVERITY_LABEL: dict[str, str] = {
    "weights": "Different model binary",
    "tokenizer": "Different tokenizer",
    "template": "Different prompt template",
    "runtime": "Different runtime configuration",
    "parameters": "Different sampling parameters",
    "version": "Different model version",
}


class DriftError(ValueError):
    """Raised when a drift comparison is not possible."""


def _walk(prefix: str, a: Any, b: Any, out: list[dict[str, Any]]) -> None:
    """Collect leaf-level differences between two nested structures."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            _walk(f"{prefix}.{key}" if prefix else str(key), a.get(key), b.get(key), out)
        return

    if isinstance(a, list) and isinstance(b, list):
        if a != b:
            out.append({"field": prefix, "a": a, "b": b})
        return

    # Absent vs present is a real difference and must be reported as one,
    # not silently treated as "both empty".
    if a != b:
        out.append({"field": prefix, "a": a, "b": b})


def _component_section(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    components = manifest.get("components") or {}
    value = components.get(name)
    return value if isinstance(value, dict) else {}


def _params_section(manifest: dict[str, Any]) -> dict[str, Any]:
    params = manifest.get("params") or {}
    return params if isinstance(params, dict) else {}


def classify(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Return the drift classes present between two effective manifests."""
    classes: list[str] = []

    for name in ("weights", "tokenizer", "template", "runtime"):
        if _component_section(a, name) != _component_section(b, name):
            classes.append(name)

    if _params_section(a) != _params_section(b):
        classes.append("parameters")

    # A different declared version is its own signal even when the underlying
    # artefacts happen to be identical — it means a rollout is halfway.
    if a.get("version") != b.get("version"):
        classes.append("version")

    return [c for c in SEVERITY_ORDER if c in classes]


def compare(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    digest_a: str | None = None,
    digest_b: str | None = None,
) -> dict[str, Any]:
    """Full drift report for a pair of effective manifests.

    Returns:
        identical  — True when the digests match (or, absent digests, when the
                     structures are byte-identical after canonicalisation)
        classes    — ordered drift classes, worst first
        severity   — classes[0] or None
        diffs      — field-level differences with both values
        summary    — one plain sentence a human can act on
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        raise DriftError("both manifests must be objects")

    if digest_a is not None and digest_b is not None:
        identical = digest_a == digest_b
    else:
        from .digest import sha256_hex

        identical = sha256_hex(a) == sha256_hex(b)

    diffs: list[dict[str, Any]] = []
    _walk("", a, b, diffs)

    classes = classify(a, b)

    # Digests can agree while structures differ only in keys we do not hash
    # (we hash a fixed component/param set). Trust the digest for identity,
    # but still report structure drift if the caller is looking at raw dicts.
    if identical and not diffs:
        return {
            "identical": True,
            "classes": [],
            "severity": None,
            "diffs": [],
            "summary": "Identical behaviour — same effective manifest, same digest.",
        }

    severity = classes[0] if classes else None

    if severity is None:
        summary = "No behavioural drift detected."
    elif severity == "weights":
        summary = (
            "Different model binary in service. This is the most serious case: "
            "the two deployments are running different weights."
        )
    elif severity == "tokenizer":
        summary = (
            "Different tokenizer. Token boundaries shift, so identical prompts "
            "produce different token ids and therefore different output."
        )
    elif severity == "template":
        summary = "Different prompt template. The model sees a different instruction."
    elif severity == "runtime":
        summary = "Different runtime configuration (framework, opset or context length)."
    elif severity == "parameters":
        summary = "Same model, different sampling parameters."
    else:
        summary = "Different model version declared."

    return {
        "identical": identical and not diffs,
        "classes": classes,
        "severity": severity,
        "diffs": diffs,
        "summary": summary,
    }


def build_matrix(deployments: list[dict[str, Any]]) -> dict[str, Any]:
    """Pairwise parity matrix across a set of deployments.

    Each deployment dict needs: id, region, environment, effective_manifest,
    digest. Returns rows for the UI plus an overall verdict.
    """
    if not deployments:
        return {
            "cells": [],
            "summary": {
                "deployment_count": 0,
                "drifting_pairs": 0,
                "all_identical": True,
                "worst_severity": None,
            },
        }

    cells: list[dict[str, Any]] = []
    drifting = 0
    worst: str | None = None

    for i, left in enumerate(deployments):
        for j, right in enumerate(deployments):
            if j <= i:
                continue
            report = compare(
                left["effective_manifest"],
                right["effective_manifest"],
                digest_a=left["digest"],
                digest_b=right["digest"],
            )
            cell = {
                "a": left["id"],
                "b": right["id"],
                "a_region": left["region"],
                "b_region": right["region"],
                "a_environment": left["environment"],
                "b_environment": right["environment"],
                "identical": report["identical"],
                "classes": report["classes"],
                "severity": report["severity"],
                "diffs": report["diffs"],
                "summary": report["summary"],
            }
            cells.append(cell)
            if not report["identical"]:
                drifting += 1
                severity = report["severity"]
                if severity is not None and (
                    worst is None or SEVERITY_ORDER.index(severity) < SEVERITY_ORDER.index(worst)
                ):
                    worst = severity

    return {
        "cells": cells,
        "summary": {
            "deployment_count": len(deployments),
            "pair_count": len(cells),
            "drifting_pairs": drifting,
            "all_identical": drifting == 0,
            "worst_severity": worst,
        },
    }
