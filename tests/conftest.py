import os

os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
os.environ["APP_ENV"] = "test"
os.environ["VALHALLA_BASE_URL"] = ""
os.environ["OPENAQ_API_KEY"] = ""
os.environ["LLM_PROVIDER"] = ""
os.environ["TRAFFIC_ROADS_CACHE"] = ""
os.environ["TOMTOM_API_KEY"] = ""
os.environ["JWT_SECRET_KEY"] = "test-only-signing-secret-with-more-than-forty-characters"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.dependencies import auth_limit, geocoding_limit, routing_limit, write_limit  # noqa: E402
from app.db.session import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client():
    for limiter in (auth_limit, geocoding_limit, routing_limit, write_limit):
        limiter.buckets.clear()
    with TestClient(app) as client:
        yield client

        async def cleanup():
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.drop_all)

        client.portal.call(cleanup)


@pytest.fixture
def account(client):
    def make(index=1):
        credentials = {"email": f"user{index}@example.com", "password": "fixture-password-123"}
        response = client.post(
            "/api/v1/auth/register", json={**credentials, "display_name": f"Fixture User {index}"}
        )
        assert response.status_code == 201, response.text
        login = client.post("/api/v1/auth/login", json=credentials)
        assert login.status_code == 200, login.text
        return {"Authorization": f"Bearer {login.json()['access_token']}"}

    return make
