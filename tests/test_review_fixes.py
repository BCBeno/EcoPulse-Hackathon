"""Regression tests for the backend/frontend integration review."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.session import Session
from app.models.entities import MappedFeature, Observation, PlannedWork
from app.services.quick_report import deterministic
from app.services.sources import SourceService

ENDPOINT = "/api/v1/reports/from-text"


def body(**changes):
    return {
        "text": "groapa mare pe carosabil, periculoasa pt biciclisti",
        "latitude": 47.16,
        "longitude": 27.59,
        **changes,
    }


@pytest.mark.parametrize(
    ("text", "category", "severity"),
    [
        # Feminine "periculoasă" used to miss the severity keyword.
        ("groapa mare pe carosabil, periculoasa pt biciclisti", "infrastructure", 4),
        # Congestion, not an obstacle.
        ("trafic blocat pe Podu Ros", "traffic", 2),
        # Only "fum" remained after the preamble, which raised a validation error (HTTP 500).
        ("raportez ca - fum", "smoke", 2),
    ],
)
def test_deterministic_review_cases(text, category, severity):
    result = deterministic(text)
    assert (result.category, result.severity) == (category, severity)
    assert len(result.description) >= 5


def test_from_text_rejects_unusable_input_without_server_error(client, account):
    headers = account()
    for text in ("ok", "-----", "....", "fum!"):
        response = client.post(ENDPOINT, headers=headers, json=body(text=text))
        assert response.status_code == 422, (text, response.text)
    assert client.post(ENDPOINT, headers=headers, json=body(text="raportez ca - fum")).status_code == 201
    assert client.post(ENDPOINT, headers=headers, json=body(accuracy_m=5000)).status_code == 422
    assert client.post(ENDPOINT, headers=headers, json=body(accuracy_m=40)).status_code == 201


def test_config_exposes_resolve_threshold(client):
    assert client.get("/api/v1/config").json()["report_resolve_disputes"] == 3


def test_display_name_is_stripped_before_length_check(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "spaces@example.com", "password": "fixture-password-123", "display_name": "  a  "},
    )
    assert response.status_code == 422


def observation(observed_at, sensor="s1"):
    return Observation(
        id=f"{sensor}:{observed_at.isoformat()}",
        provider="sensor_community",
        source_record_id=sensor,
        sensor_id=sensor,
        parameter="pm25",
        value=10,
        original_value=10,
        original_unit="µg/m³",
        unit="µg/m³",
        observed_at=observed_at,
        retrieved_at=datetime.now(UTC),
        latitude=47.15,
        longitude=27.6,
        category="LOW_COST_SENSOR_MEASUREMENT",
        quality={},
        provenance={},
    )


def test_observations_time_filters(client):
    now = datetime.now(UTC)

    async def seed():
        async with Session() as db:
            db.add_all(
                [observation(now - timedelta(hours=5), "old"), observation(now - timedelta(minutes=5), "new")]
            )
            await db.commit()

    client.portal.call(seed)
    since = (now - timedelta(hours=2)).isoformat()
    items = client.get("/api/v1/environment/observations", params={"since": since}).json()["items"]
    assert [x["sensor_id"] for x in items] == ["new"]
    until = (now - timedelta(hours=2)).isoformat()
    items = client.get("/api/v1/environment/observations", params={"until": until}).json()["items"]
    assert [x["sensor_id"] for x in items] == ["old"]
    assert client.get("/api/v1/environment/observations?since=2026-01-01T00:00:00").status_code == 422


def test_planned_works_only_returns_current_located_works(client):
    now = datetime.now(UTC)

    def work(source_id, **changes):
        values = {
            "source_id": source_id,
            "title": source_id,
            "description": "",
            "original_url": "https://politialocala-iasi.ro/x",
            "retrieved_at": now,
            "last_verified_at": now,
            "starts_at": now - timedelta(days=1),
            "ends_at": now + timedelta(days=1),
            "latitude": 47.16,
            "longitude": 27.59,
            "verification": "verified",
            "status": "active",
            **changes,
        }
        return PlannedWork(**values)

    async def seed():
        async with Session() as db:
            db.add_all(
                [
                    work("current"),
                    work("cancelled", status="cancelled"),
                    work("finished", ends_at=now - timedelta(hours=1)),
                    work("unlocated", latitude=None, longitude=None),
                    work("unreviewed", verification="needs_review"),
                ]
            )
            await db.commit()

    client.portal.call(seed)
    assert [x["title"] for x in client.get("/api/v1/disruptions/planned").json()["items"]] == ["current"]


def test_osm_sync_removes_features_deleted_upstream(client):
    def feature(source_id):
        return {
            "source_id": source_id,
            "latitude": 47.16,
            "longitude": 27.59,
            "tags": {"landuse": "construction"},
            "geometry": {"type": "Point", "coordinates": [27.59, 47.16]},
            "retrieved_at": datetime.now(UTC),
            "provenance": {},
        }

    async def run():
        service = SourceService(Settings(_env_file=None))
        batches = iter([[feature("way/1"), feature("way/2")], [feature("way/2")]])

        class FixtureProvider:
            async def fetch(self):
                return next(batches)

        service.providers["osm"] = FixtureProvider()
        try:
            await service.sync("osm")
            await service.sync("osm")
            async with Session() as db:
                assert (await db.scalars(select(MappedFeature.source_id))).all() == ["way/2"]
        finally:
            await service.http.close()

    client.portal.call(run)
