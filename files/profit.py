"""
쿠팡 상품별 일 순이익 계산 엔진
요청서 2·5번 계산식 구현
"""
from dataclasses import dataclass
from datetime import date
from typing import Optional
import bisect

AD_VAT_RATE = 1.1  # 요청서: 광고비 × 1.1


@dataclass
class CostRecord:
    """적용시작일 기준 원가. 요청서 4-3: 과거 순이익 불변 보장."""
    effective_from: date
    sale_price: float        # 판매가
    purchase_cost: float     # 매입원가 (원화 환산 후)
    fulfillment_fee: float   # 입출고 수수료(부가세 포함)

    @property
    def unit_margin(self) -> float:
        """개당순마진 = 판매가 - 매입원가 - 입출고수수료"""
        return self.sale_price - self.purchase_cost - self.fulfillment_fee


class CostHistory:
    """상품별 원가 이력. 특정 날짜 시점의 원가를 조회."""

    def __init__(self, records: list[CostRecord]):
        self._recs = sorted(records, key=lambda r: r.effective_from)
        self._keys = [r.effective_from for r in self._recs]

    def at(self, on: date) -> Optional[CostRecord]:
        """on 날짜에 유효한 원가. 이력보다 이른 날짜면 None."""
        i = bisect.bisect_right(self._keys, on) - 1
        return self._recs[i] if i >= 0 else None


@dataclass
class DailyMetrics:
    """요청서 5번 지표. None = 미수집, 0 = 실제 0 (요청서 7-2)."""
    units_sold: Optional[int] = None       # 총 판매량
    ad_units: Optional[int] = None         # 광고 판매량
    total_views: Optional[int] = None      # 총 조회수
    ad_clicks: Optional[int] = None        # 광고 클릭수
    ad_spend: Optional[float] = None       # 광고비 (부가세 제외)


@dataclass
class DailyResult:
    net_profit: Optional[float] = None
    unit_margin: Optional[float] = None
    ad_spend_vat: Optional[float] = None
    organic_units: Optional[int] = None
    organic_views: Optional[int] = None
    organic_ratio: Optional[float] = None
    warnings: list[str] = None


def calc_daily(metrics: DailyMetrics, cost: Optional[CostRecord]) -> DailyResult:
    """
    상품별 일 순이익 = 총 판매량 × 개당순마진 - 광고비 × 1.1
    """
    w = []

    if cost is None:
        w.append("COST_MISSING: 해당 날짜에 유효한 원가 없음")
        return DailyResult(warnings=w)

    margin = cost.unit_margin
    ad_vat = None if metrics.ad_spend is None else metrics.ad_spend * AD_VAT_RATE

    # 순이익: 판매량과 광고비 둘 다 있어야 확정. 광고 미집행(None)은 0으로 간주하지 않음
    if metrics.units_sold is None:
        w.append("UNITS_MISSING: 판매량 미수집")
        net = None
    else:
        net = metrics.units_sold * margin - (ad_vat or 0.0)
        if ad_vat is None:
            w.append("AD_SPEND_MISSING: 광고비 미수집, 0으로 계산됨")

    # 자연 판매량 (요청서 5: 0 미만 방지)
    organic_u = None
    if metrics.units_sold is not None and metrics.ad_units is not None:
        raw = metrics.units_sold - metrics.ad_units
        if raw < 0:
            w.append(f"NEGATIVE_ORGANIC_UNITS: 광고판매({metrics.ad_units}) > 총판매({metrics.units_sold})")
            organic_u = 0
        else:
            organic_u = raw

    # 자연 조회수 (요청서 5: 정의 확인 필요 항목)
    organic_v = None
    if metrics.total_views is not None and metrics.ad_clicks is not None:
        raw = metrics.total_views - metrics.ad_clicks
        if raw < 0:
            w.append(f"NEGATIVE_ORGANIC_VIEWS: 광고클릭({metrics.ad_clicks}) > 총조회({metrics.total_views})")
            organic_v = 0
        else:
            organic_v = raw

    # 자연판매 비중 (요청서 5: 총 판매량 0이면 0)
    ratio = None
    if organic_u is not None and metrics.units_sold is not None:
        ratio = 0.0 if metrics.units_sold == 0 else organic_u / metrics.units_sold * 100

    return DailyResult(
        net_profit=net, unit_margin=margin, ad_spend_vat=ad_vat,
        organic_units=organic_u, organic_views=organic_v,
        organic_ratio=ratio, warnings=w,
    )
