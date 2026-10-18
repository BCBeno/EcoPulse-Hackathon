"""Regression fixtures: two demolition reports on a road must trigger a real provider detour request."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import get_settings
from app.db.session import Session
from app.integrations.http import ProviderHTTP
from app.main import app
from app.models.entities import MappedFeature
from app.schemas.domain import RouteRequest
from app.services.avoidance import exclusion_ring, incident_zones, select_exclusions

DIRECT = [[27.6, 47.15], [27.6, 47.19]]
DETOUR = [[27.6, 47.15], [27.606, 47.155], [27.606, 47.185], [27.6, 47.19]]
WIDE_DETOUR = [[27.6, 47.15], [27.612, 47.155], [27.612, 47.185], [27.6, 47.19]]


def polyline(points):
    output = []
    previous = [0, 0]
    for lon, lat in points:
        for index, value in enumerate([round(lat * 1e6), round(lon * 1e6)]):
            delta = value - previous[index]
            previous[index] = value
            delta = ~(delta << 1) if delta < 0 else delta << 1
            while delta >= 32:
                output.append(chr(((delta & 31) | 32) + 63))
                delta >>= 5
            output.append(chr(delta + 63))
    return "".join(output)


def trip(points, duration):
    return {
        "summary": {"length": 4.5 if points == DIRECT else 5.2, "time": duration},
        "legs": [{"shape": polyline(points), "maneuvers": []}],
    }


def cams_payload(pm25=8.0):
    start = int(datetime.now(UTC).replace(minute=0, second=0, microsecond=0).timestamp())
    times = [start + 3600 * hour for hour in range(-2, 80)]
    return {
        "latitude": 47.17,
        "longitude": 27.6,
        "hourly_units": {
            k: "μg/m³" for k in ("pm2_5", "pm10", "nitrogen_dioxide", "ozone", "sulphur_dioxide")
        }
        | {"european_aqi": "EAQI"},
        "hourly": {
            "time": times,
            "pm2_5": [pm25] * len(times),
            "pm10": [20.0] * len(times),
            "nitrogen_dioxide": [12.0] * len(times),
            "ozone": [40.0] * len(times),
            "sulphur_dioxide": [1.0] * len(times),
            "european_aqi": [30] * len(times),
        },
    }


def request_body(**changes):
    return {
        "origin": {"latitude": 47.15, "longitude": 27.6},
        "destination": {"latitude": 47.19, "longitude": 27.6},
        "mode": "walk",
        "departure_at": datetime.now(UTC).isoformat(),
        **changes,
    }


@pytest.fixture
def routing_fixture(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "valhalla_base_url", "https://synthetic-routing.invalid")
    state = {"mode": "detour", "calls": [], "air_calls": 0, "cams": "ok"}

    def handle(request):
        if request.url.host == "air-quality-api.open-meteo.com":
            state["air_calls"] += 1
            return (
                httpx.Response(500)
                if state["cams"] == "failure"
                else httpx.Response(200, json=cams_payload())
            )
        payload = json.loads(request.url.params["json"])
        state["calls"].append(payload)
        if payload.get("exclude_polygons"):
            if state["mode"] == "failure":
                return httpx.Response(500)
            if state["mode"] == "expand" and len(payload["exclude_polygons"]) >= 3:
                return httpx.Response(200, json={"trip": trip(WIDE_DETOUR, 2100)})
            return httpx.Response(
                200,
                json={
                    "trip": trip(DIRECT, 1600)
                    if state["mode"] == "ignore"
                    else trip(DETOUR, 4000 if state["mode"] == "long" else 1950)
                },
            )
        data = {"trip": trip(DIRECT, 1600)}
        if state["mode"] == "existing_alternative":
            data["alternates"] = [{"trip": trip(DETOUR, 1950)}]
        return httpx.Response(200, json=data)

    http = ProviderHTTP(settings, httpx.MockTransport(handle))
    monkeypatch.setattr(app.state, "http", http)
    yield state
    client.portal.call(http.close)


def add_report(client, headers, latitude, **changes):
    result = client.post(
        "/api/v1/reports",
        headers=headers,
        json={
            "category": "construction",
            "description": "SYNTHETIC DEMOLITION FIXTURE: isolated test database",
            "latitude": latitude,
            "longitude": 27.6,
            "observed_at": datetime.now(UTC).isoformat(),
            **changes,
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


def test_two_demolitions_request_verified_detour_and_recommend_it(client, account, routing_fixture):
    headers = account()
    add_report(client, headers, 47.165)
    add_report(client, headers, 47.175)
    response = client.post("/api/v1/routes/alternatives", json=request_body())
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(routing_fixture["calls"]) == 2
    rings = routing_fixture["calls"][1]["exclude_polygons"]
    assert len(rings) == 2 and all(ring[0] == ring[-1] for ring in rings)
    assert all(27 < p[0] < 28 and 47 < p[1] < 48 for ring in rings for p in ring)
    assert len(result["routes"]) == 2
    fastest, safer = result["routes"]
    assert fastest["avoidance"]["remaining_count"] == 2
    assert safer["avoidance"]["remaining_count"] == 0 and safer["avoidance"]["avoided_count"] == 2
    assert result["recommended_route_id"] == safer["id"] and safer["recommended"]
    assert safer["geometry"]["coordinates"] == DETOUR
    assert safer["additional_time_s"] == 350
    assert safer["environmental_score"] is None  # No fabricated clean-air measurement on an unobserved road.
    assert "avoids_incidents" in safer["perspectives"]
    assert result["avoidance"]["status"] == "alternative_found"
    assert all(z["status"] == "pending" for z in result["avoidance"]["zones"])


def test_opt_out_does_not_request_exclusions(client, account, routing_fixture):
    add_report(client, account(), 47.17)
    result = client.post(
        "/api/v1/routes/alternatives", json=request_body(preferences={"avoid_reported_incidents": False})
    ).json()
    assert len(routing_fixture["calls"]) == 1
    assert result["avoidance"]["status"] == "disabled"
    assert result["routes"][0]["avoidance"]["remaining_count"] == 1


@pytest.mark.parametrize("mode,status", [("failure", "unavailable"), ("ignore", "no_clear_detour")])
def test_provider_failure_or_ignored_exclusions_cannot_claim_success(
    client, account, routing_fixture, mode, status
):
    routing_fixture["mode"] = mode
    add_report(client, account(), 47.17)
    result = client.post("/api/v1/routes/alternatives", json=request_body()).json()
    assert len(result["routes"]) == 1 and result["avoidance"]["status"] == status
    assert result["routes"][0]["avoidance"]["remaining_count"] == 1
    assert "avoids_incidents" not in result["routes"][0]["perspectives"]


def test_report_at_destination_does_not_request_impossible_exclusion(client, account, routing_fixture):
    add_report(client, account(), 47.19)
    result = client.post("/api/v1/routes/alternatives", json=request_body()).json()
    assert len(routing_fixture["calls"]) == 1
    assert result["avoidance"]["status"] == "endpoint_in_zone"
    assert result["avoidance"]["endpoint_zone_ids"]


def test_resolved_and_expired_incidents_do_not_force_detours(client, account, routing_fixture):
    headers = account()
    report = add_report(client, headers, 47.17)
    assert client.post(f"/api/v1/reports/{report['id']}/resolve", headers=headers).status_code == 200
    add_report(client, headers, 47.175, observed_at=(datetime.now(UTC) - timedelta(hours=25)).isoformat())
    result = client.post("/api/v1/routes/alternatives", json=request_body()).json()
    assert result["avoidance"]["status"] == "not_needed"
    assert len(routing_fixture["calls"]) == 1


def test_new_report_recalculates_despite_cached_upstream_baseline(client, account, routing_fixture):
    payload = request_body()
    first = client.post("/api/v1/routes/alternatives", json=payload).json()
    assert len(first["routes"]) == 1
    add_report(client, account(), 47.17, observed_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    second = client.post("/api/v1/routes/alternatives", json=payload).json()
    assert len(second["routes"]) == 2 and second["avoidance"]["status"] == "alternative_found"


def test_existing_valid_alternative_avoids_redundant_request(client, account, routing_fixture):
    routing_fixture["mode"] = "existing_alternative"
    add_report(client, account(), 47.17)
    result = client.post("/api/v1/routes/alternatives", json=request_body()).json()
    assert len(routing_fixture["calls"]) == 1
    assert result["routes"][1]["recommended"]


def test_extreme_detour_is_shown_without_automatic_recommendation(client, account, routing_fixture):
    routing_fixture["mode"] = "long"
    add_report(client, account(), 47.17)
    result = client.post("/api/v1/routes/alternatives", json=request_body()).json()
    assert len(result["routes"]) == 2
    assert result["avoidance"]["status"] == "no_practical_detour"
    assert result["routes"][0]["recommended"]


def test_zone_limit_future_expiry_and_osm_separation():
    now = datetime.now(UTC)
    reports = [
        SimpleNamespace(
            id=str(i),
            category="construction",
            latitude=47.16 + i * 0.001,
            longitude=27.6,
            severity=2,
            status="pending",
            observed_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
        )
        for i in range(5)
    ]
    zones = incident_zones(reports, [], now, 150)
    assert len(zones) == 5 and all(z["category"] == "COMMUNITY_REPORT" for z in zones)
    selected, endpoints, capped = select_exclusions(
        [{"geometry": {"coordinates": DIRECT}}], zones, RouteRequest(**request_body()), 2
    )
    assert len(selected) == 2 and capped and not endpoints
    assert exclusion_ring(zones[0])[0] == exclusion_ring(zones[0])[-1]
    assert incident_zones(reports, [], now + timedelta(days=1), 150) == []


def test_detour_expands_to_incidents_and_optional_osm_encountered_on_first_detour(
    client, account, routing_fixture
):
    routing_fixture["mode"] = "expand"
    headers = account()
    add_report(client, headers, 47.165)
    add_report(client, headers, 47.17, longitude=27.606)

    async def add_mapped_feature():
        async with Session() as db:
            db.add(
                MappedFeature(
                    source_id="synthetic-osm",
                    latitude=47.18,
                    longitude=27.606,
                    tags={"construction": "yes"},
                    retrieved_at=datetime.now(UTC),
                    provenance={},
                )
            )
            await db.commit()

    client.portal.call(add_mapped_feature)
    result = client.post(
        "/api/v1/routes/alternatives", json=request_body(preferences={"avoid_mapped_construction": True})
    ).json()
    assert len(routing_fixture["calls"]) == 3
    assert len(routing_fixture["calls"][1]["exclude_polygons"]) == 1
    assert len(routing_fixture["calls"][2]["exclude_polygons"]) == 3
    assert result["avoidance"]["attempts"] == 2
    assert any(
        z["category"] == "OSM_MAPPED_FEATURE" and z["status"] == "unverified"
        for z in result["avoidance"]["zones"]
    )
    selected = next(r for r in result["routes"] if r["id"] == result["recommended_route_id"])
    assert selected["geometry"]["coordinates"] == WIDE_DETOUR
    assert selected["avoidance"]["remaining_count"] == 0
    partial = next(r for r in result["routes"] if r["geometry"]["coordinates"] == DETOUR)
    assert partial["explanation"].startswith("Ocol parțial")
    assert result["avoidance"]["status"] == "alternative_found"


def test_attempt_limit_keeps_partial_warning_instead_of_claiming_complete_avoidance(
    client, account, routing_fixture, monkeypatch
):
    monkeypatch.setattr(get_settings(), "route_max_detour_attempts", 1)
    headers = account()
    add_report(client, headers, 47.165, severity=5)
    add_report(client, headers, 47.175, longitude=27.606, severity=1)
    result = client.post("/api/v1/routes/alternatives", json=request_body()).json()
    assert len(routing_fixture["calls"]) == 2
    assert result["avoidance"]["search_limited"]
    assert result["avoidance"]["status"] == "partial_detour"
    selected = next(r for r in result["routes"] if r["recommended"])
    assert selected["avoidance"]["remaining_count"] == 1
    assert selected["explanation"].startswith("Ocol parțial")


def test_five_incidents_fit_public_valhalla_vertex_limit(client, account, routing_fixture):
    headers = account()
    for latitude in (47.159, 47.165, 47.171, 47.177, 47.183):
        add_report(client, headers, latitude)
    response = client.post("/api/v1/routes/alternatives", json=request_body())
    assert response.status_code == 200, response.text
    rings = routing_fixture["calls"][1]["exclude_polygons"]
    assert len(rings) == 5
    assert sum(len(ring) for ring in rings) <= 100
    assert response.json()["avoidance"]["status"] == "alternative_found"
