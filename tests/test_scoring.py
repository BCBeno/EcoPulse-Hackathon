"""Synthetic geometry and observations used only to verify deterministic business rules."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from app.services.scoring import EnvironmentalRouteScoringService


@pytest.fixture
def scorer():
    return EnvironmentalRouteScoringService()


def route():
    return {
        "id": "synthetic-route",
        "duration_s": 300,
        "distance_m": 1000,
        "geometry": {"type": "LineString", "coordinates": [[27.60, 47.15], [27.61, 47.16]]},
    }


def observation(**changes):
    return NS(
        **{
            "id": "synthetic-observation",
            "provider": "fixture",
            "sensor_id": "synthetic-sensor",
            "canonical_sensor_id": None,
            "parameter": "pm25",
            "value": 30,
            "unit": "µg/m³",
            "category": "OFFICIAL_MEASUREMENT",
            "observed_at": datetime.now(UTC),
            "retrieved_at": datetime.now(UTC),
            "latitude": 47.15,
            "longitude": 27.60,
            "provenance": {},
            **changes,
        }
    )


def report(**changes):
    now = datetime.now(UTC)
    return NS(
        **{
            "id": "synthetic-report",
            "creator_id": "fixture-author",
            "status": "confirmed",
            "observed_at": now - timedelta(minutes=10),
            "expires_at": now + timedelta(hours=2),
            "latitude": 47.15,
            "longitude": 27.60,
            "severity": 2,
            "description": "Synthetic fixture report",
            **changes,
        }
    )


def test_missing_data_is_not_zero_or_clean(scorer):
    result = scorer.score(route(), datetime.now(UTC))
    assert result["environmental_score"] is None and result["confidence"] == "low"
    assert not result["evidence"]


def test_deterministic_score_and_sensor_dedup(scorer):
    now = datetime.now(UTC)
    one = observation()
    first = scorer.score(route(), now, [one])
    repeated = scorer.score(route(), now, [one, one])
    assert first == repeated and len(repeated["evidence"]) == 1
    assert first["environmental_score"] == 50
    assert first["confidence"] == "low"


def test_cross_provider_alias_dedup(scorer):
    now = datetime.now(UTC)
    one = observation(canonical_sensor_id="verified-physical-station")
    two = deepcopy(one)
    two.id = "copy"
    two.provider = "another_fixture"
    result = scorer.score(route(), now, [one, two])
    assert len(result["evidence"]) == 1
    assert result["confidence"] == "low"


def test_same_station_pollutants_are_not_independent(scorer):
    one = observation(provenance={"location_id": "synthetic-location"})
    two = observation(
        id="second",
        sensor_id="second-pollutant-sensor",
        parameter="pm10",
        provenance={"location_id": "synthetic-location"},
    )
    result = scorer.score(route(), datetime.now(UTC), [one, two])
    assert len(result["evidence"]) == 2 and result["confidence"] == "low"


@pytest.mark.parametrize(
    "changes",
    [
        {"value": None},
        {"unit": "ppm"},
        {"observed_at": datetime.now(UTC) - timedelta(hours=4)},
        {"latitude": 48},
        {"observed_at": datetime.now(UTC) + timedelta(hours=4)},
    ],
)
def test_invalid_stale_or_distant_data_excluded(scorer, changes):
    assert scorer.score(route(), datetime.now(UTC), [observation(**changes)])["environmental_score"] is None


def test_future_journey_does_not_reuse_current_measurement(scorer):
    assert not scorer.score(route(), datetime.now(UTC) + timedelta(days=1), [observation()])["evidence"]


def test_model_forecast_is_baseline_only(scorer):
    now = datetime.now(UTC)
    forecast = observation(category="MODEL_FORECAST", observed_at=now + timedelta(days=1))
    first = scorer.score(route(), now + timedelta(days=1), [forecast])
    second = scorer.score({**route(), "id": "second", "duration_s": 350}, now + timedelta(days=1), [forecast])
    results = scorer.compare([first, second])
    assert first["confidence"] == "low"
    assert all("lower_exposure" not in r["perspectives"] for r in results)
    assert "regional" in " ".join(first["warnings"])


def test_stale_forecast_and_horizon(scorer):
    now = datetime.now(UTC)
    stale = observation(category="MODEL_FORECAST", retrieved_at=now - timedelta(hours=8))
    assert not scorer.score(route(), now, [stale])["evidence"]
    forecast = observation(category="MODEL_FORECAST")
    assert not scorer.score(route(), now + timedelta(days=5), [forecast])["evidence"]


def test_report_penalty_never_dilutes_pollution(scorer):
    now = datetime.now(UTC)
    base = scorer.score(route(), now, [observation()])
    with_report = scorer.score(route(), now, [observation()], reports=[report(severity=1)])
    assert with_report["environmental_score"] < base["environmental_score"]
    expired = report(expires_at=now - timedelta(minutes=1))
    assert not scorer.score(route(), now, reports=[expired])["evidence"]


def test_mapped_construction_is_not_official_work(scorer):
    now = datetime.now(UTC)
    feature = NS(
        id="fixture-feature",
        retrieved_at=now,
        geometry={"type": "Point", "coordinates": [27.60, 47.15]},
        latitude=47.15,
        longitude=27.60,
        provenance={},
    )
    result = scorer.score(route(), now, features=[feature])
    assert result["evidence"][0]["category"] == "OSM_MAPPED_FEATURE"
    assert result["confidence"] == "low"
    assert "closure unverified" in result["evidence"][0]["description"]


def test_works_need_review_coordinates_and_known_dates(scorer):
    now = datetime.now(UTC)
    work = NS(
        id="fixture-work",
        source_id="fixture-work",
        title="Synthetic work",
        original_url="https://fixture.invalid",
        verification="needs_review",
        status="scheduled",
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(hours=1),
        last_verified_at=now,
        latitude=47.15,
        longitude=27.60,
    )
    assert not scorer.score(route(), now, works=[work])["evidence"]
    work.verification = "verified"
    assert scorer.score(route(), now, works=[work])["evidence"][0]["category"] == "OFFICIAL_PLANNED_WORK"
    work.ends_at = None
    assert not scorer.score(route(), now, works=[work])["evidence"]


def test_weather_is_context_without_pollution_score(scorer):
    context = observation(category="WEATHER_CONTEXT", parameter="wind_speed_10m", value=5, unit="m/s")
    result = scorer.score(route(), datetime.now(UTC), weather=[context])
    assert result["environmental_score"] is None and result["weather_context"][0]["value"] == 5


def test_comparison_additional_time_and_real_alternatives(scorer):
    now = datetime.now(UTC)
    fast = scorer.score(route(), now, [observation(value=80)])
    slower = scorer.score({**route(), "id": "slower", "duration_s": 400}, now, [observation(value=10)])
    result = scorer.compare([fast, slower])
    assert result[0]["perspectives"] == ["fastest"]
    assert "lower_exposure" in result[1]["perspectives"]
    assert result[1]["additional_time_s"] == 100
