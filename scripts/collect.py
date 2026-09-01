"""
쿠팡 API 수집 실행 스크립트 (cron 또는 수동)

실행 예시:
  python scripts/collect.py --date 2026-07-15
  python scripts/collect.py --from 2026-07-01 --to 2026-07-31
  python scripts/collect.py  (기본: 어제 날짜)
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.coupang_api import create_client, CoupangAPIError
from src.collector import collect_sales, collect_orders


def main():
    parser = argparse.ArgumentParser(description="쿠팡 판매 데이터 수집")
    parser.add_argument(
        "--date", type=str, default=None,
        help="수집 날짜 (YYYY-MM-DD). 미지정 시 어제",
    )
    parser.add_argument(
        "--from", dest="date_from", type=str, default=None,
        help="시작 날짜 (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--to", dest="date_to", type=str, default=None,
        help="종료 날짜 (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="config.json 경로 (기본: 프로젝트 루트의 config.json)",
    )
    args = parser.parse_args()

    # 날짜 결정
    if args.date:
        date_from = date_to = args.date
    elif args.date_from and args.date_to:
        date_from = args.date_from
        date_to = args.date_to
    elif args.date_from or args.date_to:
        print("--from과 --to는 함께 사용해야 합니다.")
        sys.exit(1)
    else:
        yesterday = date.today() - timedelta(days=1)
        date_from = date_to = yesterday.strftime("%Y-%m-%d")

    print(f"수집 기간: {date_from} ~ {date_to}")

    # API 클라이언트 생성
    try:
        api = create_client(args.config)
    except FileNotFoundError as e:
        print(f"\n{e}")
        sys.exit(1)
    except ValueError as e:
        print(f"\n설정 오류: {e}")
        sys.exit(1)

    # 판매 데이터 수집
    print("\n[1/2] 판매 데이터 수집 중...")
    sales_result = collect_sales(api, date_from, date_to)
    print(f"  삽입: {sales_result['inserted']}건")
    if sales_result["errors"]:
        print(f"  오류: {len(sales_result['errors'])}건")
        for e in sales_result["errors"][:5]:
            print(f"    - {e}")

    # 주문 데이터 수집
    print("\n[2/2] 주문 데이터 수집 중...")
    orders_result = collect_orders(api, date_from, date_to)
    print(f"  업데이트: {orders_result['updated']}건")
    if orders_result["errors"]:
        print(f"  오류: {len(orders_result['errors'])}건")
        for e in orders_result["errors"][:5]:
            print(f"    - {e}")

    # 요약
    total = sales_result["inserted"] + orders_result["updated"]
    errors = len(sales_result["errors"]) + len(orders_result["errors"])
    print(f"\n=== 수집 완료 ===")
    print(f"  총 처리: {total}건")
    if errors:
        print(f"  총 오류: {errors}건")


if __name__ == "__main__":
    main()
