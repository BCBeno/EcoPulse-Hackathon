"""Ingestion storage and the air endpoints against PostGIS, with seeded (not live) data."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.air.ingest import Ingestor, SourceNotConfigured, cleanup, store_forecasts, store_osm, store_readings
from app.air.models import Measurement, Sensor, SourceRun
from app.air.providers.records import ForecastValue, OsmRecord, StationReading
from app.air.service import floor_hour, refresh_now_grid
from app.http import SourceError

CENTER = (47.1585, 27.6014)


def reading(external_id: str, parameter: str, value: float, minutes_ago: int = 5, lat=47.16, lon=27.59):
    return StationReading(
        provider="sensor_community",
        external_id=external_id,
        kind="low_cost",
        lat=lat,
        lon=lon,
        parameter=parameter,
        observed_at=datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=minutes_ago),
        value=value,
        meta={"sensor_type": "SDS011"},
    )


def lattice_forecasts(pm25: float, hours: int = 30) -> list[ForecastValue]:
    start = floor_hour(datetime.now(UTC)) - timedelta(hours=1)
    values = []
    for h in range(hours):
        for lat in (47.0, 47.1, 47.2, 47.3):
            for lon in (27.3, 27.4, 27.5, 27.6, 27.7, 27.8):
                # Cleaner air in the afternoon of the forecast, to give the best window something to find.
                level = pm25 if h < 10 else 4.0
                values.append(ForecastValue(lat, lon, "pm25", start + timedelta(hours=h), level))
                values.append(ForecastValue(lat, lon, "no2", start + timedelta(hours=h), 8.0))
    return values


@pytest.fixture
async def seeded(session, settings):
    now = datetime.now(UTC)
    await store_forecasts(session, lattice_forecasts(20.0), now)
    await store_readings(
        session,
        [
            reading("100", "pm25", 45.0),
            reading("100", "pm10", 60.0),
            reading("200", "pm25", 18.0, lat=47.19, lon=27.55),
        ],
        now,
    )
    await store_osm(
        session,
        [OsmRecord("way/1", "construction", "Pasaj", {"type": "Point", "coordinates": [27.6, 47.16]}, {})],
        now,
    )
    await session.commit()
    await refresh_now_grid(session, settings)


async def test_store_readings_is_idempotent(session):
    now = datetime.now(UTC)
    batch = [reading("1", "pm25", 10.0), reading("1", "pm10", 12.0)]
    await store_readings(session, batch, now)
    await store_readings(session, [*batch, reading("1", "pm25", 11.0, minutes_ago=0)], now)
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(Sensor)) == 1
    assert await session.scalar(select(func.count()).select_from(Measurement)) == 3


async def test_cleanup_drops_old_measurements(session):
    now = datetime.now(UTC)
    await store_readings(
        session, [reading("1", "pm25", 10.0), reading("1", "pm25", 9.0, minutes_ago=8 * 24 * 60)], now
    )
    await session.commit()
    await cleanup(session, now)
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(Measurement)) == 1


async def test_ingestor_records_failures_and_skips(app, settings):
    ingestor = Ingestor(app.state.sessionmaker, http=None, settings=settings)

    async def failing(session, now):
        raise SourceError("http_502")

    async def unconfigured(session, now):
        raise SourceNotConfigured

    failed = await ingestor.run("cams", failing)
    skipped = await ingestor.run("openaq", unconfigured)
    assert (failed.status, failed.error) == ("failed", "http_502")
    assert (skipped.status, skipped.error) == ("skipped", "not_configured")
    async with app.state.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(SourceRun)) == 2


async def test_map_combines_grid_sensors_reports_and_construction(client, seeded, alice):
    created = await client.post(
        "/reports",
        json={"category": "smoke", "description": "Fum gros.", "location": {"lat": 47.16, "lon": 27.59}},
        headers=alice,
    )
    assert created.status_code == 201
    response = await client.get("/map")
    assert response.status_code == 200
    body = response.json()
    grid = body["grid"]
    assert len(grid["layers"]["eaqi"]) == grid["rows"] * grid["cols"]
    assert set(grid["layers"]) == {"eaqi", "pm25", "confidence"}
    assert all(isinstance(v, int) for v in grid["layers"]["eaqi"] if v is not None)
    assert {s["readings"]["pm25"]["value"] for s in body["sensors"]} == {45.0, 18.0}
    assert [r["id"] for r in body["reports"]] == [created.json()["id"]]
    assert body["construction"][0]["geometry"] == {"type": "Point", "coordinates": [27.6, 47.16]}
    assert body["kind"] == "now"

    again = await client.get("/map", headers={"If-None-Match": response.headers["etag"]})
    assert again.status_code == 304


async def test_map_forecast_and_horizon(client, seeded):
    later = (datetime.now(UTC) + timedelta(hours=12)).isoformat()
    body = (await client.get("/map", params={"at": later})).json()
    assert body["kind"] == "forecast"
    too_far = (datetime.now(UTC) + timedelta(hours=100)).isoformat()
    response = await client.get("/map", params={"at": too_far})
    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["field"] == "at"


async def test_air_point_near_polluting_station(client, seeded):
    body = (await client.get("/air/point", params={"lat": 47.16, "lon": 27.59})).json()
    assert body["pollutants"]["pm25"]["value"] > 30  # pulled up from the CAMS background of 20
    assert body["eaqi"] == {"level": 3, "dominant": "pm25"}
    assert body["pollutants"]["no2"]["source"] == "model"
    assert body["nearest_sensors"][0]["distance_m"] < 50
    assert body["confidence"] > 0.9


async def test_air_point_outside_city(client, seeded):
    response = await client.get("/air/point", params={"lat": 44.43, "lon": 26.10})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "outside_city"


async def test_air_forecast_finds_cleaner_hours(client, seeded):
    body = (await client.get("/air/forecast", params={"lat": 47.19, "lon": 27.70, "hours": 24})).json()
    assert len(body["hourly"]) == 24
    assert body["best_window"]["eaqi_level"] == 1
    assert body["hourly"][0]["pm25"] == pytest.approx(20.0, abs=1)
