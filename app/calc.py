"""Pure calculation core — no database, no files, no framework.

The business model
------------------
A promotion has, for each SKU, a total target quantity to sell. That target is
distributed across shops according to a *share set* (店舖生意佔比). There are
three alternative share sets per month — A, B and C — so the buyer can compare
how the same target lands differently under three distribution assumptions.

For each shop:

    alloc    = target × share            (rounded so the column sums to target)
    gap      = alloc - stock             (how much this shop is short)
    dispatch = ceil(gap / MOQ) × MOQ     (what actually gets sent, if gap > 0)

MOQ is a property of the SKU on the stock side, not of the target: the buyer
sets a target quantity, the warehouse file (SAP) says how the SKU is packed.
So ``compute_scenario`` takes ``moq_by_sku`` and never reads a MOQ off
``SkuTarget``. A SKU with no MOQ — or a stock file without the column — is
treated as 1, i.e. quantities are not rounded at all.

For each SKU:

    total_stock = Σ stock
    shortfall   = max(target - total_stock, 0)   -> what must be bought
    surplus     = Σ max(stock - alloc, 0)        -> what can be pulled from other shops

    enough = total_stock >= target

``surplus`` (可調出) is only reported when the warehouse could not cover the
demand in the first place (see ``_warehouse_short``): if D001 has the stock, the
units are sent from there and no shop is asked to give anything back. Vendor
shops are never counted — a transfer of 行貨 is arranged by the buyer in another
system, so a number here would only duplicate work done elsewhere.

Note that "enough" can be true while dispatch is non-zero: the goods exist, they
are just in the wrong shops. That distinction is the whole point of the report,
so it is modelled explicitly as ``coverage_status``.

RP Type gate
------------
A few shops (currently HB87) cannot receive promotional stock unless the SKU's
RP Type for that shop is ``RF``. When the RP Type is ``ND`` — or missing — the
dispatch is forced to zero even though the shop may be short of its allocation.
Shops outside the gate ignore RP Type entirely.

The gate is applied *after* the normal dispatch is computed, so the report can
still show how much *would* have been sent and why it was stopped, instead of
silently reporting a zero.

Supply source
-------------
Each SKU × shop row of the SAP stock file carries a ``Supply source`` code,
which says who actually supplies that shop:

    1 / 4  vendor    — 行貨, the buyer raises a PO; D001 is not involved
    2      warehouse — 倉貨, RP issues an STO from D001

The distinction is not cosmetic: goods ordered by PO never leave the warehouse,
so a vendor shop's need must not consume D001's stock nor be reported as a
warehouse shortage. Only warehouse-sourced needs are capped by D001 and only
they can be short of it. Vendor needs are reported as ``po_qty`` — a worklist
for the buyer — while ``total_dispatch`` still counts everything the shop
receives, whichever route it takes.

The two routes also answer to different targets. A PO is raised once for the
whole promotion, so a vendor shop is allocated its share of the full target
however few days D001 sends out; a warehouse shop is still measured against
the dispatch window. That is why ``Line.alloc_qty`` can exceed the scaled
``dispatch_target``: the line is flagged with ``alloc_full_period`` so the
report can say which basis it used rather than mixing the two silently.

A stock file with no Supply source column — or a blank cell, or a code we do
not recognise — is treated as warehouse, i.e. exactly the behaviour before this
rule existed; the run warns when that fallback is used.

Days of cover
-------------
A promotion can run for weeks, but shops are only sent the first few days of
stock (4 by default). ``PromoWindow`` scales the SKU target by
``dispatch_days / promotion_days`` before it is split across shops:

    dispatch_target = round(target × dispatch_days / promotion_days)
    rate            = (last month sold + MTD sold) / days on record
    expected_sales  = rate × promotion_days

``days on record`` runs from the first of last month — or the launch date for a
new product — to the as-of date, so a SKU launched five days ago is not diluted
by a month of zeroes.

Warehouse cap
-------------
What can actually be sent is limited by the warehouse (D001) net stock. When the
sum of shop needs exceeds it, the available units are shared in proportion to
need (largest remainder, whole MOQ steps) and the remainder is reported as
shortage. Pending receipts at D001 are *not* counted: they are inbound to the
warehouse and may never arrive within the promotion.

Shared target (貨組)
--------------------
A target file often gives one quantity to several SKUs — two pack sizes of the
same product, written as 4000 on the first row and a blank or merged cell
underneath. Those SKUs form a *group*: the 4000 belongs to the group, not to any
one member.

The group is the unit the buyer reviews, so the group is the unit that must
answer "夠貨". Judging members one at a time would report a shortage for the
pack that happens to be scarce even though the group as a whole is covered.

How it is calculated:

    split      = group target apportioned by sales mix (fallback: stock, equal)
    group_need = max(Σ alloc - Σ (stock + pending), 0)   per shop
    dispatch   = members' own needs, capped so they sum to group_need
    covered    = Σ (stock + pending + warehouse) over members ≥ group target

The split only decides how the group target is *reported* per member; the
per-shop cap is what keeps the dispatch honest. Without it, two members that
each look short in a shop would both receive stock for the same missing units.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

COVERAGE_SUFFICIENT = "sufficient"  # total shop stock covers the target
COVERAGE_SHORT = "short"  # not enough goods anywhere

RP_DISPATCHABLE = "RF"  # the only RP Type that unlocks dispatch at a gated shop

SUPPLY_WAREHOUSE = "warehouse"  # 倉貨: D001 -> shop, RP raises an STO
SUPPLY_VENDOR = "vendor"  # 行貨: vendor -> shop, buyer raises a PO
# Supply source codes meaning 行貨. 1 is the plain vendor route; 4 is 行貨 too
# but its goods are booked through D001 on the way to the shop — an accounting
# step, not warehouse stock, so it still never draws on D001 (user, 2026-11-17).
DEFAULT_VENDOR_SOURCES = ("1", "4")
# Codes we know and route without guessing, so the "unrecognised code" warning
# only fires for codes nobody has explained. 4 falls back to warehouse only when
# an admin deliberately takes it out of the 行貨 list.
KNOWN_WAREHOUSE_SOURCES = ("2", "4")


@dataclass(frozen=True)
class SkuTarget:
    """One SKU's target, and — when several SKUs share one quantity — its group.

    ``group`` is the SKU that owns the number in the spreadsheet (the first row
    of the block); every member carries the same key, including the owner, so a
    group is always found by ``target.group``. Standalone SKUs leave it empty.
    """

    sku: str
    target_qty: int
    description: str = ""
    group: str = ""


@dataclass(frozen=True)
class ShopSales:
    """Sales and pipeline figures from one SKU × shop row of the stock file."""

    last_month_sold: int = 0
    mtd_sold: int = 0
    pending_qty: int = 0
    launch_date: Optional[date] = None


@dataclass(frozen=True)
class PromoWindow:
    """The promotion period and how many days of stock are sent at a time."""

    start: date
    end: date
    dispatch_days: int = 4

    @property
    def days(self) -> int:
        return max((self.end - self.start).days + 1, 0)

    @property
    def effective_dispatch_days(self) -> int:
        if self.days <= 0:
            return 0
        return max(1, min(self.dispatch_days, self.days))

    def dispatch_target(self, target_qty: int) -> int:
        """Target scaled down to the dispatch window, never below one unit."""
        if target_qty <= 0 or self.days <= 0:
            return max(target_qty, 0)
        scaled = target_qty * self.effective_dispatch_days / self.days
        return max(int(math.floor(scaled + 0.5)), 1)


@dataclass(frozen=True)
class Line:
    sku: str
    shop_code: str
    share_pct: float
    alloc_qty: int
    stock_qty: int
    gap_qty: int
    dispatch_qty: int
    surplus_qty: int
    rp_type: str = ""
    blocked: bool = False
    blocked_qty: int = 0
    pending_qty: int = 0
    daily_rate: float = 0.0
    expected_sales: int = 0
    need_qty: int = 0
    short_qty: int = 0
    supply_source: str = ""
    supply_mode: str = SUPPLY_WAREHOUSE
    po_qty: int = 0
    alloc_full_period: bool = False
    # The same shop's share of the *whole* promotion target, whichever basis
    # ``alloc_qty`` was taken on. Kept for the report so "600 over 12 days"
    # and "200 over the first 4" can be read side by side instead of one
    # number that silently means different periods on different rows.
    alloc_full_qty: int = 0

    @property
    def on_hand(self) -> int:
        """Real stock plus what is already on its way to the shop."""
        return self.stock_qty + self.pending_qty

    @property
    def vendor(self) -> bool:
        """行貨: supplied by the vendor on a PO, not from the warehouse."""
        return self.supply_mode == SUPPLY_VENDOR

    @property
    def sto_qty(self) -> int:
        """The part of the dispatch that leaves D001 on an STO."""
        return max(self.dispatch_qty - self.po_qty, 0)


@dataclass(frozen=True)
class SkuSummary:
    sku: str
    description: str
    target_qty: int
    total_alloc: int
    total_stock: int
    total_dispatch: int
    surplus_qty: int
    shortfall_qty: int
    n_shops_needing: int
    coverage_status: str
    moq: int
    dispatch_target: int = 0
    total_pending: int = 0
    warehouse_qty: int = 0
    warehouse_pending: int = 0
    daily_rate: float = 0.0
    expected_sales: int = 0
    gap_target: int = 0
    need_qty: int = 0
    short_qty: int = 0
    blocked_qty: int = 0
    gap_supply: int = 0
    group: str = ""
    po_qty: int = 0
    po_target: int = 0

    @property
    def enough(self) -> bool:
        return self.coverage_status == COVERAGE_SUFFICIENT

    @property
    def sto_qty(self) -> int:
        """Dispatch that leaves the warehouse, i.e. everything not on a PO."""
        return max(self.total_dispatch - self.po_qty, 0)

    @property
    def is_group_lead(self) -> bool:
        """True for the SKU that carries the shared target (and its shortfall).

        A group's deficit is reported once — on the row that owns the number —
        so summing shortfall across SKUs never counts the same units twice.
        """
        return bool(self.group) and self.group == self.sku


@dataclass
class ScenarioResult:
    scenario: str
    lines: List[Line]
    skus: List[SkuSummary]
    share_total: float
    warnings: List[str] = field(default_factory=list)

    @property
    def n_sku(self) -> int:
        return len(self.skus)

    @property
    def n_short(self) -> int:
        """How many targets are not covered — a shared target counts once."""
        return len(
            {
                s.group or s.sku
                for s in self.skus
                if s.coverage_status == COVERAGE_SHORT
            }
        )

    @property
    def total_target(self) -> int:
        return sum(s.target_qty for s in self.skus)

    @property
    def total_dispatch(self) -> int:
        return sum(s.total_dispatch for s in self.skus)


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def normalise_shares(
    shops: Sequence[str],
    pct: Dict[str, float],
) -> Tuple[List[float], float, List[str]]:
    """Return shares that sum to exactly 1.0, the raw total, and any warnings.

    Shares are stored as entered (0–1). If the entered total deviates from 1.0
    we normalise proportionally and say so, rather than silently producing a
    distribution that does not add up to the target.
    """
    raw = [max(float(pct.get(code, 0.0) or 0.0), 0.0) for code in shops]
    total = sum(raw)
    warnings: List[str] = []

    if not shops:
        return [], 0.0, ["沒有任何啟用中的店舖"]

    if total <= 0:
        warnings.append("所有店舖佔比都是 0，無法分配目標")
        return [0.0] * len(shops), 0.0, warnings

    if abs(total - 1.0) > 5e-4:
        warnings.append(
            f"佔比總和為 {total:.4%}，不等於 100%，已按比例正規化後再分配"
        )
        return [x / total for x in raw], total, warnings

    return raw, total, warnings


def apportion(total: int, shares: Sequence[float]) -> List[int]:
    """Split an integer total by shares using the largest-remainder method.

    Guarantees sum(result) == total, so the per-shop column always reconciles
    with the SKU target. Plain rounding would leave a residual of a few units
    that nobody can explain.
    """
    n = len(shares)
    if n == 0:
        return []
    if total <= 0:
        return [0] * n
    # Nothing to allocate against: never hand units to a shop with zero share.
    if sum(shares) <= 1e-12:
        return [0] * n

    raw = [total * s for s in shares]
    out = [int(math.floor(x)) for x in raw]
    residual = total - sum(out)

    if residual > 0:
        order = sorted(range(n), key=lambda i: (-(raw[i] - out[i]), i))
        for i in order[:residual]:
            out[i] += 1
    elif residual < 0:
        order = sorted(range(n), key=lambda i: (raw[i] - out[i], i))
        for i in order[: -residual]:
            if out[i] > 0:
                out[i] -= 1
    return out


def round_up_to(qty: float, moq: float) -> int:
    """Round a dispatch quantity up to a whole multiple of MOQ."""
    if qty <= 0:
        return 0
    step = int(round(moq or 1))
    if step <= 1:
        return int(math.ceil(qty))
    return int(math.ceil(qty / step) * step)


def sku_moq(moq_by_sku: Optional[Dict[str, int]], sku: str) -> int:
    """The MOQ to apply to one SKU, taken from the stock file.

    Missing, blank or unparseable values all mean "no minimum" rather than an
    error: a stock file without a MOQ column must still produce a report.
    """
    if not moq_by_sku:
        return 1
    try:
        value = int(round(float(moq_by_sku.get(sku, 1) or 1)))
    except (TypeError, ValueError):
        return 1
    return max(value, 1)


def supply_code(value: Any) -> str:
    """'Supply source' as a comparable string.

    SAP hands the code over as text, as ``2``, or as ``2.0`` depending on how
    the cell was formatted, so all three must land on the same value — otherwise
    a whole shop silently changes route because of a cell format.
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        number = float(value)
        return str(int(number)) if number.is_integer() else str(number)
    text = str(value).strip()
    # A text cell holding "2.0" is the same code as 2; anything else is left
    # exactly as typed, so a leading zero can never be lost.
    match = re.fullmatch(r"(\d+)\.0+", text)
    return match.group(1) if match else text


def supply_mode(
    code: str, vendor_sources: Sequence[str] = DEFAULT_VENDOR_SOURCES
) -> str:
    """Which route a shop takes for this SKU: vendor (PO) or warehouse (STO).

    Only the codes configured as 行貨 (``1`` and ``4`` by default) mean vendor.
    ``4`` is the odd one: the goods are booked through D001 on their way to the
    shop, but they come from the vendor on a buyer's PO, so they are not stock
    the warehouse can promise. ``2`` is 倉貨, and so is anything blank or
    unrecognised — blank has always meant warehouse, which keeps a file without
    the column behaving exactly as before. The caller warns when the fallback is
    used, so the assumption never stays silent.
    """
    vendors = {str(v).strip().upper() for v in vendor_sources if str(v).strip()}
    return SUPPLY_VENDOR if supply_code(code).upper() in vendors else SUPPLY_WAREHOUSE


# ---------------------------------------------------------------------------
# shared targets
# ---------------------------------------------------------------------------


def split_shared_targets(
    targets: Iterable[SkuTarget],
    sales_by_sku: Optional[Dict[str, Dict[str, ShopSales]]] = None,
    stock_by_sku: Optional[Dict[str, Dict[str, float]]] = None,
) -> Tuple[List[SkuTarget], List[str]]:
    """Turn one shared quantity into a target per member SKU.

    The spreadsheet says "4000 for this block"; the report has to say how much
    of that belongs to the 7PC and how much to the 14PC. Sales mix decides,
    because that is what the shops actually sell; when the stock file carries no
    sales columns the split falls back to what is on hand, and then to an even
    split. Whichever basis is used is reported back, never applied silently.

    The returned notes are shown on the result page and written to the export,
    so the split can be checked without opening the source files again.
    """
    items = list(targets)
    groups: Dict[str, List[int]] = {}
    for index, item in enumerate(items):
        if item.group:
            groups.setdefault(item.group, []).append(index)

    out = list(items)
    notes: List[str] = []

    for key, indexes in groups.items():
        if len(indexes) < 2:
            continue
        members = [items[i] for i in indexes]
        total = max(members[0].target_qty, 0)
        if total <= 0:
            continue

        weights, basis, caveat = _group_weights([m.sku for m in members], sales_by_sku, stock_by_sku)
        parts = apportion(total, weights)

        for index, part in zip(indexes, parts):
            member = items[index]
            out[index] = replace(member, target_qty=part, group=key)

        notes.append(
            " + ".join(m.sku for m in members)
            + f"：共用 {total:,} 件，按{basis}拆為 "
            + "／".join(f"{p:,}" for p in parts)
            + (f"（{caveat}）" if caveat else "")
        )

    return out, notes


def _group_weights(
    skus: Sequence[str],
    sales_by_sku: Optional[Dict[str, Dict[str, ShopSales]]],
    stock_by_sku: Optional[Dict[str, Dict[str, float]]],
) -> Tuple[List[float], str, str]:
    """Relative share of the group target per SKU, plus how it was decided."""
    sales = []
    for sku in skus:
        per_shop = (sales_by_sku or {}).get(sku) or {}
        sales.append(
            float(
                sum(
                    max(s.last_month_sold, 0) + max(s.mtd_sold, 0)
                    for s in per_shop.values()
                )
            )
        )
    if sum(sales) > 0:
        return [s / sum(sales) for s in sales], "銷量比例", ""

    stock = [
        float(sum(((stock_by_sku or {}).get(sku) or {}).values()))
        for sku in skus
    ]
    if sum(stock) > 0:
        return [s / sum(stock) for s in stock], "現有庫存比例", "庫存檔沒有銷量數據"

    even = [1.0 / len(skus)] * len(skus)
    return even, "平均", "沒有銷量與庫存數據"


# ---------------------------------------------------------------------------
# sales rate
# ---------------------------------------------------------------------------


def sales_window_start(as_of: date, launch_date: Optional[date] = None) -> date:
    """First day covered by the Last Month + MTD sales figures.

    That is the first of last month, unless the SKU launched later — a product
    launched last week must not be divided by six weeks of zeroes.
    """
    first_of_this_month = as_of.replace(day=1)
    start = (first_of_this_month - timedelta(days=1)).replace(day=1)
    if launch_date and launch_date > start:
        return launch_date
    return start


def daily_sales_rate(sales: ShopSales, as_of: Optional[date]) -> float:
    """Units sold per day at this shop, or 0 when there is nothing to go on."""
    if as_of is None:
        return 0.0
    start = sales_window_start(as_of, sales.launch_date)
    days = (as_of - start).days + 1
    if days <= 0:
        return 0.0
    sold = max(sales.last_month_sold, 0) + max(sales.mtd_sold, 0)
    return sold / days


def dispatch_window(promo: PromoWindow) -> Optional[Tuple[date, date]]:
    """The one dispatch run of the promotion: its first ``dispatch_days`` days.

    Only the first few days of stock are sent, so there is exactly one run —
    not a series of top-ups. Clamping to ``effective_dispatch_days`` keeps the
    window inside the promotion when dispatch_days exceeds its length.

    These are the selling dates the stock is meant to cover; the warehouse
    still needs its own lead time, which we do not assume.
    """
    if promo.days <= 0:
        return None
    start = promo.start
    end = start + timedelta(days=promo.effective_dispatch_days - 1)
    return start, end


def cap_to_available(
    needs: Sequence[int], available: int, moq: float = 1
) -> List[int]:
    """Share ``available`` units across ``needs``, in whole MOQ steps.

    The scarce units go where the need is, in proportion, using the largest
    remainder method. Nothing is handed out beyond a shop's own need, and the
    result never exceeds ``available`` — a warehouse cannot send what it does
    not have.
    """
    n = len(needs)
    if n == 0:
        return []
    step = max(int(round(moq or 1)), 1)
    total = sum(needs)
    if total <= 0:
        return [0] * n
    if available >= total:
        return list(needs)

    steps_available = max(int(available), 0) // step
    if steps_available <= 0:
        return [0] * n

    max_steps = [n_ // step for n_ in needs]
    raw = [steps_available * need / total for need in needs]
    out = [min(int(math.floor(x)), cap) for x, cap in zip(raw, max_steps)]

    residual = steps_available - sum(out)
    order = sorted(range(n), key=lambda i: (-(raw[i] - out[i]), i))
    while residual > 0:
        moved = False
        for i in order:
            if residual <= 0:
                break
            if out[i] < max_steps[i]:
                out[i] += 1
                residual -= 1
                moved = True
        if not moved:
            break
    return [o * step for o in out]


def _warehouse_short(rows: Sequence[dict], dispatched: Sequence[int]) -> int:
    """How much the warehouse failed to send to the shops that rely on it.

    Only warehouse-sourced, non-gated rows count: a vendor shop is answered by a
    PO rather than D001, and a gated shop's need is refused by rule instead of
    being missing from the warehouse. Anything above zero is the signal that
    stock has to be pulled back from a shop holding more than it needs.
    """
    return sum(
        max(row["need"] - qty, 0)
        for row, qty in zip(rows, dispatched)
        if not row["vendor"] and not row["blocked"]
    )


def _cap_group_needs(prepared: Sequence[dict], n_shops: int) -> None:
    """Cap a group's per-shop needs at what the group as a whole is missing.

    Each member computes its need against its own share of the shared target,
    but the other members' stock sits in the same shop and sells against the
    same target. Handing both members their own gap would send the missing
    units twice; what the shop really lacks is ``Σ alloc − Σ on hand``.
    """
    groups: Dict[str, List[dict]] = {}
    for item in prepared:
        key = item["target"].group
        if key:
            groups.setdefault(key, []).append(item)

    for items in groups.values():
        if len(items) < 2:
            continue
        for index in range(n_shops):
            alloc = sum(item["rows"][index]["alloc"] for item in items)
            on_hand = sum(
                item["rows"][index]["stock"] + item["rows"][index]["pending"]
                for item in items
            )
            need = max(alloc - on_hand, 0)
            wants = [item["candidates"][index] for item in items]
            if sum(wants) <= need:
                continue
            for item, qty in zip(items, cap_to_available(wants, need, 1)):
                item["candidates"][index] = qty
                row = item["rows"][index]
                # The need is what the warehouse has to send, so it follows the
                # cap. Leaving it at the member's own gap would report a
                # shortage for units the group already has in that shop.
                # A blocked row keeps its need: nothing was sent, but for a
                # reason the report shows separately.
                if not row["blocked"]:
                    row["need"] = qty


def _apply_group_coverage(
    skus: Sequence[SkuSummary], targets: Sequence[SkuTarget]
) -> List[SkuSummary]:
    """Give every member the group's verdict, and report its gap once.

    A shop can only receive the group's target once, so the shortfall belongs to
    the group. It is written on the row that owns the number in the source file
    (``group == sku``) and left at zero on the other members, which keeps
    per-SKU sums — totals, the usage log — from counting it twice.
    """
    groups: Dict[str, List[SkuTarget]] = {}
    for target in targets:
        if target.group:
            groups.setdefault(target.group, []).append(target)
    if not groups:
        return list(skus)

    by_sku = {s.sku: s for s in skus}
    position = {s.sku: i for i, s in enumerate(skus)}
    out = list(skus)

    for key, members in groups.items():
        if len(members) < 2:
            continue
        rows = [by_sku[m.sku] for m in members if m.sku in by_sku]
        if len(rows) < 2:
            continue
        target_qty = sum(m.target_qty for m in members)
        covered = sum(r.total_stock + r.total_pending + r.warehouse_qty for r in rows)
        gap = max(target_qty - covered, 0)
        status = COVERAGE_SUFFICIENT if gap == 0 else COVERAGE_SHORT
        lead = next((r.sku for r in rows if r.sku == key), rows[0].sku)
        for row in rows:
            out[position[row.sku]] = replace(
                row,
                coverage_status=status,
                shortfall_qty=gap if row.sku == lead else 0,
            )
    return out


def compute_scenario(
    scenario: str,
    shops: Sequence[str],
    shares: Sequence[float],
    targets: Iterable[SkuTarget],
    stock_by_sku: Dict[str, Dict[str, float]],
    share_total: float = 1.0,
    warnings: Optional[List[str]] = None,
    rp_by_sku: Optional[Dict[str, Dict[str, str]]] = None,
    rp_gate_shops: Sequence[str] = (),
    sales_by_sku: Optional[Dict[str, Dict[str, ShopSales]]] = None,
    promo: Optional[PromoWindow] = None,
    as_of: Optional[date] = None,
    warehouse_by_sku: Optional[Dict[str, int]] = None,
    warehouse_pending_by_sku: Optional[Dict[str, int]] = None,
    moq_by_sku: Optional[Dict[str, int]] = None,
    supply_by_sku: Optional[Dict[str, Dict[str, str]]] = None,
    vendor_sources: Sequence[str] = DEFAULT_VENDOR_SOURCES,
) -> ScenarioResult:
    """Compute allocations and dispatch for one share set.

    ``promo`` scales the SKU target down to the dispatch window before it is
    split by ``shares``. ``sales_by_sku`` supplies the sales history used for the
    promotion-period comparison; pass ``None`` when the file has no sales
    columns, and the comparison is simply left blank.

    ``rp_gate_shops`` lists the shops where dispatch requires ``RP Type == RF``.
    ``warehouse_by_sku`` is the warehouse net stock that actually limits how much
    can be sent. ``moq_by_sku`` comes from the stock file (SKU × shop): it is
    what rounds each shop's need up, and the target file has no say in it.
    ``supply_by_sku`` maps SKU -> shop -> ``Supply source`` code; only
    ``vendor_sources`` codes are supplied by PO instead of from D001, and those
    rows are allocated against the full promotion target — a PO covers the whole
    period, the warehouse only the dispatch window.
    """
    warn = list(warnings or [])
    lines: List[Line] = []
    skus: List[SkuSummary] = []
    targets = list(targets)

    gate = {str(s).strip().upper() for s in rp_gate_shops if str(s).strip()}
    rp_map = rp_by_sku or {}
    sales_map = sales_by_sku or {}
    warehouse = warehouse_by_sku or {}
    warehouse_pending = warehouse_pending_by_sku or {}
    supply_map = supply_by_sku or {}
    vendors = {str(v).strip().upper() for v in vendor_sources if str(v).strip()}
    known_wh = {
        str(c).strip().upper() for c in KNOWN_WAREHOUSE_SOURCES if str(c).strip()
    }
    sales_known = sales_by_sku is not None and as_of is not None
    promo_days = promo.days if promo else 0

    blocked_skus: set = set()
    blocked_units = 0
    short_skus: set = set()
    short_units = 0
    missing_warehouse: List[str] = []
    vendor_shops: set = set()
    four_shops: set = set()
    po_units = 0
    odd_sources: set = set()

    known = set(shops)
    unknown_stock = sorted(
        {
            shop
            for per_shop in stock_by_sku.values()
            for shop in per_shop
            if shop not in known
        }
    )
    if unknown_stock:
        warn.append(
            "以下店舖在庫存檔中出現但未在店舖主檔啟用，已忽略："
            + "、".join(unknown_stock[:8])
            + ("…" if len(unknown_stock) > 8 else "")
        )

    prepared: List[dict] = []
    for target in targets:
        per_shop_stock = stock_by_sku.get(target.sku, {})
        per_shop_rp = rp_map.get(target.sku, {})
        per_shop_sales = sales_map.get(target.sku, {})
        moq = sku_moq(moq_by_sku, target.sku)

        # Only the first few days of the target are sent out from D001, so the
        # promotion target is scaled down to the dispatch window before it is
        # split across shops. 行貨 is the exception: the buyer raises one PO for
        # the whole promotion, so a vendor shop is allocated its share of the
        # full target however few days the warehouse dispatch covers.
        dispatch_target = (
            promo.dispatch_target(target.target_qty)
            if promo is not None
            else target.target_qty
        )
        window_allocs = apportion(dispatch_target, shares)
        full_allocs = apportion(target.target_qty, shares)

        available = (
            int(round(warehouse[target.sku]))
            if warehouse_by_sku is not None and target.sku in warehouse
            else None
        )

        per_shop_supply = supply_map.get(target.sku, {})
        rows: List[dict] = []
        for shop, share, window_alloc, full_alloc in zip(
            shops, shares, window_allocs, full_allocs
        ):
            stock = int(round(float(per_shop_stock.get(shop, 0) or 0)))
            sales = per_shop_sales.get(shop) or ShopSales()
            pending = int(sales.pending_qty or 0)
            on_hand = stock + pending

            code = supply_code(per_shop_supply.get(shop, ""))
            mode = supply_mode(code, vendors)
            vendor = mode == SUPPLY_VENDOR
            # The route decides which target this shop is measured against.
            alloc = full_alloc if vendor else window_alloc

            gap = alloc - on_hand
            need = round_up_to(gap, moq) if gap > 0 else 0

            rate = daily_sales_rate(sales, as_of)
            expected = int(round(rate * promo_days)) if sales_known else 0

            rp_type = str(per_shop_rp.get(shop, "") or "").strip().upper()
            gated = bool(gate) and shop.upper() in gate and rp_type != RP_DISPATCHABLE
            blocked = gated and need > 0

            if vendor:
                vendor_shops.add(shop)
                if code == "4":
                    four_shops.add(shop)
            elif code and code.upper() not in known_wh and code.upper() not in vendors:
                odd_sources.add(code)

            rows.append(
                {
                    "shop": shop,
                    "share": share,
                    "alloc": alloc,
                    "alloc_full": full_alloc,
                    "stock": stock,
                    "pending": pending,
                    "gap": gap,
                    "need": need,
                    "rate": rate,
                    "expected": expected,
                    "rp_type": rp_type,
                    "blocked": blocked,
                    "surplus": max(stock - alloc, 0),
                    "supply_source": code,
                    "vendor": vendor,
                }
            )

        # A SKU carried only by vendor shops never draws on the warehouse, so a
        # missing D001 row is not worth reporting for it.
        if (
            warehouse_by_sku is not None
            and available is None
            and any(r["need"] > 0 and not r["vendor"] for r in rows)
        ):
            missing_warehouse.append(target.sku)

        prepared.append(
            {
                "target": target,
                "rows": rows,
                "allocs": [r["alloc"] for r in rows],
                "dispatch_target": dispatch_target,
                "moq": moq,
                "available": available,
                "candidates": [0 if r["blocked"] else r["need"] for r in rows],
            }
        )

    # A shared target is filled from the group's own stock, so the members'
    # needs overlap; see _cap_group_needs.
    _cap_group_needs(prepared, len(shops))

    for item in prepared:
        target = item["target"]
        rows = item["rows"]
        allocs = item["allocs"]
        dispatch_target = item["dispatch_target"]
        moq = item["moq"]
        available = item["available"]
        candidates = item["candidates"]

        # Only warehouse-sourced shops draw on D001. A vendor shop's need is
        # filled by a PO, so letting it compete for the warehouse would starve
        # the shops that can only be served from there.
        from_warehouse = [0 if row["vendor"] else qty for row, qty in zip(rows, candidates)]
        if available is not None and sum(from_warehouse) > available:
            capped_wh = cap_to_available(from_warehouse, available, moq)
        else:
            capped_wh = from_warehouse
        # A group cap hands out single units rather than MOQ steps, so what is
        # left gets rounded once here. For a standalone SKU this is a no-op.
        capped = [
            round_up_to(qty if row["vendor"] else wh, moq)
            for row, qty, wh in zip(rows, candidates, capped_wh)
        ]

        # 可調出 is a rescue figure, not a stock report: it only means something
        # when D001 could not send what the shops asked for. With enough stock at
        # the warehouse the goods come from there and a shop holding more than
        # its allocation simply keeps it. 行貨 is left out either way — a transfer
        # of vendor stock is arranged by the buyer in another system, so a number
        # here would only duplicate work done elsewhere.
        pulling = _warehouse_short(rows, capped) > 0
        for row in rows:
            if row["vendor"] or not pulling:
                row["surplus"] = 0

        total_stock = 0
        total_pending = 0
        total_dispatch = 0
        total_need = 0
        total_blocked = 0
        total_expected = 0
        total_rate = 0.0
        total_po = 0
        total_po_target = 0
        total_short = 0
        surplus = 0
        needing = 0

        for row, dispatch in zip(rows, capped):
            # Rounding up to the MOQ can ask for a few units more than the gap.
            # Report the ask, not the gap, or the detail sheet would show a
            # dispatch larger than the need it answers.
            if dispatch > row["need"] and not row["blocked"]:
                row["need"] = dispatch
            # A shop with no share, no stock, nothing in transit and no sales is
            # pure noise in the detail sheet — every column would be zero.
            # Keep the row when sales exist, because those units are real.
            if (
                row["share"] <= 0
                and row["stock"] == 0
                and row["pending"] == 0
                and dispatch == 0
                and row["rate"] <= 0
            ):
                continue

            po = dispatch if row["vendor"] else 0
            if row["vendor"]:
                # What the buyer is being asked for over the whole promotion,
                # not just the days the warehouse covers.
                total_po_target += row["alloc"]

            total_stock += row["stock"]
            total_pending += row["pending"]
            total_dispatch += dispatch
            total_expected += row["expected"]
            total_rate += row["rate"]
            surplus += row["surplus"]
            if dispatch > 0:
                needing += 1

            if row["blocked"]:
                # A gated shop's need is neither dispatched nor a warehouse
                # shortage — the goods are there, the rule forbids sending them.
                blocked_skus.add(target.sku)
                blocked_units += row["need"]
                total_blocked += row["need"]
                short = 0
            else:
                total_need += row["need"]
                # 行貨 is ordered on a PO rather than pulled from D001, so its
                # units are a buyer worklist, not warehouse stock in demand.
                if row["vendor"]:
                    total_po += po
                    po_units += po
                # Rounding a capped quantity up to the MOQ can land a few units
                # above the need; that is an over-send, never a shortage.
                short = max(row["need"] - dispatch, 0)
                if short > 0:
                    short_skus.add(target.sku)
                    short_units += short
                total_short += short

            lines.append(
                Line(
                    sku=target.sku,
                    shop_code=row["shop"],
                    share_pct=row["share"],
                    alloc_qty=row["alloc"],
                    stock_qty=row["stock"],
                    gap_qty=row["gap"],
                    dispatch_qty=dispatch,
                    surplus_qty=row["surplus"],
                    rp_type=row["rp_type"],
                    blocked=row["blocked"],
                    blocked_qty=row["need"] if row["blocked"] else 0,
                    pending_qty=row["pending"],
                    daily_rate=round(row["rate"], 4),
                    expected_sales=row["expected"],
                    need_qty=row["need"],
                    short_qty=short,
                    supply_source=row["supply_source"],
                    supply_mode=(
                        SUPPLY_VENDOR if row["vendor"] else SUPPLY_WAREHOUSE
                    ),
                    po_qty=po,
                    alloc_full_period=row["vendor"],
                    alloc_full_qty=row["alloc_full"],
                )
            )

        covered = total_stock + total_pending + (available or 0)
        coverage_gap = max(target.target_qty - covered, 0)

        skus.append(
            SkuSummary(
                sku=target.sku,
                description=target.description,
                target_qty=target.target_qty,
                total_alloc=sum(allocs),
                total_stock=total_stock,
                total_dispatch=total_dispatch,
                surplus_qty=surplus,
                shortfall_qty=coverage_gap,
                n_shops_needing=needing,
                coverage_status=(
                    COVERAGE_SUFFICIENT if coverage_gap == 0 else COVERAGE_SHORT
                ),
                moq=moq,
                dispatch_target=dispatch_target,
                total_pending=total_pending,
                warehouse_qty=available or 0,
                warehouse_pending=int(warehouse_pending.get(target.sku, 0) or 0),
                daily_rate=round(total_rate, 4),
                expected_sales=total_expected if sales_known else 0,
                gap_target=(
                    target.target_qty - total_expected if sales_known else 0
                ),
                need_qty=total_need,
                short_qty=total_short,
                blocked_qty=total_blocked,
                gap_supply=target.target_qty - (total_stock + total_pending + total_dispatch),
                group=target.group,
                po_qty=total_po,
                po_target=total_po_target,
            )
        )

    # Judged per SKU, a shared target would report a shortage for whichever
    # pack happens to be scarce on its own. The group is what was planned, so
    # the group is what answers 夠貨.
    skus = _apply_group_coverage(skus, targets)

    if blocked_skus:
        warn.append(
            "、".join(sorted(gate))
            + f" 依 RP Type 規則不派貨：{len(blocked_skus)} 個 SKU，共少派 "
            + f"{blocked_units:,} 件（只有 RP Type = {RP_DISPATCHABLE} 才派，"
            + "ND 或未提供一律 0 件）"
        )
    if po_units:
        short_window = (
            promo is not None and promo.effective_dispatch_days < promo.days
        )
        warn.append(
            "Supply Source "
            + "／".join(sorted(vendors))
            + f" 屬行貨：{len(vendor_shops)} 間店舖共 {po_units:,} 件需由 Buyer 開 PO 落單，"
            + "已不佔 D001 倉存、不計入倉存缺貨"
            + (
                f"；行貨需求按全期 Target（{promo.days} 日）計算，"
                f"不受派貨日數（{promo.effective_dispatch_days} 日）限制"
                if short_window
                else ""
            )
            + "（"
            + "、".join(sorted(vendor_shops)[:6])
            + ("…" if len(vendor_shops) > 6 else "")
            + "）"
        )
        if four_shops:
            # The one route that looks like a warehouse movement but is not:
            # say it only when a line actually uses 4, so the note does not turn
            # up on every run just because 4 is in the configured list.
            warn.append(
                f"Supply Source 4 用於 {len(four_shops)} 間店舖：貨會經 D001 過數再到店舖"
                "（系統流程），但貨源屬行貨 — 由 Buyer 開 PO，"
                "不佔 D001 倉存、不計入倉存缺貨"
            )
    if odd_sources:
        warn.append(
            "Supply Source 出現未設定的代碼（"
            + "、".join(sorted(odd_sources))
            + "），已當作倉貨（由 D001 出 STO）處理"
        )
    if short_units:
        warn.append(
            f"貨倉存貨不足：{len(short_skus)} 個 SKU 共缺 {short_units:,} 件，"
            "已按需求比例分配可派數量"
        )
    if missing_warehouse:
        warn.append(
            f"有 {len(missing_warehouse)} 個 SKU 在貨倉沒有存貨紀錄，"
            "未套用倉存上限（例：" + missing_warehouse[0] + "）"
        )

    return ScenarioResult(
        scenario=scenario,
        lines=lines,
        skus=skus,
        share_total=share_total,
        warnings=warn,
    )


def compute_all_scenarios(
    scenarios: Dict[str, Dict[str, float]],
    shops: Sequence[str],
    targets: Sequence[SkuTarget],
    stock_by_sku: Dict[str, Dict[str, float]],
    rp_by_sku: Optional[Dict[str, Dict[str, str]]] = None,
    rp_gate_shops: Sequence[str] = (),
    sales_by_sku: Optional[Dict[str, Dict[str, ShopSales]]] = None,
    promo: Optional[PromoWindow] = None,
    as_of: Optional[date] = None,
    warehouse_by_sku: Optional[Dict[str, int]] = None,
    warehouse_pending_by_sku: Optional[Dict[str, int]] = None,
    moq_by_sku: Optional[Dict[str, int]] = None,
    supply_by_sku: Optional[Dict[str, Dict[str, str]]] = None,
    vendor_sources: Sequence[str] = DEFAULT_VENDOR_SOURCES,
) -> Dict[str, ScenarioResult]:
    """Compute every scenario from {scenario: {shop: pct}}."""
    out: Dict[str, ScenarioResult] = {}
    for name in sorted(scenarios):
        shares, total, warnings = normalise_shares(shops, scenarios[name])
        out[name] = compute_scenario(
            name,
            shops,
            shares,
            targets,
            stock_by_sku,
            total,
            warnings,
            rp_by_sku=rp_by_sku,
            rp_gate_shops=rp_gate_shops,
            sales_by_sku=sales_by_sku,
            promo=promo,
            as_of=as_of,
            warehouse_by_sku=warehouse_by_sku,
            warehouse_pending_by_sku=warehouse_pending_by_sku,
            moq_by_sku=moq_by_sku,
            supply_by_sku=supply_by_sku,
            vendor_sources=vendor_sources,
        )
    return out


_GROUP_SUM_FIELDS = (
    "target_qty", "total_alloc", "total_stock", "total_dispatch", "surplus_qty",
    "shortfall_qty", "n_shops_needing", "dispatch_target", "total_pending",
    "warehouse_qty", "warehouse_pending", "expected_sales", "gap_target",
    "need_qty", "short_qty", "blocked_qty", "gap_supply", "po_qty", "po_target",
)


def _merge_group(members: Sequence[SkuSummary]) -> SkuSummary:
    """One summary for a shared target, so the table reads 4,000 — not 3,200.

    The members' rows are what the dispatch needs; the buyer's question is about
    the group. Adding the numbers back up is the only honest way to show both.
    """
    if len(members) == 1:
        return members[0]
    first = members[0]
    values = {
        name: sum(getattr(member, name) for member in members)
        for name in _GROUP_SUM_FIELDS
    }
    label = " + ".join(member.sku for member in members[:2])
    if len(members) > 2:
        label += f" 等 {len(members)} 個"
    return SkuSummary(
        sku=label,
        description=first.description,
        moq=max(member.moq for member in members),
        daily_rate=round(sum(member.daily_rate for member in members), 4),
        coverage_status=first.coverage_status,
        group=first.group,
        **values,
    )


def compare_scenarios(results: Dict[str, ScenarioResult]) -> List[dict]:
    """Rows for the A/B/C comparison table: one per target, groups included."""
    names = sorted(results)
    if not names:
        return []

    rows: Dict[str, dict] = {}
    order: List[str] = []

    for name in names:
        grouped: Dict[str, List[SkuSummary]] = {}
        for summary in results[name].skus:
            key = summary.group or summary.sku
            if key not in grouped:
                grouped[key] = []
                if key not in rows:
                    order.append(key)
            grouped[key].append(summary)

        for key, members in grouped.items():
            merged = _merge_group(members)
            row = rows.get(key)
            if row is None:
                row = {
                    "sku": merged.sku,
                    "description": merged.description,
                    "target_qty": merged.target_qty,
                    "total_stock": merged.total_stock,
                    "shortfall_qty": merged.shortfall_qty,
                    "group_size": len(members) if len(members) > 1 else 0,
                    "scenarios": {},
                }
                rows[key] = row
            row["scenarios"][name] = merged

    out = [rows[key] for key in order]
    out.sort(key=lambda r: (-r["shortfall_qty"], r["sku"]))
    return out
