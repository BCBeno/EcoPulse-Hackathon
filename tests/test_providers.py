"""All measurements, station IDs and announcements below are synthetic test fixtures."""

import copy
from datetime import UTC, date, datetime

import httpx
import pytest

from app.core.config import Settings
from app.integrations.cams import CamsAirQualityProvider
from app.integrations.http import ProviderError, ProviderHTTP
from app.integrations.official_works import OfficialWorksProvider
from app.integrations.openaq import OpenAQProvider
from app.integrations.openstreetmap import OpenStreetMapFeaturesProvider
from app.integrations.routing import NominatimGeocodingProvider, ValhallaRoutingProvider, decode_polyline
from app.integrations.sensor_community import SensorCommunityProvider
from app.integrations.weather import OpenMeteoWeatherProvider
from app.schemas.domain import RouteRequest, normalize_value


@pytest.fixture
def settings():
    return Settings(_env_file=None)


def sensor_fixture():
    return {
        "id": "synthetic-record",
        "sensor": {"id": "fixture-sensor", "sensor_type": {"name": "SDS011"}},
        "location": {"latitude": "47.158", "longitude": "27.601"},
        "timestamp": "2026-01-01 12:00:00",
        "sensordatavalues": [
            {"value_type": "P1", "value": "22.5"},
            {"value_type": "P2", "value": "8.5"},
            {"value_type": "temperature", "value": "-5"},
            {"value_type": "humidity", "value": "65"},
        ],
    }


def weather_fixture(variables=None):
    return {
        "latitude": 47.15,
        "longitude": 27.6,
        "hourly": {
            "time": [1767268800, 1767272400],
            **(variables or {"temperature_2m": [5, None], "relative_humidity_2m": [60, 101]}),
        },
        "hourly_units": {
            "temperature_2m": "°C",
            "relative_humidity_2m": "%",
            "pm2_5": "μg/m³",
            "pm10": "μg/m³",
            "nitrogen_dioxide": "μg/m³",
            "ozone": "μg/m³",
        },
    }


def test_sensor_normalizes_and_deduplicates(settings):
    provider = SensorCommunityProvider(settings, None)
    rows = provider.parse([sensor_fixture(), sensor_fixture()])
    assert len(rows) == 4
    assert {x.parameter for x in rows} == {"pm25", "pm10", "temperature", "humidity"}
    assert next(x for x in rows if x.parameter == "temperature").value == -5
    assert next(x for x in rows if x.parameter == "humidity").category == "WEATHER_CONTEXT"
    assert rows[0].provenance["license"] == "ODbL-1.0"
    assert rows[0].observed_at.tzinfo is not None


@pytest.mark.parametrize("field,value", [("location", {}), ("sensor", {}), ("timestamp", "invalid")])
def test_sensor_invalid_metadata(settings, field, value):
    row = sensor_fixture()
    row[field] = value
    assert SensorCommunityProvider(settings, None).parse([row]) == []


@pytest.mark.parametrize("value", ["NaN", "inf", "-1", "broken", None])
def test_sensor_invalid_values_preserved_as_missing(settings, value):
    row = sensor_fixture()
    row["sensordatavalues"] = [{"value_type": "P2", "value": value}]
    result = SensorCommunityProvider(settings, None).parse([row])
    assert len(result) == 1 and result[0].value is None
    assert not result[0].quality["valid"]


def test_sensor_csv(settings):
    content = "sensor_id;lat;lon;timestamp;P1;P2\nfixture;47.15;27.6;2026-01-01T12:00:00;20;8\n"
    rows = SensorCommunityProvider(settings, None).parse_csv(content)
    assert len(rows) == 2 and rows[0].sensor_id == "fixture"


async def test_sensor_http_filter_user_agent(settings):
    def handle(request):
        assert request.url.path.endswith("/filter/box=" + settings.city_bbox)
        assert "EcoPulse" in request.headers["user-agent"]
        return httpx.Response(200, json=[sensor_fixture()])

    http = ProviderHTTP(settings, httpx.MockTransport(handle))
    assert len(await SensorCommunityProvider(settings, http).fetch()) == 4
    await http.close()


@pytest.mark.parametrize(
    "unit,value,expected", [("mg/m3", 1.2, 1200), ("µg/m³", 12, 12), ("μg/m³", 12, 12), ("ppm", 1, None)]
)
def test_explicit_unit_conversion(unit, value, expected):
    assert normalize_value(value, unit, "pm25")[1] == expected


def test_openaq_hourly_metadata(settings):
    location = {
        "id": "fixture-location",
        "name": "Synthetic official monitor",
        "isMonitor": True,
        "coordinates": {"latitude": 47.15, "longitude": 27.6},
        "licenses": [{"name": "fixture-license"}],
        "provider": {"name": "fixture-provider"},
    }
    sensor = {"id": "fixture-sensor", "parameter": {"name": "pm25", "units": "µg/m³"}}
    rows = [
        {
            "value": 8,
            "period": {"datetimeFrom": {"utc": "2026-01-01T12:00:00Z"}},
            "coverage": {"percentCoverage": 100},
        }
    ]
    result = OpenAQProvider(settings, None).parse(rows, location, sensor)[0]
    assert result.category == "OFFICIAL_MEASUREMENT"
    assert result.provenance["licenses"] == location["licenses"]
    assert result.provenance["product"] == "hourly_aggregate"
    location["isMonitor"] = False
    assert (
        OpenAQProvider(settings, None).parse(rows, location, sensor)[0].category
        == "LOW_COST_SENSOR_MEASUREMENT"
    )
    rows[0]["value"] = -999
    assert OpenAQProvider(settings, None).parse(rows, location, sensor)[0].value is None


async def test_openaq_missing_key(settings):
    settings.openaq_api_key = ""
    with pytest.raises(ProviderError, match="missing_OPENAQ_API_KEY"):
        await OpenAQProvider(settings, None).fetch()


async def test_openaq_pagination_and_auth(settings):
    settings.openaq_api_key = "synthetic-test-key"
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["X-API-Key"] == "synthetic-test-key"
        page = int(request.url.params["page"])
        return httpx.Response(
            200, json={"meta": {"found": 101}, "results": [{"id": "fixture"}] * (100 if page == 1 else 1)}
        )

    http = ProviderHTTP(settings, httpx.MockTransport(handle))
    provider = OpenAQProvider(settings, http)
    assert len(await provider.pages("/locations")) == 101
    assert len(calls) == 2
    await http.close()


async def test_openaq_discovery_sensor_hours(settings):
    settings.openaq_api_key = "test-only"
    location = {"id": 1, "isMonitor": True, "coordinates": {"latitude": 47.15, "longitude": 27.6}}
    sensor = {"id": 2, "parameter": {"name": "pm25", "units": "ug/m3"}}

    def handle(request):
        path = request.url.path
        if path == "/v3/locations":
            assert request.url.params["radius"] == "15000"
            rows = [location]
        elif path == "/v3/locations/1/sensors":
            rows = [sensor]
        else:
            assert path == "/v3/sensors/2/hours"
            assert "datetime_from" in request.url.params
            rows = [{"value": 5, "period": {"datetimeFrom": {"utc": datetime.now(UTC).isoformat()}}}]
        return httpx.Response(200, json={"results": rows})

    http = ProviderHTTP(settings, httpx.MockTransport(handle))
    assert len(await OpenAQProvider(settings, http).fetch()) == 1
    await http.close()


def test_weather_missing_units_reanalysis(settings):
    provider = OpenMeteoWeatherProvider(settings, None)
    rows = provider.parse(weather_fixture())
    assert len(rows) == 12
    assert all(x.category == "WEATHER_CONTEXT" for x in rows)
    humidity = [x for x in rows if x.parameter == "relative_humidity_2m"]
    assert humidity[1].value is None
    reanalysis = provider.parse(weather_fixture(), product="reanalysis", model="era5")
    assert reanalysis[0].provenance["product"] == "reanalysis"
    assert reanalysis[0].id != rows[0].id


def test_cams_forecast_category_horizon(settings):
    provider = CamsAirQualityProvider(settings, None)
    rows = provider.parse(weather_fixture({"pm2_5": [12, None], "pm10": [20, 25]}))
    assert len(rows) == 12
    assert all(x.category == "MODEL_FORECAST" and x.quality["resolution_km"] == 11 for x in rows)
    assert all(x.provenance["model"] == "cams_europe" for x in rows)
    assert max(x.observed_at for x in rows) > min(x.observed_at for x in rows)
    assert any(x.value is None for x in rows)


async def test_weather_request_cache(settings):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.params["forecast_days"] == "3"
        assert request.url.params["timeformat"] == "unixtime"
        assert request.url.params["wind_speed_unit"] == "ms"
        return httpx.Response(200, json=weather_fixture())

    http = ProviderHTTP(settings, httpx.MockTransport(handle))
    provider = OpenMeteoWeatherProvider(settings, http)
    await provider.fetch()
    await provider.fetch()
    assert len(calls) == 1
    await http.close()


async def test_weather_archive_parameters(settings):
    def handle(request):
        assert request.url.host == "archive-api.open-meteo.com"
        assert request.url.params["models"] == "era5"
        return httpx.Response(200, json=weather_fixture())

    http = ProviderHTTP(settings, httpx.MockTransport(handle))
    result = await OpenMeteoWeatherProvider(settings, http).history(date(2020, 1, 1), date(2020, 1, 2))
    assert result[0].provenance["product"] == "reanalysis"
    with pytest.raises(ProviderError):
        await OpenMeteoWeatherProvider(settings, http).history(date.today(), date.today())
    await http.close()


def test_overpass_query_and_dedup(settings):
    provider = OpenStreetMapFeaturesProvider(settings, None)
    assert settings.city_bbox in provider.query()
    element = {
        "type": "way",
        "id": "fixture-way",
        "center": {"lat": 47.15, "lon": 27.6},
        "tags": {"landuse": "construction"},
        "geometry": [{"lat": 47.15, "lon": 27.6}, {"lat": 47.151, "lon": 27.601}],
    }
    result = provider.parse({"elements": [element, copy.deepcopy(element), {"id": "missing-coordinates"}]})
    assert len(result) == 1 and result[0]["geometry"]["type"] == "LineString"
    assert result[0]["provenance"]["category"] == "OSM_MAPPED_FEATURE"
    assert result[0]["source_id"] == "way/fixture-way"


def test_official_feed_unknown_dates_remain_review(settings):
    provider = OfficialWorksProvider(settings, None)
    fixture = "<rss><channel><item><title>Labelled synthetic announcement</title><link>https://politialocala-iasi.ro/synthetic-test-only</link><description><![CDATA[<p>No verified dates</p>]]></description><pubDate>Wed, 01 Jan 2020 12:00:00 GMT</pubDate></item><item><link>http://127.0.0.1/secret</link></item></channel></rss>"
    rows = provider.parse(fixture)
    assert len(rows) == 1 and rows[0]["verification"] == "needs_review"
    assert "starts_at" not in rows[0] and "latitude" not in rows[0]
    assert provider.parse(fixture)[0]["source_id"] == rows[0]["source_id"]


@pytest.mark.parametrize(
    "status,code",
    [(401, "authentication_rejected"), (403, "authentication_rejected"), (400, "upstream_http_400")],
)
async def test_http_failure_no_secrets(settings, status, code):
    http = ProviderHTTP(
        settings,
        httpx.MockTransport(lambda r: httpx.Response(status, json={"secret": "should never appear"})),
    )
    with pytest.raises(ProviderError, match=code) as info:
        await http.get_json("https://fixture.invalid")
    assert "secret" not in str(info.value)
    await http.close()


async def test_retry_after_limits(settings):
    http = ProviderHTTP(
        settings, httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "60"}))
    )
    with pytest.raises(ProviderError, match="rate_limited"):
        await http.get_json("https://fixture.invalid")
    await http.close()


async def test_network_and_malformed_responses(settings):
    def fail(request):
        raise httpx.ConnectError("synthetic failure")

    http = ProviderHTTP(settings, httpx.MockTransport(fail))
    with pytest.raises(ProviderError, match="network_error"):
        await SensorCommunityProvider(settings, http).fetch()
    await http.close()
    http = ProviderHTTP(settings, httpx.MockTransport(lambda r: httpx.Response(200, text="not-json")))
    with pytest.raises(ProviderError, match="malformed_json"):
        await http.get_json("https://fixture.invalid")
    await http.close()


def test_decode_route_polyline():
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)[0] == [-120.2, 38.5]
    with pytest.raises(ValueError):
        decode_polyline("_")


async def test_routing_normalization_and_dedup(settings):
    settings.valhalla_base_url = "https://fixture.invalid"
    trip = {
        "summary": {"length": 1.2, "time": 300},
        "legs": [
            {
                "shape": "_p~iF~ps|U_ulLnnqC_mqNvxq`@",
                "maneuvers": [{"instruction": "Synthetic turn", "length": 1.2, "time": 300}],
            }
        ],
    }
    http = ProviderHTTP(
        settings,
        httpx.MockTransport(
            lambda r: httpx.Response(200, json={"trip": trip, "alternates": [{"trip": trip}]})
        ),
    )
    request = RouteRequest(
        origin={"latitude": 47.15, "longitude": 27.6}, destination={"latitude": 47.16, "longitude": 27.61}
    )
    result = await ValhallaRoutingProvider(settings, http).routes(request)
    assert len(result) == 1 and result[0]["distance_m"] == 1200
    assert result[0]["duration_s"] == 300 and result[0]["provider"] == "Valhalla"
    await http.close()


async def test_nominatim_explicit_search_cache(settings):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.params["bounded"] == "1"
        return httpx.Response(
            200, json=[{"display_name": "Synthetic location", "lat": "47.15", "lon": "27.6"}]
        )

    http = ProviderHTTP(settings, httpx.MockTransport(handle))
    provider = NominatimGeocodingProvider(settings, http)
    await provider.search("Fixture")
    await provider.search("Fixture")
    assert len(calls) == 1
    await http.close()
