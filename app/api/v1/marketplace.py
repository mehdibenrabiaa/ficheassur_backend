"""Buying leads: the marketplace a signed-in broker browses (contact details
hidden until bought), and checkout -- one card payment per cart, on Stripe
Checkout. While the broker is on the payment page their leads are held for
them; the leads are delivered once Stripe confirms the payment (webhook in
billing.py). With PAYMENTS_SIMULATED (local dev only) checkout is paid at once."""

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.v1.billing import customer_id, payments_simulated, use_stripe
from app.config import settings
from app.database import get_db
from app.models.broker import Broker
from app.models.commerce import Order, OrderItem, OrderStatus, Purchase
from app.models.lead import Lead, LeadStatus
from app.pricing import HOLD_MINUTES, MAX_SHARED_BUYERS, PRICES_CENTS, SHELF_DAYS, price_cents
from app.security import current_broker
from app.services.orders import can_take, fulfil, taken_by_lead

router = APIRouter(prefix="/api/v1/marketplace", tags=["marketplace"])

# Lead `details` keys never shown before purchase (they could identify the prospect).
# The marketplace shows leads before purchase, so anything that identifies or
# reaches the prospect must stay out. `details` arrives free-form from the
# quote forms (French labels), so this is defensive on two levels: a field
# whose name looks private is dropped, and so is any value that looks like
# contact data whatever its label (a phone number typed in "Commentaire").
# Matched anywhere in the field name...
_PRIVATE_KEY_PARTS = (
    "mail", "phone", "telephone", "téléphone", "portable", "mobile", "adresse", "address", "postal", "prenom",
    "prénom", "immat", "plaque", "plate", "naissance", "birth", "siret", "siren", "iban", "numero", "numéro",
    "contact", "commentaire", "message", "comment", "précision", "precision",
)
# ...and these only as whole words ("nom" but not "Nombre de véhicules").
_PRIVATE_KEY_WORDS = {"nom", "name", "tel", "tél", "rue", "rib", "note", "notes"}
_CONTACT_VALUE = re.compile(
    r"@|https?://|www\.|(?:\+|00)\d|\d[\d\s.\-]{7,}\d|\b\d{5}\b|\b[A-Z]{2}-?\d{3}-?[A-Z]{2}\b", re.IGNORECASE
)

MARKET_NAMES = {"AUTO": "Auto", "MOTO": "Moto", "AUTO_PRO": "Pro de l'automobile", "COMMERCIAL_AUTO": "Véhicules pro"}


class MarketLead(BaseModel):
    id: str
    insurance_type: str
    city: str
    department: str
    first_name_initial: str
    details: dict[str, Any]
    created_at: datetime
    shared_price_cents: int
    exclusive_price_cents: int
    exclusive_available: bool
    buyers: int
    max_buyers: int


class CheckoutItem(BaseModel):
    lead_id: uuid.UUID
    exclusive: bool = False


class CheckoutIn(BaseModel):
    items: list[CheckoutItem] = Field(min_length=1, max_length=50)


class CheckoutOut(BaseModel):
    status: str  # "paid" (simulated payments) | "embedded" (show Stripe's form)
    order_id: str
    # For Stripe's Embedded Checkout, mounted in the site with this secret.
    client_secret: str | None = None


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _public_details(details: dict[str, Any] | None) -> dict[str, Any]:
    out = {}
    for k, v in (details or {}).items():
        key = str(k).lower()
        words = set(re.findall(r"[^\W\d_]+", key))
        if isinstance(v, (dict, list)) or any(part in key for part in _PRIVATE_KEY_PARTS) or words & _PRIVATE_KEY_WORDS:
            continue
        if isinstance(v, str) and (len(v) > 60 or _CONTACT_VALUE.search(v)):
            continue
        out[k] = v
        if len(out) == 8:
            break
    return out


@router.get("/prices")
async def prices(_: Broker = Depends(current_broker)):
    return {
        "prices": {t: {"shared_cents": s, "exclusive_cents": e} for t, (s, e) in PRICES_CENTS.items()},
        "max_shared_buyers": MAX_SHARED_BUYERS,
    }


@router.get("/leads", response_model=list[MarketLead])
async def list_leads(
    insurance_type: str | None = Query(default=None),
    department: str | None = Query(default=None, max_length=3),
    broker: Broker = Depends(current_broker),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.now(timezone.utc) - timedelta(days=SHELF_DAYS)
    q = select(Lead).where(Lead.created_at >= since, Lead.status != LeadStatus.REJECTED, Lead.insurance_type.in_(list(PRICES_CENTS)))
    if insurance_type:
        q = q.where(Lead.insurance_type == insurance_type)
    if department:
        q = q.where(Lead.zip_code.startswith(department.strip()))
    leads = (await db.execute(q.order_by(Lead.created_at.desc()).limit(300))).scalars().all()
    if not leads:
        return []

    taken = await taken_by_lead(db, [l.id for l in leads])
    bought = await taken_by_lead(db, [l.id for l in leads], include_holds=False)
    out = []
    for lead in leads:
        t = taken.get(lead.id, [])
        if any(x.broker_id == broker.id for x in bought.get(lead.id, [])) or not can_take(t, broker.id, False):
            continue
        shared, excl = PRICES_CENTS[lead.insurance_type]
        others = [x for x in t if x.broker_id != broker.id]
        out.append(MarketLead(
            id=str(lead.id), insurance_type=lead.insurance_type, city=lead.city, department=lead.zip_code[:2],
            first_name_initial=(lead.first_name[:1].upper() + ".") if lead.first_name else "",
            details=_public_details(lead.details), created_at=_aware(lead.created_at),
            shared_price_cents=shared, exclusive_price_cents=excl, exclusive_available=can_take(t, broker.id, True),
            buyers=len(others), max_buyers=MAX_SHARED_BUYERS,
        ))
        if len(out) == 100:
            break
    return out


@router.post("/checkout", response_model=CheckoutOut)
async def checkout(payload: CheckoutIn, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    """Create the order (holding its leads) and send the broker to pay it."""
    ids = [i.lead_id for i in payload.items]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="Un même lead apparaît deux fois dans le panier.")
    simulated = payments_simulated()
    if not simulated:
        use_stripe()

    # One checkout at a time per broker: a new cart replaces an unpaid one.
    await db.execute(select(Broker.id).where(Broker.id == broker.id).with_for_update())
    await db.execute(
        update(Order).where(Order.broker_id == broker.id, Order.status == OrderStatus.pending).values(status=OrderStatus.expired)
    )
    leads = {l.id: l for l in (await db.execute(select(Lead).where(Lead.id.in_(ids)).with_for_update())).scalars().all()}
    taken = await taken_by_lead(db, ids)
    bought = await taken_by_lead(db, ids, include_holds=False)

    since = datetime.now(timezone.utc) - timedelta(days=SHELF_DAYS)
    lines = []
    for item in payload.items:
        lead = leads.get(item.lead_id)
        if not lead or lead.status == LeadStatus.REJECTED or _aware(lead.created_at) < since or lead.insurance_type not in PRICES_CENTS:
            raise HTTPException(status_code=409, detail="Un des leads n'est plus disponible. Actualisez la liste.")
        if any(t.broker_id == broker.id for t in bought.get(lead.id, [])):
            raise HTTPException(status_code=409, detail="Vous avez déjà acheté un des leads du panier.")
        if not can_take(taken.get(lead.id, []), broker.id, item.exclusive):
            raise HTTPException(status_code=409, detail="Un des leads vient d'être vendu. Actualisez la liste.")
        mode = "exclusif" if item.exclusive else "partagé"
        label = f"Lead {MARKET_NAMES.get(lead.insurance_type, lead.insurance_type)} — {lead.city} ({lead.zip_code[:2]}), {mode}"
        lines.append((item, price_cents(lead.insurance_type, item.exclusive), label))

    now = datetime.now(timezone.utc)
    order = Order(
        broker_id=broker.id, status=OrderStatus.pending, total_cents=sum(p for _, p, _ in lines),
        # A minute past Stripe's session expiry, so the hold never ends while the payment page is still open.
        expires_at=now + timedelta(minutes=HOLD_MINUTES + 1),
    )
    db.add(order)
    await db.flush()
    for item, price, label in lines:
        db.add(OrderItem(order_id=order.id, lead_id=item.lead_id, exclusive=item.exclusive, price_cents=price, label=label))
    await db.commit()

    if simulated:
        await fulfil(db, order)
        return CheckoutOut(status="paid", order_id=str(order.id))

    try:
        session = await run_in_threadpool(
            stripe.checkout.Session.create,
            mode="payment",
            customer=await customer_id(db, broker),
            line_items=[
                {"price_data": {"currency": "eur", "unit_amount": price, "product_data": {"name": label}}, "quantity": 1}
                for _, price, label in lines
            ],
            # Lets the broker save their card, and shows it again next time.
            saved_payment_method_options={"payment_method_save": "enabled"},
            metadata={"order_id": str(order.id), "broker_id": str(broker.id)},
            expires_at=int((now + timedelta(minutes=HOLD_MINUTES)).timestamp()),
            # Embedded Checkout: Stripe's form shown inside the espace pro;
            # the page confirms the order itself when it completes.
            ui_mode="embedded_page",
            redirect_on_completion="never",
            locale="fr",
        )
    except stripe.StripeError:
        order.status = OrderStatus.expired  # release the hold
        await db.commit()
        raise HTTPException(status_code=502, detail="Le paiement n'a pas pu être lancé. Réessayez dans un instant.")
    order.stripe_session_id = session.id
    await db.commit()
    return CheckoutOut(status="embedded", order_id=str(order.id), client_secret=session.client_secret)


class OrderLineView(BaseModel):
    id: str  # the order line (two leads can share a label)
    label: str
    price_cents: int
    purchase_id: str | None = None  # once paid: the lead's page in "Mes leads"


class OrderView(BaseModel):
    id: str
    status: str  # pending | paid | expired
    total_cents: int
    refunded_cents: int
    expires_at: datetime
    items: list[OrderLineView]
    # Pending only: mounts Stripe's Embedded Checkout on the payment page.
    client_secret: str | None = None


@router.get("/orders/{order_id}", response_model=OrderView)
async def get_order(order_id: uuid.UUID, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    """The payment page: the order's summary and, while it can still be
    paid, the secret to show Stripe's form (read back from Stripe, so a page
    reload keeps working)."""
    order = await db.get(Order, order_id)
    if not order or order.broker_id != broker.id:
        raise HTTPException(status_code=404, detail="Commande introuvable.")
    expires = order.expires_at if order.expires_at.tzinfo else order.expires_at.replace(tzinfo=timezone.utc)
    if order.status == OrderStatus.pending and expires < datetime.now(timezone.utc):
        order.status = OrderStatus.expired
        await db.commit()
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
    bought = {}
    if order.status == OrderStatus.paid:
        bought = {p.lead_id: str(p.id) for p in (await db.execute(select(Purchase).where(Purchase.order_id == order.id))).scalars()}
    secret = None
    if order.status == OrderStatus.pending and order.stripe_session_id and settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
        try:
            secret = (await run_in_threadpool(stripe.checkout.Session.retrieve, order.stripe_session_id)).client_secret
        except stripe.StripeError:
            raise HTTPException(status_code=502, detail="Le paiement n'a pas pu être chargé. Réessayez dans un instant.")
    return OrderView(
        id=str(order.id), status=order.status.value, total_cents=order.total_cents, refunded_cents=order.refunded_cents or 0,
        expires_at=expires, client_secret=secret,
        items=[OrderLineView(id=str(i.id), label=i.label, price_cents=i.price_cents, purchase_id=bought.get(i.lead_id)) for i in items],
    )


@router.post("/orders/{order_id}/confirm", status_code=204)
async def confirm(order_id: uuid.UUID, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    """Embedded Checkout completed: ask Stripe whether the session is paid
    and deliver the leads now, rather than waiting for the webhook (which
    can lag, and can't reach a local dev machine at all). Safe to repeat:
    fulfilment is idempotent, and only Stripe's answer counts."""
    order = await db.get(Order, order_id)
    if not order or order.broker_id != broker.id:
        raise HTTPException(status_code=404, detail="Commande introuvable.")
    if order.status == OrderStatus.paid or not order.stripe_session_id or not settings.stripe_secret_key:
        return
    stripe.api_key = settings.stripe_secret_key
    try:
        session = (await run_in_threadpool(stripe.checkout.Session.retrieve, order.stripe_session_id)).to_dict()
    except stripe.StripeError:
        return  # the webhook settles it
    if session.get("payment_status") == "paid":
        order.stripe_payment_intent_id = session.get("payment_intent")
        await fulfil(db, order)


@router.post("/orders/{order_id}/cancel", status_code=204)
async def cancel(order_id: uuid.UUID, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    """Payment form closed without paying: release the held leads right away."""
    order = await db.get(Order, order_id)
    if not order or order.broker_id != broker.id:
        raise HTTPException(status_code=404, detail="Commande introuvable.")
    if order.status == OrderStatus.pending:
        order.status = OrderStatus.expired
        await db.commit()
        if order.stripe_session_id and settings.stripe_secret_key:
            stripe.api_key = settings.stripe_secret_key
            try:
                await run_in_threadpool(stripe.checkout.Session.expire, order.stripe_session_id)
            except stripe.StripeError:
                pass  # already expired or completed: the webhook settles it
