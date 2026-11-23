"""End-to-end flow: login → import shares → upload → compute → export."""

import io
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, runstore, version
from app.main import app

SAMPLES = Path(__file__).resolve().parents[1] / "samples"

# A four-day promotion sent as four days: the target is not scaled, so the
# allocation numbers stay easy to reason about.
PROMO = {
    "promo_start": "2026-11-02",
    "promo_end": "2026-11-05",
    "dispatch_days": "4",
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(client: TestClient, password: str, name: str = "tester") -> None:
    resp = client.post(
        "/login",
        data={"password": password, "name": name, "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def test_health_no_auth(client):
    assert client.get("/health").json()["ok"] is True


def test_health_reports_version(client):
    body = client.get("/health").json()
    assert body["version"] == version.APP_VERSION
    assert body["released_at"] == version.RELEASED_AT
    assert body["started_at"]


def test_version_shown_on_login_page():
    # A fresh client, so the shared module cookie cannot bounce us to the app.
    with TestClient(app) as fresh:
        html = fresh.get("/login").text
    assert version.label() in html
    assert f"v{version.APP_VERSION}" in html


def test_root_redirects_to_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_bad_password(client):
    resp = client.post("/login", data={"password": "nope", "name": "", "next": "/"})
    assert "密碼不正確" in resp.text


def test_full_flow(client):
    login(client, "adminpw", "Ricky")

    # --- import shares, which also creates the shops ---
    shares_file = SAMPLES / "sample_shares.xlsx"
    assert shares_file.exists(), "run samples/make_samples.py first"

    resp = client.post(
        "/admin/shares/import",
        data={"period": "2026-11", **PROMO},
        files={"file": (shares_file.name, shares_file.read_bytes())},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" not in resp.headers["location"], resp.headers["location"]

    shops = db.list_shops(active_only=True)
    assert len(shops) == 20

    status = db.share_status("2026-11")
    for sc in ("A", "B", "C"):
        assert status[sc]["ready"], f"{sc} not ready: total={status[sc]['total']}"

    # --- upload target + stock and compute ---
    target = SAMPLES / "sample_target.xlsx"
    stock = SAMPLES / "sample_stock_wide.xlsx"

    resp = client.post(
        "/run/new",
        data={"period": "2026-11", **PROMO},
        files={
            "target_file": (target.name, target.read_bytes()),
            "stock_file": (stock.name, stock.read_bytes()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith("/run/"), resp.headers["location"]
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    # --- result page renders and the numbers are sane ---
    page = client.get(f"/run/{token}")
    assert page.status_code == 200
    assert "A／B／C 比較" in page.text

    run = runstore.get(token)
    assert run is not None, "the result must be readable right after computing"
    assert run.period == "2026-11"

    assert len(run.results["A"].skus) == 10

    # Allocation must reconcile with the SKU target in every scenario.
    for sc, res in run.results.items():
        for s in res.skus:
            assert s.total_alloc == s.target_qty, (sc, s.sku)

    # Samples deliberately under-stock two SKUs; random noise may short others.
    short = {s.sku for s in run.results["A"].skus if s.coverage_status == "short"}
    assert "111888009033" in short
    assert "110773004028" in short
    for s in run.results["A"].skus:
        if s.sku in short:
            assert s.shortfall_qty == s.target_qty - s.total_stock
        else:
            assert s.shortfall_qty == 0

    # Same stock, different shares -> same shortfall, different dispatch.
    shortfall = {sc: sum(s.shortfall_qty for s in r.skus) for sc, r in run.results.items()}
    dispatch = {sc: sum(s.total_dispatch for s in r.skus) for sc, r in run.results.items()}
    assert len(set(shortfall.values())) == 1
    assert len(set(dispatch.values())) > 1

    # --- detail view, both aggregate and per-SKU ---
    detail = client.get(f"/run/{token}/detail?scenario=A")
    assert detail.status_code == 200
    assert "各店派貨總量" in detail.text

    sku = "109991605010"
    per_sku = client.get(f"/run/{token}/detail?scenario=A&sku={sku}")
    assert per_sku.status_code == 200
    assert sku in per_sku.text

    lines = [ln for ln in run.results["A"].lines if ln.sku == sku]
    assert len(lines) == 20

    # --- export ---
    xlsx = client.get(f"/run/{token}/export.xlsx")
    assert xlsx.status_code == 200
    assert xlsx.headers["content-type"].startswith(
        "application/vnd.openxmlformats"
    )

    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(xlsx.content), read_only=True)
    assert wb.sheetnames[:4] == ["比較", "明細_A", "明細_B", "明細_C"]
    # No ND line needs stock in this run, so the follow-up sheet is absent.
    assert "ND 派貨" not in wb.sheetnames
    ws = wb["比較"]
    header = [c for c in next(ws.iter_rows(values_only=True))]
    # Brand Code leads the sheet so the brand is read before the article number.
    assert header[:2] == ["Brand Code", "SKU"]
    assert "A 派貨" in header
    assert "派貨目標" in header
    assert "RP 不派" in header
    assert "倉存" in header

    # The per-shop detail sheets must actually carry rows: this is the file the
    # warehouse works from, so an empty sheet would be a silent failure.
    detail_rows = list(wb["明細_A"].iter_rows(values_only=True))
    assert len(detail_rows) == 1 + 10 * 20
    assert detail_rows[0][:3] == ("Brand Code", "SKU", "貨品名稱")

    # --- the only trace a calculation leaves is a timestamp ---
    usage = db.list_usage(limit=5)
    assert usage and usage[0]["period"] == "2026-11"
    assert usage[0]["n_sku"] == 10
    assert usage[0]["at"]


def test_duplicate_sku_in_target_is_rejected(client):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "Target Qty"])
    ws.append(["AAA", 10])
    ws.append(["AAA", 20])
    buf = io.BytesIO()
    wb.save(buf)

    login(client, "adminpw")
    resp = client.post(
        "/run/new",
        data={"period": "2026-11", **PROMO},
        files={
            "target_file": ("dup.xlsx", buf.getvalue()),
            "stock_file": ("s.xlsx", (SAMPLES / "sample_stock_wide.xlsx").read_bytes()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"] or "%E9%87%8D%E8%A4%87" in resp.headers["location"]


def test_long_format_stock_parses(client):
    from app import excel_io

    parsed = excel_io.parse_stock_file((SAMPLES / "sample_stock_long.xlsx").read_bytes())
    assert parsed.ok
    assert len(parsed.rows) == 10 * 20
    assert all(isinstance(row, excel_io.StockRow) for row in parsed.rows)
    assert all(row.rp_type == "" for row in parsed.rows)


def test_share_template_matches_the_requested_layout(client):
    from openpyxl import load_workbook

    login(client, "adminpw", "Ricky")
    resp = client.get("/admin/shares/template?period=2026-11")
    assert resp.status_code == 200

    wb = load_workbook(io.BytesIO(resp.content), read_only=True)
    rows = list(wb["店舖佔比"].iter_rows(values_only=True))
    assert rows[0] == ("Region", "OM", "Site", "店舖名稱", "A", "B", "C")

    data = {r[2]: r for r in rows[1:] if r[2]}
    assert "HA19" in data
    assert data["HA19"][0] == "HK"
    assert data["HA19"][1] == "OM 1"  # carried over from the imported shares


def test_target_template_carries_the_promo_dates(client):
    """The template must show the two date columns the portal reads by default."""
    from openpyxl import load_workbook

    from app import excel_io

    login(client, "adminpw", "Ricky")
    resp = client.get("/run/template/target")
    assert resp.status_code == 200

    wb = load_workbook(io.BytesIO(resp.content), read_only=True)
    header = next(wb["SKU Target"].iter_rows(values_only=True))
    assert header == (
        "SKU",
        "貨品名稱",
        "Brand code",
        "Target Qty",
        "Start date (Promo)",
        "End date (Promo)",
    )

    # Round-trip it: the template as shipped must be usable without editing it.
    parsed = excel_io.parse_target_file(resp.content)
    assert parsed.ok, parsed.errors
    assert parsed.promo_start == date(2026, 11, 17)
    assert parsed.promo_end == date(2026, 11, 26)
    assert [row.sku for row in parsed.rows] == ["109991605010", "110331002001"]
    assert [row.brand for row in parsed.rows] == ["BR01", "BR02"]


def test_share_import_reads_region_and_om(client):
    from openpyxl import Workbook

    from app import db

    wb = Workbook()
    ws = wb.active
    ws.append(["Region", "OM", "Site", "店舖名稱", "A", "B", "C"])
    ws.append(["HK", "Alice", "HB87", "HB87 特殊店", 0.5, 0.5, 0.5])
    ws.append(["MO", "Bob", "MO01", "MO 門市", 0.5, 0.5, 0.5])
    buf = io.BytesIO()
    wb.save(buf)

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/admin/shares/import",
        data={"period": "2027-01"},
        files={"file": ("shares.xlsx", buf.getvalue())},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" not in resp.headers["location"], resp.headers["location"]

    shops = {s["code"]: s for s in db.list_shops()}
    assert shops["HB87"]["om"] == "Alice"
    assert shops["HB87"]["region"] == "HK"
    assert shops["MO01"]["om"] == "Bob"
    assert shops["MO01"]["region"] == "MO"


def test_period_is_normalised_to_year_month():
    from app.main import _period

    assert _period("2026-11") == "2026-11"
    assert _period("2026/1") == "2026-01"
    assert _period("202611") == "2026-11"
    assert _period("2026-11-16") == "2026-11"
    assert _period(" 2026-11 ") == "2026-11"
    for junk in ("", "Nov", "2026", "2026-13", "2026-00", "11/2026"):
        assert _period(junk) == "", junk


def test_shares_page_has_a_month_picker_and_an_import_month(client):
    """The month is a visible choice: page picker plus the import card's own."""
    login(client, "adminpw", "Ricky")
    html = client.get("/admin/shares?period=2026-11").text

    assert 'type="month"' in html
    assert 'id="periodPick"' in html
    assert 'id="importPeriod"' in html
    assert 'id="importTemplate"' in html
    assert "value=\"2026-11\"" in html

    # Months that already hold data are one click away.
    assert 'href="/admin/shares?period=2026-11&scenario=A"' in html


def _share_file_bytes(shop: str, pct: float) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Region", "OM", "Site", "店舖名稱", "A", "B", "C"])
    ws.append(["HK", "", shop, shop, pct, pct, pct])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_share_import_lands_in_the_month_that_was_picked(client):
    from app import db

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/admin/shares/import",
        data={"period": "2026/12"},
        files={"file": ("shares.xlsx", _share_file_bytes("HA19", 1.0))},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "level=error" not in resp.headers["location"], resp.headers["location"]
    assert "2026-12" in resp.headers["location"]

    # Stored under the canonical month, never under the typed variant.
    assert db.get_shares("2026-12", "A")["HA19"] == 1.0
    assert "2026/12" not in db.list_periods()


def test_share_import_refuses_a_month_that_is_not_a_month(client):
    from app import db

    login(client, "adminpw", "Ricky")
    before = db.list_periods()
    resp = client.post(
        "/admin/shares/import",
        data={"period": "Nov"},
        files={"file": ("shares.xlsx", _share_file_bytes("HA19", 1.0))},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "level=error" in resp.headers["location"], resp.headers["location"]
    assert db.list_periods() == before


def _sap_stock_bytes(rows) -> bytes:
    """A miniature SAP export: the wide header set is the point of the test."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(
        [
            "Product Hierarchy", "Article", "Article Description", "RP Type",
            "Safety Stock", "Stock in Transit", "SaSa Net Stock", "Site",
            "Last Month Sold Qty", "Blocked",
        ]
    )
    for sku, desc, rp, qty, site in rows:
        ws.append(["LNU", sku, desc, rp, 0, 0, qty, site, 0, 0])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_sap_export_picks_the_right_columns():
    from app import excel_io

    parsed = excel_io.parse_stock_file(
        _sap_stock_bytes([("111058302001", "面膜", "ND", 6762, "D001"),
                          ("111058302001", "面膜", "RF", 21, "HB87")]),
        {"HB87"},
    )
    assert not parsed.errors
    assert parsed.mapped["stock"] == "SaSa Net Stock"
    assert parsed.mapped["shop"] == "Site"
    assert parsed.mapped["rp_type"] == "RP Type"
    rows = {(r.sku, r.shop): r for r in parsed.rows}
    assert rows[("111058302001", "HB87")].qty == 21
    assert rows[("111058302001", "HB87")].rp_type == "RF"
    # D001 is the DC row: parsed, but treated as the warehouse later on.
    assert rows[("111058302001", "D001")].qty == 6762
    assert rows[("111058302001", "D001")].rp_type == "ND"


def _sap_consign_bytes(rows) -> bytes:
    """SAP export where consignment stock may be the only stock column filled."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(
        [
            "Article", "Site", "SaSa Net Stock", "Unrestr. Consignment",
            "Pending Received", "Supply source",
        ]
    )
    for sku, site, net, consign, source in rows:
        ws.append([sku, site, net, consign, 0, source])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_consignment_stock_counts_as_shop_stock():
    """Either column can hold the units; a consignment-only line is not a blank."""
    from app import excel_io

    parsed = excel_io.parse_stock_file(
        _sap_consign_bytes(
            [
                ("111058302001", "HA01", 10, 5, "2"),     # both: added
                ("111058302001", "HA02", None, 40, "2"),  # only consignment
                ("111058302001", "HA03", 7, None, "2"),   # only net stock
            ]
        ),
        {"HA01", "HA02", "HA03"},
    )
    assert not parsed.errors
    assert parsed.mapped["stock"] == "SaSa Net Stock"
    assert parsed.mapped["consignment"] == "Unrestr. Consignment"
    qty = {(r.sku, r.shop): r.qty for r in parsed.rows}
    assert qty[("111058302001", "HA01")] == 15
    assert qty[("111058302001", "HA02")] == 40   # previously dropped as "not a number"
    assert qty[("111058302001", "HA03")] == 7
    # Both mixes are reported, so 15 and 40 are explainable without the file.
    assert any("Unrestr. Consignment" in w and "相加" in w for w in parsed.warnings)
    assert any("SaSa Net Stock 無數" in w for w in parsed.warnings)


def test_consignment_stock_reduces_the_dispatch(client):
    """The shop's need is measured against consignment stock too."""
    from app import db, runstore

    period = "2027-06"
    sku = "111058303003"

    db.upsert_shop("HA04", "HA04 寄售店", "HK")
    db.save_shares(period, "A", {"HA04": 1.0}, "tester")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append([sku, "面膜", 60])
    target = io.BytesIO()
    wb.save(target)

    stock = _sap_consign_bytes(
        [
            (sku, "D001", 100, 0, "2"),
            (sku, "HA04", 0, 20, "2"),   # shop holds 20 as consignment, 0 net
        ]
    )

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/new",
        data={"period": period, **PROMO},
        files={
            "target_file": ("target.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    assert run.params["has_consignment"] is True
    line = {ln.shop_code: ln for ln in run.results["A"].lines}["HA04"]
    assert line.stock_qty == 20
    assert line.dispatch_qty == 40        # 60 target − 20 consignment
    # The note from reading the file follows the result instead of staying on
    # the upload form.
    assert any(
        "Unrestr. Consignment" in w for w in run.params["warnings"]["A"]
    )

    page = client.get(f"/run/{token}")
    assert page.status_code == 200
    assert "Unrestr. Consignment" in page.text

    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(client.get(f"/run/{token}/export.xlsx").content))
    params_sheet = {
        row[0]: (row[1] if len(row) > 1 else "")
        for row in book["參數"].iter_rows(values_only=True)
        if row and row[0]
    }
    assert "Unrestr. Consignment" in str(params_sheet.get("店舖存貨口徑", ""))
    book.close()


def test_hb87_blocks_nd_and_dispatches_rf(client):
    from app import db

    period = "2026-12"
    db.upsert_shop("HB87", "HB87 特殊店", "HK")
    db.upsert_shop("HA01", "HA01 普通店", "HK")
    db.save_shares(period, "A", {"HB87": 0.5, "HA01": 0.5}, "tester")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append(["111058302001", "面膜", 100])
    ws.append(["111058302002", "精華", 100])
    ws.append(["111058302003", "眼霜", 100])
    buf = io.BytesIO()
    wb.save(buf)

    stock = _sap_stock_bytes(
        [
            ("111058302001", "面膜", "ND", 0, "HB87"),   # blocked, RP says ND
            ("111058302001", "面膜", "RF", 0, "HA01"),
            ("111058302002", "精華", "RF", 0, "HB87"),   # allowed
            ("111058302002", "精華", "ND", 0, "HA01"),   # outside the gate: still sent
            ("111058302003", "眼霜", "", 0, "HB87"),     # missing RP Type: blocked
            ("111058302003", "眼霜", "RF", 0, "HA01"),
        ]
    )

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/new",
        data={"period": period, **PROMO},
        files={
            "target_file": ("target.xlsx", buf.getvalue()),
            "stock_file": ("stock.xlsx", stock),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith("/run/"), resp.headers["location"]
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    nd = [ln for ln in run.results["A"].lines if ln.sku == "111058302001"]
    nd_hb87 = next(r for r in nd if r.shop_code == "HB87")
    assert nd_hb87.dispatch_qty == 0
    assert nd_hb87.blocked is True
    assert nd_hb87.blocked_qty == 50
    assert nd_hb87.rp_type == "ND"
    assert nd_hb87.gap_qty == 50  # the need is still visible

    rf = [ln for ln in run.results["A"].lines if ln.sku == "111058302002"]
    rf_hb87 = next(r for r in rf if r.shop_code == "HB87")
    assert rf_hb87.dispatch_qty == 50
    assert rf_hb87.blocked is False

    detail = client.get(f"/run/{token}/detail?scenario=A&sku=111058302001")
    assert detail.status_code == 200
    assert "RP" in detail.text
    assert nd_hb87.need_qty == 50
    # Blocked by the RP rule, not by a lack of stock: the unit belongs to
    # blocked_qty only, so the shortage total stays about the warehouse.
    assert nd_hb87.short_qty == 0

    # A blank RP Type is blocked the same way as ND, and must stay visible as
    # missing data rather than being silently treated as RF.
    missing = next(
        ln
        for ln in run.results["A"].lines
        if ln.sku == "111058302003" and ln.shop_code == "HB87"
    )
    assert missing.blocked is True
    assert missing.rp_type == ""
    assert missing.blocked_qty == 50

    # The SKU-level numbers exclude the blocked half, so 需求/可派 reconcile.
    sku_row = next(s for s in run.results["A"].skus if s.sku == "111058302001")
    assert sku_row.need_qty == 50
    assert sku_row.total_dispatch == 50
    assert sku_row.blocked_qty == 50

    result = client.get(f"/run/{token}")
    assert "RP Type" in result.text

    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(client.get(f"/run/{token}/export.xlsx").content))
    assert "ND 派貨" in book.sheetnames
    ws = book["ND 派貨"]
    assert ws["A1"].value == "ND 派貨店舖名單"
    assert "2 間店舖" in ws["A2"].value
    assert [c.value for c in ws[3]][:3] == ["餐別", "店舖", "店舖名稱"]

    rows = list(ws.iter_rows(min_row=4, values_only=True))
    # HB87 × (ND + missing RP Type), plus HA01 × ND outside the gate. The RF
    # lines are not follow-ups and must stay out of this list.
    assert len(rows) == 3
    by_key = {(row[1], row[4]): row for row in rows}
    assert ("HB87", "111058302002") not in by_key
    assert ("HA01", "111058302001") not in by_key

    blocked = by_key[("HB87", "111058302001")]
    assert blocked[6] == "ND"
    assert blocked[7] == "不派（RP 管制）"
    assert blocked[12] == 50          # 需要派貨
    assert blocked[13] == 0           # 實際派貨
    assert "改 RF" in blocked[14]

    missing_row = by_key[("HB87", "111058302003")]
    assert missing_row[6] == "未提供"
    assert missing_row[7] == "不派（RP 管制）"
    assert "補回" in missing_row[14]

    outside_gate = by_key[("HA01", "111058302002")]
    assert outside_gate[6] == "ND"
    assert outside_gate[7] == "照派（不受管制）"
    assert outside_gate[12] == 50
    assert outside_gate[13] == 50
    assert "已照派" in outside_gate[14]

    # The detail sheet marks the blocked rows in full and only the RP cell for
    # an ND shop that is still being sent stock; the comparison sheet marks the
    # blocked quantity that points at this new list.
    detail = book["明細_A"]
    dhead = [c.value for c in detail[1]]
    d_shop = dhead.index("店舖") + 1
    d_sku = dhead.index("SKU") + 1
    d_rp = dhead.index("RP Type") + 1
    d_reason = dhead.index("不派原因") + 1
    d_rows = {
        (detail.cell(row=r, column=d_shop).value,
         detail.cell(row=r, column=d_sku).value): r
        for r in range(2, detail.max_row + 1)
    }
    blocked_row = d_rows[("HB87", "111058302001")]
    assert detail.cell(row=blocked_row, column=1).fill.fgColor.rgb == "FFE4DFEC"
    assert detail.cell(row=blocked_row, column=d_rp).value == "ND"
    assert detail.cell(row=blocked_row, column=d_rp).font.bold is True
    assert detail.cell(row=blocked_row, column=d_reason).value.startswith("RP=ND")
    outside_row = d_rows[("HA01", "111058302002")]
    assert detail.cell(row=outside_row, column=d_rp).fill.fgColor.rgb == "FFE4DFEC"
    assert detail.cell(row=outside_row, column=1).fill.fill_type is None
    rf_row = d_rows[("HB87", "111058302002")]
    assert detail.cell(row=rf_row, column=d_rp).fill.fill_type is None

    compare = book["比較"]
    chead = [c.value for c in compare[1]]
    rp_col = chead.index("RP 不派") + 1
    c_rows = {
        compare.cell(row=r, column=2).value: r
        for r in range(2, compare.max_row + 1)
    }
    assert compare.cell(row=c_rows["111058302001"], column=rp_col).value == 50
    assert (
        compare.cell(row=c_rows["111058302001"], column=rp_col).fill.fgColor.rgb
        == "FFE4DFEC"
    )
    assert (
        compare.cell(row=c_rows["111058302002"], column=rp_col).fill.fill_type
        is None
    )

    params_sheet = {
        row[0]: (row[1] if len(row) > 1 else "")
        for row in book["參數"].iter_rows(values_only=True)
        if row and row[0]
    }
    assert "A：2 間店舖" in str(params_sheet["ND 派貨"])
    assert "不派 100 件、實際派 50 件" in str(params_sheet["ND 派貨"])
    book.close()


def test_moq_comes_from_the_stock_file_not_the_target(client):
    """MOQ is a SKU property on the stock row; one in the target file is ignored."""
    period = "2027-03"
    sku = "111058302301"
    db.upsert_shop("HA19", "HA19 門市", "HK")
    db.save_shares(period, "A", {"HA19": 1.0}, "tester")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    # An old template still carrying MOQ: it must not win over the stock file.
    ws.append(["SKU", "貨品名稱", "Target Qty", "MOQ"])
    ws.append([sku, "面膜", 100, 99])
    target = io.BytesIO()
    wb.save(target)

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "店舖", "庫存", "MOQ"])
    ws.append([sku, "HA19", 0, 12])
    stock = io.BytesIO()
    wb.save(stock)

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/new",
        data={"period": period, **PROMO},
        files={
            "target_file": ("target.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock.getvalue()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    row = run.results["A"].skus[0]
    assert row.moq == 12                     # from the stock file, not 99
    assert row.need_qty == 108               # ceil(100 / 12) × 12
    assert row.total_dispatch == 108

    params = run.params
    assert params["has_moq"] is True
    # A leftover MOQ column in the target file is just not read. No warning is
    # raised about it: the result page only lists things the buyer must act on.
    warnings = " ".join(params["warnings"]["A"])
    assert "已忽略" not in warnings


def test_stock_file_without_moq_does_not_round(client):
    """No MOQ column: the need is exactly the gap, and the run says so."""
    period = "2027-04"
    sku = "111058302401"
    db.upsert_shop("HA21", "HA21 門市", "HK")
    db.save_shares(period, "A", {"HA21": 1.0}, "tester")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "Target Qty"])
    ws.append([sku, 100])
    target = io.BytesIO()
    wb.save(target)

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "店舖", "庫存"])
    ws.append([sku, "HA21", 40])
    stock = io.BytesIO()
    wb.save(stock)

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/new",
        data={"period": period, **PROMO},
        files={
            "target_file": ("target.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock.getvalue()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    row = run.results["A"].skus[0]
    assert row.moq == 1
    assert row.need_qty == 60

    assert run.params["has_moq"] is False
    assert any("沒有 MOQ 欄位" in w for w in run.params["warnings"]["A"])


def test_each_site_uses_its_own_moq(client):
    """A shop rounds to its own site's MOQ; D001's carton never reaches it.

    User's report (2026-11-23): 店舖派貨時會認錯 D001 的 MOQ. The old code took
    the largest MOQ on the SKU — exactly the warehouse carton — and rounded every
    shop to it, so a shop packing in sixes was dispatched in 48s.
    """
    period = "2027-05"
    sku = "111058302501"
    db.upsert_shop("HA19", "HA19 門市", "HK")
    db.upsert_shop("HA21", "HA21 門市", "HK")
    db.save_shares(period, "A", {"HA19": 0.5, "HA21": 0.5}, "tester")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append([sku, "面膜", 100])
    target = io.BytesIO()
    wb.save(target)

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "店舖", "庫存", "MOQ"])
    ws.append([sku, "D001", 1000, 48])       # the warehouse's own carton
    ws.append([sku, "HA19", 0, 6])           # the shops pack differently
    ws.append([sku, "HA21", 0, 12])
    stock = io.BytesIO()
    wb.save(stock)

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/new",
        data={"period": period, **PROMO},
        files={
            "target_file": ("target.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock.getvalue()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    lines = {ln.shop_code: ln for ln in run.results["A"].lines}
    assert "D001" not in lines                       # the warehouse is not a shop
    assert lines["HA19"].moq == 6
    assert lines["HA21"].moq == 12
    # 50 of gap each: 54 for the six-pack shop, 60 for the dozen-pack one. With
    # D001's 48 leaking in, both would have been sent 96.
    assert lines["HA19"].need_qty == 54
    assert lines["HA21"].need_qty == 60
    # The headline is the largest a shop actually uses, never the warehouse's.
    assert run.results["A"].skus[0].moq == 12

    # The detail page lists the step it applied to each shop, so the rounding
    # can be checked without opening the export.
    page = client.get(f"/run/{token}/detail?sku={sku}")
    assert page.status_code == 200
    assert "<th class=\"num\" title=\"取自庫存檔中該店（Site Code）自己的 MOQ" in page.text

    # The detail sheet prints the MOQ applied to each row, so a wrong step is
    # visible in the file the buyer keeps.
    export = client.get(f"/run/{token}/export.xlsx")
    assert export.status_code == 200
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(export.content))
    sheet = book["明細_A"]
    head = [c.value for c in next(sheet.iter_rows(min_row=1, max_row=1))]
    moq_col = head.index("MOQ")
    shop_col = head.index("店舖")
    by_shop = {
        row[shop_col]: row[moq_col]
        for row in sheet.iter_rows(min_row=2, values_only=True)
    }
    assert by_shop["HA19"] == 6
    assert by_shop["HA21"] == 12
    book.close()


def _sap_full_bytes(rows):
    """A SAP export with the full column set the real file carries."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(
        [
            "Region", "Article", "Shop ABC indicator", "Site", "SaSa Net Stock",
            "Safety Stock", "Pending Received", "RP Type",
            "Last Month Sold Qty", "MTD Sold Qty", "Launch Date",
        ]
    )
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_sap_file_drives_warehouse_cap_and_sales(client):
    """The real file shape: DC stock caps dispatch, sales drive the comparison."""
    period = "2027-02"
    sku_a, sku_new = "111058302101", "111058302102"

    db.upsert_shop("HA19", "HA19", "HK")
    db.upsert_shop("HA21", "HA21", "HK")
    db.save_shares(period, "A", {"HA19": 0.5, "HA21": 0.5}, "tester")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append([sku_a, "面膜", 1400])
    ws.append([sku_new, "新精華", 1400])
    target = io.BytesIO()
    wb.save(target)

    # 14-day promotion, 4 days of stock: dispatch target becomes 400.
    promo = {
        "promo_start": "2027-02-01",
        "promo_end": "2027-02-14",
        "dispatch_days": "4",
    }

    stock = _sap_full_bytes(
        [
            # DC: only 100 units, so the two shops' 400 of need cannot be met.
            ("HK", sku_a, "", "D001", 100, 0, 500, "ND", 0, 0, None),
            ("HK", sku_new, "", "D001", 1000, 0, 0, "ND", 0, 0, None),
            # 430 sold last month, nothing this month.
            ("HK", sku_a, "", "HA19", 0, 0, 0, "RF", 430, 0, None),
            ("HK", sku_a, "", "HA21", 0, 0, 0, "RF", 430, 0, None),
            # Launched a few days ago: its rate uses the short window, so the
            # 50 units sold read as 10/day rather than 50/43.
            ("HK", sku_new, "", "HA19", 0, 0, 0, "RF", 0, 50, date.today() - timedelta(days=4)),
            ("HK", sku_new, "", "HA21", 0, 0, 0, "RF", 0, 50, date.today() - timedelta(days=4)),
        ]
    )

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/new",
        data={"period": period, **promo},
        files={
            "target_file": ("target.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith("/run/"), resp.headers["location"]
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    rows = {s.sku: s for s in run.results["A"].skus}
    capped = rows[sku_a]
    assert capped.target_qty == 1400
    assert capped.dispatch_target == 400          # 1400 x 4/14
    assert capped.warehouse_qty == 100           # DC net stock is the ceiling
    assert capped.need_qty == 400
    assert capped.total_dispatch == 100
    assert capped.short_qty == 300

    # D001 never receives an allocation of its own.
    lines_a = [ln for ln in run.results["A"].lines if ln.sku == sku_a]
    assert "D001" not in {ln.shop_code for ln in lines_a}
    assert {ln.shop_code for ln in lines_a if ln.share_pct} == {"HA19", "HA21"}

    # Sales columns made it through to the per-shop numbers. 5 days on record,
    # 50 sold per shop -> 10/day each, 20/day across the two shops.
    fresh = rows[sku_new]
    assert fresh.total_dispatch == 400
    assert fresh.expected_sales == 280
    assert fresh.gap_target == 1120

    lines = [ln for ln in run.results["A"].lines if ln.sku == sku_new]
    assert [ln.daily_rate for ln in lines if ln.shop_code in ("HA19", "HA21")] == [10.0, 10.0]

    page = client.get(f"/run/{token}")
    assert page.status_code == 200
    assert "派貨目標" in page.text
    assert "D001" in page.text


def test_scenario_labels_are_shown_and_snapshotted(client):
    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/admin/settings",
        data={
            "label_A": "恆常推廣",
            "remark_A": "每月固定",
            "label_B": "精彩價&PIP",
            "remark_B": "",
            "label_C": "節日限定",
            "remark_C": "",
            "optional_C": "1",
            "rp_gate_shops": "HB87",
            "warehouse_sites": "D001",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    page = client.get("/admin/settings")
    assert page.status_code == 200
    assert "恆常推廣" in page.text
    assert "精彩價&amp;PIP" in page.text

    # A result keeps the wording it was computed with, even after a rename.
    resp = client.post(
        "/run/new",
        data={"period": "2026-11", **PROMO},
        files={
            "target_file": ("t.xlsx", (SAMPLES / "sample_target.xlsx").read_bytes()),
            "stock_file": ("s.xlsx", (SAMPLES / "sample_stock_wide.xlsx").read_bytes()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    token = resp.headers["location"].rstrip("/").split("/")[-1]
    assert "恆常推廣" in client.get(f"/run/{token}").text

    client.post(
        "/admin/settings",
        data={"label_A": "改名後", "label_B": "精彩價&PIP", "label_C": "節日限定",
              "optional_C": "1", "rp_gate_shops": "HB87", "warehouse_sites": "D001"},
        follow_redirects=False,
    )
    assert "恆常推廣" in client.get(f"/run/{token}").text
    assert "改名後" not in client.get(f"/run/{token}").text


def test_user_role_cannot_open_admin(client):
    client.cookies.clear()
    login(client, "userpw", "buyer")
    resp = client.get("/admin/shares", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]

    resp = client.get("/run/new")
    assert resp.status_code == 200


def test_merged_target_cell_is_read_as_a_shared_target():
    """C2:C3 merged is how one quantity is written for two SKUs."""
    from openpyxl import Workbook

    from app import excel_io

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append(["111058302001", "HYDRA 7PC", 4000])
    ws.append(["111058302002", "HYDRA 14PC", None])
    ws.merge_cells("C2:C3")
    buf = io.BytesIO()
    wb.save(buf)

    parsed = excel_io.parse_target_file(buf.getvalue())
    assert parsed.ok, parsed.errors
    assert [row.sku for row in parsed.rows] == ["111058302001", "111058302002"]
    assert parsed.rows[0].qty == 4000
    assert parsed.rows[1].qty == 0
    assert parsed.rows[0].group == parsed.rows[1].group == "111058302001"
    assert any("共用同一個 Target" in w for w in parsed.warnings)


def test_shared_target_flows_through_to_the_result(client):
    from openpyxl import Workbook, load_workbook

    login(client, "adminpw", "Ricky")
    shops = [s["code"] for s in db.list_shops(active_only=True)][:2]
    assert len(shops) == 2, "shares must be imported before this test"

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append(["111058302001", "HYDRA 7PC", 4000])
    ws.append(["111058302002", "HYDRA 14PC", None])
    ws.merge_cells("C2:C3")
    target = io.BytesIO()
    wb.save(target)

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", shops[0], shops[1]])
    ws.append(["111058302001", 5000, 0])
    ws.append(["111058302002", 0, 1000])
    stock = io.BytesIO()
    wb.save(stock)

    resp = client.post(
        "/run/new",
        data={"period": "2026-11", **PROMO},
        files={
            "target_file": ("shared.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock.getvalue()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    assert run is not None
    res = run.results["A"]
    # Neither SKU is dropped, and the group answers 夠貨 as a whole.
    assert {s.sku for s in res.skus} == {"111058302001", "111058302002"}
    assert all(s.enough for s in res.skus), [(s.sku, s.coverage_status) for s in res.skus]
    assert res.n_short == 0
    assert sum(s.target_qty for s in res.skus) == 4000

    rows = run.params["shared_targets"]
    assert len(rows) == 1 and "4,000" in rows[0]

    page = client.get(f"/run/{token}")
    assert page.status_code == 200
    assert "共用 Target 貨組" in page.text
    assert "111058302002" in page.text
    assert "貨組 2" in page.text

    export = client.get(f"/run/{token}/export.xlsx")
    assert export.status_code == 200
    book = load_workbook(io.BytesIO(export.content))
    compare = book["比較"]
    # One row per SKU: a joined "A + B 等 2 個" label cannot be filtered or
    # looked up, and it hid which articles the shared target covered.
    out = list(compare.iter_rows(min_row=2, values_only=True))
    assert [r[1] for r in out] == ["111058302001", "111058302002"]
    # Brand Code leads, then 推廣時期 (3) and 日數 (4) before the quantities.
    assert sum(r[5] for r in out) == 4000      # the shares add back to the group
    assert "4,000" in compare["B2"].comment.text

    # The members keep their own 貨名 on the detail sheet — they used to come
    # out blank because the name was looked up by the joined group label.
    detail_names = {
        row[1]: row[2]
        for row in book["明細_A"].iter_rows(min_row=2, values_only=True)
    }
    assert detail_names["111058302001"] == "HYDRA 7PC"
    assert detail_names["111058302002"] == "HYDRA 14PC"

    params = [row for row in book["參數"].iter_rows(values_only=True)]
    assert any(row[0] == "共用 Target" for row in params)


def _sap_safety_bytes(rows) -> bytes:
    """SAP export carrying Safety Stock, the floor a group member is lifted to."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Article", "Site", "SaSa Net Stock", "Safety Stock", "MOQ"])
    for sku, site, qty, safety, moq in rows:
        ws.append([sku, site, qty, safety, moq])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_sap_export_reads_safety_stock(client):
    from app import excel_io

    parsed = excel_io.parse_stock_file(
        _sap_safety_bytes([("111058302001", "HA19", 0, 120, 6),
                           ("111058302001", "HA19", 0, 120, 6)])
    )
    assert not parsed.errors
    assert parsed.mapped["safety"] == "Safety Stock"
    # Repeating the row must not double the floor, nor let a blank cancel it.
    assert parsed.rows[0].safety == 120
    assert parsed.rows[0].qty == 0


def test_stock_file_without_safety_stock_reports_none(client):
    from app import excel_io

    parsed = excel_io.parse_stock_file((SAMPLES / "sample_stock_wide.xlsx").read_bytes())
    assert "safety" not in [k for k, v in parsed.mapped.items() if v]
    assert all(row.safety == 0 for row in parsed.rows)


def _group_target_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append(["111058302001", "HYDRA 7PC", 400])
    ws.append(["111058302002", "HYDRA 14PC", None])
    ws.merge_cells("C2:C3")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_group_topup_runs_from_the_run_form(client):
    """A covered group still gets a floor, and the export says so."""
    from openpyxl import Workbook, load_workbook

    login(client, "adminpw", "Ricky")
    shop = [s["code"] for s in db.list_shops(active_only=True)][0]

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "店舖", "庫存", "Last Month Sold Qty", "Safety Stock"])
    ws.append(["111058302001", shop, 0, 800, 100])
    ws.append(["111058302002", shop, 450, 200, 0])
    stock = io.BytesIO()
    wb.save(stock)

    resp = client.post(
        "/run/new",
        data={"period": "2026-11", "group_topup": "1", **PROMO},
        files={
            "target_file": ("shared.xlsx", _group_target_bytes()),
            "stock_file": ("stock.xlsx", stock.getvalue()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]
    run = runstore.get(token)
    assert run is not None and run.params["group_topup"] is True
    assert run.params["has_safety"] is True

    res = run.results["A"]
    a = next(s for s in res.skus if s.sku == "111058302001")
    b = next(s for s in res.skus if s.sku == "111058302002")
    # The group is covered by the 14PC's 450 on hand in this shop, so only the
    # floor moves there; the other shops are short of their own shares.
    assert a.enough and b.enough
    assert a.topup_qty == 100
    at_shop = {
        ln.sku: ln for ln in res.lines if ln.shop_code == shop
    }
    assert at_shop["111058302001"].dispatch_qty == 100
    assert at_shop["111058302001"].topup_qty == 100
    assert at_shop["111058302002"].dispatch_qty == 0
    assert any("貨組補底" in w for w in res.warnings)

    page = client.get(f"/run/{token}")
    assert "Safety 補底" in page.text

    export = client.get(f"/run/{token}/export.xlsx")
    book = load_workbook(io.BytesIO(export.content))
    detail = book["明細_A"]
    head = [c.value for c in detail[1]]
    topup_col = head.index("補底") + 1
    safety_col = head.index("Safety Stock") + 1
    rows = {
        (detail.cell(row=r, column=2).value, detail.cell(row=r, column=4).value): r
        for r in range(2, detail.max_row + 1)
    }
    floor_row = rows[("111058302001", shop)]
    assert detail.cell(row=floor_row, column=topup_col).value == 100
    assert detail.cell(row=floor_row, column=safety_col).value == 100
    # The pack that already covered the group was not topped up.
    assert not detail.cell(row=rows[("111058302002", shop)], column=topup_col).value

    params = {row[0]: row[1] for row in book["參數"].iter_rows(values_only=True)}
    assert "Safety" in params["貨組補底"]
    assert "Safety Stock" in params["Safety Stock 來源"]

    # Hover notes must stay on their own cell. openpyxl's write-only mode reuses
    # one cell object per row and only replaces it after a styled cell, so a
    # comment on a plain cell used to be copied to everything to its right.
    compare = book["比較"]
    assert compare["B2"].comment is not None      # 貨組 note on the SKU cell
    assert compare["C2"].comment is None          # … and not on the name
    # 17 fixed columns, then 需求 / 派貨 / 開 PO / 缺貨: the row that carries the
    # floor says so on its 派貨 cell, and only there.
    assert compare.cell(row=2, column=19).comment is not None
    assert compare.cell(row=2, column=20).comment is None
    assert compare.cell(row=3, column=19).comment is None


def test_group_topup_is_off_when_the_box_is_not_ticked(client):
    from openpyxl import Workbook

    login(client, "adminpw", "Ricky")
    shop = [s["code"] for s in db.list_shops(active_only=True)][0]

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "店舖", "庫存", "Last Month Sold Qty", "Safety Stock"])
    ws.append(["111058302001", shop, 0, 800, 100])
    ws.append(["111058302002", shop, 450, 200, 0])
    stock = io.BytesIO()
    wb.save(stock)

    resp = client.post(
        "/run/new",
        data={"period": "2026-11", **PROMO},  # no group_topup = unchecked
        files={
            "target_file": ("shared.xlsx", _group_target_bytes()),
            "stock_file": ("stock.xlsx", stock.getvalue()),
        },
        follow_redirects=False,
    )
    token = resp.headers["location"].rstrip("/").split("/")[-1]
    run = runstore.get(token)
    assert run.params["group_topup"] is False
    res = run.results["A"]
    at_shop = {ln.sku: ln for ln in res.lines if ln.shop_code == shop}
    assert at_shop["111058302001"].dispatch_qty == 0
    assert at_shop["111058302001"].topup_qty == 0


def _sap_supply_bytes(rows) -> bytes:
    """SAP export carrying Supply source, the column that picks the route."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(
        [
            "Article", "Article Description", "Site", "Supply source",
            "RP Type", "SaSa Net Stock", "Pending Received",
            "Last Month Sold Qty", "MTD Sold Qty",
        ]
    )
    for sku, desc, site, source, qty in rows:
        ws.append([sku, desc, site, source, "RF", qty, 0, 0, 0])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_supply_source_sends_vendor_shops_to_a_po(client):
    """1／4 行貨 goes on a PO; only the warehouse shop competes for D001."""
    period = "2027-04"
    sku = "111058303001"

    db.upsert_shop("HA01", "HA01 倉貨店", "HK")
    db.upsert_shop("HA02", "HA02 行貨店", "HK")
    db.save_shares(period, "A", {"HA01": 0.5, "HA02": 0.5}, "tester")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append([sku, "面膜", 200])
    target = io.BytesIO()
    wb.save(target)

    stock = _sap_supply_bytes(
        [
            # D001 holds 100 — exactly enough for the warehouse shop's half.
            (sku, "面膜", "D001", "2", 100),
            (sku, "面膜", "HA01", "2", 0),
            (sku, "面膜", "HA02", "1", 0),   # 行貨
        ]
    )

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/new",
        data={"period": period, **PROMO},
        files={
            "target_file": ("target.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith("/run/"), resp.headers["location"]
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    assert run.params["has_supply_source"] is True
    assert run.params["vendor_supply_sources"] == ["1", "4"]

    lines = {ln.shop_code: ln for ln in run.results["A"].lines}
    vendor = lines["HA02"]
    assert vendor.vendor is True
    assert vendor.supply_source == "1"
    assert vendor.po_qty == 100          # full need, not limited by D001
    assert vendor.sto_qty == 0
    assert vendor.short_qty == 0

    warehouse = lines["HA01"]
    assert warehouse.vendor is False
    assert warehouse.po_qty == 0
    assert warehouse.dispatch_qty == 100  # D001 not shared with the vendor shop

    summary = run.results["A"].skus[0]
    assert summary.po_qty == 100
    assert summary.sto_qty == 100
    assert summary.total_dispatch == 200
    assert summary.short_qty == 0
    assert any("開 PO" in w for w in run.params["warnings"]["A"])

    page = client.get(f"/run/{token}")
    assert page.status_code == 200
    assert "需開 PO" in page.text
    assert "行貨" in page.text

    detail = client.get(f"/run/{token}/detail?scenario=A&sku={sku}")
    assert detail.status_code == 200
    assert "貨源" in detail.text
    assert "HA02" in detail.text

    export = client.get(f"/run/{token}/export.xlsx")
    assert export.status_code == 200
    from openpyxl import load_workbook

    wb2 = load_workbook(io.BytesIO(export.content), read_only=True)
    compare_head = [c for c in next(wb2["比較"].iter_rows(values_only=True))]
    # Per scenario, because the PO quantity follows the share mix. The header
    # says 全期: a PO covers the whole promotion, not the dispatch window.
    assert "A 開 PO（全期）" in compare_head
    assert "開 PO" not in compare_head
    detail_head = [c for c in next(wb2["明細_A"].iter_rows(values_only=True))]
    assert "Supply Source" in detail_head
    assert "可派 STO" in detail_head
    assert "開 PO（全期）" in detail_head
    assert "分配基準" in detail_head
    wb2.close()


def test_vendor_po_covers_the_whole_promotion(client):
    """A 4-day dispatch still buys the full period for a vendor shop.

    The buyer raises one PO for the whole promotion, so the vendor shop is
    measured against the full target while the warehouse shop is not.
    """
    period = "2027-05"
    sku = "111058304001"

    db.upsert_shop("HB01", "HB01 倉貨店", "HK")
    db.upsert_shop("HB02", "HB02 行貨店", "HK")
    db.save_shares(period, "A", {"HB01": 0.5, "HB02": 0.5}, "tester")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append([sku, "精華", 1200])
    target = io.BytesIO()
    wb.save(target)

    stock = _sap_supply_bytes(
        [
            (sku, "精華", "D001", "2", 300),
            (sku, "精華", "HB01", "2", 0),
            (sku, "精華", "HB02", "4", 0),   # 行貨（經 D001 過數）
        ]
    )

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/new",
        data={
            "period": period,
            "promo_start": "2026-11-02",
            "promo_end": "2026-11-13",      # 12 days
            "dispatch_days": "4",           # but only 4 days are sent
        },
        files={
            "target_file": ("target.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    lines = {ln.shop_code: ln for ln in run.results["A"].lines}

    vendor = lines["HB02"]
    assert vendor.vendor is True
    assert vendor.alloc_full_period is True
    assert vendor.alloc_qty == 600            # half of the whole 1,200
    assert vendor.po_qty == 600

    warehouse = lines["HB01"]
    assert warehouse.alloc_full_period is False
    assert warehouse.alloc_qty == 200         # half of 1,200 × 4/12
    assert warehouse.dispatch_qty == 200      # D001 has 300, so nothing short

    summary = run.results["A"].skus[0]
    assert summary.po_qty == 600
    assert summary.po_target == 600
    assert summary.dispatch_target == 400
    assert any("全期" in w for w in run.params["warnings"]["A"])
    # 4 is the route that passes through D001 without being warehouse stock.
    assert run.params["supply_four_shops"] == ["HB02"]
    assert any("D001 過數" in w for w in run.params["warnings"]["A"])

    page = client.get(f"/run/{token}")
    assert page.status_code == 200
    assert "需開 PO（行貨 · 全期）" in page.text
    assert "經 D001 過數" in page.text

    detail = client.get(f"/run/{token}/detail?scenario=A&sku={sku}")
    assert detail.status_code == 200
    assert "行貨全期目標" in detail.text
    assert "全期" in detail.text
    # The two target bases are labelled with their day counts, so 600 over 12
    # days next to 200 over the first 4 cannot be mistaken for a mistake.
    assert "全期目標" in detail.text
    assert "12日 Target" in detail.text
    assert "4日 Target" in detail.text

    export = client.get(f"/run/{token}/export.xlsx")
    from openpyxl import load_workbook

    wb2 = load_workbook(io.BytesIO(export.content), read_only=True)
    rows = list(wb2["明細_A"].iter_rows(values_only=True))
    head = list(rows[0])
    basis = head.index("分配基準")
    full = head.index("全期目標（12日 Target）")
    windowed = head.index("分配數量（4日 Target）")
    by_shop = {r[3]: r for r in rows[1:]}
    assert by_shop["HB02"][basis] == "全期"
    assert by_shop["HB01"][basis] == "派貨期"
    # 全期目標 = the shop's share of the whole promotion target, whoever
    # supplies it; 分配數量 = what this route is actually measured against.
    assert by_shop["HB02"][full] == 600        # half of 1,200
    assert by_shop["HB02"][windowed] == 600    # 行貨: same, the PO covers 12 days
    assert by_shop["HB01"][full] == 600        # half of 1,200
    assert by_shop["HB01"][windowed] == 200    # 倉貨: half of 1,200 × 4/12
    wb2.close()


def test_export_colours_what_has_to_be_raised(client):
    """The three action columns are tinted, and the sheet is laid out to be read.

    Ops already paints 開 PO yellow, 可派 STO orange and 缺貨 red by hand every
    month; the export should hand that back ready to send, with the header
    frozen and a filter so column 17 is still identifiable at row 150.
    """
    period = "2027-07"
    sku = "111058305001"

    db.upsert_shop("HB01", "HB01 倉貨店", "HK")
    db.upsert_shop("HB02", "HB02 行貨店", "HK")
    db.save_shares(period, "A", {"HB01": 0.5, "HB02": 0.5}, "tester")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append([sku, "面膜", 400])
    target = io.BytesIO()
    wb.save(target)

    # D001 holds 50 against a 200-piece warehouse demand: 50 goes out on an STO
    # and 150 is short. The vendor shop covers the whole period with a PO.
    stock = _sap_supply_bytes(
        [
            (sku, "面膜", "D001", "2", 50),
            (sku, "面膜", "HB01", "2", 0),
            (sku, "面膜", "HB02", "4", 0),
        ]
    )

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/new",
        data={
            "period": period,
            "promo_start": "2027-07-05",
            "promo_end": "2027-07-08",     # 4 days, all of them dispatched
            "dispatch_days": "4",
        },
        files={
            "target_file": ("target.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    from openpyxl import load_workbook

    export = client.get(f"/run/{token}/export.xlsx")
    wb2 = load_workbook(io.BytesIO(export.content))     # not read_only: styles
    ws = wb2["明細_A"]

    head = [c.value for c in ws[1]]
    sto = head.index("可派 STO") + 1
    po = head.index("開 PO（全期）") + 1
    short = head.index("缺貨") + 1
    name_col = head.index("店舖名稱") + 1
    rows = {ws.cell(row=r, column=4).value: r for r in range(2, ws.max_row + 1)}

    # The shop name sits right after its code: the sheet is read by people who
    # know "HB01 倉貨店" better than "HB01".
    assert head[4] == "店舖名稱"
    assert ws.cell(row=rows["HB01"], column=name_col).value == "HB01 倉貨店"
    assert ws.cell(row=rows["HB02"], column=name_col).value == "HB02 行貨店"

    # Warehouse shop: 50 on an STO, 150 short, nothing on a PO.
    hb01 = rows["HB01"]
    assert ws.cell(row=hb01, column=sto).value == 50
    assert ws.cell(row=hb01, column=sto).fill.fgColor.rgb == "FFFFC000"
    assert ws.cell(row=hb01, column=short).value == 150
    assert ws.cell(row=hb01, column=short).fill.fgColor.rgb == "FFFFC7CE"
    assert not ws.cell(row=hb01, column=po).value
    assert ws.cell(row=hb01, column=po).fill.fill_type is None

    # Vendor shop: 200 on a PO, and no STO colour bleeding into that column.
    hb02 = rows["HB02"]
    assert ws.cell(row=hb02, column=po).value == 200
    assert ws.cell(row=hb02, column=po).fill.fgColor.rgb == "FFFFFF00"
    assert not ws.cell(row=hb02, column=sto).value
    assert ws.cell(row=hb02, column=sto).fill.fill_type is None

    # Layout: the identifying columns stay put, the header wraps, and the
    # filter covers exactly the rows that exist.
    assert ws.freeze_panes == "F2"
    # The filter follows the header, so a new column cannot leave it short.
    assert ws.auto_filter.ref == f"A1:AA{1 + len(rows)}"
    assert ws.row_dimensions[1].height >= 40
    assert ws["A1"].alignment.wrap_text is True
    assert ws.column_dimensions["C"].width >= 30

    compare = wb2["比較"]
    chead = [c.value for c in compare[1]]
    cpo = chead.index("A 開 PO（全期）") + 1
    assert compare.cell(row=2, column=cpo).fill.fgColor.rgb == "FFFFFF00"
    assert compare.freeze_panes == "D2"
    wb2.close()


def test_export_states_the_promo_window_on_every_row(client):
    """「推廣時期」「日數」ride on each row of the comparison sheet.

    The sheet leaves the portal, and 派貨目標 is only a fraction of Target, so
    the window it was scaled from has to be on the same line: 280 next to 700
    looks like a mistake without it.
    """
    period = "2027-08"
    sku = "111058306001"

    db.upsert_shop("HC01", "HC01 門市", "HK")
    db.save_shares(period, "A", {"HC01": 1.0}, "tester")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append([sku, "面膜", 700])
    target = io.BytesIO()
    wb.save(target)

    wb = Workbook()
    ws = wb.active
    ws.append(["Article", "Site", "SaSa Net Stock"])
    ws.append([sku, "HC01", 0])
    stock = io.BytesIO()
    wb.save(stock)

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/new",
        data={
            "period": period,
            "promo_start": "2027-08-02",
            "promo_end": "2027-08-11",     # 10 days, the first 4 dispatched
            "dispatch_days": "4",
        },
        files={
            "target_file": ("target.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock.getvalue()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(client.get(f"/run/{token}/export.xlsx").content))
    ws = book["比較"]
    head = [c.value for c in ws[1]]
    assert head[3] == "推廣時期"
    assert head[4] == "日數"

    row = [c.value for c in ws[2]]
    assert row[3] == "2027-08-02 → 2027-08-11"
    assert row[4] == "10 日（派 4 日）"
    assert row[5] == 700
    assert row[6] == 280                  # 700 × 4/10

    # The hover note says which of the two day counts scales the target.
    assert ws["E1"].comment is not None
    assert "4/10" in ws["E1"].comment.text


def test_supply_source_column_missing_is_reported(client):
    """No column: every shop is 倉貨 and the page says so, rather than assuming."""
    period = "2027-05"
    sku = "111058303002"

    db.upsert_shop("HA03", "HA03", "HK")
    db.save_shares(period, "A", {"HA03": 1.0}, "tester")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append([sku, "面膜", 60])
    target = io.BytesIO()
    wb.save(target)

    wb = Workbook()
    ws = wb.active
    ws.append(["Article", "Site", "SaSa Net Stock", "RP Type", "MOQ"])
    ws.append([sku, "HA03", 0, "RF", 6])
    stock = io.BytesIO()
    wb.save(stock)

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/new",
        data={"period": period, **PROMO},
        files={
            "target_file": ("target.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock.getvalue()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    assert run.params["has_supply_source"] is False
    assert run.params["vendor_supply_sources"] == []
    assert run.results["A"].skus[0].po_qty == 0
    assert any("沒有 Supply Source 欄位" in w for w in run.params["warnings"]["A"])


def test_supply_source_codes_are_configurable(client):
    login(client, "adminpw", "Ricky")
    try:
        resp = client.post(
            "/admin/settings",
            data={
                "label_A": "恆常推廣", "label_B": "精彩價&PIP", "label_C": "",
                "warehouse_sites": "D001",
                "rp_gate_shops": "HB87",
                "vendor_supply_sources": "1,4,5",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        assert db.vendor_supply_sources() == ["1", "4", "5"]
        page = client.get("/admin/settings")
        assert "1,4,5" in page.text
    finally:
        db.set_setting(db.SUPPLY_KEY, db.DEFAULT_SUPPLY)
    assert db.vendor_supply_sources() == ["1", "4"]


# ---------------------------------------------------------------------------
# promo dates printed in the target file
# ---------------------------------------------------------------------------


def _promo_target(rows, merge_dates=False) -> bytes:
    """A target sheet whose promo window sits on its lines, as ops sends it."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty", "Start date (Promo)", "End date (Promo)"])
    for row in rows:
        ws.append(list(row))
    if merge_dates and len(rows) > 1:
        ws.merge_cells(start_row=2, start_column=4, end_row=1 + len(rows), end_column=4)
        ws.merge_cells(start_row=2, start_column=5, end_row=1 + len(rows), end_column=5)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_promo_dates_are_read_from_the_target_file():
    from app import excel_io

    parsed = excel_io.parse_target_file(
        _promo_target(
            [
                ("111058302001", "HYDRA 7PC", 4000, "17-Nov-26", "26-Nov-26"),
                ("111058302002", "HYDRA 14PC", 1200, "17-Nov-26", "26-Nov-26"),
            ]
        )
    )
    assert parsed.ok, parsed.errors
    assert parsed.promo_start == date(2026, 11, 17)
    assert parsed.promo_end == date(2026, 11, 26)
    assert parsed.warnings == [], parsed.warnings


def test_merged_promo_dates_are_read_once():
    """Dates merged over the block still count, as they do for shared targets."""
    from app import excel_io

    parsed = excel_io.parse_target_file(
        _promo_target(
            [
                ("111058302001", "HYDRA 7PC", 4000, "17-Nov-26", "26-Nov-26"),
                ("111058302002", "HYDRA 14PC", 1200, None, None),
            ],
            merge_dates=True,
        )
    )
    assert parsed.promo_start == date(2026, 11, 17)
    assert parsed.promo_end == date(2026, 11, 26)


def test_two_promo_rounds_keep_the_most_used_and_say_so():
    from app import excel_io

    parsed = excel_io.parse_target_file(
        _promo_target(
            [
                ("111058302001", "HYDRA 7PC", 4000, "17-Nov-26", "26-Nov-26"),
                ("111058302002", "HYDRA 14PC", 1200, "17-Nov-26", "26-Nov-26"),
                ("111058302003", "HYDRA 28PC", 800, "01-Dec-26", "10-Dec-26"),
            ]
        )
    )
    assert parsed.promo_start == date(2026, 11, 17)
    assert parsed.promo_end == date(2026, 11, 26)
    assert any("不止一組推廣日期" in w for w in parsed.warnings)


def test_blank_promo_columns_fall_back_to_the_form():
    from app import excel_io

    parsed = excel_io.parse_target_file(
        _promo_target([("111058302001", "HYDRA 7PC", 4000, None, None)])
    )
    assert parsed.promo_start is None and parsed.promo_end is None
    assert any("兩欄都是空白" in w for w in parsed.warnings)


def test_choosing_the_file_reports_the_dates_before_the_run(client):
    """The upload page asks this the moment a file is picked."""
    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/probe/dates",
        files={
            "target_file": (
                "with_dates.xlsx",
                _promo_target(
                    [
                        ("111058302001", "HYDRA 7PC", 4000, "17-Nov-26", "26-Nov-26"),
                        ("111058302002", "HYDRA 14PC", 1200, "17-Nov-26", "26-Nov-26"),
                    ]
                ),
            )
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["start"] == "2026-11-17"
    assert body["end"] == "2026-11-26"
    assert body["days"] == 10
    assert body["n_sku"] == 2


def test_file_without_dates_says_so_before_the_run(client):
    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/probe/dates",
        files={
            "target_file": (
                "no_dates.xlsx",
                _promo_target([("111058302001", "HYDRA 7PC", 4000, None, None)]),
            )
        },
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["start"] == ""
    assert any("推廣" in w for w in body["warnings"])


def test_probe_needs_a_session():
    with TestClient(app) as fresh:
        resp = fresh.post(
            "/run/probe/dates",
            files={"target_file": ("t.xlsx", b"x")},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_promo_dates_come_from_the_file_not_the_form(client):
    from openpyxl import Workbook

    login(client, "adminpw", "Ricky")
    shops = [s["code"] for s in db.list_shops(active_only=True)][:2]
    target = _promo_target(
        [("111058302001", "HYDRA 7PC", 4000, "17-Nov-26", "26-Nov-26")]
    )

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", shops[0], shops[1]])
    ws.append(["111058302001", 1000, 2000])
    stock = io.BytesIO()
    wb.save(stock)

    # No date_source field: automatic is what the upload page posts by default.
    resp = client.post(
        "/run/new",
        data={"period": "2026-11", **PROMO},
        files={
            "target_file": ("with_dates.xlsx", target),
            "stock_file": ("stock.xlsx", stock.getvalue()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    assert run is not None, resp.headers["location"]
    assert run.params["promo_source"] == "target_file"
    assert run.params["promo_start"] == "2026-11-17"
    assert run.params["promo_end"] == "2026-11-26"
    assert run.params["promo_days"] == 10
    # 4 dispatch days out of a 10-day promo: 4000 -> 1600.
    assert run.results["A"].skus[0].dispatch_target == 1600
    # The typed dates disagreed with the sheet, which must not pass silently.
    assert any("與 Target 檔不符" in w for w in run.params["warnings"]["A"])

    page = client.get(f"/run/{token}")
    assert "由 Target 檔讀取" in page.text


def test_manual_dates_override_the_target_file(client):
    from openpyxl import Workbook

    login(client, "adminpw", "Ricky")
    shops = [s["code"] for s in db.list_shops(active_only=True)][:2]
    target = _promo_target(
        [("111058302001", "HYDRA 7PC", 4000, "17-Nov-26", "26-Nov-26")]
    )

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", shops[0], shops[1]])
    ws.append(["111058302001", 1000, 2000])
    stock = io.BytesIO()
    wb.save(stock)

    resp = client.post(
        "/run/new",
        data={"period": "2026-11", **PROMO, "date_source": "manual"},
        files={
            "target_file": ("with_dates.xlsx", target),
            "stock_file": ("stock.xlsx", stock.getvalue()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    assert run is not None, resp.headers["location"]
    assert run.params["promo_source"] == "manual"
    assert run.params["promo_start"] == "2026-11-02"
    assert run.params["promo_days"] == 4
    # 4 dispatch days of a 4-day promo: the target is not scaled.
    assert run.results["A"].skus[0].dispatch_target == 4000


def test_legend_note_prefers_the_written_remark():
    """餐別與規則 的備註 wins; the built-in gloss is only a fallback."""
    from app.excel_io import _legend_note

    assert (
        _legend_note("A", {"label": "恆常推廣", "hint": "只在每月第一個星期五推"})
        == "A 餐 · 恆常推廣：只在每月第一個星期五推"
    )
    assert _legend_note("A", {"label": "恆常推廣", "remark": "舊快照沒有 hint"}) == (
        "A 餐 · 恆常推廣：舊快照沒有 hint"
    )
    assert _legend_note("C", {"label": "", "hint": ""}) == "C 餐"


def test_result_and_excel_say_what_abc_mean(client):
    """The legend is where the numbers are read: result page, 參數, headers."""
    from openpyxl import load_workbook

    login(client, "adminpw", "Ricky")
    # Self-contained: the legend test may run on its own, so make sure the month
    # it computes has shares to read.
    client.post(
        "/admin/shares/import",
        data={"period": "2026-11"},
        files={"file": ("shares.xlsx", (SAMPLES / "sample_shares.xlsx").read_bytes())},
        follow_redirects=False,
    )
    resp = client.post(
        "/run/new",
        data={"period": "2026-11", **PROMO},
        files={
            "target_file": ("t.xlsx", (SAMPLES / "sample_target.xlsx").read_bytes()),
            "stock_file": ("s.xlsx", (SAMPLES / "sample_stock_wide.xlsx").read_bytes()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    page = client.get(f"/run/{token}").text
    assert "餐別說明" in page
    assert "A 餐 · 恆常推廣" in page
    assert "每月恆常推出的推廣活動。" in page

    book = load_workbook(io.BytesIO(client.get(f"/run/{token}/export.xlsx").content))
    legend = [row for row in book["參數"].iter_rows(values_only=True) if row[0] == "餐別"]
    assert len(legend) == 3
    assert legend[0][1].startswith("A 餐 · 恆常推廣：")
    assert "每月恆常推出的推廣活動。" in legend[0][1]

    # The comparison header stays short; the plan name rides on its hover note.
    header = book["比較"]
    assert header["R1"].value == "A 需求"
    assert header["R1"].comment is not None
    assert "恆常推廣" in header["R1"].comment.text


def test_brand_code_is_read_as_a_label():
    """Brand code is copied from the sheet, never derived or guessed.

    Ops asked for it because an article number alone does not say whose SKU a
    line is; it is a label on the export, so nothing about the numbers may
    depend on it.
    """
    from openpyxl import Workbook

    from app import excel_io

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "SKU DESC", "Brand code", "Target Qty"])
    ws.append(["109991605010", "潔面乳 120ml", "BR01", 100])
    ws.append(["110331002001", "精華 30ml", 12, 200])  # a numeric code cell
    buf = io.BytesIO()
    wb.save(buf)

    parsed = excel_io.parse_target_file(buf.getvalue())
    assert parsed.ok, parsed.errors
    assert parsed.mapped["brand"] == "Brand code"
    assert [row.brand for row in parsed.rows] == ["BR01", "12"]

    # A sheet without the column keeps working; the export just leaves it blank.
    plain = Workbook()
    ws2 = plain.active
    ws2.append(["SKU", "Target Qty"])
    ws2.append(["109991605010", 100])
    buf2 = io.BytesIO()
    plain.save(buf2)

    parsed2 = excel_io.parse_target_file(buf2.getvalue())
    assert parsed2.ok, parsed2.errors
    assert parsed2.mapped["brand"] == ""
    assert parsed2.rows[0].brand == ""


def test_brand_code_leads_the_sku_in_the_export(client):
    """The export puts Brand Code in front of SKU on both sheets."""
    period = "2027-10"
    sku = "111058402001"

    db.upsert_shop("HD01", "HD01 門市", "HK")
    db.save_shares(period, "A", {"HD01": 1.0}, "tester")

    from openpyxl import Workbook, load_workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "SKU DESC", "Brand code", "Target Qty"])
    ws.append([sku, "面膜 5片裝", "BR07", 200])
    target = io.BytesIO()
    wb.save(target)

    wide = Workbook()
    ws2 = wide.active
    ws2.append(["SKU", "店舖", "庫存"])
    ws2.append([sku, "HD01", 40])
    stock = io.BytesIO()
    wide.save(stock)

    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/run/new",
        data={"period": period, **PROMO},
        files={
            "target_file": ("target.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock.getvalue()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    assert run is not None
    assert run.params["has_brand"] is True
    assert run.results["A"].skus[0].brand == "BR07"

    # On screen too, so the column can be checked without downloading anything.
    page = client.get(f"/run/{token}")
    assert "BR07" in page.text

    book = load_workbook(io.BytesIO(client.get(f"/run/{token}/export.xlsx").content))
    compare = list(book["比較"].iter_rows(values_only=True))
    assert compare[0][:2] == ("Brand Code", "SKU")
    assert compare[1][:2] == ("BR07", sku)

    detail = list(book["明細_A"].iter_rows(values_only=True))
    assert detail[0][:3] == ("Brand Code", "SKU", "貨品名稱")
    assert detail[1][0] == "BR07"
    assert detail[1][1] == sku

    notes = {
        row[0]: row[1]
        for row in book["參數"].iter_rows(values_only=True)
    }
    assert "Brand code" in notes["Brand Code 來源"]


def test_transfer_suggestion_is_opt_in_and_stays_inside_one_om(client):
    """建議調貨: off unless ticked, same OM only, and carried into the export."""
    from openpyxl import Workbook, load_workbook

    period = "2027-08"
    sku = "111058304004"

    # Two OMs, each with one shop short and one holding spare.
    db.upsert_shop("HA11", "HA11 缺貨店", "HK", 1, "OM 1")
    db.upsert_shop("HA12", "HA12 盈餘店", "HK", 1, "OM 1")
    db.upsert_shop("HA13", "HA13 缺貨店", "HK", 1, "OM 2")
    db.upsert_shop("HA14", "HA14 盈餘店", "HK", 1, "OM 2")
    db.save_shares(
        period,
        "A",
        {"HA11": 0.25, "HA12": 0.25, "HA13": 0.25, "HA14": 0.25},
        "tester",
    )

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append([sku, "面膜", 100])
    target = io.BytesIO()
    wb.save(target)

    # D001 holds nothing: the shops' only hope is each other.
    stock = _sap_consign_bytes(
        [
            (sku, "D001", 0, 0, "2"),
            (sku, "HA11", 0, 0, "2"),
            (sku, "HA12", 100, 0, "2"),
            (sku, "HA13", 0, 0, "2"),
            (sku, "HA14", 100, 0, "2"),
        ]
    )

    def run_with(extra: dict) -> str:
        login(client, "adminpw", "Ricky")
        resp = client.post(
            "/run/new",
            data={"period": period, **PROMO, **extra},
            files={
                "target_file": ("t.xlsx", target.getvalue()),
                "stock_file": ("s.xlsx", stock),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        return resp.headers["location"].rstrip("/").split("/")[-1]

    # Default: no advice, no sheet, and the parameter says it was off.
    token = run_with({})
    run = runstore.get(token)
    assert run.params["suggest_transfer"] is False
    assert run.results["A"].transfers == []
    book = load_workbook(io.BytesIO(client.get(f"/run/{token}/export.xlsx").content))
    assert "調貨建議" not in book.sheetnames
    notes = {row[0]: row[1] for row in book["參數"].iter_rows(values_only=True)}
    assert notes["建議調貨"] == "未開啟（預設）"

    # Ticked: one pair per OM, and the pair never crosses the OM boundary.
    token = run_with({"suggest_transfer": "1"})
    run = runstore.get(token)
    assert run.params["suggest_transfer"] is True
    assert [
        (t.om, t.from_shop, t.to_shop, t.qty) for t in run.results["A"].transfers
    ] == [("OM 1", "HA12", "HA11", 25), ("OM 2", "HA14", "HA13", 25)]
    # The dispatch itself is untouched: HA11 still shows the 25 it is short.
    line = {ln.shop_code: ln for ln in run.results["A"].lines}["HA11"]
    assert line.dispatch_qty == 0
    assert line.short_qty == 25

    page = client.get(f"/run/{token}/detail?scenario=A&sku={sku}")
    assert "建議調貨" in page.text
    assert "HA12" in page.text and "HA11" in page.text

    book = load_workbook(io.BytesIO(client.get(f"/run/{token}/export.xlsx").content))
    assert "調貨建議" in book.sheetnames
    transfer_rows = list(book["調貨建議"].iter_rows(values_only=True))
    assert transfer_rows[0][:4] == ("餐別", "SKU", "貨品名稱", "OM")
    assert transfer_rows[1] == ("A", sku, "面膜", "OM 1", "HA12", "HA12 盈餘店", "HA11", "HA11 缺貨店", 25)
    assert transfer_rows[2][0] == "A" and transfer_rows[2][8] == 25


def test_shares_can_be_saved_as_money_and_are_stored_as_percent(client):
    """The grid accepts $$ and converts it; the database only ever sees %.

    Storing the raw amounts would make one month's plan incomparable with the
    next, and the readiness check (total ≈ 100%) would never pass again.
    """
    login(client, "adminpw", "Ricky")

    resp = client.post(
        "/admin/shares",
        data={
            "period": "2027-03",
            "scenario": "B",
            "unit": "amt",
            "pct_HB01": "3000",
            "pct_HB02": "1000",
            "pct_HB03": "0",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "level=ok" in resp.headers["location"], resp.headers["location"]

    shares = db.get_shares("2027-03", "B")
    assert shares == {"HB01": pytest.approx(0.75), "HB02": pytest.approx(0.25)}
    assert db.share_status("2027-03")["B"]["ready"] is True

    note = db.get_share_set("2027-03", "B")["note"]
    assert "總額" in note and "4,000" in note


def test_shares_with_zero_money_are_refused(client):
    """No total means no ratio: refuse instead of saving a grid of zeros."""
    login(client, "adminpw", "Ricky")
    resp = client.post(
        "/admin/shares",
        data={"period": "2027-03", "scenario": "C", "unit": "amt", "pct_HB01": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "level=error" in resp.headers["location"], resp.headers["location"]
    assert db.get_shares("2027-03", "C") == {}


def test_temp_meal_is_a_fourth_meal_that_is_never_stored(client):
    """A one-off mix must show up in the result and the export, not on disk."""
    login(client, "adminpw", "Ricky")

    shops = [s["code"] for s in db.list_shops(active_only=True)]
    # Ascending weights, so the mix cannot coincide with the sample A/B/C split.
    pasted = "\n".join(
        f"{code}\t{(i + 1) * 1000}" for i, code in enumerate(shops)
    )

    target = SAMPLES / "sample_target.xlsx"
    stock = SAMPLES / "sample_stock_wide.xlsx"
    resp = client.post(
        "/run/new",
        data={
            "period": "2026-11",
            **PROMO,
            "temp_on": "1",
            "temp_name": "聖誕特別場",
            "temp_unit": "amt",
            "temp_rows": pasted,
        },
        files={
            "target_file": (target.name, target.read_bytes()),
            "stock_file": (stock.name, stock.read_bytes()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    run = runstore.get(token)
    assert run is not None
    assert run.params["scenarios"] == ["A", "B", "C", "T"]
    assert set(run.results) == {"A", "B", "C", "T"}

    # The temp mix allocates the whole target too, just across a different set
    # of shops: it is another meal, not a patch on A/B/C.
    for summary in run.results["T"].skus:
        assert summary.total_alloc == summary.target_qty
    assert run.params["share_totals"]["T"] == pytest.approx(1.0)
    assert run.params["temp_label"] == "聖誕特別場"
    assert run.params["temp_amount"] == pytest.approx(sum((i + 1) * 1000 for i in range(len(shops))))

    # It says out loud that it is not saved.
    assert any("沒有存入資料庫" in w for w in run.results["T"].warnings)

    # Nothing was written to the month: no T scenario, A/B/C untouched.
    assert db.get_shares("2026-11", "T") == {}
    assert set(db.share_status("2026-11")) == set(db.SCENARIOS)

    # The mix really is different: a shop the monthly grid leaves at 0% now
    # takes a share, so the two allocations cannot be identical.
    sku = run.results["T"].skus[0].sku
    alloc_a = {ln.shop_code: ln.alloc_qty for ln in run.results["A"].lines if ln.sku == sku}
    alloc_t = {ln.shop_code: ln.alloc_qty for ln in run.results["T"].lines if ln.sku == sku}
    assert alloc_a != alloc_t

    # --- the page and the workbook both carry the fourth column ---
    page = client.get(f"/run/{token}")
    assert page.status_code == 200
    assert "A／B／C／T 比較" in page.text
    assert "聖誕特別場" in page.text

    detail = client.get(f"/run/{token}/detail?scenario=T")
    assert detail.status_code == 200
    assert "臨時餐" in detail.text

    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(client.get(f"/run/{token}/export.xlsx").content))
    assert "明細_T" in book.sheetnames
    header = list(book["比較"].iter_rows(max_row=1, values_only=True))[0]
    assert "T 派貨" in header
    assert "T 開 PO（全期）" in header

    n_shops = len(db.active_shop_codes())
    assert len(list(book["明細_T"].iter_rows(values_only=True))) == 1 + 10 * n_shops

    # 參數 must carry the caveat: the workbook outlives the result, and the next
    # run will not have a T.
    param_rows = [r for r in book["參數"].iter_rows(values_only=True) if r and r[0]]
    assert any(
        key == "餐別" and str(value).startswith("T 餐 · 聖誕特別場")
        for key, value in param_rows
    )
    assert any(
        key == "臨時餐" and "沒有存入資料庫" in str(value)
        for key, value in param_rows
    )

