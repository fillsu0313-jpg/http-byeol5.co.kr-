"""
product_costs를 같은 coupang_name을 가진 모든 활성 옵션에 복사.
같은 상품이 재등록되면서 seller_product_id가 바뀌어도
coupang_name은 동일하므로 이걸 기준으로 확장.
"""
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "coupang.db"


def expand():
    conn = sqlite3.connect(str(DB_PATH))

    existing_costs = conn.execute("SELECT * FROM product_costs").fetchall()
    print(f"기존 원가 항목: {len(existing_costs)}개")

    expanded = 0
    for cost in existing_costs:
        vid = cost[0]

        row = conn.execute(
            "SELECT coupang_name FROM products WHERE vendor_item_id = ?",
            (vid,),
        ).fetchone()
        if not row or not row[0]:
            continue
        cname = row[0]

        # 같은 coupang_name의 모든 활성 옵션
        siblings = conn.execute(
            "SELECT vendor_item_id FROM products "
            "WHERE coupang_name = ? AND is_active = 1 AND vendor_item_id != ?",
            (cname, vid),
        ).fetchall()

        for (sib_vid,) in siblings:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO product_costs
                       (vendor_item_id, effective_from, sale_price, purchase_cost,
                        commission_fee, fulfillment_fee, other_unit_cost, memo)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (sib_vid, cost[1], cost[2], cost[3],
                     cost[4], cost[5], cost[6], cost[7]),
                )
                expanded += 1
            except sqlite3.IntegrityError:
                pass

    conn.commit()

    # 검증
    cost_vids = set(
        r[0] for r in conn.execute(
            "SELECT DISTINCT vendor_item_id FROM product_costs"
        ).fetchall()
    )
    sales_vids = set(
        r[0] for r in conn.execute(
            "SELECT DISTINCT vendor_item_id FROM daily_sales"
        ).fetchall()
    )
    overlap = cost_vids & sales_vids

    total_costs = conn.execute("SELECT COUNT(*) FROM product_costs").fetchone()[0]
    profit_rows = conn.execute(
        'SELECT COUNT(*) FROM v_daily_profit '
        'WHERE net_profit IS NOT NULL AND stat_date BETWEEN "2026-07-01" AND "2026-07-31"'
    ).fetchone()[0]

    print(f"추가된 원가 항목: {expanded}개")
    print(f"총 원가 항목: {total_costs}개")
    print(f"원가 상품: {len(cost_vids)}개, 판매 상품: {len(sales_vids)}개, 매칭: {len(overlap)}개")
    print(f"v_daily_profit 7월 (순이익 계산됨): {profit_rows}행")

    conn.close()


if __name__ == "__main__":
    expand()
