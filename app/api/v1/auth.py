"""Espace pro sign-in: self-service sign-up, login, activation (first
password from a link an admin sends, for accounts an admin opened), and the
signed-in broker's own profile."""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.leads import limiter
from app.config import settings
from app.database import get_db
from app.models.broker import Broker, BrokerStatus
from app.models.oauth_identity import OAuthIdentity
from app.schemas.access_request import AccessRequestCreate
from app.schemas.lead import InsuranceType
from app.security import create_access_token, current_broker, hash_password, hash_token, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class ActivateIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=8, max_length=200)


class RegisterIn(AccessRequestCreate):
    """The partner form's fields (same validation: phone, ORIAS format) plus
    the password. Markets are optional here -- the sign-up page doesn't ask
    for them. The ORIAS number is stored but nothing waits on it -- the
    account is usable straight away."""
    lead_types: list[InsuranceType] = Field(default_factory=list, max_length=10)
    password: str = Field(min_length=8, max_length=200)


class GoogleIn(BaseModel):
    access_token: str = Field(min_length=10, max_length=4096)


class BrokerOut(BaseModel):
    id: str
    email: str
    full_name: str
    company: str
    role: str
    lead_types: list[str]
    phone: str | None = None
    orias_number: str | None = None
    has_password: bool = True  # False for accounts created with Google only
    created_at: datetime | None = None


class ProfileIn(BaseModel):
    """Mon compte: what a broker can change themselves (the email is the
    login, so it changes through us). Same rules as the sign-up form."""
    full_name: str = Field(min_length=2, max_length=120)
    company: str = Field(min_length=2, max_length=160)
    phone: str | None = Field(default=None, max_length=25)
    orias_number: str | None = Field(default=None, max_length=12)
    lead_types: list[InsuranceType] = Field(default_factory=list, max_length=10)

    @field_validator("full_name", "company", "phone", "orias_number", mode="before")
    @classmethod
    def strip(cls, v):
        return AccessRequestCreate.strip(v)

    @field_validator("phone")
    @classmethod
    def phone_e164(cls, v: str | None) -> str | None:
        return AccessRequestCreate.e164(v) if v else None

    @field_validator("orias_number")
    @classmethod
    def orias(cls, v: str | None) -> str | None:
        return AccessRequestCreate.orias(v)


class PasswordIn(BaseModel):
    """A session alone never changes the password: the broker proves it's
    them -- the current password, or for an account created with Google
    (no password yet) a fresh Google sign-in to the same email."""
    current_password: str | None = Field(default=None, max_length=200)
    google_access_token: str | None = Field(default=None, max_length=4096)
    new_password: str = Field(min_length=8, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    broker: BrokerOut


def broker_out(b: Broker) -> BrokerOut:
    created = b.created_at.replace(tzinfo=timezone.utc) if b.created_at and b.created_at.tzinfo is None else b.created_at
    return BrokerOut(
        id=str(b.id), email=b.email, full_name=b.full_name, company=b.company, role=b.role.value, lead_types=b.lead_types or [],
        phone=b.phone, orias_number=b.orias_number, has_password=bool(b.password_hash), created_at=created,
    )


@router.post("/register", response_model=TokenOut, status_code=201)
@limiter.limit("5/hour")
async def register(request: Request, payload: RegisterIn, db: AsyncSession = Depends(get_db)):
    if (await db.execute(select(Broker.id).where(Broker.email == payload.email))).first():
        raise HTTPException(status_code=409, detail="Un compte existe déjà avec cet email. Connectez-vous.")
    now = datetime.now(timezone.utc)
    broker = Broker(
        email=payload.email, full_name=payload.full_name, company=payload.company, phone=payload.phone,
        orias_number=payload.orias_number, lead_types=[t.value for t in payload.lead_types],
        status=BrokerStatus.active, password_hash=hash_password(payload.password), last_login_at=now,
    )
    db.add(broker)
    await db.commit()
    await db.refresh(broker)
    return TokenOut(access_token=create_access_token(broker), broker=broker_out(broker))


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


def _google_get(url: str, access_token: str | None = None) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"} if access_token else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.loads(res.read())
    except (urllib.error.URLError, ValueError):
        return {}


async def _verified_google_email(access_token: str | None) -> str:
    """The verified email behind a Google access token issued to *our*
    client ID (a token from another site can't be replayed here)."""
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="La connexion avec Google n'est pas encore configurée.")
    if not access_token:
        raise HTTPException(status_code=400, detail="Confirmez votre identité avec Google.")
    info = await run_in_threadpool(_google_get, "https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode({"access_token": access_token}))
    if info.get("aud") != settings.google_client_id or str(info.get("email_verified")).lower() != "true" or not info.get("email"):
        raise HTTPException(status_code=401, detail="Connexion Google refusée. Réessayez.")
    return info["email"].lower()


@router.post("/google", response_model=TokenOut)
@limiter.limit("10/minute")
async def google(request: Request, payload: GoogleIn, db: AsyncSession = Depends(get_db)):
    """"Continuer avec Google": the site gets an access token from Google's
    popup; we check with Google that it was issued to *our* client ID (so a
    token from another site can't be replayed here) and read the verified
    email. Existing account -> signed in (an invited one is activated: Google
    just proved the email). New email -> account created, like /register;
    the cabinet name is completed later in Mon compte."""
    email = await _verified_google_email(payload.access_token)
    broker = (await db.execute(select(Broker).where(Broker.email == email))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if broker:
        if broker.status == BrokerStatus.disabled:
            raise HTTPException(status_code=403, detail="Ce compte est désactivé. Contactez-nous.")
        if broker.status != BrokerStatus.active:
            broker.status = BrokerStatus.active
            broker.activation_token_hash = None
            broker.activation_expires_at = None
    else:
        profile = await run_in_threadpool(_google_get, "https://openidconnect.googleapis.com/v1/userinfo", payload.access_token)
        broker = Broker(
            email=email, full_name=(profile.get("name") or email.split("@")[0])[:120], company="",
            lead_types=[], status=BrokerStatus.active, last_login_at=now,
        )
        db.add(broker)
    broker.last_login_at = now
    await db.commit()
    await db.refresh(broker)
    return TokenOut(access_token=create_access_token(broker), broker=broker_out(broker))


@router.patch("/me", response_model=BrokerOut)
async def update_me(payload: ProfileIn, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    broker.full_name = payload.full_name
    broker.company = payload.company
    broker.phone = payload.phone
    broker.orias_number = payload.orias_number
    broker.lead_types = [t.value for t in payload.lead_types]
    await db.commit()
    return broker_out(broker)


@router.post("/password", response_model=TokenOut)
@limiter.limit("10/hour")
async def change_password(request: Request, payload: PasswordIn, broker: Broker = Depends(current_broker), db: AsyncSession = Depends(get_db)):
    """Changes the password and signs out every other session (their tokens
    carry the old password's fingerprint); this one gets a fresh token."""
    if broker.password_hash:
        if not verify_password(payload.current_password or "", broker.password_hash):
            raise HTTPException(status_code=400, detail="Mot de passe actuel incorrect.")
    else:
        email = await _verified_google_email(payload.google_access_token)
        if email != broker.email.lower():
            raise HTTPException(status_code=400, detail="Confirmez avec le compte Google de cet espace pro.")
    if payload.current_password and payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="Choisissez un mot de passe différent de l'actuel.")
    broker.password_hash = hash_password(payload.new_password)
    await db.commit()
    return TokenOut(access_token=create_access_token(broker), broker=broker_out(broker))



# --- "Continuer avec Microsoft": the site gets an ID token from Microsoft's
# popup (any work/school or personal account); we check its signature against
# Microsoft's published keys, that it was issued to *our* app, and by the
# tenant it claims. Accounts are linked by Microsoft's user id (OAuthIdentity),
# not by email -- see that model for why.

_MS_JWKS = jwt.PyJWKClient("https://login.microsoftonline.com/common/discovery/v2.0/keys", cache_keys=True)


class MicrosoftIn(BaseModel):
    id_token: str = Field(min_length=20, max_length=8192)


def _verify_microsoft(token: str) -> dict:
    """The token's claims, or HTTP 401."""
    try:
        key = _MS_JWKS.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=settings.microsoft_client_id)
    except (jwt.PyJWTError, OSError) as e:
        raise HTTPException(status_code=401, detail="Connexion Microsoft refusée. Réessayez.") from e
    tid = claims.get("tid")
    if not tid or claims.get("iss") != f"https://login.microsoftonline.com/{tid}/v2.0" or not (claims.get("oid") or claims.get("sub")):
        raise HTTPException(status_code=401, detail="Connexion Microsoft refusée. Réessayez.")
    return claims


@router.post("/microsoft", response_model=TokenOut)
@limiter.limit("10/minute")
async def microsoft(request: Request, payload: MicrosoftIn, db: AsyncSession = Depends(get_db)):
    if not settings.microsoft_client_id:
        raise HTTPException(status_code=503, detail="La connexion avec Microsoft n'est pas encore configurée.")
    claims = await run_in_threadpool(_verify_microsoft, payload.id_token)
    subject = f"{claims['tid']}:{claims.get('oid') or claims['sub']}"
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    now = datetime.now(timezone.utc)

    identity = (await db.execute(select(OAuthIdentity).where(OAuthIdentity.provider == "microsoft", OAuthIdentity.subject == subject))).scalar_one_or_none()
    if identity:
        broker = await db.get(Broker, identity.broker_id)
        if not broker or broker.status == BrokerStatus.disabled:
            raise HTTPException(status_code=403, detail="Ce compte est désactivé. Contactez-nous.")
    else:
        if "@" not in email:
            raise HTTPException(status_code=400, detail="Votre compte Microsoft ne communique pas d'adresse email. Inscrivez-vous avec votre email.")
        if (await db.execute(select(Broker.id).where(Broker.email == email))).first():
            # Never attach a Microsoft account to an existing FicheAssur account on the email alone.
            raise HTTPException(status_code=409, detail="Un compte FicheAssur existe déjà avec cet email. Connectez-vous avec votre mot de passe ou avec Google.")
        broker = Broker(email=email, full_name=(claims.get("name") or email.split("@")[0])[:120], company="", lead_types=[],
                        status=BrokerStatus.active)
        db.add(broker)
        await db.flush()
        db.add(OAuthIdentity(provider="microsoft", subject=subject, broker_id=broker.id, email=email))
    broker.status = BrokerStatus.active if broker.status != BrokerStatus.disabled else broker.status
    broker.last_login_at = now
    await db.commit()
    await db.refresh(broker)
    return TokenOut(access_token=create_access_token(broker), broker=broker_out(broker))
