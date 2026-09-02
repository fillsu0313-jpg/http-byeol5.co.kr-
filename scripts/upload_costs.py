"""
원가 엑셀(.xlsx) 업로드 CLI.

실행:
  python scripts/upload_costs.py path/to/costs.xlsx
  python scripts/upload_costs.py path/to/costs.xlsx --dry-run
"""
import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.cost_parser import parse_cost_excel, save_cost_data


def main():
    parser = argparse.ArgumentParser(description="원가 엑셀 업로드")
    parser.add_argument("file", help="엑셀 파일 경로 (.xlsx)")
    parser.add_argument("--dry-run", action="store_true", help="DB 변경 없이 미리보기")
    args = parser.parse_args()

    filepath = Path(args.file)
    if not filepath.exists():
        print(f"파일 없음: {args.file}")
        sys.exit(1)

    print(f"파일: {filepath}")
    parsed = parse_cost_excel(str(filepath))

    if parsed["warnings"]:
        print(f"\n경고:")
        for w in parsed["warnings"]:
            print(f"  - {w}")

    rows = parsed["rows"]
    print(f"\n파싱 결과: {len(rows)}건")

    if not rows:
        print("업로드할 데이터가 없습니다.")
        sys.exit(0)

    # 요약
    vids = set(r["vendor_item_id"] for r in rows)
    dates = sorted(set(r["effective_from"] for r in rows))
    print(f"  상품: {len(vids)}개")
    print(f"  적용시작일: {dates[0]} ~ {dates[-1]}" if len(dates) > 1 else f"  적용시작일: {dates[0]}")

    if args.dry_run:
        print(f"\n--- 미리보기 (상위 20건) ---")
        print(f"{'vendorItemId':>15} | {'적용시작일':>12} | {'판매가':>8} | {'매입원가':>8} | {'수수료':>6} | {'입출고':>6} | {'기타':>6}")
        print("-" * 80)
        for r in rows[:20]:
            print(f"{r['vendor_item_id']:>15} | {r['effective_from']:>12} | {r['sale_price']:>8,.0f} | {r['purchase_cost']:>8,.0f} | {r['commission_fee']:>6,.0f} | {r['fulfillment_fee']:>6,.0f} | {r['other_unit_cost']:>6,.0f}")
        if len(rows) > 20:
            print(f"  ... 외 {len(rows) - 20}건")
        print(f"\n[DRY RUN] DB 변경 없음.")
        return

    result = save_cost_data(rows)
    print(f"\n=== 완료 ===")
    print(f"  삽입/수정: {result['inserted']}건")
    if result["skipped"]:
        print(f"  건너뜀: {result['skipped']}건")
    if result["errors"]:
        print(f"  오류:")
        for e in result["errors"][:10]:
            print(f"    - {e}")


if __name__ == "__main__":
    main()
