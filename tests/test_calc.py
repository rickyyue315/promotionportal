"""Tests for the pure calculation core."""

import math

import pytest

from app.calc import (
    COVERAGE_SHORT,
    COVERAGE_SUFFICIENT,
    SkuTarget,
    apportion,
    compare_scenarios,
    compute_all_scenarios,
    compute_scenario,
    normalise_shares,
    round_up_to,
)


class TestApportion:
    def test_sums_exactly_to_total(self):
        shares = [1 / 3, 1 / 3, 1 / 3]
        out = apportion(100, shares)
        assert sum(out) == 100

    def test_largest_remainder_goes_to_biggest_fraction(self):
        # 100 split 50/30/20 -> exact, no residual
        assert apportion(100, [0.5, 0.3, 0.2]) == [50, 30, 20]

    def test_residual_distributed_not_dropped(self):
        # 10 split 3 ways: 3.33/3.33/3.33 -> 4/3/3
        out = apportion(10, [1 / 3, 1 / 3, 1 / 3])
        assert sum(out) == 10
        assert max(out) - min(out) <= 1

    def test_zero_total(self):
        assert apportion(0, [0.5, 0.5]) == [0, 0]

    def test_zero_shares(self):
        assert apportion(50, [0.0, 0.0]) == [0, 0]

    def test_single_shop_gets_everything(self):
        assert apportion(777, [1.0]) == [777]

    def test_skewed_shares(self):
        out = apportion(1000, [0.999, 0.001])
        assert sum(out) == 1000
        assert out == [999, 1]


class TestRoundUpTo:
    @pytest.mark.parametrize(
        "qty,moq,expected",
        [
            (0, 12, 0),
            (-5, 12, 0),
            (1, 12, 12),
            (12, 12, 12),
            (13, 12, 24),
            (24, 12, 24),
            (7, 1, 7),
            (7, 0, 7),
            (7.2, 1, 8),
        ],
    )
    def test_cases(self, qty, moq, expected):
        assert round_up_to(qty, moq) == expected


class TestNormaliseShares:
    def test_exact(self):
        shares, total, warn = normalise_shares(["A", "B"], {"A": 0.6, "B": 0.4})
        assert shares == [0.6, 0.4]
        assert total == pytest.approx(1.0)
        assert warn == []

    def test_normalises_and_warns(self):
        shares, total, warn = normalise_shares(["A", "B"], {"A": 0.6, "B": 0.6})
        assert total == pytest.approx(1.2)
        assert shares == [pytest.approx(0.5), pytest.approx(0.5)]
        assert any("正規化" in w for w in warn)

    def test_missing_shop_is_zero(self):
        shares, _, _ = normalise_shares(["A", "B", "C"], {"A": 1.0})
        assert shares == [1.0, 0.0, 0.0]

    def test_negative_treated_as_zero(self):
        shares, _, _ = normalise_shares(["A", "B"], {"A": 1.2, "B": -0.2})
        assert shares[1] == 0.0

    def test_all_zero_warns(self):
        shares, total, warn = normalise_shares(["A", "B"], {})
        assert shares == [0.0, 0.0]
        assert total == 0.0
        assert warn


class TestComputeScenario:
    def _run(self, targets, stock, shares=(0.5, 0.5)):
        shops = ["S1", "S2"]
        return compute_scenario(
            "A", shops, list(shares), targets, stock
        )

    def test_basic_dispatch(self):
        # target 100, split 50/50, stock 10/80 -> need 40 / 0
        res = self._run(
            [SkuTarget("SKU1", 100)],
            {"SKU1": {"S1": 10, "S2": 80}},
        )
        line = {(l.shop_code): l for l in res.lines}
        assert line["S1"].alloc_qty == 50
        assert line["S1"].dispatch_qty == 40
        assert line["S2"].alloc_qty == 50
        assert line["S2"].dispatch_qty == 0
        assert line["S2"].surplus_qty == 30

        sku = res.skus[0]
        assert sku.total_stock == 90
        assert sku.total_dispatch == 40
        assert sku.shortfall_qty == 10
        assert sku.coverage_status == COVERAGE_SHORT

    def test_enough_but_still_needs_transfer(self):
        """The key business distinction: goods exist, just in the wrong shop."""
        res = self._run(
            [SkuTarget("SKU1", 100)],
            {"SKU1": {"S1": 0, "S2": 200}},
        )
        sku = res.skus[0]
        assert sku.total_stock == 200
        assert sku.shortfall_qty == 0
        assert sku.coverage_status == COVERAGE_SUFFICIENT
        assert sku.total_dispatch == 50  # S1 still needs 50 moved in
        assert sku.surplus_qty == 150  # S2 can give away 150

    def test_moq_rounds_dispatch_up(self):
        res = compute_scenario(
            "A",
            ["S1"],
            [1.0],
            [SkuTarget("SKU1", 100, moq=12)],
            {"SKU1": {"S1": 5}},
        )
        # gap = 95 -> ceil(95/12)*12 = 96
        assert res.lines[0].dispatch_qty == 96

    def test_moq_does_not_apply_when_no_gap(self):
        res = compute_scenario(
            "A", ["S1"], [1.0], [SkuTarget("SKU1", 10, moq=12)], {"SKU1": {"S1": 50}}
        )
        assert res.lines[0].dispatch_qty == 0

    def test_alloc_always_sums_to_target(self):
        targets = [SkuTarget("SKU1", 833), SkuTarget("SKU2", 7)]
        res = compute_scenario(
            "A",
            ["S1", "S2", "S3"],
            [0.333, 0.333, 0.334],
            targets,
            {},
        )
        for sku in res.skus:
            assert sku.total_alloc == sku.target_qty

    def test_missing_stock_row_counts_as_zero(self):
        res = compute_scenario("A", ["S1"], [1.0], [SkuTarget("SKU1", 10)], {})
        assert res.lines[0].stock_qty == 0
        assert res.lines[0].dispatch_qty == 10
        assert res.skus[0].shortfall_qty == 10

    def test_unknown_shop_in_stock_is_ignored_with_warning(self):
        res = compute_scenario(
            "A", ["S1"], [1.0], [SkuTarget("SKU1", 10)], {"SKU1": {"ZZ9": 99}}
        )
        assert res.lines[0].stock_qty == 0
        assert any("未在店舖主檔" in w for w in res.warnings)


class TestMultiScenario:
    def test_three_scenarios_share_same_target(self):
        shops = ["S1", "S2"]
        scenarios = {
            "A": {"S1": 0.8, "S2": 0.2},
            "B": {"S1": 0.5, "S2": 0.5},
            "C": {"S1": 0.2, "S2": 0.8},
        }
        stock = {"SKU1": {"S1": 30, "S2": 30}}
        res = compute_all_scenarios(scenarios, shops, [SkuTarget("SKU1", 100)], stock)

        assert set(res) == {"A", "B", "C"}
        assert res["A"].skus[0].total_alloc == 100
        assert res["A"].lines[0].alloc_qty == 80
        assert res["C"].lines[0].alloc_qty == 20
        # stock is the same, so shortfall is identical across scenarios
        assert {r.skus[0].shortfall_qty for r in res.values()} == {40}

    def test_compare_rows_sorted_by_shortfall(self):
        shops = ["S1"]
        scenarios = {"A": {"S1": 1.0}}
        targets = [SkuTarget("OK", 10), SkuTarget("BAD", 100)]
        stock = {"OK": {"S1": 50}, "BAD": {"S1": 0}}
        res = compute_all_scenarios(scenarios, shops, targets, stock)
        rows = compare_scenarios(res)
        assert [r["sku"] for r in rows] == ["BAD", "OK"]
