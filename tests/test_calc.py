"""Tests for the pure calculation core."""

import math
from datetime import date

import pytest

from app.calc import (
    COVERAGE_SHORT,
    COVERAGE_SUFFICIENT,
    PromoWindow,
    ShopSales,
    SkuTarget,
    apportion,
    cap_to_available,
    compare_scenarios,
    compute_all_scenarios,
    compute_scenario,
    daily_sales_rate,
    dispatch_batches,
    normalise_shares,
    round_up_to,
    sales_window_start,
    sku_moq,
    split_shared_targets,
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


class TestSkuMoq:
    def test_missing_map_means_one(self):
        assert sku_moq(None, "SKU1") == 1
        assert sku_moq({}, "SKU1") == 1

    def test_unknown_sku_means_one(self):
        assert sku_moq({"SKU2": 12}, "SKU1") == 1

    def test_blank_or_zero_means_one(self):
        assert sku_moq({"SKU1": 0}, "SKU1") == 1
        assert sku_moq({"SKU1": ""}, "SKU1") == 1

    def test_value_is_read_from_the_stock_map(self):
        assert sku_moq({"SKU1": 12}, "SKU1") == 12
        assert sku_moq({"SKU1": "6"}, "SKU1") == 6


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
            [SkuTarget("SKU1", 100)],
            {"SKU1": {"S1": 5}},
            moq_by_sku={"SKU1": 12},
        )
        # gap = 95 -> ceil(95/12)*12 = 96
        assert res.lines[0].dispatch_qty == 96
        assert res.skus[0].moq == 12

    def test_moq_does_not_apply_when_no_gap(self):
        res = compute_scenario(
            "A",
            ["S1"],
            [1.0],
            [SkuTarget("SKU1", 10)],
            {"SKU1": {"S1": 50}},
            moq_by_sku={"SKU1": 12},
        )
        assert res.lines[0].dispatch_qty == 0

    def test_without_moq_nothing_is_rounded(self):
        """No MOQ in the stock file: the need is exactly the gap."""
        res = compute_scenario(
            "A", ["S1"], [1.0], [SkuTarget("SKU1", 100)], {"SKU1": {"S1": 5}}
        )
        assert res.lines[0].dispatch_qty == 95
        assert res.skus[0].moq == 1

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


class TestRpGate:
    """HB87 can only receive stock when its RP Type is RF."""

    def _run(self, rp, stock=None, shops=("HB87", "HA01"), qty=100):
        return compute_scenario(
            "A",
            list(shops),
            [0.5, 0.5],
            [SkuTarget("S1", qty)],
            stock or {"S1": {"HB87": 0, "HA01": 0}},
            rp_by_sku={"S1": {"HB87": rp}} if rp is not None else {},
            rp_gate_shops=["HB87"],
        )

    def test_nd_blocks_dispatch_and_keeps_the_gap(self):
        res = self._run("ND")
        line = {l.shop_code: l for l in res.lines}
        assert line["HB87"].alloc_qty == 50
        assert line["HB87"].gap_qty == 50
        assert line["HB87"].dispatch_qty == 0
        assert line["HB87"].blocked is True
        assert line["HB87"].blocked_qty == 50
        assert line["HB87"].rp_type == "ND"

    def test_rf_allows_dispatch(self):
        res = self._run("RF")
        line = {l.shop_code: l for l in res.lines}
        assert line["HB87"].dispatch_qty == 50
        assert line["HB87"].blocked is False

    def test_missing_rp_type_blocks(self):
        res = self._run(None)
        line = {l.shop_code: l for l in res.lines}
        assert line["HB87"].dispatch_qty == 0
        assert line["HB87"].blocked is True
        assert line["HB87"].rp_type == ""

    def test_other_shops_ignore_rp_type(self):
        # HA01 also reports ND, but it is not a gated shop, so it still gets goods.
        res = compute_scenario(
            "A",
            ["HB87", "HA01"],
            [0.5, 0.5],
            [SkuTarget("S1", 100)],
            {"S1": {"HB87": 0, "HA01": 0}},
            rp_by_sku={"S1": {"HB87": "ND", "HA01": "ND"}},
            rp_gate_shops=["HB87"],
        )
        line = {l.shop_code: l for l in res.lines}
        assert line["HB87"].dispatch_qty == 0
        assert line["HA01"].dispatch_qty == 50
        assert res.skus[0].total_dispatch == 50

    def test_no_gap_is_not_reported_as_blocked(self):
        res = compute_scenario(
            "A",
            ["HB87"],
            [1.0],
            [SkuTarget("S1", 10)],
            {"S1": {"HB87": 50}},
            rp_by_sku={"S1": {"HB87": "ND"}},
            rp_gate_shops=["HB87"],
        )
        assert res.lines[0].dispatch_qty == 0
        assert res.lines[0].blocked is False

    def test_warning_reports_the_lost_units(self):
        res = self._run("ND")
        assert any("RP Type" in w and "50" in w for w in res.warnings)

    def test_gate_applies_in_every_scenario(self):
        res = compute_all_scenarios(
            {"A": {"HB87": 0.5, "HA01": 0.5}, "B": {"HB87": 0.0, "HA01": 1.0}},
            ["HB87", "HA01"],
            [SkuTarget("S1", 100)],
            {"S1": {"HB87": 0, "HA01": 0}},
            rp_by_sku={"S1": {"HB87": "ND"}},
            rp_gate_shops=["HB87"],
        )
        assert res["A"].lines[0].dispatch_qty == 0
        assert res["A"].skus[0].total_dispatch == 50
        assert res["B"].skus[0].total_dispatch == 100


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


class TestPromoWindow:
    def test_days_are_inclusive(self):
        w = PromoWindow(start=date(2026, 11, 2), end=date(2026, 11, 15))
        assert w.days == 14
    def test_single_day_promotion(self):
        w = PromoWindow(start=date(2026, 11, 2), end=date(2026, 11, 2))
        assert w.days == 1
        assert w.effective_dispatch_days == 1

    def test_target_scaled_to_dispatch_window(self):
        # 14-day promotion, 4 days of stock: 1400 -> 400
        w = PromoWindow(start=date(2026, 11, 2), end=date(2026, 11, 15), dispatch_days=4)
        assert w.dispatch_target(1400) == 400

    def test_short_promotion_is_not_scaled_down(self):
        w = PromoWindow(start=date(2026, 11, 2), end=date(2026, 11, 3), dispatch_days=4)
        assert w.days == 2
        assert w.effective_dispatch_days == 2
        assert w.dispatch_target(1400) == 1400

    def test_scaled_target_never_rounds_to_zero(self):
        w = PromoWindow(start=date(2026, 11, 2), end=date(2026, 11, 30), dispatch_days=1)
        assert w.dispatch_target(10) == 1

    def test_zero_target_stays_zero(self):
        w = PromoWindow(start=date(2026, 11, 2), end=date(2026, 11, 15), dispatch_days=4)
        assert w.dispatch_target(0) == 0


class TestSalesRate:
    def test_window_starts_first_of_last_month(self):
        assert sales_window_start(date(2026, 11, 12)) == date(2026, 10, 1)

    def test_launch_date_wins_when_later(self):
        # A product launched last week must not be diluted by a month of zeroes.
        assert sales_window_start(date(2026, 11, 12), date(2026, 11, 10)) == date(
            2026, 11, 10
        )

    def test_launch_date_before_window_is_ignored(self):
        assert sales_window_start(date(2026, 11, 12), date(2025, 1, 1)) == date(
            2026, 10, 1
        )

    def test_rate_uses_both_months_over_days_on_record(self):
        # 1 Oct -> 12 Nov inclusive = 43 days; (300 + 240) / 43
        sales = ShopSales(last_month_sold=300, mtd_sold=240)
        assert daily_sales_rate(sales, date(2026, 11, 12)) == pytest.approx(540 / 43)

    def test_new_product_not_diluted(self):
        # Launched 8 Nov, 4 days on record, 40 sold -> 10/day not 540/43.
        sales = ShopSales(last_month_sold=0, mtd_sold=40, launch_date=date(2026, 11, 8))
        assert daily_sales_rate(sales, date(2026, 11, 12)) == pytest.approx(8.0)

    def test_no_as_of_means_no_rate(self):
        assert daily_sales_rate(ShopSales(mtd_sold=40), None) == 0.0

    def test_negative_sales_clamped(self):
        sales = ShopSales(last_month_sold=-50, mtd_sold=43)
        assert daily_sales_rate(sales, date(2026, 11, 12)) >= 0


class TestCapToAvailable:
    def test_no_cap_when_enough(self):
        assert cap_to_available([10, 20], 100) == [10, 20]

    def test_shares_scarce_units_by_need(self):
        # 100 available against 300 of need -> a third each, 10/20/70 -> 10/20/30
        assert cap_to_available([100, 200], 100) == pytest.approx([33, 67], abs=1)

    def test_never_exceeds_available(self):
        out = cap_to_available([90, 90, 90], 100)
        assert sum(out) <= 100

    def test_nothing_available_sends_nothing(self):
        assert cap_to_available([10, 20], 0) == [0, 0]

    def test_respects_moq_steps(self):
        out = cap_to_available([10, 10], 6, moq=3)
        assert all(v % 3 == 0 for v in out)

    def test_never_gives_more_than_asked(self):
        out = cap_to_available([3, 100], 50)
        assert out[0] <= 3


class TestPromoAndWarehouseInScenario:
    SHOPS = ["S1", "S2"]
    SHARES = [0.5, 0.5]

    def test_dispatch_uses_scaled_target(self):
        promo = PromoWindow(
            start=date(2026, 11, 2), end=date(2026, 11, 15), dispatch_days=4
        )
        res = compute_scenario(
            "A", self.SHOPS, self.SHARES, [SkuTarget("K", 1400)], {}, promo=promo
        )
        s = res.skus[0]
        assert s.target_qty == 1400
        assert s.dispatch_target == 400
        assert s.total_alloc == 400
        assert [ln.alloc_qty for ln in res.lines] == [200, 200]

    def test_pending_received_reduces_the_gap(self):
        promo = PromoWindow(
            start=date(2026, 11, 2), end=date(2026, 11, 15), dispatch_days=4
        )
        sales = {"K": {"S1": ShopSales(pending_qty=60), "S2": ShopSales()}}
        res = compute_scenario(
            "A",
            self.SHOPS,
            self.SHARES,
            [SkuTarget("K", 1400)],
            {"K": {"S1": 0, "S2": 0}},
            promo=promo,
            sales_by_sku=sales,
            as_of=date(2026, 11, 12),
        )
        by_shop = {ln.shop_code: ln for ln in res.lines}
        assert by_shop["S1"].need_qty == 140  # 200 - 60 in transit
        assert by_shop["S2"].need_qty == 200

    def test_warehouse_caps_total_dispatch(self):
        res = compute_scenario(
            "A",
            self.SHOPS,
            self.SHARES,
            [SkuTarget("K", 200)],
            {},
            warehouse_by_sku={"K": 120},
        )
        s = res.skus[0]
        assert s.need_qty == 200
        assert s.total_dispatch == 120
        assert s.short_qty == 80
        assert s.warehouse_qty == 120

    def test_warehouse_covering_need_sets_no_shortage(self):
        res = compute_scenario(
            "A",
            self.SHOPS,
            self.SHARES,
            [SkuTarget("K", 200)],
            {},
            warehouse_by_sku={"K": 500},
        )
        assert res.skus[0].total_dispatch == 200
        assert res.skus[0].short_qty == 0

    def test_gate_units_are_not_charged_against_the_warehouse(self):
        # HB87 blocked, S2 still gets its full need: blocked units must not eat
        # the available stock.
        res = compute_scenario(
            "A",
            ["HB87", "S2"],
            [0.5, 0.5],
            [SkuTarget("K", 200)],
            {},
            rp_by_sku={"K": {"HB87": "ND"}},
            rp_gate_shops=["HB87"],
            warehouse_by_sku={"K": 100},
        )
        by_shop = {ln.shop_code: ln for ln in res.lines}
        assert by_shop["HB87"].dispatch_qty == 0
        assert by_shop["S2"].dispatch_qty == 100
        assert res.skus[0].short_qty == 0

    def test_coverage_counts_warehouse_stock(self):
        # Shops hold 60, warehouse 40, target 100 -> covered.
        res = compute_scenario(
            "A",
            self.SHOPS,
            self.SHARES,
            [SkuTarget("K", 100)],
            {"K": {"S1": 30, "S2": 30}},
            warehouse_by_sku={"K": 40},
        )
        assert res.skus[0].enough
        assert res.skus[0].shortfall_qty == 0

    def test_missing_warehouse_row_warns_and_skips_cap(self):
        res = compute_scenario(
            "A",
            self.SHOPS,
            self.SHARES,
            [SkuTarget("K", 200)],
            {},
            warehouse_by_sku={},
        )
        assert res.skus[0].total_dispatch == 200
        assert any("貨倉沒有存貨紀錄" in w for w in res.warnings)

    def test_expected_sales_and_target_gap(self):
        promo = PromoWindow(
            start=date(2026, 11, 2), end=date(2026, 11, 15), dispatch_days=4
        )
        # 43 days on record, 86 sold -> 2/day per shop, 4/day total.
        sales = {
            "K": {
                "S1": ShopSales(last_month_sold=43, mtd_sold=43),
                "S2": ShopSales(last_month_sold=43, mtd_sold=43),
            }
        }
        res = compute_scenario(
            "A",
            self.SHOPS,
            self.SHARES,
            [SkuTarget("K", 1400)],
            {},
            promo=promo,
            sales_by_sku=sales,
            as_of=date(2026, 11, 12),
        )
        s = res.skus[0]
        assert s.expected_sales == 56  # 4/day * 14 days
        assert s.gap_target == 1344

    def test_no_sales_data_leaves_the_gap_blank(self):
        res = compute_scenario(
            "A", self.SHOPS, self.SHARES, [SkuTarget("K", 100)], {}
        )
        assert res.skus[0].expected_sales == 0
        assert res.skus[0].gap_target == 0


class TestDispatchBatches:
    def test_splits_a_fortnight_into_four_day_runs(self):
        w = PromoWindow(start=date(2026, 11, 16), end=date(2026, 11, 29), dispatch_days=4)
        assert dispatch_batches(w) == [
            (1, date(2026, 11, 16), date(2026, 11, 19)),
            (2, date(2026, 11, 20), date(2026, 11, 23)),
            (3, date(2026, 11, 24), date(2026, 11, 27)),
            (4, date(2026, 11, 28), date(2026, 11, 29)),  # short tail
        ]

    def test_batches_cover_every_day_exactly_once(self):
        w = PromoWindow(start=date(2026, 11, 1), end=date(2026, 11, 30), dispatch_days=7)
        days = []
        for _idx, start, end in dispatch_batches(w):
            days.extend((end - start).days + 1 for _ in [0])
        assert sum(days) == 30

    def test_dispatch_longer_than_promotion_gives_one_batch(self):
        w = PromoWindow(start=date(2026, 11, 16), end=date(2026, 11, 18), dispatch_days=4)
        assert dispatch_batches(w) == [(1, date(2026, 11, 16), date(2026, 11, 18))]

    def test_empty_window_has_no_batches(self):
        w = PromoWindow(start=date(2026, 11, 18), end=date(2026, 11, 16), dispatch_days=4)
        assert dispatch_batches(w) == []


class TestSharedTarget:
    """One 4,000 written over two SKUs, as the target file often does."""

    A = "111058302001"
    B = "111058302002"

    def targets(self):
        return [SkuTarget(self.A, 4000, group=self.A), SkuTarget(self.B, 0, group=self.A)]

    def sales(self):
        return {
            self.A: {"S1": ShopSales(last_month_sold=800, mtd_sold=200)},
            self.B: {"S1": ShopSales(last_month_sold=200, mtd_sold=50)},
        }

    def test_split_uses_the_sales_mix(self):
        out, notes = split_shared_targets(self.targets(), sales_by_sku=self.sales())
        assert [t.target_qty for t in out] == [3200, 800]
        assert sum(t.target_qty for t in out) == 4000
        assert "銷量比例" in notes[0]
        assert self.A in notes[0] and self.B in notes[0]

    def test_split_falls_back_to_stock_when_there_is_no_sales_data(self):
        out, notes = split_shared_targets(
            self.targets(), stock_by_sku={self.A: {"S1": 5000}, self.B: {"S1": 1000}}
        )
        assert [t.target_qty for t in out] == [3333, 667]
        assert "庫存" in notes[0]

    def test_split_falls_back_to_even_when_there_is_nothing_to_go_on(self):
        out, notes = split_shared_targets(self.targets())
        assert [t.target_qty for t in out] == [2000, 2000]
        assert "平均" in notes[0]

    def test_a_standalone_target_is_left_alone(self):
        out, notes = split_shared_targets([SkuTarget("X", 10)])
        assert out[0].target_qty == 10 and out[0].group == ""
        assert notes == []

    def test_group_stock_covers_every_member(self):
        targets, _ = split_shared_targets(self.targets(), sales_by_sku=self.sales())
        res = compute_scenario(
            "A",
            ["S1"],
            [1.0],
            targets,
            {self.A: {"S1": 5000}, self.B: {"S1": 1000}},
        )
        assert all(s.enough for s in res.skus)
        assert res.n_short == 0
        assert sum(s.shortfall_qty for s in res.skus) == 0

    def test_group_shortfall_is_reported_once(self):
        targets, _ = split_shared_targets(self.targets(), sales_by_sku=self.sales())
        res = compute_scenario(
            "A", ["S1"], [1.0], targets, {self.A: {"S1": 300}, self.B: {"S1": 100}}
        )
        assert res.n_short == 1
        assert sum(s.shortfall_qty for s in res.skus) == 3600
        assert next(s for s in res.skus if s.sku == self.A).shortfall_qty == 3600

    def test_dispatch_is_capped_by_what_the_group_lacks(self):
        # The shop holds 3,500 of the 7PC and none of the 14PC. The 14PC's own
        # share is short, but the group only lacks 500 — sending 800 would
        # duplicate what the 7PC already covers.
        targets, _ = split_shared_targets(self.targets(), sales_by_sku=self.sales())
        res = compute_scenario(
            "A", ["S1"], [1.0], targets, {self.A: {"S1": 3500}, self.B: {}}
        )
        assert sum(line.dispatch_qty for line in res.lines) == 500
        assert sum(s.need_qty for s in res.skus) == 500

    def test_a_covered_group_asks_for_nothing_even_when_one_pack_is_missing(self):
        targets, _ = split_shared_targets(self.targets(), sales_by_sku=self.sales())
        res = compute_scenario(
            "A", ["S1"], [1.0], targets, {self.A: {"S1": 4000}, self.B: {}}
        )
        assert sum(line.dispatch_qty for line in res.lines) == 0
        assert all(line.short_qty == 0 for line in res.lines)

    def test_compare_shows_one_row_for_the_group(self):
        targets, _ = split_shared_targets(self.targets(), sales_by_sku=self.sales())
        res = compute_scenario(
            "A", ["S1"], [1.0], targets, {self.A: {"S1": 3500}, self.B: {}}
        )
        rows = compare_scenarios({"A": res})
        assert len(rows) == 1
        row = rows[0]
        assert row["group_size"] == 2
        assert row["target_qty"] == 4000
        assert row["total_stock"] == 3500
        assert row["sku"] == f"{self.A} + {self.B}"

    def test_members_keep_their_own_moq_when_the_group_is_capped(self):
        targets, _ = split_shared_targets(self.targets(), sales_by_sku=self.sales())
        res = compute_scenario(
            "A",
            ["S1"],
            [1.0],
            targets,
            {self.A: {"S1": 3500}, self.B: {}},
            moq_by_sku={self.B: 6},
        )
        dispatched = sum(line.dispatch_qty for line in res.lines)
        assert dispatched % 6 == 0
        assert dispatched >= 500
