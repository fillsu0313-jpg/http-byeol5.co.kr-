"""
FastAPI 앱 + 라우터
"""
import csv
import io
import tempfile
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from datetime import date, timedelta, datetime
import calendar
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


def fmt_short(value):
    """축약 숫자. 1만 이상이면 '만' 단위, 1천 이상이면 '천' 단위."""
    if value is None:
        return "-"
    v = round(value)
    abs_v = abs(v)
    sign = "-" if v < 0 else "+"
    if abs_v >= 10000:
        return f"{sign}{abs_v // 10000}만"
    if abs_v >= 1000:
        return f"{sign}{abs_v // 1000}천"
    return f"{sign}{abs_v:,}"


templates.env.filters["fmt"] = fmt_number
templates.env.filters["fmt_short"] = fmt_short


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


@app.get("/period", response_class=HTMLResponse)
async def period_analysis(request: Request, period: Optional[str] = None,
                          from_date: Optional[str] = None, to_date: Optional[str] = None):
    today = date.today()

    if period == "week":
        # 이번주 (월요일~일요일)
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif period == "last_month":
        first_this = today.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        start = last_month_end.replace(day=1)
        end = last_month_end
    elif period == "custom" and from_date and to_date:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
    else:
        # 기본: 이번 달
        period = "month"
        start = today.replace(day=1)
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])

    from_str = start.isoformat()
    to_str = end.isoformat()

    summary = db.get_period_summary(from_str, to_str)
    ranking = db.get_period_ranking(from_str, to_str)
    daily_totals = db.get_daily_totals(from_str, to_str)

    # 캘린더 그리드 생성 (start~end 범위의 모든 날짜)
    daily_map = {d["stat_date"]: d for d in daily_totals}

    # 캘린더 주 단위 배열 구성
    cal_start = start - timedelta(days=start.weekday())  # 월요일로 정렬
    cal_end = end + timedelta(days=(6 - end.weekday()))   # 일요일로 정렬
    weeks = []
    current = cal_start
    while current <= cal_end:
        week = []
        for _ in range(7):
            in_range = start <= current <= end
            day_data = daily_map.get(current.isoformat())
            week.append({
                "date": current,
                "date_str": current.isoformat(),
                "in_range": in_range,
                "is_today": current == today,
                "profit": day_data["total_profit"] if day_data else None,
                "units": day_data["total_units"] if day_data else None,
                "count": day_data["product_count"] if day_data else None,
            })
            current += timedelta(days=1)
        weeks.append(week)

    # Best / Worst 분리
    best = [r for r in ranking if r.get("net_profit") is not None and r["net_profit"] > 0][:10]
    worst_all = [r for r in ranking if r.get("net_profit") is not None and r["net_profit"] < 0]
    worst = list(reversed(worst_all))[:10]

    return templates.TemplateResponse(request, "period.html", {
        "period": period,
        "from_date": from_str,
        "to_date": to_str,
        "start_display": start.strftime("%Y.%m.%d"),
        "end_display": end.strftime("%Y.%m.%d"),
        "summary": summary,
        "weeks": weeks,
        "best": best,
        "worst": worst,
        "today": today.isoformat(),
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
async def cost_sheet(request: Request, tab: Optional[str] = None, vid: Optional[int] = None):
    """원가 관리 — 스프레드시트 + 이력 조회"""
    all_costs = db.get_all_latest_costs()
    # 이력 조회 탭
    history_costs = []
    history_product = None
    if tab == "history" and vid:
        history_costs = db.get_product_costs(vid)
        history_product = db.get_product(vid)
    return templates.TemplateResponse(request, "cost_sheet.html", {
        "all_costs": all_costs,
        "tab": tab or "sheet",
        "today": date.today().isoformat(),
        "history_vid": vid,
        "history_costs": history_costs,
        "history_product": history_product,
    })


@app.get("/costs/bulk")
async def cost_bulk_redirect():
    """하위호환: /costs/bulk → /costs 리다이렉트"""
    return RedirectResponse(url="/costs", status_code=301)


@app.get("/api/costs/csv")
async def api_costs_csv():
    """전 상품 최신 원가 CSV 다운로드"""
    all_costs = db.get_all_latest_costs()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "vendor_item_id", "상품명", "적용시작일", "판매가",
        "위안단가", "환율", "매입원가", "판매수수료",
        "입출고수수료", "기타비용", "메모", "개당순마진",
    ])
    for r in all_costs:
        writer.writerow([
            r["vendor_item_id"], r["display_name"],
            r.get("effective_from") or "",
            r.get("sale_price") or "",
            r.get("purchase_cost_fx") or "",
            r.get("fx_rate") or "",
            r.get("purchase_cost") or "",
            r.get("commission_fee") or "",
            r.get("fulfillment_fee") or "",
            r.get("other_unit_cost") or "",
            r.get("memo") or "",
            r.get("unit_margin") or "",
        ])
    buf = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    return StreamingResponse(
        buf,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="cost_master.csv"'},
    )


@app.post("/api/costs/csv")
async def api_upload_costs_csv(request: Request):
    """CSV 업로드 → 파싱 → bulk_upsert_costs"""
    data = await request.json()
    rows = data.get("rows", [])
    if not rows:
        raise HTTPException(400, "저장할 데이터가 없습니다")
    result = db.bulk_upsert_costs(rows)
    return JSONResponse(content=result)


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
