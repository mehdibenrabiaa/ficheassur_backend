"""The broker's CRM: the leads they bought, with full contact details, a
pipeline status, a callback reminder, notes, a history timeline, a dashboard
summary and a CSV export."""

import csv
import io
import uuid
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.broker import Broker
from app.models.commerce import CrmStatus, Purchase, PurchaseEvent, PurchaseNote
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


class CrmLeadDetail(CrmLead):
    notes: list[NoteOut]
    events: list[EventOut]


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
    return CrmLeadDetail(
        **_out(p, lead).model_dump(),
        notes=[NoteOut(id=str(n.id), body=n.body, created_at=_aware(n.created_at)) for n in notes],
        events=[EventOut(id=str(e.id), kind=e.kind, detail=e.detail, created_at=_aware(e.created_at)) for e in events],
    )


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
