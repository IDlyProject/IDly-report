"""Lemon Squeezy 결제 (MoR).

결제창
- LEMONSQUEEZY_API_KEY·STORE_ID·VARIANT_ID가 있으면 Checkout API로 사용자마다 결제창을 만든다
  (결제 후 돌아올 주소, 이메일 미리 채우기, custom data)
- 없으면 고정 결제 링크(LEMONSQUEEZY_CHECKOUT_URL)에 같은 custom data를 쿼리로 붙인다
- 어느 쪽이든 custom data에 report_id·user_id를 넣어, 웹훅이 오면 그 사용자의 그 리포트만 연다

웹훅 (POST /api/webhooks/lemonsqueezy)
- X-Signature = HMAC-SHA256(서명 비밀값, 요청 원문) 을 확인한다
- order_created(status=paid) → 결제 완료, order_refunded → 환불(리포트 다시 잠금)
- API 키는 서버 환경변수에만 둔다. 화면 코드·저장소에 넣지 않는다
"""

import hashlib
import hmac
import json
import os
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import httpx

CHECKOUT_URL = os.getenv(
    "LEMONSQUEEZY_CHECKOUT_URL", "https://idly.lemonsqueezy.com/checkout/buy/5f3c2eed-bafc-44db-9f11-2ff201c1fbe7"
)
API_KEY = os.getenv("LEMONSQUEEZY_API_KEY", "")
STORE_ID = os.getenv("LEMONSQUEEZY_STORE_ID", "")
VARIANT_ID = os.getenv("LEMONSQUEEZY_VARIANT_ID", "")
WEBHOOK_SECRET = os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET", "")


class BillingError(Exception):
    pass


def create_checkout(report_id: str, user_id: str, email: Optional[str], return_url: str) -> str:
    """결제창 주소. embed=1이면 화면 위 오버레이(Lemon.js)로 열린다."""
    custom = {"report_id": report_id, "user_id": user_id}
    if API_KEY and STORE_ID and VARIANT_ID:
        body = {
            "data": {
                "type": "checkouts",
                "attributes": {
                    "product_options": {"redirect_url": return_url},
                    "checkout_options": {"embed": True},
                    "checkout_data": {**({"email": email} if email else {}), "custom": custom},
                },
                "relationships": {
                    "store": {"data": {"type": "stores", "id": STORE_ID}},
                    "variant": {"data": {"type": "variants", "id": VARIANT_ID}},
                },
            }
        }
        res = httpx.post("https://api.lemonsqueezy.com/v1/checkouts", json=body, timeout=20, headers={
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
        })
        if res.status_code >= 300:
            raise BillingError(f"결제창을 만들지 못했어요 ({res.status_code}): {res.text[:300]}")
        return res.json()["data"]["attributes"]["url"]

    # 고정 링크 + 쿼리로 custom data·이메일
    parts = urlparse(CHECKOUT_URL)
    query = dict(parse_qsl(parts.query))
    query.update({f"checkout[custom][{k}]": v for k, v in custom.items()})
    if email:
        query["checkout[email]"] = email
    query["embed"] = "1"
    return urlunparse(parts._replace(query=urlencode(query)))


def verify_signature(raw: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET or not signature:
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_webhook(raw: bytes) -> Optional[Dict[str, Any]]:
    """우리가 처리할 이벤트만 {event, order_id, report_id, user_id, status, amount, currency}로 돌려준다."""
    payload = json.loads(raw)
    meta = payload.get("meta") or {}
    event = meta.get("event_name")
    if event not in ("order_created", "order_refunded"):
        return None
    attrs = (payload.get("data") or {}).get("attributes") or {}
    custom = meta.get("custom_data") or {}
    if not custom.get("report_id"):
        return None
    paid = event == "order_created" and attrs.get("status") == "paid"
    return {
        "event": event,
        "order_id": str((payload.get("data") or {}).get("id")),
        "report_id": custom["report_id"],
        "user_id": custom.get("user_id"),
        "status": "paid" if paid else "refunded" if event == "order_refunded" or attrs.get("refunded") else attrs.get("status"),
        "amount": attrs.get("total"),
        "currency": attrs.get("currency"),
        "test_mode": bool(attrs.get("test_mode")),
    }
