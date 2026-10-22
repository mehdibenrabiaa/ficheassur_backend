"""Leads put on sale by New World Courtage, sent from its backend (never from
a browser). Machine-to-machine: authenticated with a shared secret in the
X-Import-Key header (IMPORT_API_KEY here, FICHEASSUR_IMPORT_KEY over there).

- PUT    /api/v1/imports/leads/{source_ref}  list or update the lead (idempotent)
- DELETE /api/v1/imports/leads/{source_ref}  withdraw it -- only while nobody
  has bought it or is paying for it
- GET    /api/v1/imports/leads/{source_ref}  its state here (on sale, bought...)
"""

import hmac
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.lead import Lead, LeadStatus
from app.models.lead_import import LeadImport
from app.pricing import PRICES_CENTS, SHELF_DAYS
from app.schemas.access_request import AccessRequestCreate
from app.services.orders import taken_by_lead

router = APIRouter(prefix="/api/v1/imports", tags=["imports"])
SOURCE = "nwc"


def require_import_key(x_import_key: str | None = Header(default=None)) -> None:
    if not settings.import_api_key:
        raise HTTPException(status_code=503, detail="Import non configuré (IMPORT_API_KEY).")
    if not x_import_key or not hmac.compare_digest(x_import_key, settings.import_api_key):
        raise HTTPException(status_code=401, detail="Clé d'import invalide.")


class ImportedLead(BaseModel):
    insurance_type: str
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(default="", max_length=80)
    email: EmailStr
    phone: str = Field(min_length=6, max_length=25)
    zip_code: str = Field(pattern=r"^\d{5}$")
    city: str = Field(min_length=1, max_length=120)
    details: dict[str, Any] = Field(default_factory=dict)
    requested_at: datetime  # when the prospect asked for a quote (the lead's age on the marketplace)
    partner_consent_at: datetime  # when they agreed to be contacted by partner brokers

    @field_validator("insurance_type")
    @classmethod
    def market(cls, v: str) -> str:
        if v not in PRICES_CENTS:
            raise ValueError(f"Marché inconnu : {v}")
        return v

    @field_validator("phone")
    @classmethod
    def phone_e164(cls, v: str) -> str:
        return AccessRequestCreate.e164(v)

    @field_validator("details")
    @classmethod
    def bounded(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 30:
            raise ValueError("Trop de champs de qualification.")
        return {str(k)[:80]: str(val)[:200] for k, val in v.items() if val not in (None, "")}


class ImportState(BaseModel):
    lead_id: str
    status: str  # on_sale | sold | withdrawn | expired
    buyers: int
    exclusive: bool


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def _state(db: AsyncSession, imp: LeadImport, lead: Lead) -> ImportState:
    taken = (await taken_by_lead(db, [lead.id], include_holds=False)).get(lead.id, [])
    if imp.withdrawn_at:
        status = "withdrawn"
    elif taken:
        status = "sold"
    elif _aware(lead.created_at) < datetime.now(timezone.utc) - timedelta(days=SHELF_DAYS):
        status = "expired"
    else:
        status = "on_sale"
    return ImportState(lead_id=str(lead.id), status=status, buyers=len(taken), exclusive=any(t.exclusive for t in taken))


async def _find(db: AsyncSession, source_ref: str) -> tuple[LeadImport, Lead] | None:
    imp = (await db.execute(select(LeadImport).where(LeadImport.source == SOURCE, LeadImport.source_ref == source_ref))).scalar_one_or_none()
    return (imp, await db.get(Lead, imp.lead_id)) if imp else None


@router.put("/leads/{source_ref}", response_model=ImportState, dependencies=[Depends(require_import_key)])
async def put_lead(source_ref: str, payload: ImportedLead, db: AsyncSession = Depends(get_db)):
    if _aware(payload.requested_at) < datetime.now(timezone.utc) - timedelta(days=SHELF_DAYS):
        raise HTTPException(status_code=422, detail=f"Demande de plus de {SHELF_DAYS} jours : elle ne serait plus visible à la vente.")
    found = await _find(db, source_ref[:80])
    data = payload.model_dump(exclude={"requested_at", "partner_consent_at"})
    data["details"] = {**payload.details, "Consentement partenaires": "Oui"}
    if found:
        imp, lead = found
        if (await taken_by_lead(db, [lead.id])).get(lead.id):
            raise HTTPException(status_code=409, detail="Ce lead a déjà été acheté (ou est en cours de paiement) : il ne peut plus être modifié.")
        for k, v in data.items():
            setattr(lead, k, v)
        lead.status = LeadStatus.NEW
        imp.withdrawn_at = None
    else:
        lead = Lead(id=uuid.uuid4(), **data, opt_in_marketing=True, status=LeadStatus.NEW, created_at=_aware(payload.requested_at))
        db.add(lead)
        await db.flush()
        imp = LeadImport(source=SOURCE, source_ref=source_ref[:80], lead_id=lead.id)
        db.add(imp)
    await db.commit()
    return await _state(db, imp, lead)


@router.get("/leads/{source_ref}", response_model=ImportState, dependencies=[Depends(require_import_key)])
async def get_lead(source_ref: str, db: AsyncSession = Depends(get_db)):
    found = await _find(db, source_ref)
    if not found:
        raise HTTPException(status_code=404, detail="Lead inconnu.")
    return await _state(db, *found)


@router.delete("/leads/{source_ref}", response_model=ImportState, dependencies=[Depends(require_import_key)])
async def withdraw_lead(source_ref: str, db: AsyncSession = Depends(get_db)):
    found = await _find(db, source_ref)
    if not found:
        raise HTTPException(status_code=404, detail="Lead inconnu.")
    imp, lead = found
    if (await taken_by_lead(db, [lead.id])).get(lead.id):
        raise HTTPException(status_code=409, detail="Ce lead a déjà été acheté (ou est en cours de paiement) : il ne peut plus être retiré.")
    lead.status = LeadStatus.REJECTED  # hidden from the marketplace
    imp.withdrawn_at = datetime.now(timezone.utc)
    await db.commit()
    return await _state(db, imp, lead)
