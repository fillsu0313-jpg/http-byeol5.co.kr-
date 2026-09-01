"""
FastAPI 앱 + 라우터
"""
import tempfile
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from datetime import date
from typing import Optional

from src import db

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="쿠팡 순이익 대시보드")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def fmt_number(value):
    """천단위 콤마. None → '-'. 정수에 가까운 float은 정수로 표시."""
    if value is None:
        return "-"
    if isinstance(value, float):
        rounded = round(value)
        if abs(value - rounded) < 0.01:
            return f"{rounded:,}"
        return f"{value:,.1f}"
    return f"{value:,}"


templates.env.filters["fmt"] = fmt_number


# ──────────────── 페이지 라우트 ────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, date: Optional[str] = None):
    dates = db.get_available_dates()
    selected = date if date else (dates[0] if dates else None)
    rows = db.get_daily_profit(selected) if selected else []

    # 합계 계산
    totals = {
        "units_sold": 0, "ad_units": 0, "organic_units": 0,
        "total_views": 0, "ad_clicks": 0, "organic_views": 0,
        "ad_spend": 0.0, "net_profit": 0.0,
    }
    has_data = False
    for r in rows:
        has_data = True
        for k in totals:
            if r.get(k) is not None:
                totals[k] += r[k]

    return templates.TemplateResponse(request, "dashboard.html", {
        "dates": dates,
        "selected_date": selected,
        "rows": rows,
        "totals": totals if has_data else None,
    })


@app.get("/product/{vendor_item_id}", response_class=HTMLResponse)
async def product_detail(request: Request, vendor_item_id: int):
    product = db.get_product(vendor_item_id)
    if not product:
        raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")
    costs = db.get_product_costs(vendor_item_id)
    daily = db.get_product_daily(vendor_item_id)
    return templates.TemplateResponse(request, "product_detail.html", {
        "product": product,
        "costs": costs,
        "daily": daily,
    })


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    logs = db.get_ingest_logs("ads", limit=10)
    return templates.TemplateResponse(request, "upload.html", {
        "logs": logs,
    })


@app.post("/upload/ads")
async def upload_ads(file: UploadFile = File(...)):
    """광고 엑셀 파일 업로드 → 파싱 → DB 저장"""
    if not file.filename.endswith((".xlsx", ".xls")):
        return JSONResponse(
            content={"ok": False, "error": "xlsx 또는 xls 파일만 업로드 가능합니다."},
            status_code=400,
        )
    from src.ad_parser import parse_ad_report, save_ad_data

    # 임시 파일에 저장 후 파싱
    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        parsed = parse_ad_report(tmp_path)
        if not parsed["rows"]:
            return JSONResponse(content={
                "ok": False,
                "error": "파싱된 데이터가 없습니다.",
                "warnings": parsed["warnings"],
            })
        result = save_ad_data(parsed["rows"])
        return JSONResponse(content={
            "ok": True,
            "inserted": result["inserted"],
            "skipped": result["skipped"],
            "warnings": parsed["warnings"] + result["errors"][:10],
        })
    except Exception as e:
        return JSONResponse(
            content={"ok": False, "error": str(e)},
            status_code=500,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.get("/costs", response_class=HTMLResponse)
async def cost_manage(request: Request, vid: Optional[int] = None):
    products = db.get_products()
    selected_vid = vid if vid else (products[0]["vendor_item_id"] if products else None)
    costs = db.get_product_costs(selected_vid) if selected_vid else []
    selected_product = None
    if selected_vid:
        selected_product = db.get_product(selected_vid)
    return templates.TemplateResponse(request, "cost_manage.html", {
        "products": products,
        "selected_vid": selected_vid,
        "selected_product": selected_product,
        "costs": costs,
    })


# ──────────────── API 라우트 ────────────────

@app.get("/api/daily")
async def api_daily(date: str):
    rows = db.get_daily_profit(date)
    return JSONResponse(content=rows)


@app.get("/api/product/{vendor_item_id}/history")
async def api_product_history(
    vendor_item_id: int,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
):
    daily = db.get_product_daily(vendor_item_id, from_date, to_date)
    return JSONResponse(content=daily)


@app.get("/api/costs/{vendor_item_id}")
async def api_get_costs(vendor_item_id: int):
    costs = db.get_product_costs(vendor_item_id)
    return JSONResponse(content=costs)


@app.post("/api/costs")
async def api_add_cost(request: Request):
    data = await request.json()
    required = ["vendor_item_id", "effective_from", "sale_price", "purchase_cost"]
    for field in required:
        if field not in data:
            raise HTTPException(400, f"필수 필드 누락: {field}")
    cost_id = db.upsert_product_cost(
        vendor_item_id=int(data["vendor_item_id"]),
        effective_from=data["effective_from"],
        sale_price=float(data["sale_price"]),
        purchase_cost=float(data["purchase_cost"]),
        commission_fee=float(data.get("commission_fee", 0)),
        fulfillment_fee=float(data.get("fulfillment_fee", 0)),
        other_unit_cost=float(data.get("other_unit_cost", 0)),
        purchase_cost_fx=float(data["purchase_cost_fx"]) if data.get("purchase_cost_fx") else None,
        fx_rate=float(data["fx_rate"]) if data.get("fx_rate") else None,
        memo=data.get("memo"),
    )
    return JSONResponse(content={"id": cost_id, "ok": True})


@app.put("/api/costs/{cost_id}")
async def api_update_cost(cost_id: int, request: Request):
    data = await request.json()
    ok = db.update_product_cost(cost_id, **data)
    if not ok:
        raise HTTPException(400, "수정할 필드가 없습니다")
    return JSONResponse(content={"ok": True})


@app.delete("/api/costs/{cost_id}")
async def api_delete_cost(cost_id: int):
    ok = db.delete_product_cost(cost_id)
    if not ok:
        raise HTTPException(404, "원가를 찾을 수 없습니다")
    return JSONResponse(content={"ok": True})
