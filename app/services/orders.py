"""Lead availability (sold + held for someone's payment in progress) and
order fulfilment (paid order -> purchases in the broker's CRM)."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.models.commerce import Order, OrderItem, OrderStatus, Purchase, PurchaseEvent
from app.models.lead import Lead
from app.pricing import MAX_SHARED_BUYERS


@dataclass
class Taken:
    broker_id: uuid.UUID
    exclusive: bool


async def taken_by_lead(db: AsyncSession, lead_ids: list[uuid.UUID], *, include_holds: bool = True) -> dict[uuid.UUID, list[Taken]]:
    """Per lead: who bought it, plus (by default) who holds it in an order
    still on the payment page."""
    out: dict[uuid.UUID, list[Taken]] = {}
    for r in (await db.execute(select(Purchase.lead_id, Purchase.broker_id, Purchase.exclusive).where(Purchase.lead_id.in_(lead_ids)))).all():
        out.setdefault(r.lead_id, []).append(Taken(r.broker_id, r.exclusive))
    if include_holds:
        holds = await db.execute(
            select(OrderItem.lead_id, Order.broker_id, OrderItem.exclusive)
            .join(Order, Order.id == OrderItem.order_id)
            .where(OrderItem.lead_id.in_(lead_ids), Order.status == OrderStatus.pending, Order.expires_at > datetime.now(timezone.utc))
        )
        for r in holds.all():
            out.setdefault(r.lead_id, []).append(Taken(r.broker_id, r.exclusive))
    return out


def can_take(taken: list[Taken], broker_id: uuid.UUID, exclusive: bool) -> bool:
    """Whether this broker can buy the lead (shared or exclusive) given who
    already has or holds it. The broker's own holds don't count against them."""
    others = [t for t in taken if t.broker_id != broker_id]
    if any(t.exclusive for t in others):
        return False
    if exclusive:
        return not others
    return len(others) < MAX_SHARED_BUYERS


async def fulfil(db: AsyncSession, order: Order) -> None:
    """Turn a paid order into purchases. Idempotent (Stripe retries webhooks).
    An item that can no longer be delivered -- the hold expired and someone
    else bought the lead meanwhile -- is refunded instead."""
    if order.status == OrderStatus.paid:
        return
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
    lead_ids = [i.lead_id for i in items]
    await db.execute(select(Lead.id).where(Lead.id.in_(lead_ids)).with_for_update())  # PostgreSQL row locks
    sold = await taken_by_lead(db, lead_ids, include_holds=False)

    refund = 0
    for item in items:
        bought_already = any(t.broker_id == order.broker_id for t in sold.get(item.lead_id, []))
        if bought_already or not can_take(sold.get(item.lead_id, []), order.broker_id, item.exclusive):
            refund += item.price_cents
            continue
        p = Purchase(lead_id=item.lead_id, broker_id=order.broker_id, order_id=order.id, exclusive=item.exclusive, price_cents=item.price_cents)
        db.add(p)
        await db.flush()
        db.add(PurchaseEvent(purchase_id=p.id, kind="purchased", detail=f"Lead acheté ({'exclusif' if item.exclusive else 'partagé'})"))

    order.status = OrderStatus.paid
    order.paid_at = datetime.now(timezone.utc)
    order.refunded_cents = refund
    await db.commit()

    if refund and order.stripe_payment_intent_id and settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
        await run_in_threadpool(stripe.Refund.create, payment_intent=order.stripe_payment_intent_id, amount=refund)
