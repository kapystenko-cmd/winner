"""
Ендпоінти підписок WayForPay.

Потік:
  POST /api/payments/subscribe          -> повертає {pay_url, fields} для форми WayForPay
  POST /api/payments/wayforpay/callback -> WayForPay шле результат (сервер→сервер)
  POST /api/payments/cancel             -> вимкнути автоподовження
  GET  /api/payments/status             -> стан підписки

Після успішної оплати виставляємо на User: plan + subscription_active_until + reports_this_month=0.
Існуюча перевірка ліміту в /reports/create і лічильник у /generate самі це застосують —
генерацію і безкоштовні 3 (free_reports_left) НЕ чіпаємо.
"""
from __future__ import annotations

import calendar
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.billing import BillingProfile, PaymentTransaction
from app.models.models import ActivityLog, SubscriptionPlan, User
from app.services import wayforpay as wfp

payments_router = APIRouter(prefix="/api/payments", tags=["payments"])


def _add_month(d: datetime) -> datetime:
    m = d.month + 1
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day)


def _client(user: User) -> dict:
    parts = (user.full_name or "").split(" ", 1)
    return {
        "first_name": parts[0] if parts else "",
        "last_name": parts[1] if len(parts) > 1 else "",
        "email": user.email or "",
        "phone": user.phone or "",
    }


@payments_router.post("/subscribe")
async def subscribe(payload: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    plan_code = (payload or {}).get("plan")
    if plan_code not in wfp.PLANS:
        raise HTTPException(400, "Невідомий тариф")

    order_ref = wfp.new_order_reference(user.id, plan_code)
    db.add(PaymentTransaction(
        order_reference=order_ref, user_id=user.id, plan_code=plan_code,
        kind="initial", amount=wfp.PLANS[plan_code]["price"], currency=wfp.CURRENCY,
        transaction_status="Created",
    ))
    db.add(ActivityLog(user_id=user.id, action="subscribe_init",
                       details={"plan": plan_code, "order": order_ref}))
    await db.flush()

    fields = wfp.build_purchase_fields(order_ref, plan_code, _client(user))
    return {"pay_url": wfp.WFP_PAY_URL, "fields": fields}


@payments_router.post("/wayforpay/callback")
async def wayforpay_callback(request: Request, db: AsyncSession = Depends(get_db)):
    data = wfp.parse_callback(await request.body())
    if not data or not wfp.verify_callback(data):
        raise HTTPException(400, "bad signature")

    order_ref = data.get("orderReference")
    status = data.get("transactionStatus")
    rec_token = data.get("recToken") or None
    card_pan = data.get("cardPan") or None

    tx = (await db.execute(
        select(PaymentTransaction).where(PaymentTransaction.order_reference == order_ref)
    )).scalar_one_or_none()

    if tx is not None:
        tx.transaction_status = status
        tx.reason_code = str(data.get("reasonCode", ""))
        tx.raw = json.dumps(data, ensure_ascii=False)

        if status == "Approved":
            user = await db.get(User, tx.user_id)
            if user is not None:
                now = datetime.utcnow()
                user.plan = SubscriptionPlan(tx.plan_code)
                user.subscription_active_until = _add_month(now)
                user.reports_this_month = 0

                profile = (await db.execute(
                    select(BillingProfile).where(BillingProfile.user_id == user.id)
                )).scalar_one_or_none()
                if profile is None:
                    profile = BillingProfile(user_id=user.id)
                    db.add(profile)
                profile.plan_code = tx.plan_code
                profile.autopay = True
                profile.failed_charges = 0
                if rec_token:
                    profile.rec_token = rec_token
                if card_pan:
                    profile.card_pan = card_pan

                db.add(ActivityLog(user_id=user.id, action="subscription_paid",
                                   details={"plan": tx.plan_code, "order": order_ref}))

    return JSONResponse(wfp.callback_response(order_ref))


@payments_router.post("/cancel")
async def cancel(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Вимкнути автоподовження. Доступ лишається до кінця оплаченого періоду."""
    profile = (await db.execute(
        select(BillingProfile).where(BillingProfile.user_id == user.id)
    )).scalar_one_or_none()
    if profile is None:
        raise HTTPException(404, "Підписки немає")
    profile.autopay = False
    db.add(ActivityLog(user_id=user.id, action="subscription_cancel_autopay", details={}))
    return {"ok": True, "autopay": False, "active_until": user.subscription_active_until}


@payments_router.get("/status")
async def status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    profile = (await db.execute(
        select(BillingProfile).where(BillingProfile.user_id == user.id)
    )).scalar_one_or_none()
    plan_value = user.plan.value if hasattr(user.plan, "value") else str(user.plan or "free")
    active = bool(user.is_admin) or bool(
        user.subscription_active_until and user.subscription_active_until >= datetime.utcnow()
    )
    return {
        "plan": plan_value,
        "active": active,
        "autopay": bool(profile.autopay) if profile else False,
        "card_pan": profile.card_pan if profile else None,
        "subscription_active_until": user.subscription_active_until.isoformat() if user.subscription_active_until else None,
        "reports_this_month": user.reports_this_month or 0,
        "free_reports_left": max(0, user.free_reports_left or 0),
    }
