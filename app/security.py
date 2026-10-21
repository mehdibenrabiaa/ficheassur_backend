"""Espace pro authentication: password hashing, login tokens, the current
broker dependency. Standard library scrypt for passwords (no extra
dependency), PyJWT for short-lived bearer tokens."""

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.broker import Broker, BrokerRole, BrokerStatus

TOKEN_TTL = timedelta(hours=12)
ACTIVATION_TTL = timedelta(days=7)
_ALGO = "HS256"
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 64}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt_b64, digest_b64 = stored.split("$")
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def new_activation_token() -> tuple[str, str]:
    """(token for the link, sha256 hash to store)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_version(broker: Broker) -> str:
    """A fingerprint of the current password, put in every login token: when
    the password changes, every token issued before stops working -- so a
    stolen session can't outlive a password change (and no extra column)."""
    return hmac.new(settings.secret_key.encode(), (broker.password_hash or "no-password").encode(), "sha256").hexdigest()[:16]


def create_access_token(broker: Broker) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(broker.id), "role": broker.role.value, "iat": now, "exp": now + TOKEN_TTL, "scope": "espace-pro",
        "pv": password_version(broker),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGO)


async def current_broker(authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> Broker:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Connexion requise.")
    try:
        payload = jwt.decode(authorization.removeprefix("Bearer ").strip(), settings.secret_key, algorithms=[_ALGO])
        if payload.get("scope") != "espace-pro":
            raise jwt.InvalidTokenError()
        broker = await db.get(Broker, uuid.UUID(payload["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(status_code=401, detail="Session expirée, merci de vous reconnecter.")
    if not broker or broker.status != BrokerStatus.active or not hmac.compare_digest(str(payload.get("pv", "")), password_version(broker)):
        raise HTTPException(status_code=401, detail="Session expirée, merci de vous reconnecter.")
    return broker


async def current_admin(broker: Broker = Depends(current_broker)) -> Broker:
    if broker.role != BrokerRole.admin:
        raise HTTPException(status_code=403, detail="Réservé à l'administrateur FicheAssur.")
    return broker
