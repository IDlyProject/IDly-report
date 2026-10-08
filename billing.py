"""Paddle Billing 결제 (MoR).

결제창
- 화면에서 Paddle.js 오버레이 결제창을 연다 (PADDLE_CLIENT_TOKEN, PADDLE_PRICE_ID)
- custom data에 report_id·user_id를 넣어, 웹훅이 오면 그 사용자의 그 리포트만 연다

웹훅 (POST /api/webhooks/paddle)
- Paddle-Signature: ts=<초>;h1=<HMAC-SHA256(서명 비밀값, "ts:요청 원문")> 를 확인한다
- transaction.completed → 결제 완료
- adjustment.created/updated (action=refund·chargeback, status=approved) → 환불 (리포트 다시 잠금)
- 클라이언트 토큰은 화면에 노출되는 공개 값이다. 웹훅 비밀값은 서버 환경변수에만 둔다
"""

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

# sandbox | production (클라이언트 토큰도 같은 환경 것이어야 한다: test_… / live_…)
ENVIRONMENT = os.getenv("PADDLE_ENV", "production")
CLIENT_TOKEN = os.getenv("PADDLE_CLIENT_TOKEN", "")
PRICE_ID = os.getenv("PADDLE_PRICE_ID", "")
WEBHOOK_SECRET = os.getenv("PADDLE_WEBHOOK_SECRET", "")
# 서명 시각이 이보다 오래된 웹훅은 재전송 공격으로 보고 버린다
SIGNATURE_TOLERANCE = 300


class BillingError(Exception):
    pass


def checkout_options(report_id: str, user_id: str, email: Optional[str]) -> Dict[str, Any]:
    """화면이 Paddle.Checkout.open에 넘길 값."""
    if not (CLIENT_TOKEN and PRICE_ID):
        raise BillingError("PADDLE_CLIENT_TOKEN·PADDLE_PRICE_ID가 설정되지 않았어요.")
    return {
        "environment": ENVIRONMENT,
        "token": CLIENT_TOKEN,
        "priceId": PRICE_ID,
        "email": email or None,
        "customData": {"report_id": report_id, "user_id": user_id},
    }


def verify_signature(raw: bytes, header: str) -> bool:
    if not WEBHOOK_SECRET or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(";") if "=" in p)
    ts, sig = parts.get("ts", ""), parts.get("h1", "")
    if not ts.isdigit() or not sig or abs(time.time() - int(ts)) > SIGNATURE_TOLERANCE:
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode(), ts.encode() + b":" + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _amount(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_webhook(raw: bytes) -> Optional[Dict[str, Any]]:
    """우리가 처리할 이벤트만 {event, order_id, report_id, user_id, status, amount, currency}로 돌려준다.
    환불(adjustment)에는 custom data가 없다. report_id 없이 order_id(거래 id)만 주고, 호출한 쪽이 결제 기록에서 찾는다."""
    payload = json.loads(raw)
    event = payload.get("event_type")
    data = payload.get("data") or {}
    if event == "transaction.completed":
        custom = data.get("custom_data") or {}
        # 우리 상품 가격으로 결제한 거래만 (다른 가격 id로 연 결제창은 무시)
        prices = {((item.get("price") or {}).get("id")) for item in data.get("items") or []}
        if not custom.get("report_id") or PRICE_ID not in prices:
            return None
        totals = (data.get("details") or {}).get("totals") or {}
        return {
            "event": event,
            "order_id": str(data.get("id")),
            "report_id": custom["report_id"],
            "user_id": custom.get("user_id"),
            "status": "paid",
            "amount": _amount(totals.get("grand_total")),
            "currency": data.get("currency_code"),
        }
    if event in ("adjustment.created", "adjustment.updated"):
        if data.get("action") not in ("refund", "chargeback") or data.get("status") != "approved":
            return None
        totals = data.get("totals") or {}
        return {
            "event": event,
            "order_id": str(data.get("transaction_id")),
            "report_id": None,
            "user_id": None,
            "status": "refunded",
            "amount": _amount(totals.get("total")),
            "currency": data.get("currency_code"),
        }
    return None
