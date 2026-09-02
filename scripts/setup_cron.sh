#!/bin/bash
# 서버에서 실행: bash /opt/coupang-profit/scripts/setup_cron.sh
# 매일 새벽 3시에 전날 데이터 수집

CRON_JOB="0 3 * * * cd /opt/coupang-profit && /opt/coupang-profit/.venv/bin/python scripts/collect.py >> /var/log/coupang-collect.log 2>&1"

# 기존 cron에 이미 있는지 확인
(crontab -l 2>/dev/null | grep -q "coupang-profit/scripts/collect.py") && {
    echo "이미 등록되어 있습니다."
    crontab -l | grep "collect.py"
    exit 0
}

# 추가
(crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
echo "cron 등록 완료:"
echo "  $CRON_JOB"
echo ""
echo "로그 확인: tail -f /var/log/coupang-collect.log"
