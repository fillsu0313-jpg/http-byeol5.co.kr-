"""
쿠팡 API 응답 → DB 삽입 수집기
"""
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.coupang_api import CoupangAPI
from src.db import get_db

BASE_DIR = Path(__file__).resolve().parent.parent


def collect_sales(
    api: CoupangAPI,
    date_from: str,
    date_to: str,
) -> dict:
    """
    revenue-history 조회 → daily_sales INSERT OR REPLACE.
    반환: {"inserted": int, "errors": list[str]}
    """
    result = {"inserted": 0, "errors": []}

    try:
        items = api.get_revenue_history(date_from, date_to)
    except Exception as e:
        result["errors"].append(f"API 호출 실패: {e}")
        _log_ingest(date_from, "sales", "api", 0, "failed", str(e))
        return result

    # vendorItemId + 날짜별 집계
    daily = defaultdict(lambda: {"units_sold": 0, "gross_revenue": 0.0})
    for item in items:
        vid = item.get("vendorItemId")
        # 날짜 형식: API에서 YYYY-MM-DD 또는 timestamp
        ordered_date = item.get("recognizedAt", item.get("orderedDate", ""))
        if not vid or not ordered_date:
            continue
        # 날짜만 추출 (YYYY-MM-DD)
        stat_date = str(ordered_date)[:10]
        key = (stat_date, vid)
        daily[key]["units_sold"] += int(item.get("quantity", 0))
        daily[key]["gross_revenue"] += float(item.get("revenue", 0))

    with get_db() as conn:
        for (stat_date, vid), vals in daily.items():
            # 상품이 등록되어 있는지 확인
            exists = conn.execute(
                "SELECT 1 FROM products WHERE vendor_item_id = ?", (vid,)
            ).fetchone()
            if not exists:
                result["errors"].append(
                    f"미등록 상품 건너뜀: vendor_item_id={vid}"
                )
                continue
            conn.execute(
                """INSERT OR REPLACE INTO daily_sales
                   (stat_date, vendor_item_id, units_sold, gross_revenue, source)
                   VALUES (?, ?, ?, ?, 'api')""",
                (stat_date, vid, vals["units_sold"], vals["gross_revenue"]),
            )
            result["inserted"] += 1

    _log_ingest(
        date_from, "sales", "api",
        result["inserted"],
        "ok" if not result["errors"] else "partial",
        "; ".join(result["errors"][:5]) if result["errors"] else None,
    )
    return result


def collect_orders(
    api: CoupangAPI,
    date_from: str,
    date_to: str,
    status: str = "FINAL_DELIVERY",
) -> dict:
    """
    ordersheets 조회 → daily_sales.units_sold 업데이트.
    주문 데이터로 판매량을 보정하거나 보충할 때 사용.
    반환: {"updated": int, "errors": list[str]}
    """
    result = {"updated": 0, "errors": []}

    try:
        items = api.get_order_sheets(date_from, date_to, status)
    except Exception as e:
        result["errors"].append(f"API 호출 실패: {e}")
        return result

    # vendorItemId + 날짜별 집계
    daily = defaultdict(int)
    for item in items:
        vid = item.get("vendorItemId")
        ordered_at = item.get("orderedAt", item.get("createdAt", ""))
        if not vid or not ordered_at:
            continue
        stat_date = str(ordered_at)[:10]
        daily[(stat_date, vid)] += int(item.get("shippingCount", 1))

    with get_db() as conn:
        for (stat_date, vid), units in daily.items():
            exists = conn.execute(
                "SELECT 1 FROM products WHERE vendor_item_id = ?", (vid,)
            ).fetchone()
            if not exists:
                continue
            # daily_sales에 행이 있으면 units_sold만 갱신, 없으면 삽입
            row = conn.execute(
                "SELECT 1 FROM daily_sales WHERE stat_date = ? AND vendor_item_id = ?",
                (stat_date, vid),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE daily_sales SET units_sold = ?
                       WHERE stat_date = ? AND vendor_item_id = ?""",
                    (units, stat_date, vid),
                )
            else:
                conn.execute(
                    """INSERT INTO daily_sales
                       (stat_date, vendor_item_id, units_sold, source)
                       VALUES (?, ?, ?, 'api')""",
                    (stat_date, vid, units),
                )
            result["updated"] += 1

    return result


def _log_ingest(
    stat_date: str,
    data_type: str,
    source: str,
    row_count: int,
    status: str,
    message: Optional[str] = None,
):
    """ingest_log 기록"""
    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO ingest_log
                   (stat_date, data_type, source, row_count, status, message)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (stat_date, data_type, source, row_count, status, message),
            )
    except Exception:
        pass  # 로그 실패로 메인 흐름 중단 방지
