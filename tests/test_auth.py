"""Partner request -> admin accepts -> activation link -> password -> login."""
import asyncio
import uuid

from app.database import SessionLocal
from app.models.broker import Broker, BrokerRole, BrokerStatus
from app.security import hash_password


def make_admin(email):
    async def go():
        async with SessionLocal() as db:
            db.add(Broker(email=email, full_name="Admin", company="FicheAssur", role=BrokerRole.admin,
                          status=BrokerStatus.active, password_hash=hash_password("admin-pass-123")))
            await db.commit()
    asyncio.run(go())


def login(client, email, password):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def test_partner_request_to_active_broker(client):
    suffix = uuid.uuid4().hex[:6]
    admin_email, broker_email = f"admin{suffix}@exemple.fr", f"courtier{suffix}@exemple.fr"
    make_admin(admin_email)
    admin_h = {"Authorization": "Bearer " + login(client, admin_email, "admin-pass-123").json()["access_token"]}

    r = client.post("/api/v1/access-requests", json={
        "full_name": "Jean Courtier", "company": "Cabinet JC", "email": broker_email, "phone": "0612345678",
        "orias_number": "12345678", "lead_types": ["AUTO", "MOTO"], "message": None,
    })
    assert r.status_code == 201
    req = next(x for x in client.get("/api/v1/admin/access-requests", headers=admin_h).json() if x["email"] == broker_email)
    assert req["broker_status"] is None

    # Not activated yet: no login.
    link = client.post(f"/api/v1/admin/access-requests/{req['id']}/accept", headers=admin_h).json()
    token = link["activation_url"].split("token=")[1]
    assert login(client, broker_email, "whatever-123").status_code == 401

    # Activate with the link: signed in, link can't be reused.
    act = client.post("/api/v1/auth/activate", json={"token": token, "password": "mon-mot-de-passe"})
    assert act.status_code == 200 and act.json()["broker"]["role"] == "broker"
    assert client.post("/api/v1/auth/activate", json={"token": token, "password": "autre-pass-123"}).status_code == 400

    # Normal login from now on; /me works; admin routes don't.
    tok = login(client, broker_email, "mon-mot-de-passe").json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    me = client.get("/api/v1/auth/me", headers=h).json()
    assert me["company"] == "Cabinet JC" and me["lead_types"] == ["AUTO", "MOTO"]
    assert client.get("/api/v1/admin/brokers", headers=h).status_code == 403
    assert login(client, broker_email, "mauvais-pass").status_code == 401

    # Accepting again once active is refused.
    assert client.post(f"/api/v1/admin/access-requests/{req['id']}/accept", headers=admin_h).status_code == 409


def test_unauthenticated_and_bad_tokens_are_rejected(client):
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/v1/admin/access-requests").status_code == 401


def test_self_signup_gives_immediate_access(client):
    email = f"inscrit{uuid.uuid4().hex[:6]}@exemple.fr"
    body = {"full_name": "Léa Courtier", "company": "Cabinet LC", "email": email, "phone": "0612345678",
            "orias_number": "", "lead_types": ["AUTO"], "password": "mon-mot-de-passe"}
    r = client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201 and r.json()["broker"]["role"] == "broker"

    # Signed in straight away, and can log in normally afterwards.
    h = {"Authorization": "Bearer " + r.json()["access_token"]}
    assert client.get("/api/v1/auth/me", headers=h).json()["email"] == email
    assert login(client, email, "mon-mot-de-passe").status_code == 200

    # Same email twice, or a short password, is refused.
    assert client.post("/api/v1/auth/register", json=body).status_code == 409
    assert client.post("/api/v1/auth/register", json={**body, "email": "x" + email, "password": "court"}).status_code == 422
