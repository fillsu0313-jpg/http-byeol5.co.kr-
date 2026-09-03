"""
product_costs의 해시 기반 vendor_item_id를 실제 API vendor_item_id로 마이그레이션.

방식:
1. 해시 ID 상품의 display_name으로 실제 API 상품을 검색 (토큰 매칭 + 힌트)
2. 매칭된 상품의 seller_product_id가 같은 모든 옵션에 원가 복사
   (같은 등록상품의 옵션들은 원가가 동일)
3. 해시 기반 product_costs 행을 실제 ID로 교체

실행:
  python scripts/migrate_costs.py --dry-run
  python scripts/migrate_costs.py
"""
import re
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.db import get_db


# 약칭 → 검색 키워드 (자동 매칭 실패 시 수동 힌트)
HINTS = {
    "u형머리띠": "귀안아픈 머리띠",
    "각도기세트": "각도기",
    "간식 봉투m": "구디백 포장 봉투",
    "간식 봉투s": "구디백 포장 봉투",
    "네일자석": "네일 자석",
    "딜라이프 2+1 대나무효자": "대나무 효자손",
    "문신스티커": "타투 문신",
    "물병세척솔": "물병 세척",
    "물집방지": "물집방지",
    "반짇고리함": "반짇고리",
    "보습비닐": "보습 비닐",
    "보습양말": "보습 양말",
    "빗살머리띠": "빗살 고정",
    "뿌리볼륨빗": "뿌리볼륨",
    "세탁기 틈새솔": "틈새",
    "손가락 지압 롤러": "손가락 마사지",
    "실리콘받침대": "실리콘 받침",
    "어항꾸미기": "어항",
    "연필홀더": "연필",
    "운동화끈": "신발끈",
    "장갑걸이": "장갑",
    "젤네일 쑥오프": "네일 아트",
    "줄눈헤라": "줄눈",
    "지워지는볼펜": "지워지는",
    "짱짱 머리끈": "머리끈",
    "칼베임손가락골무": "손가락 커버 보호대 실리콘 골무",
    "큐티클 쪽가위": "큐티클",
    "파티선글라스": "파티",
    "펜슬샤프너": "샤프너",
    "하트집게핀": "하트 집게핀",
    "허리바지고무줄": "고무줄",
}


def tokenize(s):
    return set(re.findall(r"[가-힣a-zA-Z0-9]+", s.lower()))


def find_match(cost_name, active_products):
    """cost_name으로 active 상품 중 매칭되는 것 찾기"""
    cn_tokens = tokenize(cost_name)

    # Pass 1: 토큰 완전 포함
    candidates = []
    for vid, dname, cname, spid in active_products:
        all_text = ((dname or "") + " " + (cname or "")).lower()
        if cn_tokens and cn_tokens.issubset(tokenize(all_text)):
            candidates.append((vid, dname, spid))

    if candidates:
        return candidates

    # Pass 2: 힌트 기반
    hint = HINTS.get(cost_name, "")
    if hint:
        hint_lower = hint.lower()
        for vid, dname, cname, spid in active_products:
            all_text = ((dname or "") + " " + (cname or "")).lower()
            if hint_lower in all_text:
                candidates.append((vid, dname, spid))

    return candidates


def migrate(dry_run=False):
    conn = sqlite3.connect(str(BASE_DIR / "data" / "coupang.db"))
    conn.row_factory = sqlite3.Row

    # 해시 기반 원가 상품 목록
    cost_products = conn.execute("""
        SELECT DISTINCT p.display_name, p.vendor_item_id
        FROM product_costs pc
        JOIN products p ON p.vendor_item_id = pc.vendor_item_id
        WHERE p.is_active = 0
        ORDER BY p.display_name
    """).fetchall()

    # 실제 API 상품
    active = conn.execute("""
        SELECT vendor_item_id, display_name, coupang_name, seller_product_id
        FROM products WHERE is_active = 1
    """).fetchall()

    print(f"마이그레이션 대상: {len(cost_products)}개 상품")
    print()

    matched = 0
    migrated_costs = 0
    unmatched = []

    for row in cost_products:
        cost_name = row[0]
        hash_vid = row[1]

        candidates = find_match(cost_name, active)
        if not candidates:
            unmatched.append(cost_name)
            continue

        matched += 1

        # 첫 번째 후보의 seller_product_id로 같은 상품의 모든 옵션 찾기
        target_spid = candidates[0][2]
        if target_spid:
            sibling_vids = [
                r[0] for r in conn.execute(
                    "SELECT vendor_item_id FROM products WHERE seller_product_id = ? AND is_active = 1",
                    (target_spid,),
                ).fetchall()
            ]
        else:
            sibling_vids = [candidates[0][0]]

        # 해시 ID의 원가 이력 가져오기
        cost_rows = conn.execute(
            "SELECT * FROM product_costs WHERE vendor_item_id = ?",
            (hash_vid,),
        ).fetchall()

        print(f"  {cost_name:25s} hash={hash_vid} -> {len(sibling_vids)} options, {len(cost_rows)} cost rows")

        if dry_run:
            continue

        # 각 옵션에 원가 복사
        for cost_row in cost_rows:
            for new_vid in sibling_vids:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO product_costs
                           (vendor_item_id, effective_from, sale_price, purchase_cost,
                            commission_fee, fulfillment_fee, other_unit_cost, memo)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (new_vid, cost_row["effective_from"], cost_row["sale_price"],
                         cost_row["purchase_cost"], cost_row["commission_fee"],
                         cost_row["fulfillment_fee"], cost_row["other_unit_cost"],
                         cost_row["memo"]),
                    )
                    migrated_costs += 1
                except sqlite3.IntegrityError:
                    pass  # 이미 존재

    if not dry_run:
        conn.commit()

    print(f"\n=== 결과 ===")
    print(f"  매칭 성공: {matched}/{len(cost_products)}")
    print(f"  원가 행 복사: {migrated_costs}건")
    if unmatched:
        print(f"  매칭 실패 ({len(unmatched)}):")
        for u in unmatched:
            print(f"    X {u}")

    if dry_run:
        print("\n[DRY RUN] DB 변경 없음.")
    else:
        print(f"\n완료. 해시 기반 원가는 보존됨 (수동 삭제 필요 시 별도 처리).")

    conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="원가 데이터 vendorItemId 마이그레이션")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
