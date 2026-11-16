"""Where a calculation lives: in memory, for a while, nowhere else.

The portal persists one thing — the monthly shop shares. A calculation is a
one-off: it exists so the person who just ran it can read the result, drill into
shops and export the Excel they need. Nothing is written to disk.

The practical effects, all deliberate:

* a redeploy builds a new container, so any open result is gone;
* results expire after ``TTL_SECONDS`` of wall-clock time;
* only the most recent ``MAX_RUNS`` are kept, so a long session cannot grow the
  process without bound.

What survives is a single row in ``usage_log``: when it was run, by whom, for
which month, and how many SKUs it carried.
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from . import calc

TTL_SECONDS = 2 * 60 * 60
MAX_RUNS = 6


@dataclass
class Run:
    """One calculation, kept only for the person who just ran it."""

    id: str
    period: str
    created_at: str
    created_by: str
    params: Dict[str, Any]
    results: Dict[str, calc.ScenarioResult] = field(default_factory=dict)

    @property
    def n_sku(self) -> int:
        return len({s.sku for res in self.results.values() for s in res.skus})

    @property
    def n_short(self) -> int:
        return len(
            {
                s.sku
                for res in self.results.values()
                for s in res.skus
                if s.coverage_status == calc.COVERAGE_SHORT
            }
        )

    @property
    def scenarios(self) -> list:
        return [sc for sc in ("A", "B", "C") if sc in self.results]


# token -> (stored_at, run). Ordered so the oldest entry is evicted first.
_STORE: "OrderedDict[str, tuple[float, Run]]" = OrderedDict()


def put(
    period: str,
    created_at: str,
    created_by: str,
    params: Dict[str, Any],
    results: Dict[str, calc.ScenarioResult],
) -> Run:
    """Store a finished calculation and hand back the object to redirect to."""
    _prune()
    run = Run(
        id=secrets.token_urlsafe(6),
        period=period,
        created_at=created_at,
        created_by=created_by,
        params=params,
        results=results,
    )
    _STORE[run.id] = (time.time(), run)
    while len(_STORE) > MAX_RUNS:
        _STORE.popitem(last=False)
    return run


def get(token: str) -> Optional[Run]:
    """The run behind a URL, or None when it expired or never existed."""
    _prune()
    entry = _STORE.get(token)
    return entry[1] if entry else None


def clear() -> None:
    """Drop everything — used by tests, and by anything that must not leak."""
    _STORE.clear()


def _prune() -> None:
    cutoff = time.time() - TTL_SECONDS
    for token, (stored_at, _run) in list(_STORE.items()):
        if stored_at < cutoff:
            _STORE.pop(token, None)
