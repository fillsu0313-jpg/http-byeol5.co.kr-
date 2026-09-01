-- 쿠팡 상품별 일 순이익 관리 — DB 스키마
-- 요청서 4-3, 6-3, 7-1, 7-2 대응

-- ─────────────────────────────────────────────
-- 상품 마스터
-- 기준 키는 vendor_item_id(옵션ID). 변경되지 않는 ID이므로 안전.
-- product_id(노출상품ID)는 링크 생성용이며 머지/분리로 바뀔 수 있어 별도 보관.
-- ─────────────────────────────────────────────
CREATE TABLE products (
    vendor_item_id   BIGINT PRIMARY KEY,       -- 옵션ID (불변, 조인 키)
    seller_product_id BIGINT,                  -- 등록상품ID (불변, 묶음 관리용)
    product_id       BIGINT,                   -- 노출상품ID (링크용, 변경 가능)
    coupang_name     TEXT NOT NULL,            -- 쿠팡 등록상품명
    display_name     TEXT,                     -- 대표님이 쓰는 이름 ("네일자석")
    parent_group     TEXT,                     -- 대표상품 합산용 (요청서 7-1)
    is_active        BOOLEAN DEFAULT TRUE,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_products_display ON products(display_name);
CREATE INDEX idx_products_group   ON products(parent_group);

-- ─────────────────────────────────────────────
-- 원가 이력  ★요청서 4-3의 핵심
-- "원가를 변경해도 변경일 이전 계산 결과는 유지"
-- 7월 실측: 68개 상품에 127개 이력 발생 (상품당 월 1.9회 변경)
-- ─────────────────────────────────────────────
CREATE TABLE product_costs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_item_id   BIGINT NOT NULL REFERENCES products(vendor_item_id),
    effective_from   DATE   NOT NULL,          -- 적용 시작일
    sale_price       NUMERIC(12,2) NOT NULL,   -- 판매가
    purchase_cost_fx NUMERIC(12,4),            -- 매입단가 (위안)
    fx_rate          NUMERIC(10,4),            -- 적용 환율 (현재 엑셀 330 고정)
    purchase_cost    NUMERIC(12,2) NOT NULL,   -- 매입원가 (원화) = fx × rate
    commission_fee   NUMERIC(12,2) DEFAULT 0,  -- 판매수수료
    fulfillment_fee  NUMERIC(12,2) DEFAULT 0,  -- 입출고·물류비 (부가세 포함)
    other_unit_cost  NUMERIC(12,2) DEFAULT 0,  -- 기타 개당 비용
    memo             TEXT,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (vendor_item_id, effective_from)
);
CREATE INDEX idx_costs_lookup ON product_costs(vendor_item_id, effective_from DESC);

-- 개당순마진 = sale_price - purchase_cost - commission_fee
--            - fulfillment_fee - other_unit_cost
-- ※ 값으로 저장하지 않고 조회 시 계산. 하드코딩 오류(7월 소음방지 29행) 원천 차단.

-- ─────────────────────────────────────────────
-- 일별 판매 실적 (쿠팡 API 또는 엑셀 업로드)
-- NULL = 미수집, 0 = 실제 0  (요청서 7-2)
-- ─────────────────────────────────────────────
CREATE TABLE daily_sales (
    stat_date      DATE   NOT NULL,
    vendor_item_id BIGINT NOT NULL REFERENCES products(vendor_item_id),
    units_sold     INTEGER,                    -- 총 판매량
    gross_revenue  NUMERIC(14,2),              -- 매출
    total_views    INTEGER,                    -- 총 조회수
    cancel_units   INTEGER,                    -- 취소 (※ 정의 확인 필요)
    return_units   INTEGER,                    -- 반품 (※ 정의 확인 필요)
    source         TEXT NOT NULL,              -- 'api' | 'upload'
    collected_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stat_date, vendor_item_id)
);

-- ─────────────────────────────────────────────
-- 일별 광고 실적 (광고 API 제한 → 보고서 업로드 병행)
-- ─────────────────────────────────────────────
CREATE TABLE daily_ads (
    stat_date      DATE   NOT NULL,
    vendor_item_id BIGINT NOT NULL REFERENCES products(vendor_item_id),
    impressions    INTEGER,                    -- 노출수
    ad_clicks      INTEGER,                    -- 광고 클릭수
    ad_units       INTEGER,                    -- 광고 판매량
    ad_spend       NUMERIC(14,2),              -- 광고비 (부가세 제외 · 원본 그대로)
    source         TEXT NOT NULL,
    collected_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stat_date, vendor_item_id)
);
-- ※ ad_spend는 원본값 저장. ×1.1은 조회 시 적용.
--   부가세율이 바뀌거나 정의가 달라져도 원본이 남아있어야 재계산 가능.

-- ─────────────────────────────────────────────
-- 일회성 투자비용  (요청서 9-4, 공고 4번)
-- 개당 원가와 성격이 달라 분리. 회수기간 산출용.
-- ─────────────────────────────────────────────
CREATE TABLE product_investments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_item_id BIGINT NOT NULL REFERENCES products(vendor_item_id),
    spent_on       DATE   NOT NULL,
    category       TEXT   NOT NULL,  -- 상세페이지|외부광고|초도상품대금|운송비|디자인|인증|포장|기타
    amount         NUMERIC(14,2) NOT NULL,
    memo           TEXT
);
CREATE INDEX idx_invest_item ON product_investments(vendor_item_id, spent_on);

-- ─────────────────────────────────────────────
-- 변경 메모  (요청서 4-2: 가격/광고 변경 전후 성과 비교)
-- ─────────────────────────────────────────────
CREATE TABLE change_notes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_item_id BIGINT REFERENCES products(vendor_item_id),
    note_date      DATE NOT NULL,
    change_type    TEXT,             -- 가격|광고|상세페이지|기타
    note           TEXT NOT NULL,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────────────────────
-- 수집 이력  (요청서 6-4: 누락·중복 표시)
-- ─────────────────────────────────────────────
CREATE TABLE ingest_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stat_date    DATE NOT NULL,
    data_type    TEXT NOT NULL,      -- 'sales' | 'ads'
    source       TEXT NOT NULL,
    row_count    INTEGER,
    status       TEXT NOT NULL,      -- ok | partial | failed | duplicate
    message      TEXT,
    ran_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────────────────────
-- 일별 대시보드 조회 (요청서 4-1)
-- 원가는 stat_date 시점 이력을 조인 → 과거 순이익 불변
-- ─────────────────────────────────────────────
CREATE VIEW v_daily_profit AS
SELECT
    s.stat_date,
    p.display_name,
    p.parent_group,
    s.units_sold,
    a.ad_units,
    CASE WHEN s.units_sold IS NULL OR a.ad_units IS NULL THEN NULL
         WHEN s.units_sold - a.ad_units < 0 THEN 0
         ELSE s.units_sold - a.ad_units END              AS organic_units,
    s.total_views,
    a.ad_clicks,
    CASE WHEN s.total_views IS NULL OR a.ad_clicks IS NULL THEN NULL
         WHEN s.total_views - a.ad_clicks < 0 THEN 0
         ELSE s.total_views - a.ad_clicks END            AS organic_views,
    c.sale_price - c.purchase_cost - c.commission_fee
      - c.fulfillment_fee - c.other_unit_cost            AS unit_margin,
    a.ad_spend * 1.1                                     AS ad_spend_vat,
    s.units_sold * (c.sale_price - c.purchase_cost - c.commission_fee
      - c.fulfillment_fee - c.other_unit_cost)
      - COALESCE(a.ad_spend, 0) * 1.1                    AS net_profit
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
      );
