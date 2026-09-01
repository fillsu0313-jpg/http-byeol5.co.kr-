"""
쿠팡 WING/오픈 API 클라이언트
인증: HMAC-SHA256
"""
import hashlib
import hmac
import json
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import requests

# 쿠팡 API 기본 URL
BASE_URL = "https://api-gateway.coupang.com"


class CoupangAPIError(Exception):
    """API 호출 실패"""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"[{status_code}] {message}")


class CoupangAPI:
    """
    쿠팡 오픈 API 래퍼.
    사용 전 config.json에 access_key, secret_key, vendor_id 필요.
    """

    def __init__(self, access_key: str, secret_key: str, vendor_id: str):
        self.access_key = access_key
        self.secret_key = secret_key
        self.vendor_id = vendor_id
        self._session = requests.Session()
        self._last_request_time = 0.0

    def _sign(self, method: str, path: str, query: str = "") -> dict:
        """HMAC-SHA256 서명 생성 → Authorization 헤더 반환"""
        dt = datetime.utcnow().strftime("%y%m%dT%H%M%SZ")
        message = f"{dt}{method}{path}{query}"
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        auth = (
            f"CEA algorithm=HmacSHA256, "
            f"access-key={self.access_key}, "
            f"signed-date={dt}, "
            f"signature={signature}"
        )
        return {
            "Authorization": auth,
            "Content-Type": "application/json;charset=UTF-8",
        }

    def _rate_limit(self):
        """5 req/sec 제한 대응: 요청 간 최소 0.2초"""
        elapsed = time.time() - self._last_request_time
        if elapsed < 0.2:
            time.sleep(0.2 - elapsed)
        self._last_request_time = time.time()

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET 요청 + 인증 + Rate limit"""
        self._rate_limit()
        query = urlencode(params) if params else ""
        url = BASE_URL + path
        if query:
            url += "?" + query
        headers = self._sign("GET", path, query)
        resp = self._session.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise CoupangAPIError(resp.status_code, resp.text[:500])
        return resp.json()

    def get_revenue_history(
        self, date_from: str, date_to: str
    ) -> list[dict]:
        """
        판매 데이터 조회 (페이징 자동 처리).
        date_from, date_to: 'YYYY-MM-DD' 형식
        반환: [{vendorItemId, orderedDate, revenue, quantity, ...}, ...]
        """
        path = "/v2/providers/openapi/apis/api/v1/revenue-history"
        all_items = []
        next_token = None

        while True:
            params = {
                "vendorId": self.vendor_id,
                "recognizedFrom": date_from,
                "recognizedTo": date_to,
                "maxPerPage": 100,
            }
            if next_token:
                params["nextToken"] = next_token

            data = self._get(path, params)
            items = data.get("data", [])
            all_items.extend(items)

            next_token = data.get("nextToken")
            if not next_token or not items:
                break

        return all_items

    def get_order_sheets(
        self,
        date_from: str,
        date_to: str,
        status: str = "FINAL_DELIVERY",
    ) -> list[dict]:
        """
        주문 데이터 조회 (페이징 자동 처리).
        status: ACCEPT, INSTRUCT, DEPARTURE, DELIVERING, FINAL_DELIVERY
        반환: [{orderId, vendorItemId, shippingCount, orderedAt, ...}, ...]
        """
        path = f"/v2/providers/openapi/apis/api/v5/vendors/{self.vendor_id}/ordersheets"
        all_items = []
        next_token = None

        while True:
            params = {
                "createdAtFrom": date_from,
                "createdAtTo": date_to,
                "status": status,
                "maxPerPage": 50,
            }
            if next_token:
                params["nextToken"] = next_token

            data = self._get(path, params)
            items = data.get("data", [])
            all_items.extend(items)

            next_token = data.get("nextToken")
            if not next_token or not items:
                break

        return all_items


def load_config(config_path: str = None) -> dict:
    """config.json에서 API 키 로드"""
    from pathlib import Path
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config.json"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(
            f"설정 파일을 찾을 수 없습니다: {config_path}\n"
            "config.example.json을 복사하여 config.json을 만들고 API 키를 입력하세요."
        )

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    required = ["access_key", "secret_key", "vendor_id"]
    missing = [k for k in required if not config.get(k)]
    if missing:
        raise ValueError(f"config.json에 누락된 필드: {', '.join(missing)}")

    return config


def create_client(config_path: str = None) -> CoupangAPI:
    """config.json 로드 → CoupangAPI 인스턴스 생성"""
    cfg = load_config(config_path)
    return CoupangAPI(cfg["access_key"], cfg["secret_key"], cfg["vendor_id"])
