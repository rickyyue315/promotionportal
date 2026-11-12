"""Generate sample workbooks so the portal can be tried without real data."""

from __future__ import annotations

import random
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

OUT = Path(__file__).resolve().parent

SHOPS_HK = ["HA19", "HA21", "HB38", "HB66", "HC12", "HD16", "HBA3", "HK01", "HK02", "HK03",
            "HK04", "HK05", "HK06", "HK07", "HK08", "HK09"]
SHOPS_MO = ["MO01", "MO02", "MO03", "MO04"]

SKUS = [
    ("109991605010", "柔潤潔面乳 120ml", 2400, 12),
    ("110331002001", "保濕精華 30ml", 1800, 6),
    ("110552003014", "防曬乳液 SPF50 50ml", 960, 12),
    ("110773004028", "眼霜 15ml", 640, 6),
    ("110994005003", "面膜 5片裝", 3200, 24),
    ("111225006017", "卸妝水 400ml", 1200, 12),
    ("111446007021", "唇膏 #04", 720, 12),
    ("111667008009", "粉底液 #02", 540, 6),
    ("111888009033", "香水 50ml", 360, 3),
    ("112009010012", "身體乳 250ml", 1500, 12),
]

HEAD = Font(bold=True)
FILL = PatternFill("solid", fgColor="EDF1F5")


def _head(ws, headers):
    ws.append(headers)
    for cell in ws[1]:
        cell.font = HEAD
        cell.fill = FILL


def shares() -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "店舖佔比"
    _head(ws, ["Shop", "店舖名稱", "Region", "A", "B", "C"])

    random.seed(7)
    all_shops = [(c, "HK") for c in SHOPS_HK] + [(c, "MO") for c in SHOPS_MO]

    # A: concentrated in big shops. B: flatter. C: skewed to a few flagships.
    weights_a = [random.uniform(1.6, 2.4) for _ in SHOPS_HK] + [random.uniform(0.8, 1.2) for _ in SHOPS_MO]
    weights_b = [random.uniform(0.9, 1.1) for _ in all_shops]
    weights_c = [random.uniform(0.5, 1.0) for _ in SHOPS_HK] + [random.uniform(0.5, 1.0) for _ in SHOPS_MO]
    for i in (0, 2, 4):
        weights_c[i] *= 2.6

    def pct(weights):
        total = sum(weights)
        return [w / total for w in weights]

    pa, pb, pc = pct(weights_a), pct(weights_b), pct(weights_c)

    for idx, (code, region) in enumerate(all_shops):
        ws.append([code, f"門市 {code}", region, round(pa[idx], 6), round(pb[idx], 6), round(pc[idx], 6)])
        for col in (4, 5, 6):
            ws.cell(row=ws.max_row, column=col).number_format = "0.00%"

    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 20
    path = OUT / "sample_shares.xlsx"
    wb.save(path)
    return path


def targets() -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "SKU Target"
    _head(ws, ["SKU", "貨品名稱", "Target Qty", "MOQ"])
    for sku, name, qty, moq in SKUS:
        ws.append([sku, name, qty, moq])
    for col, width in zip("ABCD", (18, 28, 12, 8)):
        ws.column_dimensions[col].width = width
    path = OUT / "sample_target.xlsx"
    wb.save(path)
    return path


def stock_wide() -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    shops = SHOPS_HK + SHOPS_MO
    _head(ws, ["SKU"] + shops)

    random.seed(11)
    for sku, _name, qty, _moq in SKUS:
        # Deliberately leave two SKUs short overall so the "不夠貨" path is visible.
        scale = 0.55 if sku in ("111888009033", "110773004028") else 1.05
        row = [sku]
        for _ in shops:
            row.append(int(qty / len(shops) * scale * random.uniform(0.2, 1.8)))
        ws.append(row)

    ws.column_dimensions["A"].width = 18
    path = OUT / "sample_stock_wide.xlsx"
    wb.save(path)
    return path


def stock_long() -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    _head(ws, ["SKU", "店舖", "庫存"])

    random.seed(11)
    for sku, _name, qty, _moq in SKUS:
        scale = 0.55 if sku in ("111888009033", "110773004028") else 1.05
        for shop in SHOPS_HK + SHOPS_MO:
            ws.append([sku, shop, int(qty / 20 * scale * random.uniform(0.2, 1.8))])
    ws.column_dimensions["A"].width = 18
    path = OUT / "sample_stock_long.xlsx"
    wb.save(path)
    return path


if __name__ == "__main__":
    for path in (shares(), targets(), stock_wide(), stock_long()):
        print("wrote", path.name)
