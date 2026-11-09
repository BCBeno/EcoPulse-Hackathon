"""Integration tests run against a real PostGIS database (deploy/compose.dev.yaml, database ecopulse_test)."""

import os
from collections.abc import AsyncIterator

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import ROOT, Settings
from app.main import create_app
from app.models import Base
from app.security import RateLimiter

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://ecopulse:ecopulse@localhost:5433/ecopulse_test"
)
ADMIN_KEY = "test-admin-key"


def make_settings(**overrides) -> Settings:
    values = {
        "app_env": "test",
        "database_url": TEST_DATABASE_URL,
        "admin_api_key": ADMIN_KEY,
        "log_level": "WARNING",
    }
    return Settings(_env_file=None, **(values | overrides))


@pytest.fixture(scope="session")
def migrated() -> None:
    """Rebuild the test schema from the migrations once per run."""
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = TEST_DATABASE_URL
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return make_settings()


@pytest.fixture(scope="session")
async def app(migrated, settings):
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture(autouse=True)
async def clean_state(request) -> AsyncIterator[None]:
    yield
    if "app" not in request.fixturenames:
        return
    application = request.getfixturevalue("app")
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with application.state.engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} CASCADE"))
    application.state.limiter = RateLimiter()


@pytest.fixture
async def session(app):
    async with app.state.sessionmaker() as db:
        yield db


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as http:
        yield http


async def new_device(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post("/devices")
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture
async def alice(client) -> dict[str, str]:
    return await new_device(client)


@pytest.fixture
async def bob(client) -> dict[str, str]:
    return await new_device(client)
