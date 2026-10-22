"""Read-only, administrator-only reporting across FicheAssur's clients and sales."""
import csv
import io
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.broker import Broker, BrokerRole, BrokerStatus
from app.models.commerce import Order, OrderItem, OrderStatus, Purchase
from app.models.lead import Lead, LeadStatus
from app.models.article import Article
from app.pricing import MAX_SHARED_BUYERS, PRICES_CENTS, SHELF_DAYS
from app.security import current_admin

router = APIRouter(prefix="/api/v1/admin/management", tags=["management"], dependencies=[Depends(current_admin)])


def dates(query, column, since, until):
    if since and until and since > until:
        raise HTTPException(422, "La date de début doit précéder la date de fin.")
    if since:
        query = query.where(column >= datetime.combine(since, time.min, timezone.utc))
    if until:
        query = query.where(column < datetime.combine(until + timedelta(days=1), time.min, timezone.utc))
    return query


def inventory():
    sold = select(Purchase.lead_id, func.count().label("sales"), func.max(case((Purchase.exclusive.is_(True), 1), else_=0)).label("exclusive")).group_by(Purchase.lead_id).subquery()
    held = select(OrderItem.lead_id, func.count(func.distinct(Order.broker_id)).label("holds"), func.max(case((OrderItem.exclusive.is_(True), 1), else_=0)).label("exclusive")).join(Order, Order.id == OrderItem.order_id).where(Order.status == OrderStatus.pending, Order.expires_at > datetime.now(timezone.utc)).group_by(OrderItem.lead_id).subquery()
    sales, holds = func.coalesce(sold.c.sales, 0), func.coalesce(held.c.holds, 0)
    state = case(
        (Lead.status == LeadStatus.REJECTED, "withdrawn"),
        (~Lead.insurance_type.in_(list(PRICES_CENTS)), "unavailable"),
        (Lead.created_at < datetime.now(timezone.utc) - timedelta(days=SHELF_DAYS), "expired"),
        (or_(sold.c.exclusive == 1, sales >= MAX_SHARED_BUYERS), "sold_out"),
        (or_(held.c.exclusive == 1, sales + holds >= MAX_SHARED_BUYERS), "reserved"),
        else_="available",
    )
    return select(Lead, sales.label("sales"), holds.label("holds"), state.label("availability")).outerjoin(sold, sold.c.lead_id == Lead.id).outerjoin(held, held.c.lead_id == Lead.id), state


async def page_rows(db, query, page, limit):
    total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    return (await db.execute(query.offset((page - 1) * limit).limit(limit))).all(), {"total": total, "page": page, "limit": limit}


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db)):
    clients = select(Broker).where(Broker.role == BrokerRole.broker).subquery()
    sales = select(Purchase).join(Broker, Broker.id == Purchase.broker_id).where(Broker.role == BrokerRole.broker).subquery()
    inv, _ = inventory()
    inv = inv.subquery()
    counts = dict((await db.execute(select(inv.c.availability, func.count()).group_by(inv.c.availability))).all())
    totals = (await db.execute(select(func.count(), func.count(func.distinct(sales.c.lead_id)), func.count(func.distinct(sales.c.broker_id)), func.coalesce(func.sum(sales.c.price_cents), 0)).select_from(sales))).one()
    markets = (await db.execute(select(Lead.insurance_type, func.count(Purchase.id), func.sum(Purchase.price_cents)).join(Purchase, Purchase.lead_id == Lead.id).join(Broker, Broker.id == Purchase.broker_id).where(Broker.role == BrokerRole.broker).group_by(Lead.insurance_type))).all()
    return {"clients": await db.scalar(select(func.count()).select_from(clients)), "active_clients": await db.scalar(select(func.count()).select_from(clients).where(clients.c.status == BrokerStatus.active)), "purchasing_clients": totals[2], "sales": totals[0], "unique_leads_sold": totals[1], "revenue_cents": totals[3], "leads": sum(counts.values()), "inventory": counts, "articles": await db.scalar(select(func.count()).select_from(Article)), "published_articles": await db.scalar(select(func.count()).select_from(Article).where(Article.status == "published")), "by_market": [{"market": m, "sales": n, "revenue_cents": amount} for m, n, amount in markets]}


@router.get("/leads")
async def leads(q: str = Query("", max_length=200), market: str | None = None, availability: Literal["available", "reserved", "sold_out", "withdrawn", "expired", "unavailable"] | None = None, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    query, state = inventory()
    if q:
        query = query.where(or_(*(col.ilike(f"%{q}%") for col in (Lead.first_name, Lead.last_name, Lead.email, Lead.city))))
    if market:
        query = query.where(Lead.insurance_type == market)
    if availability:
        query = query.where(state == availability)
    rows, meta = await page_rows(db, query.order_by(Lead.created_at.desc(), Lead.id), page, limit)
    return {**meta, "items": [{"id": str(l.id), "name": f"{l.first_name} {l.last_name}".strip(), "email": l.email, "city": l.city, "zip_code": l.zip_code, "market": l.insurance_type, "created_at": l.created_at, "sales": n, "holds": h, "availability": a} for l, n, h, a in rows]}


def client_totals():
    return select(Purchase.broker_id, func.count().label("purchases"), func.sum(Purchase.price_cents).label("spent_cents"), func.max(Purchase.created_at).label("last_purchase_at")).group_by(Purchase.broker_id).subquery()


def client_out(b, n, total, last):
    return {"id": str(b.id), "name": b.full_name, "email": b.email, "company": b.company, "phone": b.phone, "orias_number": b.orias_number, "status": b.status.value, "created_at": b.created_at, "last_login_at": b.last_login_at, "purchases": n or 0, "spent_cents": total or 0, "last_purchase_at": last}


@router.get("/clients")
async def clients(q: str = Query("", max_length=200), status: Literal["active", "invited", "disabled"] | None = None, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    totals = client_totals()
    query = select(Broker, totals.c.purchases, totals.c.spent_cents, totals.c.last_purchase_at).outerjoin(totals, totals.c.broker_id == Broker.id).where(Broker.role == BrokerRole.broker)
    if q:
        query = query.where(or_(Broker.full_name.ilike(f"%{q}%"), Broker.email.ilike(f"%{q}%"), Broker.company.ilike(f"%{q}%")))
    if status:
        query = query.where(Broker.status == BrokerStatus(status))
    rows, meta = await page_rows(db, query.order_by(Broker.created_at.desc(), Broker.id), page, limit)
    return {**meta, "items": [client_out(*row) for row in rows]}


def sale_query(client_id=None, lead_id=None, q="", market=None, since=None, until=None):
    query = select(Purchase, Broker, Lead).join(Broker, Broker.id == Purchase.broker_id).join(Lead, Lead.id == Purchase.lead_id).where(Broker.role == BrokerRole.broker)
    if client_id:
        query = query.where(Purchase.broker_id == client_id)
    if lead_id:
        query = query.where(Purchase.lead_id == lead_id)
    if market:
        query = query.where(Lead.insurance_type == market)
    if q:
        query = query.where(or_(Broker.company.ilike(f"%{q}%"), Broker.email.ilike(f"%{q}%"), Broker.full_name.ilike(f"%{q}%"), Lead.first_name.ilike(f"%{q}%"), Lead.last_name.ilike(f"%{q}%"), Lead.email.ilike(f"%{q}%")))
    return dates(query, Purchase.created_at, since, until)


def sale_out(p, b, l):
    return {"id": str(p.id), "client_id": str(b.id), "client_name": b.full_name, "company": b.company, "client_email": b.email, "lead_id": str(l.id), "lead_name": f"{l.first_name} {l.last_name}".strip(), "lead_email": l.email, "market": l.insurance_type, "city": l.city, "exclusive": p.exclusive, "price_cents": p.price_cents, "purchased_at": p.created_at, "order_id": str(p.order_id) if p.order_id else None}


@router.get("/sales")
async def sales(q: str = Query("", max_length=200), market: str | None = None, client_id: uuid.UUID | None = None, lead_id: uuid.UUID | None = None, since: date | None = None, until: date | None = None, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    query = sale_query(client_id, lead_id, q, market, since, until)
    filtered = query.subquery()
    amount = await db.scalar(select(func.coalesce(func.sum(filtered.c.price_cents), 0)))
    rows, meta = await page_rows(db, query.order_by(Purchase.created_at.desc(), Purchase.id), page, limit)
    return {**meta, "amount_cents": amount, "items": [sale_out(*row) for row in rows]}


@router.get("/clients/{client_id}")
async def client_detail(client_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    b = await db.get(Broker, client_id)
    if not b or b.role != BrokerRole.broker:
        raise HTTPException(404, "Client introuvable.")
    row = (await db.execute(select(func.count(), func.sum(Purchase.price_cents), func.max(Purchase.created_at)).where(Purchase.broker_id == client_id))).one()
    by_market = (await db.execute(select(Lead.insurance_type, func.count(Purchase.id), func.sum(Purchase.price_cents)).join(Purchase, Purchase.lead_id == Lead.id).where(Purchase.broker_id == client_id).group_by(Lead.insurance_type))).all()
    return {**client_out(b, *row), "by_market": [{"market": m, "sales": n, "spent_cents": amount} for m, n, amount in by_market]}


@router.get("/export.csv")
async def export_sales(client_id: uuid.UUID | None = None, lead_id: uuid.UUID | None = None, q: str = Query("", max_length=200), market: str | None = None, since: date | None = None, until: date | None = None, db: AsyncSession = Depends(get_db)):
    query = sale_query(client_id, lead_id, q, market, since, until)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    if total > 10000:
        raise HTTPException(422, "Réduisez la période : maximum 10 000 ventes par export.")
    rows = (await db.execute(query.order_by(Purchase.created_at.desc(), Purchase.id))).all()
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=";")
    writer.writerow(["Vente", "Date", "Client", "Société", "Email client", "Lead", "Marché", "Exclusif", "Montant EUR", "Commande"])
    def safe(value):
        value = str(value)
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value
    for p, b, l in rows:
        writer.writerow([safe(v) for v in (p.id, p.created_at.isoformat(), b.full_name, b.company, b.email, f"{l.first_name} {l.last_name}", l.insurance_type, "Oui" if p.exclusive else "Non", f"{p.price_cents / 100:.2f}", p.order_id or "")])
    return Response(content="\ufeff" + stream.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="ficheassur-ventes.csv"', "Cache-Control": "no-store"})
