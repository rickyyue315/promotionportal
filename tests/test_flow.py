"""End-to-end flow: login → import shares → upload → compute → export."""

import io
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
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
    run_id = int(resp.headers["location"].rstrip("/").split("/")[-1])

    # --- result page renders and the numbers are sane ---
    page = client.get(f"/run/{run_id}")
    assert page.status_code == 200
    assert "A／B／C 比較" in page.text

    rows = db.get_run_skus(run_id, "A")
    assert len(rows) == 10

    # Allocation must reconcile with the SKU target in every scenario.
    for sc in ("A", "B", "C"):
        for row in db.get_run_skus(run_id, sc):
            assert row["total_alloc"] == row["target_qty"], (sc, row["sku"])

    # Samples deliberately under-stock two SKUs; random noise may short others.
    short = {r["sku"] for r in db.get_run_skus(run_id, "A") if r["coverage_status"] == "short"}
    assert "111888009033" in short
    assert "110773004028" in short
    for row in db.get_run_skus(run_id, "A"):
        if row["sku"] in short:
            assert row["shortfall_qty"] == row["target_qty"] - row["total_stock"]
        else:
            assert row["shortfall_qty"] == 0

    # Same stock, different shares -> same shortfall, different dispatch.
    totals = {sc: db.run_totals(run_id, sc) for sc in ("A", "B", "C")}
    assert len({t["shortfall_qty"] for t in totals.values()}) == 1
    assert len({t["total_dispatch"] for t in totals.values()}) > 1

    # --- detail view, both aggregate and per-SKU ---
    detail = client.get(f"/run/{run_id}/detail?scenario=A")
    assert detail.status_code == 200
    assert "各店派貨總量" in detail.text

    sku = "109991605010"
    per_sku = client.get(f"/run/{run_id}/detail?scenario=A&sku={sku}")
    assert per_sku.status_code == 200
    assert sku in per_sku.text

    lines = db.get_run_lines(run_id, "A", sku)
    assert len(lines) == 20

    # --- export ---
    xlsx = client.get(f"/run/{run_id}/export.xlsx")
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
    ws.append(["SKU", "貨品名稱", "Target Qty", "MOQ"])
    ws.append(["111058302001", "面膜", 100, 1])
    ws.append(["111058302002", "精華", 100, 1])
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
    run_id = int(resp.headers["location"].rstrip("/").split("/")[-1])

    nd = db.get_run_lines(run_id, "A", "111058302001")
    nd_hb87 = next(r for r in nd if r["shop_code"] == "HB87")
    assert nd_hb87["dispatch_qty"] == 0
    assert nd_hb87["blocked"] == 1
    assert nd_hb87["blocked_qty"] == 50
    assert nd_hb87["rp_type"] == "ND"
    assert nd_hb87["gap_qty"] == 50  # the need is still visible

    rf = db.get_run_lines(run_id, "A", "111058302002")
    rf_hb87 = next(r for r in rf if r["shop_code"] == "HB87")
    assert rf_hb87["dispatch_qty"] == 50
    assert rf_hb87["blocked"] == 0

    detail = client.get(f"/run/{run_id}/detail?scenario=A&sku=111058302001")
    assert detail.status_code == 200
    assert "RP" in detail.text
    assert nd_hb87["need_qty"] == 50
    # Blocked by the RP rule, not by a lack of stock: the unit belongs to
    # blocked_qty only, so the shortage total stays about the warehouse.
    assert nd_hb87["short_qty"] == 0

    # The SKU-level numbers exclude the blocked half, so 需求/可派 reconcile.
    sku_row = next(r for r in db.get_run_skus(run_id, "A") if r["sku"] == "111058302001")
    assert sku_row["need_qty"] == 50
    assert sku_row["total_dispatch"] == 50
    assert sku_row["blocked_qty"] == 50

    result = client.get(f"/run/{run_id}")
    assert "RP Type" in result.text


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
    ws.append(["SKU", "貨品名稱", "Target Qty", "MOQ"])
    ws.append([sku_a, "面膜", 1400, 1])
    ws.append([sku_new, "新精華", 1400, 1])
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
    run_id = int(resp.headers["location"].rstrip("/").split("/")[-1])

    rows = {r["sku"]: r for r in db.get_run_skus(run_id, "A")}
    capped = rows[sku_a]
    assert capped["target_qty"] == 1400
    assert capped["dispatch_target"] == 400          # 1400 x 4/14
    assert capped["warehouse_qty"] == 100           # DC net stock is the ceiling
    assert capped["need_qty"] == 400
    assert capped["total_dispatch"] == 100
    assert capped["short_qty"] == 300

    # D001 never receives an allocation of its own.
    lines_a = db.get_run_lines(run_id, "A", sku_a)
    assert "D001" not in {ln["shop_code"] for ln in lines_a}
    assert {ln["shop_code"] for ln in lines_a if ln["share_pct"]} == {"HA19", "HA21"}

    # Sales columns made it through to the per-shop numbers. 5 days on record,
    # 50 sold per shop -> 10/day each, 20/day across the two shops.
    fresh = rows[sku_new]
    assert fresh["total_dispatch"] == 400
    assert fresh["expected_sales"] == 280
    assert fresh["gap_target"] == 1120

    lines = db.get_run_lines(run_id, "A", sku_new)
    assert [ln["daily_rate"] for ln in lines if ln["shop_code"] in ("HA19", "HA21")] == [10.0, 10.0]

    page = client.get(f"/run/{run_id}")
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

    # A batch keeps the wording it was computed with.
    runs = [r for r in db.list_runs(limit=50) if r["period"] == "2026-12"]
    run_id = runs[0]["id"]
    result = client.get(f"/run/{run_id}")
    assert "恆常推廣" in result.text

    client.post(
        "/admin/settings",
        data={"label_A": "改名後", "label_B": "精彩價&PIP", "label_C": "節日限定",
              "optional_C": "1", "rp_gate_shops": "HB87", "warehouse_sites": "D001"},
        follow_redirects=False,
    )
    assert "恆常推廣" in client.get(f"/run/{run_id}").text
    assert "改名後" not in client.get(f"/run/{run_id}").text


def test_user_role_cannot_open_admin(client):
    client.cookies.clear()
    login(client, "userpw", "buyer")
    resp = client.get("/admin/shares", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]

    resp = client.get("/run/new")
    assert resp.status_code == 200
