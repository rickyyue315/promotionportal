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
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

COVERAGE_SUFFICIENT = "sufficient"  # total shop stock covers the target
COVERAGE_SHORT = "short"  # not enough goods anywhere

RP_DISPATCHABLE = "RF"  # the only RP Type that unlocks dispatch at a gated shop


@dataclass(frozen=True)
class SkuTarget:
    sku: str
    target_qty: int
    description: str = ""


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

    @property
    def on_hand(self) -> int:
        """Real stock plus what is already on its way to the shop."""
        return self.stock_qty + self.pending_qty


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

    @property
    def enough(self) -> bool:
        return self.coverage_status == COVERAGE_SUFFICIENT


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
        return sum(1 for s in self.skus if s.coverage_status == COVERAGE_SHORT)

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


def dispatch_batches(promo: PromoWindow) -> List[Tuple[int, date, date]]:
    """Split the promotion into consecutive dispatch runs.

    Each run covers ``dispatch_days`` days of selling (the last one may be
    shorter). These are the dates a shop is expected to be stocked for; the
    warehouse still needs its own lead time, which we do not assume.
    """
    if promo.days <= 0:
        return []
    step = max(promo.effective_dispatch_days, 1)
    out: List[Tuple[int, date, date]] = []
    day = promo.start
    index = 1
    while day <= promo.end:
        end = min(day + timedelta(days=step - 1), promo.end)
        out.append((index, day, end))
        day = end + timedelta(days=1)
        index += 1
    return out


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
    """
    warn = list(warnings or [])
    lines: List[Line] = []
    skus: List[SkuSummary] = []

    gate = {str(s).strip().upper() for s in rp_gate_shops if str(s).strip()}
    rp_map = rp_by_sku or {}
    sales_map = sales_by_sku or {}
    warehouse = warehouse_by_sku or {}
    warehouse_pending = warehouse_pending_by_sku or {}
    sales_known = sales_by_sku is not None and as_of is not None
    promo_days = promo.days if promo else 0

    blocked_skus: set = set()
    blocked_units = 0
    short_skus: set = set()
    short_units = 0
    missing_warehouse: List[str] = []

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

    for target in targets:
        per_shop_stock = stock_by_sku.get(target.sku, {})
        per_shop_rp = rp_map.get(target.sku, {})
        per_shop_sales = sales_map.get(target.sku, {})
        moq = sku_moq(moq_by_sku, target.sku)

        # Only the first few days of the target are sent out; the rest is
        # replenished later, so it must not inflate this round of dispatch.
        dispatch_target = (
            promo.dispatch_target(target.target_qty)
            if promo is not None
            else target.target_qty
        )
        allocs = apportion(dispatch_target, shares)

        available = (
            int(round(warehouse[target.sku]))
            if warehouse_by_sku is not None and target.sku in warehouse
            else None
        )
        if warehouse_by_sku is not None and available is None:
            missing_warehouse.append(target.sku)

        rows: List[dict] = []
        for shop, share, alloc in zip(shops, shares, allocs):
            stock = int(round(float(per_shop_stock.get(shop, 0) or 0)))
            sales = per_shop_sales.get(shop) or ShopSales()
            pending = int(sales.pending_qty or 0)
            on_hand = stock + pending

            gap = alloc - on_hand
            need = round_up_to(gap, moq) if gap > 0 else 0

            rate = daily_sales_rate(sales, as_of)
            expected = int(round(rate * promo_days)) if sales_known else 0

            rp_type = str(per_shop_rp.get(shop, "") or "").strip().upper()
            gated = bool(gate) and shop.upper() in gate and rp_type != RP_DISPATCHABLE
            blocked = gated and need > 0

            rows.append(
                {
                    "shop": shop,
                    "share": share,
                    "alloc": alloc,
                    "stock": stock,
                    "pending": pending,
                    "gap": gap,
                    "need": need,
                    "rate": rate,
                    "expected": expected,
                    "rp_type": rp_type,
                    "blocked": blocked,
                    "surplus": max(stock - alloc, 0),
                }
            )

        candidates = [0 if r["blocked"] else r["need"] for r in rows]
        if available is not None and sum(candidates) > available:
            capped = cap_to_available(candidates, available, moq)
        else:
            capped = candidates

        total_stock = 0
        total_pending = 0
        total_dispatch = 0
        total_need = 0
        total_blocked = 0
        total_expected = 0
        total_rate = 0.0
        surplus = 0
        needing = 0

        for row, dispatch in zip(rows, capped):
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
                short = row["need"] - dispatch
                if short > 0:
                    short_skus.add(target.sku)
                    short_units += short

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
                short_qty=total_need - total_dispatch,
                blocked_qty=total_blocked,
                gap_supply=target.target_qty - (total_stock + total_pending + total_dispatch),
            )
        )

    if blocked_skus:
        warn.append(
            "、".join(sorted(gate))
            + f" 依 RP Type 規則不派貨：{len(blocked_skus)} 個 SKU，共少派 "
            + f"{blocked_units:,} 件（只有 RP Type = {RP_DISPATCHABLE} 才派，"
            + "ND 或未提供一律 0 件）"
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
        )
    return out


def compare_scenarios(results: Dict[str, ScenarioResult]) -> List[dict]:
    """Rows for the A/B/C comparison table: one row per SKU."""
    names = sorted(results)
    if not names:
        return []

    by_sku: Dict[str, dict] = {}
    for name in names:
        for s in results[name].skus:
            row = by_sku.setdefault(
                s.sku,
                {
                    "sku": s.sku,
                    "description": s.description,
                    "target_qty": s.target_qty,
                    "total_stock": s.total_stock,
                    "shortfall_qty": s.shortfall_qty,
                    "scenarios": {},
                },
            )
            row["scenarios"][name] = s

    rows = list(by_sku.values())
    rows.sort(key=lambda r: (-r["shortfall_qty"], r["sku"]))
    return rows
