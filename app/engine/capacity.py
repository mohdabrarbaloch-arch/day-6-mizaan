"""Capacity arithmetic and cost projection.

Money is handled with ``Decimal`` and returned as a quantised string. Floats
would let two identical calls disagree in the last digit, and a cost report that
changes when you refresh it destroys trust faster than a wrong number does.

Hours in a month is fixed at 730 (365 × 24 / 12) rather than "30 days", so a
projection is reproducible and comparable across regions.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

HOURS_PER_MONTH = Decimal("730")
CENT = Decimal("0.01")


def monthly_cost(replicas: int, gpus_per_replica: int, usd_per_gpu_hour: str | float) -> str:
    """Projected monthly cost as a 2dp string.

    ``usd_per_gpu_hour`` is accepted as a string or number and converted to
    Decimal via its string form, so 2.48 passed as a float still yields 2.48
    and not 2.4800000000000004.
    """
    if replicas < 0:
        raise ValueError("replicas cannot be negative")
    if gpus_per_replica < 0:
        raise ValueError("gpus_per_replica cannot be negative")
    rate = Decimal(str(usd_per_gpu_hour))
    if rate < 0:
        raise ValueError("rate cannot be negative")
    total = Decimal(replicas) * Decimal(gpus_per_replica) * rate * HOURS_PER_MONTH
    return str(total.quantize(CENT, rounding=ROUND_HALF_UP))


def gpus_requested(replicas: int, gpus_per_replica: int) -> int:
    """Total GPUs a deployment holds."""
    if replicas < 0 or gpus_per_replica < 0:
        raise ValueError("replicas and gpus_per_replica cannot be negative")
    return replicas * gpus_per_replica


def fits(quota: int, used: int, requested: int) -> dict[str, Any]:
    """Whether `requested` GPUs fit in the remaining quota.

    Returns a structured result rather than a bare bool so the API can put the
    numbers in the 409 body — "quota exceeded" without the arithmetic is a
    support ticket waiting to happen.
    """
    if quota < 0 or used < 0 or requested < 0:
        raise ValueError("quota, used and requested cannot be negative")
    available = max(quota - used, 0)
    ok = requested <= available
    return {
        "ok": ok,
        "quota": quota,
        "used": used,
        "available": available,
        "requested": requested,
        "shortfall": 0 if ok else requested - available,
    }


def utilisation(quota: int, used: int) -> float:
    """Fraction of quota in use, clamped to [0, 1] for display."""
    if quota <= 0:
        return 0.0
    return min(max(used / quota, 0.0), 1.0)


def summarise_regions(regions: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a region list into totals for the dashboard header.

    Each region dict carries: name, gpu_quota, gpu_used, usd_per_gpu_hour.
    """
    total_quota = sum(int(r.get("gpu_quota", 0)) for r in regions)
    total_used = sum(int(r.get("gpu_used", 0)) for r in regions)
    return {
        "region_count": len(regions),
        "total_quota": total_quota,
        "total_used": total_used,
        "total_available": max(total_quota - total_used, 0),
        "utilisation": utilisation(total_quota, total_used),
        "regions": [
            {
                "name": r.get("name"),
                "gpu_quota": int(r.get("gpu_quota", 0)),
                "gpu_used": int(r.get("gpu_used", 0)),
                "available": max(int(r.get("gpu_quota", 0)) - int(r.get("gpu_used", 0)), 0),
                "utilisation": utilisation(int(r.get("gpu_quota", 0)), int(r.get("gpu_used", 0))),
                "usd_per_gpu_hour": str(r.get("usd_per_gpu_hour", "0")),
            }
            for r in regions
        ],
    }


def fleet_cost(deployments: list[dict[str, Any]], rates: dict[str, str]) -> dict[str, Any]:
    """Total monthly cost across deployments, grouped by region.

    Deployments that name a region with no rate are reported as unpriced rather
    than costed at zero — a zero would understate the bill and look like a
    working number.
    """
    by_region: dict[str, Decimal] = {}
    unpriced: list[str] = []
    total = Decimal("0")

    for dep in deployments:
        region = dep.get("region")
        rate = rates.get(region)
        if rate is None:
            dep_id = dep.get("id")
            if dep_id is not None:
                unpriced.append(str(dep_id))
            continue
        cost = Decimal(
            monthly_cost(
                int(dep.get("replicas", 0)),
                int(dep.get("gpus_per_replica", 0)),
                rate,
            )
        )
        by_region[region] = by_region.get(region, Decimal("0")) + cost
        total += cost

    return {
        "total_monthly_usd": str(total.quantize(CENT, rounding=ROUND_HALF_UP)),
        "by_region": {
            region: str(value.quantize(CENT, rounding=ROUND_HALF_UP))
            for region, value in sorted(by_region.items())
        },
        "unpriced_deployments": unpriced,
        "deployment_count": len(deployments),
        "hours_per_month": str(HOURS_PER_MONTH),
    }
