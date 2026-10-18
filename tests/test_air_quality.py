"""All concentrations below are synthetic test fixtures."""

import math
import time
from datetime import UTC, datetime

import httpx
import pytest

from app.core.config import Settings
from app.integrations.http import ProviderError, ProviderHTTP
from app.services.air_quality_service import AirQualityService


@pytest.fixture
def settings():
    return Settings(_env_file=None)


def ts(*args):
    return int(datetime(*args, tzinfo=UTC).timestamp())


def payload():
    # Romania leaves summer time on 2026-10-25 at 01:00 UTC: 03:00 local happens twice.
    times = [ts(2026, 10, 25, 0), ts(2026, 10, 25, 1), ts(2026, 10, 25, 2)]
    return {
        "latitude": 47.2,
        "longitude": 27.6,
        "utc_offset_seconds": 10800,
        "timezone": "Europe/Bucharest",
        "current": {
            "time": times[0],
            "interval": 3600,
            "european_aqi": 41,
            "pm10": 20.0,
            "pm2_5": 16.0,
            "nitrogen_dioxide": 4.2,
            "ozone": 36.0,
            "sulphur_dioxide": 0.5,
        },
        "hourly": {
            "time": times,
            "pm10": [9.9, 130.0, None],
            "pm2_5": [7.7, 4.0, None],
            "nitrogen_dioxide": [30.0, 2.0, None],
            "ozone": [32.0, 31.0, None],
            "sulphur_dioxide": [0.4, 0.4, None],
            "european_aqi": [19, 60],
        },
    }


@pytest.mark.parametrize(
    ("pollutant", "value", "code"),
    [
        ("PM2.5", 0, 1),
        ("PM2.5", 4.99, 1),
        ("PM2.5", 5, 2),
        ("PM2.5", 15, 3),
        ("PM2.5", 50, 4),
        ("PM2.5", 90, 5),
        ("PM2.5", 140, 6),
        ("PM2.5", 10_000, 6),
        ("PM10", 44.9, 2),
        ("PM10", 270, 6),
        ("NO2", 25, 3),
        ("NO2", 150, 6),
        ("PM2.5", None, 0),
        ("PM2.5", -1, 0),
        ("PM2.5", math.nan, 0),
        ("PM2.5", math.inf, 0),
        ("PM2.5", True, 0),
        ("PM2.5", "12", 0),
        ("O3", 59.9, 1),
        ("O3", 120, 4),
        ("O3", 180, 6),
        ("SO2", 39.9, 2),
        ("SO2", 125, 4),
        ("CO", 10, 0),
    ],
)
def test_badge_thresholds(pollutant, value, code):
    badge = AirQualityService.get_aqi_badge(pollutant, value)
    assert badge["code"] == code
    assert (badge["recommendation"] is None) == (code == 0)


def test_badge_labels_match_rnmca_scale():
    labels = [AirQualityService.get_aqi_badge("PM2.5", v)["label"] for v in (1, 10, 20, 60, 100, 200)]
    assert labels == ["Bun", "Acceptabil", "Moderat", "Rău", "Foarte rău", "Extrem de rău"]
    assert AirQualityService.get_aqi_badge("PM2.5", None)["label"] == "Necunoscut"


def test_parse_current_and_overall_index():
    data = AirQualityService.parse(payload())
    current = data["current"]
    assert data["city"] == "Iasi" and data["timezone"] == "Europe/Bucharest"
    assert current["pm2_5"] == {
        "value": 16.0,
        "unit": "µg/m³",
        "code": 3,
        "label": "Moderat",
        "recommendation": "Poluare ușoară. Persoanele extrem de sensibile pot resimți iritații.",
    }
    assert current["pm10"]["code"] == 2 and current["no2"]["code"] == 1
    assert current["ozone"] == {"value": 36.0, "unit": "µg/m³"}
    assert current["european_aqi"] == 41
    assert current["rnmca"]["code"] == 3 and current["rnmca"]["pollutant"] == "pm2_5"
    assert current["alert"] is True
    assert current["timestamp"] == "2026-10-25T03:00"
    assert current["timestamp_utc"] == "2026-10-25T00:00:00Z"


def test_parse_forecast_handles_dst_nulls_and_short_series():
    hours = AirQualityService.parse(payload())["forecast_hourly"]
    assert [h["datetime"] for h in hours] == ["2026-10-25T03:00", "2026-10-25T03:00", "2026-10-25T04:00"]
    assert [h["datetime_utc"] for h in hours] == [
        "2026-10-25T00:00:00Z",
        "2026-10-25T01:00:00Z",
        "2026-10-25T02:00:00Z",
    ]
    first, second, last = hours
    # NO2 30 µg/m³ is "Moderat" while PM2.5 is only "Acceptabil": the overall index takes the worst.
    assert (first["aqi_pm25_code"], first["aqi_code"], first["aqi_label"]) == (2, 3, "Moderat")
    assert (second["aqi_pm25_code"], second["aqi_code"]) == (1, 4)
    assert last["pm2_5"] is None and last["european_aqi"] is None
    assert (last["aqi_pm25_code"], last["aqi_pm25_label"], last["aqi_code"]) == (0, "Necunoscut", 0)


def test_parse_without_alert_and_missing_values():
    raw = payload()
    raw["current"].update(pm2_5=None, pm10=None, nitrogen_dioxide=None)
    current = AirQualityService.parse(raw)["current"]
    assert current["pm2_5"]["value"] is None and current["pm2_5"]["code"] == 0
    assert current["rnmca"] == {"code": 0, "label": "Necunoscut", "recommendation": None, "pollutant": None}
    assert current["alert"] is False


def test_parse_unknown_timezone_falls_back_to_offset():
    raw = payload()
    raw["timezone"] = "Not/AZone"
    data = AirQualityService.parse(raw)
    assert data["current"]["timestamp"] == "2026-10-25T03:00"


@pytest.mark.parametrize("raw", [None, [], {}, {"current": "x"}])
def test_parse_rejects_unexpected_schema(raw):
    with pytest.raises(ProviderError):
        AirQualityService.parse(raw)


def test_parse_without_hourly_block():
    raw = payload()
    del raw["hourly"]
    assert AirQualityService.parse(raw)["forecast_hourly"] == []


async def test_fetch_parameters_and_ten_minute_cache(settings):
    calls = []

    def handle(request):
        calls.append(request)
        params = request.url.params
        assert request.url.host == "air-quality-api.open-meteo.com"
        assert (params["latitude"], params["longitude"]) == ("47.16", "27.58")
        assert params["current"] == "european_aqi,pm10,pm2_5,nitrogen_dioxide,ozone,sulphur_dioxide"
        assert params["hourly"] == "pm10,pm2_5,nitrogen_dioxide,ozone,sulphur_dioxide,european_aqi"
        assert params["forecast_days"] == "3"
        assert params["timezone"] == "auto"
        assert params["timeformat"] == "unixtime"
        return httpx.Response(200, json=payload())

    http = ProviderHTTP(settings, httpx.MockTransport(handle))
    service = AirQualityService(http)
    first = await service.fetch_air_quality()
    second = await service.fetch_air_quality()
    assert len(calls) == 1 and first == second
    key = next(iter(http.cache))
    assert 590 < http.cache[key][0] - time.monotonic() <= 600
    await http.close()


def use_transport(client, settings, handle):
    # Swap the app's provider client for a mocked one; the lifespan closes whichever is installed.
    client.portal.call(client.app.state.http.close)
    client.app.state.http = ProviderHTTP(settings, httpx.MockTransport(handle))


def test_endpoint(client, settings):
    requested = []

    def handle(request):
        requested.append(request.url.params["forecast_days"])
        return httpx.Response(200, json=payload())

    use_transport(client, settings, handle)
    response = client.get("/api/v1/air-quality", params={"forecast_days": 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current"]["pm2_5"]["label"] == "Moderat"
    assert len(body["forecast_hourly"]) == 3
    assert requested == ["2"]
    assert client.get("/api/v1/air-quality").status_code == 200
    assert requested == ["2", "3"]


@pytest.mark.parametrize("days", [0, 8, "x"])
def test_endpoint_validates_forecast_days(client, days):
    assert client.get("/api/v1/air-quality", params={"forecast_days": days}).status_code == 422


def test_endpoint_upstream_failure(client, settings):
    use_transport(client, settings, lambda request: httpx.Response(500))
    response = client.get("/api/v1/air-quality")
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_http_500"
