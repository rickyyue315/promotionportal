"""Promotion Dispatch Portal — FastAPI application."""

from __future__ import annotations

import os
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, calc, db, excel_io, runstore, version

BASE = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="推廣派貨 Portal", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

templates.env.filters["pct"] = lambda v: f"{(v or 0) * 100:.2f}%"
templates.env.filters["num"] = lambda v: f"{int(v or 0):,}"


def scname(sc: str, labels: Optional[Dict[str, Any]] = None) -> str:
    """'A 餐 · 恆常推廣' — the meal plan name with its business meaning."""
    meta = (labels or {}).get(sc) or {}
    label = meta.get("label") if isinstance(meta, dict) else str(meta or "")
    return f"{sc} 餐 · {label}" if label else f"{sc} 餐"


templates.env.globals["scname"] = scname
# Every page carries the build label, so "is the latest version live?" is
# answered by looking at the screen instead of guessing from behaviour.
templates.env.globals["version"] = version.as_dict()
templates.env.globals["version_label"] = version.label()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    user = auth.current_user(request)
    base = {
        "request": request,
        "user": user,
        "is_admin": auth.is_admin(user),
        "msg": request.query_params.get("msg", ""),
        "level": request.query_params.get("level", "info"),
        "period": request.query_params.get("period", db.current_period()),
        "scenarios": db.SCENARIOS,
        "labels": db.scenario_labels(),
        "rp_gate_shops": db.rp_gate_shops(),
        "now": db.now_minute(),
    }
    base.update(ctx)
    return templates.TemplateResponse(request, name, base)


def back(url: str, msg: str = "", level: str = "info") -> RedirectResponse:
    if not msg:
        return RedirectResponse(url, status_code=303)
    sep = "&" if "?" in url else "?"
    from urllib.parse import quote

    return RedirectResponse(f"{url}{sep}msg={quote(msg)}&level={level}", status_code=303)


def _parse_date(value: str) -> Optional[date]:
    """Accept the HTML date input (YYYY-MM-DD) and the usual typed variants."""
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _period(text: str) -> str:
    """Normalise a month to ``YYYY-MM``, or "" when it is not a month.

    The month pickers post ``YYYY-MM``, but links, old bookmarks and people
    typing by hand may carry ``2026/11``, ``202611`` or a full date. Returning
    "" lets the caller fall back (page) or refuse (a write) instead of quietly
    creating a junk row in the share table.
    """
    raw = (text or "").strip().replace("/", "-").replace(".", "-")
    if not raw:
        return ""
    match = re.fullmatch(r"(\d{4})-?(\d{1,2})(?:-\d{1,2})?", raw)
    if not match:
        return ""
    year, month = int(match.group(1)), int(match.group(2))
    if not 2000 <= year <= 2100 or not 1 <= month <= 12:
        return ""
    return f"{year:04d}-{month:02d}"


BAD_PERIOD = "月份的格式應為 YYYY-MM，例如 2026-11。"


@app.exception_handler(auth.AuthRequired)
def _need_login(request: Request, exc: auth.AuthRequired):
    nxt = request.url.path
    return RedirectResponse(f"/login?next={nxt}", status_code=303)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if auth.current_user(request):
        return RedirectResponse(next or "/", status_code=303)
    return render(request, "login.html", next=next, standalone=True)


@app.post("/login")
def login_submit(
    request: Request,
    password: str = Form(""),
    name: str = Form(""),
    next: str = Form("/"),
):
    role = auth.authenticate(password)
    if not role:
        return render(
            request,
            "login.html",
            next=next,
            standalone=True,
            error="密碼不正確",
        )
    resp = RedirectResponse(next or "/", status_code=303)
    resp.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_token(role, name.strip() or role),
        max_age=auth.MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("COOKIE_SECURE", "1") == "1",
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.get("/health")
def health():
    # Unauthenticated on purpose: Railway's healthcheck and a quick deploy
    # verification both need it, and it exposes nothing but build metadata.
    # The storage fields are what a redeploy check needs: same path before and
    # after means the database is on the volume, not on throwaway disk.
    store = db.storage_status()
    return {
        "ok": True,
        "time": db.now(),
        **version.as_dict(),
        "data_dir": store["path"],
        "data_source": store["source"],
        "persistent": not store["ephemeral"],
        "n_periods": store["n_periods"],
    }


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    auth.require_user(request)
    period = db.current_period()
    status = db.share_status(period)
    latest = db.latest_usage()
    usage = db.usage_summary(period)

    # Optional plans (C, usually) do not make the month look "not ready".
    required = [sc for sc in db.SCENARIOS if not status[sc]["optional"]]

    return render(
        request,
        "dashboard.html",
        period=period,
        status=status,
        latest=latest,
        usage=usage,
        recent=db.list_usage(limit=5),
        keep_minutes=runstore.TTL_SECONDS // 60,
        n_shops=len(db.active_shop_codes()),
        required=required,
        n_ready=sum(1 for sc in required if status[sc]["ready"]),
        missing=[sc for sc in required if not status[sc]["ready"]],
        ready_all=all(status[sc]["ready"] for sc in required),
        storage=db.storage_status(),
    )


# ---------------------------------------------------------------------------
# shops (admin)
# ---------------------------------------------------------------------------


@app.get("/admin/shops", response_class=HTMLResponse)
def shops_page(request: Request):
    auth.require_admin(request)
    return render(request, "shops.html", shops=db.list_shops())


@app.post("/admin/shops")
def shop_add(
    request: Request,
    code: str = Form(...),
    name: str = Form(""),
    region: str = Form("HK"),
    om: str = Form(""),
):
    auth.require_admin(request)
    if not code.strip():
        return back("/admin/shops", "店舖代碼不可空白", "error")
    db.upsert_shop(code, name, region, om=om.strip())
    return back("/admin/shops", f"已新增店舖 {code.strip().upper()}")


@app.post("/admin/shops/{shop_id}")
def shop_update(
    request: Request,
    shop_id: int,
    name: str = Form(""),
    region: str = Form("HK"),
    om: Optional[str] = Form(None),
    active: str = Form("0"),
):
    auth.require_admin(request)
    db.update_shop(shop_id, name, region, 1 if active == "1" else 0, om=om)
    return back("/admin/shops", "已更新")


@app.post("/admin/shops/{shop_id}/delete")
def shop_delete(request: Request, shop_id: int):
    auth.require_admin(request)
    db.delete_shop(shop_id)
    return back("/admin/shops", "已刪除店舖")


# ---------------------------------------------------------------------------
# settings (admin)
# ---------------------------------------------------------------------------


@app.get("/admin/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    auth.require_admin(request)
    return render(
        request,
        "settings.html",
        meta=db.scenario_settings(),
        gate_raw=db.get_setting(db.RP_GATE_KEY, db.DEFAULT_RP_GATE),
        warehouse_raw=db.get_setting(db.WAREHOUSE_KEY, db.DEFAULT_WAREHOUSE),
        supply_raw=db.get_setting(db.SUPPLY_KEY, db.DEFAULT_SUPPLY),
        shop_codes={s["code"] for s in db.list_shops()},
        storage=db.storage_status(),
    )


@app.post("/admin/settings")
async def settings_save(request: Request):
    auth.require_admin(request)
    form = await request.form()

    items: Dict[str, Dict[str, Any]] = {}
    for sc in db.SCENARIOS:
        items[sc] = {
            "label": str(form.get(f"label_{sc}", "")).strip(),
            "remark": str(form.get(f"remark_{sc}", "")).strip(),
            "optional": form.get(f"optional_{sc}") == "1",
        }
    db.save_scenario_settings(items)

    gate_raw = str(form.get("rp_gate_shops", "")).strip()
    db.set_setting(db.RP_GATE_KEY, gate_raw)

    warehouse_raw = str(form.get("warehouse_sites", "")).strip()
    db.set_setting(db.WAREHOUSE_KEY, warehouse_raw)

    # Only touched when the form actually carries the field: an older cached
    # page (or a test posting the previous field set) must not blank the rule.
    if "vendor_supply_sources" in form:
        db.set_setting(
            db.SUPPLY_KEY, str(form.get("vendor_supply_sources", "")).strip()
        )

    notes = ["已儲存餐別名稱、貨倉、RP Type 與 Supply Source 規則"]
    level = "ok"

    if not db.warehouse_sites():
        notes.append("貨倉代碼留空，派貨將不受倉存限制")
        level = "warn"

    if not db.vendor_supply_sources():
        notes.append("Supply Source 行貨代碼留空，所有店舖都會當作倉貨（D001 出 STO）")
        level = "warn"

    known = {s["code"] for s in db.list_shops()}
    unknown = [code for code in db.rp_gate_shops() if code not in known]
    if unknown:
        notes.append("、".join(unknown) + " 不在店舖主檔內，RP Type 規則不會生效")
        level = "warn"

    clash = sorted(set(db.warehouse_sites()) & known)
    if clash:
        notes.append("、".join(clash) + " 同時是貨倉與店舖，會當作貨倉處理")
        level = "warn"

    return back("/admin/settings", "；".join(notes), level)


@app.get("/admin/backup/db")
def backup_db(request: Request):
    """Download the whole database.

    Shares live in the same SQLite file as shops, uploads and runs, so one file
    is the complete backup — and the only thing that helps if a deploy ever
    starts from an empty disk again.
    """
    auth.require_admin(request)
    stamp = db.stamp()
    with tempfile.TemporaryDirectory() as tmp:
        payload = db.backup_to(Path(tmp) / "portal.db").read_bytes()
    return Response(
        payload,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="portal_backup_{stamp}.db"'
        },
    )


# ---------------------------------------------------------------------------
# shares (admin)
# ---------------------------------------------------------------------------


@app.get("/admin/shares", response_class=HTMLResponse)
def shares_page(request: Request, period: str = "", scenario: str = "A"):
    auth.require_admin(request)
    period = _period(period) or db.current_period()
    scenario = scenario if scenario in db.SCENARIOS else "A"
    shops = db.list_shops(active_only=True)
    current = db.get_shares(period, scenario)
    status = db.share_status(period)

    total = sum(current.values())
    return render(
        request,
        "shares.html",
        period=period,
        scenario=scenario,
        shops=shops,
        current=current,
        all_shares=db.get_all_shares(period),
        status=status,
        total=total,
        recorded=db.list_periods()[:12],
        storage=db.storage_status(),
    )


@app.post("/admin/shares")
async def shares_save(request: Request):
    auth.require_admin(request)
    form = await request.form()
    period = _period(str(form.get("period", "")))
    if not period:
        return back("/admin/shares", BAD_PERIOD, "error")
    scenario = str(form.get("scenario", "A"))
    # Two ways to say the same mix: percentages, or the money behind them. The
    # database only ever stores percentages (see save_shares), so a month never
    # ends up with one plan in dollars and another in percent.
    unit = str(form.get("unit", "pct")).strip().lower()
    unit = "amt" if unit in ("amt", "amount", "$", "money") else "pct"
    user = (auth.current_user(request) or {}).get("name", "")

    typed: Dict[str, float] = {}
    for key, value in form.items():
        if not key.startswith("pct_"):
            continue
        code = key[4:]
        number = excel_io.to_number(value)
        if number is None:
            continue
        typed[code] = max(number, 0.0)

    amount_total = 0.0
    if unit == "amt":
        pct, amount_total, _warnings = calc.weights_to_shares(typed)
        if not pct:
            return back(
                f"/admin/shares?period={period}&scenario={scenario}",
                "以金額輸入，但所有店舖的總額是 0，無法換算成佔比",
                "error",
            )
        note_text = f"以金額輸入，總額 ${amount_total:,.0f}，儲存時已換算成百分比"
    else:
        pct = {code: value / 100.0 for code, value in typed.items()}
        note_text = ""

    db.save_shares(period, scenario, pct, user, note_text)
    total = sum(pct.values())
    level = "ok" if abs(total - 1) < 0.005 else "warn"
    label = db.scenario_settings()[scenario]["label"]
    who = f"{scenario} 餐 · {label}" if label else f"{scenario} 餐"
    note = f"{who} 已儲存，總和 {total:.2%}"
    if unit == "amt":
        note += f"（輸入總額 ${amount_total:,.0f}，已按比例換算）"
    return back(f"/admin/shares?period={period}&scenario={scenario}", note, level)


@app.post("/admin/shares/import")
async def shares_import(
    request: Request,
    period: str = Form(...),
    file: UploadFile = File(...),
):
    auth.require_admin(request)
    period = _period(period)
    if not period:
        return back("/admin/shares", BAD_PERIOD, "error")
    data = await file.read()
    parsed = excel_io.parse_share_file(data)

    if parsed.errors:
        return back(
            f"/admin/shares?period={period}",
            "匯入失敗：" + "；".join(parsed.errors),
            "error",
        )

    user = (auth.current_user(request) or {}).get("name", "")

    # Create shops in the order they appear in the file so the admin grid and
    # the detail reports read in the same sequence the buyer typed.
    known = {s["code"] for s in db.list_shops()}
    created = 0
    for code in parsed.order:
        if code not in known:
            db.upsert_shop(
                code,
                parsed.names.get(code, ""),
                parsed.regions.get(code, "HK"),
                om=parsed.oms.get(code, ""),
            )
            known.add(code)
            created += 1

    # OM may have changed for shops that already exist; a blank cell never
    # wipes what is already on file.
    for code, om in parsed.oms.items():
        if code in known:
            db.update_shop_om(code, om)

    db.save_shares_bulk(period, parsed.by_scenario, user)

    parts = [f"{sc} {len(p)} 間店舖" for sc, p in parsed.by_scenario.items()]
    msg = f"已匯入到 {period}：" + "、".join(parts)
    if created:
        msg += f"；同時新增 {created} 間店舖"
    if parsed.warnings:
        msg += "｜" + "；".join(parsed.warnings[:3])
    return back(f"/admin/shares?period={period}", msg, "ok" if not parsed.warnings else "warn")


@app.get("/admin/shares/template")
def shares_template(request: Request, period: str = ""):
    auth.require_admin(request)
    period = _period(period) or db.current_period()
    shops = [dict(s) for s in db.list_shops(active_only=True)]
    existing = db.get_all_shares(period)
    data = excel_io.export_share_template(shops, existing, period)
    return _xlsx(data, f"店舖佔比_{period}.xlsx")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@app.get("/run/new", response_class=HTMLResponse)
def run_new_page(request: Request, period: str = ""):
    user = auth.require_user(request)
    period = _period(period) or db.current_period()
    status = db.share_status(period)
    required = [sc for sc in db.SCENARIOS if not status[sc]["optional"]]
    today = db.today()
    return render(
        request,
        "upload.html",
        period=period,
        status=status,
        ready=any(v["ready"] for v in status.values()),
        required=required,
        missing=[sc for sc in required if not status[sc]["ready"]],
        promo_start=today.isoformat(),
        promo_end=(today + timedelta(days=13)).isoformat(),
        dispatch_days=4,
        can_admin=auth.is_admin(user),
    )


@app.get("/run/template/{kind}")
def template_download(request: Request, kind: str):
    auth.require_user(request)
    if kind == "target":
        return _xlsx(excel_io.export_target_template(), "SKU_Target_範本.xlsx")
    if kind == "stock":
        shops = db.active_shop_codes()
        return _xlsx(excel_io.export_stock_template(shops), "店舖庫存_範本.xlsx")
    return back("/run/new", "未知的範本類型", "error")


@app.post("/run/probe/dates")
async def run_probe_dates(request: Request, target_file: UploadFile = File(...)):
    """What the upload page asks the moment a target file is chosen.

    Reading the sheet here is not a second implementation of the dates: it is
    the same parse the run will do, so the numbers shown before pressing 開始計算
    are the numbers the run will use. It also surfaces a broken file early,
    while the person is still looking at the file picker.
    """
    auth.require_user(request)
    data = await target_file.read()
    if not data:
        return JSONResponse({"ok": False, "errors": ["檔案是空的"], "warnings": []})

    parsed = excel_io.parse_target_file(data)
    start, end = parsed.promo_start, parsed.promo_end
    found = bool(start and end)
    return JSONResponse(
        {
            "ok": found,
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
            "days": (end - start).days + 1 if found else 0,
            "n_sku": len(parsed.rows),
            "errors": parsed.errors[:3],
            # Only the notes about the dates themselves: the rest belong to the
            # run and would be noise next to a file picker.
            "warnings": [w for w in parsed.warnings if "推廣" in w][:2],
        }
    )


@app.post("/run/new")
async def run_create(
    request: Request,
    period: str = Form(...),
    promo_start: str = Form(""),
    promo_end: str = Form(""),
    dispatch_days: int = Form(4),
    date_source: str = Form("auto"),
    group_topup: str = Form(""),
    suggest_transfer: str = Form(""),
    temp_on: str = Form(""),
    temp_name: str = Form(""),
    temp_unit: str = Form("pct"),
    temp_rows: str = Form(""),
    target_file: UploadFile = File(...),
    stock_file: UploadFile = File(...),
):
    user = auth.require_user(request)
    name = user.get("name", "")

    period = _period(period) or db.current_period()
    status = db.share_status(period)
    ready = [sc for sc in db.SCENARIOS if status[sc]["ready"]]
    if not ready:
        return back(
            "/run/new",
            f"{period} 的 A/B/C 佔比尚未設定完成，請先到後台設定或在下方匯入佔比 Excel。",
            "error",
        )

    target_data = await target_file.read()
    stock_data = await stock_file.read()

    parsed_t = excel_io.parse_target_file(target_data)
    parsed_s = excel_io.parse_stock_file(stock_data, set(db.active_shop_codes()))

    # 臨時餐：a one-off mix for this run only. It is deliberately never written
    # to share_set — the monthly A/B/C grid is what the next run and the share
    # template read, so a special promotion must not overwrite it. The scenario
    # travels as "T" and lives only as long as this result does.
    temp_label = ""
    temp_shares: Dict[str, float] = {}
    temp_total = 0.0
    temp_notes: List[str] = []
    temp_errors: List[str] = []
    temp_unit = str(temp_unit).strip().lower()
    temp_unit = "amt" if temp_unit in ("amt", "amount", "$", "money") else "pct"
    temp_wanted = str(temp_on).strip().lower() not in ("", "0", "false", "off", "no")
    if temp_wanted:
        temp_label = (temp_name or "").strip()[:20] or "臨時餐"
        temp_shares, temp_total, temp_notes, temp_errors = excel_io.parse_share_text(
            temp_rows, unit=temp_unit
        )

    # The target sheet already prints the promo window on its lines, so by
    # default that is the window we use: the dates on screen are then the same
    # ones the buyer read in the file, with nothing to re-type and mistype.
    # "manual" is the escape hatch for a file that carries no dates or carries
    # the wrong ones.
    manual_start = _parse_date(promo_start)
    manual_end = _parse_date(promo_end)
    file_dates = parsed_t.promo_start and parsed_t.promo_end
    if date_source == "manual" or not file_dates:
        start, end = manual_start, manual_end
        promo_source = "manual"
    else:
        start, end = parsed_t.promo_start, parsed_t.promo_end
        promo_source = "target_file"

    problems: List[str] = []
    if not start or not end:
        problems.append(
            "請填寫推廣開始日期與完結日期"
            if date_source == "manual"
            else "Target 檔讀不到推廣日期，請填寫推廣開始日期與完結日期"
        )
    elif end < start:
        problems.append("完結日期不可早於開始日期")
    if dispatch_days < 1:
        problems.append("派貨日數至少 1 日")
    if parsed_t.errors:
        problems.append("SKU Target：" + "；".join(parsed_t.errors))
    if parsed_s.errors:
        problems.append("店舖庫存：" + "；".join(parsed_s.errors))
    if temp_errors:
        problems.append("臨時餐：" + "；".join(temp_errors[:3]))
    elif temp_wanted and not temp_shares:
        problems.append("臨時餐：所有店舖的數值都是 0，無法換算成佔比")
    if problems:
        return back("/run/new", "上載檔案或日期有問題 — " + " ｜ ".join(problems), "error")

    promo = calc.PromoWindow(start=start, end=end, dispatch_days=dispatch_days)
    if promo.days <= 0:
        return back("/run/new", "推廣日數不正確，請檢查日期", "error")

    # A hand-typed date that disagrees with the sheet is exactly the mistake
    # this feature exists to remove, so say it out loud rather than swap the
    # numbers in silence.
    promo_note = ""
    if promo_source == "target_file" and manual_start and manual_end:
        if (manual_start, manual_end) != (start, end):
            promo_note = (
                f"畫面的日期 {manual_start:%d-%b-%y} → {manual_end:%d-%b-%y} 與 Target 檔不符，"
                f"已採用檔案的 {start:%d-%b-%y} → {end:%d-%b-%y}。"
            )

    # The warehouse holds stock but never receives an allocation; its net stock
    # is what limits dispatch. Pending receipts at the warehouse are inbound
    # and are deliberately not counted.
    warehouse_codes = set(db.warehouse_sites())
    stock_by_sku: Dict[str, Dict[str, int]] = {}
    rp_by_sku: Dict[str, Dict[str, str]] = {}
    sales_by_sku: Dict[str, Dict[str, calc.ShopSales]] = {}
    supply_by_sku: Dict[str, Dict[str, str]] = {}
    # The shop's own Safety Stock: never stock, only the floor a member of a
    # shared target is topped up to (see calc's "Safety floor for a group").
    safety_by_sku: Dict[str, Dict[str, int]] = {}
    warehouse_by_sku: Dict[str, int] = {}
    warehouse_pending: Dict[str, int] = {}
    # MOQ is read per site (SKU -> shop code -> MOQ). A shop rounds its need up
    # to the packing size on its own row, so one site's figure never leaks into
    # another. The warehouse row is skipped below like every other per-shop
    # field: D001's MOQ is a carton size for the warehouse, and taking the
    # largest value per SKU used to send it to shops that order in sixes
    # (user, 2026-11-23).
    moq_by_site: Dict[str, Dict[str, int]] = {}

    for row in parsed_s.rows:
        if row.shop in warehouse_codes:
            warehouse_by_sku[row.sku] = warehouse_by_sku.get(row.sku, 0) + row.qty
            warehouse_pending[row.sku] = (
                warehouse_pending.get(row.sku, 0) + row.pending
            )
            continue
        if row.moq > 1:
            per_shop_moq = moq_by_site.setdefault(row.sku, {})
            per_shop_moq[row.shop] = max(per_shop_moq.get(row.shop, 1), row.moq)
        stock_by_sku.setdefault(row.sku, {})[row.shop] = row.qty
        if row.rp_type:
            rp_by_sku.setdefault(row.sku, {})[row.shop] = row.rp_type
        # Supply source decides the route, so it is kept per SKU × shop; the
        # warehouse's own row is skipped above — 忽視 D001.
        if row.supply_source:
            supply_by_sku.setdefault(row.sku, {})[row.shop] = row.supply_source
        if row.safety > 0:
            safety_by_sku.setdefault(row.sku, {})[row.shop] = row.safety
        sales_by_sku.setdefault(row.sku, {})[row.shop] = calc.ShopSales(
            last_month_sold=row.last_month_sold,
            mtd_sold=row.mtd_sold,
            pending_qty=row.pending,
            launch_date=row.launch_date,
        )

    has_sales = bool(parsed_s.mapped.get("last_month_sold")) and bool(
        parsed_s.mapped.get("mtd_sold")
    )

    # One target row can cover several SKUs (two pack sizes sharing a quantity,
    # written as a merged cell or a blank). They are split here — once, before
    # any scenario — so A/B/C and the export all report the same numbers.
    targets = [
        calc.SkuTarget(
            sku=row.sku,
            target_qty=row.qty,
            description=row.description,
            group=row.group,
            brand=row.brand,
        )
        for row in parsed_t.rows
    ]
    targets, shared_targets = calc.split_shared_targets(
        targets,
        sales_by_sku=sales_by_sku if has_sales else None,
        stock_by_sku=stock_by_sku,
    )

    # SKUs in the stock file but absent from the target file are simply not
    # promoted this period; flag it only when it looks like a mismatch.
    target_skus = {t.sku for t in targets}
    stock_only = sorted(set(stock_by_sku) - target_skus)
    target_only = sorted(target_skus - set(stock_by_sku))

    shares = {sc: db.get_shares(period, sc) for sc in ready}
    # 臨時餐 rides after A/B/C: same targets, same stock, different shop mix.
    # It is part of this run only — nothing is written back to the database.
    run_scenarios = list(ready)
    if temp_wanted and temp_shares:
        shares["T"] = temp_shares
        run_scenarios.append("T")
    shops = db.active_shop_codes()
    # Names are a label on the code, not part of the arithmetic, but the export
    # is read by people who know "HB01" far less well than its shop name.
    shop_rows = db.list_shops(active_only=True)
    shop_names = {
        row["code"]: (row["name"] or row["code"])
        for row in shop_rows
    }
    # OM is the boundary for a stock transfer: an OM answers for its own shops,
    # so the suggestion never crosses into another OM's stock.
    om_by_shop = {row["code"]: str(row["om"] or "").strip() for row in shop_rows}
    # Only shops that actually take part in this run can gate anything; keeping
    # the filtered list in the snapshot stops the result page explaining a rule
    # that never applied.
    shop_set = set(shops)
    gate = [code for code in db.rp_gate_shops() if code in shop_set]
    has_warehouse = bool(warehouse_by_sku)
    has_supply = bool(parsed_s.mapped.get("supply_source"))
    has_safety = bool(parsed_s.mapped.get("safety"))
    vendors = db.vendor_supply_sources()
    # The floor is a deliberate over-send, so it is an opt-in per run rather
    # than an always-on rule; the form's checkbox arrives only when ticked.
    topup_on = group_topup.strip().lower() not in ("", "0", "false", "off", "no")
    # A transfer notice needs 4–7 days and may not clear before the promotion
    # opens, so this is off unless the operator asks for it. It never replaces
    # the PO/STO numbers.
    transfer_on = suggest_transfer.strip().lower() not in ("", "0", "false", "off", "no")

    results = calc.compute_all_scenarios(
        shares,
        shops,
        targets,
        stock_by_sku,
        rp_by_sku=rp_by_sku,
        rp_gate_shops=gate,
        sales_by_sku=sales_by_sku if has_sales else None,
        promo=promo,
        as_of=db.today(),
        warehouse_by_sku=warehouse_by_sku if has_warehouse else None,
        warehouse_pending_by_sku=warehouse_pending,
        moq_by_site=moq_by_site,
        supply_by_sku=supply_by_sku,
        vendor_sources=vendors,
        safety_by_sku=safety_by_sku,
        group_topup=topup_on,
        om_by_shop=om_by_shop,
        suggest_transfer=transfer_on,
    )

    gate_set = set(gate)
    gate_has_rows = any(row.shop in gate_set for row in parsed_s.rows)
    if gate and gate_has_rows and not parsed_s.mapped.get("rp_type"):
        for res in results.values():
            res.warnings.append(
                "庫存檔沒有 RP Type 欄位，"
                + "、".join(sorted(gate_set))
                + " 全部 SKU 依規則當作不可派貨（0 件）"
            )

    if warehouse_codes and parsed_s.rows and not has_warehouse:
        for res in results.values():
            res.warnings.append(
                "庫存檔沒有貨倉（"
                + "、".join(sorted(warehouse_codes))
                + "）的紀錄，未套用倉存上限"
            )

    if not has_sales:
        for res in results.values():
            res.warnings.append(
                "庫存檔沒有 Last Month Sold Qty／MTD Sold Qty，未計算推廣期預計銷量與銷售差距"
            )

    if parsed_s.rows and not has_supply:
        for res in results.values():
            res.warnings.append(
                "庫存檔沒有 Supply Source 欄位，全部店舖當作倉貨（由 D001 開 STO）；"
                "如有行貨店舖，請在檔案加一欄 Supply Source（1／4 行貨；2 倉貨）"
            )

    if parsed_s.rows and not parsed_s.mapped.get("moq"):
        for res in results.values():
            res.warnings.append(
                "庫存檔沒有 MOQ 欄位，全部 SKU 當作 MOQ = 1（需求不進位）"
            )

    if topup_on and not has_safety and any(t.group for t in targets):
        # The rule was asked for but cannot run: say so rather than leaving a
        # ticked checkbox that quietly did nothing.
        for res in results.values():
            res.warnings.append(
                "已勾選「貨組補底」，但庫存檔沒有 Safety Stock 欄位，所以沒有補底；"
                "要補底請在庫存檔加一欄 Safety Stock（每個 SKU × 店舖的補底線）"
            )

    if stock_only:
        for res in results.values():
            res.warnings.append(
                f"庫存檔有 {len(stock_only)} 個 SKU 不在 Target 檔內，已忽略（例：{stock_only[0]}）"
            )
    if target_only:
        for res in results.values():
            res.warnings.append(
                f"有 {len(target_only)} 個 SKU 在 Target 檔內但庫存檔沒有紀錄，已當作 0 庫存"
                f"（例：{target_only[0]}）"
            )

    if promo_note:
        for res in results.values():
            res.warnings.insert(0, promo_note)

    # What reading the stock file had to assume — duplicate rows added up, lines
    # skipped, consignment folded into SaSa Net Stock — describes the numbers in
    # this result, so it belongs on the result page rather than disappearing with
    # the upload form. The "偵測為…" lines only say which layout was recognised
    # and would be noise on every run.
    stock_notes = [w for w in parsed_s.warnings if not w.startswith("偵測為")]
    if stock_notes:
        for res in results.values():
            res.warnings.extend(stock_notes)

    if "T" in results:
        # Said on the result itself, because a fourth column that is nowhere in
        # the database is exactly the kind of thing someone re-opens later and
        # cannot find again.
        note = [
            f"臨時餐「{temp_label}」只用於今次計算，沒有存入資料庫；"
            "A／B／C 的每月佔比不受影響"
        ]
        if temp_unit == "amt":
            note.append(
                f"臨時餐以金額輸入，總額 ${temp_total:,.0f}，已按比例換算成佔比"
            )
        note.extend(temp_notes)
        unknown = sorted(set(temp_shares) - shop_set)
        if unknown:
            note.append(
                f"臨時餐有 {len(unknown)} 個店舖代碼不在店舖主檔，已忽略"
                f"（例：{unknown[0]}）"
            )
        results["T"].warnings[:0] = note

    # Nothing about the files or the numbers goes to disk. The finished result
    # is handed to runstore (memory, a couple of hours) so the result page,
    # the drill-downs and the export work; the database only learns that a
    # calculation happened.
    # Which shops actually took the 4 route, so the page explains 過數 only when
    # it applies instead of on every run.
    four_shops = sorted(
        {
            line.shop_code
            for res in results.values()
            for line in res.lines
            if line.supply_source == "4"
        }
    )
    params = {
        "scenarios": run_scenarios,
        "share_totals": {sc: sum(shares[sc].values()) for sc in run_scenarios},
        "warnings": {sc: results[sc].warnings for sc in run_scenarios},
        "n_shops": len(shops),
        "target_file": target_file.filename or "",
        "stock_file": stock_file.filename or "",
        "promo_start": start.isoformat(),
        "promo_end": end.isoformat(),
        "promo_source": promo_source,
        "promo_days": promo.days,
        "dispatch_days": promo.effective_dispatch_days,
        "as_of": db.today().isoformat(),
        "warehouse_sites": sorted(warehouse_codes),
        "has_sales": has_sales,
        "has_moq": bool(parsed_s.mapped.get("moq")),
        # Whether the target sheet carried Brand code: the export always prints
        # the column, so 參數 has to say whether a blank means "not provided".
        "has_brand": bool(parsed_t.mapped.get("brand")),
        "has_supply_source": has_supply,
        # Whether the stock file carried Safety Stock, and whether the floor was
        # asked for: the page and the export explain the rule with these two.
        "has_safety": has_safety,
        "group_topup": topup_on,
        # Advice only, and off unless asked for: the transfer notice may not
        # clear before the promotion starts, so it never stands in for a PO/STO.
        "suggest_transfer": transfer_on,
        "has_om": any(om_by_shop.values()),
        # Whether the stock figure includes Unrestr. Consignment: the result
        # page and the export both state the 口徑 instead of leaving the reader
        # to guess what "現有庫存" was made of.
        "has_consignment": bool(parsed_s.mapped.get("consignment")),
        # Snapshot the rule as it was applied: changing the setting later must
        # not relabel a batch that was already computed.
        "vendor_supply_sources": vendors if has_supply else [],
        "supply_four_shops": four_shops,
        # How each shared target was split, so the result page and the export
        # can show the basis without re-reading either file.
        "shared_targets": shared_targets,
        # Snapshot the names so the page keeps the wording it was computed
        # with, even if the labels are renamed while it is still open.
        "scenario_labels": {
            **db.scenario_labels(),
            **(
                {"T": {"label": temp_label, "remark": "臨時餐，只用於今次計算"}}
                if "T" in results
                else {}
            ),
        },
        # The temp mix's own numbers, so the result page can print what was
        # typed without keeping a copy of the input file.
        "temp_label": temp_label if "T" in results else "",
        "temp_amount": temp_total if ("T" in results and temp_unit == "amt") else 0.0,
        "rp_gate_shops": gate,
        # Same idea as the labels: the shop name is what the sheet is computed
        # with, so a rename while the result is still open does not rewrite it.
        "shop_names": shop_names,
    }
    run = runstore.put(period, db.now(), name, params, results)
    db.log_usage(period, name, list(results), run.n_sku, run.n_short)

    return RedirectResponse(f"/run/{run.id}", status_code=303)


@app.get("/run/{run_id}", response_class=HTMLResponse)
def run_result(request: Request, run_id: str):
    auth.require_user(request)
    run = runstore.get(run_id)
    if not run:
        return back("/runs", _expired(), "warn")

    results = run.results
    compare_rows = calc.compare_scenarios(results)
    params = run.params
    # The run remembers which meals it was computed with (A/B/C, plus the
    # temp one when it was used), so the page never invents a column that has
    # no result behind it.
    run_scenarios = [sc for sc in params.get("scenarios", db.SCENARIOS) if sc in results]
    totals = {sc: _totals(results.get(sc)) for sc in run_scenarios}

    # Cheapest scenario by dispatch volume. Shortfall is identical across
    # scenarios by construction (same target, same stock), so ranking by it
    # would be meaningless — see the note on the result page.
    active = {sc: t for sc, t in totals.items() if t["n_sku"]}
    best_dispatch = min(active, key=lambda s: active[s]["total_dispatch"]) if active else None

    today = db.today()
    dispatch = _run_dispatch(params, today)
    days_to_start = (
        (dispatch["start"] - today).days
        if dispatch and today < dispatch["start"]
        else None
    )

    return render(
        request,
        "result.html",
        run=run,
        results=results,
        rows=compare_rows,
        totals=totals,
        scenarios=run_scenarios,
        warnings=params.get("warnings", {}),
        share_totals=params.get("share_totals", {}),
        params=params,
        labels=_run_labels(params),
        gate=_run_gate(params),
        best_dispatch=best_dispatch,
        n_short_total=sum(1 for r in compare_rows if r["shortfall_qty"] > 0),
        today=today,
        days_to_start=days_to_start,
        keep_minutes=runstore.TTL_SECONDS // 60,
        po_any=any(t["po_qty"] for t in totals.values()),
        supply=_run_supply(params),
        dispatch=dispatch,
    )


@app.get("/run/{run_id}/detail", response_class=HTMLResponse)
def run_detail(
    request: Request,
    run_id: str,
    scenario: str = "A",
    sku: str = "",
    only_need: int = 0,
):
    auth.require_user(request)
    run = runstore.get(run_id)
    if not run:
        return back("/runs", _expired(), "warn")

    # The scenario list comes from the run, not from the database: the temp meal
    # only exists here, and a month may carry two or three regular ones.
    run_scenarios = [sc for sc in run.params.get("scenarios", db.SCENARIOS) if sc in run.results]
    scenario = scenario if scenario in run_scenarios else (run_scenarios[0] if run_scenarios else "A")
    res = run.results.get(scenario)
    skus = _sorted_skus(res)
    available = [sc for sc in run_scenarios if run.results.get(sc) and run.results[sc].skus]
    all_lines = res.lines if res else []

    if sku:
        lines = _sorted_lines([ln for ln in all_lines if ln.sku == sku])
        by_shop: List[dict] = []
        selected = next((s for s in skus if s.sku == sku), None)
        # Only the chosen SKU's pairs: the table sits under its shop list, so
        # mixing in other SKUs would read as if it belonged to this one.
        transfers = [t for t in (res.transfers if res else []) if t.sku == sku]
    else:
        lines = []
        selected = None
        transfers = []
        by_shop = _by_shop(all_lines, bool(only_need))

    return render(
        request,
        "detail.html",
        run=run,
        scenario=scenario,
        skus=skus,
        selected=selected,
        lines=lines,
        by_shop=by_shop,
        transfers=transfers,
        sku=sku,
        only_need=only_need,
        available_scenarios=available,
        scenarios=run_scenarios,
        labels=_run_labels(run.params),
        gate=_run_gate(run.params),
        supply=_run_supply(run.params),
        params=run.params,
        shop_names=run.params.get("shop_names") or {},
        keep_minutes=runstore.TTL_SECONDS // 60,
    )


@app.get("/run/{run_id}/export.xlsx")
def run_export(request: Request, run_id: str):
    auth.require_user(request)
    run = runstore.get(run_id)
    if not run:
        return back("/runs", _expired(), "warn")

    rows = calc.compare_scenarios(run.results)
    data = excel_io.export_run(
        {
            "period": run.period,
            "created_at": run.created_at,
            "created_by": run.created_by,
            "n_sku": run.n_sku,
            "n_short": run.n_short,
        },
        run.results,
        rows,
        tuple(run.results),
        labels=_run_labels(run.params),
        gate_shops=_run_gate(run.params),
        params=run.params,
    )
    stamp = db.stamp()
    return _xlsx(data, f"派貨計算_{run.period}_{stamp}.xlsx")


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    auth.require_user(request)
    return render(
        request,
        "runs.html",
        usage=db.list_usage(limit=200),
        keep_minutes=runstore.TTL_SECONDS // 60,
    )


# ---------------------------------------------------------------------------
# internal
# ---------------------------------------------------------------------------


def _expired() -> str:
    return (
        "計算結果不會保存，只在計算後保留約 "
        f"{runstore.TTL_SECONDS // 60} 分鐘（重新部署亦會清掉）。"
        "這個結果已經過期，請重新計算。"
    )


def _run_labels(params: dict) -> Dict[str, Any]:
    """Names the batch was computed with, falling back to the live settings."""
    labels = params.get("scenario_labels")
    if isinstance(labels, dict) and labels:
        return labels
    return db.scenario_labels()


def _run_dispatch(params: dict, today: date) -> Optional[dict]:
    """The single dispatch run of a calculated batch, rebuilt from its own dates.

    Rebuilt rather than recomputed from today, so an old batch still shows the
    dates it was planned for. Only one run exists: the promotion is stocked for
    its first ``dispatch_days`` days and not topped up afterwards, so anything
    beyond that window is reported as unstocked rather than as a later batch.
    """
    start = _parse_date(params.get("promo_start", ""))
    end = _parse_date(params.get("promo_end", ""))
    if not start or not end or end < start:
        return None
    try:
        days = int(params.get("dispatch_days") or 4)
    except (TypeError, ValueError):
        days = 4
    promo = calc.PromoWindow(start=start, end=end, dispatch_days=days)
    window = calc.dispatch_window(promo)
    if window is None:
        return None
    win_start, win_end = window
    uncovered = max((promo.end - win_end).days, 0)
    return {
        "start": win_start,
        "end": win_end,
        "days": promo.effective_dispatch_days,
        "active": win_start <= today <= win_end,
        "uncovered_days": uncovered,
        "uncovered_start": win_end + timedelta(days=1) if uncovered else None,
        "uncovered_end": promo.end,
    }


def _run_gate(params: dict) -> List[str]:
    """Gated shops that took part in this calculation.

    The snapshot is used rather than the live setting: showing today's gate on a
    result computed before it existed would describe a run that never applied it.
    """
    gate = params.get("rp_gate_shops")
    return gate if isinstance(gate, list) else []


def _run_supply(params: dict) -> dict:
    """Supply-source rule as this batch applied it.

    ``codes`` empty means the file carried no Supply source column and every
    row was treated as 倉貨 — the page should then explain the fallback instead
    of showing an empty rule.
    """
    codes = params.get("vendor_supply_sources")
    return {
        "codes": codes if isinstance(codes, list) else [],
        "has_column": bool(params.get("has_supply_source")),
    }


def _totals(res) -> Dict[str, int]:
    """Per-scenario sums the result page shows, straight off the live result.

    Mirrors the keys ``db.run_totals`` used to produce, so templates did not
    have to change when the results stopped being stored.
    """
    keys = (
        "target_qty", "dispatch_target", "total_stock", "total_pending",
        "warehouse_qty", "warehouse_pending", "total_dispatch", "need_qty",
        "short_qty", "blocked_qty", "expected_sales", "gap_target",
        "shortfall_qty", "surplus_qty", "po_qty", "sto_qty", "po_target",
        "topup_qty",
    )
    if res is None:
        out = {k: 0 for k in keys}
        out.update({"n_sku": 0, "n_short": 0, "transfer_qty": 0, "n_transfer": 0})
        return out

    out = {k: sum(getattr(s, k, 0) for s in res.skus) for k in keys}
    out["n_sku"] = len(res.skus)
    out["n_short"] = res.n_short
    # Transfers live beside the lines rather than on a SKU summary: they are
    # pairs of shops, and their sum is what the result page reports.
    out["transfer_qty"] = sum(t.qty for t in res.transfers)
    out["n_transfer"] = len(res.transfers)
    return out


def _sorted_skus(res) -> List[Any]:
    """Short SKUs first, then by how much they fall short — same as before."""
    if not res:
        return []
    return sorted(
        res.skus,
        key=lambda s: (0 if s.shortfall_qty > 0 else 1, -s.shortfall_qty, s.sku),
    )


def _sorted_lines(lines) -> List[Any]:
    """Shop order from the master file, so every report reads the same way."""
    order = {row["code"]: i for i, row in enumerate(db.list_shops())}
    return sorted(lines, key=lambda ln: (order.get(ln.shop_code, 9999), ln.shop_code))


def _by_shop(lines, only_need: bool) -> List[dict]:
    """Whole-scenario totals per shop, the aggregate view of the detail page."""
    agg: Dict[str, dict] = {}
    for ln in lines:
        row = agg.setdefault(
            ln.shop_code,
            {
                "shop_code": ln.shop_code,
                "alloc": 0,
                "stock": 0,
                "dispatch": 0,
                "po": 0,
                "surplus": 0,
                "n_sku": 0,
                "vendor": False,
            },
        )
        row["alloc"] += ln.alloc_qty
        row["stock"] += ln.stock_qty
        row["dispatch"] += ln.dispatch_qty
        row["po"] += ln.po_qty
        row["surplus"] += ln.surplus_qty
        row["vendor"] = row["vendor"] or ln.vendor
        if ln.dispatch_qty > 0:
            row["n_sku"] += 1

    rows = [r for r in agg.values() if not only_need or r["dispatch"] > 0]
    rows.sort(key=lambda r: (-r["dispatch"], r["shop_code"]))
    return rows


def _xlsx(data: bytes, filename: str) -> Response:
    from urllib.parse import quote

    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )
