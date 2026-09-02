"""
원가 엑셀 파서
엑셀 양식:
  vendorItemId | 적용시작일 | 판매가 | 위안단가 | 환율 | 매입원가 | 판매수수료 | 입출고수수료 | 기타비용 | 메모

실행:
  python scripts/upload_costs.py path/to/costs.xlsx
  python scripts/upload_costs.py path/to/costs.xlsx --dry-run
"""
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import openpyxl

from src.db import get_db


# 헤더 이름 → 내부 키 매핑 (여러 표기 지원)
_HEADER_ALIASES = {
    "vendoritemid": "vendor_item_id",
    "vendor_item_id": "vendor_item_id",
    "옵션id": "vendor_item_id",
    "옵션 id": "vendor_item_id",
    "적용시작일": "effective_from",
    "날짜": "effective_from",
    "판매가": "sale_price",
    "위안단가": "purchase_cost_fx",
    "환율": "fx_rate",
    "매입원가": "purchase_cost",
    "판매수수료": "commission_fee",
    "입출고수수료": "fulfillment_fee",
    "기타비용": "other_unit_cost",
    "기타개당비용": "other_unit_cost",
    "메모": "memo",
}


def parse_cost_excel(file_path: str) -> dict:
    """
    원가 엑셀 → 파싱 결과 반환.

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

    # 헤더 매핑
    header = [str(h).strip() if h else "" for h in rows_raw[0]]
    col_map = {}  # 내부키 → 컬럼 인덱스
    for i, h in enumerate(header):
        key = _HEADER_ALIASES.get(h.lower().replace(" ", ""))
        if key and key not in col_map:
            col_map[key] = i

    # 필수 컬럼 확인
    missing = {"vendor_item_id", "effective_from", "sale_price", "purchase_cost"} - set(col_map.keys())
    if missing:
        # purchase_cost가 없어도 위안단가+환율이 있으면 OK
        if missing == {"purchase_cost"} and "purchase_cost_fx" in col_map and "fx_rate" in col_map:
            pass
        else:
            warnings.append(f"필수 컬럼 누락: {missing}. 헤더: {header[:12]}")
            return {"rows": [], "warnings": warnings}

    # 행 파싱
    rows_data = []
    skipped = 0

    for row_idx, row in enumerate(rows_raw[1:], start=2):
        # vendorItemId
        vid_raw = row[col_map["vendor_item_id"]]
        if not vid_raw:
            skipped += 1
            continue
        try:
            vid = int(float(vid_raw))
        except (ValueError, TypeError):
            warnings.append(f"{row_idx}행: vendorItemId 파싱 실패 ({vid_raw})")
            skipped += 1
            continue

        # 적용시작일
        date_raw = row[col_map["effective_from"]]
        effective_from = _parse_date(date_raw)
        if not effective_from:
            warnings.append(f"{row_idx}행: 날짜 파싱 실패 ({date_raw})")
            skipped += 1
            continue

        # 판매가
        sale_price = _parse_num(row[col_map["sale_price"]]) if "sale_price" in col_map else None
        if sale_price is None or sale_price <= 0:
            warnings.append(f"{row_idx}행: 판매가 누락 또는 0 이하 ({sale_price})")
            skipped += 1
            continue

        # 위안단가, 환율
        purchase_cost_fx = _parse_num_or_none(row[col_map["purchase_cost_fx"]]) if "purchase_cost_fx" in col_map else None
        fx_rate = _parse_num_or_none(row[col_map["fx_rate"]]) if "fx_rate" in col_map else None

        # 매입원가: 직접 입력값 우선, 없으면 위안단가×환율 계산
        if "purchase_cost" in col_map:
            purchase_cost = _parse_num_or_none(row[col_map["purchase_cost"]])
        else:
            purchase_cost = None

        if (purchase_cost is None or purchase_cost == 0) and purchase_cost_fx and fx_rate:
            purchase_cost = round(purchase_cost_fx * fx_rate)

        if purchase_cost is None or purchase_cost < 0:
            warnings.append(f"{row_idx}행: 매입원가를 결정할 수 없습니다 (매입원가={purchase_cost}, 위안단가={purchase_cost_fx}, 환율={fx_rate})")
            skipped += 1
            continue

        # 선택 필드
        commission_fee = _parse_num(row[col_map["commission_fee"]]) if "commission_fee" in col_map else 0
        fulfillment_fee = _parse_num(row[col_map["fulfillment_fee"]]) if "fulfillment_fee" in col_map else 0
        other_unit_cost = _parse_num(row[col_map["other_unit_cost"]]) if "other_unit_cost" in col_map else 0
        memo = str(row[col_map["memo"]]).strip() if "memo" in col_map and row[col_map["memo"]] else None

        rows_data.append({
            "vendor_item_id": vid,
            "effective_from": effective_from,
            "sale_price": sale_price,
            "purchase_cost": purchase_cost,
            "commission_fee": commission_fee,
            "fulfillment_fee": fulfillment_fee,
            "other_unit_cost": other_unit_cost,
            "purchase_cost_fx": purchase_cost_fx,
            "fx_rate": fx_rate,
            "memo": memo,
        })

    if skipped:
        warnings.append(f"건너뛴 행: {skipped}개")

    return {"rows": rows_data, "warnings": warnings}


def save_cost_data(rows: list[dict]) -> dict:
    """파싱된 원가 데이터 → product_costs upsert (단일 트랜잭션)"""
    result = {"inserted": 0, "skipped": 0, "errors": []}

    with get_db() as conn:
        for row in rows:
            try:
                vid = row["vendor_item_id"]
                ef = row["effective_from"]
                existing = conn.execute(
                    "SELECT id FROM product_costs WHERE vendor_item_id = ? AND effective_from = ?",
                    (vid, ef),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE product_costs SET sale_price=?, purchase_cost=?,
                           commission_fee=?, fulfillment_fee=?, other_unit_cost=?,
                           purchase_cost_fx=?, fx_rate=?, memo=?
                           WHERE id=?""",
                        (row["sale_price"], row["purchase_cost"],
                         row["commission_fee"], row["fulfillment_fee"],
                         row["other_unit_cost"], row.get("purchase_cost_fx"),
                         row.get("fx_rate"), row.get("memo"), existing["id"]),
                    )
                else:
                    conn.execute(
                        """INSERT INTO product_costs
                           (vendor_item_id, effective_from, sale_price, purchase_cost,
                            commission_fee, fulfillment_fee, other_unit_cost,
                            purchase_cost_fx, fx_rate, memo)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (vid, ef, row["sale_price"], row["purchase_cost"],
                         row["commission_fee"], row["fulfillment_fee"],
                         row["other_unit_cost"], row.get("purchase_cost_fx"),
                         row.get("fx_rate"), row.get("memo")),
                    )
                result["inserted"] += 1
            except Exception as e:
                result["skipped"] += 1
                result["errors"].append(f"VID {row['vendor_item_id']}: {e}")

        # ingest_log
        if result["inserted"] > 0:
            vids = set(r["vendor_item_id"] for r in rows)
            conn.execute(
                """INSERT INTO ingest_log
                   (stat_date, data_type, source, row_count, status, message)
                   VALUES (?, 'costs', 'upload', ?, 'ok', ?)""",
                (
                    rows[0]["effective_from"],
                    result["inserted"],
                    f"원가 {result['inserted']}건, 상품 {len(vids)}개",
                ),
            )

    return result


def _parse_date(raw) -> Optional[str]:
    """다양한 날짜 형식 → 'YYYY-MM-DD'"""
    if isinstance(raw, date):
        return raw.strftime("%Y-%m-%d")
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")

    if isinstance(raw, (int, float)):
        s = str(int(raw))
        if len(s) == 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"

    s = str(raw).strip()

    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"

    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y"):
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


def _parse_num_or_none(val) -> Optional[float]:
    """숫자 파싱. None/빈값 → None"""
    if val is None:
        return None
    try:
        v = float(val)
        return v if v != 0 else None
    except (ValueError, TypeError):
        return None
