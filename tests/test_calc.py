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
    SUPPLY_VENDOR,
    SUPPLY_WAREHOUSE,
    apportion,
    cap_to_available,
    compare_scenarios,
    compute_all_scenarios,
    compute_scenario,
    daily_sales_rate,
    dispatch_window,
    normalise_shares,
    round_up_to,
    sales_window_start,
    sku_moq,
    split_shared_targets,
    supply_code,
    supply_mode,
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
        # No warehouse in this file, so nothing proves D001 is short and the
        # 可調出 column stays empty; see TestSurplus for the gating rules.
        assert line["S2"].surplus_qty == 0

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
        # S2 holds 150 more than its share, but the move is D001's job: the
        # warehouse was never asked, so 可調出 is not raised.
        assert sku.surplus_qty == 0

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


class TestDispatchWindow:
    def test_window_is_the_first_dispatch_days_of_the_promotion(self):
        w = PromoWindow(start=date(2026, 11, 16), end=date(2026, 11, 29), dispatch_days=4)
        assert dispatch_window(w) == (date(2026, 11, 16), date(2026, 11, 19))

    def test_window_never_runs_past_the_promotion(self):
        w = PromoWindow(start=date(2026, 11, 16), end=date(2026, 11, 18), dispatch_days=4)
        assert dispatch_window(w) == (date(2026, 11, 16), date(2026, 11, 18))

    def test_whole_promotion_is_one_window(self):
        w = PromoWindow(start=date(2026, 11, 16), end=date(2026, 11, 19), dispatch_days=4)
        assert dispatch_window(w) == (date(2026, 11, 16), date(2026, 11, 19))

    def test_empty_window_has_no_dispatch(self):
        w = PromoWindow(start=date(2026, 11, 18), end=date(2026, 11, 16), dispatch_days=4)
        assert dispatch_window(w) is None


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

    def test_compare_splits_the_group_into_one_row_per_sku(self):
        """The table lists the SKUs apart, so each can be filtered or looked up."""
        targets, _ = split_shared_targets(self.targets(), sales_by_sku=self.sales())
        res = compute_scenario(
            "A", ["S1"], [1.0], targets, {self.A: {"S1": 3500}, self.B: {}}
        )
        rows = compare_scenarios({"A": res})
        assert [r["sku"] for r in rows] == [self.A, self.B]
        a, b = rows
        assert a["group_size"] == b["group_size"] == 2
        assert a["group"] == b["group"] == self.A
        assert a["members"] == [self.A, self.B]
        # The single number the buyer wrote is still accounted for: the shares
        # add back up to 4,000 and to the 3,500 actually on hand.
        assert a["group_total"] == b["group_total"] == 4000
        assert a["target_qty"] + b["target_qty"] == 4000
        assert a["total_stock"] + b["total_stock"] == 3500
        # The group is short as a whole: every member says 不夠貨, but the gap
        # is only written once so sums cannot count it twice.
        assert a["short"] is True and b["short"] is True
        assert sum(r["shortfall_qty"] for r in rows) == 500

    def test_a_group_row_carries_its_own_description_and_brand(self):
        targets = [
            SkuTarget(self.A, 4000, description="7 片裝", group=self.A, brand="BR01"),
            SkuTarget(self.B, 0, description="14 片裝", group=self.A, brand="BR01"),
        ]
        targets, _ = split_shared_targets(targets)
        res = compute_scenario(
            "A", ["S1"], [1.0], targets, {self.A: {"S1": 500}, self.B: {"S1": 500}}
        )
        rows = {r["sku"]: r for r in compare_scenarios({"A": res})}
        assert rows[self.A]["description"] == "7 片裝"
        assert rows[self.B]["description"] == "14 片裝"
        assert rows[self.B]["brand"] == "BR01"

    def test_standalone_skus_still_sort_by_their_own_gap(self):
        # The sort key prefers the group's gap; a SKU without a group must fall
        # back to its own, or the biggest shortage would stop coming first.
        targets = [SkuTarget("A_SMALL", 100), SkuTarget("Z_BIG", 1000)]
        res = compute_scenario("A", ["S1"], [1.0], targets, {})
        rows = compare_scenarios({"A": res})
        assert [r["sku"] for r in rows] == ["Z_BIG", "A_SMALL"]
        assert all(r["short"] for r in rows)

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


class TestGroupSafetyTopup:
    """A covered group still lifts a pack to the shop's Safety Stock.

    The user's case (2026-11-17): group Target 400, the 7PC holds 450 in the
    shop and the 14PC holds none. The group is covered, so nothing used to be
    sent — and the shop ran the promotion with an empty shelf for one pack.
    """

    A = "111058302001"
    B = "111058302002"

    def targets(self):
        return [
            SkuTarget(self.A, 200, group=self.A),
            SkuTarget(self.B, 200, group=self.A),
        ]

    def run(self, stock, safety, **kwargs):
        return compute_scenario(
            "A",
            ["S1"],
            [1.0],
            self.targets(),
            stock,
            safety_by_sku=safety,
            **kwargs,
        )

    @staticmethod
    def line(res, sku):
        return next(ln for ln in res.lines if ln.sku == sku)

    def test_the_empty_pack_is_topped_up_to_the_shop_safety(self):
        res = self.run(
            {self.A: {"S1": 0}, self.B: {"S1": 450}}, {self.A: {"S1": 100}}
        )
        a = self.line(res, self.A)
        assert a.need_qty == 100
        assert a.dispatch_qty == 100
        assert a.topup_qty == 100
        assert a.safety_qty == 100
        assert a.short_qty == 0
        # The pack that already covers the group is left alone.
        assert self.line(res, self.B).dispatch_qty == 0

    def test_only_the_gap_to_the_floor_is_sent(self):
        # 60 on hand against a floor of 100: 40 more, not another 100.
        res = self.run(
            {self.A: {"S1": 60}, self.B: {"S1": 450}}, {self.A: {"S1": 100}}
        )
        a = self.line(res, self.A)
        assert a.need_qty == 40 and a.topup_qty == 40

    def test_a_shop_already_at_the_floor_gets_nothing(self):
        res = self.run(
            {self.A: {"S1": 120}, self.B: {"S1": 450}}, {self.A: {"S1": 100}}
        )
        assert self.line(res, self.A).dispatch_qty == 0

    def test_the_topup_rounds_up_to_the_sku_moq(self):
        res = self.run(
            {self.A: {"S1": 0}, self.B: {"S1": 450}},
            {self.A: {"S1": 100}},
            moq_by_sku={self.A: 6},
        )
        assert self.line(res, self.A).dispatch_qty == 102

    def test_pending_counts_towards_the_floor(self):
        # 80 already on its way: the shop is 20 short of its 100, not 100.
        res = self.run(
            {self.A: {"S1": 0}, self.B: {"S1": 450}},
            {self.A: {"S1": 100}},
            sales_by_sku={self.A: {"S1": ShopSales(pending_qty=80)}},
            as_of=date(2026, 11, 17),
        )
        assert self.line(res, self.A).dispatch_qty == 20

    def test_a_bigger_need_is_never_lowered_to_the_floor(self):
        # Short of its own share by 200 while the floor is 100: keep the 200.
        res = compute_scenario(
            "A", ["S1"], [1.0], self.targets(), {self.A: {"S1": 0}},
            safety_by_sku={self.A: {"S1": 100}},
        )
        a = self.line(res, self.A)
        assert a.need_qty == 200 and a.topup_qty == 0

    def test_no_safety_figure_means_no_topup(self):
        res = self.run({self.A: {"S1": 0}, self.B: {"S1": 450}}, None)
        assert sum(ln.dispatch_qty for ln in res.lines) == 0

    def test_it_can_be_switched_off_for_a_run(self):
        res = self.run(
            {self.A: {"S1": 0}, self.B: {"S1": 450}},
            {self.A: {"S1": 100}},
            group_topup=False,
        )
        assert sum(ln.dispatch_qty for ln in res.lines) == 0

    def test_a_standalone_sku_is_not_topped_up(self):
        # Without a group the floor does not apply: the SKU's own gap is its ask.
        res = compute_scenario(
            "A", ["S1"], [1.0], [SkuTarget("X", 100)], {"X": {"S1": 0}},
            safety_by_sku={"X": {"S1": 50}},
        )
        x = self.line(res, "X")
        assert x.need_qty == 100 and x.topup_qty == 0

    def test_a_shop_without_a_share_is_not_opened_for_the_floor(self):
        res = compute_scenario(
            "A", ["S1", "S2"], [1.0, 0.0], self.targets(),
            {self.A: {"S1": 0, "S2": 0}, self.B: {"S1": 450}},
            safety_by_sku={self.A: {"S1": 100, "S2": 100}},
        )
        assert self.line(res, self.A).shop_code == "S1"
        assert self.line(res, self.A).dispatch_qty == 100
        assert not [ln for ln in res.lines if ln.shop_code == "S2" and ln.dispatch_qty]

    def test_d001_pays_the_target_before_the_floor(self):
        # One SKU, two shops: S2 needs its own 100 for the Target, S1 only a
        # 100 floor. D001 has exactly 100 — the Target must win.
        res = compute_scenario(
            "A",
            ["S1", "S2"],
            [0.5, 0.5],
            self.targets(),
            {self.A: {"S1": 0}, self.B: {"S1": 400}},
            warehouse_by_sku={self.A: 100, self.B: 1000},
            safety_by_sku={self.A: {"S1": 100}},
        )
        s1 = next(ln for ln in res.lines if ln.sku == self.A and ln.shop_code == "S1")
        s2 = next(ln for ln in res.lines if ln.sku == self.A and ln.shop_code == "S2")
        assert s2.dispatch_qty == 100 and s2.short_qty == 0
        assert s1.dispatch_qty == 0 and s1.short_qty == 100
        assert s1.topup_qty == 100

    def test_a_gated_shop_still_refuses_the_floor(self):
        # A already covers its own share, so the only thing that would be sent
        # is the floor — and the RP gate is not overruled by it.
        res = compute_scenario(
            "A", ["S1"], [1.0], self.targets(),
            {self.A: {"S1": 250}, self.B: {"S1": 450}},
            rp_by_sku={self.A: {"S1": "ND"}},
            rp_gate_shops=["S1"],
            safety_by_sku={self.A: {"S1": 300}},
        )
        a = self.line(res, self.A)
        assert a.dispatch_qty == 0
        assert a.blocked is True and a.blocked_qty == 50

    def test_a_vendor_shop_gets_the_floor_as_a_po(self):
        res = compute_scenario(
            "A", ["S1"], [1.0], self.targets(),
            {self.A: {"S1": 0}, self.B: {"S1": 450}},
            supply_by_sku={self.A: {"S1": "1"}},
            safety_by_sku={self.A: {"S1": 100}},
        )
        a = self.line(res, self.A)
        assert a.vendor is True
        assert a.po_qty == 100 and a.sto_qty == 0
        assert a.topup_qty == 100

    def test_the_floor_is_not_scaled_by_the_dispatch_window(self):
        # A 14-day promotion sent for 4 days halves nothing here: Safety Stock
        # is a standing shelf level, not a share of the promotion.
        promo = PromoWindow(
            start=date(2026, 11, 1), end=date(2026, 11, 14), dispatch_days=4
        )
        res = self.run(
            {self.A: {"S1": 0}, self.B: {"S1": 450}},
            {self.A: {"S1": 100}},
            promo=promo,
            as_of=date(2026, 11, 17),
        )
        a = self.line(res, self.A)
        assert a.dispatch_qty == 100
        assert a.topup_qty == 100

    def test_the_summary_and_warning_carry_the_topup(self):
        res = self.run(
            {self.A: {"S1": 0}, self.B: {"S1": 450}}, {self.A: {"S1": 100}}
        )
        assert next(s for s in res.skus if s.sku == self.A).topup_qty == 100
        assert any("貨組補底" in w for w in res.warnings)


class TestSupplySource:
    """1／4 行貨 is a PO for the buyer; 2 倉貨 is an STO from D001."""

    def test_codes_are_normalised(self):
        for raw in (2, 2.0, "2", " 2 ", "2.0"):
            assert supply_code(raw) == "2"
        assert supply_code(None) == ""
        assert supply_code(True) == ""

    def test_vendor_codes_are_one_and_four(self):
        assert supply_mode("1") == SUPPLY_VENDOR
        assert supply_mode("4") == SUPPLY_VENDOR         # 經 D001 過數，但仍是行貨
        assert supply_mode("2") == SUPPLY_WAREHOUSE
        assert supply_mode("") == SUPPLY_WAREHOUSE          # no column / blank
        assert supply_mode("9") == SUPPLY_WAREHOUSE         # unrecognised

    def test_vendor_route_is_configurable(self):
        assert supply_mode("3", ["3"]) == SUPPLY_VENDOR
        assert supply_mode("1", ["3"]) == SUPPLY_WAREHOUSE

    def test_code_four_is_a_po_that_never_touches_the_warehouse(self):
        # 4 goes through D001 on paper only: the goods come from the vendor on a
        # PO, so the warehouse's 50 units are all the 倉貨 shop can get.
        res = compute_scenario(
            "A",
            ["S1", "S2"],
            [0.5, 0.5],
            [SkuTarget("K", 200)],
            {},
            warehouse_by_sku={"K": 50},
            supply_by_sku={"K": {"S1": "4", "S2": "2"}},
        )
        by_shop = {ln.shop_code: ln for ln in res.lines}
        assert by_shop["S1"].vendor is True
        assert by_shop["S1"].po_qty == 100
        assert by_shop["S1"].sto_qty == 0
        assert by_shop["S1"].short_qty == 0
        assert by_shop["S2"].dispatch_qty == 50

        sku = res.skus[0]
        assert sku.po_qty == 100
        assert sku.short_qty == 50              # only the warehouse route is short
        # The one route that looks like a warehouse movement is spelled out.
        assert any("D001 過數" in w for w in res.warnings)
        assert not any("未設定的代碼" in w for w in res.warnings)

    def test_vendor_shop_is_not_capped_by_the_warehouse(self):
        res = compute_scenario(
            "A",
            ["S1", "S2"],
            [0.5, 0.5],
            [SkuTarget("K", 200)],
            {},
            warehouse_by_sku={"K": 50},
            supply_by_sku={"K": {"S1": "1", "S2": "2"}},
        )
        by_shop = {ln.shop_code: ln for ln in res.lines}
        assert by_shop["S1"].vendor
        assert by_shop["S1"].po_qty == 100     # full need, no D001 ceiling
        assert by_shop["S1"].sto_qty == 0
        assert by_shop["S2"].po_qty == 0
        assert by_shop["S2"].dispatch_qty == 50  # warehouse still caps this one

        sku = res.skus[0]
        assert sku.po_qty == 100
        assert sku.sto_qty == 50
        assert sku.total_dispatch == 150
        assert sku.short_qty == 50              # only the warehouse route is short
        # 1 is the plain vendor route: no 過數 note on its runs.
        assert not any("D001 過數" in w for w in res.warnings)

    def test_vendor_units_do_not_eat_the_warehouse_quota(self):
        # Without the split, S2 would have received only part of its 60 because
        # the vendor shop was competing for the same 60 units.
        res = compute_scenario(
            "A",
            ["PO1", "WH1"],
            [0.5, 0.5],
            [SkuTarget("K", 120)],
            {},
            warehouse_by_sku={"K": 60},
            supply_by_sku={"K": {"PO1": "4", "WH1": "2"}},
        )
        by_shop = {ln.shop_code: ln for ln in res.lines}
        assert by_shop["PO1"].po_qty == 60
        assert by_shop["WH1"].dispatch_qty == 60
        assert res.skus[0].short_qty == 0
        assert any("行貨" in w and "開 PO" in w for w in res.warnings)
        assert any("D001 過數" in w for w in res.warnings)   # 4 是行貨但經 D001

    def test_blank_supply_source_keeps_the_old_behaviour(self):
        kwargs = dict(warehouse_by_sku={"K": 120})
        plain = compute_scenario(
            "A", ["S1", "S2"], [0.5, 0.5], [SkuTarget("K", 200)], {}, **kwargs
        )
        blank = compute_scenario(
            "A", ["S1", "S2"], [0.5, 0.5], [SkuTarget("K", 200)], {},
            supply_by_sku={"K": {"S1": "", "S2": ""}}, **kwargs
        )
        assert [l.dispatch_qty for l in plain.lines] == [l.dispatch_qty for l in blank.lines]
        assert blank.skus[0].po_qty == 0

    def test_unrecognised_code_is_reported(self):
        res = compute_scenario(
            "A", ["S1"], [1.0], [SkuTarget("K", 50)], {},
            supply_by_sku={"K": {"S1": "7"}},
        )
        assert any("未設定的代碼" in w for w in res.warnings)
        assert res.skus[0].po_qty == 0

    def test_group_summary_carries_the_po_quantity(self):
        # Two members of one group: P1 from the warehouse, P2 on a PO. They show
        # as separate rows now, so the group's PO figure is their sum.
        targets = [
            SkuTarget("P1", 100, group="P1"),
            SkuTarget("P2", 100, group="P1"),
        ]
        res = compute_scenario(
            "A", ["S1"], [1.0], targets,
            {"P1": {"S1": 0}, "P2": {"S1": 0}},
            supply_by_sku={"P1": {"S1": "2"}, "P2": {"S1": "1"}},
        )
        rows = compare_scenarios({"A": res})
        assert sum(r["scenarios"]["A"].po_qty for r in rows) == 100
        assert sum(r["scenarios"]["A"].total_dispatch for r in rows) == 200


class TestVendorFullPeriodTarget:
    """A PO is raised once for the whole promotion, so 行貨 ignores the window.

    The warehouse still only stocks the first ``dispatch_days`` days, so the
    two routes are measured against different targets — the line says which.
    """

    PROMO = PromoWindow(
        start=date(2026, 11, 17), end=date(2026, 11, 30), dispatch_days=4
    )

    def test_vendor_row_allocates_the_full_target_warehouse_stays_on_the_window(
        self,
    ):
        # 1,400 over 14 days, 4-day dispatch => warehouse target 400, full 1,400.
        res = compute_scenario(
            "A",
            ["V1", "W1"],
            [0.5, 0.5],
            [SkuTarget("K", 1400)],
            {},
            promo=self.PROMO,
            supply_by_sku={"K": {"V1": "1", "W1": "2"}},
        )
        by_shop = {ln.shop_code: ln for ln in res.lines}

        assert by_shop["V1"].alloc_qty == 700          # half of 1,400
        assert by_shop["V1"].alloc_full_period is True
        assert by_shop["V1"].po_qty == 700

        assert by_shop["W1"].alloc_qty == 200          # half of 400
        assert by_shop["W1"].alloc_full_period is False
        assert by_shop["W1"].dispatch_qty == 200

    def test_vendor_shop_with_one_hundred_percent_gets_the_whole_target(self):
        res = compute_scenario(
            "A",
            ["V1"],
            [1.0],
            [SkuTarget("K", 1000)],
            {},
            promo=PromoWindow(
                start=date(2026, 11, 17), end=date(2026, 11, 26), dispatch_days=4
            ),
            supply_by_sku={"K": {"V1": "4"}},
        )
        line = res.lines[0]
        assert line.alloc_qty == 1000
        assert line.po_qty == 1000                     # not 400
        assert res.skus[0].dispatch_target == 400       # the window is still reported
        assert res.skus[0].po_target == 1000

    def test_vendor_stock_offsets_the_full_period_allocation(self):
        res = compute_scenario(
            "A",
            ["V1"],
            [1.0],
            [SkuTarget("K", 1000)],
            {"K": {"V1": 300}},
            promo=self.PROMO,
            supply_by_sku={"K": {"V1": "1"}},
        )
        assert res.lines[0].alloc_qty == 1000
        assert res.lines[0].gap_qty == 700
        assert res.lines[0].po_qty == 700

    def test_vendor_po_still_rounds_up_to_moq(self):
        res = compute_scenario(
            "A",
            ["V1"],
            [1.0],
            [SkuTarget("K", 1000)],
            {},
            promo=self.PROMO,
            moq_by_sku={"K": 12},
            supply_by_sku={"K": {"V1": "1"}},
        )
        assert res.lines[0].po_qty == 1008

    def test_without_a_promotion_vendor_alloc_is_the_target_anyway(self):
        res = compute_scenario(
            "A",
            ["V1", "W1"],
            [0.5, 0.5],
            [SkuTarget("K", 400)],
            {},
            supply_by_sku={"K": {"V1": "1", "W1": "2"}},
        )
        by_shop = {ln.shop_code: ln for ln in res.lines}
        assert by_shop["V1"].alloc_qty == 200
        assert by_shop["W1"].alloc_qty == 200

    def test_warning_tells_the_buyer_the_po_is_for_the_whole_period(self):
        res = compute_scenario(
            "A",
            ["V1", "W1"],
            [0.5, 0.5],
            [SkuTarget("K", 1400)],
            {},
            promo=self.PROMO,
            supply_by_sku={"K": {"V1": "1", "W1": "2"}},
        )
        note = next(w for w in res.warnings if "行貨" in w)
        assert "全期" in note
        assert "14 日" in note
        assert "4 日" in note

    def test_group_po_target_adds_up_across_members(self):
        targets = [
            SkuTarget("P1", 100, group="P1"),
            SkuTarget("P2", 240, group="P1"),
        ]
        res = compute_scenario(
            "A",
            ["V1"],
            [1.0],
            targets,
            {"P1": {"V1": 0}, "P2": {"V1": 0}},
            promo=self.PROMO,
            supply_by_sku={"P1": {"V1": "1"}, "P2": {"V1": "1"}},
        )
        row = compare_scenarios({"A": res})
        assert sum(r["scenarios"]["A"].po_target for r in row) == 340
        assert sum(r["scenarios"]["A"].po_qty for r in row) == 340


class TestSurplus:
    """可調出 is a rescue figure, not a stock report.

    It only answers "where can we take stock from when D001 cannot cope", so it
    stays empty whenever the warehouse covers the demand, and 行貨 never counts
    (the buyer moves vendor stock in another system).
    """

    def _run(self, shops, stock, warehouse, target=100, shares=None, **kwargs):
        shares = shares or [1.0 / len(shops)] * len(shops)
        return compute_scenario(
            "A",
            list(shops),
            list(shares),
            [SkuTarget("K", target)],
            stock,
            warehouse_by_sku=warehouse,
            **kwargs,
        )

    def test_surplus_shown_when_the_warehouse_cannot_cope(self):
        # S1 needs 50, D001 only has 20 -> 30 short, so S2's 150 spare matters.
        res = self._run(["S1", "S2"], {"K": {"S1": 0, "S2": 200}}, {"K": 20})
        by_shop = {ln.shop_code: ln for ln in res.lines}
        assert res.skus[0].short_qty == 30
        assert by_shop["S2"].surplus_qty == 150
        assert res.skus[0].surplus_qty == 150

    def test_surplus_hidden_when_the_warehouse_has_enough(self):
        # Same shops, D001 can cover the 50 -> the stock comes from D001 and
        # nobody is asked to give anything back.
        res = self._run(["S1", "S2"], {"K": {"S1": 0, "S2": 200}}, {"K": 50})
        by_shop = {ln.shop_code: ln for ln in res.lines}
        assert res.skus[0].short_qty == 0
        assert by_shop["S2"].surplus_qty == 0
        assert res.skus[0].surplus_qty == 0

    def test_surplus_looks_at_stock_only_not_pending(self):
        # 在途 has not reached the shop, so it cannot be pulled out of it.
        res = self._run(
            ["S1", "S2"],
            {"K": {"S1": 0, "S2": 60}},
            {"K": 0},
            sales_by_sku={"K": {"S2": ShopSales(pending_qty=200)}},
        )
        by_shop = {ln.shop_code: ln for ln in res.lines}
        assert by_shop["S2"].pending_qty == 200
        assert by_shop["S2"].surplus_qty == 10  # 60 stock - 50 allocation

    def test_vendor_surplus_is_never_counted(self):
        # V1 sits on 250 spare, but 行貨 transfers are arranged elsewhere.
        res = self._run(
            ["W1", "V1"],
            {"K": {"W1": 0, "V1": 300}},
            {"K": 10},
            supply_by_sku={"K": {"W1": "2", "V1": "1"}},
        )
        by_shop = {ln.shop_code: ln for ln in res.lines}
        assert by_shop["V1"].vendor
        assert by_shop["V1"].surplus_qty == 0
        assert res.skus[0].surplus_qty == 0

    def test_surplus_uses_the_shops_own_allocation_basis(self):
        # A warehouse shop is measured on the 4-day target, a vendor shop on the
        # full 14-day one, and 可調出 follows the same basis.
        promo = PromoWindow(start=date(2026, 11, 1), end=date(2026, 11, 14), dispatch_days=4)
        res = self._run(
            ["W1", "W2", "V1"],
            {"K": {"W1": 200, "W2": 0, "V1": 800}},
            {"K": 0},
            target=1400,
            shares=[0.25, 0.25, 0.5],
            promo=promo,
            supply_by_sku={"K": {"W1": "2", "W2": "2", "V1": "1"}},
        )
        by_shop = {ln.shop_code: ln for ln in res.lines}
        # 4-day target 400 -> W1 100, W2 100; 14-day target 1400 -> V1 700.
        assert by_shop["W1"].alloc_qty == 100
        assert by_shop["W1"].surplus_qty == 100  # 200 stock - 100 (not the 350)
        assert by_shop["W2"].alloc_qty == 100
        assert by_shop["W2"].surplus_qty == 0
        assert by_shop["V1"].alloc_qty == 700
        assert by_shop["V1"].surplus_qty == 0  # 100 spare, but 行貨
        assert res.skus[0].surplus_qty == 100
        assert by_shop["V1"].alloc_qty == 700
        assert by_shop["V1"].surplus_qty == 0  # dead stock, still not pullable


class TestTransferSuggestions:
    """建議調貨 is advice, and it stops at the OM boundary.

    It never changes a dispatch number: the short shop keeps its 缺貨 and its
    STO. It exists because a transfer notice takes 4–7 days, so the operator
    has to decide whether the wait is worth it — hence off by default.
    """

    def _run(self, shops, stock, warehouse, om, target=100, shares=None, **kwargs):
        shares = shares or [1.0 / len(shops)] * len(shops)
        return compute_scenario(
            "A",
            list(shops),
            list(shares),
            [SkuTarget("K", target)],
            stock,
            warehouse_by_sku=warehouse,
            om_by_shop=om,
            suggest_transfer=True,
            **kwargs,
        )

    def test_pairs_a_short_shop_with_a_spare_one_in_the_same_om(self):
        res = self._run(
            ["S1", "S2", "S3", "S4"],
            {"K": {"S1": 0, "S2": 100, "S3": 0, "S4": 100}},
            {"K": 0},
            {"S1": "OM 1", "S2": "OM 1", "S3": "OM 2", "S4": "OM 2"},
        )
        assert [(t.from_shop, t.to_shop, t.qty, t.om) for t in res.transfers] == [
            ("S2", "S1", 25, "OM 1"),
            ("S4", "S3", 25, "OM 2"),
        ]

    def test_never_crosses_into_another_om(self):
        # The only spare sits in OM 2, the shortage in OM 1: no suggestion.
        res = self._run(
            ["S1", "S2"],
            {"K": {"S1": 0, "S2": 100}},
            {"K": 0},
            {"S1": "OM 1", "S2": "OM 2"},
        )
        assert res.transfers == []

    def test_a_blank_om_is_never_paired(self):
        res = self._run(
            ["S1", "S2"],
            {"K": {"S1": 0, "S2": 100}},
            {"K": 0},
            {"S1": "", "S2": ""},
        )
        assert res.transfers == []
        assert any("沒有 OM" in w for w in res.warnings)

    def test_nothing_to_suggest_when_the_warehouse_covers_the_need(self):
        res = self._run(
            ["S1", "S2"],
            {"K": {"S1": 0, "S2": 100}},
            {"K": 50},
            {"S1": "OM 1", "S2": "OM 1"},
        )
        assert res.skus[0].short_qty == 0
        assert res.transfers == []

    def test_a_partial_warehouse_shortage_is_enough(self):
        # D001 has 60 of the 100 asked for: not zero, but still short.
        res = self._run(
            ["S1", "S2", "S3"],
            {"K": {"S1": 0, "S2": 0, "S3": 100}},
            {"K": 60},
            {"S1": "OM 1", "S2": "OM 1", "S3": "OM 1"},
            target=150,
        )
        assert res.skus[0].short_qty == 40
        assert [(t.from_shop, t.to_shop, t.qty) for t in res.transfers] == [
            ("S3", "S1", 20),
            ("S3", "S2", 20),
        ]

    def test_one_spare_shop_can_cover_several_destinations(self):
        res = self._run(
            ["S1", "S2", "S3"],
            {"K": {"S1": 0, "S2": 0, "S3": 100}},
            {"K": 0},
            {"S1": "OM 1", "S2": "OM 1", "S3": "OM 1"},
            target=120,
        )
        assert [(t.from_shop, t.to_shop, t.qty) for t in res.transfers] == [
            ("S3", "S1", 40),
            ("S3", "S2", 20),
        ]

    def test_vendor_shops_are_out_of_scope_at_both_ends(self):
        res = self._run(
            ["W1", "V1"],
            {"K": {"W1": 0, "V1": 300}},
            {"K": 0},
            {"W1": "OM 1", "V1": "OM 1"},
            supply_by_sku={"K": {"W1": "2", "V1": "1"}},
        )
        assert res.transfers == []

    def test_off_unless_asked_for(self):
        res = compute_scenario(
            "A",
            ["S1", "S2"],
            [0.5, 0.5],
            [SkuTarget("K", 100)],
            {"K": {"S1": 0, "S2": 100}},
            warehouse_by_sku={"K": 0},
            om_by_shop={"S1": "OM 1", "S2": "OM 1"},
        )
        assert res.transfers == []

    def test_quantity_follows_the_gap_not_the_moq(self):
        # A shop-to-shop move is not a purchase order, so MOQ does not apply.
        from app.calc import Line, suggest_transfers

        lines = [
            Line(
                sku="K",
                shop_code="S1",
                share_pct=0.5,
                alloc_qty=10,
                stock_qty=0,
                gap_qty=10,
                dispatch_qty=0,
                surplus_qty=0,
                need_qty=7,
                short_qty=7,
            ),
            Line(
                sku="K",
                shop_code="S2",
                share_pct=0.5,
                alloc_qty=10,
                stock_qty=30,
                gap_qty=-20,
                dispatch_qty=0,
                surplus_qty=20,
            ),
        ]
        out = suggest_transfers(lines, {"S1": "OM 1", "S2": "OM 1"})
        assert [(t.from_shop, t.to_shop, t.qty) for t in out] == [("S2", "S1", 7)]

