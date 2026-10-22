"""NWC -> FicheAssur: list, update, withdraw a lead; key and sale rules."""
import uuid
from datetime import datetime, timedelta, timezone

from app.config import settings
from tests.test_commerce import buy, signup

KEY = "test-import-key"


def payload(**over):
    now = datetime.now(timezone.utc)
    return {
        "insurance_type": "AUTO", "first_name": "Nadia", "last_name": "Amrani", "email": "nadia@exemple.fr",
        "phone": "06 11 22 33 44", "zip_code": "69003", "city": "Lyon", "details": {"Véhicule": "SUV", "Bonus-malus": "0,50"},
        "requested_at": (now - timedelta(days=2)).isoformat(), "partner_consent_at": (now - timedelta(days=2)).isoformat(), **over,
    }


def test_import_flow(client, monkeypatch):
    monkeypatch.setattr(settings, "import_api_key", KEY)
    monkeypatch.setattr(settings, "payments_simulated", True)
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    ref = f"nwc-{uuid.uuid4().hex[:6]}"
    url = f"/api/v1/imports/leads/{ref}"
    h = {"X-Import-Key": KEY}

    assert client.put(url, json=payload()).status_code == 401
    assert client.put(url, json=payload(), headers={"X-Import-Key": "wrong"}).status_code == 401
    assert client.put(url, json=payload(zip_code="ABC"), headers=h).status_code == 422
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    assert client.put(url, json=payload(requested_at=old), headers=h).status_code == 422

    r = client.put(url, json=payload(), headers=h)
    assert r.status_code == 200 and r.json()["status"] == "on_sale"
    lead_id = r.json()["lead_id"]
    # Same ref again: updated, not duplicated.
    assert client.put(url, json=payload(city="Villeurbanne"), headers=h).json()["lead_id"] == lead_id

    broker = signup(client)
    market = {l["id"]: l for l in client.get("/api/v1/marketplace/leads", headers=broker).json()}
    assert lead_id in market and market[lead_id]["city"] == "Villeurbanne" and "Nadia" not in str(market[lead_id])

    # Withdrawn: gone from the marketplace; listed again: back.
    assert client.delete(url, headers=h).json()["status"] == "withdrawn"
    assert lead_id not in [l["id"] for l in client.get("/api/v1/marketplace/leads", headers=broker).json()]
    assert client.put(url, json=payload(), headers=h).json()["status"] == "on_sale"

    # Bought: can't be changed or withdrawn any more.
    assert buy(client, broker, lead_id).json()["status"] == "paid"
    assert client.get(url, headers=h).json()["status"] == "sold"
    assert client.delete(url, headers=h).status_code == 409
    assert client.put(url, json=payload(), headers=h).status_code == 409
