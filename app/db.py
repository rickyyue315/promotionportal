"""SQLite data layer.

One file on disk, no server. On Railway the file lives on a mounted volume so it
survives redeploys — set ``DATA_DIR`` to the mount path.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCENARIOS = ("A", "B", "C")

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "portal.db"

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS shop (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL DEFAULT '',
    region     TEXT NOT NULL DEFAULT 'HK',
    active     INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS share_set (
    id         INTEGER PRIMARY KEY,
    period     TEXT NOT NULL,
    scenario   TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    updated_at TEXT,
    updated_by TEXT,
    UNIQUE (period, scenario)
);

CREATE TABLE IF NOT EXISTS share_entry (
    id           INTEGER PRIMARY KEY,
    share_set_id INTEGER NOT NULL REFERENCES share_set(id) ON DELETE CASCADE,
    shop_code    TEXT NOT NULL,
    pct          REAL NOT NULL DEFAULT 0,
    UNIQUE (share_set_id, shop_code)
);

CREATE TABLE IF NOT EXISTS upload (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    period      TEXT NOT NULL,
    filename    TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL,
    uploaded_by TEXT NOT NULL DEFAULT '',
    n_rows      INTEGER NOT NULL DEFAULT 0,
    meta_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sku_target (
    id          INTEGER PRIMARY KEY,
    upload_id   INTEGER NOT NULL REFERENCES upload(id) ON DELETE CASCADE,
    sku         TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    target_qty  INTEGER NOT NULL,
    moq         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_sku_target_upload ON sku_target(upload_id);

CREATE TABLE IF NOT EXISTS shop_stock (
    id        INTEGER PRIMARY KEY,
    upload_id INTEGER NOT NULL REFERENCES upload(id) ON DELETE CASCADE,
    sku       TEXT NOT NULL,
    shop_code TEXT NOT NULL,
    qty       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_shop_stock_upload ON shop_stock(upload_id);
CREATE INDEX IF NOT EXISTS ix_shop_stock_sku ON shop_stock(upload_id, sku);

CREATE TABLE IF NOT EXISTS run (
    id                INTEGER PRIMARY KEY,
    period            TEXT NOT NULL,
    target_upload_id  INTEGER REFERENCES upload(id),
    stock_upload_id   INTEGER REFERENCES upload(id),
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL DEFAULT '',
    params_json       TEXT NOT NULL DEFAULT '{}',
    n_sku             INTEGER NOT NULL DEFAULT 0,
    n_short           INTEGER NOT NULL DEFAULT 0,
    note              TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS run_sku (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    scenario       TEXT NOT NULL,
    sku            TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    target_qty     INTEGER NOT NULL,
    total_alloc    INTEGER NOT NULL,
    total_stock    INTEGER NOT NULL,
    total_dispatch INTEGER NOT NULL,
    surplus_qty    INTEGER NOT NULL,
    shortfall_qty  INTEGER NOT NULL,
    n_shops_needing INTEGER NOT NULL,
    coverage_status TEXT NOT NULL,
    moq            INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_run_sku ON run_sku(run_id, scenario);

CREATE TABLE IF NOT EXISTS run_line (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    scenario     TEXT NOT NULL,
    sku          TEXT NOT NULL,
    shop_code    TEXT NOT NULL,
    share_pct    REAL NOT NULL,
    alloc_qty    INTEGER NOT NULL,
    stock_qty    INTEGER NOT NULL,
    gap_qty      INTEGER NOT NULL,
    dispatch_qty INTEGER NOT NULL,
    surplus_qty  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_run_line_sku ON run_line(run_id, scenario, sku);
CREATE INDEX IF NOT EXISTS ix_run_line_shop ON run_line(run_id, scenario, shop_code);
"""


@contextmanager
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def current_period() -> str:
    return datetime.now().strftime("%Y-%m")


# ---------------------------------------------------------------------------
# shops
# ---------------------------------------------------------------------------


def list_shops(active_only: bool = False) -> List[sqlite3.Row]:
    sql = "SELECT * FROM shop"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort_order, code"
    with db() as conn:
        return conn.execute(sql).fetchall()


def upsert_shop(code: str, name: str = "", region: str = "HK", active: int = 1) -> int:
    code = code.strip().upper()
    with db() as conn:
        cur = conn.execute("SELECT id FROM shop WHERE code = ?", (code,))
        row = cur.fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO shop (code, name, region, active, sort_order) "
            "VALUES (?, ?, ?, ?, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM shop))",
            (code, name, region, active),
        )
        return int(cur.lastrowid)


def update_shop(shop_id: int, name: str, region: str, active: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE shop SET name = ?, region = ?, active = ? WHERE id = ?",
            (name, region.upper()[:2], 1 if active else 0, shop_id),
        )


def delete_shop(shop_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM shop WHERE id = ?", (shop_id,))


def active_shop_codes() -> List[str]:
    return [r["code"] for r in list_shops(active_only=True)]


# ---------------------------------------------------------------------------
# share sets
# ---------------------------------------------------------------------------


def get_share_set(period: str, scenario: str) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM share_set WHERE period = ? AND scenario = ?",
            (period, scenario),
        ).fetchone()


def get_shares(period: str, scenario: str) -> Dict[str, float]:
    with db() as conn:
        rows = conn.execute(
            "SELECT e.shop_code, e.pct FROM share_entry e "
            "JOIN share_set s ON s.id = e.share_set_id "
            "WHERE s.period = ? AND s.scenario = ?",
            (period, scenario),
        ).fetchall()
    return {r["shop_code"]: float(r["pct"]) for r in rows}


def get_all_shares(period: str) -> Dict[str, Dict[str, float]]:
    return {s: get_shares(period, s) for s in SCENARIOS}


def save_shares(
    period: str,
    scenario: str,
    pct: Dict[str, float],
    user: str = "",
    note: str = "",
) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO share_set (period, scenario, note, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(period, scenario) DO UPDATE SET "
            "note = excluded.note, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by",
            (period, scenario, note, now(), user),
        )
        row = conn.execute(
            "SELECT id FROM share_set WHERE period = ? AND scenario = ?",
            (period, scenario),
        ).fetchone()
        set_id = int(row["id"])
        conn.execute("DELETE FROM share_entry WHERE share_set_id = ?", (set_id,))
        conn.executemany(
            "INSERT INTO share_entry (share_set_id, shop_code, pct) VALUES (?, ?, ?)",
            [(set_id, code, float(v)) for code, v in pct.items() if float(v or 0) != 0],
        )


def save_shares_bulk(
    period: str, by_scenario: Dict[str, Dict[str, float]], user: str = ""
) -> None:
    for scenario, pct in by_scenario.items():
        if scenario in SCENARIOS:
            save_shares(period, scenario, pct, user)


def list_periods() -> List[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT period FROM share_set "
            "UNION SELECT DISTINCT period FROM run ORDER BY 1 DESC"
        ).fetchall()
    return [r["period"] for r in rows]


def share_status(period: str) -> Dict[str, Dict[str, Any]]:
    """Per-scenario readiness for a month: total %, shops covered, updated at."""
    shops = active_shop_codes()
    out: Dict[str, Dict[str, Any]] = {}
    for scenario in SCENARIOS:
        pct = get_shares(period, scenario)
        meta = get_share_set(period, scenario)
        covered = sum(1 for c in shops if pct.get(c, 0) > 0)
        out[scenario] = {
            "total": sum(pct.values()),
            "n_shops": covered,
            "n_expected": len(shops),
            "updated_at": meta["updated_at"] if meta else None,
            "updated_by": meta["updated_by"] if meta else "",
            "ready": bool(pct) and abs(sum(pct.values()) - 1.0) < 0.005,
        }
    return out


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------


def create_upload(
    kind: str,
    period: str,
    filename: str,
    user: str,
    n_rows: int,
    meta: Optional[dict] = None,
) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO upload (kind, period, filename, uploaded_at, uploaded_by, "
            "n_rows, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, period, filename, now(), user, n_rows, json.dumps(meta or {})),
        )
        return int(cur.lastrowid)


def add_targets(upload_id: int, rows: Iterable[Tuple[str, str, int, int]]) -> None:
    with db() as conn:
        conn.executemany(
            "INSERT INTO sku_target (upload_id, sku, description, target_qty, moq) "
            "VALUES (?, ?, ?, ?, ?)",
            [(upload_id, *r) for r in rows],
        )


def add_stock(upload_id: int, rows: Iterable[Tuple[str, str, int]]) -> None:
    with db() as conn:
        conn.executemany(
            "INSERT INTO shop_stock (upload_id, sku, shop_code, qty) VALUES (?, ?, ?, ?)",
            [(upload_id, *r) for r in rows],
        )


def get_upload(upload_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM upload WHERE id = ?", (upload_id,)).fetchone()


def list_uploads(kind: str, period: Optional[str] = None) -> List[sqlite3.Row]:
    sql = "SELECT * FROM upload WHERE kind = ?"
    args: List[Any] = [kind]
    if period:
        sql += " AND period = ?"
        args.append(period)
    sql += " ORDER BY id DESC"
    with db() as conn:
        return conn.execute(sql, args).fetchall()


def load_targets(upload_id: int) -> List[Tuple[str, str, int, int]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT sku, description, target_qty, moq FROM sku_target "
            "WHERE upload_id = ? ORDER BY id",
            (upload_id,),
        ).fetchall()
    return [(r["sku"], r["description"], int(r["target_qty"]), int(r["moq"])) for r in rows]


def load_stock(upload_id: int) -> Dict[str, Dict[str, int]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT sku, shop_code, qty FROM shop_stock WHERE upload_id = ?",
            (upload_id,),
        ).fetchall()
    out: Dict[str, Dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["sku"], {})[r["shop_code"]] = int(r["qty"])
    return out


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


def create_run(
    period: str,
    target_upload_id: int,
    stock_upload_id: int,
    user: str,
    params: dict,
) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO run (period, target_upload_id, stock_upload_id, created_at, "
            "created_by, params_json) VALUES (?, ?, ?, ?, ?, ?)",
            (period, target_upload_id, stock_upload_id, now(), user, json.dumps(params)),
        )
        return int(cur.lastrowid)


def save_results(run_id: int, results: Dict[str, Any]) -> None:
    sku_rows: List[tuple] = []
    line_rows: List[tuple] = []

    for scenario, res in results.items():
        for s in res.skus:
            sku_rows.append(
                (
                    run_id, scenario, s.sku, s.description, s.target_qty,
                    s.total_alloc, s.total_stock, s.total_dispatch, s.surplus_qty,
                    s.shortfall_qty, s.n_shops_needing, s.coverage_status, s.moq,
                )
            )
        for ln in res.lines:
            line_rows.append(
                (
                    run_id, scenario, ln.sku, ln.shop_code, ln.share_pct,
                    ln.alloc_qty, ln.stock_qty, ln.gap_qty, ln.dispatch_qty,
                    ln.surplus_qty,
                )
            )

    n_sku = len({r[2] for r in sku_rows})
    n_short = len({r[2] for r in sku_rows if r[11] == "short"})

    with db() as conn:
        conn.executemany(
            "INSERT INTO run_sku (run_id, scenario, sku, description, target_qty, "
            "total_alloc, total_stock, total_dispatch, surplus_qty, shortfall_qty, "
            "n_shops_needing, coverage_status, moq) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            sku_rows,
        )
        conn.executemany(
            "INSERT INTO run_line (run_id, scenario, sku, shop_code, share_pct, "
            "alloc_qty, stock_qty, gap_qty, dispatch_qty, surplus_qty) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            line_rows,
        )
        conn.execute(
            "UPDATE run SET n_sku = ?, n_short = ? WHERE id = ?",
            (n_sku, n_short, run_id),
        )


def get_run(run_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()


def list_runs(limit: int = 50) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM run ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def get_run_skus(run_id: int, scenario: str) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM run_sku WHERE run_id = ? AND scenario = ? "
            "ORDER BY (shortfall_qty > 0) DESC, shortfall_qty DESC, sku",
            (run_id, scenario),
        ).fetchall()


def get_run_lines(
    run_id: int, scenario: str, sku: str
) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT l.*, s.sort_order FROM run_line l "
            "LEFT JOIN shop s ON s.code = l.shop_code "
            "WHERE l.run_id = ? AND l.scenario = ? AND l.sku = ? "
            "ORDER BY s.sort_order, l.shop_code",
            (run_id, scenario, sku),
        ).fetchall()


def dispatch_by_shop(run_id: int, scenario: str, only_need: bool = False) -> List[sqlite3.Row]:
    """Whole-scenario aggregate per shop, computed in SQL."""
    sql = (
        "SELECT shop_code, SUM(alloc_qty) AS alloc, SUM(stock_qty) AS stock, "
        "SUM(dispatch_qty) AS dispatch, SUM(surplus_qty) AS surplus, "
        "SUM(CASE WHEN dispatch_qty > 0 THEN 1 ELSE 0 END) AS n_sku "
        "FROM run_line WHERE run_id = ? AND scenario = ? "
    )
    if only_need:
        sql += "AND dispatch_qty > 0 "
    sql += "GROUP BY shop_code ORDER BY dispatch DESC, shop_code"
    with db() as conn:
        return conn.execute(sql, (run_id, scenario)).fetchall()


def run_totals(run_id: int, scenario: str) -> Dict[str, int]:
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(target_qty),0) AS target_qty, "
            "COALESCE(SUM(total_stock),0) AS total_stock, "
            "COALESCE(SUM(total_dispatch),0) AS total_dispatch, "
            "COALESCE(SUM(shortfall_qty),0) AS shortfall_qty, "
            "COALESCE(SUM(surplus_qty),0) AS surplus_qty, "
            "COUNT(*) AS n_sku, "
            "SUM(CASE WHEN coverage_status = 'short' THEN 1 ELSE 0 END) AS n_short "
            "FROM run_sku WHERE run_id = ? AND scenario = ?",
            (run_id, scenario),
        ).fetchone()
    return {k: int(row[k] or 0) for k in row.keys()}


def delete_run(run_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM run WHERE id = ?", (run_id,))
