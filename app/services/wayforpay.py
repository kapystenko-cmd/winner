"""
WayForPay підписки для ocinka.pro.

Модель: токенізація (recToken) + власне щомісячне списання.
  1) Перший платіж — Purchase: клієнт платить на стороні WayForPay
     (картка / Apple Pay / Google Pay). У колбеку приходить recToken.
  2) Далі щомісячне списання робимо самі через CHARGE по recToken.

recToken повертається і для Apple Pay / Google Pay (підтверджено саппортом WayForPay),
тож рекурент працює для всіх способів оплати.

Порядок полів для HMAC-MD5 підпису звірено з docs.wayforpay:
  Purchase / CHARGE:  merchantAccount;merchantDomainName;orderReference;orderDate;
                      amount;currency;productName[*];productCount[*];productPrice[*]
  Колбек (перевірка): merchantAccount;orderReference;amount;currency;
                      authCode;cardPan;transactionStatus;reasonCode
  Відповідь на колбек: orderReference;status;time

Ключі — з settings (.env): WAYFORPAY_MERCHANT / WAYFORPAY_DOMAIN / WAYFORPAY_SECRET_KEY / SITE_URL.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import parse_qs, unquote_plus

import httpx

from app.core.config import settings

WFP_API_URL = "https://api.wayforpay.com/api"      # CHARGE / host2host
WFP_PAY_URL = "https://secure.wayforpay.com/pay"   # перший платіж (форма)
CURRENCY = "UAH"


def _site_url() -> str:
    return (settings.site_url or "https://ocinka.pro").rstrip("/")


def service_url() -> str:
    return f"{_site_url()}/api/payments/wayforpay/callback"


def return_url() -> str:
    return f"{_site_url()}/payment-result.html"


# Тарифи: code == значення enum SubscriptionPlan. quota (к-сть звітів) задається
# у app/api/routes.py (plan_limits) — тут лише ціна і назва для платежу.
PLANS = {
    "basic":    {"title": "Старт — 10 звітів на місяць",    "price": 990},
    "standard": {"title": "Про — 30 звітів на місяць",      "price": 2000},
    "pro":      {"title": "Максимум — 60 звітів на місяць",  "price": 3500},
}


def _sign(fields) -> str:
    """HMAC-MD5 по рядку полів через ';' (UTF-8)."""
    msg = ";".join("" if f is None else str(f) for f in fields)
    return hmac.new(
        (settings.wayforpay_secret_key or "").encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.md5,
    ).hexdigest()


def new_order_reference(user_id, plan_code: str) -> str:
    return f"ocinka-{plan_code}-{user_id}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _product(plan_code: str):
    plan = PLANS[plan_code]
    return [plan["title"]], [1], [plan["price"]]  # name[], count[], price[]


def build_purchase_fields(order_reference: str, plan_code: str, client: dict) -> dict:
    """Параметри форми першого платежу. Фронт сабмітить їх POST-ом на WFP_PAY_URL."""
    amount = PLANS[plan_code]["price"]
    order_date = int(time.time())
    names, counts, prices = _product(plan_code)

    signature = _sign([
        settings.wayforpay_merchant, settings.wayforpay_domain,
        order_reference, order_date, amount, CURRENCY,
        *names, *counts, *prices,
    ])
    return {
        "merchantAccount": settings.wayforpay_merchant,
        "merchantDomainName": settings.wayforpay_domain,
        "merchantSignature": signature,
        "merchantAuthType": "SimpleSignature",
        "orderReference": order_reference,
        "orderDate": order_date,
        "amount": amount,
        "currency": CURRENCY,
        "productName": names,
        "productPrice": prices,
        "productCount": counts,
        "clientFirstName": client.get("first_name", ""),
        "clientLastName": client.get("last_name", ""),
        "clientEmail": client.get("email", ""),
        "clientPhone": client.get("phone", ""),
        "language": "UA",
        "returnUrl": return_url(),
        "serviceUrl": service_url(),
    }


async def charge_by_token(order_reference: str, plan_code: str, rec_token: str, client: dict) -> dict:
    """Щомісячне списання по recToken (merchant-initiated, без клієнта)."""
    amount = PLANS[plan_code]["price"]
    order_date = int(time.time())
    names, counts, prices = _product(plan_code)

    signature = _sign([
        settings.wayforpay_merchant, settings.wayforpay_domain,
        order_reference, order_date, amount, CURRENCY,
        *names, *counts, *prices,
    ])
    payload = {
        "transactionType": "CHARGE",
        "merchantAccount": settings.wayforpay_merchant,
        "merchantDomainName": settings.wayforpay_domain,
        "merchantSignature": signature,
        "apiVersion": 1,
        "orderReference": order_reference,
        "orderDate": order_date,
        "amount": amount,
        "currency": CURRENCY,
        "productName": names,
        "productPrice": prices,
        "productCount": counts,
        "recToken": rec_token,
        "merchantTransactionType": "SALE",
        "merchantTransactionSecureType": "AUTO",
        "clientFirstName": client.get("first_name", ""),
        "clientLastName": client.get("last_name", ""),
        "clientEmail": client.get("email", ""),
        "clientPhone": client.get("phone", ""),
    }
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(WFP_API_URL, json=payload)
        try:
            return r.json()
        except Exception:
            return {"transactionStatus": "Error", "reason": r.text, "reasonCode": "-1"}


def parse_callback(raw: bytes) -> dict:
    """WayForPay може слати JSON у тілі або form-urlencoded, де JSON — ключ/значення."""
    text = (raw or b"").decode("utf-8", "ignore").strip()
    if not text:
        return {}
    if text.lstrip().startswith("{"):
        return json.loads(text)
    decoded = unquote_plus(text)
    i, j = decoded.find("{"), decoded.rfind("}")
    if i != -1 and j != -1 and j > i:
        return json.loads(decoded[i:j + 1])
    qs = parse_qs(text, keep_blank_values=True)
    for part in [*qs.keys(), *(v for vals in qs.values() for v in vals)]:
        d = unquote_plus(part).strip()
        if d.startswith("{"):
            return json.loads(d)
    return {}


def verify_callback(data: dict) -> bool:
    expected = _sign([
        data.get("merchantAccount"),
        data.get("orderReference"),
        data.get("amount"),
        data.get("currency"),
        data.get("authCode"),
        data.get("cardPan"),
        data.get("transactionStatus"),
        data.get("reasonCode"),
    ])
    return hmac.compare_digest(expected, str(data.get("merchantSignature", "")))


def callback_response(order_reference: str) -> dict:
    """Обов'язкова відповідь WayForPay на колбек (інакше шле повтори)."""
    t = int(time.time())
    return {
        "orderReference": order_reference,
        "status": "accept",
        "time": t,
        "signature": _sign([order_reference, "accept", t]),
    }
