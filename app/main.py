"""Promotion Dispatch Portal — FastAPI application."""

from __future__ import annotations

import io
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, calc, db, excel_io, version

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
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
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
    runs = db.list_runs(limit=5)
    latest = runs[0] if runs else None

    latest_totals = None
    if latest:
        latest_totals = {
            sc: db.run_totals(latest["id"], sc) for sc in db.SCENARIOS
        }

    # Optional plans (C, usually) do not make the month look "not ready".
    required = [sc for sc in db.SCENARIOS if not status[sc]["optional"]]

    return render(
        request,
        "dashboard.html",
        period=period,
        status=status,
        runs=runs,
        latest=latest,
        latest_totals=latest_totals,
        latest_labels=_run_labels(_run_params(latest)) if latest else db.scenario_labels(),
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

    notes = ["已儲存餐別名稱、貨倉與 RP Type 規則"]
    level = "ok"

    if not db.warehouse_sites():
        notes.append("貨倉代碼留空，派貨將不受倉存限制")
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
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
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
    period = period or db.current_period()
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
        periods=_period_options(period),
        storage=db.storage_status(),
    )


@app.post("/admin/shares")
async def shares_save(request: Request):
    auth.require_admin(request)
    form = await request.form()
    period = str(form.get("period", db.current_period()))
    scenario = str(form.get("scenario", "A"))
    unit = str(form.get("unit", "pct"))
    user = (auth.current_user(request) or {}).get("name", "")

    pct: Dict[str, float] = {}
    for key, value in form.items():
        if not key.startswith("pct_"):
            continue
        code = key[4:]
        number = excel_io.to_number(value)
        if number is None:
            continue
        if unit == "pct":
            number = number / 100.0
        elif abs(number) > 1.5:
            number = number / 100.0
        pct[code] = max(number, 0.0)

    db.save_shares(period, scenario, pct, user)
    total = sum(pct.values())
    level = "ok" if abs(total - 1) < 0.005 else "warn"
    label = db.scenario_settings()[scenario]["label"]
    who = f"{scenario} 餐 · {label}" if label else f"{scenario} 餐"
    note = f"{who} 已儲存，總和 {total:.2%}"
    return back(f"/admin/shares?period={period}&scenario={scenario}", note, level)


@app.post("/admin/shares/import")
async def shares_import(
    request: Request,
    period: str = Form(...),
    file: UploadFile = File(...),
):
    auth.require_admin(request)
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
    msg = f"已匯入 {period}：" + "、".join(parts)
    if created:
        msg += f"；同時新增 {created} 間店舖"
    if parsed.warnings:
        msg += "｜" + "；".join(parsed.warnings[:3])
    return back(f"/admin/shares?period={period}", msg, "ok" if not parsed.warnings else "warn")


@app.get("/admin/shares/template")
def shares_template(request: Request, period: str = ""):
    auth.require_admin(request)
    period = period or db.current_period()
    shops = [dict(s) for s in db.list_shops(active_only=True)]
    existing = db.get_all_shares(period)
    data = excel_io.export_share_template(shops, existing, period)
    return _xlsx(data, f"店舖佔比_{period}.xlsx")


def _period_options(selected: str, back_months: int = 6) -> List[str]:
    options = set(db.list_periods())
    today = datetime.now()
    year, month = today.year, today.month
    for _ in range(back_months):
        options.add(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    if selected:
        options.add(selected)
    return sorted(options, reverse=True)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@app.get("/run/new", response_class=HTMLResponse)
def run_new_page(request: Request, period: str = ""):
    user = auth.require_user(request)
    period = period or db.current_period()
    status = db.share_status(period)
    required = [sc for sc in db.SCENARIOS if not status[sc]["optional"]]
    today = date.today()
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
        target_uploads=db.list_uploads("target"),
        stock_uploads=db.list_uploads("stock"),
        periods=_period_options(period),
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


@app.post("/run/new")
async def run_create(
    request: Request,
    period: str = Form(...),
    promo_start: str = Form(""),
    promo_end: str = Form(""),
    dispatch_days: int = Form(4),
    target_file: UploadFile = File(...),
    stock_file: UploadFile = File(...),
):
    user = auth.require_user(request)
    name = user.get("name", "")

    status = db.share_status(period)
    ready = [sc for sc in db.SCENARIOS if status[sc]["ready"]]
    if not ready:
        return back(
            "/run/new",
            f"{period} 的 A/B/C 佔比尚未設定完成，請先到後台設定或在下方匯入佔比 Excel。",
            "error",
        )

    start = _parse_date(promo_start)
    end = _parse_date(promo_end)
    problems: List[str] = []
    if not start or not end:
        problems.append("請填寫推廣開始日期與完結日期")
    elif end < start:
        problems.append("完結日期不可早於開始日期")
    if dispatch_days < 1:
        problems.append("派貨日數至少 1 日")
    if problems:
        return back("/run/new", "；".join(problems), "error")

    promo = calc.PromoWindow(start=start, end=end, dispatch_days=dispatch_days)
    if promo.days <= 0:
        return back("/run/new", "推廣日數不正確，請檢查日期", "error")

    target_data = await target_file.read()
    stock_data = await stock_file.read()

    parsed_t = excel_io.parse_target_file(target_data)
    parsed_s = excel_io.parse_stock_file(stock_data, set(db.active_shop_codes()))

    if parsed_t.errors:
        problems.append("SKU Target：" + "；".join(parsed_t.errors))
    if parsed_s.errors:
        problems.append("店舖庫存：" + "；".join(parsed_s.errors))
    if problems:
        return back("/run/new", "上載檔案有問題 — " + " ｜ ".join(problems), "error")

    targets = [
        calc.SkuTarget(sku=sku, target_qty=qty, description=desc)
        for sku, desc, qty in parsed_t.rows
    ]

    # The warehouse holds stock but never receives an allocation; its net stock
    # is what limits dispatch. Pending receipts at the warehouse are inbound
    # and are deliberately not counted.
    warehouse_codes = set(db.warehouse_sites())
    stock_by_sku: Dict[str, Dict[str, int]] = {}
    rp_by_sku: Dict[str, Dict[str, str]] = {}
    sales_by_sku: Dict[str, Dict[str, calc.ShopSales]] = {}
    warehouse_by_sku: Dict[str, int] = {}
    warehouse_pending: Dict[str, int] = {}
    # MOQ is a SKU property carried on the stock file; when shops disagree the
    # largest wins (never under-order) and the conflict is reported.
    moq_by_sku: Dict[str, int] = {}
    moq_conflict: set = set()

    for row in parsed_s.rows:
        if row.moq > 1:
            prev = moq_by_sku.get(row.sku)
            if prev is None:
                moq_by_sku[row.sku] = row.moq
            elif prev != row.moq:
                moq_conflict.add(row.sku)
                moq_by_sku[row.sku] = max(prev, row.moq)
        if row.shop in warehouse_codes:
            warehouse_by_sku[row.sku] = warehouse_by_sku.get(row.sku, 0) + row.qty
            warehouse_pending[row.sku] = (
                warehouse_pending.get(row.sku, 0) + row.pending
            )
            continue
        stock_by_sku.setdefault(row.sku, {})[row.shop] = row.qty
        if row.rp_type:
            rp_by_sku.setdefault(row.sku, {})[row.shop] = row.rp_type
        sales_by_sku.setdefault(row.sku, {})[row.shop] = calc.ShopSales(
            last_month_sold=row.last_month_sold,
            mtd_sold=row.mtd_sold,
            pending_qty=row.pending,
            launch_date=row.launch_date,
        )

    has_sales = bool(parsed_s.mapped.get("last_month_sold")) and bool(
        parsed_s.mapped.get("mtd_sold")
    )

    # SKUs in the stock file but absent from the target file are simply not
    # promoted this period; flag it only when it looks like a mismatch.
    target_skus = {t.sku for t in targets}
    stock_only = sorted(set(stock_by_sku) - target_skus)
    target_only = sorted(target_skus - set(stock_by_sku))

    shares = {sc: db.get_shares(period, sc) for sc in ready}
    shops = db.active_shop_codes()
    # Only shops that actually take part in this run can gate anything; keeping
    # the filtered list in the snapshot stops the result page explaining a rule
    # that never applied.
    shop_set = set(shops)
    gate = [code for code in db.rp_gate_shops() if code in shop_set]
    has_warehouse = bool(warehouse_by_sku)

    results = calc.compute_all_scenarios(
        shares,
        shops,
        targets,
        stock_by_sku,
        rp_by_sku=rp_by_sku,
        rp_gate_shops=gate,
        sales_by_sku=sales_by_sku if has_sales else None,
        promo=promo,
        as_of=date.today(),
        warehouse_by_sku=warehouse_by_sku if has_warehouse else None,
        warehouse_pending_by_sku=warehouse_pending,
        moq_by_sku=moq_by_sku,
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

    if parsed_s.rows and not parsed_s.mapped.get("moq"):
        for res in results.values():
            res.warnings.append(
                "庫存檔沒有 MOQ 欄位，全部 SKU 當作 MOQ = 1（需求不進位）"
            )
    if parsed_t.mapped.get("moq_ignored"):
        for res in results.values():
            res.warnings.append(
                f"Target 檔的「{parsed_t.mapped['moq_ignored']}」欄已忽略 —— "
                "MOQ 改由店舖庫存檔讀取"
            )
    if moq_conflict:
        for res in results.values():
            res.warnings.append(
                f"有 {len(moq_conflict)} 個 SKU 在庫存檔不同店舖的 MOQ 不一致，"
                "已取最大值（例：" + sorted(moq_conflict)[0] + "）"
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

    target_upload = db.create_upload(
        "target",
        period,
        target_file.filename or "",
        name,
        len(parsed_t.rows),
        {"mapped": parsed_t.mapped, "warnings": parsed_t.warnings},
    )
    db.add_targets(target_upload, parsed_t.rows)

    stock_upload = db.create_upload(
        "stock",
        period,
        stock_file.filename or "",
        name,
        len(parsed_s.rows),
        {"mapped": parsed_s.mapped, "warnings": parsed_s.warnings},
    )
    db.add_stock(stock_upload, parsed_s.rows)

    run_id = db.create_run(
        period,
        target_upload,
        stock_upload,
        name,
        {
            "scenarios": ready,
            "share_totals": {sc: sum(shares[sc].values()) for sc in ready},
            "warnings": {sc: results[sc].warnings for sc in ready},
            "n_shops": len(shops),
            "target_file": target_file.filename or "",
            "stock_file": stock_file.filename or "",
            "promo_start": start.isoformat(),
            "promo_end": end.isoformat(),
            "promo_days": promo.days,
            "dispatch_days": promo.effective_dispatch_days,
            "as_of": date.today().isoformat(),
            "warehouse_sites": sorted(warehouse_codes),
            "has_sales": has_sales,
            "has_moq": bool(parsed_s.mapped.get("moq")),
            # Snapshot the names so an old batch keeps the wording it was
            # computed with, even after the labels are renamed.
            "scenario_labels": db.scenario_labels(),
            "rp_gate_shops": gate,
        },
    )
    db.save_results(run_id, results)

    return RedirectResponse(f"/run/{run_id}", status_code=303)


@app.get("/run/{run_id}", response_class=HTMLResponse)
def run_result(request: Request, run_id: int):
    auth.require_user(request)
    run = db.get_run(run_id)
    if not run:
        return back("/runs", "找不到這個計算批次", "error")

    results = _load_results(run_id)
    compare_rows = calc.compare_scenarios(results)
    # Every scenario gets an entry, so templates can iterate A/B/C safely even
    # when a month only has one plan configured.
    totals = {sc: db.run_totals(run_id, sc) for sc in db.SCENARIOS}
    params = _run_params(run)

    # Cheapest scenario by dispatch volume. Shortfall is identical across
    # scenarios by construction (same target, same stock), so ranking by it
    # would be meaningless — see the note on the result page.
    active = {sc: t for sc, t in totals.items() if t["n_sku"]}
    best_dispatch = min(active, key=lambda s: active[s]["total_dispatch"]) if active else None

    schedule = _run_schedule(params)
    today = date.today()
    days_to_start = (
        (schedule[0][1] - today).days
        if schedule and today < schedule[0][1]
        else None
    )

    return render(
        request,
        "result.html",
        run=run,
        results=results,
        rows=compare_rows,
        totals=totals,
        warnings=params.get("warnings", {}),
        share_totals=params.get("share_totals", {}),
        params=params,
        labels=_run_labels(params),
        gate=_run_gate(params),
        best_dispatch=best_dispatch,
        n_short_total=sum(1 for r in compare_rows if r["shortfall_qty"] > 0),
        schedule=schedule,
        today=today,
        days_to_start=days_to_start,
    )


@app.get("/run/{run_id}/detail", response_class=HTMLResponse)
def run_detail(
    request: Request,
    run_id: int,
    scenario: str = "A",
    sku: str = "",
    only_need: int = 0,
):
    auth.require_user(request)
    run = db.get_run(run_id)
    if not run:
        return back("/runs", "找不到這個計算批次", "error")

    scenario = scenario if scenario in db.SCENARIOS else "A"
    skus = db.get_run_skus(run_id, scenario)
    available = [sc for sc in db.SCENARIOS if db.run_totals(run_id, sc).get("n_sku")]

    if sku:
        lines = db.get_run_lines(run_id, scenario, sku)
        by_shop: List[dict] = []
        selected = next((s for s in skus if s["sku"] == sku), None)
    else:
        lines = []
        selected = None
        by_shop = [dict(r) for r in db.dispatch_by_shop(run_id, scenario, bool(only_need))]

    params = _run_params(run)

    return render(
        request,
        "detail.html",
        run=run,
        scenario=scenario,
        skus=skus,
        selected=selected,
        lines=lines,
        by_shop=by_shop,
        sku=sku,
        only_need=only_need,
        available_scenarios=available,
        labels=_run_labels(params),
        gate=_run_gate(params),
    )


@app.get("/run/{run_id}/export.xlsx")
def run_export(request: Request, run_id: int):
    auth.require_user(request)
    run = db.get_run(run_id)
    if not run:
        return back("/runs", "找不到這個計算批次", "error")

    results = _load_results(run_id, with_lines=True)
    rows = calc.compare_scenarios(results)
    params = _run_params(run)
    data = excel_io.export_run(
        dict(run),
        results,
        rows,
        tuple(results),
        labels=_run_labels(params),
        gate_shops=_run_gate(params),
        params=params,
    )
    stamp = datetime.now().strftime("%Y%m%d%H%M")
    return _xlsx(data, f"派貨計算_{run['period']}_{stamp}.xlsx")


@app.post("/run/{run_id}/delete")
def run_delete(request: Request, run_id: int):
    auth.require_admin(request)
    db.delete_run(run_id)
    return back("/runs", "已刪除批次")


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    auth.require_user(request)
    return render(request, "runs.html", runs=db.list_runs(limit=100))


# ---------------------------------------------------------------------------
# internal
# ---------------------------------------------------------------------------


def _run_params(run) -> dict:
    import json

    try:
        return json.loads(run["params_json"] or "{}")
    except (ValueError, TypeError):
        return {}


def _run_labels(params: dict) -> Dict[str, Any]:
    """Names the batch was computed with, falling back to the live settings."""
    labels = params.get("scenario_labels")
    if isinstance(labels, dict) and labels:
        return labels
    return db.scenario_labels()


def _run_schedule(params: dict) -> List[tuple]:
    """Dispatch runs for a batch, rebuilt from its own dates.

    Stored rather than recomputed from today, so an old batch still shows the
    dates it was planned for.
    """
    start = _parse_date(params.get("promo_start", ""))
    end = _parse_date(params.get("promo_end", ""))
    if not start or not end or end < start:
        return []
    try:
        days = int(params.get("dispatch_days") or 4)
    except (TypeError, ValueError):
        days = 4
    return calc.dispatch_batches(
        calc.PromoWindow(start=start, end=end, dispatch_days=days)
    )


def _run_gate(params: dict) -> List[str]:
    """Gated shops that took part in this batch.

    Batches computed before the rule existed have no key, and must not inherit
    today's configuration — that would describe a run that never applied it.
    """
    gate = params.get("rp_gate_shops")
    return gate if isinstance(gate, list) else []


def _load_results(run_id: int, with_lines: bool = False) -> Dict[str, calc.ScenarioResult]:
    """Rebuild per-SKU results from storage.

    ``with_lines`` also reloads the per-shop rows, which the export needs but the
    summary pages do not.
    """
    run = db.get_run(run_id)
    params = _run_params(run)
    totals = params.get("share_totals", {})
    warnings = params.get("warnings", {})

    out: Dict[str, calc.ScenarioResult] = {}
    for sc in db.SCENARIOS:
        rows = db.get_run_skus(run_id, sc)
        if not rows:
            continue
        skus = [
            calc.SkuSummary(
                sku=r["sku"],
                description=r["description"],
                target_qty=r["target_qty"],
                total_alloc=r["total_alloc"],
                total_stock=r["total_stock"],
                total_dispatch=r["total_dispatch"],
                surplus_qty=r["surplus_qty"],
                shortfall_qty=r["shortfall_qty"],
                n_shops_needing=r["n_shops_needing"],
                coverage_status=r["coverage_status"],
                moq=r["moq"],
                dispatch_target=r["dispatch_target"] or 0,
                total_pending=r["total_pending"] or 0,
                warehouse_qty=r["warehouse_qty"] or 0,
                warehouse_pending=r["warehouse_pending"] or 0,
                daily_rate=r["daily_rate"] or 0.0,
                expected_sales=r["expected_sales"] or 0,
                gap_target=r["gap_target"] or 0,
                need_qty=r["need_qty"] or 0,
                short_qty=r["short_qty"] or 0,
                blocked_qty=r["blocked_qty"] or 0,
                gap_supply=r["gap_supply"] or 0,
            )
            for r in rows
        ]
        lines = (
            [
                calc.Line(
                    sku=r["sku"],
                    shop_code=r["shop_code"],
                    share_pct=r["share_pct"],
                    alloc_qty=r["alloc_qty"],
                    stock_qty=r["stock_qty"],
                    gap_qty=r["gap_qty"],
                    dispatch_qty=r["dispatch_qty"],
                    surplus_qty=r["surplus_qty"],
                    rp_type=r["rp_type"] or "",
                    blocked=bool(r["blocked"]),
                    blocked_qty=r["blocked_qty"] or 0,
                    pending_qty=r["pending_qty"] or 0,
                    daily_rate=r["daily_rate"] or 0.0,
                    expected_sales=r["expected_sales"] or 0,
                    need_qty=r["need_qty"] or 0,
                    short_qty=r["short_qty"] or 0,
                )
                for r in db.get_scenario_lines(run_id, sc)
            ]
            if with_lines
            else []
        )
        out[sc] = calc.ScenarioResult(
            scenario=sc,
            lines=lines,
            skus=skus,
            share_total=float(totals.get(sc, 1.0)),
            warnings=list(warnings.get(sc, [])),
        )
    return out


def _xlsx(data: bytes, filename: str) -> Response:
    from urllib.parse import quote

    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )
