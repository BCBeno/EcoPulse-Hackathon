"""Synthetic CAMS forecasts used only to verify RNMCA classification and future-route integration."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from test_avoidance import request_body, routing_fixture  # noqa: F401

from app.core.config import Settings
from app.db.session import Session
from app.models.entities import Observation
from app.services.air_quality import classify, route_air_quality, trip_forecast

ROUTE = {"duration_s": 1800, "geometry": {"coordinates": [[27.6, 47.15], [27.6, 47.19]]}}


def forecast(parameter, value, observed_at, **changes):
    return NS(
        **{
            "category": "MODEL_FORECAST",
            "parameter": parameter,
            "value": value,
            "observed_at": observed_at,
            "retrieved_at": datetime.now(UTC),
            "latitude": 47.17,
            "longitude": 27.6,
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("parameter", "value", "code", "label"),
    [
        ("pm25", 4.9, 1, "Bun"),
        ("pm25", 5.0, 2, "Acceptabil"),
        ("pm25", 49.9, 3, "Moderat"),
        ("pm10", 150, 4, "Rău"),
        ("no2", 120, 5, "Foarte rău"),
        ("pm25", 500, 6, "Extrem de rău"),
    ],
)
def test_rnmca_bands(parameter, value, code, label):
    result = classify(parameter, value)
    assert (result["code"], result["label"]) == (code, label)
    assert result["recommendation"]


def test_unclassified_pollutants_have_no_rnmca_index():
    assert classify("o3", 40) is None
    assert classify("pm25", None) is None


def test_trip_uses_worst_hour_inside_window_only():
    departure = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    rows = [
        forecast("pm25", 8, departure),
        forecast("pm25", 60, departure + timedelta(hours=1)),
        forecast("pm25", 200, departure + timedelta(hours=5)),
        forecast("no2", 12, departure),
    ]
    result = trip_forecast(rows, departure, departure + timedelta(minutes=70), 47.17, 27.6)
    assert result["pollutants"]["pm25"]["max"] == 60
    assert result["index"]["code"] == 4
    assert result["dominant_pollutant"] == "pm25"
    assert len(result["hourly"]) == 2


def test_stale_or_distant_forecasts_are_ignored():
    departure = datetime.now(UTC) + timedelta(hours=3)
    rows = [
        forecast("pm25", 60, departure, retrieved_at=datetime.now(UTC) - timedelta(hours=8)),
        forecast("pm25", 60, departure, latitude=46.5),
        forecast("pm25", 60, departure, category="OFFICIAL_MEASUREMENT"),
    ]
    assert trip_forecast(rows, departure, departure, 47.17, 27.6) is None
    assert route_air_quality(ROUTE, departure, rows, "cached") == {
        "available": False,
        "status": "no_forecast_for_window",
    }


def test_future_route_fetches_live_cams_when_cache_is_empty(client, routing_fixture):  # noqa: F811
    departure = datetime.now(UTC) + timedelta(days=1)
    result = client.post("/api/v1/routes/alternatives", json=request_body(departure_at=departure.isoformat()))
    assert result.status_code == 200, result.text
    body = result.json()
    assert routing_fixture["air_calls"] == 1
    assert body["air_quality_forecast"]["status"] == "live"
    air = body["routes"][0]["air_quality"]
    assert air["available"] and air["index"]["label"] == "Acceptabil"
    assert air["pollutants"]["pm25"]["max"] == 8.0
    assert air["pollutants"]["european_aqi"]["unit"] == "EAQI"
    # A live fetch is shown as forecast context only; it does not alter the environmental score.
    assert body["routes"][0]["environmental_score"] is None


def test_cached_cams_rows_avoid_live_request(client, routing_fixture):  # noqa: F811
    departure = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=2)

    async def seed():
        async with Session() as db:
            db.add(
                Observation(
                    id="synthetic-cams",
                    provider="cams",
                    source_record_id="synthetic",
                    sensor_id="grid:47.17:27.6",
                    parameter="pm25",
                    original_value=95,
                    original_unit="μg/m³",
                    value=95,
                    unit="µg/m³",
                    observed_at=departure,
                    retrieved_at=datetime.now(UTC),
                    latitude=47.17,
                    longitude=27.6,
                    category="MODEL_FORECAST",
                    quality={},
                    provenance={},
                )
            )
            await db.commit()

    client.portal.call(seed)
    body = client.post(
        "/api/v1/routes/alternatives", json=request_body(departure_at=departure.isoformat())
    ).json()
    assert routing_fixture["air_calls"] == 0
    assert body["air_quality_forecast"]["status"] == "cached"
    assert body["routes"][0]["air_quality"]["index"]["label"] == "Foarte rău"
    city = client.get(
        "/api/v1/environment/air-quality",
        params={"start": departure.isoformat(), "minutes": 60},
    ).json()
    assert city["available"] and city["index"]["code"] == 5


def test_departure_beyond_horizon_or_provider_failure_is_not_clean_air(client, routing_fixture):  # noqa: F811
    far = datetime.now(UTC) + timedelta(days=Settings(_env_file=None).forecast_days, hours=2)
    body = client.post("/api/v1/routes/alternatives", json=request_body(departure_at=far.isoformat())).json()
    assert routing_fixture["air_calls"] == 0
    assert body["routes"][0]["air_quality"] == {"available": False, "status": "beyond_forecast_horizon"}
    assert any("horizon" in w for w in body["warnings"])
    routing_fixture["cams"] = "failure"
    soon = datetime.now(UTC) + timedelta(hours=6)
    body = client.post("/api/v1/routes/alternatives", json=request_body(departure_at=soon.isoformat())).json()
    assert body["air_quality_forecast"]["status"] == "unavailable"
    assert body["routes"][0]["air_quality"]["available"] is False


def test_city_forecast_falls_back_to_live_cams_for_frontend(client, routing_fixture):  # noqa: F811
    start = datetime.now(UTC) + timedelta(hours=20)
    city = client.get(
        "/api/v1/environment/air-quality", params={"start": start.isoformat(), "minutes": 120}
    ).json()
    assert routing_fixture["air_calls"] == 1
    assert city["status"] == "live" and city["index"]["label"] == "Acceptabil"
    far = datetime.now(UTC) + timedelta(days=10)
    city = client.get("/api/v1/environment/air-quality", params={"start": far.isoformat()}).json()
    assert city == {**city, "available": False, "status": "beyond_forecast_horizon"}
    assert routing_fixture["air_calls"] == 1
