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

For each SKU:

    total_stock = Σ stock
    shortfall   = max(target - total_stock, 0)   -> what must be bought
    surplus     = Σ max(stock - alloc, 0)        -> what can be pulled from other shops

    enough = total_stock >= target

Note that "enough" can be true while dispatch is non-zero: the goods exist, they
are just in the wrong shops. That distinction is the whole point of the report,
so it is modelled explicitly as ``coverage_status``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

COVERAGE_SUFFICIENT = "sufficient"  # total shop stock covers the target
COVERAGE_SHORT = "short"  # not enough goods anywhere


@dataclass(frozen=True)
class SkuTarget:
    sku: str
    target_qty: int
    moq: int = 1
    description: str = ""


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


# ---------------------------------------------------------------------------
# scenario
# ---------------------------------------------------------------------------


def compute_scenario(
    scenario: str,
    shops: Sequence[str],
    shares: Sequence[float],
    targets: Iterable[SkuTarget],
    stock_by_sku: Dict[str, Dict[str, float]],
    share_total: float = 1.0,
    warnings: Optional[List[str]] = None,
) -> ScenarioResult:
    """Compute allocations and dispatch for one share set."""
    warn = list(warnings or [])
    lines: List[Line] = []
    skus: List[SkuSummary] = []

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
        allocs = apportion(target.target_qty, shares)

        total_stock = 0
        total_dispatch = 0
        surplus = 0
        needing = 0

        for shop, share, alloc in zip(shops, shares, allocs):
            stock = int(round(float(per_shop_stock.get(shop, 0) or 0)))
            total_stock += stock

            gap = alloc - stock
            dispatch = round_up_to(gap, target.moq) if gap > 0 else 0
            over = max(stock - alloc, 0)

            total_dispatch += dispatch
            surplus += over
            if dispatch > 0:
                needing += 1

            lines.append(
                Line(
                    sku=target.sku,
                    shop_code=shop,
                    share_pct=share,
                    alloc_qty=alloc,
                    stock_qty=stock,
                    gap_qty=gap,
                    dispatch_qty=dispatch,
                    surplus_qty=over,
                )
            )

        shortfall = max(target.target_qty - total_stock, 0)
        skus.append(
            SkuSummary(
                sku=target.sku,
                description=target.description,
                target_qty=target.target_qty,
                total_alloc=sum(allocs),
                total_stock=total_stock,
                total_dispatch=total_dispatch,
                surplus_qty=surplus,
                shortfall_qty=shortfall,
                n_shops_needing=needing,
                coverage_status=(
                    COVERAGE_SUFFICIENT if shortfall == 0 else COVERAGE_SHORT
                ),
                moq=int(round(target.moq or 1)),
            )
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
) -> Dict[str, ScenarioResult]:
    """Compute every scenario from {scenario: {shop: pct}}."""
    out: Dict[str, ScenarioResult] = {}
    for name in sorted(scenarios):
        shares, total, warnings = normalise_shares(shops, scenarios[name])
        out[name] = compute_scenario(
            name, shops, shares, targets, stock_by_sku, total, warnings
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
