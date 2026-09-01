"""
광고센터 엑셀 보고서 파서
쿠팡 광고센터에 공개 API가 없으므로 수동 다운로드한 엑셀을 파싱.

TODO: 광고 보고서 원본 샘플 수령 후 컬럼명·시트명 확인 필요
      현재 스키마는 추정 기반.
"""
import sqlite3
from pathlib import Path
from typing import Optional

import openpyxl

from src.db import get_db


def parse_ad_report(file_path: str) -> dict:
    """
    광고센터 엑셀 보고서 → 파싱 결과 반환.

    추정 컬럼 (원본 수령 시 수정 필요):
    - 옵션ID (또는 vendorItemId)
    - 날짜
    - 노출수
    - 클릭수
    - 광고비 (VAT 제외 원본값으로 추정. 엑셀에 VAT 포함이면 ÷1.1 필요)
    - 판매수량

    반환: {"rows": list[dict], "warnings": list[str]}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {file_path}")

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)

    # TODO: 실제 시트명 확인 필요. 첫 번째 시트 사용.
    ws = wb.active
    warnings = []
    rows_data = []

    # 헤더 행 찾기: 첫 번째 행 기준
    header_row = None
    col_map = {}

    # 추정 컬럼명 매핑 (여러 변형 대응)
    COLUMN_ALIASES = {
        "vendor_item_id": ["옵션id", "옵션ID", "vendoritemid", "vendorItemId", "상품옵션ID"],
        "stat_date": ["날짜", "일자", "date", "보고일자"],
        "impressions": ["노출수", "노출", "impressions"],
        "ad_clicks": ["클릭수", "클릭", "clicks"],
        "ad_spend": ["광고비", "총비용", "비용", "cost", "spend"],
        "ad_units": ["판매수량", "전환수", "주문수", "판매량", "conversions"],
    }

    for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=1, values_only=False), 1):
        for cell in row:
            val = str(cell.value or "").strip()
            val_lower = val.lower()
            for field, aliases in COLUMN_ALIASES.items():
                if val_lower in [a.lower() for a in aliases]:
                    col_map[field] = cell.column - 1  # 0-indexed
                    break
        header_row = row_idx

    if not col_map.get("vendor_item_id"):
        # 옵션ID 컬럼이 없으면 파싱 불가
        warnings.append("'옵션ID' 컬럼을 찾을 수 없습니다. 컬럼명을 확인하세요.")
        wb.close()
        return {"rows": [], "warnings": warnings}

    if not col_map.get("stat_date"):
        warnings.append("'날짜' 컬럼을 찾을 수 없습니다.")
        wb.close()
        return {"rows": [], "warnings": warnings}

    found_cols = list(col_map.keys())
    missing_cols = [k for k in COLUMN_ALIASES if k not in col_map]
    if missing_cols:
        warnings.append(f"누락 컬럼 (0으로 처리): {', '.join(missing_cols)}")

    # 데이터 행 파싱
    for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
        cells = list(row)
        vid_idx = col_map.get("vendor_item_id")
        vid = cells[vid_idx] if vid_idx is not None and vid_idx < len(cells) else None
        if vid is None:
            continue

        try:
            vid = int(vid)
        except (ValueError, TypeError):
            warnings.append(f"옵션ID 변환 실패: {vid}")
            continue

        date_idx = col_map.get("stat_date")
        raw_date = cells[date_idx] if date_idx is not None and date_idx < len(cells) else None
        if raw_date is None:
            continue

        # 날짜 변환
        stat_date = _parse_date(raw_date)
        if not stat_date:
            warnings.append(f"날짜 변환 실패: {raw_date}")
            continue

        def _get_num(field):
            idx = col_map.get(field)
            if idx is None or idx >= len(cells):
                return None
            v = cells[idx]
            if v is None or str(v).strip() == "":
                return None
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        rows_data.append({
            "stat_date": stat_date,
            "vendor_item_id": vid,
            "impressions": _safe_int(_get_num("impressions")),
            "ad_clicks": _safe_int(_get_num("ad_clicks")),
            "ad_spend": _get_num("ad_spend"),  # 원본값 저장, ×1.1은 조회 시
            "ad_units": _safe_int(_get_num("ad_units")),
        })

    wb.close()
    return {"rows": rows_data, "warnings": warnings}


def save_ad_data(rows: list[dict]) -> dict:
    """파싱된 광고 데이터 → daily_ads INSERT OR REPLACE"""
    result = {"inserted": 0, "skipped": 0, "errors": []}

    with get_db() as conn:
        for row in rows:
            # 상품 존재 확인
            exists = conn.execute(
                "SELECT 1 FROM products WHERE vendor_item_id = ?",
                (row["vendor_item_id"],),
            ).fetchone()
            if not exists:
                result["skipped"] += 1
                result["errors"].append(
                    f"미등록 상품: vendor_item_id={row['vendor_item_id']}"
                )
                continue

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
                   VALUES (?, 'ads', 'upload', ?, ?, ?)""",
                (
                    date_range,
                    result["inserted"],
                    "ok" if not result["errors"] else "partial",
                    f"skipped={result['skipped']}" if result["skipped"] else None,
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
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _safe_int(val) -> Optional[int]:
    """float/None → Optional[int]"""
    if val is None:
        return None
    return int(val)
