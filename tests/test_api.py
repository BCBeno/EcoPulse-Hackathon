from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import get_settings
from app.core.security import password_hash
from app.db.session import Session
from app.models.entities import User


def report_payload(**updates):
    return {
        "category": "dust",
        "description": "Labelled test fixture: visible dust.",
        "latitude": 47.158,
        "longitude": 27.601,
        "observed_at": datetime.now(UTC).isoformat(),
        **updates,
    }


def route_payload(**updates):
    return {
        "origin": {"latitude": 47.158, "longitude": 27.601},
        "destination": {"latitude": 47.18, "longitude": 27.57},
        "mode": "walk",
        "departure_at": datetime.now(UTC).isoformat(),
        **updates,
    }


def test_health_config_openapi(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/api/v1/health/ready").status_code == 200
    config = client.get("/api/v1/config").json()
    assert config["timezone"] == "Europe/Bucharest"
    assert "openaq_api_key" not in str(config)
    assert "/api/v1/reports" in client.get("/openapi.json").json()["paths"]


def test_authentication_validation(client, account):
    headers = account()
    assert client.get("/api/v1/auth/me", headers=headers).json()["display_name"] == "Fixture User 1"
    assert client.get("/api/v1/auth/me").status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "user1@example.com", "password": "incorrect"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "missing@example.com", "password": "incorrect"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/register",
            json={
                "email": "user1@example.com",
                "password": "long-fixture-password",
                "display_name": "Duplicate",
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/auth/register", json={"email": "bad", "password": "short", "display_name": "x"}
        ).status_code
        == 422
    )


def test_password_hash_and_expired_jwt(client, account):
    account()

    async def stored():
        from sqlalchemy import select

        async with Session() as db:
            return (await db.scalar(select(User))).password_hash

    hashed = client.portal.call(stored)
    assert hashed != "fixture-password-123" and password_hash.verify("fixture-password-123", hashed)
    token = jwt.encode(
        {
            "sub": "x",
            "iat": datetime.now(UTC) - timedelta(hours=2),
            "exp": datetime.now(UTC) - timedelta(hours=1),
            "iss": "ecopulse",
        },
        get_settings().jwt_secret_key,
        algorithm="HS256",
    )
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer malformed"}).status_code == 401


def test_reports_geography_and_validation(client, account):
    headers = account()
    assert client.post("/api/v1/reports", json=report_payload()).status_code == 401
    response = client.post("/api/v1/reports", headers=headers, json=report_payload())
    assert response.status_code == 201, response.text
    report = response.json()
    assert "creator_id" not in report
    assert client.get("/api/v1/reports?bbox=47.1,27.5,47.2,27.7").json()["items"][0]["id"] == report["id"]
    assert not client.get("/api/v1/reports?bbox=48,27,48.1,27.1").json()["items"]
    assert client.get("/api/v1/reports?bbox=broken").status_code == 422
    assert (
        client.post("/api/v1/reports", headers=headers, json=report_payload(latitude=91)).status_code == 422
    )
    assert (
        client.post("/api/v1/reports", headers=headers, json=report_payload(latitude=45)).status_code == 422
    )
    assert (
        client.post(
            "/api/v1/reports", headers=headers, json=report_payload(observed_at="2026-01-01T10:00:00")
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/reports",
            headers=headers,
            json=report_payload(observed_at=(datetime.now(UTC) + timedelta(days=1)).isoformat()),
        ).status_code
        == 422
    )


def test_votes_unique_and_resolve_authorization(client, account):
    author, voter, voter2 = account(1), account(2), account(3)
    report = client.post("/api/v1/reports", headers=author, json=report_payload()).json()
    path = f"/api/v1/reports/{report['id']}"
    assert client.post(path + "/confirm", headers=author).status_code == 403
    assert client.post(path + "/confirm", headers=voter).json()["confirmations"] == 1
    assert client.post(path + "/confirm", headers=voter).status_code == 409
    assert client.post(path + "/dispute", headers=voter).status_code == 409
    assert client.post(path + "/confirm", headers=voter2).json()["status"] == "confirmed"
    assert client.post(path + "/resolve", headers=voter).status_code == 403
    assert client.post(path + "/flag", headers=voter).status_code == 200
    assert client.post(path + "/resolve", headers=author).json()["status"] == "resolved"
    assert client.post(path + "/dispute", headers=voter2).status_code == 409


def test_disputes_from_other_users_resolve_report(client, account):
    author, *voters = (account(i) for i in range(1, 5))
    report = client.post("/api/v1/reports", headers=author, json=report_payload()).json()
    path = f"/api/v1/reports/{report['id']}"
    assert client.post(path + "/dispute", headers=voters[0]).json()["status"] == "pending"
    assert client.post(path + "/dispute", headers=voters[1]).json()["status"] == "pending"
    assert client.post(path + "/dispute", headers=voters[2]).json()["status"] == "resolved"
    assert all(item["id"] != report["id"] for item in client.get("/api/v1/reports").json()["items"])


def test_expired_report_not_active(client, account):
    headers = account()
    report = client.post(
        "/api/v1/reports",
        headers=headers,
        json=report_payload(
            category="traffic", observed_at=(datetime.now(UTC) - timedelta(hours=3)).isoformat()
        ),
    ).json()
    assert report["status"] == "expired"
    assert client.get("/api/v1/reports").json()["items"] == []
    assert len(client.get("/api/v1/reports?status=expired").json()["items"]) == 1


def test_journey_ownership_and_crud(client, account):
    one, two = account(), account(2)
    payload = {"name": "Fixture journey", "request": route_payload()}
    response = client.post("/api/v1/journeys", headers=one, json=payload)
    assert response.status_code == 201, response.text
    path = f"/api/v1/journeys/{response.json()['id']}"
    assert client.get(path, headers=two).status_code == 404
    assert client.put(path, headers=two, json=payload).status_code == 404
    assert client.delete(path, headers=two).status_code == 404
    assert client.get("/api/v1/journeys", headers=two).json()["items"] == []
    assert client.put(path, headers=one, json={**payload, "name": "Changed"}).json()["name"] == "Changed"
    assert client.delete(path, headers=one).status_code == 204
    assert client.get(path, headers=one).status_code == 404


def test_admin_private_and_moderation(client, account):
    headers = account()
    assert client.get("/api/v1/admin/data-sources/status").status_code == 401
    assert client.get("/api/v1/admin/data-sources/status", headers=headers).status_code == 403

    async def promote():
        from sqlalchemy import select

        async with Session() as db:
            user = await db.scalar(select(User))
            user.role = "admin"
            await db.commit()

    client.portal.call(promote)
    assert len(client.get("/api/v1/admin/data-sources/status", headers=headers).json()["items"]) == 6
    report = client.post("/api/v1/reports", headers=headers, json=report_payload()).json()
    assert client.post(f"/api/v1/admin/reports/{report['id']}/hide", headers=headers).status_code == 200
    assert client.get(f"/api/v1/reports/{report['id']}").status_code == 404


def test_route_invalid_or_missing_configuration(client):
    assert (
        client.post("/api/v1/routes/alternatives", json=route_payload(mode="helicopter")).status_code == 422
    )
    assert (
        client.post(
            "/api/v1/routes/alternatives", json=route_payload(departure_at="2026-10-17T09:00:00")
        ).status_code
        == 422
    )
    assert (
        client.post("/api/v1/routes/alternatives", json=route_payload()).json()["code"]
        == "missing_VALHALLA_BASE_URL"
    )
    assert (
        client.post("/api/v1/routes/alternatives", json=route_payload(mode="motorcycle")).json()["code"]
        == "motorcycle_beta_profile_disabled"
    )


def test_assistant_fallback_and_ownership(client, account):
    one, two = account(), account(2)
    result = client.post("/api/v1/assistant/conversations", headers=one)
    assert result.status_code == 201
    path = f"/api/v1/assistant/conversations/{result.json()['id']}"
    assert client.get(path, headers=two).status_code == 404
    assert client.post(path + "/messages", headers=two, json={"content": "test"}).status_code == 404
    response = client.post(
        path + "/messages", headers=one, json={"content": "Compară rutele", "route_request": route_payload()}
    )
    assert response.status_code == 200, response.text
    assert response.json()["engine"] == "deterministic" and response.json()["evidence"] == []
    assert len(client.get(path, headers=one).json()["messages"]) == 2


def test_auth_rate_limit(client):
    for _ in range(10):
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": "missing@example.com", "password": "bad"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "missing@example.com", "password": "bad"}
        ).status_code
        == 429
    )
