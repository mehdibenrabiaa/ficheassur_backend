"""Continuer avec Microsoft: Microsoft's token check is faked, the rest is real."""
import uuid

from fastapi import HTTPException

import app.api.v1.auth as auth
from app.config import settings
from tests.test_auth import make_admin  # noqa: F401  (shared helpers)


def fake_ms(tid="tenant-a", oid=None, email=None, name="Marc Courtier"):
    oid = oid or uuid.uuid4().hex
    def verify(token):
        if token.startswith("bad"):
            raise HTTPException(status_code=401, detail="Connexion Microsoft refusée. Réessayez.")
        return {"tid": tid, "oid": oid, "preferred_username": email, "name": name}
    return verify


def post(client, token="eyJ.fake.token.long-enough"):
    return client.post("/api/v1/auth/microsoft", json={"id_token": token})


def test_microsoft_sign_in(client, monkeypatch):
    monkeypatch.setattr(settings, "microsoft_client_id", "")
    assert post(client).status_code == 503
    monkeypatch.setattr(settings, "microsoft_client_id", "client-id")

    email = f"ms{uuid.uuid4().hex[:6]}@cabinet.fr"
    monkeypatch.setattr(auth, "_verify_microsoft", fake_ms(oid="user-1", email=email))
    assert post(client, "bad.token.long-enough-x").status_code == 401
    first = post(client)
    assert first.status_code == 200 and first.json()["broker"]["email"] == email
    # Same Microsoft account again: same FicheAssur account.
    assert post(client).json()["broker"]["id"] == first.json()["broker"]["id"]

    # Another Microsoft account claiming that email: refused, not attached to the existing account.
    monkeypatch.setattr(auth, "_verify_microsoft", fake_ms(tid="evil-tenant", oid="attacker", email=email))
    r = post(client)
    assert r.status_code == 409 and "existe déjà" in r.json()["detail"]

    # No email at all: refused with a clear message.
    monkeypatch.setattr(auth, "_verify_microsoft", fake_ms(oid="no-mail", email=None))
    assert post(client).status_code == 400
