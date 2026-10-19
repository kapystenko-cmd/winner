"""
Моделі платежів WayForPay.

Стан підписки (план, активність, лічильник місяця) живе на User
(plan / subscription_active_until / reports_this_month) — як і раніше.
Тут лише операційні дані для списання і історія транзакцій.

Таблиці створюються автоматично в init_db() (Base.metadata.create_all) при старті.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class BillingProfile(Base):
    """Платіжний профіль користувача: токен картки + стан автосписання."""
    __tablename__ = "billing_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True, index=True, nullable=False)

    plan_code = Column(String(20))              # basic / standard / pro
    rec_token = Column(Text)                    # токен картки для CHARGE
    card_pan = Column(String(32))               # маскована картка (44****4444)
    autopay = Column(Boolean, default=True)     # автоподовження увімкнене
    failed_charges = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PaymentTransaction(Base):
    """Історія платежів (перший платіж і рекурентні списання)."""
    __tablename__ = "payment_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_reference = Column(String(120), unique=True, index=True, nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)

    plan_code = Column(String(20))
    kind = Column(String(20))                   # initial | recurring
    amount = Column(Integer)
    currency = Column(String(8), default="UAH")

    transaction_status = Column(String(32))     # Approved / Declined / ...
    reason_code = Column(String(32))
    raw = Column(Text)                          # сирий колбек/відповідь

    created_at = Column(DateTime, default=datetime.utcnow)
