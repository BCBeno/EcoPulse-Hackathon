from uuid import UUID

from sqlalchemy import select

from app.devices.models import Device
from app.security import hash_token


async def test_create_device_returns_token_and_stores_only_its_hash(client, session):
    response = await client.post("/devices")
    assert response.status_code == 201
    body = response.json()
    assert len(body["token"]) >= 40
    device = await session.scalar(select(Device).where(Device.id == UUID(body["device_id"])))
    assert device.token_hash == hash_token(body["token"])
    assert body["token"] not in device.token_hash


async def test_me_returns_device_and_quota(client, alice):
    response = await client.get("/devices/me", headers=alice)
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "user"
    assert body["agent_quota"]["limit"] == 30
    assert body["agent_quota"]["used"] == 0
    assert body["agent_quota"]["resets_at"].endswith("Z") or "+00:00" in body["agent_quota"]["resets_at"]


async def test_me_requires_token(client):
    response = await client.get("/devices/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_unknown_token_is_rejected(client):
    response = await client.get("/devices/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Sesiunea nu mai este validă."


async def test_device_creation_is_rate_limited_per_ip(client, app, settings):
    for _ in range(settings.devices_per_hour_per_ip):
        assert (await client.post("/devices")).status_code == 201
    response = await client.post("/devices")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert int(response.headers["retry-after"]) > 0
