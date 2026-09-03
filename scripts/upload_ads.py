"""
쿠팡 광고 보고서 엑셀(.xlsx)을 파싱하여 daily_ads 테이블에 저장.

엑셀 구조 (캠페인 > 광고그룹 > 상품 일별):
  날짜 | ... | 광고집행 옵션ID | ... | 노출수 | 클릭수 | 광고비 | ... | 총 판매수량(1일) | ...

같은 (날짜, 옵션ID)에 검색/비검색 등 여러 행이 있으므로 합산.
순이익 공식이 일별 기준이므로 14일이 아닌 1일 판매수량 사용.

실행:
  python scripts/upload_ads.py path/to/report.xlsx
  python scripts/upload_ads.py path/to/report.xlsx --dry-run
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.db import get_db


def parse_date(raw) -> str:
    """20260801 또는 20260801.0 → '2026-08-01'"""
    s = str(int(float(raw)))
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def parse_num(val) -> float:
    """숫자 파싱. None/빈값 → 0"""
    if val is None:
        return 0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0


def load_excel(filepath: str) -> dict[tuple, dict]:
    """
    엑셀 파싱 → (stat_date, vendor_item_id) 기준 합산.
    반환: {(date, vid): {impressions, ad_clicks, ad_spend, ad_units}}
    """
    import openpyxl

    wb = openpyxl.load_workbook(filepath, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if len(rows) < 2:
        print("데이터 없음")
        return {}

    header = [str(h).strip() if h else "" for h in rows[0]]

    # 컬럼 인덱스 찾기
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
        elif h == "총 판매수량(1일)":
            col_map["units_1d"] = i

    # 컬럼 인덱스 폴백 (헤더 매칭 실패 시 알려진 고정 위치 사용)
    FALLBACK_COLS = {
        "date": 0, "vid": 8, "impressions": 13,
        "clicks": 14, "spend": 15, "units_1d": 20,
    }
    missing = {"date", "vid", "impressions", "clicks", "spend"} - set(col_map.keys())
    if missing:
        if len(header) >= 21:
            print(f"  헤더 매칭 실패, 컬럼 인덱스 폴백 사용: {missing}")
            for key in list(missing) + (["units_1d"] if "units_1d" not in col_map else []):
                col_map[key] = FALLBACK_COLS[key]
        else:
            print(f"필수 컬럼 누락: {missing}")
            print(f"헤더: {header}")
            sys.exit(1)

    # 집계
    agg = defaultdict(lambda: {"impressions": 0, "ad_clicks": 0, "ad_spend": 0.0, "ad_units": 0})

    skipped = 0
    for row in rows[1:]:
        try:
            stat_date = parse_date(row[col_map["date"]])
        except (ValueError, TypeError):
            skipped += 1
            continue

        vid_raw = row[col_map["vid"]]
        if not vid_raw:
            skipped += 1
            continue
        vid = int(float(vid_raw))

        key = (stat_date, vid)
        agg[key]["impressions"] += int(parse_num(row[col_map["impressions"]]))
        agg[key]["ad_clicks"] += int(parse_num(row[col_map["clicks"]]))
        agg[key]["ad_spend"] += parse_num(row[col_map["spend"]])
        if "units_1d" in col_map:
            agg[key]["ad_units"] += int(parse_num(row[col_map["units_1d"]]))

    if skipped:
        print(f"  건너뛴 행: {skipped}개")

    return dict(agg)


def upload(filepath: str, dry_run: bool = False):
    """메인 업로드 로직"""
    print(f"파일: {filepath}")
    daily = load_excel(filepath)
    print(f"  집계: {len(daily)}개 (상품×일)")

    if not daily:
        return

    # 날짜 범위
    dates = sorted(set(d for d, _ in daily.keys()))
    print(f"  기간: {dates[0]} ~ {dates[-1]} ({len(dates)}일)")

    # 상품 수
    vids = set(v for _, v in daily.keys())
    print(f"  상품: {vids.__len__()}개")

    # 광고비 합계
    total_spend = sum(d["ad_spend"] for d in daily.values())
    print(f"  광고비 합계: {total_spend:,.0f}원")

    if dry_run:
        print("\n--- 일별 요약 ---")
        by_date = defaultdict(lambda: {"spend": 0, "clicks": 0, "units": 0})
        for (dt, _), d in daily.items():
            by_date[dt]["spend"] += d["ad_spend"]
            by_date[dt]["clicks"] += d["ad_clicks"]
            by_date[dt]["units"] += d["ad_units"]
        for dt in sorted(by_date.keys()):
            s = by_date[dt]
            print(f"  {dt} | 광고비 {s['spend']:>8,.0f}원 | 클릭 {s['clicks']:>4} | 판매 {s['units']:>3}")
        print(f"\n[DRY RUN] DB 변경 없음.")
        return

    # DB 저장 (RG vid → MP vid 변환)
    inserted = 0
    updated = 0
    with get_db() as conn:
        # RG→MP 매핑 로드
        rg_to_mp = dict(conn.execute(
            "SELECT rg_vendor_item_id, vendor_item_id FROM products WHERE rg_vendor_item_id IS NOT NULL"
        ).fetchall())
        rg_converted = 0

        for (stat_date, vid), d in daily.items():
            # 광고 보고서의 옵션ID는 RG vendorItemId → MP vendorItemId로 변환
            if vid in rg_to_mp:
                vid = rg_to_mp[vid]
                rg_converted += 1
            existing = conn.execute(
                "SELECT 1 FROM daily_ads WHERE stat_date = ? AND vendor_item_id = ?",
                (stat_date, vid),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE daily_ads
                       SET impressions = ?, ad_clicks = ?, ad_spend = ?, ad_units = ?,
                           source = 'upload', collected_at = CURRENT_TIMESTAMP
                       WHERE stat_date = ? AND vendor_item_id = ?""",
                    (d["impressions"], d["ad_clicks"], d["ad_spend"], d["ad_units"],
                     stat_date, vid),
                )
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO daily_ads
                       (stat_date, vendor_item_id, impressions, ad_clicks, ad_spend, ad_units, source)
                       VALUES (?, ?, ?, ?, ?, ?, 'upload')""",
                    (stat_date, vid, d["impressions"], d["ad_clicks"], d["ad_spend"], d["ad_units"]),
                )
                inserted += 1

        # ingest_log
        conn.execute(
            """INSERT INTO ingest_log (stat_date, data_type, source, row_count, status, message)
               VALUES (?, 'ads', 'upload', ?, 'ok', ?)""",
            (dates[0], inserted + updated, f"신규 {inserted}, 업데이트 {updated}, 기간 {dates[0]}~{dates[-1]}"),
        )

    print(f"\n=== 완료 ===")
    print(f"  신규: {inserted}건")
    print(f"  업데이트: {updated}건")
    print(f"  RG→MP 변환: {rg_converted}건")


def main():
    parser = argparse.ArgumentParser(description="쿠팡 광고 보고서 업로드")
    parser.add_argument("file", help="엑셀 파일 경로 (.xlsx)")
    parser.add_argument("--dry-run", action="store_true", help="DB 변경 없이 미리보기")
    args = parser.parse_args()

    if not Path(args.file).exists():
        print(f"파일 없음: {args.file}")
        sys.exit(1)

    upload(args.file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
