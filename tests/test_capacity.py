"""Capacity arithmetic and cost projection."""

from __future__ import annotations

import pytest

from app.engine import capacity


class TestCost:
    def test_cost_uses_730_hours(self):
        # 2 replicas × 4 GPUs × $2.48 × 730 = $14,483.20
        assert capacity.monthly_cost(2, 4, "2.48") == "14483.20"

    def test_cost_is_repeatable(self):
        """A cost that changes when you refresh destroys trust."""
        first = capacity.monthly_cost(3, 2, "2.61")
        second = capacity.monthly_cost(3, 2, "2.61")
        assert first == second

    def test_float_rate_does_not_leak_binary_noise(self):
        """2.48 as a float must not become 2.4800000000000004."""
        assert capacity.monthly_cost(1, 1, 2.48) == "1810.40"

    def test_zero_replicas_costs_zero(self):
        assert capacity.monthly_cost(0, 4, "2.48") == "0.00"

    def test_zero_gpus_costs_zero(self):
        assert capacity.monthly_cost(2, 0, "2.48") == "0.00"

    def test_negative_replicas_rejected(self):
        with pytest.raises(ValueError):
            capacity.monthly_cost(-1, 2, "2.48")

    def test_negative_rate_rejected(self):
        with pytest.raises(ValueError):
            capacity.monthly_cost(1, 1, "-1")

    def test_two_decimals_always(self):
        assert capacity.monthly_cost(1, 1, "1").endswith(".00")


class TestQuota:
    def test_fits_when_room_available(self):
        r = capacity.fits(64, 12, 8)
        assert r["ok"] is True
        assert r["available"] == 52
        assert r["shortfall"] == 0

    def test_exact_fit_is_allowed(self):
        r = capacity.fits(10, 6, 4)
        assert r["ok"] is True
        assert r["available"] == 4

    def test_over_quota_reports_shortfall(self):
        r = capacity.fits(10, 8, 5)
        assert r["ok"] is False
        assert r["shortfall"] == 3

    def test_over_used_quota_has_zero_available(self):
        """A region already oversubscribed must not report negative room."""
        r = capacity.fits(10, 14, 1)
        assert r["ok"] is False
        assert r["available"] == 0

    def test_zero_request_always_fits(self):
        assert capacity.fits(0, 0, 0)["ok"] is True

    def test_negative_inputs_rejected(self):
        with pytest.raises(ValueError):
            capacity.fits(-1, 0, 0)


class TestUtilisation:
    def test_half_used(self):
        assert capacity.utilisation(10, 5) == 0.5

    def test_zero_quota_is_zero_not_error(self):
        assert capacity.utilisation(0, 5) == 0.0

    def test_clamped_above_one(self):
        assert capacity.utilisation(10, 30) == 1.0

    def test_negative_used_clamped_to_zero(self):
        assert capacity.utilisation(10, -5) == 0.0


class TestRegionSummary:
    def test_totals_aggregate(self):
        s = capacity.summarise_regions(
            [
                {"name": "us-east", "gpu_quota": 64, "gpu_used": 12, "usd_per_gpu_hour": "2.48"},
                {"name": "eu-west", "gpu_quota": 32, "gpu_used": 8, "usd_per_gpu_hour": "2.61"},
            ]
        )
        assert s["total_quota"] == 96
        assert s["total_used"] == 20
        assert s["total_available"] == 76
        assert s["region_count"] == 2

    def test_per_region_available_never_negative(self):
        s = capacity.summarise_regions(
            [
                {"name": "x", "gpu_quota": 4, "gpu_used": 9, "usd_per_gpu_hour": "1"},
            ]
        )
        assert s["regions"][0]["available"] == 0


class TestFleetCost:
    def test_fleet_total_sums_regions(self):
        result = capacity.fleet_cost(
            [
                {"id": 1, "region": "us-east", "replicas": 1, "gpus_per_replica": 1},
                {"id": 2, "region": "us-east", "replicas": 2, "gpus_per_replica": 1},
            ],
            {"us-east": "2.00"},
        )
        # 3 GPUs × 2.00 × 730 = 4380.00
        assert result["total_monthly_usd"] == "4380.00"
        assert result["by_region"]["us-east"] == "4380.00"

    def test_unpriced_region_is_excluded_not_zeroed(self):
        """Costing an unknown region at zero would understate the bill."""
        result = capacity.fleet_cost(
            [{"id": 7, "region": "mars", "replicas": 4, "gpus_per_replica": 1}],
            {"us-east": "2.00"},
        )
        assert result["total_monthly_usd"] == "0.00"
        assert result["unpriced_deployments"] == ["7"]

    def test_empty_fleet(self):
        result = capacity.fleet_cost([], {})
        assert result["total_monthly_usd"] == "0.00"
        assert result["by_region"] == {}

    def test_hours_per_month_is_stated(self):
        result = capacity.fleet_cost([], {})
        assert result["hours_per_month"] == "730"
