"""
Щоденний крон: рекурентне списання підписок, у яких закінчується період.

Запуск: `python -m app.services.billing_cron`
Або в crontab раз на добу (приклад у коментарі внизу).

На кожну підписку з autopay=True, у якої до кінця періоду лишилось <= 1 день:
  CHARGE по recToken
    Approved -> продовжуємо місяць (subscription_active_until += 1 міс), reports_this_month=0
    Declined -> failed_charges++, після 3 невдач autopay=False (доступ згасне сам по даті)
"""
from __future__ import annotations

import asyncio
import calendar
import json
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.database import async_session
from app.models.billing import BillingProfile, PaymentTransaction
from app.models.models import SubscriptionPlan, User
from app.services import wayforpay as wfp

MAX_FAILED = 3
RENEW_WINDOW_DAYS = 1  # починаємо списувати за добу до кінця періоду


def _add_month(d: datetime) -> datetime:
    m = d.month + 1
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day)


async def charge_due_subscriptions() -> dict:
    now = datetime.utcnow()
    deadline = now + timedelta(days=RENEW_WINDOW_DAYS)
    ok = failed = 0
    due = []

    async with async_session() as db:
        profiles = (await db.execute(
            select(BillingProfile).where(
                BillingProfile.autopay.is_(True),
                BillingProfile.rec_token.is_not(None),
            )
        )).scalars().all()

        for p in profiles:
            user = await db.get(User, p.user_id)
            if user is None or user.is_admin:
                continue
            plan_value = user.plan.value if hasattr(user.plan, "value") else str(user.plan or "free")
            if plan_value == SubscriptionPlan.FREE.value:
                continue
            if user.subscription_active_until and user.subscription_active_until <= deadline:
                due.append((user, p))

        for user, p in due:
            plan_code = p.plan_code or (user.plan.value if hasattr(user.plan, "value") else None)
            if plan_code not in wfp.PLANS:
                continue
            order_ref = wfp.new_order_reference(user.id, plan_code)
            res = await wfp.charge_by_token(order_ref, plan_code, p.rec_token, {
                "first_name": (user.full_name or "").split(" ", 1)[0],
                "email": user.email or "",
                "phone": user.phone or "",
            })
            status = res.get("transactionStatus")

            db.add(PaymentTransaction(
                order_reference=order_ref, user_id=user.id, plan_code=plan_code,
                kind="recurring", amount=wfp.PLANS[plan_code]["price"], currency=wfp.CURRENCY,
                transaction_status=status, reason_code=str(res.get("reasonCode", "")),
                raw=json.dumps(res, ensure_ascii=False),
            ))

            if status == "Approved":
                user.subscription_active_until = _add_month(now)
                user.reports_this_month = 0
                p.failed_charges = 0
                ok += 1
            else:
                p.failed_charges = (p.failed_charges or 0) + 1
                if p.failed_charges >= MAX_FAILED:
                    p.autopay = False
                failed += 1

        await db.commit()

    return {"charged": ok, "failed": failed, "due": len(due)}


async def main():
    result = await charge_due_subscriptions()
    print("billing_cron:", result)


if __name__ == "__main__":
    asyncio.run(main())

# crontab (раз на добу о 03:10):
# 10 3 * * *  cd /path/winner && /path/venv/bin/python -m app.services.billing_cron >> /var/log/ocinka_billing.log 2>&1
