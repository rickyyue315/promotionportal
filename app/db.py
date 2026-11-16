"""SQLite data layer.

One file on disk, no server. On Railway the file lives on a mounted volume so it
survives redeploys.

Where the file lives, in order of preference:

1. ``DATA_DIR`` when set explicitly;
2. ``RAILWAY_VOLUME_MOUNT_PATH`` — Railway publishes this as soon as a volume is
   attached, so attaching a volume is enough. Relying on people to also set
   ``DATA_DIR`` was a silent data-loss trap: without it every deploy started
   from an empty database and the saved shares simply vanished;
3. ``./data`` — fine locally, throwaway inside a container.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("portal.db")


def _app_tz():
    """Hong Kong wall-clock time by default.

    A Railway container runs on UTC, so a calculation made at 12:28 in Hong Kong
    used to be stamped 04:28 — and the default period could even be the previous
    month during the first eight hours of the 1st. ``APP_TZ`` overrides the zone;
    the fixed offset is only a fallback for images without a tz database.
    """
    name = os.environ.get("APP_TZ", "Asia/Hong_Kong").strip() or "Asia/Hong_Kong"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Time zone %s unavailable, using UTC+8", name)
        return timezone(timedelta(hours=8))


APP_TZ = _app_tz()

SCENARIOS = ("A", "B", "C")

# Shops where dispatch is gated by the SKU's RP Type. Everything outside this
# list ignores RP Type completely.
RP_GATE_KEY = "rp_gate_shops"
DEFAULT_RP_GATE = "HB87"

# Sites that are warehouses, not shops. They hold stock but never take part in
# the allocation, and their quantity is reported separately.
WAREHOUSE_KEY = "warehouse_sites"
DEFAULT_WAREHOUSE = "D001"

# Supply source codes meaning 行貨: the shop is supplied by the vendor, so the
# buyer raises a PO and D001 is not involved. Everything else — 2 倉貨, blank,
# unknown — is supplied from the warehouse on an STO.
SUPPLY_KEY = "vendor_supply_sources"
DEFAULT_SUPPLY = "1,4"

DEFAULT_SCENARIO_SETTINGS: Dict[str, Dict[str, Any]] = {
    "A": {"label": "恆常推廣", "remark": "", "optional": 0},
    "B": {"label": "精彩價&PIP", "remark": "", "optional": 0},
    "C": {"label": "", "remark": "", "optional": 1},
}

DATA_DIR_SOURCES = ("DATA_DIR", "volume", "default")


def resolve_data_dir() -> Tuple[Path, str]:
    """``(directory, why)`` — where portal.db should live and who decided it.

    Kept as a function so the fallback order can be tested without reloading the
    module, and so a deploy can be reasoned about without reading the code.
    """
    explicit = os.environ.get("DATA_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser(), "DATA_DIR"

    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume:
        return Path(volume).expanduser(), "volume"

    return Path("./data"), "default"


DATA_DIR, DATA_DIR_SOURCE = resolve_data_dir()
DB_PATH = DATA_DIR / "portal.db"

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS shop (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL DEFAULT '',
    region     TEXT NOT NULL DEFAULT 'HK',
    om         TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS scenario_setting (
    scenario TEXT PRIMARY KEY,
    label    TEXT NOT NULL DEFAULT '',
    remark   TEXT NOT NULL DEFAULT '',
    optional INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- What a calculation leaves behind: a timestamp, not a copy of the work.
-- Targets, shop stock and every computed line live in memory only
-- (see app/runstore.py) and disappear with the process.
CREATE TABLE IF NOT EXISTS usage_log (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL,
    user      TEXT NOT NULL DEFAULT '',
    period    TEXT NOT NULL DEFAULT '',
    scenarios TEXT NOT NULL DEFAULT '',
    n_sku     INTEGER NOT NULL DEFAULT 0,
    n_short   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_usage_at ON usage_log(at);
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
        _migrate(conn)
        _seed_settings(conn)
    # Printed once per boot: the deploy log then answers "did this container
    # write to the volume or to its own throwaway disk?" without guessing.
    log.warning("SQLite: %s (source=%s)", DB_PATH.resolve(), DATA_DIR_SOURCE)


# Columns added after the first deployments. CREATE TABLE IF NOT EXISTS never
# alters an existing table, so older databases need these added explicitly.
_ADDED_COLUMNS = {
    "shop": {"om": "TEXT NOT NULL DEFAULT ''"},
}

# Tables from the era when every upload and every computed line was stored.
# Dropped on sight: keeping them would leave the commercial data they describe
# on disk, which is exactly what this design no longer does.
_LEGACY_TABLES = (
    "run_line",
    "run_sku",
    "run",
    "sku_target",
    "shop_stock",
    "upload",
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    for table in _LEGACY_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def _seed_settings(conn: sqlite3.Connection) -> None:
    for scenario, values in DEFAULT_SCENARIO_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO scenario_setting (scenario, label, remark, optional) "
            "VALUES (?, ?, ?, ?)",
            (scenario, values["label"], values["remark"], values["optional"]),
        )
    conn.execute(
        "INSERT OR IGNORE INTO setting (key, value) VALUES (?, ?)",
        (RP_GATE_KEY, DEFAULT_RP_GATE),
    )
    conn.execute(
        "INSERT OR IGNORE INTO setting (key, value) VALUES (?, ?)",
        (WAREHOUSE_KEY, DEFAULT_WAREHOUSE),
    )
    conn.execute(
        "INSERT OR IGNORE INTO setting (key, value) VALUES (?, ?)",
        (SUPPLY_KEY, DEFAULT_SUPPLY),
    )


def now() -> str:
    """Local wall-clock time, naive on purpose: it is only ever displayed."""
    return datetime.now(APP_TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def now_minute() -> str:
    return datetime.now(APP_TZ).strftime("%Y-%m-%d %H:%M")


def stamp() -> str:
    """Filename stamp, e.g. 20261116_1235."""
    return datetime.now(APP_TZ).strftime("%Y%m%d_%H%M")


def today() -> date:
    return datetime.now(APP_TZ).date()


def current_period() -> str:
    return datetime.now(APP_TZ).strftime("%Y-%m")


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


def upsert_shop(
    code: str, name: str = "", region: str = "HK", active: int = 1, om: str = ""
) -> int:
    code = code.strip().upper()
    with db() as conn:
        cur = conn.execute("SELECT id FROM shop WHERE code = ?", (code,))
        row = cur.fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO shop (code, name, region, om, active, sort_order) "
            "VALUES (?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM shop))",
            (code, name, region, om, active),
        )
        return int(cur.lastrowid)


def update_shop(
    shop_id: int, name: str, region: str, active: int, om: Optional[str] = None
) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE shop SET name = ?, region = ?, active = ? WHERE id = ?",
            (name, region.upper()[:2], 1 if active else 0, shop_id),
        )
        # Only touch OM when the caller actually supplied it, so a stale form
        # cannot silently wipe it.
        if om is not None:
            conn.execute("UPDATE shop SET om = ? WHERE id = ?", (om.strip(), shop_id))


def update_shop_om(code: str, om: str) -> None:
    code = code.strip().upper()
    with db() as conn:
        conn.execute("UPDATE shop SET om = ? WHERE code = ?", (om.strip(), code))


def delete_shop(shop_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM shop WHERE id = ?", (shop_id,))


def active_shop_codes() -> List[str]:
    return [r["code"] for r in list_shops(active_only=True)]


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def scenario_settings() -> Dict[str, Dict[str, Any]]:
    """Label, remark and optional flag for each meal plan.

    Falls back to the built-in defaults when a row is missing, so a database
    created before this table existed still renders sensibly.
    """
    with db() as conn:
        rows = conn.execute("SELECT * FROM scenario_setting").fetchall()
    stored = {r["scenario"]: r for r in rows}
    out: Dict[str, Dict[str, Any]] = {}
    for sc in SCENARIOS:
        default = DEFAULT_SCENARIO_SETTINGS[sc]
        row = stored.get(sc)
        out[sc] = {
            "label": (row["label"] if row else default["label"]) or "",
            "remark": (row["remark"] if row else default["remark"]) or "",
            "optional": int(row["optional"]) if row else int(default["optional"]),
        }
    return out


def scenario_labels() -> Dict[str, Dict[str, str]]:
    """Just the display strings — what gets snapshotted into a run."""
    return {
        sc: {"label": meta["label"], "remark": meta["remark"]}
        for sc, meta in scenario_settings().items()
    }


def save_scenario_settings(items: Dict[str, Dict[str, Any]]) -> None:
    with db() as conn:
        for sc, values in items.items():
            if sc not in SCENARIOS:
                continue
            conn.execute(
                "INSERT INTO scenario_setting (scenario, label, remark, optional) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(scenario) DO UPDATE SET label = excluded.label, "
                "remark = excluded.remark, optional = excluded.optional",
                (
                    sc,
                    str(values.get("label", ""))[:60],
                    str(values.get("remark", ""))[:300],
                    1 if values.get("optional") else 0,
                ),
            )


def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO setting (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def rp_gate_shops() -> List[str]:
    """Shop codes where dispatch requires RP Type = RF."""
    return _code_list(RP_GATE_KEY, DEFAULT_RP_GATE)


def warehouse_sites() -> List[str]:
    """Site codes that are warehouses, not shops (stock reported separately)."""
    return _code_list(WAREHOUSE_KEY, DEFAULT_WAREHOUSE)


def vendor_supply_sources() -> List[str]:
    """Supply source codes that mean 行貨 (buyer opens a PO, no STO)."""
    return _code_list(SUPPLY_KEY, DEFAULT_SUPPLY)


def _code_list(key: str, default: str) -> List[str]:
    raw = get_setting(key, default)
    return [p.strip().upper() for p in re.split(r"[,，;；\s]+", raw or "") if p.strip()]


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
            "SELECT DISTINCT period FROM share_set ORDER BY period DESC"
        ).fetchall()
    return [r["period"] for r in rows]


def share_status(period: str) -> Dict[str, Dict[str, Any]]:
    """Per-scenario readiness for a month: total %, shops covered, updated at."""
    shops = active_shop_codes()
    settings = scenario_settings()
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
            "optional": bool(settings[scenario]["optional"]),
            "label": settings[scenario]["label"],
            "remark": settings[scenario]["remark"],
        }
    return out


# ---------------------------------------------------------------------------
# storage: where the data lives and whether it survives a deploy
# ---------------------------------------------------------------------------

_RAILWAY_ENV_KEYS = (
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_VOLUME_MOUNT_PATH",
)


def _railway_volume() -> Optional[Path]:
    raw = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    return Path(raw).expanduser() if raw else None


def _inside(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def storage_status() -> Dict[str, Any]:
    """Enough truth to answer "why did my shares disappear?" from the screen.

    ``ephemeral`` is the one that matters: a Railway container with no volume
    behind the database file loses everything on the next deploy.
    """
    path = DB_PATH
    volume = _railway_volume()
    on_railway = any(os.environ.get(k) for k in _RAILWAY_ENV_KEYS)
    volume_backed = bool(volume) and _inside(path, volume)

    info: Dict[str, Any] = {
        "path": str(path.resolve()),
        "source": DATA_DIR_SOURCE,
        "on_railway": on_railway,
        "volume_path": str(volume) if volume else "",
        "volume_backed": volume_backed,
        "ephemeral": on_railway and not volume_backed,
        "exists": False,
        "size_bytes": 0,
        "n_share_sets": 0,
        "n_periods": 0,
        "last_share_update": "",
    }

    try:
        if path.exists():
            info["exists"] = True
            info["size_bytes"] = path.stat().st_size
    except OSError:
        pass

    try:
        with db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT period) AS p, "
                "COALESCE(MAX(updated_at), '') AS last FROM share_set"
            ).fetchone()
        info["n_share_sets"] = int(row["n"] or 0)
        info["n_periods"] = int(row["p"] or 0)
        info["last_share_update"] = row["last"] or ""
    except sqlite3.Error:
        pass

    return info


def backup_to(dest: Path) -> Path:
    """Consistent copy of the live database, WAL included.

    ``sqlite3.Connection.backup`` is used instead of copying the file so a
    backup taken mid-write is still valid.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(dest)
    try:
        with db() as conn:
            conn.backup(target)
    finally:
        target.close()
    return dest


# ---------------------------------------------------------------------------
# usage log — the only trace a calculation leaves
# ---------------------------------------------------------------------------


def log_usage(
    period: str,
    user: str = "",
    scenarios: Sequence[str] = (),
    n_sku: int = 0,
    n_short: int = 0,
) -> None:
    """Record *that* a calculation ran, never what was in it."""
    with db() as conn:
        conn.execute(
            "INSERT INTO usage_log (at, user, period, scenarios, n_sku, n_short) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now(), user, period, ",".join(scenarios), int(n_sku), int(n_short)),
        )


def list_usage(limit: int = 100) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM usage_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def latest_usage() -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM usage_log ORDER BY id DESC LIMIT 1"
        ).fetchone()


def usage_summary(period: str) -> Dict[str, Any]:
    """How many times a month was calculated, for the dashboard."""
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(at), '') AS last "
            "FROM usage_log WHERE period = ?",
            (period,),
        ).fetchone()
    return {"n": int(row["n"] or 0), "last": row["last"] or ""}
