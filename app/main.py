"""Promotion Dispatch Portal — FastAPI application."""

from __future__ import annotations

import io
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, calc, db, excel_io

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
    return {"ok": True, "time": db.now()}


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

    return render(
        request,
        "dashboard.html",
        period=period,
        status=status,
        runs=runs,
        latest=latest,
        latest_totals=latest_totals,
        n_shops=len(db.active_shop_codes()),
        ready_all=all(v["ready"] for v in status.values()),
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
):
    auth.require_admin(request)
    if not code.strip():
        return back("/admin/shops", "店舖代碼不可空白", "error")
    db.upsert_shop(code, name, region)
    return back("/admin/shops", f"已新增店舖 {code.strip().upper()}")


@app.post("/admin/shops/{shop_id}")
def shop_update(
    request: Request,
    shop_id: int,
    name: str = Form(""),
    region: str = Form("HK"),
    active: str = Form("0"),
):
    auth.require_admin(request)
    db.update_shop(shop_id, name, region, 1 if active == "1" else 0)
    return back("/admin/shops", "已更新")


@app.post("/admin/shops/{shop_id}/delete")
def shop_delete(request: Request, shop_id: int):
    auth.require_admin(request)
    db.delete_shop(shop_id)
    return back("/admin/shops", "已刪除店舖")


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
    note = f"{scenario} 餐已儲存，總和 {total:.2%}"
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
            db.upsert_shop(code, parsed.names.get(code, ""), parsed.regions.get(code, "HK"))
            known.add(code)
            created += 1

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
    return render(
        request,
        "upload.html",
        period=period,
        status=status,
        ready=any(v["ready"] for v in status.values()),
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

    target_data = await target_file.read()
    stock_data = await stock_file.read()

    parsed_t = excel_io.parse_target_file(target_data)
    parsed_s = excel_io.parse_stock_file(stock_data, set(db.active_shop_codes()))

    problems: List[str] = []
    if parsed_t.errors:
        problems.append("SKU Target：" + "；".join(parsed_t.errors))
    if parsed_s.errors:
        problems.append("店舖庫存：" + "；".join(parsed_s.errors))
    if problems:
        return back("/run/new", "上載檔案有問題 — " + " ｜ ".join(problems), "error")

    targets = [
        calc.SkuTarget(sku=sku, target_qty=qty, moq=moq, description=desc)
        for sku, desc, qty, moq in parsed_t.rows
    ]
    stock_by_sku: Dict[str, Dict[str, int]] = {}
    for sku, shop, qty in parsed_s.rows:
        stock_by_sku.setdefault(sku, {})[shop] = qty

    # SKUs in the stock file but absent from the target file are simply not
    # promoted this period; flag it only when it looks like a mismatch.
    target_skus = {t.sku for t in targets}
    stock_only = sorted(set(stock_by_sku) - target_skus)
    target_only = sorted(target_skus - set(stock_by_sku))

    shares = {sc: db.get_shares(period, sc) for sc in ready}
    shops = db.active_shop_codes()
    results = calc.compute_all_scenarios(shares, shops, targets, stock_by_sku)

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
    totals = {sc: db.run_totals(run_id, sc) for sc in results}
    params = _run_params(run)

    # Cheapest scenario by dispatch volume. Shortfall is identical across
    # scenarios by construction (same target, same stock), so ranking by it
    # would be meaningless — see the note on the result page.
    active = {sc: t for sc, t in totals.items() if t["n_sku"]}
    best_dispatch = min(active, key=lambda s: active[s]["total_dispatch"]) if active else None

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
        best_dispatch=best_dispatch,
        n_short_total=sum(1 for r in compare_rows if r["shortfall_qty"] > 0),
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
    )


@app.get("/run/{run_id}/export.xlsx")
def run_export(request: Request, run_id: int):
    auth.require_user(request)
    run = db.get_run(run_id)
    if not run:
        return back("/runs", "找不到這個計算批次", "error")

    results = _load_results(run_id)
    rows = calc.compare_scenarios(results)
    data = excel_io.export_run(dict(run), results, rows, tuple(results))
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


def _load_results(run_id: int) -> Dict[str, calc.ScenarioResult]:
    """Rebuild per-SKU results from storage (lines are queried only when needed)."""
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
            )
            for r in rows
        ]
        out[sc] = calc.ScenarioResult(
            scenario=sc,
            lines=[],
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
