"""
광고센터 엑셀 보고서 파서
쿠팡 광고센터에 공개 API가 없으므로 수동 다운로드한 엑셀을 파싱.

엑셀 구조 (캠페인 > 광고그룹 > 상품 일별):
  날짜 | ... | 광고집행 옵션ID | ... | 노출수 | 클릭수 | 광고비 | ... | 총 판매수량(14일) | ...

같은 (날짜, 옵션ID)에 검색/비검색 등 여러 행이 있으므로 합산.
"""
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional

import openpyxl

from src.db import get_db


def parse_ad_report(file_path: str) -> dict:
    """
    광고센터 엑셀 보고서 → 파싱 결과 반환.

    반환: {"rows": list[dict], "warnings": list[str]}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {file_path}")

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb.active
    rows_raw = list(ws.iter_rows(values_only=True))
    wb.close()

    warnings = []

    if len(rows_raw) < 2:
        warnings.append("데이터 행이 없습니다.")
        return {"rows": [], "warnings": warnings}

    # 헤더 파싱
    header = [str(h).strip() if h else "" for h in rows_raw[0]]
    col_map = {}
    for i, h in enumerate(header):
        if h == "날짜":
            col_map["date"] = i
        elif h == "광고집행 옵션ID":
            col_map["vid"] = i
        elif h == "노출수":
            col_map["impressions"] = i
        elif h == "클릭수":
            col_map["clicks"] = i
        elif h == "광고비":
            col_map["spend"] = i
        elif h == "총 판매수량(14일)":
            col_map["units_14d"] = i

    missing = {"date", "vid", "impressions", "clicks", "spend"} - set(col_map.keys())
    if missing:
        warnings.append(f"필수 컬럼 누락: {missing}. 헤더: {header[:10]}")
        return {"rows": [], "warnings": warnings}

    # (날짜, 옵션ID) 기준 합산
    agg = defaultdict(lambda: {"impressions": 0, "ad_clicks": 0, "ad_spend": 0.0, "ad_units": 0})
    skipped = 0

    for row in rows_raw[1:]:
        try:
            stat_date = _parse_date(row[col_map["date"]])
        except (ValueError, TypeError, IndexError):
            skipped += 1
            continue

        if not stat_date:
            skipped += 1
            continue

        vid_raw = row[col_map["vid"]]
        if not vid_raw:
            skipped += 1
            continue
        try:
            vid = int(float(vid_raw))
        except (ValueError, TypeError):
            skipped += 1
            continue

        key = (stat_date, vid)
        agg[key]["impressions"] += int(_parse_num(row[col_map["impressions"]]))
        agg[key]["ad_clicks"] += int(_parse_num(row[col_map["clicks"]]))
        agg[key]["ad_spend"] += _parse_num(row[col_map["spend"]])
        if "units_14d" in col_map:
            agg[key]["ad_units"] += int(_parse_num(row[col_map["units_14d"]]))

    if skipped:
        warnings.append(f"건너뛴 행: {skipped}개")

    # dict → list
    rows_data = []
    for (stat_date, vid), d in agg.items():
        rows_data.append({
            "stat_date": stat_date,
            "vendor_item_id": vid,
            "impressions": d["impressions"],
            "ad_clicks": d["ad_clicks"],
            "ad_spend": d["ad_spend"],
            "ad_units": d["ad_units"],
        })

    return {"rows": rows_data, "warnings": warnings}


def save_ad_data(rows: list[dict]) -> dict:
    """파싱된 광고 데이터 → daily_ads INSERT OR REPLACE"""
    result = {"inserted": 0, "skipped": 0, "errors": []}

    with get_db() as conn:
        for row in rows:
            conn.execute(
                """INSERT OR REPLACE INTO daily_ads
                   (stat_date, vendor_item_id, impressions, ad_clicks, ad_units, ad_spend, source)
                   VALUES (?, ?, ?, ?, ?, ?, 'upload')""",
                (
                    row["stat_date"],
                    row["vendor_item_id"],
                    row["impressions"],
                    row["ad_clicks"],
                    row["ad_units"],
                    row["ad_spend"],
                ),
            )
            result["inserted"] += 1

        # ingest_log
        if result["inserted"] > 0:
            stat_dates = sorted(set(r["stat_date"] for r in rows if r["stat_date"]))
            date_range = f"{stat_dates[0]}~{stat_dates[-1]}" if stat_dates else ""
            conn.execute(
                """INSERT INTO ingest_log
                   (stat_date, data_type, source, row_count, status, message)
                   VALUES (?, 'ads', 'upload', ?, 'ok', ?)""",
                (
                    stat_dates[0] if stat_dates else None,
                    result["inserted"],
                    f"기간 {date_range}, {result['inserted']}건",
                ),
            )

    return result


def _parse_date(raw) -> Optional[str]:
    """다양한 날짜 형식 → 'YYYY-MM-DD'"""
    from datetime import datetime, date

    if isinstance(raw, date):
        return raw.strftime("%Y-%m-%d")
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")

    # 숫자형 (20260801 또는 20260801.0)
    if isinstance(raw, (int, float)):
        s = str(int(raw))
        if len(s) == 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"

    s = str(raw).strip()

    # "20260801" 문자열
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"

    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_num(val) -> float:
    """숫자 파싱. None/빈값 → 0"""
    if val is None:
        return 0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0
