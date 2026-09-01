"""7월 엑셀 2,026행을 계산 엔진에 통과시켜 검증"""
import pandas as pd, numpy as np, sys
from datetime import date
from profit import CostRecord, CostHistory, DailyMetrics, calc_daily

df = pd.read_excel('/mnt/user-data/uploads/7월_상품별_흐름__1.xlsx', sheet_name='상품별흐름-판매')
d = df[df['상품명'].notna()].copy()
for c in ['판매가','단가','개당순마진','판매량','광고','총 조회수','광고 클릭수','광고비','수입']:
    d[c] = pd.to_numeric(d[c], errors='coerce')
d['dt'] = pd.to_datetime(d['일자'].astype(str).str.replace('.','-',regex=False), errors='coerce').dt.date

# 원가 이력 로드
cm = pd.read_csv('cost_master.csv')
cm['적용시작일'] = pd.to_datetime(cm['적용시작일']).dt.date
hist = {}
for name, g in cm.groupby('상품명'):
    recs = []
    for _, r in g.iterrows():
        # 입출고수수료 = 판매가 - 매입단가 - 개당순마진 (역산)
        fee = r['판매가'] - r['매입단가'] - r['개당순마진']
        recs.append(CostRecord(r['적용시작일'], r['판매가'], r['매입단가'], fee))
    hist[name] = CostHistory(recs)

ok = mismatch = skipped = 0
diffs = []
warn_counts = {}

for _, r in d.iterrows():
    h = hist.get(r['상품명'])
    cost = h.at(r['dt']) if h else None
    m = DailyMetrics(
        units_sold = None if pd.isna(r['판매량']) else int(r['판매량']),
        ad_units   = None if pd.isna(r['광고']) else int(r['광고']),
        total_views= None if pd.isna(r['총 조회수']) else int(r['총 조회수']),
        ad_clicks  = None if pd.isna(r['광고 클릭수']) else int(r['광고 클릭수']),
        ad_spend   = None if pd.isna(r['광고비']) else r['광고비']/1.1,  # 엑셀 광고비열은 이미 ×1.1
    )
    res = calc_daily(m, cost)
    for w in (res.warnings or []):
        k = w.split(':')[0]; warn_counts[k] = warn_counts.get(k,0)+1

    if res.net_profit is None or pd.isna(r['수입']):
        skipped += 1; continue
    delta = res.net_profit - r['수입']
    if abs(delta) < 1:
        ok += 1
    else:
        mismatch += 1
        diffs.append((r['일자'], r['상품명'], round(r['수입']), round(res.net_profit), round(delta)))

print(f"검증 대상 {len(d)}행")
print(f"  일치      {ok}")
print(f"  불일치    {mismatch}")
print(f"  계산불가  {skipped}")
print()
print("=== 경고 유형별 발생 건수 ===")
for k,v in sorted(warn_counts.items(), key=lambda x:-x[1]): print(f"  {k}: {v}")
print()
if diffs:
    dd = pd.DataFrame(diffs, columns=['일자','상품명','엑셀','엔진','차이'])
    print(f"=== 불일치 상위 (금액 절대값 기준) ===")
    print(dd.reindex(dd['차이'].abs().sort_values(ascending=False).index).head(15).to_string(index=False))
    print()
    print("불일치 상품별 집계:")
    print(dd.groupby('상품명')['차이'].agg(['count','sum']).sort_values('count',ascending=False).head(10).to_string())
