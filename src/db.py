"""
DB 연결 관리 + 공통 쿼리 함수
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "coupang.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_available_dates() -> list[str]:
    """daily_sales에 데이터가 있는 날짜 목록 (내림차순)"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT stat_date FROM daily_sales ORDER BY stat_date DESC"
        ).fetchall()
        return [r["stat_date"] for r in rows]


def get_daily_profit(stat_date: str) -> list[dict]:
    """v_daily_profit VIEW 조회"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT
                p.vendor_item_id,
                p.display_name,
                s.units_sold,
                a.ad_units,
                CASE WHEN s.units_sold IS NULL OR a.ad_units IS NULL THEN NULL
                     WHEN s.units_sold - a.ad_units < 0 THEN 0
                     ELSE s.units_sold - a.ad_units END AS organic_units,
                s.total_views,
                a.ad_clicks,
                CASE WHEN s.total_views IS NULL OR a.ad_clicks IS NULL THEN NULL
                     WHEN s.total_views - a.ad_clicks < 0 THEN 0
                     ELSE s.total_views - a.ad_clicks END AS organic_views,
                CASE WHEN s.units_sold IS NOT NULL AND s.units_sold > 0
                     THEN ROUND(CAST(s.units_sold AS REAL) /
                          NULLIF(s.total_views, 0) * 100, 2)
                     ELSE NULL END AS conversion_rate,
                a.ad_spend,
                c.sale_price - c.purchase_cost - COALESCE(c.commission_fee, 0)
                  - COALESCE(c.fulfillment_fee, 0) - COALESCE(c.other_unit_cost, 0) AS unit_margin,
                CASE WHEN s.units_sold IS NULL THEN NULL
                     ELSE s.units_sold * (c.sale_price - c.purchase_cost
                       - COALESCE(c.commission_fee, 0) - COALESCE(c.fulfillment_fee, 0)
                       - COALESCE(c.other_unit_cost, 0))
                       - COALESCE(a.ad_spend, 0) * 1.1
                END AS net_profit
            FROM daily_sales s
            JOIN products p ON p.vendor_item_id = s.vendor_item_id
            LEFT JOIN daily_ads a
                   ON a.stat_date = s.stat_date AND a.vendor_item_id = s.vendor_item_id
            LEFT JOIN product_costs c
                   ON c.vendor_item_id = s.vendor_item_id
                  AND c.effective_from = (
                        SELECT MAX(effective_from) FROM product_costs
                        WHERE vendor_item_id = s.vendor_item_id
                          AND effective_from <= s.stat_date
                  )
            WHERE s.stat_date = ?
            ORDER BY net_profit DESC""",
            (stat_date,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_prev_date(stat_date: str) -> Optional[str]:
    """주어진 날짜의 직전 데이터 존재 날짜"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(stat_date) FROM daily_sales WHERE stat_date < ?",
            (stat_date,),
        ).fetchone()
        return row[0] if row and row[0] else None


def get_daily_profit_map(stat_date: str) -> dict[int, dict]:
    """v_daily_profit 결과를 vendor_item_id → dict 맵으로 반환"""
    rows = get_daily_profit(stat_date)
    return {r["vendor_item_id"]: r for r in rows}


def get_products() -> list[dict]:
    """상품 목록 (원가이력 수 포함)"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT p.vendor_item_id, p.display_name, p.is_active,
                      COUNT(c.id) as cost_count
               FROM products p
               LEFT JOIN product_costs c ON c.vendor_item_id = p.vendor_item_id
               GROUP BY p.vendor_item_id
               ORDER BY p.display_name"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_product(vendor_item_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE vendor_item_id = ?", (vendor_item_id,)
        ).fetchone()
        return dict(row) if row else None


def get_product_costs(vendor_item_id: int) -> list[dict]:
    """원가 이력 (내림차순)"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, effective_from, sale_price, purchase_cost_fx, fx_rate,
                      purchase_cost, commission_fee, fulfillment_fee, other_unit_cost, memo,
                      sale_price - purchase_cost - COALESCE(commission_fee, 0)
                        - COALESCE(fulfillment_fee, 0) - COALESCE(other_unit_cost, 0) AS unit_margin
               FROM product_costs
               WHERE vendor_item_id = ?
               ORDER BY effective_from DESC""",
            (vendor_item_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_product_cost(
    vendor_item_id: int,
    effective_from: str,
    sale_price: float,
    purchase_cost: float,
    commission_fee: float = 0,
    fulfillment_fee: float = 0,
    other_unit_cost: float = 0,
    purchase_cost_fx: Optional[float] = None,
    fx_rate: Optional[float] = None,
    memo: Optional[str] = None,
) -> int:
    """원가 추가. 동일 (vendor_item_id, effective_from) 존재 시 업데이트."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM product_costs WHERE vendor_item_id = ? AND effective_from = ?",
            (vendor_item_id, effective_from),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE product_costs SET sale_price=?, purchase_cost=?,
                   commission_fee=?, fulfillment_fee=?, other_unit_cost=?,
                   purchase_cost_fx=?, fx_rate=?, memo=?
                   WHERE id=?""",
                (sale_price, purchase_cost, commission_fee, fulfillment_fee,
                 other_unit_cost, purchase_cost_fx, fx_rate, memo, existing["id"]),
            )
            return existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO product_costs
                   (vendor_item_id, effective_from, sale_price, purchase_cost,
                    commission_fee, fulfillment_fee, other_unit_cost,
                    purchase_cost_fx, fx_rate, memo)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (vendor_item_id, effective_from, sale_price, purchase_cost,
                 commission_fee, fulfillment_fee, other_unit_cost,
                 purchase_cost_fx, fx_rate, memo),
            )
            return cur.lastrowid


def update_product_cost(cost_id: int, **kwargs) -> bool:
    """원가 수정"""
    allowed = {"effective_from", "sale_price", "purchase_cost", "commission_fee",
               "fulfillment_fee", "other_unit_cost", "purchase_cost_fx", "fx_rate", "memo"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with get_db() as conn:
        conn.execute(
            f"UPDATE product_costs SET {set_clause} WHERE id=?",
            (*fields.values(), cost_id),
        )
        return True


def delete_product_cost(cost_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM product_costs WHERE id=?", (cost_id,))
        return cur.rowcount > 0


def get_ingest_logs(data_type: Optional[str] = None, limit: int = 20) -> list[dict]:
    """수집 이력 조회"""
    with get_db() as conn:
        query = "SELECT * FROM ingest_log"
        params = []
        if data_type:
            query += " WHERE data_type = ?"
            params.append(data_type)
        query += " ORDER BY ran_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


# ──────────────── 변경 메모 (change_notes) ────────────────

def get_change_notes(vendor_item_id: int) -> list[dict]:
    """변경 메모 조회 (내림차순)"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, note_date, change_type, note, created_at
               FROM change_notes
               WHERE vendor_item_id = ?
               ORDER BY note_date DESC, id DESC""",
            (vendor_item_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def add_change_note(vendor_item_id: int, note_date: str, change_type: str, note: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO change_notes (vendor_item_id, note_date, change_type, note)
               VALUES (?, ?, ?, ?)""",
            (vendor_item_id, note_date, change_type, note),
        )
        return cur.lastrowid


def delete_change_note(note_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM change_notes WHERE id=?", (note_id,))
        return cur.rowcount > 0


# ──────────────── 데이터 수집 상태 ────────────────

def get_collection_status() -> dict:
    """최근 수집 상태 요약"""
    with get_db() as conn:
        latest_sales = conn.execute(
            "SELECT MAX(stat_date) FROM daily_sales"
        ).fetchone()[0]
        latest_ads = conn.execute(
            "SELECT MAX(stat_date) FROM daily_ads"
        ).fetchone()[0]
        latest_log = conn.execute(
            "SELECT ran_at, status, message FROM ingest_log ORDER BY ran_at DESC LIMIT 1"
        ).fetchone()
        sales_count = conn.execute(
            "SELECT COUNT(DISTINCT vendor_item_id) FROM daily_sales WHERE stat_date = ?",
            (latest_sales,) if latest_sales else ("",),
        ).fetchone()[0]
        return {
            "latest_sales": latest_sales,
            "latest_ads": latest_ads,
            "latest_log": dict(latest_log) if latest_log else None,
            "latest_sales_products": sales_count,
        }


def get_product_daily(vendor_item_id: int, from_date: Optional[str] = None, to_date: Optional[str] = None) -> list[dict]:
    """상품별 일별 데이터 (차트용 필드 포함)"""
    query = """
        SELECT s.stat_date, s.units_sold, s.total_views,
               a.ad_units, a.ad_clicks, a.ad_spend,
               CASE WHEN s.units_sold IS NULL OR a.ad_units IS NULL THEN NULL
                    WHEN s.units_sold - a.ad_units < 0 THEN 0
                    ELSE s.units_sold - a.ad_units END AS organic_units,
               CASE WHEN s.total_views IS NULL OR a.ad_clicks IS NULL THEN NULL
                    WHEN s.total_views - a.ad_clicks < 0 THEN 0
                    ELSE s.total_views - a.ad_clicks END AS organic_views,
               CASE WHEN s.units_sold IS NOT NULL AND s.units_sold > 0
                    THEN ROUND(CAST(s.units_sold AS REAL) /
                         NULLIF(s.total_views, 0) * 100, 2)
                    ELSE NULL END AS conversion_rate,
               c.sale_price - c.purchase_cost - COALESCE(c.commission_fee, 0)
                 - COALESCE(c.fulfillment_fee, 0) - COALESCE(c.other_unit_cost, 0) AS unit_margin,
               CASE WHEN s.units_sold IS NULL THEN NULL
                    ELSE s.units_sold * (c.sale_price - c.purchase_cost
                      - COALESCE(c.commission_fee, 0) - COALESCE(c.fulfillment_fee, 0)
                      - COALESCE(c.other_unit_cost, 0))
                      - COALESCE(a.ad_spend, 0) * 1.1
               END AS net_profit
        FROM daily_sales s
        LEFT JOIN daily_ads a ON a.stat_date = s.stat_date AND a.vendor_item_id = s.vendor_item_id
        LEFT JOIN product_costs c ON c.vendor_item_id = s.vendor_item_id
              AND c.effective_from = (
                    SELECT MAX(effective_from) FROM product_costs
                    WHERE vendor_item_id = s.vendor_item_id AND effective_from <= s.stat_date)
        WHERE s.vendor_item_id = ?
    """
    params: list = [vendor_item_id]
    if from_date:
        query += " AND s.stat_date >= ?"
        params.append(from_date)
    if to_date:
        query += " AND s.stat_date <= ?"
        params.append(to_date)
    query += " ORDER BY s.stat_date DESC"

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
