"""The broker's CRM: the leads they bought, with full contact details, a
pipeline status, a callback reminder, notes, a history timeline, a dashboard
summary and a CSV export."""

import csv
import io
import uuid
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.broker import Broker
from typing import Literal

from app.models.commerce import BrokerExpense, CrmStatus, Purchase, PurchaseEvent, PurchaseNote, PurchaseRevenue
from app.models.lead import Lead
from app.security import current_broker

router = APIRouter(prefix="/api/v1/crm", tags=["crm"])

PARIS = ZoneInfo("Europe/Paris")  # how times are written in the history

STATUS_LABELS = {
    CrmStatus.new: "Nouveau",
    CrmStatus.contacted: "Contacté",
    CrmStatus.quote_sent: "Devis envoyé",
    CrmStatus.won: "Signé",
    CrmStatus.lost: "Perdu",
}


class CrmLead(BaseModel):
    id: str  # the purchase id
    lead_id: str
    insurance_type: str
    first_name: str
    last_name: str
    email: str
    phone: str
    zip_code: str
    city: str
    details: dict[str, Any]
    exclusive: bool
    price_cents: int
    status: CrmStatus
    reminder_at: datetime | None
    purchased_at: datetime
    lead_created_at: datetime


class NoteOut(BaseModel):
    id: str
    body: str
    created_at: datetime


class EventOut(BaseModel):
    id: str
    kind: str
    detail: str
    created_at: datetime


class RevenueOut(BaseModel):
    id: str
    amount_cents: int
    label: str
    invoiced_on: date
    created_at: datetime


class RevenueIn(BaseModel):
    amount_cents: int = Field(gt=0, le=100_000_000)  # up to 1 M€ a line
    label: str = Field(min_length=1, max_length=80)
    invoiced_on: date


class CrmLeadDetail(CrmLead):
    notes: list[NoteOut]
    events: list[EventOut]
    revenues: list[RevenueOut]


class CrmUpdate(BaseModel):
    status: CrmStatus | None = None
    # Send null (with clear_reminder) to remove the reminder.
    reminder_at: datetime | None = None
    clear_reminder: bool = False


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _out(p: Purchase, lead: Lead) -> CrmLead:
    return CrmLead(
        id=str(p.id), lead_id=str(lead.id), insurance_type=lead.insurance_type, first_name=lead.first_name,
        last_name=lead.last_name, email=lead.email, phone=lead.phone, zip_code=lead.zip_code, city=lead.city,
        details=lead.details or {}, exclusive=p.exclusive, price_cents=p.price_cents, status=p.crm_status,
        reminder_at=_aware(p.reminder_at), purchased_at=_aware(p.created_at), lead_created_at=_aware(lead.created_at),
    )


def _mine(broker: Broker):
    return select(Purchase, Lead).join(Lead, Lead.id == Purchase.lead_id).where(Purchase.broker_id == broker.id)


async def _get(db: AsyncSession, broker: Broker, purchase_id: uuid.UUID) -> tuple[Purchase, Lead]:
    row = (await db.execute(_mine(broker).where(Purchase.id == purchase_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Lead introuvable.")
    return row[0], row[1]


def _filtered(broker: Broker, status: CrmStatus | None, q: str | None):
    query = _mine(broker)
    if status:
        query = query.where(Purchase.crm_status == status)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(
            Lead.first_name.ilike(like), Lead.last_name.ilike(like), Lead.email.ilike(like),
            Lead.phone.ilike(like), Lead.city.ilike(like), Lead.zip_code.ilike(like),
        ))
    return query.order_by(Purchase.created_at.desc())


@router.get("/leads", response_model=list[CrmLead])
async def list_leads(
    status: CrmStatus | None = None,
    q: str | None = Query(default=None, max_length=100),
    broker: Broker = Depends(current_broker),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(_filtered(broker, status, q).limit(1000))).all()
    return [_out(p, lead) for p, lead in rows]


@router.get("/leads/{purchase_id}", response_model=CrmLeadDetail)
async def get_lead(purchase_id: uuid.UUID, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    p, lead = await _get(db, broker, purchase_id)
    notes = (await db.execute(select(PurchaseNote).where(PurchaseNote.purchase_id == p.id).order_by(PurchaseNote.created_at.desc()))).scalars().all()
    events = (await db.execute(select(PurchaseEvent).where(PurchaseEvent.purchase_id == p.id).order_by(PurchaseEvent.created_at.desc()))).scalars().all()
    revenues = (await db.execute(select(PurchaseRevenue).where(PurchaseRevenue.purchase_id == p.id).order_by(PurchaseRevenue.invoiced_on.desc()))).scalars().all()
    return CrmLeadDetail(
        **_out(p, lead).model_dump(),
        notes=[NoteOut(id=str(n.id), body=n.body, created_at=_aware(n.created_at)) for n in notes],
        events=[EventOut(id=str(e.id), kind=e.kind, detail=e.detail, created_at=_aware(e.created_at)) for e in events],
        revenues=[_revenue_out(r) for r in revenues],
    )


def _revenue_out(r: PurchaseRevenue) -> RevenueOut:
    return RevenueOut(id=str(r.id), amount_cents=r.amount_cents, label=r.label, invoiced_on=r.invoiced_on, created_at=_aware(r.created_at))


def _euros(cents: int) -> str:
    return f"{cents / 100:,.2f} €".replace(",", " ").replace(".", ",")


@router.post("/leads/{purchase_id}/revenues", response_model=RevenueOut, status_code=201)
async def add_revenue(purchase_id: uuid.UUID, payload: RevenueIn, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    """Record an amount the broker invoiced for this lead (commission, fees...)."""
    p, _ = await _get(db, broker, purchase_id)
    r = PurchaseRevenue(purchase_id=p.id, broker_id=broker.id, amount_cents=payload.amount_cents, label=payload.label.strip(), invoiced_on=payload.invoiced_on)
    db.add(r)
    db.add(PurchaseEvent(purchase_id=p.id, kind="revenue", detail=f"Chiffre d'affaires ajouté : {r.label}, {_euros(r.amount_cents)}"))
    await db.commit()
    await db.refresh(r)
    return _revenue_out(r)


@router.delete("/revenues/{revenue_id}", status_code=204)
async def delete_revenue(revenue_id: uuid.UUID, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    r = await db.get(PurchaseRevenue, revenue_id)
    if not r or r.broker_id != broker.id:
        raise HTTPException(status_code=404, detail="Montant introuvable.")
    db.add(PurchaseEvent(purchase_id=r.purchase_id, kind="revenue", detail=f"Chiffre d'affaires supprimé : {r.label}, {_euros(r.amount_cents)}"))
    await db.delete(r)
    await db.commit()


@router.patch("/leads/{purchase_id}", response_model=CrmLead)
async def update_lead(purchase_id: uuid.UUID, payload: CrmUpdate, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    p, lead = await _get(db, broker, purchase_id)
    if payload.status and payload.status != p.crm_status:
        db.add(PurchaseEvent(purchase_id=p.id, kind="status", detail=f"{STATUS_LABELS[p.crm_status]} → {STATUS_LABELS[payload.status]}"))
        p.crm_status = payload.status
    if payload.clear_reminder and p.reminder_at:
        p.reminder_at = None
        db.add(PurchaseEvent(purchase_id=p.id, kind="reminder", detail="Rappel supprimé"))
    elif payload.reminder_at:
        when = _aware(payload.reminder_at)
        p.reminder_at = when
        db.add(PurchaseEvent(purchase_id=p.id, kind="reminder", detail=f"Rappel prévu le {when.astimezone(PARIS):%d/%m/%Y à %H:%M}"))
    await db.commit()
    await db.refresh(p)
    return _out(p, lead)


@router.post("/leads/{purchase_id}/notes", response_model=NoteOut, status_code=201)
async def add_note(purchase_id: uuid.UUID, payload: NoteIn, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    p, _ = await _get(db, broker, purchase_id)
    note = PurchaseNote(purchase_id=p.id, body=payload.body.strip())
    db.add(note)
    db.add(PurchaseEvent(purchase_id=p.id, kind="note", detail="Note ajoutée"))
    await db.commit()
    return NoteOut(id=str(note.id), body=note.body, created_at=_aware(note.created_at))


@router.get("/summary")
async def summary(broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    """The dashboard: spent this month, leads per status, reminders due
    (today or overdue), and the latest purchases."""
    counts = dict((await db.execute(
        select(Purchase.crm_status, func.count()).where(Purchase.broker_id == broker.id).group_by(Purchase.crm_status)
    )).all())
    end_of_today = datetime.combine(datetime.now(timezone.utc).date(), time.max, tzinfo=timezone.utc)
    due = (await db.execute(
        _mine(broker).where(Purchase.reminder_at.is_not(None), Purchase.reminder_at <= end_of_today).order_by(Purchase.reminder_at).limit(20)
    )).all()
    recent = (await db.execute(_mine(broker).order_by(Purchase.created_at.desc()).limit(5))).all()
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    spent = await db.scalar(
        select(func.coalesce(func.sum(Purchase.price_cents), 0)).where(Purchase.broker_id == broker.id, Purchase.created_at >= month_start)
    )
    return {
        "spent_month_cents": int(spent or 0),
        "counts": {s.value: counts.get(s, 0) for s in CrmStatus},
        "total": sum(counts.values()),
        "reminders_due": [_out(p, lead) for p, lead in due],
        "recent": [_out(p, lead) for p, lead in recent],
    }


@router.get("/export.csv")
async def export_csv(
    status: CrmStatus | None = None,
    q: str | None = Query(default=None, max_length=100),
    broker: Broker = Depends(current_broker),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(_filtered(broker, status, q))).all()
    buf = io.StringIO()
    buf.write("﻿")  # BOM: Excel opens accents correctly
    w = csv.writer(buf, delimiter=";")  # French Excel expects ";"
    w.writerow(["Acheté le", "Marché", "Prénom", "Nom", "Email", "Téléphone", "Code postal", "Ville", "Statut", "Rappel", "Exclusif", "Prix (€)"])
    for p, lead in rows:
        reminder = _aware(p.reminder_at)
        w.writerow([
            f"{_aware(p.created_at):%d/%m/%Y}", lead.insurance_type, lead.first_name, lead.last_name, lead.email, lead.phone,
            lead.zip_code, lead.city, STATUS_LABELS[p.crm_status], f"{reminder.astimezone(PARIS):%d/%m/%Y %H:%M}" if reminder else "",
            "Oui" if p.exclusive else "Non", f"{p.price_cents / 100:.2f}".replace(".", ","),
        ])
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="mes-leads-ficheassur.csv"'},
    )


# --- Portfolio views: the same data as a lead's page, across all of the
# broker's leads (always through _mine/broker_id, never another broker's).

class LeadRef(BaseModel):
    """Who an item is about, enough to show and link it."""
    purchase_id: str
    name: str
    phone: str
    city: str
    insurance_type: str
    status: CrmStatus


def _ref(p: Purchase, lead: Lead) -> LeadRef:
    return LeadRef(purchase_id=str(p.id), name=f"{lead.first_name} {lead.last_name}".strip(), phone=lead.phone,
                   city=lead.city, insurance_type=lead.insurance_type, status=p.crm_status)


class ReminderItem(BaseModel):
    reminder_at: datetime
    lead: LeadRef


class PortfolioNote(BaseModel):
    id: str
    body: str
    created_at: datetime
    lead: LeadRef


class ActivityItem(BaseModel):
    id: str
    kind: str
    detail: str
    created_at: datetime
    lead: LeadRef


@router.get("/reminders", response_model=list[ReminderItem])
async def reminders(broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    """Every callback planned, soonest (or most overdue) first."""
    rows = (await db.execute(_mine(broker).where(Purchase.reminder_at.isnot(None)).order_by(Purchase.reminder_at))).all()
    return [ReminderItem(reminder_at=_aware(p.reminder_at), lead=_ref(p, lead)) for p, lead in rows]


# Notes and activity can grow to thousands of rows: they are served a page
# at a time, newest first, with a cursor (keyset pagination: "after the last
# row you got", stable while new rows arrive at the top -- unlike offsets),
# and filtered/searched on the server, never in the browser.
PAGE_SIZE = 50


def _cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    return f"{created_at.isoformat()}_{row_id}"


def _after_cursor(query, model, cursor: str | None):
    if not cursor:
        return query
    try:
        at, row_id = cursor.rsplit("_", 1)
        at_dt, rid = datetime.fromisoformat(at), uuid.UUID(row_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Curseur invalide.")
    return query.where(or_(model.created_at < at_dt, and_(model.created_at == at_dt, model.id < rid)))


class NotesPage(BaseModel):
    items: list[PortfolioNote]
    total: int
    next_cursor: str | None


class ActivityPage(BaseModel):
    items: list[ActivityItem]
    total: int
    next_cursor: str | None


@router.get("/notes", response_model=NotesPage)
async def all_notes(
    q: str | None = Query(default=None, max_length=100),
    cursor: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=PAGE_SIZE, ge=1, le=100),
    broker: Broker = Depends(current_broker),
    db: AsyncSession = Depends(get_db),
):
    """The broker's notes, newest first; q searches the note and the prospect's name or city."""
    query = (
        select(PurchaseNote, Purchase, Lead)
        .join(Purchase, Purchase.id == PurchaseNote.purchase_id)
        .join(Lead, Lead.id == Purchase.lead_id)
        .where(Purchase.broker_id == broker.id)
    )
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(PurchaseNote.body.ilike(like), Lead.first_name.ilike(like), Lead.last_name.ilike(like), Lead.city.ilike(like)))
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = (await db.execute(
        _after_cursor(query, PurchaseNote, cursor).order_by(PurchaseNote.created_at.desc(), PurchaseNote.id.desc()).limit(limit + 1)
    )).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return NotesPage(
        items=[PortfolioNote(id=str(n.id), body=n.body, created_at=_aware(n.created_at), lead=_ref(p, lead)) for n, p, lead in rows],
        total=total,
        next_cursor=_cursor(rows[-1][0].created_at, rows[-1][0].id) if more else None,
    )


@router.get("/activity", response_model=ActivityPage)
async def activity(
    kind: str | None = Query(default=None, pattern="^(purchased|status|reminder|note|revenue)$"),
    cursor: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=PAGE_SIZE, ge=1, le=100),
    broker: Broker = Depends(current_broker),
    db: AsyncSession = Depends(get_db),
):
    """The history of the whole portfolio (purchases, status changes, reminders, notes), newest first."""
    query = (
        select(PurchaseEvent, Purchase, Lead)
        .join(Purchase, Purchase.id == PurchaseEvent.purchase_id)
        .join(Lead, Lead.id == Purchase.lead_id)
        .where(Purchase.broker_id == broker.id)
    )
    if kind:
        query = query.where(PurchaseEvent.kind == kind)
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = (await db.execute(
        _after_cursor(query, PurchaseEvent, cursor).order_by(PurchaseEvent.created_at.desc(), PurchaseEvent.id.desc()).limit(limit + 1)
    )).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return ActivityPage(
        items=[ActivityItem(id=str(e.id), kind=e.kind, detail=e.detail, created_at=_aware(e.created_at), lead=_ref(p, lead)) for e, p, lead in rows],
        total=total,
        next_cursor=_cursor(rows[-1][0].created_at, rows[-1][0].id) if more else None,
    )



# --- Finances: what the broker spent on leads against what those leads earned.

class FinanceTotals(BaseModel):
    spent_cents: int
    revenue_cents: int
    leads: int
    leads_with_revenue: int


class FinanceMarket(FinanceTotals):
    insurance_type: str


class FinanceSummary(FinanceTotals):
    expenses_cents: int = 0  # frais généraux over the period


class FinanceMonth(BaseModel):
    month: str  # "2026-10"
    spent_cents: int
    revenue_cents: int
    expenses_cents: int = 0


class FinanceLead(BaseModel):
    lead: LeadRef
    price_cents: int
    revenue_cents: int


class FinanceOut(BaseModel):
    totals: FinanceSummary
    by_market: list[FinanceMarket]
    months: list[FinanceMonth]  # oldest first: the period's months, or the 12 up to its end
    top_leads: list[FinanceLead]


def _month(d: datetime | date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _months_between(first: date, last: date) -> list[str]:
    keys, y, m = [], first.year, first.month
    while (y, m) <= (last.year, last.month):
        keys.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return keys


@router.get("/finance", response_model=FinanceOut)
async def finance(
    since: date | None = Query(default=None, description="Period start (default: the beginning)"),
    until: date | None = Query(default=None, description="Period end, included (default: today)"),
    market: str | None = Query(default=None, pattern="^(AUTO|MOTO|AUTO_PRO|COMMERCIAL_AUTO)$", description="One market only"),
    broker: Broker = Depends(current_broker),
    db: AsyncSession = Depends(get_db),
):
    """What happened over a period, each amount on its own date: leads by
    purchase date (spent), revenue by invoice date, frais by the month they
    are paid -- so "October" means what was bought, invoiced and paid in
    October. Per market, per month, and the leads that earned the most over
    the period. With a market, only that market's leads and revenue -- frais
    généraux belong to no market, so they are left out. Only this broker's data."""
    today = datetime.now(PARIS).date()
    last = until or today
    if since and since > last:
        raise HTTPException(status_code=422, detail="La date de début est après la date de fin.")

    rows = (await db.execute(_mine(broker).where(Lead.insurance_type == market) if market else _mine(broker))).all()
    by_id = {p.id: (p, lead) for p, lead in rows}
    bought_on = {p.id: _aware(p.created_at).astimezone(PARIS).date() for p, _ in rows}
    revenues = (await db.execute(select(PurchaseRevenue).where(PurchaseRevenue.broker_id == broker.id))).scalars().all()
    expenses = [] if market else (await db.execute(select(BrokerExpense).where(BrokerExpense.broker_id == broker.id))).scalars().all()
    first = since or min([*bought_on.values(), *(r.invoiced_on for r in revenues), *(e.start_on for e in expenses)], default=today)

    in_period = lambda d: first <= d <= last  # noqa: E731
    bought = [(p, lead) for p, lead in rows if in_period(bought_on[p.id])]
    invoiced = [r for r in revenues if in_period(r.invoiced_on) and r.purchase_id in by_id]
    earned: dict[uuid.UUID, int] = {}
    for r in invoiced:
        earned[r.purchase_id] = earned.get(r.purchase_id, 0) + r.amount_cents

    def totals(market: str | None = None) -> dict:
        b = [p for p, lead in bought if market is None or lead.insurance_type == market]
        e = {pid: c for pid, c in earned.items() if market is None or by_id[pid][1].insurance_type == market}
        return {"spent_cents": sum(p.price_cents for p in b), "revenue_cents": sum(e.values()), "leads": len(b), "leads_with_revenue": len(e)}

    markets = {lead.insurance_type for _, lead in bought} | {by_id[pid][1].insurance_type for pid in earned}

    # The chart: the period's months when it spans two or more, otherwise the
    # 12 months up to its end (a single month has no trend). At most 24.
    chart_keys = _months_between(first, last) if (since and (last.year, last.month) != (first.year, first.month)) else []
    if not chart_keys:
        y, m = last.year, last.month - 11
        while m < 1:
            y, m = y - 1, m + 12
        chart_keys = _months_between(date(y, m, 1), last)
    chart_keys = chart_keys[-24:]
    month = {k: {"spent_cents": 0, "revenue_cents": 0, "expenses_cents": 0} for k in chart_keys}
    for p, _ in rows:
        k = _month(bought_on[p.id])
        if k in month:
            month[k]["spent_cents"] += p.price_cents
    for r in revenues:
        k = _month(r.invoiced_on)
        if k in month:
            month[k]["revenue_cents"] += r.amount_cents
    chart_first = date(int(chart_keys[0][:4]), int(chart_keys[0][5:]), 1)
    for e in expenses:
        for k, cents in _occurrences(e, chart_first, last):
            if k in month:
                month[k]["expenses_cents"] += cents

    top = sorted(earned, key=lambda pid: earned[pid] - by_id[pid][0].price_cents, reverse=True)[:10]
    return FinanceOut(
        totals=FinanceSummary(**totals(), expenses_cents=sum(c for e in expenses for _, c in _occurrences(e, first, last))),
        by_market=sorted((FinanceMarket(insurance_type=k, **totals(k)) for k in markets), key=lambda x: (-x.revenue_cents, -x.spent_cents)),
        months=[FinanceMonth(month=k, **v) for k, v in month.items()],
        top_leads=[FinanceLead(lead=_ref(*by_id[pid]), price_cents=by_id[pid][0].price_cents, revenue_cents=earned[pid]) for pid in top],
    )



# --- Frais généraux: the broker's other running costs.

def _occurrences(e: BrokerExpense, start: date, end: date):
    """(month "YYYY-MM", cents) for each time the expense is paid between
    start and end (inclusive, by month): once on start_on; monthly every
    month from start_on; yearly on each anniversary month. Stops at end_on."""
    last = min(end, e.end_on) if e.end_on else end
    lo = max(start.replace(day=1), e.start_on.replace(day=1))
    hi = last.replace(day=1)
    if e.frequency == "once":
        if lo <= e.start_on.replace(day=1) <= hi:
            yield _month(e.start_on), e.amount_cents
        return
    y, m = lo.year, lo.month
    while (y, m) <= (hi.year, hi.month):
        if e.frequency == "monthly" or m == e.start_on.month:
            yield f"{y:04d}-{m:02d}", e.amount_cents
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


EXPENSE_CATEGORIES = ("phone", "internet", "software", "other")


class ExpenseIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    category: Literal["phone", "internet", "software", "other"]
    amount_cents: int = Field(gt=0, le=10_000_000)
    frequency: Literal["once", "monthly", "yearly"]
    start_on: date
    end_on: date | None = None


class ExpenseOut(ExpenseIn):
    id: str
    monthly_cents: int  # what it weighs per month (yearly / 12, one-off: 0)


def _expense_out(e: BrokerExpense) -> ExpenseOut:
    monthly = e.amount_cents if e.frequency == "monthly" else round(e.amount_cents / 12) if e.frequency == "yearly" else 0
    return ExpenseOut(id=str(e.id), label=e.label, category=e.category, amount_cents=e.amount_cents, frequency=e.frequency,
                      start_on=e.start_on, end_on=e.end_on, monthly_cents=monthly)


async def _own_expense(db: AsyncSession, broker: Broker, expense_id: uuid.UUID) -> BrokerExpense:
    e = await db.get(BrokerExpense, expense_id)
    if not e or e.broker_id != broker.id:
        raise HTTPException(status_code=404, detail="Frais introuvable.")
    return e


@router.get("/expenses", response_model=list[ExpenseOut])
async def list_expenses(broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(BrokerExpense).where(BrokerExpense.broker_id == broker.id).order_by(BrokerExpense.start_on.desc()))).scalars().all()
    return [_expense_out(e) for e in rows]


@router.post("/expenses", response_model=ExpenseOut, status_code=201)
async def add_expense(payload: ExpenseIn, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    if payload.end_on and payload.end_on < payload.start_on:
        raise HTTPException(status_code=422, detail="La date de fin est avant la date de début.")
    e = BrokerExpense(broker_id=broker.id, **payload.model_dump())
    e.label = e.label.strip()
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return _expense_out(e)


class ExpenseStop(BaseModel):
    end_on: date | None  # null: running again


@router.patch("/expenses/{expense_id}", response_model=ExpenseOut)
async def stop_expense(expense_id: uuid.UUID, payload: ExpenseStop, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    """Stop a subscription (from end_on), or restart it (end_on null)."""
    e = await _own_expense(db, broker, expense_id)
    if payload.end_on and payload.end_on < e.start_on:
        raise HTTPException(status_code=422, detail="La date de fin est avant la date de début.")
    e.end_on = payload.end_on
    await db.commit()
    return _expense_out(e)


@router.delete("/expenses/{expense_id}", status_code=204)
async def delete_expense(expense_id: uuid.UUID, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    await db.delete(await _own_expense(db, broker, expense_id))
    await db.commit()
