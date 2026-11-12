"""End-to-end flow: login → import shares → upload → compute → export."""

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


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
        data={"period": "2026-11"},
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
        data={"period": "2026-11"},
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
    assert "A 派貨合計" in header


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
        data={"period": "2026-11"},
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


def test_user_role_cannot_open_admin(client):
    client.cookies.clear()
    login(client, "userpw", "buyer")
    resp = client.get("/admin/shares", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]

    resp = client.get("/run/new")
    assert resp.status_code == 200
