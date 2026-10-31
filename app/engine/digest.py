"""Canonical manifest encoding and deterministic deployment digests.

The whole product rests on one property: the same manifest must produce the
same digest in every process, on every machine, in any key order. That is
harder than `hashlib.sha256(json.dumps(m)).hexdigest()` because:

* Python dicts preserve insertion order, so two logically-identical manifests
  built in different orders serialise differently.
* Floats render with repr-dependent precision, so 0.7 and 0.70 can differ and
  scientific notation can appear at unexpected magnitudes.
* Nested containers hide both problems.

`canonicalise` therefore walks the structure and rebuilds it with sorted keys
and fixed-precision numbers. Everything else in this module is arithmetic on
top of that.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Manifest component names, in the canonical order they appear in reports.
COMPONENTS: tuple[str, ...] = ("weights", "tokenizer", "template", "runtime")

# Sampling parameters tracked at the manifest root.
PARAMS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "repetition_penalty",
    "stop_sequences",
)

# Float precision used when rendering numbers into the canonical form.
# Six places is enough for any sampling parameter and short enough that
# 0.1 + 0.2 style noise cannot leak into a digest.
_FLOAT_PRECISION = 6


class ManifestError(ValueError):
    """Raised when a manifest is structurally unusable for hashing."""


def _round(value: float) -> float:
    """Round to a stable precision, normalising -0.0 to 0.0."""
    rounded = round(float(value), _FLOAT_PRECISION)
    # `-0.0 == 0.0` is True but they render differently in JSON.
    return 0.0 if rounded == 0.0 else rounded


def canonicalise(value: Any) -> Any:
    """Recursively rebuild `value` with sorted keys and fixed-precision floats.

    Lists keep their order — order is meaningful for stop sequences — but every
    dict becomes key-sorted and every float is rounded. Non-finite floats raise,
    because they cannot be serialised consistently.
    """
    if isinstance(value, bool):
        # Must precede the int check: bool is a subclass of int.
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ManifestError(
                "manifest contains a non-finite number (NaN or infinity), "
                "which cannot be canonicalised"
            )
        return _round(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if value is None:
        return None
    if isinstance(value, dict):
        return {
            str(k): canonicalise(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (list, tuple)):
        return [canonicalise(v) for v in value]
    raise ManifestError(f"cannot canonicalise value of type {type(value).__name__}")


def encode(value: Any) -> bytes:
    """Canonical bytes for hashing: sorted keys, no insignificant whitespace."""
    canonical = canonicalise(value)
    return json.dumps(
        canonical,
        separators=(",", ":"),
        ensure_ascii=True,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """SHA-256 hex digest of the canonical encoding of `value`."""
    return hashlib.sha256(encode(value)).hexdigest()


def component_digests(manifest: dict[str, Any]) -> dict[str, str]:
    """Per-component digests, so a report can say *which* piece moved.

    A missing component hashes the same as an empty one only if both are
    genuinely absent — we hash a sentinel so "absent" and "{}" stay distinct.
    """
    out: dict[str, str] = {}
    components = manifest.get("components") or {}
    if not isinstance(components, dict):
        raise ManifestError("manifest.components must be an object")
    for name in COMPONENTS:
        if name in components:
            out[name] = sha256_hex({"component": name, "value": components[name]})
        else:
            out[name] = sha256_hex({"component": name, "absent": True})
    return out


def manifest_digest(manifest: dict[str, Any]) -> str:
    """Whole-manifest digest — the artefact identity.

    Covers components *and* params, because a temperature change alters
    behaviour exactly as much as a weights change does — it just alters it less
    visibly, which is precisely why it needs to be in the hash.

    ``name`` and ``version`` are deliberately excluded. They are registry labels,
    not behaviour: two entries for the same weights must share a digest, or the
    digest stops being an identity and becomes a row id with extra steps.
    """
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be an object")
    components = manifest.get("components") or {}
    params = manifest.get("params") or {}
    if not isinstance(components, dict):
        raise ManifestError("manifest.components must be an object")
    if not isinstance(params, dict):
        raise ManifestError("manifest.params must be an object")

    payload = {
        "components": {k: components[k] for k in COMPONENTS if k in components},
        "params": {k: params[k] for k in PARAMS if k in params},
        # Unknown params still change behaviour, so they must still change the
        # hash. Dropping them would let a team add `min_p` and get a digest that
        # claims two different deployments are identical.
        "extra_params": {k: v for k, v in params.items() if k not in PARAMS},
        # Anything outside components/params/name/version still counts.
        "extra": {
            k: v
            for k, v in manifest.items()
            if k not in ("components", "params", "name", "version")
        },
    }
    return sha256_hex(payload)


def merge_overrides(manifest: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Apply a region override onto a manifest, returning a new manifest.

    Only `components.<name>.<field>` and `params.<name>` paths are addressable.
    A dotted path like ``components.runtime.max_seq_len`` updates one leaf and
    leaves its siblings alone — that is the difference between "this region
    serves a shorter context" and "this region serves a different runtime".
    """
    out = json.loads(json.dumps(manifest))  # deep copy via canonical-safe path
    if not overrides:
        return out

    if not isinstance(overrides, dict):
        raise ManifestError("overrides must be an object")

    for path, new_value in overrides.items():
        if not isinstance(path, str) or not path:
            raise ManifestError("override keys must be non-empty strings")
        parts = path.split(".")

        if parts[0] == "params":
            if len(parts) != 2:
                raise ManifestError(
                    f"invalid override path {path!r}; params overrides are params.<name>"
                )
            out.setdefault("params", {})[parts[1]] = new_value
            continue

        if parts[0] == "components":
            if len(parts) < 2:
                raise ManifestError(
                    f"invalid override path {path!r}; use components.<name>.<field>"
                )
            if len(parts) == 2:
                # Replace the whole component.
                out.setdefault("components", {})[parts[1]] = new_value
                continue
            # Set one leaf, creating intermediate objects as needed.
            cursor = out.setdefault("components", {}).setdefault(parts[1], {})
            if not isinstance(cursor, dict):
                raise ManifestError(f"cannot set {path!r}: components.{parts[1]} is not an object")
            for segment in parts[2:-1]:
                nxt = cursor.setdefault(segment, {})
                if not isinstance(nxt, dict):
                    raise ManifestError(f"cannot set {path!r}: {segment} is not an object")
                cursor = nxt
            cursor[parts[-1]] = new_value
            continue

        raise ManifestError(
            f"invalid override path {path!r}; must start with 'components' or 'params'"
        )

    return out


def digest_short(digest: str, length: int = 12) -> str:
    """Human-facing abbreviation. Never used for comparison."""
    return digest[:length]


def manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """Compact, human-readable facts about a manifest — for UI and reports."""
    components = manifest.get("components") or {}
    params = manifest.get("params") or {}

    weights = components.get("weights") or {}
    tokenizer = components.get("tokenizer") or {}
    runtime = components.get("runtime") or {}

    return {
        "weight_format": weights.get("format"),
        "quantization": weights.get("quantization") or "none",
        "vocab_size": tokenizer.get("vocab_size"),
        "max_seq_len": runtime.get("max_seq_len"),
        "framework": runtime.get("framework"),
        "temperature": params.get("temperature"),
        "component_digests": component_digests(manifest),
    }
