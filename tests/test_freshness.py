from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.api.router import measurement_output


def test_measured_weather_freshness_uses_observation_time():
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id="synthetic",
        provider="sensor_community",
        sensor_id="fixture",
        source_record_id="fixture",
        category="WEATHER_CONTEXT",
        parameter="humidity",
        value=50,
        original_value=50,
        unit="%",
        original_unit="%",
        latitude=47.15,
        longitude=27.6,
        observed_at=now - timedelta(hours=5),
        retrieved_at=now,
        quality={},
        provenance={"product": "measurement"},
    )
    assert measurement_output(row)["freshness"] == "stale"
    row.provenance = {"product": "forecast"}
    assert measurement_output(row)["freshness"] == "fresh"
