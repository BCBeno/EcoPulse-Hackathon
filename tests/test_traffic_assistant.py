"""Synthetic roads, CAMS values and reports used only to verify traffic and assistant rules."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from test_avoidance import cams_payload

from app.core.config import Settings, get_settings
from app.integrations.http import ProviderHTTP
from app.integrations.traffic import OsmMajorRoadsProvider, TomTomFlowProvider
from app.main import app
from app.services.air_assistant import deterministic_answer, is_question, target_time
from app.services.traffic import band, demand, traffic_at

ZONE = ZoneInfo("Europe/Bucharest")


def overpass_payload():
    return {
        "elements": [
            {
                "type": "way",
                "id": 1,
                "tags": {"highway": "primary", "name": "Bulevardul Sintetic"},
                "geometry": [{"lat": 47.17, "lon": 27.57}, {"lat": 47.175, "lon": 27.575}],
            },
            {
                "type": "way",
                "id": 2,
                "tags": {"highway": "tertiary", "name": "Strada Liniștită"},
                "geometry": [{"lat": 47.16, "lon": 27.59}, {"lat": 47.161, "lon": 27.592}],
            },
            {"type": "way", "id": 3, "tags": {"highway": "residential"}, "geometry": []},
        ]
    }


@pytest.fixture
def providers(client, monkeypatch):
    state = {"overpass": "ok", "overpass_calls": 0}

    def handle(request):
        if request.url.host == "air-quality-api.open-meteo.com":
            return httpx.Response(200, json=cams_payload())
        state["overpass_calls"] += 1
        if state["overpass"] == "failure":
            return httpx.Response(504)
        return httpx.Response(200, json=overpass_payload())

    http = ProviderHTTP(get_settings(), httpx.MockTransport(handle))
    monkeypatch.setattr(app.state, "http", http)
    OsmMajorRoadsProvider.memory.clear()
    yield state
    OsmMajorRoadsProvider.memory.clear()
    client.portal.call(http.close)


def local(day, hour):
    # 2026-10-19 is a Monday, 2026-10-18 a Sunday.
    return datetime(2026, 10, day, hour, tzinfo=ZONE)


def test_rush_hour_is_busier_than_night_and_weekend():
    assert demand(local(19, 8), "Europe/Bucharest") > demand(local(19, 13), "Europe/Bucharest")
    assert demand(local(19, 13), "Europe/Bucharest") > demand(local(19, 3), "Europe/Bucharest")
    assert demand(local(18, 8), "Europe/Bucharest") < demand(local(19, 8), "Europe/Bucharest")
    assert band(0.1)[0] == "low" and band(0.95)[0] == "severe"


def test_traffic_endpoint_ranks_major_roads_and_applies_reports(client, account, providers):
    rush = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    rush = rush.astimezone(ZONE).replace(hour=8).astimezone(UTC)
    body = client.get("/api/v1/traffic", params={"at": rush.isoformat()}).json()
    assert body["available"] and body["status"] == "estimated"
    levels = {s["name"]: s["level"] for s in body["segments"]}
    assert len(levels) == 2  # residential ways and empty geometry are excluded
    assert levels["Bulevardul Sintetic"] > levels["Strada Liniștită"]

    headers = account()
    response = client.post(
        "/api/v1/reports",
        headers=headers,
        json={
            "category": "traffic",
            "description": "SYNTHETIC FIXTURE: blocked junction",
            "latitude": 47.1605,
            "longitude": 27.591,
            "observed_at": datetime.now(UTC).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    now = client.get("/api/v1/traffic").json()
    quiet = next(s for s in now["segments"] if s["name"] == "Strada Liniștită")
    assert quiet["source"] == "community_report" and quiet["band"] == "severe"
    assert "community_reports" in now["sources"]
    assert providers["overpass_calls"] == 1  # roads are cached between requests


def test_traffic_without_roads_still_answers_with_time_profile(client, providers):
    providers["overpass"] = "failure"
    body = client.get("/api/v1/traffic").json()
    assert body["status"] == "roads_unavailable" and body["segments"] == []
    assert body["sources"] == ["time_profile"] and body["city_label"]


def test_road_cache_survives_overpass_outage(tmp_path, providers, client):
    settings = Settings(_env_file=None, traffic_roads_cache=str(tmp_path / "roads.json"))
    provider = OsmMajorRoadsProvider(settings, app.state.http)
    assert len(client.portal.call(provider.fetch)) == 2
    OsmMajorRoadsProvider.memory.clear()
    providers["overpass"] = "failure"
    assert len(client.portal.call(provider.fetch)) == 2  # refresh fails, last good copy is reused


@pytest.mark.parametrize(
    ("text", "question"),
    [
        ("Cum e aerul acum?", True),
        ("pot sa alerg maine dimineata", True),
        ("e trafic mare pe Copou", False),
        ("miroase a fum langa parc", False),
    ],
)
def test_question_or_report(text, question):
    assert is_question(text) is question


def test_target_time_understands_romanian_phrases():
    settings = Settings(_env_file=None)
    now = local(18, 21).astimezone(UTC)
    assert target_time("Cum e aerul mâine la 17?", settings, now).astimezone(ZONE) == local(19, 17)
    assert target_time("dar la 7?", settings, now).astimezone(ZONE) == local(19, 7)
    assert target_time("peste 2 ore?", settings, now) == now + timedelta(hours=2)
    assert target_time("cum e aerul?", settings, now) == now


def test_ask_answers_air_questions_and_hands_reports_back(client, providers):
    answer = client.post(
        "/api/v1/assistant/ask",
        json={"text": "Cum e aerul mâine la 17?", "latitude": 47.172, "longitude": 27.572},
    ).json()
    assert answer["kind"] == "answer" and answer["engine"] == "deterministic"
    assert "Acceptabil" in answer["reply"] and "RNMCA 2/6" in answer["reply"]
    assert answer["facts"]["traffic"]["nearby"][0]["name"] == "Bulevardul Sintetic"
    report = client.post("/api/v1/assistant/ask", json={"text": "miroase a fum langa parc"}).json()
    assert report == {"kind": "report"}


def test_missing_forecast_is_not_described_as_clean_air():
    reply = deterministic_answer("cum e aerul?", {"when": "Acum", "forecast_status": "unavailable"})
    assert "nu înseamnă aer curat" in reply


def test_explicit_report_category_overrides_text_inference(client, account):
    body = client.post(
        "/api/v1/reports/from-text",
        headers=account(),
        json={"text": "ceva ciudat aici", "latitude": 47.16, "longitude": 27.59, "category": "smoke"},
    )
    assert body.status_code == 201, body.text
    assert body.json()["report"]["category"] == "smoke"


def test_tomtom_spreads_over_its_stretch_and_calibrates_estimates(client, monkeypatch):
    roads = {
        "elements": [
            {"type": "way", "id": 10, "tags": {"highway": "primary", "name": "Bulevardul Măsurat"},
             "geometry": [{"lat": 47.170, "lon": 27.570}, {"lat": 47.176, "lon": 27.576}]},
            {"type": "way", "id": 11, "tags": {"highway": "primary", "name": "Bulevardul Continuare"},
             "geometry": [{"lat": 47.177, "lon": 27.577}, {"lat": 47.179, "lon": 27.579}]},
            {"type": "way", "id": 12, "tags": {"highway": "secondary", "name": "Strada Transversală"},
             "geometry": [{"lat": 47.171, "lon": 27.576}, {"lat": 47.175, "lon": 27.570}]},
            {"type": "way", "id": 13, "tags": {"highway": "tertiary", "name": "Strada Departe"},
             "geometry": [{"lat": 47.150, "lon": 27.600}, {"lat": 47.153, "lon": 27.603}]},
        ]
    }  # fmt: skip
    calls = []

    def handle(request):
        if request.url.host == "api.tomtom.com":
            calls.append(request.url.params["point"])
            stretch = [{"latitude": 47.170, "longitude": 27.570}, {"latitude": 47.180, "longitude": 27.580}]
            return httpx.Response(
                200,
                json={
                    "flowSegmentData": {
                        "currentSpeed": 10,
                        "freeFlowSpeed": 40,
                        "coordinates": {"coordinate": stretch},
                    }
                },
            )
        return httpx.Response(200, json=roads)

    settings = Settings(
        _env_file=None, tomtom_api_key="synthetic-key", tomtom_max_segments=1, traffic_roads_cache=""
    )
    http = ProviderHTTP(settings, httpx.MockTransport(handle))
    OsmMajorRoadsProvider.memory.clear()
    TomTomFlowProvider.cache.clear()

    async def run():
        from app.db.session import Session

        async with Session() as db:
            return await traffic_at(db, datetime.now(UTC), settings, http)

    try:
        result = client.portal.call(run)
    finally:
        OsmMajorRoadsProvider.memory.clear()
        TomTomFlowProvider.cache.clear()
        client.portal.call(http.close)
    by_name = {s["name"]: s for s in result["segments"]}
    assert len(calls) == 1 and "tomtom_realtime" in result["sources"]
    assert by_name["Bulevardul Măsurat"]["band"] == "severe"  # 10/40 km/h
    assert by_name["Bulevardul Continuare"]["source"] == "tomtom"  # same TomTom stretch, no extra request
    assert by_name["Strada Transversală"]["source"] == "profile_calibrated"  # crosses, does not follow
    assert by_name["Strada Departe"]["source"] == "profile_calibrated"
