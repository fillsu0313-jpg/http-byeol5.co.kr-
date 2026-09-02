"""
쿠팡 API에서 상품 목록을 가져와 DB products 테이블에 동기화.

- API에서 전체 상품 + 옵션(vendorItem) 조회
- 신규 → INSERT, 기존 → coupang_name/is_active 업데이트
- 기존 임시 ID(7월 수기 데이터)는 is_active=0 처리

실행:
  python scripts/sync_products.py
  python scripts/sync_products.py --dry-run
"""
import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.coupang_api import create_client, CoupangAPIError
from src.db import get_db


def extract_vendor_items(detail: dict, list_info: dict) -> list[dict]:
    """
    상품 상세 API 응답에서 vendorItem 정보 추출.
    vendorItemId는 items[].marketplaceItemData.vendorItemId에 위치.
    """
    result = []
    seller_product_id = detail.get("sellerProductId") or list_info.get("sellerProductId")
    product_id = list_info.get("productId")
    product_name = detail.get("sellerProductName") or list_info.get("sellerProductName", "")
    status = list_info.get("statusName", "")

    for item in detail.get("items", []):
        vid = item.get("vendorItemId")
        if not vid:
            mp = item.get("marketplaceItemData") or {}
            vid = mp.get("vendorItemId")
        if not vid:
            rg = item.get("rocketGrowthItemData") or {}
            vid = rg.get("vendorItemId")
        if not vid:
            continue

        result.append({
            "vendor_item_id": int(vid),
            "seller_product_id": seller_product_id,
            "product_id": product_id,
            "coupang_name": product_name,
            "option_name": item.get("itemName", ""),
            "status": status,
        })

    return result


def sync(dry_run: bool = False):
    """API에서 상품 목록 조회 → DB 동기화"""
    print("쿠팡 API에서 상품 목록 조회 중...")
    try:
        api = create_client()
    except (FileNotFoundError, ValueError) as e:
        print(f"\n설정 오류: {e}")
        sys.exit(1)

    try:
        api_products = api.get_seller_products()
    except CoupangAPIError as e:
        print(f"\nAPI 오류: {e}")
        sys.exit(1)

    print(f"  상품: {len(api_products)}개")

    # 각 상품 상세 조회 → vendorItem 추출
    vendor_items = []
    total = len(api_products)
    for i, product in enumerate(api_products, 1):
        sp_id = product.get("sellerProductId")
        if not sp_id:
            continue
        try:
            detail = api.get_seller_product_detail(sp_id)
            vendor_items.extend(extract_vendor_items(detail, product))
            if i % 50 == 0 or i == total:
                print(f"  상세 조회: {i}/{total} ({len(vendor_items)}개 옵션)")
        except CoupangAPIError as e:
            print(f"  경고: {sp_id} 조회 실패 - {e}")

    print(f"  총 옵션(vendorItem): {len(vendor_items)}개")

    # DB 기존 상품 조회
    with get_db() as conn:
        existing = {
            row["vendor_item_id"]
            for row in conn.execute("SELECT vendor_item_id FROM products").fetchall()
        }

    api_ids = {vi["vendor_item_id"] for vi in vendor_items}
    new_count = len(api_ids - existing)
    update_count = len(api_ids & existing)
    deactivate_ids = existing - api_ids  # API에 없는 기존 상품

    print(f"\n  신규: {new_count}개")
    print(f"  업데이트: {update_count}개")
    print(f"  비활성화: {len(deactivate_ids)}개 (API에 없음)")

    if dry_run:
        print("\n[DRY RUN] DB 변경 없음.")
        return

    # DB 반영
    with get_db() as conn:
        for vi in vendor_items:
            display = vi["coupang_name"]
            if vi["option_name"]:
                display += f" [{vi['option_name']}]"

            conn.execute(
                """INSERT INTO products
                   (vendor_item_id, seller_product_id, product_id,
                    coupang_name, display_name, is_active)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(vendor_item_id) DO UPDATE SET
                    seller_product_id = excluded.seller_product_id,
                    product_id = excluded.product_id,
                    coupang_name = excluded.coupang_name,
                    is_active = 1""",
                (vi["vendor_item_id"], vi["seller_product_id"],
                 vi["product_id"], vi["coupang_name"], display),
            )

        # API에 없는 기존 상품 비활성화
        if deactivate_ids:
            placeholders = ",".join("?" * len(deactivate_ids))
            conn.execute(
                f"UPDATE products SET is_active = 0 WHERE vendor_item_id IN ({placeholders})",
                list(deactivate_ids),
            )

    print(f"\n=== 완료 ===")
    print(f"  등록/업데이트: {len(vendor_items)}개")
    print(f"  비활성화: {len(deactivate_ids)}개")


def main():
    parser = argparse.ArgumentParser(description="쿠팡 상품 목록 동기화")
    parser.add_argument("--dry-run", action="store_true", help="DB 변경 없이 미리보기")
    args = parser.parse_args()
    sync(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
