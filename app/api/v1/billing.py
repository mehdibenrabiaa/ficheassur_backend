"""Card payments: the broker's saved card and orders, Stripe's customer
portal to manage cards, and the Stripe webhook that delivers paid orders.
Brokers pay per purchase -- each cart is one Stripe Checkout payment (see
marketplace.py). PAYMENTS_SIMULATED (local dev only, and only while no Stripe
key is set) skips the card payment."""

import uuid
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import get_db
from app.models.broker import Broker
from app.models.commerce import BillingAccount, Order, OrderItem, OrderStatus
from app.security import current_broker
from app.services.orders import fulfil

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


class CardOut(BaseModel):
    id: str  # Stripe payment method id
    brand: str
    last4: str
    exp_month: int
    exp_year: int
    default: bool = False


class OrderLine(BaseModel):
    label: str
    price_cents: int


class OrderOut(BaseModel):
    id: str
    total_cents: int
    refunded_cents: int
    paid_at: datetime
    items: list[OrderLine]


class BillingOut(BaseModel):
    simulated: bool
    card: CardOut | None  # the default one (or the first)
    cards: list[CardOut] = []
    orders: list[OrderOut]


def payments_simulated() -> bool:
    return settings.payments_simulated and not settings.stripe_secret_key


def use_stripe() -> None:
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Le paiement par carte n'est pas encore configuré.")
    stripe.api_key = settings.stripe_secret_key


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def customer_id(db: AsyncSession, broker: Broker) -> str:
    """The broker's Stripe customer, created on their first payment."""
    acct = await db.get(BillingAccount, broker.id)
    if acct:
        return acct.stripe_customer_id
    customer = await run_in_threadpool(
        stripe.Customer.create, email=broker.email, name=broker.company, metadata={"broker_id": str(broker.id)}
    )
    db.add(BillingAccount(broker_id=broker.id, stripe_customer_id=customer.id))
    await db.commit()
    return customer.id


@router.get("", response_model=BillingOut)
async def billing(broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    orders = (await db.execute(
        select(Order).where(Order.broker_id == broker.id, Order.status == OrderStatus.paid).order_by(Order.paid_at.desc()).limit(100)
    )).scalars().all()
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id.in_([o.id for o in orders])))).scalars().all() if orders else []

    cards: list[CardOut] = []
    acct = await db.get(BillingAccount, broker.id)
    if acct and settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
        try:
            cards = await _cards(acct.stripe_customer_id)
        except stripe.StripeError:
            pass  # the page still lists the orders if Stripe is unreachable

    return BillingOut(
        simulated=payments_simulated(),
        card=next((c for c in cards if c.default), cards[0] if cards else None),
        cards=cards,
        orders=[
            OrderOut(
                id=str(o.id), total_cents=o.total_cents, refunded_cents=o.refunded_cents, paid_at=_aware(o.paid_at),
                items=[OrderLine(label=i.label, price_cents=i.price_cents) for i in items if i.order_id == o.id],
            )
            for o in orders
        ],
    )


async def _cards(customer: str) -> list[CardOut]:
    pms = await run_in_threadpool(stripe.PaymentMethod.list, customer=customer, type="card", limit=20)
    cust = await run_in_threadpool(stripe.Customer.retrieve, customer)
    default = (cust.to_dict().get("invoice_settings") or {}).get("default_payment_method")
    out = [
        CardOut(id=pm.id, brand=pm.card.brand, last4=pm.card.last4, exp_month=pm.card.exp_month, exp_year=pm.card.exp_year, default=pm.id == default)
        for pm in pms.data
    ]
    if out and not any(c.default for c in out):
        out[0].default = True  # no explicit default: Stripe uses the most recent
    return out


# Saved cards, managed inside the espace pro: the card form is Stripe's
# Payment Element on our page (card details go straight to Stripe), backed by
# a SetupIntent. Each card is checked to belong to this broker's customer.

@router.post("/cards/setup")
async def card_setup(broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    """A SetupIntent to save a new card from the page's form."""
    use_stripe()
    intent = await run_in_threadpool(
        stripe.SetupIntent.create, customer=await customer_id(db, broker), usage="off_session",
        allowed_payment_method_types=["card"],  # this form saves cards only (not PayPal, wallets...)
    )
    return {"client_secret": intent.client_secret}


async def _own_card(db: AsyncSession, broker: Broker, payment_method_id: str):
    use_stripe()
    acct = await db.get(BillingAccount, broker.id)
    try:
        pm = await run_in_threadpool(stripe.PaymentMethod.retrieve, payment_method_id)
    except stripe.StripeError:
        pm = None
    if not acct or not pm or pm.customer != acct.stripe_customer_id:
        raise HTTPException(status_code=404, detail="Carte introuvable.")
    return acct, pm


@router.post("/cards/{payment_method_id}/default", status_code=204)
async def card_default(payment_method_id: str, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    acct, pm = await _own_card(db, broker, payment_method_id)
    await run_in_threadpool(stripe.Customer.modify, acct.stripe_customer_id, invoice_settings={"default_payment_method": pm.id})


@router.delete("/cards/{payment_method_id}", status_code=204)
async def card_delete(payment_method_id: str, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    _, pm = await _own_card(db, broker, payment_method_id)
    await run_in_threadpool(stripe.PaymentMethod.detach, pm.id)


@router.post("/portal")
async def portal(broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    """Stripe's hosted page to add, change or remove saved cards and get receipts."""
    use_stripe()
    session = await run_in_threadpool(
        stripe.billing_portal.Session.create,
        customer=await customer_id(db, broker),
        return_url=f"{settings.site_url.rstrip('/')}/espace-pro/paiements",
    )
    return {"url": session.url}


@router.post("/webhook", include_in_schema=False)
async def webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Stripe -> us. A paid Checkout session delivers its order's leads; an
    expired one releases them."""
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook non configuré.")
    try:
        event = stripe.Webhook.construct_event(
            await request.body(), request.headers.get("stripe-signature", ""), settings.stripe_webhook_secret
        )
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Signature invalide.")

    session = event["data"]["object"].to_dict()  # stripe>=13: objects are not dicts
    order_id = (session.get("metadata") or {}).get("order_id")
    order = await db.get(Order, uuid.UUID(order_id)) if order_id else None
    if not order:
        return {"received": True}

    paid = event["type"] == "checkout.session.async_payment_succeeded" or (
        event["type"] == "checkout.session.completed" and session.get("payment_status") == "paid"
    )
    if paid:
        order.stripe_payment_intent_id = session.get("payment_intent")
        await fulfil(db, order)
    elif event["type"] in ("checkout.session.expired", "checkout.session.async_payment_failed") and order.status == OrderStatus.pending:
        order.status = OrderStatus.expired
        await db.commit()
    return {"received": True}
