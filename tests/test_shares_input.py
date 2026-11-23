"""Money in, percentage out — and the pasted mix used by the 臨時餐.

Two entry points accept a mix expressed in dollars: the monthly share grid and
the temp meal on the upload page. Both lean on the same two primitives, so they
are checked here rather than through HTTP.
"""

import io

import pytest
from openpyxl import Workbook

from app import excel_io
from app.calc import apportion, weights_to_shares


class TestWeightsToShares:
    def test_money_becomes_the_same_share_as_the_percentage_would(self):
        shares, total, warnings = weights_to_shares({"HB01": 3000, "HB02": 1000})
        assert warnings == []
        assert total == 4000
        assert shares["HB01"] == pytest.approx(0.75)
        assert shares["HB02"] == pytest.approx(0.25)
        assert sum(shares.values()) == pytest.approx(1.0)

    def test_zero_total_is_refused_rather_than_dividing_by_zero(self):
        shares, total, warnings = weights_to_shares({"HB01": 0, "HB02": 0})
        assert shares == {}
        assert total == 0
        assert "無法換算" in warnings[0]

    def test_zero_shop_is_dropped_like_a_blank_percentage(self):
        shares, _total, _warn = weights_to_shares({"HB01": 100, "HB02": 0})
        assert set(shares) == {"HB01"}
        assert shares["HB01"] == pytest.approx(1.0)

    def test_negative_amount_never_becomes_a_negative_share(self):
        shares, _total, _warn = weights_to_shares({"HB01": -50, "HB02": 100})
        assert shares["HB02"] == pytest.approx(1.0)
        assert "HB01" not in shares

    def test_converted_shares_still_split_a_target_exactly(self):
        shares, _total, _warn = weights_to_shares({"A": 1000, "B": 1000, "C": 1000})
        assert sum(apportion(100, [shares[k] for k in ("A", "B", "C")])) == 100


class TestParseShareText:
    def test_tab_separated_paste_is_read(self):
        shares, total, warnings, errors = excel_io.parse_share_text(
            "HB01\t300\nHB02\t100", unit="amt"
        )
        assert errors == [] and warnings == []
        assert total == 400
        assert shares["HB01"] == pytest.approx(0.75)

    def test_comma_space_and_dollar_signs_are_accepted(self):
        shares, total, _warn, errors = excel_io.parse_share_text(
            "hb01, $1,200\nHB02 800", unit="amt"
        )
        assert errors == [], errors
        assert total == 2000
        assert shares["HB01"] == pytest.approx(0.6)

    def test_percent_mode_keeps_percentages(self):
        shares, _total, _warn, errors = excel_io.parse_share_text("HB01\t60\nHB02\t30")
        assert errors == []
        assert shares["HB01"] == pytest.approx(0.6)
        assert shares["HB02"] == pytest.approx(0.3)

    def test_header_and_comment_lines_are_skipped(self):
        shares, _total, _warn, errors = excel_io.parse_share_text(
            "# 店舖\t金額\nHB01\t100"
        )
        assert errors == []
        assert set(shares) == {"HB01"}

    def test_bad_lines_are_reported_with_their_line_number(self):
        _shares, _total, _warn, errors = excel_io.parse_share_text("HB01\nHB02\tabc")
        assert len(errors) == 2
        assert "第 1 行" in errors[0]
        assert "第 2 行" in errors[1]

    def test_duplicate_shop_keeps_the_last_value_and_says_so(self):
        shares, total, warnings, _errors = excel_io.parse_share_text(
            "HB01\t100\nHB01\t300", unit="amt"
        )
        assert total == 300
        assert shares["HB01"] == pytest.approx(1.0)
        assert any("多於一次" in w for w in warnings)

    def test_empty_paste_is_an_error_not_an_empty_mix(self):
        _shares, _total, _warn, errors = excel_io.parse_share_text("   \n\n")
        assert errors and "沒有讀到" in errors[0]


class TestShareFileWithMoneyColumns:
    def _book(self, rows):
        wb = Workbook()
        ws = wb.active
        ws.append(["Site", "A", "B", "C"])
        for row in rows:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_a_column_in_dollars_is_converted_by_its_total(self):
        # 12,000 cannot be a percentage, so the whole column is money.
        data = self._book([["HB01", 12000, 0.5, 0.5], ["HB02", 4000, 0.5, 0.5]])
        parsed = excel_io.parse_share_file(data)
        assert parsed.errors == []
        assert parsed.by_scenario["A"]["HB01"] == pytest.approx(0.75)
        assert parsed.by_scenario["A"]["HB02"] == pytest.approx(0.25)
        assert any("金額" in w for w in parsed.warnings)

    def test_percentage_columns_still_use_the_old_rule(self):
        data = self._book([["HB01", 60, 40, 0], ["HB02", 40, 60, 0]])
        parsed = excel_io.parse_share_file(data)
        assert parsed.by_scenario["A"]["HB01"] == pytest.approx(0.6)
        assert not any("金額" in w for w in parsed.warnings)


class TestHeaderRowInPaste:
    def test_a_copied_header_is_skipped_and_reported(self):
        shares, _total, warnings, errors = excel_io.parse_share_text(
            "店舖\t金額\nHB01\t100", unit="amt"
        )
        assert errors == []
        assert set(shares) == {"HB01"}
        assert any("略過 1 行" in w for w in warnings)
