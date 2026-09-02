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
        매출내역 조회 (페이징 자동 처리, 최대 31일).
        date_from, date_to: 'YYYY-MM-DD' 형식
        반환: [{orderId, saleDate, recognitionDate, items: [{vendorItemId, quantity, salePrice, ...}], ...}, ...]
        """
        path = "/v2/providers/openapi/apis/api/v1/revenue-history"
        all_items = []
        token = ""

        while True:
            params = {
                "vendorId": self.vendor_id,
                "recognitionDateFrom": date_from,
                "recognitionDateTo": date_to,
                "token": token,
                "maxPerPage": 50,
            }

            data = self._get(path, params)
            items = data.get("data", [])
            all_items.extend(items)

            if not data.get("hasNext"):
                break
            token = data.get("nextToken", "")
            if not token:
                break

        return all_items

    def get_seller_products(self, next_token: str = None) -> list[dict]:
        """
        셀러 상품 목록 전체 조회 (페이징 자동 처리).
        반환: [{sellerProductId, vendorItemId, productId, sellerProductName, ...}, ...]
        각 상품의 옵션(vendorItem) 정보를 포함.
        """
        path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
        all_items = []
        _next = next_token

        while True:
            params = {"vendorId": self.vendor_id, "maxPerPage": 100}
            if _next:
                params["nextToken"] = _next

            data = self._get(path, params)
            items = data.get("data", [])
            all_items.extend(items)

            _next = data.get("nextToken")
            if not _next or not items:
                break

        return all_items

    def get_seller_product_detail(self, seller_product_id: int) -> dict:
        """
        셀러 상품 상세 조회 — items(옵션) 배열 포함.
        반환: {sellerProductId, items: [{vendorItemId, itemName, ...}], ...}
        """
        path = f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}"
        data = self._get(path)
        return data.get("data", data)

    def get_order_sheets(
        self,
        date_from: str,
        date_to: str,
        status: str = "FINAL_DELIVERY",
    ) -> list[dict]:
        """
        주문 데이터 조회 (페이징 자동 처리).
        date_from, date_to: 'YYYY-MM-DD' 형식 (자동으로 +09:00 추가)
        status: ACCEPT, INSTRUCT, DEPARTURE, DELIVERING, FINAL_DELIVERY
        반환: [{orderId, orderedAt, orderItems: [{vendorItemId, shippingCount, ...}], ...}, ...]
        """
        # 쿠팡 주문서 API는 yyyy-MM-dd+09:00 형식 필요
        if "+0" not in date_from:
            date_from = f"{date_from}+09:00"
        if "+0" not in date_to:
            date_to = f"{date_to}+09:00"

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
    """
    API 키 로드. 우선순위:
    1. .env 파일 (COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, COUPANG_VENDOR_ID)
    2. config.json (하위 호환)
    """
    import os
    from pathlib import Path

    try:
        from dotenv import load_dotenv
        env_path = Path(__file__).resolve().parent.parent / ".env"
        load_dotenv(env_path)
    except ImportError:
        pass

    # .env에서 읽기 시도
    env_config = {
        "access_key": os.environ.get("COUPANG_ACCESS_KEY", ""),
        "secret_key": os.environ.get("COUPANG_SECRET_KEY", ""),
        "vendor_id": os.environ.get("COUPANG_VENDOR_ID", ""),
    }

    if all(env_config.values()):
        return env_config

    # .env에 값이 없으면 config.json 폴백
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config.json"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(
            "API 키를 찾을 수 없습니다.\n"
            ".env 파일에 COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, COUPANG_VENDOR_ID를 설정하거나,\n"
            "config.json을 만들어 access_key, secret_key, vendor_id를 입력하세요."
        )

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    required = ["access_key", "secret_key", "vendor_id"]
    missing = [k for k in required if not config.get(k)]
    if missing:
        raise ValueError(f"config.json에 누락된 필드: {', '.join(missing)}")

    return config


def create_client(config_path: str = None) -> CoupangAPI:
    """설정 로드 → CoupangAPI 인스턴스 생성 (.env 우선, config.json 폴백)"""
    cfg = load_config(config_path)
    return CoupangAPI(cfg["access_key"], cfg["secret_key"], cfg["vendor_id"])
