"""
DB 초기화 + cost_master.csv 임포트
실행: python scripts/init_db.py
"""
import csv
import hashlib
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "coupang.db"
SCHEMA_PATH = BASE_DIR / "files" / "schema.sql"
CSV_PATH = BASE_DIR / "files" / "cost_master.csv"


def make_vendor_item_id(name: str) -> int:
    """상품명 → 임시 vendor_item_id. vendorItemId 매핑표 수령 전까지 사용."""
    h = int(hashlib.sha256(name.encode("utf-8")).hexdigest(), 16)
    return h % 10_000_000 + 100_000


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    if DB_PATH.exists():
        DB_PATH.unlink()
        print(f"기존 DB 삭제: {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # 스키마 실행
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    print("스키마 생성 완료")

    # CSV 임포트
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # 상품명 → vendor_item_id 매핑 (충돌 검사)
    name_to_id: dict[str, int] = {}
    id_to_name: dict[int, str] = {}
    product_names = sorted(set(r["상품명"].strip() for r in rows))

    for name in product_names:
        vid = make_vendor_item_id(name)
        # 충돌 해결
        while vid in id_to_name and id_to_name[vid] != name:
            vid += 1
        name_to_id[name] = vid
        id_to_name[vid] = name

    # products 테이블 삽입
    for name in product_names:
        vid = name_to_id[name]
        conn.execute(
            "INSERT INTO products (vendor_item_id, coupang_name, display_name) VALUES (?, ?, ?)",
            (vid, name, name),
        )
    print(f"상품 {len(product_names)}개 삽입")

    # product_costs 테이블 삽입
    cost_count = 0
    for r in rows:
        name = r["상품명"].strip()
        vid = name_to_id[name]
        effective_from = r["적용시작일"]
        sale_price = float(r["판매가"])
        purchase_cost = float(r["매입단가"])
        unit_margin = float(r["개당순마진"])
        # 입출고수수료 역산: 판매가 - 매입단가 - 개당순마진
        fulfillment_fee = sale_price - purchase_cost - unit_margin

        conn.execute(
            """INSERT INTO product_costs
               (vendor_item_id, effective_from, sale_price, purchase_cost, fulfillment_fee)
               VALUES (?, ?, ?, ?, ?)""",
            (vid, effective_from, sale_price, purchase_cost, round(fulfillment_fee, 2)),
        )
        cost_count += 1

    conn.commit()
    print(f"원가 이력 {cost_count}개 삽입")

    # 검증
    prod_count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    cost_total = conn.execute("SELECT COUNT(*) FROM product_costs").fetchone()[0]
    print(f"\n검증: products={prod_count}, product_costs={cost_total}")
    print(f"DB 저장 위치: {DB_PATH}")

    conn.close()


if __name__ == "__main__":
    init_db()
