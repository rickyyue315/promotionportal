"""Generate sample workbooks so the portal can be tried without real data."""

from __future__ import annotations

import random
from datetime import datetime
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
    _head(ws, ["Region", "OM", "Site", "店舖名稱", "A", "B", "C"])

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
        ws.append(
            [
                region,
                f"OM {idx % 4 + 1}",
                code,
                f"門市 {code}",
                round(pa[idx], 6),
                round(pb[idx], 6),
                round(pc[idx], 6),
            ]
        )
        for col in (5, 6, 7):
            ws.cell(row=ws.max_row, column=col).number_format = "0.00%"

    for col, width in zip("ABCDEFG", (10, 10, 12, 20, 10, 10, 10)):
        ws.column_dimensions[col].width = width
    path = OUT / "sample_shares.xlsx"
    wb.save(path)
    return path


def targets() -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "SKU Target"
    # No MOQ here on purpose: the minimum order rides on the stock file.
    _head(ws, ["SKU", "貨品名稱", "Target Qty"])
    for sku, name, qty, _moq in SKUS:
        ws.append([sku, name, qty])
    for col, width in zip("ABC", (18, 28, 12)):
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


def stock_sap() -> Path:
    """A SAP-style export: one row per SKU × site, with sales and pipeline.

    Mirrors the real LNU export closely enough to exercise the column
    detection: 'Safety Stock' sits to the left of 'SaSa Net Stock', and
    'Shop ABC indicator' contains the word 'shop', so both are traps.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    _head(
        ws,
        [
            "Region",
            "Article",
            "Shop ABC indicator",
            "Site",
            "SaSa Net Stock",
            "Safety Stock",
            "Pending Received",
            "RP Type",
            "Last Month Sold Qty",
            "MTD Sold Qty",
            "Launch Date",
            "MOQ",
        ],
    )

    random.seed(23)
    # One SKU is deliberately starved so the warehouse cap and the shortage
    # column are visible without needing real data.
    starved = "111888009033"
    for sku, _name, qty, moq in SKUS:
        scale = 0.55 if sku in ("111888009033", "110773004028") else 1.05
        # Warehouse first: its net stock is the dispatch ceiling.
        ws.append(
            [
                "HK",
                sku,
                "",
                "D001",
                40 if sku == starved else int(qty * 0.75),
                int(qty * 0.1),
                int(qty * 0.2),
                "ND",
                0,
                0,
                None,
                moq,
            ]
        )
        launched = sku == "112009010012"  # one deliberate new product
        for shop in SHOPS_HK + SHOPS_MO:
            sold_last = 0 if launched else int(qty / 20 * random.uniform(1.5, 3.0))
            sold_mtd = int(qty / 20 * random.uniform(0.6, 1.4))
            on_hand = 0 if sku == starved else int(
                qty / 20 * scale * random.uniform(0.2, 1.8)
            )
            ws.append(
                [
                    "MO" if shop.startswith("MO") else "HK",
                    sku,
                    "",
                    shop,
                    on_hand,
                    int(qty / 20 * 0.15),
                    int(qty / 20 * random.uniform(0, 0.4)),
                    "ND" if shop == "HB87" else "RF",
                    sold_last,
                    sold_mtd,
                    datetime(2026, 11, 5) if launched else datetime(2025, 3, 1),
                    moq,
                ]
            )

    for col, width in zip(
        "ABCDEFGHIJKL", (8, 16, 18, 10, 16, 14, 18, 10, 20, 16, 14, 8)
    ):
        ws.column_dimensions[col].width = width
    path = OUT / "sample_stock_sap.xlsx"
    wb.save(path)
    return path


if __name__ == "__main__":
    for path in (shares(), targets(), stock_wide(), stock_long(), stock_sap()):
        print("wrote", path.name)
