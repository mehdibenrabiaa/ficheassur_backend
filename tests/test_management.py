import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from app.database import SessionLocal
from app.models.broker import Broker, BrokerRole, BrokerStatus
from app.models.commerce import Order, OrderItem, OrderStatus, Purchase
from app.models.lead import Lead, LeadStatus
from app.security import create_access_token


def account(admin=False):
    async def go():
        async with SessionLocal() as db:
            b = Broker(email=f"{uuid.uuid4().hex}@example.fr", full_name="Gestion test", company="Cabinet Test", role=BrokerRole.admin if admin else BrokerRole.broker, status=BrokerStatus.active)
            db.add(b)
            await db.commit()
            return b.id, {"Authorization": "Bearer " + create_access_token(b)}
    return asyncio.run(go())


def test_management_requires_active_administrator(client):
    _, broker = account()
    for path in ["/api/v1/admin/management/summary", "/api/v1/admin/management/clients", "/api/v1/admin/management/leads", "/api/v1/admin/management/sales", "/api/v1/admin/management/export.csv", "/api/v1/admin/articles"]:
        assert client.get(path).status_code == 401
        assert client.get(path, headers=broker).status_code == 403
    assert client.post('/api/v1/admin/articles', headers=broker, json={}).status_code == 403


def test_editorial_workflow_and_public_visibility(client):
    _, headers = account(True)
    slug = 'guide-' + uuid.uuid4().hex
    body = {"title": "Un guide pour les courtiers", "slug": slug, "excerpt": "Introduction du guide.", "kind": "guide", "status": "draft", "blocks": [{"type": "heading", "text": "Première étape"}, {"type": "paragraph", "text": "Contenu utile aux courtiers."}]}
    r = client.post('/api/v1/admin/articles', headers=headers, json=body)
    assert r.status_code == 201, r.text
    article_id = r.json()['id']
    assert client.get(f'/api/v1/articles/{slug}').status_code == 404
    assert not client.get(f'/api/v1/articles?q={slug}').json()['items']
    assert client.post('/api/v1/admin/articles', headers=headers, json=body).status_code == 409
    assert client.put(f'/api/v1/admin/articles/{article_id}', headers=headers, json={**body, 'status': 'published', 'blocks': []}).status_code == 422
    r = client.put(f'/api/v1/admin/articles/{article_id}', headers=headers, json={**body, 'status': 'published'})
    assert r.status_code == 200 and r.json()['published_at']
    assert client.get(f'/api/v1/articles/{slug}').json()['blocks'] == body['blocks']
    assert any(a['slug'] == slug for a in client.get('/api/v1/articles?kind=guide').json()['items'])
    assert client.put(f'/api/v1/admin/articles/{article_id}', headers=headers, json={**body, 'status': 'published', 'slug': slug + '-change'}).status_code == 409
    assert client.put(f'/api/v1/admin/articles/{article_id}', headers=headers, json=body).status_code == 200
    assert client.get(f'/api/v1/articles/{slug}').status_code == 404
    for invalid in ['javascript:alert(1)', '//tracking.example/a.svg', '/\\tracking.example/a.svg']:
        assert client.put(f'/api/v1/admin/articles/{article_id}', headers=headers, json={**body, 'cover_url': invalid}).status_code == 422
    assert client.get('/api/v1/articles?page=0').status_code == 422


def test_reports_distinguish_clients_sales_and_shared_leads(client):
    admin_id, headers = account(True)
    before = client.get('/api/v1/admin/management/summary', headers=headers).json()
    a_id, _ = account()
    b_id, _ = account()
    prefix = uuid.uuid4().hex
    async def seed():
        async with SessionLocal() as db:
            lead = Lead(first_name='=FORMULA', last_name=prefix, email=f'{prefix}@example.fr', phone='+33612345678', zip_code='75001', city='Paris', insurance_type='AUTO', opt_in_marketing=True)
            db.add(lead)
            await db.flush()
            for broker_id in [a_id, b_id]:
                db.add(Purchase(lead_id=lead.id, broker_id=broker_id, exclusive=False, price_cents=1500, created_at=datetime(2026, 1, 15, 12, tzinfo=timezone.utc)))
            # A pending cart must never count as a sale.
            db.add(Order(broker_id=a_id, total_cents=9999, status=OrderStatus.pending, expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)))
            await db.commit()
            return str(lead.id)
    lead_id = asyncio.run(seed())
    after = client.get('/api/v1/admin/management/summary', headers=headers).json()
    assert after['clients'] == before['clients'] + 2
    assert after['sales'] == before['sales'] + 2
    assert after['unique_leads_sold'] == before['unique_leads_sold'] + 1
    assert after['revenue_cents'] == before['revenue_cents'] + 3000
    clients = client.get('/api/v1/admin/management/clients', headers=headers).json()
    assert all(c['id'] != str(admin_id) for c in clients['items'])
    detail = client.get(f'/api/v1/admin/management/clients/{a_id}', headers=headers).json()
    assert detail['purchases'] == 1 and detail['spent_cents'] == 1500
    r = client.get(f'/api/v1/admin/management/sales?lead_id={lead_id}&since=2026-01-15&until=2026-01-15&limit=1', headers=headers).json()
    assert r['total'] == 2 and len(r['items']) == 1 and r['amount_cents'] == 3000
    r = client.get(f'/api/v1/admin/management/sales?client_id={a_id}', headers=headers).json()
    assert r['total'] == 1 and all(s['client_id'] == str(a_id) for s in r['items'])
    assert client.get(f'/api/v1/admin/management/clients/{admin_id}', headers=headers).status_code == 404
    assert client.get('/api/v1/admin/management/sales?since=2026-02-01&until=2026-01-01', headers=headers).status_code == 422
    exported = client.get(f'/api/v1/admin/management/export.csv?lead_id={lead_id}', headers=headers)
    assert exported.status_code == 200 and "'=FORMULA" in exported.text and '15.00' in exported.text


def test_inventory_handles_holds_age_withdrawals_and_exclusivity(client):
    _, headers = account(True)
    broker_id, _ = account()
    prefix = uuid.uuid4().hex
    async def seed():
        async with SessionLocal() as db:
            leads = {}
            for state in ['available', 'reserved', 'sold_out', 'expired', 'withdrawn']:
                l = Lead(first_name=state, last_name=prefix, email=f'{state}-{prefix}@example.fr', phone='+33612345678', zip_code='75001', city='Paris', insurance_type='AUTO', opt_in_marketing=True, status=LeadStatus.REJECTED if state == 'withdrawn' else LeadStatus.NEW)
                if state == 'expired':
                    l.created_at = datetime.now(timezone.utc) - timedelta(days=31)
                db.add(l)
                await db.flush()
                leads[state] = l
            db.add(Purchase(lead_id=leads['sold_out'].id, broker_id=broker_id, exclusive=True, price_cents=4000))
            order = Order(broker_id=broker_id, total_cents=4000, status=OrderStatus.pending, expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
            db.add(order)
            await db.flush()
            db.add(OrderItem(order_id=order.id, lead_id=leads['reserved'].id, exclusive=True, price_cents=4000, label='Réservation'))
            await db.commit()
    asyncio.run(seed())
    data = client.get(f'/api/v1/admin/management/leads?q={prefix}', headers=headers).json()
    assert data['total'] == 5
    assert {l['name'].split()[0]: l['availability'] for l in data['items']} == {k: k for k in ['available', 'reserved', 'sold_out', 'expired', 'withdrawn']}
