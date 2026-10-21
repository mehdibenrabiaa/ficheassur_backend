"""The /assistance page's contact form, and its inbox for admins."""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.leads import limiter
from app.database import get_db
from app.models.broker import Broker
from app.models.support import SupportRequest
from app.security import current_admin, current_broker

router = APIRouter(prefix="/api/v1/support", tags=["support"])

Topic = Literal["compte", "achat", "paiement", "qualite", "crm", "partenariat", "autre"]


class SupportIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    topic: Topic
    message: str = Field(min_length=10, max_length=4000)


class SupportOut(BaseModel):
    id: str
    full_name: str
    email: str
    topic: str
    message: str
    from_broker: bool
    resolved: bool
    created_at: datetime


def support_out(r: SupportRequest) -> SupportOut:
    return SupportOut(id=str(r.id), full_name=r.full_name, email=r.email, topic=r.topic, message=r.message,
                      from_broker=r.broker_id is not None, resolved=r.resolved, created_at=r.created_at)


@router.post("", status_code=201)
@limiter.limit("5/hour")
async def create_support_request(request: Request, payload: SupportIn, authorization: str | None = Header(default=None),
                                 db: AsyncSession = Depends(get_db)):
    # Signed in? Tie the message to the account; anyone may write, though.
    broker_id = None
    if authorization:
        try:
            broker_id = (await current_broker(authorization, db)).id
        except HTTPException:
            pass
    db.add(SupportRequest(full_name=payload.full_name.strip(), email=payload.email.lower(), topic=payload.topic,
                          message=payload.message.strip(), broker_id=broker_id, ip_address=get_remote_address(request)))
    await db.commit()
    return {"ok": True}


@router.get("", response_model=list[SupportOut])
async def list_support_requests(_: Broker = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SupportRequest).order_by(SupportRequest.created_at.desc()).limit(500))).scalars()
    return [support_out(r) for r in rows]


@router.post("/{request_id}/resolve", response_model=SupportOut)
async def resolve_support_request(request_id: uuid.UUID, resolved: bool = True, _: Broker = Depends(current_admin),
                                  db: AsyncSession = Depends(get_db)):
    r = await db.get(SupportRequest, request_id)
    if not r:
        raise HTTPException(status_code=404, detail="Message introuvable.")
    r.resolved = resolved
    await db.commit()
    return support_out(r)
