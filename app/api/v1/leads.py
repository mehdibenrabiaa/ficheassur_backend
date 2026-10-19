import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.lead import Lead, LeadStatus
from app.schemas.lead import LeadSubmit, LeadSubmitResponse
from app.services.matching import match_lead

logger = logging.getLogger("ficheassur.leads")

# In-memory, per process. With several workers or servers, back it with
# Redis instead: Limiter(..., storage_uri="redis://...").
limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/api/v1/leads", tags=["leads"])

DEDUP_WINDOW = timedelta(hours=24)


@router.post("/submit", response_model=LeadSubmitResponse)
@limiter.limit("3/5minutes")
async def submit_lead(
    request: Request,
    payload: LeadSubmit,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    # 1. Body validation already happened (Pydantic). 2. Rate limit via the
    # decorator above; IP reputation is a blocklist for now.
    ip = get_remote_address(request)
    if ip in settings.blocked_ips_set:
        raise HTTPException(status_code=403, detail="Requête refusée.")

    if payload.opt_in_marketing is not True:
        raise HTTPException(status_code=400, detail="Le consentement est obligatoire pour envoyer la demande.")

    # 3. Same phone or email in the last 24h: answer exactly like a success.
    since = datetime.now(timezone.utc) - DEDUP_WINDOW
    duplicate = await db.scalar(
        select(Lead.id).where(
            or_(Lead.email == payload.email, Lead.phone == payload.phone),
            Lead.created_at >= since,
        ).limit(1)
    )
    if duplicate:
        logger.info("Duplicate lead ignored (existing %s)", duplicate)
        return LeadSubmitResponse()

    # 4. Sanitizing (strip, lowercase email, E.164 phone) is done by the
    # schema validators. 5. Persist.
    lead = Lead(
        **payload.model_dump(),
        status=LeadStatus.NEW,
        ip_address=ip,
        user_agent=(request.headers.get("user-agent") or "")[:512],
    )
    db.add(lead)
    await db.commit()

    # 6. Matching runs after the response.
    background.add_task(match_lead, lead.id)
    return LeadSubmitResponse()
