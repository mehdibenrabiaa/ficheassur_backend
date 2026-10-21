"""Buying leads and working them: orders (paid by card, per purchase),
purchases (which also carry the broker's CRM state for that lead), notes and
the history timeline."""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OrderStatus(str, enum.Enum):
    pending = "pending"  # on the payment page; its leads are held until expires_at
    paid = "paid"        # paid and delivered: one Purchase per item
    expired = "expired"  # never paid; the hold is released


class Order(Base):
    """A cart checked out: one card payment for one or more leads."""

    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    broker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brokers.id"), index=True)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus, name="order_status"), default=OrderStatus.pending, index=True)
    total_cents: Mapped[int] = mapped_column(Integer)
    # What was delivered and charged (an item no longer available at payment
    # time is refunded and left out).
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)
    stripe_session_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id"), index=True)
    exclusive: Mapped[bool] = mapped_column(Boolean)
    price_cents: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(160))  # "Lead Auto — Paris (75), partagé"


class BillingAccount(Base):
    """The broker's Stripe customer (saved cards live at Stripe, not here)."""

    __tablename__ = "billing_accounts"

    broker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brokers.id"), primary_key=True)
    stripe_customer_id: Mapped[str] = mapped_column(String(255), unique=True)


class CrmStatus(str, enum.Enum):
    new = "new"
    contacted = "contacted"
    quote_sent = "quote_sent"
    won = "won"
    lost = "lost"


class Purchase(Base):
    """A lead bought by a broker -- and, from then on, that lead's card in
    the broker's CRM (status, next reminder)."""

    __tablename__ = "purchases"
    __table_args__ = (UniqueConstraint("lead_id", "broker_id", name="uq_purchase_lead_broker"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id"), index=True)
    broker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brokers.id"), index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"), index=True)
    exclusive: Mapped[bool] = mapped_column(Boolean)
    price_cents: Mapped[int] = mapped_column(Integer)
    crm_status: Mapped[CrmStatus] = mapped_column(Enum(CrmStatus, name="crm_status"), default=CrmStatus.new, index=True)
    reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class PurchaseNote(Base):
    __tablename__ = "purchase_notes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    purchase_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchases.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PurchaseEvent(Base):
    """The lead's history in the CRM: bought, status changes, reminders set,
    notes added -- shown as a timeline."""

    __tablename__ = "purchase_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    purchase_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # purchased | status | reminder | note
    detail: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
