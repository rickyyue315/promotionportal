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
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .calc import supply_code

SHOP_RE = re.compile(r"^[A-Z]{1,4}\d{1,4}[A-Z0-9]{0,2}$")

SKU_KEYS = ("sku", "article", "item", "product code", "貨號", "產品編號", "貨品編號", "編號")
TARGET_KEYS = ("target qty", "target", "qty", "quantity", "數量", "目標", "目標數量")
MOQ_KEYS = ("moq", "minimum order", "起訂", "最小訂購")
# The promo window is usually repeated on every line of the target sheet, so
# the dates can be read instead of re-typed on the upload form.
PROMO_START_KEYS = (
    "start date (promo)",
    "promo start date",
    "promo start",
    "start date",
    "promotion start",
    "推廣開始日期",
    "推廣開始",
    "開始日期",
)
PROMO_END_KEYS = (
    "end date (promo)",
    "promo end date",
    "promo end",
    "end date",
    "promotion end",
    "推廣完結日期",
    "推廣結束日期",
    "推廣完結",
    "完結日期",
    "結束日期",
)
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
SUPPLY_KEYS = ("supply source", "supply_source", "supplysource", "source of supply", "貨源", "供應來源")
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
    calculation, not stock. ``moq`` rides along because the minimum order is a
    packaging property of the SKU, which SAP keeps on this row rather than on
    the target list. ``supply_source`` says who supplies the shop — 1／4 行貨
    (buyer's PO) or 2 倉貨 (STO from D001) — so the row can be routed correctly.
    """

    sku: str
    shop: str
    qty: int
    rp_type: str = ""
    pending: int = 0
    last_month_sold: int = 0
    mtd_sold: int = 0
    launch_date: Optional[date] = None
    moq: int = 1
    supply_source: str = ""


@dataclass(frozen=True)
class TargetRow:
    """One SKU line of the target file.

    ``qty`` is 0 for the members of a shared target: only the row that carries
    the number in the spreadsheet has it, and ``group`` names that row's SKU.
    Standalone SKUs leave ``group`` empty.
    """

    sku: str
    description: str
    qty: int
    group: str = ""


@dataclass
class Parsed:
    rows: List[Any] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    sheet: str = ""
    mapped: Dict[str, str] = field(default_factory=dict)
    # Only the target file carries these: the promo window printed on its
    # lines. Empty means the upload form's dates are the only source.
    promo_start: Optional[date] = None
    promo_end: Optional[date] = None

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.rows)


def _promo_window(
    rows: Sequence[Sequence[Any]], i_start: Optional[int], i_end: Optional[int]
) -> Tuple[Optional[date], Optional[date], List[Tuple[date, date]]]:
    """The promo dates the sheet repeats, plus any rounds it also mentions.

    Counting rather than reading the first non-empty cell handles both shapes
    seen in practice: a date typed on every line, and a merged block where only
    the top cell holds the value. A file that spans two promo rounds keeps the
    round with the most lines and reports the others instead of picking
    silently.
    """
    starts: Dict[date, int] = {}
    ends: Dict[date, int] = {}
    pairs: Dict[Tuple[date, date], int] = {}

    for row in rows:
        s = to_date(row[i_start]) if i_start is not None and i_start < len(row) else None
        e = to_date(row[i_end]) if i_end is not None and i_end < len(row) else None
        if s:
            starts[s] = starts.get(s, 0) + 1
        if e:
            ends[e] = ends.get(e, 0) + 1
        if s and e and e >= s:
            pairs[(s, e)] = pairs.get((s, e), 0) + 1

    def top(counter: Dict[date, int]) -> Optional[date]:
        if not counter:
            return None
        return min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    if pairs:
        best = min(pairs.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))[0]
        others = [p for p in pairs if p != best]
        return best[0], best[1], others
    return top(starts), top(ends), []



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
    i_desc = _find(headers, DESC_KEYS, exclude={i_sku} if i_sku is not None else ())
    # MOQ lives on the stock side (SKU × shop), not here. A leftover MOQ column
    # in an old template is simply not read: it is not a quantity to act on and
    # the run has nothing useful to say about it, so it stays out of the way.

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
        "description": raw[h][i_desc] if i_desc is not None else "",
    }

    # ---- promo window printed inside the sheet -----------------------------
    taken = {i for i in (i_sku, i_target, i_desc) if i is not None}
    i_pstart = _find(headers, PROMO_START_KEYS, exclude=taken)
    i_pend = _find(
        headers,
        PROMO_END_KEYS,
        exclude=taken | ({i_pstart} if i_pstart is not None else set()),
    )
    if i_pstart is not None or i_pend is not None:
        out.mapped["promo_start"] = (
            str(raw[h][i_pstart]) if i_pstart is not None else ""
        )
        out.mapped["promo_end"] = str(raw[h][i_pend]) if i_pend is not None else ""
        d_start, d_end, other_rounds = _promo_window(body, i_pstart, i_pend)
        if i_pstart is not None and i_pend is None:
            out.warnings.append(
                f"Target 檔只有「{raw[h][i_pstart]}」而沒有推廣完結日期欄，"
                "推廣期改用畫面輸入的日期。"
            )
        elif i_pend is not None and i_pstart is None:
            out.warnings.append(
                f"Target 檔只有「{raw[h][i_pend]}」而沒有推廣開始日期欄，"
                "推廣期改用畫面輸入的日期。"
            )
        elif d_start and d_end:
            out.promo_start, out.promo_end = d_start, d_end
            if other_rounds:
                spread = "、".join(
                    f"{s:%d-%b-%y}→{e:%d-%b-%y}" for s, e in sorted(other_rounds)[:4]
                )
                out.warnings.append(
                    f"Target 檔有不止一組推廣日期（{spread}），"
                    f"已採用行數最多的一組：{d_start:%d-%b-%y} → {d_end:%d-%b-%y}。"
                )
        else:
            out.warnings.append(
                f"Target 檔的「{raw[h][i_pstart]}」／「{raw[h][i_pend]}」兩欄都是空白，"
                "推廣期改用畫面輸入的日期。"
            )

    seen: Dict[str, int] = {}
    duplicates: List[str] = []
    bad_rows: List[str] = []
    fractional: List[str] = []
    shared: List[str] = []
    group_key = ""

    for offset, row in enumerate(body, start=h + 2):
        if not any(c not in (None, "") for c in row):
            continue
        sku = str(row[i_sku]).strip() if i_sku < len(row) and row[i_sku] is not None else ""
        if not sku:
            continue
        # Excel renders long numeric article codes as 1.10331e+11 or 110331002001.0
        if re.fullmatch(r"\d+\.0", sku):
            sku = sku[:-2]

        desc = ""
        if i_desc is not None and i_desc < len(row) and row[i_desc] is not None:
            desc = str(row[i_desc]).strip()

        raw_qty = row[i_target] if i_target < len(row) else None
        qty = to_number(raw_qty)
        if qty is None:
            # A blank under a filled cell is how one target is written for a
            # block of SKUs: merged across the rows, or simply left empty once.
            # Both mean "same target as above", never "no target" — a row with
            # no quantity at all cannot be allocated, so it would otherwise be
            # dropped and its stock silently ignored.
            if group_key and not str(raw_qty or "").strip():
                if sku in seen:
                    duplicates.append(sku)
                    continue
                seen[sku] = 1
                shared.append(sku)
                out.rows.append(TargetRow(sku, desc, 0, group=group_key))
                continue
            bad_rows.append(f"第 {offset} 行（{sku}）：Target 不是數字")
            continue
        if qty < 0:
            bad_rows.append(f"第 {offset} 行（{sku}）：Target 是負數 {qty:g}，已略過")
            continue
        if qty != int(qty):
            fractional.append(sku)
        qty_i = int(round(qty))

        if sku in seen:
            duplicates.append(sku)
            continue
        seen[sku] = 1
        group_key = sku
        out.rows.append(TargetRow(sku, desc, qty_i))

    # The first row of a block gains its group only once a follower proves it
    # has one, so parse in order and fix the owner up at the end.
    members = {row.group for row in out.rows if row.group}
    out.rows = [
        TargetRow(r.sku, r.description, r.qty, r.group or (r.sku if r.sku in members else ""))
        for r in out.rows
    ]

    if shared:
        out.warnings.append(
            f"有 {len(shared)} 個 SKU 的 Target 空白（合併儲存格或留空），"
            "已當作與上一行共用同一個 Target："
            + "、".join(shared[:8])
            + ("…" if len(shared) > 8 else "")
            + "。如屬漏填，請在 Excel 補上數值。"
        )

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
    i_moq = _find(headers, MOQ_KEYS, exclude={i_sku, i_shop, i_stock})
    i_supply = _find(
        headers, SUPPLY_KEYS, exclude={i_sku, i_shop, i_stock, i_rp, i_moq}
    )

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
            "moq": original[i_moq] if i_moq is not None else "",
            "supply_source": original[i_supply] if i_supply is not None else "",
        }
        out.warnings.append("偵測為長表格式（每行一個 SKU × 店舖）")
        pairs2: Dict[Tuple[str, str], int] = {}
        rp_map: Dict[Tuple[str, str], str] = {}
        rp_conflict = set()
        pending_map: Dict[Tuple[str, str], int] = {}
        sales_map: Dict[Tuple[str, str], Tuple[int, int]] = {}
        launch_map: Dict[Tuple[str, str], date] = {}
        moq_map: Dict[Tuple[str, str], int] = {}
        supply_map: Dict[Tuple[str, str], str] = {}
        supply_conflict = set()
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

            m = to_number(row[i_moq]) if i_moq is not None and i_moq < len(row) else None
            if m and m > 1:
                # Repeated on every site of the same SKU; the largest wins so a
                # stray 1 on one row cannot cancel the real minimum.
                moq_map[key] = max(moq_map.get(key, 1), int(round(m)))

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

            if i_supply is not None and i_supply < len(row):
                # Kept as text: SAP writes 2 as a number in some exports and as
                # text in others, and the route must not depend on cell format.
                code = supply_code(row[i_supply])
                if code:
                    prev = supply_map.get(key, "")
                    if not prev:
                        supply_map[key] = code
                    elif prev != code:
                        # Two routes for one SKU × shop: pick the warehouse so
                        # the goods still come from somewhere, and say so.
                        supply_conflict.add(f"{sku}@{shop}")
                        supply_map[key] = "2"

        if dup2:
            out.warnings.append(
                "同一 SKU 同一店舖出現多於一次，已相加：" + "、".join(sorted(dup2)[:5])
            )
        if rp_conflict:
            out.warnings.append(
                "以下 SKU × 店舖有多個不同的 RP Type，已當作未提供："
                + "、".join(sorted(rp_conflict)[:5])
            )
        if supply_conflict:
            out.warnings.append(
                "以下 SKU × 店舖的 Supply Source 不一致，已當作倉貨（D001 出 STO）："
                + "、".join(sorted(supply_conflict)[:5])
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
                moq=moq_map.get((sku, shop), 1),
                supply_source=supply_map.get((sku, shop), ""),
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

_HEAD_FILL = PatternFill("solid", fgColor="FFEDF1F5")
_HEAD_FONT = Font(bold=True)
_MONEY = "#,##0"

# The colours ops already paints these columns with by hand: yellow = raise a
# PO, orange = raise an STO from D001, pale red = the stock is not there at all.
# Matching the palette means the export needs no retouching before it is sent.
_PO_FILL = PatternFill("solid", fgColor="FFFFFF00")
_STO_FILL = PatternFill("solid", fgColor="FFFFC000")
_SHORT_FILL = PatternFill("solid", fgColor="FFFFC7CE")
_SHORT_FONT = Font(color="FF9C0006")


def _tinted(
    ws,
    value,
    fill: Optional[PatternFill] = None,
    fmt: Optional[str] = None,
    font: Optional[Font] = None,
) -> WriteOnlyCell:
    """A write-only cell that carries a colour, so the sheet reads at a glance."""
    cell = WriteOnlyCell(ws, value=value)
    if fill is not None:
        cell.fill = fill
    if fmt:
        cell.number_format = fmt
    if font is not None:
        cell.font = font
    return cell


def _put_header(ws, headers: Sequence[str]) -> None:
    cells = []
    for text in headers:
        cell = WriteOnlyCell(ws, value=text)
        cell.font = _HEAD_FONT
        cell.fill = _HEAD_FILL
        # Headings such as 全期目標（10日 Target） are wider than any sensible
        # column, so they wrap instead of being clipped.
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cells.append(cell)
    ws.append(cells)


def _target_heads(params: dict) -> Tuple[str, str]:
    """Column names for the full-period target and the dispatch-window share.

    The detail sheet carries two target bases at once — 行貨 is allocated over
    the whole promotion, 倉貨 over the first few days — so each quantity says
    which period it is measured against. Without the days in the header the
    same SKU looks like it has two unrelated allocations.
    """
    promo_days = params.get("promo_days")
    dispatch_days = params.get("dispatch_days")
    full = f"全期目標（{promo_days}日 Target）" if promo_days else "全期目標"
    window = (
        f"分配數量（{dispatch_days}日 Target）" if dispatch_days else "分配數量"
    )
    return full, window


# Widths that fit the Chinese headings and the numbers under them. The default
# 8.43 clips most of them, and this file is read by people, not by a parser.
_DETAIL_WIDTHS = {
    "A": 13, "B": 38, "C": 8, "D": 7, "E": 9, "F": 10, "G": 8, "H": 16,
    "I": 16, "J": 9, "K": 7, "L": 7, "M": 7, "N": 7, "O": 8, "P": 10,
    "Q": 12, "R": 8, "S": 8, "T": 9, "U": 9, "V": 9, "W": 22,
}


def _detail_layout(ws, last_row: int) -> None:
    """Freeze the identifying columns, filter the header, size the columns.

    A frozen header and auto-filter are what the shop-by-shop sheets are used
    for: scroll to row 140 and you still know which column you are reading.
    """
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:W{last_row}"
    ws.row_dimensions[1].height = 45
    for col, width in _DETAIL_WIDTHS.items():
        ws.column_dimensions[col].width = width


def _compare_layout(ws, last_row: int, n_cols: int) -> None:
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(n_cols)}{last_row}"
    ws.row_dimensions[1].height = 30
    ws.column_dimensions["A"].width = 13
    ws.column_dimensions["B"].width = 38
    for idx in range(3, n_cols + 1):
        ws.column_dimensions[get_column_letter(idx)].width = 9.5


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
    # 開 PO sits inside the scenario block because it moves with the share mix,
    # unlike 派貨目標 or 日銷 which are the same for A/B/C.
    for sc in scenarios:
        headers += [f"{sc} 需求", f"{sc} 派貨", f"{sc} 開 PO（全期）", f"{sc} 缺貨"]
    # Layout has to be set before the first row is written: a write-only sheet
    # flushes its column widths and pane settings with the header row, so
    # anything set afterwards is silently dropped.
    _compare_layout(ws, 1 + len(compare_rows), len(headers))
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
        po_cols: List[int] = []
        short_cols: List[int] = []
        for sc in scenarios:
            s = row["scenarios"].get(sc)
            # Remember where the two action columns land: the block repeats per
            # scenario, so their positions move with the scenario count.
            po_cols.append(len(line) + 2)
            short_cols.append(len(line) + 3)
            line += [
                s.need_qty if s else 0,
                s.total_dispatch if s else 0,
                s.po_qty if s else 0,
                s.short_qty if s else 0,
            ]
        cells = list(line)
        for idx in po_cols:
            if line[idx]:
                cells[idx] = _tinted(ws, line[idx], _PO_FILL)
        for idx in short_cols:
            if line[idx]:
                cells[idx] = _tinted(ws, line[idx], _SHORT_FILL, font=_SHORT_FONT)
        ws.append(cells)

    # --- detail sheet per scenario ---
    desc_of = {r["sku"]: r.get("description", "") for r in compare_rows}
    full_head, window_head = _target_heads(params)
    for sc in scenarios:
        res = results.get(sc)
        if not res:
            continue
        ws2 = wb.create_sheet(f"明細_{sc}")
        detail_heads = [
            "SKU", "貨品名稱", "店舖", "貨源", "Supply Source", "分配基準", "佔比",
            full_head, window_head, "現有庫存", "在途", "可用", "差距", "MOQ",
            "需求", "可派 STO", "開 PO（全期）", "缺貨",
            "日銷", "預計銷量", "可調出", "RP Type", "不派原因",
        ]
        # Before the header row, for the same reason as the comparison sheet.
        _detail_layout(ws2, 1 + len(res.lines))
        _put_header(ws2, detail_heads)
        moq_of = {s.sku: s.moq for s in res.skus}
        # Positions of the three action columns in the row list below: 需求 (14)
        # is where 分配量 lands, 可派 STO (15), 開 PO（全期）(16), 缺貨 (17).
        _STO_COL, _PO_COL, _SHORT_COL = 15, 16, 17
        for ln in res.lines:
            reason = ""
            if ln.blocked:
                reason = (
                    f"RP={ln.rp_type}" if ln.rp_type else "未提供 RP Type"
                ) + f"（需求 {ln.need_qty}）"
            values: List[Any] = [
                ln.sku,
                desc_of.get(ln.sku, ""),
                ln.shop_code,
                "行貨" if ln.vendor else "倉貨",
                ln.supply_source,
                "全期" if ln.alloc_full_period else "派貨期",
                round(ln.share_pct, 6),
                ln.alloc_full_qty,
                ln.alloc_qty,
                ln.stock_qty,
                ln.pending_qty,
                ln.on_hand,
                ln.gap_qty,
                moq_of.get(ln.sku, 1),
                ln.need_qty,
                ln.sto_qty,
                ln.po_qty,
                ln.short_qty,
                round(ln.daily_rate, 2),
                ln.expected_sales,
                ln.surplus_qty,
                ln.rp_type,
                reason,
            ]
            # The three columns the warehouse acts on carry a colour, the same
            # way the sheets are marked up by hand: yellow = raise a PO, orange
            # = raise an STO from D001, pale red = the stock simply is not there.
            if ln.sto_qty:
                values[_STO_COL] = _tinted(ws2, ln.sto_qty, _STO_FILL)
            if ln.po_qty:
                values[_PO_COL] = _tinted(ws2, ln.po_qty, _PO_FILL)
            if ln.short_qty:
                values[_SHORT_COL] = _tinted(
                    ws2, ln.short_qty, _SHORT_FILL, font=_SHORT_FONT
                )
            ws2.append(values)

    # --- parameters ---
    ws3 = wb.create_sheet("參數")
    ws3.append(["項目", "值"])
    ws3.append(["期間", run.get("period", "")])
    ws3.append(["計算時間", run.get("created_at", "")])
    ws3.append(["執行者", run.get("created_by", "")])
    ws3.append(["SKU 數", run.get("n_sku", "")])
    ws3.append(["不夠貨 SKU 數", run.get("n_short", "")])
    ws3.append(
        [
            "顏色",
            "黃 = 需要開 PO（Buyer 落單）；橙 = 需要開 STO（RP 由 D001 出貨）；"
            "淺紅 = 缺貨（倉存不足，要跟進）",
        ]
    )
    for note in params.get("shared_targets") or []:
        # The comparison sheet shows a shared target as one row; this is where
        # the per-SKU split behind that row can be checked.
        ws3.append(["共用 Target", note])
    if params.get("promo_start"):
        ws3.append(["推廣開始", params.get("promo_start", "")])
        ws3.append(["推廣完結", params.get("promo_end", "")])
        ws3.append(["推廣日數", params.get("promo_days", "")])
        ws3.append(["派貨日數", params.get("dispatch_days", "")])
        ws3.append(
            [
                f"明細「{full_head}」",
                f"整段推廣（{params.get('promo_days', '')} 日）的 Target × 該店佔比",
            ]
        )
        ws3.append(
            [
                f"明細「{window_head}」",
                f"只計首 {params.get('dispatch_days', '')} 日的 Target × 該店佔比；"
                "行貨店舖例外，仍按全期 Target 分配（看「分配基準」欄）",
            ]
        )
        ws3.append(
            [
                "推廣期來源",
                "Target 檔的 Start date (Promo)／End date (Promo)"
                if params.get("promo_source") == "target_file"
                else "畫面輸入",
            ]
        )
    if params.get("as_of"):
        ws3.append(["銷售數據截止", params.get("as_of", "")])
    if params.get("warehouse_sites"):
        ws3.append(["貨倉", "、".join(params["warehouse_sites"])])
        ws3.append(["貨倉口徑", "只計 SaSa Net Stock；Pending Received 為入倉量，不計入"])
    if params.get("has_sales") is False:
        ws3.append(["銷售數據", "庫存檔沒有銷售欄位，未計算預計銷量與差距"])
    if params.get("has_moq") is False:
        ws3.append(["MOQ", "庫存檔沒有 MOQ 欄，全部當作 1（需求不進位）"])
    elif params.get("has_moq"):
        ws3.append(["MOQ 來源", "店舖庫存檔的 MOQ 欄；每店需求向上取至 MOQ 倍數"])
    for sc in scenarios:
        name = (labels.get(sc) or {}).get("label") or ""
        if name:
            ws3.append([f"{sc} 餐名稱", name])
    if gate:
        ws3.append(["RP Type 管制店舖", "、".join(sorted(gate))])
        ws3.append(["RP Type 規則", "只有 RP Type = RF 才派貨；ND 或未提供一律 0 件"])
    if params.get("vendor_supply_sources"):
        ws3.append(
            ["行貨 Supply Source", "、".join(params["vendor_supply_sources"])]
        )
        ws3.append(
            [
                "Supply Source 規則",
                "行貨由 Buyer 開 PO，不佔 D001 倉存、不計入缺貨；"
                "2（或其他代碼）為倉貨，由 RP 開 STO，受倉存上限限制",
            ]
        )
        ws3.append(
            [
                "行貨計算基準",
                "按整段推廣的全期 Target 計算（不受派貨日數縮小）；"
                "倉貨仍只派首 " + str(params.get("dispatch_days", "")) + " 日",
            ]
        )
    elif params.get("has_supply_source") is False:
        ws3.append(["Supply Source", "庫存檔沒有此欄，全部店舖當作倉貨（D001 出 STO）"])
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
    # The template mirrors a real target sheet, promo window included: the two
    # date columns are what the portal reads by default, so a template without
    # them would send people back to typing dates on the form.
    sample_start = date(2026, 11, 17)
    sample_end = date(2026, 11, 26)

    wb = Workbook()
    ws = wb.active
    ws.title = "SKU Target"
    ws.append(
        ["SKU", "貨品名稱", "Target Qty", "Start date (Promo)", "End date (Promo)"]
    )
    for cell in ws[1]:
        cell.font = _HEAD_FONT
        cell.fill = _HEAD_FILL
    for sku, name, qty in (
        ("109991605010", "示範貨品 A", 1200),
        ("110331002001", "示範貨品 B", 800),
    ):
        ws.append([sku, name, qty, sample_start, sample_end])
        # Real date cells shown the way ops writes them (17-Nov-26), so the
        # column can be copied straight out of a live sheet.
        for col in (4, 5):
            ws.cell(row=ws.max_row, column=col).number_format = "dd-mmm-yy"
    for col, width in zip("ABCDE", (18, 30, 12, 18, 18)):
        ws.column_dimensions[col].width = width

    note = wb.create_sheet("說明")
    note.append(["SKU", "必填，不可重複"])
    note.append(["Target Qty", "必填，整數"])
    note.append(
        [
            "共用 Target",
            "同一貨品的多個包裝可只填第一行（其餘留空或合併儲存格），"
            "系統當一個貨組，按銷量比例拆回各 SKU",
        ]
    )
    note.append(
        [
            "Start date (Promo)／End date (Promo)",
            "選填（格式例如 17-Nov-26）。有這兩欄就自動當推廣期，"
            "逐行重複或整段合併儲存格都可以；留空才用畫面上填的日期",
        ]
    )
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
    note.append(["長表可加一欄 MOQ；每店需求會向上取至 MOQ 的倍數（同一 SKU 取最大）"])
    note.append(
        [
            "長表可加一欄 Supply Source；1／4 是行貨（Buyer 開 PO，不佔 D001 倉存），"
            "2 是倉貨（RP 由 D001 開 STO）",
        ]
    )
    note.append(["SAP 匯出的明細（Article / Site / SaSa Net Stock / RP Type / MOQ）可直接上載"])
    return _save(wb)


def _save(wb: Workbook) -> bytes:
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
