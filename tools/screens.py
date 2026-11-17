"""Dev tool: render pages to PNG for visual review.

Runs the app in-process with TestClient, walks through the real flow, inlines the
stylesheet, then screenshots each page with headless Chrome.

    python tools/screens.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA = ROOT / "tools" / "_shotdata"
SITE = ROOT / "tools" / "_shots"
for path in (DATA, SITE):
    path.mkdir(parents=True, exist_ok=True)

# Start from an empty database each run; screenshots overwrite by name.
for name in ("portal.db", "portal.db-wal", "portal.db-shm"):
    stale = DATA / name
    if stale.exists():
        stale.unlink()

os.environ["DATA_DIR"] = str(DATA)
os.environ["SECRET_KEY"] = "shot"
os.environ["ADMIN_PASSWORD"] = "adminpw"
os.environ["USER_PASSWORD"] = "userpw"
os.environ["COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")


def inline(html: str) -> str:
    return html.replace(
        '<link rel="stylesheet" href="/static/app.css">',
        f"<style>{CSS}</style>",
    )


def shoot(client: TestClient, path: str, name: str, height: int = 1200) -> Path:
    html = inline(client.get(path).text)
    file = SITE / f"{name}.html"
    file.write_text(html, encoding="utf-8")
    png = SITE / f"{name}.png"
    subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--window-size=1440,{height}",
            f"--screenshot={png}",
            file.as_uri(),
        ],
        check=True,
        capture_output=True,
    )
    print(f"  {png.name}")
    return png


def _hb87_demo(client: TestClient) -> str:
    """A small month that shows the HB87 RP Type gate in isolation."""
    from openpyxl import Workbook

    period = "2026-12"

    wb = Workbook()
    ws = wb.active
    ws.title = "Shares"
    ws.append(["Shop", "店舖名稱", "A"])
    ws.append(["HB87", "HB87 特殊店", 0.6])
    ws.append(["HA01", "HA01 門市", 0.4])
    buf = io.BytesIO()
    wb.save(buf)
    client.post(
        "/admin/shares/import",
        data={"period": period},
        files={"file": ("shares.xlsx", buf.getvalue())},
        follow_redirects=False,
    )

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append(["111058302001", "抗痘面膜 7片", 100])
    ws.append(["111058302002", "保濕精華 30ml", 80])
    tbuf = io.BytesIO()
    wb.save(tbuf)

    wb = Workbook()
    ws = wb.active
    ws.append(
        ["Article", "Article Description", "RP Type", "Safety Stock",
         "SaSa Net Stock", "Site", "MOQ"]
    )
    ws.append(["111058302001", "抗痘面膜 7片", "ND", 0, 0, "HB87", 6])
    ws.append(["111058302001", "抗痘面膜 7片", "RF", 0, 0, "HA01", 6])
    ws.append(["111058302002", "保濕精華 30ml", "RF", 0, 0, "HB87", 6])
    ws.append(["111058302002", "保濕精華 30ml", "RF", 0, 0, "HA01", 6])
    sbuf = io.BytesIO()
    wb.save(sbuf)

    resp = client.post(
        "/run/new",
        data={
            "period": period,
            # A 4-day promotion sent as 4 days: the target is not scaled, so the
            # screenshot shows the gate rather than the arithmetic.
            "promo_start": "2026-12-01",
            "promo_end": "2026-12-04",
            "dispatch_days": "4",
        },
        files={
            "target_file": ("target.xlsx", tbuf.getvalue()),
            "stock_file": ("stock.xlsx", sbuf.getvalue()),
        },
        follow_redirects=False,
    )
    return resp.headers["location"].rstrip("/").split("/")[-1]


def _shared_demo(client: TestClient) -> str:
    """Two pack sizes sharing one target, the way the real target file writes it."""
    from openpyxl import Workbook

    period = "2027-01"

    wb = Workbook()
    ws = wb.active
    ws.title = "Shares"
    ws.append(["Shop", "店舖名稱", "A"])
    ws.append(["HA01", "HA01 門市", 0.5])
    ws.append(["HA02", "HA02 門市", 0.3])
    ws.append(["HA03", "HA03 門市", 0.2])
    buf = io.BytesIO()
    wb.save(buf)
    client.post(
        "/admin/shares/import",
        data={"period": period},
        files={"file": ("shares.xlsx", buf.getvalue())},
        follow_redirects=False,
    )

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append(["111058302001", "HYDRA-AZ FACE MASK, N/A, 7PC", 4000])
    ws.append(["111058302002", "HYDRA-AZ FACE MASK, N/A, 14PC", None])
    ws.merge_cells("C2:C3")
    tbuf = io.BytesIO()
    wb.save(tbuf)

    wb = Workbook()
    ws = wb.active
    ws.append(
        ["Article", "Site", "SaSa Net Stock",
         "Last Month Sold Qty", "MTD Sold Qty", "MOQ"]
    )
    rows = [
        ("111058302001", "HA01", 1800, 800, 200, 6),
        ("111058302001", "HA02", 1000, 480, 120, 6),
        ("111058302001", "HA03", 600, 240, 60, 6),
        ("111058302001", "D001", 500, 0, 0, 6),
        ("111058302002", "HA01", 300, 200, 50, 6),
        ("111058302002", "HA02", 200, 120, 30, 6),
        ("111058302002", "HA03", 0, 40, 10, 6),
    ]
    for sku, site, stock, last, mtd, moq in rows:
        ws.append([sku, site, stock, last, mtd, moq])
    sbuf = io.BytesIO()
    wb.save(sbuf)

    resp = client.post(
        "/run/new",
        data={
            "period": period,
            "promo_start": "2027-01-05",
            "promo_end": "2027-01-08",
            "dispatch_days": "4",
        },
        files={
            "target_file": ("target.xlsx", tbuf.getvalue()),
            "stock_file": ("stock.xlsx", sbuf.getvalue()),
        },
        follow_redirects=False,
    )
    return resp.headers["location"].rstrip("/").split("/")[-1]


def _supply_demo(client: TestClient) -> str:
    """One 行貨 shop among 倉貨 shops: PO worklist next to the STO dispatch."""
    from openpyxl import Workbook

    period = "2027-06"

    wb = Workbook()
    ws = wb.active
    ws.title = "Shares"
    ws.append(["Shop", "店舖名稱", "A"])
    ws.append(["HA01", "HA01 門市", 0.4])
    ws.append(["HA02", "HA02 門市", 0.3])
    ws.append(["HA03", "HA03 行貨店", 0.3])
    buf = io.BytesIO()
    wb.save(buf)
    client.post(
        "/admin/shares/import",
        data={"period": period},
        files={"file": ("shares.xlsx", buf.getvalue())},
        follow_redirects=False,
    )

    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "貨品名稱", "Target Qty"])
    ws.append(["111058302001", "抗痘面膜 7片", 600])
    ws.append(["111058302002", "保濕精華 30ml", 300])
    tbuf = io.BytesIO()
    wb.save(tbuf)

    wb = Workbook()
    ws = wb.active
    ws.append(
        ["Article", "Article Description", "Site", "Supply source",
         "RP Type", "SaSa Net Stock", "Pending Received", "MOQ"]
    )
    # D001 holds 200, far less than the two 倉貨 shops need; HA03 is 行貨 and
    # therefore does not compete for it.
    rows = [
        ("111058302001", "抗痘面膜 7片", "D001", "2", "ND", 200, 0, 6),
        ("111058302001", "抗痘面膜 7片", "HA01", "2", "RF", 0, 0, 6),
        ("111058302001", "抗痘面膜 7片", "HA02", "2", "RF", 0, 0, 6),
        ("111058302001", "抗痘面膜 7片", "HA03", "1", "RF", 0, 0, 6),
        ("111058302002", "保濕精華 30ml", "D001", "2", "ND", 400, 0, 6),
        ("111058302002", "保濕精華 30ml", "HA01", "2", "RF", 20, 0, 6),
        ("111058302002", "保濕精華 30ml", "HA02", "2", "RF", 0, 0, 6),
        ("111058302002", "保濕精華 30ml", "HA03", "4", "RF", 0, 0, 6),
    ]
    for row in rows:
        ws.append(list(row))
    sbuf = io.BytesIO()
    wb.save(sbuf)

    resp = client.post(
        "/run/new",
        data={
            "period": period,
            # 12-day promotion, 4-day dispatch: the warehouse shops are only
            # stocked for four days while the 行貨 shop's PO covers the lot.
            "promo_start": "2027-06-01",
            "promo_end": "2027-06-12",
            "dispatch_days": "4",
        },
        files={
            "target_file": ("target.xlsx", tbuf.getvalue()),
            "stock_file": ("stock.xlsx", sbuf.getvalue()),
        },
        follow_redirects=False,
    )
    return resp.headers["location"].rstrip("/").split("/")[-1]


def main() -> None:
    samples = ROOT / "samples"
    with TestClient(app) as c:
        c.post("/login", data={"password": "adminpw", "name": "Ricky", "next": "/"})
        c.post(
            "/admin/shares/import",
            data={"period": "2026-11"},
            files={"file": ("shares.xlsx", (samples / "sample_shares.xlsx").read_bytes())},
            follow_redirects=False,
        )
        resp = c.post(
            "/run/new",
            data={
                "period": "2026-11",
                # 14 days of promotion but only 4 days of stock: 1400 -> 400.
                # Typed on purpose (date_source=manual) so the screenshot keeps
                # showing the scaling; the sample file's own dates are 4 days.
                "promo_start": "2026-11-16",
                "promo_end": "2026-11-29",
                "dispatch_days": "4",
                "date_source": "manual",
            },
            files={
                "target_file": ("t.xlsx", (samples / "sample_target.xlsx").read_bytes()),
                "stock_file": ("s.xlsx", (samples / "sample_stock_sap.xlsx").read_bytes()),
            },
            follow_redirects=False,
        )
        run_id = resp.headers["location"].rstrip("/").split("/")[-1]

        print("rendering:")
        shoot(c, "/", "01_dashboard", 1250)
        shoot(c, "/admin/shares?period=2026-11&scenario=B", "02_shares", 1250)
        shoot(c, "/run/new?period=2026-11", "03_upload", 1700)
        shoot(c, f"/run/{run_id}", "04_result", 1500)
        shoot(c, f"/run/{run_id}/detail?scenario=A", "05_detail_shops", 1300)
        shoot(c, f"/run/{run_id}/detail?scenario=A&sku=111888009033", "06_detail_sku", 1300)
        shoot(c, "/admin/shops", "07_shops", 1200)
        shoot(c, "/runs", "08_runs", 900)

        # A month that exercises the HB87 RP Type rule on its own. It also puts
        # HB87 into the shop master, so the settings page no longer warns.
        hb_run = _hb87_demo(c)
        shoot(c, "/admin/settings", "10_settings", 1500)
        shoot(c, f"/run/{hb_run}", "11_result_hb87", 1400)
        shoot(c, f"/run/{hb_run}/detail?scenario=A&sku=111058302001", "12_detail_hb87", 1500)

        # A merged target cell: two pack sizes on one quantity.
        shared_run = _shared_demo(c)
        shoot(c, f"/run/{shared_run}", "13_result_shared", 1500)
        shoot(
            c,
            f"/run/{shared_run}/detail?scenario=A&sku=111058302002",
            "14_detail_shared",
            1300,
        )

        # 行貨 (Supply source 1／4) next to 倉貨: the PO worklist.
        supply_run = _supply_demo(c)
        shoot(c, f"/run/{supply_run}", "15_result_supply", 1600)
        shoot(
            c,
            f"/run/{supply_run}/detail?scenario=A&sku=111058302001",
            "16_detail_supply",
            1300,
        )

    # login page has no session
    with TestClient(app) as c:
        html = inline(c.get("/login").text)
        file = SITE / "09_login.html"
        file.write_text(html, encoding="utf-8")
        subprocess.run(
            [
                CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--window-size=1440,900", f"--screenshot={SITE / '09_login.png'}",
                file.as_uri(),
            ],
            check=True, capture_output=True,
        )
        print("  09_login.png")

    print(f"\noutput: {SITE}")


if __name__ == "__main__":
    main()
