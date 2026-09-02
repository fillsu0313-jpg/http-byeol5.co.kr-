"""
FastAPI 앱 + 라우터
"""
import io
import tempfile
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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

    # 전일 대비 변화 계산
    prev_date = db.get_prev_date(selected) if selected else None
    prev_map = db.get_daily_profit_map(prev_date) if prev_date else {}
    for r in rows:
        vid = r["vendor_item_id"]
        prev = prev_map.get(vid)
        if prev and r.get("net_profit") is not None and prev.get("net_profit") is not None:
            r["prev_profit"] = prev["net_profit"]
            r["profit_change"] = r["net_profit"] - prev["net_profit"]
        else:
            r["prev_profit"] = None
            r["profit_change"] = None

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

    # 평균 전환율
    conv_sum, conv_count = 0.0, 0
    for r in rows:
        if r.get("conversion_rate") is not None:
            conv_sum += r["conversion_rate"]
            conv_count += 1
    totals["avg_conversion"] = round(conv_sum / conv_count, 2) if conv_count else None

    # 합계 전일 대비
    prev_total = sum(p.get("net_profit", 0) or 0 for p in prev_map.values())
    totals["profit_change"] = totals["net_profit"] - prev_total if prev_map else None

    # 수집 상태
    collection = db.get_collection_status()

    return templates.TemplateResponse(request, "dashboard.html", {
        "dates": dates,
        "selected_date": selected,
        "prev_date": prev_date,
        "rows": rows,
        "totals": totals if has_data else None,
        "collection": collection,
    })


@app.get("/product/{vendor_item_id}", response_class=HTMLResponse)
async def product_detail(request: Request, vendor_item_id: int,
                         period: Optional[str] = None,
                         from_date: Optional[str] = None,
                         to_date: Optional[str] = None):
    product = db.get_product(vendor_item_id)
    if not product:
        raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")

    # 기간 계산
    import datetime
    today = datetime.date.today()
    if period == "7d":
        from_date = (today - datetime.timedelta(days=7)).isoformat()
        to_date = today.isoformat()
    elif period == "30d":
        from_date = (today - datetime.timedelta(days=30)).isoformat()
        to_date = today.isoformat()
    elif period == "90d":
        from_date = (today - datetime.timedelta(days=90)).isoformat()
        to_date = today.isoformat()
    # period == "all" or custom from/to → 그대로 사용

    costs = db.get_product_costs(vendor_item_id)
    daily = db.get_product_daily(vendor_item_id, from_date, to_date)
    notes = db.get_change_notes(vendor_item_id)

    # 쿠팡 상품 링크 (productId 기반)
    coupang_link = None
    if product.get("product_id"):
        coupang_link = f"https://www.coupang.com/vp/products/{product['product_id']}"

    return templates.TemplateResponse(request, "product_detail.html", {
        "product": product,
        "costs": costs,
        "daily": daily,
        "notes": notes,
        "coupang_link": coupang_link,
        "period": period or "all",
        "from_date": from_date or "",
        "to_date": to_date or "",
    })


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request, tab: Optional[str] = None):
    ads_logs = db.get_ingest_logs("ads", limit=10)
    cost_logs = db.get_ingest_logs("costs", limit=10)
    return templates.TemplateResponse(request, "upload.html", {
        "ads_logs": ads_logs,
        "cost_logs": cost_logs,
        "active_tab": tab or "ads",
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


@app.post("/upload/costs")
async def upload_costs(file: UploadFile = File(...)):
    """원가 엑셀 파일 업로드 → 파싱 → DB 저장"""
    if not file.filename.endswith((".xlsx", ".xls")):
        return JSONResponse(
            content={"ok": False, "error": "xlsx 또는 xls 파일만 업로드 가능합니다."},
            status_code=400,
        )
    from src.cost_parser import parse_cost_excel, save_cost_data

    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        parsed = parse_cost_excel(tmp_path)
        if not parsed["rows"]:
            return JSONResponse(content={
                "ok": False,
                "error": "파싱된 데이터가 없습니다.",
                "warnings": parsed["warnings"],
            })
        result = save_cost_data(parsed["rows"])
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
    products = db.get_products_with_cost_status()
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


@app.get("/costs/bulk", response_class=HTMLResponse)
async def cost_bulk(request: Request, filter: Optional[str] = None, q: Optional[str] = None):
    """원가 일괄 입력 페이지"""
    products = db.get_products_with_cost_status()
    if filter == "no_cost":
        products = [p for p in products if p["cost_count"] == 0]
    if q:
        q_lower = q.lower()
        products = [p for p in products if q_lower in (p.get("display_name") or "").lower()]
    return templates.TemplateResponse(request, "cost_bulk.html", {
        "products": products,
        "filter": filter or "all",
        "q": q or "",
        "today": date.today().isoformat(),
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


@app.post("/api/costs/bulk")
async def api_bulk_costs(request: Request):
    """원가 일괄 등록/수정"""
    data = await request.json()
    rows = data.get("rows", [])
    if not rows:
        raise HTTPException(400, "저장할 데이터가 없습니다")
    result = db.bulk_upsert_costs(rows)
    return JSONResponse(content=result)


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


# ──────────────── 메모 API ────────────────

@app.post("/api/notes")
async def api_add_note(request: Request):
    data = await request.json()
    for field in ("vendor_item_id", "note_date", "note"):
        if not data.get(field):
            raise HTTPException(400, f"필수 필드 누락: {field}")
    note_id = db.add_change_note(
        vendor_item_id=int(data["vendor_item_id"]),
        note_date=data["note_date"],
        change_type=data.get("change_type", "기타"),
        note=data["note"],
    )
    return JSONResponse(content={"id": note_id, "ok": True})


@app.delete("/api/notes/{note_id}")
async def api_delete_note(note_id: int):
    ok = db.delete_change_note(note_id)
    if not ok:
        raise HTTPException(404, "메모를 찾을 수 없습니다")
    return JSONResponse(content={"ok": True})


# ──────────────── 엑셀 다운로드 ────────────────

@app.get("/download/daily")
async def download_daily(date: str):
    """일별 대시보드 엑셀 다운로드"""
    import openpyxl
    rows = db.get_daily_profit(date)
    if not rows:
        raise HTTPException(404, "데이터가 없습니다")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = date
    headers = ["상품명", "vendorItemId", "판매량", "광고판매", "자연판매",
               "총조회수", "광고클릭수", "자연조회수", "전환율(%)",
               "광고비", "개당순마진", "일순이익"]
    ws.append(headers)
    for r in rows:
        ws.append([
            r.get("display_name"), r.get("vendor_item_id"),
            r.get("units_sold"), r.get("ad_units"), r.get("organic_units"),
            r.get("total_views"), r.get("ad_clicks"), r.get("organic_views"),
            r.get("conversion_rate"),
            r.get("ad_spend"), r.get("unit_margin"), r.get("net_profit"),
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"daily_{date}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/download/product/{vendor_item_id}")
async def download_product(vendor_item_id: int,
                           from_date: Optional[str] = None,
                           to_date: Optional[str] = None):
    """상품별 상세 엑셀 다운로드"""
    import openpyxl
    product = db.get_product(vendor_item_id)
    if not product:
        raise HTTPException(404, "상품을 찾을 수 없습니다")
    daily = db.get_product_daily(vendor_item_id, from_date, to_date)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = str(product.get("display_name", ""))[:31]
    headers = ["날짜", "판매량", "광고판매", "자연판매", "총조회수",
               "광고클릭수", "자연조회수", "전환율(%)",
               "광고비", "개당순마진", "일순이익"]
    ws.append(headers)
    for d in daily:
        ws.append([
            d.get("stat_date"), d.get("units_sold"),
            d.get("ad_units"), d.get("organic_units"),
            d.get("total_views"), d.get("ad_clicks"), d.get("organic_views"),
            d.get("conversion_rate"),
            d.get("ad_spend"), d.get("unit_margin"), d.get("net_profit"),
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = product.get("display_name", vendor_item_id)
    filename = f"product_{vendor_item_id}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
