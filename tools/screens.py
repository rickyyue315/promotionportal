"""Dev tool: render pages to PNG for visual review.

Runs the app in-process with TestClient, walks through the real flow, inlines the
stylesheet, then screenshots each page with headless Chrome.

    python tools/screens.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA = ROOT / "tools" / "_shotdata"
SITE = ROOT / "tools" / "_shots"
for path in (DATA, SITE):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)

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
            data={"period": "2026-11"},
            files={
                "target_file": ("t.xlsx", (samples / "sample_target.xlsx").read_bytes()),
                "stock_file": ("s.xlsx", (samples / "sample_stock_wide.xlsx").read_bytes()),
            },
            follow_redirects=False,
        )
        run_id = int(resp.headers["location"].rstrip("/").split("/")[-1])

        print("rendering:")
        shoot(c, "/", "01_dashboard", 1250)
        shoot(c, "/admin/shares?period=2026-11&scenario=B", "02_shares", 1250)
        shoot(c, "/run/new?period=2026-11", "03_upload", 1150)
        shoot(c, f"/run/{run_id}", "04_result", 1500)
        shoot(c, f"/run/{run_id}/detail?scenario=A", "05_detail_shops", 1300)
        shoot(c, f"/run/{run_id}/detail?scenario=A&sku=109991605010", "06_detail_sku", 1300)
        shoot(c, "/admin/shops", "07_shops", 1200)
        shoot(c, "/runs", "08_runs", 900)

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
