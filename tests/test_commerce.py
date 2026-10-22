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

    # Portfolio pages: reminders, notes (searchable) and activity across leads.
    rem = client.get("/api/v1/crm/reminders", headers=h).json()
    assert len(rem) == 1 and rem[0]["lead"]["purchase_id"] == pid and rem[0]["lead"]["name"].startswith("Camille")
    notes = client.get("/api/v1/crm/notes", headers=h).json()
    assert [n["body"] for n in notes["items"]] == ["Rappeler après 18 h"] and notes["total"] == 1 and notes["next_cursor"] is None
    assert client.get("/api/v1/crm/notes?q=18 h", headers=h).json()["items"][0]["lead"]["purchase_id"] == pid
    assert client.get("/api/v1/crm/notes?q=introuvable", headers=h).json()["items"] == []
    events = client.get("/api/v1/crm/activity", headers=h).json()
    assert {e["kind"] for e in events["items"]} == {"purchased", "status", "reminder", "note"} and events["total"] == 4
    assert [e["kind"] for e in client.get("/api/v1/crm/activity?kind=note", headers=h).json()["items"]] == ["note"]

    # Many notes: pages of `limit`, each row exactly once, newest first.
    for i in range(7):
        client.post(f"/api/v1/crm/leads/{pid}/notes", json={"body": f"Note {i}"}, headers=h)
    seen, cursor = [], None
    while True:
        page = client.get("/api/v1/crm/notes", params={"limit": 3, **({"cursor": cursor} if cursor else {})}, headers=h).json()
        assert page["total"] == 8 and len(page["items"]) <= 3
        seen += [n["id"] for n in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 8

    # Another broker can't see this broker's CRM lead -- nor its reminders, notes or activity.
    other = signup(client)
    assert client.get(f"/api/v1/crm/leads/{pid}", headers=other).status_code == 404
    assert client.get("/api/v1/crm/leads", headers=other).json() == []
    assert client.get("/api/v1/crm/reminders", headers=other).json() == []
    for path in ("notes", "activity"):
        assert client.get(f"/api/v1/crm/{path}", headers=other).json()["items"] == []
    for path in ("reminders", "notes", "activity"):
        assert client.get(f"/api/v1/crm/{path}").status_code == 401


def test_hold_while_another_broker_pays(client, monkeypatch):
    a, b = signup(client), signup(client)
    lead_id = make_lead()

    # Broker A starts paying for it as exclusive (order left pending: no Stripe here, so stop before payment).
    monkeypatch.setattr(settings, "payments_simulated", False)
    monkeypatch.setattr("app.api.v1.marketplace.use_stripe", lambda: None)
    monkeypatch.setattr("app.api.v1.marketplace.customer_id", lambda db, broker: _async("cus_test"))
    monkeypatch.setattr("stripe.checkout.Session.create", lambda **kw: type("S", (), {"id": f"cs_{uuid.uuid4().hex}", "client_secret": "cs_secret_test"})())
    r = buy(client, a, lead_id, exclusive=True)
    assert r.status_code == 200 and r.json()["status"] == "embedded" and r.json()["client_secret"] == "cs_secret_test"
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


def test_payment_page_order_view(client):
    a, b = signup(client), signup(client)
    lead_id = make_lead()
    r = buy(client, a, lead_id)  # simulated payment: paid at once
    order_id = r.json()["order_id"]
    view = client.get(f"/api/v1/marketplace/orders/{order_id}", headers=a).json()
    assert view["status"] == "paid" and view["client_secret"] is None
    assert len(view["items"]) == 1 and view["items"][0]["purchase_id"]
    # The purchase link opens the lead in the buyer's CRM.
    assert client.get(f"/api/v1/crm/leads/{view['items'][0]['purchase_id']}", headers=a).status_code == 200
    # Someone else's order: not found.
    assert client.get(f"/api/v1/marketplace/orders/{order_id}", headers=b).status_code == 404


def test_marketplace_hides_contact_details():
    from app.api.v1.marketplace import _public_details
    shown = _public_details({
        "Véhicule": "SUV", "Sinistres (3 ans)": "1", "Âge du conducteur": "26-40 ans", "Cylindrée": "750 cm³",
        "Téléphone portable": "06 12 34 56 78", "E-mail": "a@b.fr", "Nom de famille": "Martin", "Plaque": "AB-123-CD",
        "Commentaire": "rappelez-moi", "Précisions": "appelez le 0612345678", "Site": "www.exemple.fr",
        "Ville": "Lyon 69003", "Voiture": "AB-123-CD", "Autre": {"x": 1}, "Nom": "X", "Tél.": "x",
        "Nombre de véhicules": "2 à 5", "Chiffre d'affaires": "200 k€ – 1 M€", "Deux-roues": "Trail",
    })
    assert shown == {"Véhicule": "SUV", "Sinistres (3 ans)": "1", "Âge du conducteur": "26-40 ans", "Cylindrée": "750 cm³",
                     "Nombre de véhicules": "2 à 5", "Chiffre d'affaires": "200 k€ – 1 M€", "Deux-roues": "Trail"}



def test_revenue_and_finance(client):
    h = signup(client)
    lead_id = make_lead()
    buy(client, h, lead_id)
    pid = client.get("/api/v1/crm/leads", headers=h).json()[0]["id"]
    price = client.get("/api/v1/crm/leads", headers=h).json()[0]["price_cents"]

    r = client.post(f"/api/v1/crm/leads/{pid}/revenues", json={"amount_cents": 30000, "label": "Commission", "invoiced_on": "2026-10-02"}, headers=h)
    assert r.status_code == 201
    client.post(f"/api/v1/crm/leads/{pid}/revenues", json={"amount_cents": 5000, "label": "Frais de courtage", "invoiced_on": "2026-10-03"}, headers=h)
    assert client.post(f"/api/v1/crm/leads/{pid}/revenues", json={"amount_cents": 0, "label": "x", "invoiced_on": "2026-10-03"}, headers=h).status_code == 422

    detail = client.get(f"/api/v1/crm/leads/{pid}", headers=h).json()
    assert sum(x["amount_cents"] for x in detail["revenues"]) == 35000 and any(e["kind"] == "revenue" for e in detail["events"])

    f = client.get("/api/v1/crm/finance", headers=h).json()
    assert f["totals"] == {"spent_cents": price, "revenue_cents": 35000, "leads": 1, "leads_with_revenue": 1, "expenses_cents": 0}
    assert len(f["months"]) == 12 and f["top_leads"][0]["revenue_cents"] == 35000

    # Another broker: can't add to, delete from, or see this broker's finances.
    other = signup(client)
    assert client.post(f"/api/v1/crm/leads/{pid}/revenues", json={"amount_cents": 100, "label": "x", "invoiced_on": "2026-10-03"}, headers=other).status_code == 404
    assert client.delete(f"/api/v1/crm/revenues/{r.json()['id']}", headers=other).status_code == 404
    assert client.get("/api/v1/crm/finance", headers=other).json()["totals"]["revenue_cents"] == 0

    assert client.delete(f"/api/v1/crm/revenues/{r.json()['id']}", headers=h).status_code == 204
    assert client.get("/api/v1/crm/finance", headers=h).json()["totals"]["revenue_cents"] == 5000


def test_exclusive_sale_removes_lead_for_everyone_else(client):
    a, b = signup(client), signup(client)
    lead_id = make_lead()
    listed = lambda h: [l["id"] for l in client.get("/api/v1/marketplace/leads", headers=h).json()]
    assert lead_id in listed(b)

    assert buy(client, a, lead_id, exclusive=True).json()["status"] == "paid"
    # Gone from the other broker's marketplace, and buying it anyway is refused (shared or exclusive).
    assert lead_id not in listed(b)
    assert buy(client, b, lead_id).status_code == 409
    assert buy(client, b, lead_id, exclusive=True).status_code == 409


def test_shared_lead_caps_buyers_and_blocks_exclusive(client):
    from app.pricing import MAX_SHARED_BUYERS
    lead_id = make_lead()
    first = signup(client)
    buy(client, first, lead_id)
    # Once anyone has it shared, nobody can take it exclusive.
    other = signup(client)
    item = next(l for l in client.get("/api/v1/marketplace/leads", headers=other).json() if l["id"] == lead_id)
    assert item["exclusive_available"] is False
    assert buy(client, other, lead_id, exclusive=True).status_code == 409
    for _ in range(MAX_SHARED_BUYERS - 1):
        assert buy(client, signup(client), lead_id).json()["status"] == "paid"
    # Full: hidden for the next broker.
    late = signup(client)
    assert lead_id not in [l["id"] for l in client.get("/api/v1/marketplace/leads", headers=late).json()]
    assert buy(client, late, lead_id).status_code == 409



def test_general_expenses_in_finance(client):
    from datetime import date
    from app.api.v1.crm import _occurrences
    from app.models.commerce import BrokerExpense

    # Occurrences: monthly every month, yearly on its anniversary, one-off once, stopped at end_on.
    jan, dec = date(2026, 1, 1), date(2026, 12, 31)
    monthly = BrokerExpense(amount_cents=3000, frequency="monthly", start_on=date(2026, 3, 15), end_on=date(2026, 8, 1))
    assert [m for m, _ in _occurrences(monthly, jan, dec)] == ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
    yearly = BrokerExpense(amount_cents=12000, frequency="yearly", start_on=date(2025, 6, 1), end_on=None)
    assert list(_occurrences(yearly, jan, dec)) == [("2026-06", 12000)]
    once = BrokerExpense(amount_cents=500, frequency="once", start_on=date(2026, 2, 3), end_on=None)
    assert list(_occurrences(once, jan, dec)) == [("2026-02", 500)] and list(_occurrences(once, date(2026, 3, 1), dec)) == []

    h = signup(client)
    today = date.today()
    e = client.post("/api/v1/crm/expenses", json={"label": "Forfait mobile", "category": "phone", "amount_cents": 2999, "frequency": "monthly", "start_on": today.replace(day=1).isoformat()}, headers=h)
    assert e.status_code == 201 and e.json()["monthly_cents"] == 2999
    assert client.post("/api/v1/crm/expenses", json={"label": "x", "category": "loisirs", "amount_cents": 1, "frequency": "monthly", "start_on": "2026-01-01"}, headers=h).status_code == 422
    f = client.get("/api/v1/crm/finance", headers=h).json()
    assert f["totals"]["expenses_cents"] == 2999 and f["months"][-1]["expenses_cents"] == 2999

    # Only the owner sees, stops or deletes it.
    other = signup(client)
    assert client.get("/api/v1/crm/expenses", headers=other).json() == []
    assert client.delete(f"/api/v1/crm/expenses/{e.json()['id']}", headers=other).status_code == 404
    assert client.patch(f"/api/v1/crm/expenses/{e.json()['id']}", json={"end_on": today.isoformat()}, headers=h).json()["end_on"] == today.isoformat()
    assert client.delete(f"/api/v1/crm/expenses/{e.json()['id']}", headers=h).status_code == 204
    assert client.get("/api/v1/crm/finance", headers=h).json()["totals"]["expenses_cents"] == 0



def test_finance_periods(client):
    """Each amount on its own date: a commission invoiced in another month
    counts in that month, not in the month the lead was bought."""
    from datetime import date
    from datetime import datetime
    from zoneinfo import ZoneInfo
    h = signup(client)
    buy(client, h, make_lead())
    pid = client.get("/api/v1/crm/leads", headers=h).json()[0]["id"]
    today = datetime.now(ZoneInfo("Europe/Paris")).date()  # the backend counts days in Paris time
    this_month = today.replace(day=1)
    client.post(f"/api/v1/crm/leads/{pid}/revenues", json={"amount_cents": 20000, "label": "Commission", "invoiced_on": today.isoformat()}, headers=h)
    client.post(f"/api/v1/crm/leads/{pid}/revenues", json={"amount_cents": 7000, "label": "Ancienne", "invoiced_on": "2020-03-10"}, headers=h)

    month = client.get("/api/v1/crm/finance", params={"since": this_month.isoformat(), "until": today.isoformat()}, headers=h).json()
    assert month["totals"]["revenue_cents"] == 20000 and month["totals"]["leads"] == 1
    assert len(month["months"]) == 12 and month["months"][-1]["month"] == f"{today:%Y-%m}"  # one month: 12-month trend

    march = client.get("/api/v1/crm/finance", params={"since": "2020-03-01", "until": "2020-03-31"}, headers=h).json()
    assert march["totals"]["revenue_cents"] == 7000 and march["totals"]["spent_cents"] == 0 and march["totals"]["leads"] == 0

    custom = client.get("/api/v1/crm/finance", params={"since": "2020-01-01", "until": "2020-06-30"}, headers=h).json()
    assert [m["month"] for m in custom["months"]] == ["2020-01", "2020-02", "2020-03", "2020-04", "2020-05", "2020-06"]
    assert client.get("/api/v1/crm/finance", params={"since": "2020-06-30", "until": "2020-01-01"}, headers=h).status_code == 422
    assert client.get("/api/v1/crm/finance", headers=h).json()["totals"]["revenue_cents"] == 27000



def test_finance_by_market(client):
    from datetime import date
    h = signup(client)
    buy(client, h, make_lead())  # make_lead: an AUTO lead
    pid = client.get("/api/v1/crm/leads", headers=h).json()[0]["id"]
    client.post(f"/api/v1/crm/leads/{pid}/revenues", json={"amount_cents": 10000, "label": "Commission", "invoiced_on": date.today().isoformat()}, headers=h)
    client.post("/api/v1/crm/expenses", json={"label": "Box", "category": "internet", "amount_cents": 3000, "frequency": "once", "start_on": date.today().isoformat()}, headers=h)

    auto = client.get("/api/v1/crm/finance", params={"market": "AUTO"}, headers=h).json()["totals"]
    assert auto["revenue_cents"] == 10000 and auto["leads"] == 1 and auto["expenses_cents"] == 0  # frais: no market
    moto = client.get("/api/v1/crm/finance", params={"market": "MOTO"}, headers=h).json()["totals"]
    assert moto["revenue_cents"] == 0 and moto["leads"] == 0
    assert client.get("/api/v1/crm/finance", headers=h).json()["totals"]["expenses_cents"] == 3000
    assert client.get("/api/v1/crm/finance", params={"market": "SANTE"}, headers=h).status_code == 422
