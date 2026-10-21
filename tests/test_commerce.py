"""Browse the marketplace -> check out (simulated card payment) -> work the
lead in the CRM; holds while another broker is paying; shared/exclusive limits."""
import asyncio
import uuid

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models.lead import Lead
from app.pricing import MAX_SHARED_BUYERS, PRICES_CENTS


@pytest.fixture(autouse=True)
def simulated_payments(monkeypatch):
    monkeypatch.setattr(settings, "payments_simulated", True)
    monkeypatch.setattr(settings, "stripe_secret_key", "")


def make_lead(insurance_type="AUTO", zip_code="75011"):
    lead_id = uuid.uuid4()

    async def go():
        async with SessionLocal() as db:
            db.add(Lead(
                id=lead_id, insurance_type=insurance_type, zip_code=zip_code, city="Paris", first_name="Camille",
                last_name="Martin", email=f"{lead_id.hex[:8]}@exemple.fr", phone="+33612345678",
                details={"age": "26-40", "phone": "+33612345678"}, opt_in_marketing=True,
            ))
            await db.commit()
    asyncio.run(go())
    return str(lead_id)


def signup(client):
    r = client.post("/api/v1/auth/register", json={
        "full_name": "Test Courtier", "company": "Cabinet T", "email": f"c{uuid.uuid4().hex[:8]}@exemple.fr",
        "phone": "0612345678", "orias_number": None, "lead_types": ["AUTO"], "password": "mot-de-passe",
    })
    assert r.status_code == 201
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def buy(client, h, lead_id, exclusive=False):
    return client.post("/api/v1/marketplace/checkout", json={"items": [{"lead_id": lead_id, "exclusive": exclusive}]}, headers=h)


def test_buy_and_work_a_lead(client):
    h = signup(client)
    lead_id = make_lead()
    shared, _ = PRICES_CENTS["AUTO"]

    # Listed with contact details hidden.
    listed = next(l for l in client.get("/api/v1/marketplace/leads", headers=h).json() if l["id"] == lead_id)
    assert listed["first_name_initial"] == "C." and "phone" not in listed["details"] and listed["exclusive_available"]

    # Paid at checkout (simulated): delivered, gone from this broker's marketplace, in the order history.
    r = buy(client, h, lead_id)
    assert r.status_code == 200 and r.json()["status"] == "paid"
    assert all(l["id"] != lead_id for l in client.get("/api/v1/marketplace/leads", headers=h).json())
    assert buy(client, h, lead_id).status_code == 409
    orders = client.get("/api/v1/billing", headers=h).json()["orders"]
    assert len(orders) == 1 and orders[0]["total_cents"] == shared and "partagé" in orders[0]["items"][0]["label"]

    # In the CRM with full contact details; status, reminder and note recorded in the history.
    crm = client.get("/api/v1/crm/leads", headers=h).json()
    assert len(crm) == 1 and crm[0]["phone"] == "+33612345678" and crm[0]["status"] == "new"
    pid = crm[0]["id"]
    assert client.patch(f"/api/v1/crm/leads/{pid}", json={"status": "contacted", "reminder_at": "2020-01-01T09:00:00Z"}, headers=h).status_code == 200
    assert client.post(f"/api/v1/crm/leads/{pid}/notes", json={"body": "Rappeler après 18 h"}, headers=h).status_code == 201
    detail = client.get(f"/api/v1/crm/leads/{pid}", headers=h).json()
    assert detail["status"] == "contacted" and detail["notes"][0]["body"] == "Rappeler après 18 h"
    assert {e["kind"] for e in detail["events"]} == {"purchased", "status", "reminder", "note"}

    summary = client.get("/api/v1/crm/summary", headers=h).json()
    assert summary["counts"]["contacted"] == 1 and len(summary["reminders_due"]) == 1 and summary["spent_month_cents"] == shared

    csv = client.get("/api/v1/crm/export.csv", headers=h)
    assert csv.status_code == 200 and "Camille" in csv.text and "Contacté" in csv.text

    # Another broker can't see this broker's CRM lead.
    assert client.get(f"/api/v1/crm/leads/{pid}", headers=signup(client)).status_code == 404


def test_hold_while_another_broker_pays(client, monkeypatch):
    a, b = signup(client), signup(client)
    lead_id = make_lead()

    # Broker A starts paying for it as exclusive (order left pending: no Stripe here, so stop before payment).
    monkeypatch.setattr(settings, "payments_simulated", False)
    monkeypatch.setattr("app.api.v1.marketplace.use_stripe", lambda: None)
    monkeypatch.setattr("app.api.v1.marketplace.customer_id", lambda db, broker: _async("cus_test"))
    monkeypatch.setattr("stripe.checkout.Session.create", lambda **kw: type("S", (), {"id": f"cs_{uuid.uuid4().hex}", "url": "https://stripe.test/pay"})())
    r = buy(client, a, lead_id, exclusive=True)
    assert r.status_code == 200 and r.json()["status"] == "redirect"
    order_id = r.json()["order_id"]

    # Held: hidden from B, and B can't buy it.
    monkeypatch.setattr(settings, "payments_simulated", True)
    assert all(l["id"] != lead_id for l in client.get("/api/v1/marketplace/leads", headers=b).json())
    assert buy(client, b, lead_id).status_code == 409

    # A comes back without paying: released for B.
    assert client.post(f"/api/v1/marketplace/orders/{order_id}/cancel", headers=a).status_code == 204
    assert buy(client, b, lead_id).status_code == 200


async def _async(value):
    return value


def test_shared_and_exclusive_limits(client):
    brokers = [signup(client) for _ in range(MAX_SHARED_BUYERS + 1)]

    # Exclusive: one buyer, then gone for everyone.
    excl = make_lead()
    assert buy(client, brokers[0], excl, exclusive=True).status_code == 200
    assert buy(client, brokers[1], excl).status_code == 409

    # Shared: up to MAX_SHARED_BUYERS, and no exclusive once someone bought it.
    shared = make_lead()
    for h in brokers[:MAX_SHARED_BUYERS]:
        assert buy(client, h, shared).status_code == 200
    assert buy(client, brokers[-1], shared).status_code == 409

    half = make_lead()
    buy(client, brokers[0], half)
    assert buy(client, brokers[1], half, exclusive=True).status_code == 409
