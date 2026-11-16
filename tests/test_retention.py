"""What a calculation may and may not leave behind.

The rule from the business side: keep the monthly shares, keep a note of when
the tool was used, and store nothing about the uploaded target or the computed
dispatch. These tests pin that down so a future change cannot quietly start
persisting commercial data again.
"""

import io

import pytest
from fastapi.testclient import TestClient

from app import db, runstore
from app.main import app

SAMPLES = __import__("pathlib").Path(__file__).resolve().parents[1] / "samples"

PROMO = {
    "promo_start": "2026-11-02",
    "promo_end": "2026-11-05",
    "dispatch_days": "4",
}

LEGACY = ("upload", "sku_target", "shop_stock", "run", "run_sku", "run_line")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> None:
    resp = client.post(
        "/login",
        data={"password": "adminpw", "name": "tester", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def _tables() -> set:
    with db.db() as conn:
        return {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def test_legacy_calculation_tables_are_gone():
    names = _tables()
    assert not (set(LEGACY) & names), "計算內容不應該再有地方可以存"
    assert {"shop", "share_set", "share_entry", "usage_log"} <= names


def test_usage_log_only_holds_metadata():
    with db.db() as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(usage_log)")}
    # A timestamp, who, which month, which plans, how big — nothing per SKU.
    assert columns == {"id", "at", "user", "period", "scenarios", "n_sku", "n_short"}
    assert not any(c in columns for c in ("sku", "target_qty", "stock_qty", "payload"))


def test_a_run_leaves_no_numbers_in_the_database(client):
    _login(client)
    db.upsert_shop("HA99", "HA99 測試店", "HK")
    db.save_shares("2028-01", "A", {"HA99": 1.0}, "tester")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append(["999000000001", "測試貨品", 100])
    target = io.BytesIO()
    wb.save(target)

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "店舖", "庫存"])
    ws.append(["999000000001", "HA99", 0])
    stock = io.BytesIO()
    wb.save(stock)

    resp = client.post(
        "/run/new",
        data={"period": "2028-01", **PROMO},
        files={
            "target_file": ("target.xlsx", target.getvalue()),
            "stock_file": ("stock.xlsx", stock.getvalue()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    # The result is readable now …
    assert client.get(f"/run/{token}").status_code == 200
    assert runstore.get(token) is not None

    # … and the database knows only that it happened.
    entry = db.list_usage(limit=1)[0]
    assert entry["period"] == "2028-01"
    assert entry["n_sku"] == 1
    with db.db() as conn:
        dump = "\n".join(
            str(row)
            for table in _tables()
            for row in conn.execute(f"SELECT * FROM {table}")
        )
    assert "999000000001" not in dump, "SKU 不應出現在資料庫任何一處"


def test_expired_result_explains_itself(client):
    _login(client)
    resp = client.get("/run/does-not-exist", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/runs")
    assert "msg=" in location


def test_store_drops_the_oldest_when_full():
    runstore.clear()
    calls = []
    for _ in range(runstore.MAX_RUNS + 2):
        calls.append(runstore.put("2028-02", db.now(), "tester", {}, {}))
    assert len(runstore._STORE) == runstore.MAX_RUNS
    assert runstore.get(calls[0].id) is None
    assert runstore.get(calls[-1].id) is not None
    runstore.clear()


def test_timestamps_are_hong_kong_wall_clock():
    """A Railway container is UTC; the people reading the log are in Hong Kong."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    stamp = db.now()
    # Naive on purpose: every template shows it as-is.
    assert "+" not in stamp and stamp.count(":") == 2

    stored = datetime.fromisoformat(stamp)
    hk_now = datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
    assert abs(hk_now - stored) < timedelta(minutes=2)

    assert db.today().isoformat() == hk_now.date().isoformat()
    assert db.current_period() == hk_now.strftime("%Y-%m")
