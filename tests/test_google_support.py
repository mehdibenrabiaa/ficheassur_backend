"""Continuer avec Google: Google's answers are faked, the rest is real."""
import uuid

import app.api.v1.auth as auth
from app.config import settings

CLIENT_ID = "test-client.apps.googleusercontent.com"


def fake_google(email, aud=CLIENT_ID, verified="true", name="Jeanne Google"):
    def get(url, access_token=None):
        if "tokeninfo" in url:
            return {"aud": aud, "email": email, "email_verified": verified}
        return {"name": name}
    return get


def test_google_sign_in(client, monkeypatch):
    email = f"g{uuid.uuid4().hex[:6]}@exemple.fr"
    post = lambda: client.post("/api/v1/auth/google", json={"access_token": "ya29.fake-token"})

    monkeypatch.setattr(settings, "google_client_id", "")
    assert post().status_code == 503  # not configured

    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(auth, "_google_get", fake_google(email, aud="someone-elses-app"))
    assert post().status_code == 401  # token issued to another site
    monkeypatch.setattr(auth, "_google_get", fake_google(email, verified="false"))
    assert post().status_code == 401

    monkeypatch.setattr(auth, "_google_get", fake_google(email.upper()))
    first = post()
    assert first.status_code == 200
    assert first.json()["broker"]["email"] == email and first.json()["broker"]["full_name"] == "Jeanne Google"
    again = post()  # same email: the same account, not a second one
    assert again.json()["broker"]["id"] == first.json()["broker"]["id"]
    h = {"Authorization": "Bearer " + again.json()["access_token"]}
    assert client.get("/api/v1/auth/me", headers=h).status_code == 200

    # No password yet: setting one needs a fresh Google sign-in to the same email, not just the session.
    new = {"new_password": "mot-de-passe-1"}
    assert client.post("/api/v1/auth/password", json=new, headers=h).status_code == 400
    monkeypatch.setattr(auth, "_google_get", fake_google("quelquun.dautre@exemple.fr"))
    assert client.post("/api/v1/auth/password", json={**new, "google_access_token": "ya29.x"}, headers=h).status_code == 400
    monkeypatch.setattr(auth, "_google_get", fake_google(email))
    assert client.post("/api/v1/auth/password", json={**new, "google_access_token": "ya29.x"}, headers=h).status_code == 200


def test_support_form_and_admin_inbox(client):
    from tests.test_auth import login, make_admin
    r = client.post("/api/v1/support", json={"full_name": "Paul", "email": "paul@exemple.fr", "topic": "paiement", "message": "Ma facture n'apparaît pas."})
    assert r.status_code == 201
    assert client.post("/api/v1/support", json={"full_name": "Paul", "email": "paul@exemple.fr", "topic": "x", "message": "trop court"}).status_code == 422
    assert client.get("/api/v1/support").status_code == 401  # admins only
    email = f"admin{uuid.uuid4().hex[:6]}@exemple.fr"
    make_admin(email)
    h = {"Authorization": "Bearer " + login(client, email, "admin-pass-123").json()["access_token"]}
    msg = next(m for m in client.get("/api/v1/support", headers=h).json() if m["email"] == "paul@exemple.fr")
    assert msg["topic"] == "paiement" and not msg["resolved"] and not msg["from_broker"]
    assert client.post(f"/api/v1/support/{msg['id']}/resolve", headers=h).json()["resolved"] is True
