"""FicheAssur admin (role "admin"): partner requests and broker accounts.
Accepting a request creates the broker and returns their one-time
activation link -- no email sending yet, the admin sends it."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.access_request import AccessRequest
from app.models.broker import Broker, BrokerStatus
from app.security import ACTIVATION_TTL, current_admin, new_activation_token

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(current_admin)])


class AccessRequestOut(BaseModel):
    id: str
    full_name: str
    company: str
    email: str
    phone: str
    orias_number: str | None
    lead_types: list[str]
    message: str | None
    created_at: datetime
    # Set once accepted: the broker account's status.
    broker_status: str | None = None


class BrokerAdminOut(BaseModel):
    id: str
    email: str
    full_name: str
    company: str
    orias_number: str | None
    role: str
    status: str
    created_at: datetime
    last_login_at: datetime | None


class ActivationLinkOut(BaseModel):
    broker: BrokerAdminOut
    activation_url: str
    expires_at: datetime


def _broker_admin_out(b: Broker) -> BrokerAdminOut:
    return BrokerAdminOut(
        id=str(b.id), email=b.email, full_name=b.full_name, company=b.company, orias_number=b.orias_number,
        role=b.role.value, status=b.status.value, created_at=b.created_at, last_login_at=b.last_login_at,
    )


async def _issue_link(db: AsyncSession, broker: Broker) -> ActivationLinkOut:
    token, token_hash = new_activation_token()
    broker.activation_token_hash = token_hash
    broker.activation_expires_at = datetime.now(timezone.utc) + ACTIVATION_TTL
    await db.commit()
    await db.refresh(broker)
    return ActivationLinkOut(
        broker=_broker_admin_out(broker),
        activation_url=f"{settings.site_url.rstrip('/')}/activer?token={token}",
        expires_at=broker.activation_expires_at,
    )


@router.get("/access-requests", response_model=list[AccessRequestOut])
async def list_access_requests(db: AsyncSession = Depends(get_db)):
    requests = (await db.execute(select(AccessRequest).order_by(AccessRequest.created_at.desc()))).scalars().all()
    brokers = {b.email: b for b in (await db.execute(select(Broker))).scalars().all()}
    return [
        AccessRequestOut(
            id=str(r.id), full_name=r.full_name, company=r.company, email=r.email, phone=r.phone,
            orias_number=r.orias_number, lead_types=r.lead_types or [], message=r.message, created_at=r.created_at,
            broker_status=brokers[r.email].status.value if r.email in brokers else None,
        )
        for r in requests
    ]


@router.post("/access-requests/{request_id}/accept", response_model=ActivationLinkOut)
async def accept_access_request(request_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    req = await db.get(AccessRequest, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Demande introuvable.")
    broker = (await db.execute(select(Broker).where(Broker.email == req.email))).scalar_one_or_none()
    if broker and broker.status != BrokerStatus.invited:
        raise HTTPException(status_code=409, detail="Un compte actif existe déjà pour cet email.")
    if not broker:
        broker = Broker(
            email=req.email, full_name=req.full_name, company=req.company, phone=req.phone,
            orias_number=req.orias_number, lead_types=req.lead_types or [], access_request_id=req.id,
        )
        db.add(broker)
    # Already invited: a fresh link replaces the old one.
    return await _issue_link(db, broker)


@router.get("/brokers", response_model=list[BrokerAdminOut])
async def list_brokers(db: AsyncSession = Depends(get_db)):
    return [_broker_admin_out(b) for b in (await db.execute(select(Broker).order_by(Broker.created_at.desc()))).scalars().all()]


@router.post("/brokers/{broker_id}/activation-link", response_model=ActivationLinkOut)
async def resend_activation_link(broker_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    broker = await db.get(Broker, broker_id)
    if not broker or broker.status != BrokerStatus.invited:
        raise HTTPException(status_code=404, detail="Aucun compte en attente d'activation.")
    return await _issue_link(db, broker)
