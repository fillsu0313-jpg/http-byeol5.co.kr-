"""
쿠팡 API에서 일별 판매 데이터를 수집하여 daily_sales 테이블에 저장.

- 주문서 API (FINAL_DELIVERY)로 배송완료된 주문 집계
- (주문일, vendorItemId) 기준 판매량·매출 합산
- 매출내역 API로 수수료 정보 보충 (향후)

실행:
  python scripts/sync_daily_sales.py                  # 어제 데이터
  python scripts/sync_daily_sales.py --date 2026-09-01
  python scripts/sync_daily_sales.py --from 2026-08-01 --to 2026-08-31
  python scripts/sync_daily_sales.py --dry-run
"""
import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.coupang_api import create_client, CoupangAPIError
from src.db import get_db


def fetch_orders(api, date_from: str, date_to: str) -> list[dict]:
    """주문서 API로 배송완료 주문 조회"""
    return api.get_order_sheets(date_from, date_to, status="FINAL_DELIVERY")


def aggregate_daily(orders: list[dict]) -> dict[tuple, dict]:
    """
    주문 목록을 (주문일, vendorItemId) 기준으로 집계.
    반환: {(stat_date, vendor_item_id): {units_sold, gross_revenue, cancel_units}}
    """
    agg = defaultdict(lambda: {"units_sold": 0, "gross_revenue": 0, "cancel_units": 0})

    for order in orders:
        # 주문일 추출 (orderedAt: "2026-08-26T02:57:49+09:00" → "2026-08-26")
        ordered_at = order.get("orderedAt", "")
        if not ordered_at:
            continue
        stat_date = ordered_at[:10]

        for item in order.get("orderItems", []):
            vid = item.get("vendorItemId")
            if not vid:
                continue

            key = (stat_date, int(vid))
            qty = item.get("shippingCount", 0) or 0
            price = 0
            sales_price = item.get("salesPrice") or item.get("orderPrice") or {}
            if isinstance(sales_price, dict):
                price = sales_price.get("units", 0) or 0
            elif isinstance(sales_price, (int, float)):
                price = sales_price

            cancel = item.get("cancelCount", 0) or 0

            agg[key]["units_sold"] += qty
            agg[key]["gross_revenue"] += price * qty
            agg[key]["cancel_units"] += cancel

    return dict(agg)


def sync(date_from: str, date_to: str, dry_run: bool = False):
    """메인 수집 로직"""
    print(f"수집 기간: {date_from} ~ {date_to}")

    try:
        api = create_client()
    except (FileNotFoundError, ValueError) as e:
        print(f"\n설정 오류: {e}")
        sys.exit(1)

    # 주문서 조회
    print("주문서 조회 중...")
    try:
        orders = fetch_orders(api, date_from, date_to)
    except CoupangAPIError as e:
        print(f"\nAPI 오류: {e}")
        # ingest_log에 실패 기록
        if not dry_run:
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO ingest_log (stat_date, data_type, source, row_count, status, message)
                       VALUES (?, 'sales', 'api', 0, 'failed', ?)""",
                    (date_from, str(e)),
                )
        sys.exit(1)

    print(f"  주문서: {len(orders)}건")

    # 집계
    daily = aggregate_daily(orders)
    print(f"  집계: {len(daily)}개 (상품×일)")

    if not daily:
        print("  수집할 데이터 없음.")
        if not dry_run:
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO ingest_log (stat_date, data_type, source, row_count, status, message)
                       VALUES (?, 'sales', 'api', 0, 'ok', '데이터 없음')""",
                    (date_from,),
                )
        return

    if dry_run:
        print("\n--- 미리보기 (상위 20개) ---")
        sorted_items = sorted(daily.items(), key=lambda x: -x[1]["gross_revenue"])
        for (dt, vid), d in sorted_items[:20]:
            print(f"  {dt} | {vid} | {d['units_sold']}개 | {d['gross_revenue']:,.0f}원")
        print(f"\n[DRY RUN] DB 변경 없음.")
        return

    # DB 저장
    inserted = 0
    updated = 0
    with get_db() as conn:
        for (stat_date, vid), d in daily.items():
            existing = conn.execute(
                "SELECT 1 FROM daily_sales WHERE stat_date = ? AND vendor_item_id = ?",
                (stat_date, vid),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE daily_sales
                       SET units_sold = ?, gross_revenue = ?, cancel_units = ?,
                           source = 'api', collected_at = CURRENT_TIMESTAMP
                       WHERE stat_date = ? AND vendor_item_id = ?""",
                    (d["units_sold"], d["gross_revenue"], d["cancel_units"],
                     stat_date, vid),
                )
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO daily_sales
                       (stat_date, vendor_item_id, units_sold, gross_revenue, cancel_units, source)
                       VALUES (?, ?, ?, ?, ?, 'api')""",
                    (stat_date, vid, d["units_sold"], d["gross_revenue"], d["cancel_units"]),
                )
                inserted += 1

        # ingest_log 기록
        conn.execute(
            """INSERT INTO ingest_log (stat_date, data_type, source, row_count, status, message)
               VALUES (?, 'sales', 'api', ?, 'ok', ?)""",
            (date_from, inserted + updated, f"신규 {inserted}, 업데이트 {updated}"),
        )

    print(f"\n=== 완료 ===")
    print(f"  신규: {inserted}건")
    print(f"  업데이트: {updated}건")


def main():
    parser = argparse.ArgumentParser(description="쿠팡 일별 판매 데이터 수집")
    parser.add_argument("--date", help="특정 날짜 (YYYY-MM-DD)")
    parser.add_argument("--from", dest="date_from", help="시작일 (YYYY-MM-DD)")
    parser.add_argument("--to", dest="date_to", help="종료일 (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="DB 변경 없이 미리보기")
    args = parser.parse_args()

    if args.date:
        d = args.date
        sync(d, d, dry_run=args.dry_run)
    elif args.date_from and args.date_to:
        sync(args.date_from, args.date_to, dry_run=args.dry_run)
    else:
        # 기본: 어제
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        sync(yesterday, yesterday, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
