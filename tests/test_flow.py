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
    ws = wb["比較"]
    header = [c for c in next(ws.iter_rows(values_only=True))]
    assert header[0] == "SKU"
    assert "A 派貨" in header
    assert "派貨目標" in header
    assert "RP 不派" in header
    assert "倉存" in header

    # The per-shop detail sheets must actually carry rows: this is the file the
    # warehouse works from, so an empty sheet would be a silent failure.
    detail_rows = list(wb["明細_A"].iter_rows(values_only=True))
    assert len(detail_rows) == 1 + 10 * 20
    assert detail_rows[0][:3] == ("SKU", "貨品名稱", "店舖")

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
    buf = io.BytesIO()
    wb.save(buf)

    stock = _sap_stock_bytes(
        [
            ("111058302001", "面膜", "ND", 0, "HB87"),   # blocked
            ("111058302001", "面膜", "RF", 0, "HA01"),
            ("111058302002", "精華", "RF", 0, "HB87"),   # allowed
            ("111058302002", "精華", "RF", 0, "HA01"),
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

    # The SKU-level numbers exclude the blocked half, so 需求/可派 reconcile.
    sku_row = next(s for s in run.results["A"].skus if s.sku == "111058302001")
    assert sku_row.need_qty == 50
    assert sku_row.total_dispatch == 50
    assert sku_row.blocked_qty == 50

    result = client.get(f"/run/{token}")
    assert "RP Type" in result.text


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
    warnings = " ".join(params["warnings"]["A"])
    assert "已忽略" in warnings and "MOQ" in warnings


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
    compare = [c.value for c in book["比較"][2]]
    assert compare[0] == "111058302001 + 111058302002"
    assert compare[2] == 4000
    params = [row for row in book["參數"].iter_rows(values_only=True)]
    assert any(row[0] == "共用 Target" for row in params)


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
    # Per scenario, because the PO quantity follows the share mix.
    assert "A 開 PO" in compare_head
    assert "開 PO" not in compare_head
    detail_head = [c for c in next(wb2["明細_A"].iter_rows(values_only=True))]
    assert "Supply Source" in detail_head
    assert "可派 STO" in detail_head
    assert "開 PO" in detail_head
    wb2.close()


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
