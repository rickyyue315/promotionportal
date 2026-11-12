"""Excel import and export.

Real-world spreadsheets from retail ops are never tidy: headers drift, numbers
arrive as text with thousand separators, shop stock often comes as a wide pivot
(SKU rows × shop columns) rather than a long list. This module does the
guessing, and — more importantly — refuses to guess silently. Every assumption
it makes is reported back as a warning so the buyer can see what was interpreted.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill

SHOP_RE = re.compile(r"^[A-Z]{1,4}\d{1,4}[A-Z0-9]{0,2}$")

SKU_KEYS = ("sku", "article", "item", "product code", "貨號", "產品編號", "貨品編號", "編號")
TARGET_KEYS = ("target qty", "target", "qty", "quantity", "數量", "目標", "目標數量")
MOQ_KEYS = ("moq", "minimum order", "起訂", "最小訂購")
DESC_KEYS = ("description", "desc", "name", "名稱", "描述", "貨品名稱")
SHOP_KEYS = ("shop", "site", "store", "outlet", "店舖", "店鋪", "分店", "門市")
# Ordered most-specific first. A SAP export carries Safety Stock, Stock in
# Transit and SaSa Net Stock side by side; "stock" alone must not win.
STOCK_KEYS = (
    "sasa net stock",
    "net stock",
    "on hand",
    "unrestricted",
    "stock",
    "qty",
    "quantity",
    "存貨",
    "庫存",
    "現有",
    "數量",
)
STOCK_REJECT = (
    "safety",
    "transit",
    "sold",
    "blocked",
    "open dn",
    "pending",
    "consignment",
    "quality",
)
RP_KEYS = ("rp type", "rp_type", "rp code", "rp")
PENDING_KEYS = ("pending received", "pending", "在途")
LAST_MONTH_KEYS = ("last month sold qty", "last month sold", "上月銷量", "上月銷售")
MTD_KEYS = ("mtd sold qty", "mtd sold", "本月銷量", "本月銷售")
LAUNCH_KEYS = ("launch date", "上市日期", "推出日期")
SCENARIO_KEYS = ("scenario", "plan", "set", "套餐", "方案", "餐")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _norm(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower().replace("\u3000", " ")


def _header_row(rows: Sequence[Sequence[Any]]) -> int:
    """Pick the row that looks most like a header (most non-empty text cells)."""
    best, best_score = 0, -1
    for i, row in enumerate(rows[:15]):
        score = sum(1 for c in row if isinstance(c, str) and c.strip())
        if score > best_score:
            best, best_score = i, score
    return best


def _find(
    headers: Sequence[str],
    keys: Sequence[str],
    exclude: Iterable[str] = (),
    reject: Sequence[str] = (),
) -> Optional[int]:
    """Pick the column that best matches ``keys``.

    Keys are tried in order of specificity rather than left to right by column
    position: an exact header match always wins, then the earliest key in
    ``keys``. Without that, a SAP export matches "Safety Stock" before "SaSa Net
    Stock" simply because it sits further left.
    """
    excl = {e for e in exclude}
    bad = tuple(reject)

    def usable(i: int, head: str) -> bool:
        return i not in excl and not any(word in head for word in bad)

    for key in keys:
        for i, head in enumerate(headers):
            if usable(i, head) and head == key:
                return i
    for key in keys:
        for i, head in enumerate(headers):
            if usable(i, head) and key in head:
                return i
    return None


def to_number(value: Any) -> Optional[float]:
    """Parse '1,234.5', '1 234', '(20)', '1.2k' and plain numbers."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").replace(" ", "").replace("%", "")
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def to_date(value: Any) -> Optional[date]:
    """Parse Excel date cells and the usual text spellings."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def looks_like_shop(value: Any, known: set) -> bool:
    text = str(value or "").strip().upper()
    if not text:
        return False
    return text in known or bool(SHOP_RE.match(text))


@dataclass(frozen=True)
class StockRow:
    """One SKU × shop line from the stock file.

    Only ``qty`` (SaSa Net Stock) and ``pending`` (Pending Received) are
    quantities we act on. The sales figures are rates fuel for the 4-day cover
    calculation, not stock.
    """

    sku: str
    shop: str
    qty: int
    rp_type: str = ""
    pending: int = 0
    last_month_sold: int = 0
    mtd_sold: int = 0
    launch_date: Optional[date] = None


@dataclass
class Parsed:
    rows: List[Any] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    sheet: str = ""
    mapped: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.rows)


# ---------------------------------------------------------------------------
# target file: SKU × Target Qty
# ---------------------------------------------------------------------------


def parse_target_file(
    data: bytes, known_shops: Optional[set] = None
) -> Parsed:
    out = Parsed()
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        out.errors.append(f"無法開啟 Excel：{exc}")
        return out

    ws = wb[wb.sheetnames[0]]
    raw = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    if not raw:
        out.errors.append("工作表是空的")
        return out

    out.sheet = ws.title
    h = _header_row(raw)
    headers = [_norm(c) for c in raw[h]]
    body = raw[h + 1 :]

    i_sku = _find(headers, SKU_KEYS)
    i_target = _find(headers, TARGET_KEYS, exclude={i_sku} if i_sku is not None else ())
    i_moq = _find(headers, MOQ_KEYS)
    i_desc = _find(headers, DESC_KEYS, exclude={i_sku} if i_sku is not None else ())

    if i_sku is None:
        out.errors.append(
            "找不到 SKU 欄位。請確保第一列是標題，並有 'SKU'、'Article' 或 '貨號' 欄。"
        )
        return out
    if i_target is None:
        out.errors.append(
            "找不到 Target 數量欄位。請確保有 'Target Qty'、'數量' 或 '目標' 欄。"
        )
        return out

    out.mapped = {
        "sku": raw[h][i_sku],
        "target": raw[h][i_target],
        "moq": raw[h][i_moq] if i_moq is not None else "",
        "description": raw[h][i_desc] if i_desc is not None else "",
    }

    seen: Dict[str, int] = {}
    duplicates: List[str] = []
    bad_rows: List[str] = []
    fractional: List[str] = []

    for offset, row in enumerate(body, start=h + 2):
        if not any(c not in (None, "") for c in row):
            continue
        sku = str(row[i_sku]).strip() if i_sku < len(row) and row[i_sku] is not None else ""
        if not sku:
            continue
        # Excel renders long numeric article codes as 1.10331e+11 or 110331002001.0
        if re.fullmatch(r"\d+\.0", sku):
            sku = sku[:-2]

        qty = to_number(row[i_target]) if i_target < len(row) else None
        if qty is None:
            bad_rows.append(f"第 {offset} 行（{sku}）：Target 不是數字")
            continue
        if qty < 0:
            bad_rows.append(f"第 {offset} 行（{sku}）：Target 是負數 {qty:g}，已略過")
            continue
        if qty != int(qty):
            fractional.append(sku)
        qty_i = int(round(qty))

        moq = 1
        if i_moq is not None and i_moq < len(row):
            m = to_number(row[i_moq])
            if m and m > 1:
                moq = int(round(m))
        desc = ""
        if i_desc is not None and i_desc < len(row) and row[i_desc] is not None:
            desc = str(row[i_desc]).strip()

        if sku in seen:
            duplicates.append(sku)
            continue
        seen[sku] = 1
        out.rows.append((sku, desc, qty_i, moq))

    if duplicates:
        out.errors.append(
            "SKU 重複出現，無法判斷哪一個 Target 才對："
            + "、".join(sorted(set(duplicates))[:10])
            + ("…" if len(set(duplicates)) > 10 else "")
        )
    if bad_rows:
        out.warnings.extend(bad_rows[:10])
        if len(bad_rows) > 10:
            out.warnings.append(f"另有 {len(bad_rows) - 10} 行格式有問題，已略過")
    if fractional:
        out.warnings.append(
            "以下 SKU 的 Target 有小數，已四捨五入為整數："
            + "、".join(fractional[:8])
        )
    if not out.rows and not out.errors:
        out.errors.append("找不到任何有效的 SKU 資料列")

    return out


# ---------------------------------------------------------------------------
# stock file: SKU × shop
# ---------------------------------------------------------------------------


def parse_stock_file(data: bytes, known_shops: Optional[set] = None) -> Parsed:
    out = Parsed()
    known = {s.upper() for s in (known_shops or set())}
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        out.errors.append(f"無法開啟 Excel：{exc}")
        return out

    ws = wb[wb.sheetnames[0]]
    raw = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    if not raw:
        out.errors.append("工作表是空的")
        return out

    out.sheet = ws.title
    h = _header_row(raw)
    headers = [_norm(c) for c in raw[h]]
    original = [str(c).strip() if c is not None else "" for c in raw[h]]
    body = raw[h + 1 :]

    i_sku = _find(headers, SKU_KEYS)
    if i_sku is None:
        out.errors.append("找不到 SKU 欄位（SKU／Article／貨號）。")
        return out

    i_shop = _find(headers, SHOP_KEYS, exclude={i_sku}, reject=("indicator",))
    i_stock = _find(
        headers,
        STOCK_KEYS,
        exclude={i_sku, i_shop} if i_shop is not None else {i_sku},
        reject=STOCK_REJECT,
    )
    i_rp = _find(headers, RP_KEYS, exclude={i_sku, i_shop, i_stock})
    i_pending = _find(headers, PENDING_KEYS, exclude={i_sku, i_shop, i_stock, i_rp})
    i_last = _find(headers, LAST_MONTH_KEYS, exclude={i_sku, i_shop, i_stock, i_rp})
    i_mtd = _find(headers, MTD_KEYS, exclude={i_sku, i_shop, i_stock, i_rp})
    i_launch = _find(headers, LAUNCH_KEYS, exclude={i_sku, i_shop, i_stock, i_rp})

    # Is it a wide pivot (one column per shop)?
    shop_cols = [
        i
        for i in range(len(headers))
        if i != i_sku and looks_like_shop(original[i], known)
    ]
    wide = i_shop is None and len(shop_cols) >= 2

    if wide:
        out.mapped = {"sku": original[i_sku], "shop_columns": len(shop_cols)}
        shop_names = {i: original[i].upper() for i in shop_cols}
        out.warnings.append(
            f"偵測為寬表格式：{len(shop_cols)} 個店舖欄位 "
            f"（{original[shop_cols[0]]} … {original[shop_cols[-1]]}）"
        )
        pairs: Dict[Tuple[str, str], int] = {}
        dup = set()
        for row in body:
            if not any(c not in (None, "") for c in row):
                continue
            if i_sku >= len(row) or row[i_sku] is None:
                continue
            sku = str(row[i_sku]).strip()
            if re.fullmatch(r"\d+\.0", sku):
                sku = sku[:-2]
            if not sku:
                continue
            for i in shop_cols:
                qty = to_number(row[i]) if i < len(row) else None
                if not qty:
                    continue
                key = (sku, shop_names[i])
                if key in pairs:
                    dup.add(f"{sku}@{shop_names[i]}")
                pairs[key] = pairs.get(key, 0) + int(round(qty))
        if dup:
            out.warnings.append(
                "同一 SKU 同一店舖出現多於一次，已相加：" + "、".join(sorted(dup)[:5])
            )
        # A pivot has no RP Type or sales columns; the gate treats RP as not
        # provided and the 4-day cover simply cannot be calculated.
        out.rows = [StockRow(sku, shop, qty) for (sku, shop), qty in pairs.items()]
    elif i_shop is not None and i_stock is not None:
        out.mapped = {
            "sku": original[i_sku],
            "shop": original[i_shop],
            "stock": original[i_stock],
            "rp_type": original[i_rp] if i_rp is not None else "",
            "pending": original[i_pending] if i_pending is not None else "",
            "last_month_sold": original[i_last] if i_last is not None else "",
            "mtd_sold": original[i_mtd] if i_mtd is not None else "",
            "launch_date": original[i_launch] if i_launch is not None else "",
        }
        out.warnings.append("偵測為長表格式（每行一個 SKU × 店舖）")
        pairs2: Dict[Tuple[str, str], int] = {}
        rp_map: Dict[Tuple[str, str], str] = {}
        rp_conflict = set()
        pending_map: Dict[Tuple[str, str], int] = {}
        sales_map: Dict[Tuple[str, str], Tuple[int, int]] = {}
        launch_map: Dict[Tuple[str, str], date] = {}
        dup2 = set()
        bad = 0
        for offset, row in enumerate(body, start=h + 2):
            if not any(c not in (None, "") for c in row):
                continue
            sku = str(row[i_sku]).strip() if i_sku < len(row) and row[i_sku] is not None else ""
            if re.fullmatch(r"\d+\.0", sku or ""):
                sku = sku[:-2]
            shop = str(row[i_shop]).strip().upper() if i_shop < len(row) and row[i_shop] is not None else ""
            if not sku or not shop:
                continue
            qty = to_number(row[i_stock]) if i_stock < len(row) else None
            if qty is None:
                bad += 1
                continue
            key = (sku, shop)
            if key in pairs2:
                dup2.add(f"{sku}@{shop}")
            pairs2[key] = pairs2.get(key, 0) + int(round(qty))

            pending = to_number(row[i_pending]) if i_pending is not None and i_pending < len(row) else None
            if pending:
                pending_map[key] = pending_map.get(key, 0) + int(round(pending))

            last_month = to_number(row[i_last]) if i_last is not None and i_last < len(row) else None
            mtd = to_number(row[i_mtd]) if i_mtd is not None and i_mtd < len(row) else None
            if last_month is not None or mtd is not None:
                pl, pm = sales_map.get(key, (0, 0))
                # Sales are repeated on every row of the same SKU × shop, so
                # take the largest rather than adding them up.
                sales_map[key] = (
                    max(pl, int(round(last_month or 0))),
                    max(pm, int(round(mtd or 0))),
                )

            launch = to_date(row[i_launch]) if i_launch is not None and i_launch < len(row) else None
            if launch and (key not in launch_map or launch < launch_map[key]):
                launch_map[key] = launch

            if i_rp is not None and i_rp < len(row):
                rp = str(row[i_rp] or "").strip().upper()
                if rp:
                    prev = rp_map.get(key, "")
                    if not prev:
                        rp_map[key] = rp
                    elif prev != rp:
                        # Conflicting rows for the same SKU × shop: refuse to
                        # guess, leave it blank so the gate blocks dispatch.
                        rp_conflict.add(f"{sku}@{shop}")
                        rp_map[key] = ""

        if dup2:
            out.warnings.append(
                "同一 SKU 同一店舖出現多於一次，已相加：" + "、".join(sorted(dup2)[:5])
            )
        if rp_conflict:
            out.warnings.append(
                "以下 SKU × 店舖有多個不同的 RP Type，已當作未提供："
                + "、".join(sorted(rp_conflict)[:5])
            )
        if bad:
            out.warnings.append(f"有 {bad} 行的庫存不是數字，已略過")
        out.rows = [
            StockRow(
                sku=sku,
                shop=shop,
                qty=qty,
                rp_type=rp_map.get((sku, shop), ""),
                pending=pending_map.get((sku, shop), 0),
                last_month_sold=sales_map.get((sku, shop), (0, 0))[0],
                mtd_sold=sales_map.get((sku, shop), (0, 0))[1],
                launch_date=launch_map.get((sku, shop)),
            )
            for (sku, shop), qty in pairs2.items()
        ]
    else:
        out.errors.append(
            "無法辨識庫存格式。需要其中一種：\n"
            "・長表：SKU / 店舖 / 庫存 三欄\n"
            "・寬表：第一欄 SKU，之後每欄一個店舖代碼"
        )
        return out

    if not out.rows:
        out.errors.append("找不到任何有效的庫存資料")
    return out


# ---------------------------------------------------------------------------
# share file: Shop × A/B/C
# ---------------------------------------------------------------------------


@dataclass
class ParsedShares:
    by_scenario: Dict[str, Dict[str, float]] = field(default_factory=dict)
    names: Dict[str, str] = field(default_factory=dict)
    regions: Dict[str, str] = field(default_factory=dict)
    oms: Dict[str, str] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    mapped: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.by_scenario)


REGION_KEYS = ("region", "地區", "區域", "country")
OM_KEYS = ("om", "operations manager", "區域經理")


def parse_share_file(data: bytes) -> ParsedShares:
    out = ParsedShares()
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        out.errors.append(f"無法開啟 Excel：{exc}")
        return out

    ws = wb[wb.sheetnames[0]]
    raw = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    if not raw:
        out.errors.append("工作表是空的")
        return out

    h = _header_row(raw)
    headers = [_norm(c) for c in raw[h]]
    original = [str(c).strip() if c is not None else "" for c in raw[h]]
    body = raw[h + 1 :]

    i_shop = _find(headers, SHOP_KEYS)
    if i_shop is None:
        out.errors.append("找不到店舖欄位（Shop／Site／店舖）。")
        return out

    scenario_cols: Dict[str, int] = {}
    for name in ("A", "B", "C"):
        for i, head in enumerate(headers):
            if i == i_shop:
                continue
            if re.search(rf"(^|[^a-z]){name.lower()}([^a-z]|$)", head):
                scenario_cols[name] = i
                break

    if not scenario_cols:
        out.errors.append(
            "找不到 A / B / C 佔比欄位。欄名請包含 A、B、C，例如 'A餐'、'Plan A'、'A%'。"
        )
        return out

    out.mapped = {
        "shop": original[i_shop],
        **{k: original[v] for k, v in scenario_cols.items()},
    }

    by_scenario: Dict[str, Dict[str, float]] = {k: {} for k in scenario_cols}
    i_name = _find(headers, DESC_KEYS, exclude={i_shop})
    i_region = _find(headers, REGION_KEYS, exclude={i_shop})
    i_om = _find(headers, OM_KEYS, exclude={i_shop, i_name, i_region})
    out.mapped.update(
        {
            "name": original[i_name] if i_name is not None else "",
            "region": original[i_region] if i_region is not None else "",
            "om": original[i_om] if i_om is not None else "",
        }
    )

    for offset, row in enumerate(body, start=h + 2):
        if not any(c not in (None, "") for c in row):
            continue
        shop = (
            str(row[i_shop]).strip().upper()
            if i_shop < len(row) and row[i_shop] is not None
            else ""
        )
        if not shop:
            continue
        if shop not in out.order:
            out.order.append(shop)
        if i_name is not None and i_name < len(row) and row[i_name] is not None:
            out.names[shop] = str(row[i_name]).strip()
        if i_region is not None and i_region < len(row) and row[i_region] is not None:
            region = str(row[i_region]).strip().upper()[:2]
            if region:
                out.regions[shop] = region
        if i_om is not None and i_om < len(row) and row[i_om] is not None:
            om = str(row[i_om]).strip()
            if om:
                out.oms[shop] = om

        for scenario, col in scenario_cols.items():
            raw_val = row[col] if col < len(row) else None
            pct = to_number(raw_val)
            if pct is None:
                continue
            # Accept both 0.125 and 12.5 (meaning 12.5%)
            if abs(pct) > 1.5:
                pct = pct / 100.0
            if pct < 0:
                out.warnings.append(f"第 {offset} 行（{shop} {scenario}）：負數已設為 0")
                pct = 0.0
            by_scenario[scenario][shop] = pct

    out.by_scenario = by_scenario
    for scenario, pct in by_scenario.items():
        total = sum(pct.values())
        if not pct:
            out.errors.append(f"{scenario} 餐沒有任何佔比資料")
        elif abs(total - 1.0) > 0.02:
            out.warnings.append(
                f"{scenario} 餐佔比總和為 {total:.2%}，與 100% 相差較大，請確認是否正確"
            )

    return out


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

_HEAD_FILL = PatternFill("solid", fgColor="EDF1F5")
_HEAD_FONT = Font(bold=True)
_MONEY = "#,##0"


def _put_header(ws, headers: Sequence[str]) -> None:
    cells = []
    for text in headers:
        cell = WriteOnlyCell(ws, value=text)
        cell.font = _HEAD_FONT
        cell.fill = _HEAD_FILL
        cells.append(cell)
    ws.append(cells)


def export_run(
    run: dict,
    results: Dict[str, Any],
    compare_rows: List[dict],
    scenarios: Sequence[str] = ("A", "B", "C"),
    labels: Optional[Dict[str, Dict[str, str]]] = None,
    gate_shops: Sequence[str] = (),
    params: Optional[dict] = None,
) -> bytes:
    labels = labels or {}
    gate = {str(s).strip().upper() for s in gate_shops if str(s).strip()}
    params = params or {}
    wb = Workbook(write_only=True)
    wb.properties.title = f"推廣派貨計算 {run.get('period', '')}"

    # --- comparison sheet ---
    ws = wb.create_sheet("比較")
    headers = [
        "SKU", "貨品名稱", "Target", "派貨目標", "店舖庫存", "在途",
        "倉存", "倉存在途", "日銷", "預計銷量", "目標差距", "貨源缺口",
        "RP 不派", "夠貨",
    ]
    for sc in scenarios:
        headers += [f"{sc} 需求", f"{sc} 派貨", f"{sc} 缺貨"]
    _put_header(ws, headers)

    for row in compare_rows:
        first = next(iter(row["scenarios"].values()), None)
        line: List[Any] = [
            row["sku"],
            row["description"],
            row["target_qty"],
            first.dispatch_target if first else 0,
            first.total_stock if first else row["total_stock"],
            first.total_pending if first else 0,
            first.warehouse_qty if first else 0,
            first.warehouse_pending if first else 0,
            round(first.daily_rate, 2) if first else 0,
            first.expected_sales if first else 0,
            first.gap_target if first else 0,
            row["shortfall_qty"],
            first.blocked_qty if first else 0,
            "是" if first and first.enough else "否",
        ]
        for sc in scenarios:
            s = row["scenarios"].get(sc)
            line += [
                s.need_qty if s else 0,
                s.total_dispatch if s else 0,
                s.short_qty if s else 0,
            ]
        ws.append(line)

    # --- detail sheet per scenario ---
    desc_of = {r["sku"]: r.get("description", "") for r in compare_rows}
    for sc in scenarios:
        res = results.get(sc)
        if not res:
            continue
        ws2 = wb.create_sheet(f"明細_{sc}")
        _put_header(
            ws2,
            [
                "SKU", "貨品名稱", "店舖", "佔比", "分配數量", "現有庫存",
                "在途", "可用", "差距", "需求", "可派", "缺貨",
                "日銷", "預計銷量", "可調出", "RP Type", "不派原因",
            ],
        )
        for ln in res.lines:
            reason = ""
            if ln.blocked:
                reason = (
                    f"RP={ln.rp_type}" if ln.rp_type else "未提供 RP Type"
                ) + f"（需求 {ln.need_qty}）"
            ws2.append(
                [
                    ln.sku,
                    desc_of.get(ln.sku, ""),
                    ln.shop_code,
                    round(ln.share_pct, 6),
                    ln.alloc_qty,
                    ln.stock_qty,
                    ln.pending_qty,
                    ln.on_hand,
                    ln.gap_qty,
                    ln.need_qty,
                    ln.dispatch_qty,
                    ln.short_qty,
                    round(ln.daily_rate, 2),
                    ln.expected_sales,
                    ln.surplus_qty,
                    ln.rp_type,
                    reason,
                ]
            )

    # --- parameters ---
    ws3 = wb.create_sheet("參數")
    ws3.append(["項目", "值"])
    ws3.append(["期間", run.get("period", "")])
    ws3.append(["計算時間", run.get("created_at", "")])
    ws3.append(["執行者", run.get("created_by", "")])
    ws3.append(["SKU 數", run.get("n_sku", "")])
    ws3.append(["不夠貨 SKU 數", run.get("n_short", "")])
    if params.get("promo_start"):
        ws3.append(["推廣開始", params.get("promo_start", "")])
        ws3.append(["推廣完結", params.get("promo_end", "")])
        ws3.append(["推廣日數", params.get("promo_days", "")])
        ws3.append(["派貨日數", params.get("dispatch_days", "")])
    if params.get("as_of"):
        ws3.append(["銷售數據截止", params.get("as_of", "")])
    if params.get("warehouse_sites"):
        ws3.append(["貨倉", "、".join(params["warehouse_sites"])])
        ws3.append(["貨倉口徑", "只計 SaSa Net Stock；Pending Received 為入倉量，不計入"])
    if params.get("has_sales") is False:
        ws3.append(["銷售數據", "庫存檔沒有銷售欄位，未計算預計銷量與差距"])
    for sc in scenarios:
        name = (labels.get(sc) or {}).get("label") or ""
        if name:
            ws3.append([f"{sc} 餐名稱", name])
    if gate:
        ws3.append(["RP Type 管制店舖", "、".join(sorted(gate))])
        ws3.append(["RP Type 規則", "只有 RP Type = RF 才派貨；ND 或未提供一律 0 件"])
    for sc in scenarios:
        res = results.get(sc)
        if res:
            ws3.append([f"{sc} 佔比總和", f"{res.share_total:.4%}"])
            for w in res.warnings:
                ws3.append([f"{sc} 提示", w])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def export_share_template(
    shops: Sequence[dict], existing: Dict[str, Dict[str, float]], period: str
) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "店舖佔比"
    ws.append(["Region", "OM", "Site", "店舖名稱", "A", "B", "C"])
    for cell in ws[1]:
        cell.font = _HEAD_FONT
        cell.fill = _HEAD_FILL

    for shop in shops:
        code = shop["code"]
        ws.append(
            [
                shop.get("region", "HK"),
                shop.get("om", ""),
                code,
                shop.get("name", ""),
                existing.get("A", {}).get(code, 0),
                existing.get("B", {}).get(code, 0),
                existing.get("C", {}).get(code, 0),
            ]
        )
        for col in (5, 6, 7):
            ws.cell(row=ws.max_row, column=col).number_format = "0.00%"

    for col, width in zip("ABCDEFG", (10, 18, 12, 28, 10, 10, 10)):
        ws.column_dimensions[col].width = width

    note = wb.create_sheet("說明")
    note.append([f"{period} 店舖生意佔比設定"])
    note.append([])
    note.append(["Region／OM／店舖名稱 只作參考，匯入時 Region 與 OM 會更新到店舖主檔。"])
    note.append(["Site 是店舖代碼，不可留空；不存在的代碼會自動建立店舖。"])
    note.append(["A / B / C 三欄相加各自要等於 100%。"])
    note.append(["可以填 0.125 或 12.5，系統都會當作 12.5%。"])
    note.append(["留空的店舖視為 0%。"])
    note.append(["上載後系統會即時顯示每餐的總和，未達 100% 會警告。"])
    return _save(wb)


def export_target_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "SKU Target"
    ws.append(["SKU", "貨品名稱", "Target Qty", "MOQ"])
    for cell in ws[1]:
        cell.font = _HEAD_FONT
        cell.fill = _HEAD_FILL
    ws.append(["109991605010", "示範貨品 A", 1200, 12])
    ws.append(["110331002001", "示範貨品 B", 800, 1])
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 10

    note = wb.create_sheet("說明")
    note.append(["SKU", "必填，不可重複"])
    note.append(["Target Qty", "必填，整數"])
    note.append(["MOQ", "選填，派貨量會向上取至 MOQ 的倍數；留空等於 1"])
    return _save(wb)


def export_stock_template(shops: Sequence[str]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    ws.append(["SKU"] + list(shops))
    for cell in ws[1]:
        cell.font = _HEAD_FONT
        cell.fill = _HEAD_FILL
    ws.append(["109991605010"] + [0] * len(shops))
    ws.column_dimensions["A"].width = 18
    for idx in range(2, len(shops) + 2):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = 9

    note = wb.create_sheet("說明")
    note.append(["寬表：第一欄 SKU，之後每欄一個店舖代碼"])
    note.append(["或改成三欄長表：SKU / 店舖 / 庫存，系統會自動辨識"])
    note.append(["長表可加一欄 RP Type（例如 ND / RF）；HB87 只有 RF 才派貨，其餘店舖不看這欄"])
    note.append(["SAP 匯出的明細（Article / Site / SaSa Net Stock / RP Type）可直接上載"])
    return _save(wb)


def _save(wb: Workbook) -> bytes:
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
