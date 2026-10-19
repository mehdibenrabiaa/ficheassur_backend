"""Espace pro sign-in: login, activation (first password from the link an
admin sends), and the signed-in broker's own profile."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.leads import limiter
from app.database import get_db
from app.models.broker import Broker, BrokerStatus
from app.security import create_access_token, current_broker, hash_password, hash_token, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class ActivateIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=8, max_length=200)


class BrokerOut(BaseModel):
    id: str
    email: str
    full_name: str
    company: str
    role: str
    lead_types: list[str]


class TokenOut(BaseModel):
    access_token: str
    broker: BrokerOut


def broker_out(b: Broker) -> BrokerOut:
    return BrokerOut(id=str(b.id), email=b.email, full_name=b.full_name, company=b.company, role=b.role.value, lead_types=b.lead_types or [])


@router.post("/login", response_model=TokenOut)
@limiter.limit("10/minute")
async def login(request: Request, payload: LoginIn, db: AsyncSession = Depends(get_db)):
    broker = (await db.execute(select(Broker).where(Broker.email == payload.email.lower()))).scalar_one_or_none()
    # Same message whatever went wrong, so the form can't be used to find accounts.
    if not broker or not verify_password(payload.password, broker.password_hash):
        raise HTTPException(status_code=401, detail="Email ou mot de passe incorrect.")
    if broker.status == BrokerStatus.disabled:
        raise HTTPException(status_code=403, detail="Ce compte est désactivé. Contactez-nous.")
    if broker.status != BrokerStatus.active:
        raise HTTPException(status_code=403, detail="Activez d'abord votre compte avec le lien reçu.")
    broker.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    return TokenOut(access_token=create_access_token(broker), broker=broker_out(broker))


@router.post("/activate", response_model=TokenOut)
@limiter.limit("10/minute")
async def activate(request: Request, payload: ActivateIn, db: AsyncSession = Depends(get_db)):
    broker = (await db.execute(select(Broker).where(Broker.activation_token_hash == hash_token(payload.token)))).scalar_one_or_none()
    expires = broker.activation_expires_at if broker else None
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if not broker or not expires or expires < datetime.now(timezone.utc) or broker.status == BrokerStatus.disabled:
        raise HTTPException(status_code=400, detail="Ce lien d'activation n'est plus valable. Demandez-en un nouveau.")
    broker.password_hash = hash_password(payload.password)
    broker.status = BrokerStatus.active
    broker.activation_token_hash = None  # one use only
    broker.activation_expires_at = None
    broker.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    return TokenOut(access_token=create_access_token(broker), broker=broker_out(broker))


@router.get("/me", response_model=BrokerOut)
async def me(broker: Broker = Depends(current_broker)):
    return broker_out(broker)
