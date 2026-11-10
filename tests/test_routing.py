"""Routing: GraphHopper parsing, penalty areas, exposure, route choice, places and the /routes endpoint."""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import numpy as np
import pytest

from app.air.grid import GridSpec
from app.config import Bounds
from app.places.service import local_search, normalize, parse_photon, reverse_label
from app.routing import areas
from app.routing.exposure import Boost, Candidate, choose, measure, overlap, segments
from app.routing.graphhopper import GhPath, parse_path
from app.routing.service import google_maps_url

CITY = Bounds(47.05, 27.40, 47.26, 27.75)
SPEC = GridSpec.for_bounds(CITY)


def straight(lat: float, lon_from: float, lon_to: float, minutes: float, n: int = 20) -> GhPath:
    coords = [(lon_from + (lon_to - lon_from) * i / n, lat) for i in range(n + 1)]
    return GhPath(
        distance_m=1000, duration_s=minutes * 60, coordinates=coords, segment_s=[minutes * 60 / n] * n
    )


def flat(value: float) -> dict[str, np.ndarray]:
    shape = (SPEC.rows, SPEC.cols)
    return {"pm25": np.full(shape, value), "eaqi": np.full(shape, 3.0), "confidence": np.full(shape, 1.0)}


# --- GraphHopper parsing ---------------------------------------------------------------------------


def test_parse_path_spreads_time_details_by_length():
    path = {
        "distance": 300,
        "time": 300_000,
        "points": {"coordinates": [[27.59, 47.16], [27.591, 47.16], [27.593, 47.16]]},
        "details": {"time": [[0, 2, 300_000]]},
    }
    parsed = parse_path(path)
    assert parsed.duration_s == 300
    assert parsed.segment_s == pytest.approx([100, 200], rel=0.01)


# --- exposure and choice ---------------------------------------------------------------------------


def test_exposure_index_is_concentration_times_hours():
    path = straight(47.16, 27.58, 27.60, minutes=30)
    exposure = measure(path, SPEC, flat(20.0))
    assert exposure.index == pytest.approx(10.0, rel=0.01)  # 20 µg/m³ x 0.5 h
    assert exposure.avg_pm25 == pytest.approx(20.0)
    assert exposure.max_level == 3


def test_air_report_adds_exposure_inside_its_radius():
    path = straight(47.16, 27.58, 27.60, minutes=30)
    smoke = Boost(lat=47.16, lon=27.59, radius_m=300, extra=50.0)
    assert measure(path, SPEC, flat(20.0), (smoke,)).index > measure(path, SPEC, flat(20.0)).index * 1.2


def test_choose_keeps_three_distinct_routes():
    fast = straight(47.160, 27.58, 27.60, minutes=20)
    mid = straight(47.165, 27.58, 27.60, minutes=23)
    clean = straight(47.170, 27.58, 27.60, minutes=26)
    candidates = [
        Candidate(fast, measure(fast, SPEC, flat(30.0))),
        Candidate(mid, measure(mid, SPEC, flat(20.0))),
        Candidate(clean, measure(clean, SPEC, flat(10.0))),
    ]
    chosen = choose(candidates)
    assert [c.kinds for c in chosen] == [["cleanest"], ["balanced"], ["fastest"]]


def test_choose_merges_when_gain_is_small_or_route_too_slow():
    fast = straight(47.160, 27.58, 27.60, minutes=20)
    slow = straight(47.170, 27.58, 27.60, minutes=40)  # more than 1.5 x
    similar = straight(47.165, 27.58, 27.60, minutes=21)
    candidates = [
        Candidate(fast, measure(fast, SPEC, flat(20.0))),
        Candidate(slow, measure(slow, SPEC, flat(5.0))),
        Candidate(similar, measure(similar, SPEC, flat(19.5))),
    ]
    [only] = choose(candidates)
    assert only.kinds == ["fastest", "cleanest"]


def test_overlap_detects_the_same_route():
    a = straight(47.16, 27.58, 27.60, minutes=20)
    assert overlap(a, a) == pytest.approx(1.0)
    assert overlap(a, straight(47.17, 27.58, 27.60, minutes=20)) == 0


def test_segments_share_boundaries():
    assert segments([2, 2, 3, 3, 3, 2]) == [(0, 2, 2), (2, 5, 3), (5, 5, 2)]


# --- penalty areas ---------------------------------------------------------------------------------


def test_hotspots_are_relative_to_the_city():
    pm25 = np.full((SPEC.rows, SPEC.cols), 20.0)
    pm25[40:45, 50:55] = 35.0
    masks = areas.hotspot_masks(pm25, np.full_like(pm25, 3.0))
    assert masks["hot2"][42, 52] and not masks["hot2"][0, 0]
    assert not np.any(masks["hot1"] & masks["hot2"])
    polygons = areas.mask_polygons(SPEC, masks["hot2"])
    assert len(polygons) == 1


def test_custom_model_has_one_area_per_polygon():
    tiers = {"hot1": [areas.circle(47.16, 27.59, 200), areas.circle(47.18, 27.6, 200)], "obstacle": []}
    model = areas.custom_model("cleanest", tiers)
    assert [f["id"] for f in model["areas"]["features"]] == ["hot1_0", "hot1_1"]
    assert model["priority"] == [{"if": "in_hot1_0 || in_hot1_1", "multiply_by": "0.5"}]
    assert areas.custom_model("balanced", {"hot1": []}) is None


def test_buffer_is_in_metres():
    from shapely.geometry import Point

    [polygon] = areas.buffered(Point(27.59, 47.16), 100)
    width_m = (polygon.bounds[2] - polygon.bounds[0]) * 111_320 * np.cos(np.radians(47.16))
    height_m = (polygon.bounds[3] - polygon.bounds[1]) * 111_320
    assert width_m == pytest.approx(200, rel=0.05) and height_m == pytest.approx(200, rel=0.05)


def test_google_maps_url_has_waypoints_and_mode():
    url = google_maps_url(straight(47.16, 27.58, 27.60, minutes=20, n=50), "bike")
    query = parse_qs(urlparse(url).query)
    assert query["travelmode"] == ["bicycling"]
    assert len(query["waypoints"][0].split("|")) == 8


# --- places ----------------------------------------------------------------------------------------


def test_local_search_ignores_diacritics_and_uses_aliases():
    assert normalize("Podu  Roș!") == "podu ros"
    assert local_search("podu ros")[0].name == "Podu Roș"
    assert local_search("gara")[0].name == "Gara Iași"
    assert local_search("xyz") == []


def test_photon_and_reverse_parsing():
    data = {
        "features": [
            {
                "geometry": {"coordinates": [27.585, 47.164]},
                "properties": {
                    "name": "Strada Lăpușneanu",
                    "type": "street",
                    "district": "Centru",
                    "osm_id": 1,
                    "osm_type": "W",
                },
            },
            {"geometry": {"coordinates": [26.1, 44.4]}, "properties": {"name": "București", "type": "city"}},
        ]
    }
    [place] = parse_photon(data, CITY)
    assert (place.label, place.kind) == ("Strada Lăpușneanu, Centru, Iași", "street")
    assert reverse_label({"address": {"road": "Piața Unirii", "suburb": "Centru"}}) == "Piața Unirii, Centru"


# --- /routes with a fake GraphHopper ----------------------------------------------------------------


def gh_path(lat: float, minutes: float) -> dict:
    coords = [[27.58 + 0.002 * i, lat] for i in range(11)]
    return {"distance": 1500, "time": minutes * 60_000, "points": {"coordinates": coords}, "details": {}}


def fake_graphhopper(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body.get("custom_model"):
        return httpx.Response(200, json={"paths": [gh_path(47.170, 24)]})
    return httpx.Response(200, json={"paths": [gh_path(47.160, 20)]})


@pytest.fixture
async def fake_gh(app):
    original = app.state.http
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(fake_graphhopper))
    yield
    await app.state.http.aclose()
    app.state.http = original


async def test_routes_avoid_a_smoke_report(client, alice, fake_gh):
    report = await client.post(
        "/reports",
        json={
            "category": "smoke",
            "severity": 3,
            "description": "Fum gros.",
            "location": {"lat": 47.160, "lon": 27.59},
        },
        headers=alice,
    )
    assert report.status_code == 201
    response = await client.post(
        "/routes",
        json={
            "origin": {"lat": 47.16, "lon": 27.58},
            "destination": {"lat": 47.16, "lon": 27.60},
            "mode": "walk",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    kinds = [r["kinds"] for r in body["routes"]]
    assert kinds == [["cleanest"], ["fastest"]]
    fastest = body["routes"][-1]
    assert fastest["reports_nearby"] == [report.json()["id"]]
    assert fastest["warnings"] == ["Trece pe lângă un raport activ: fum."]
    assert body["routes"][0]["exposure"]["vs_fastest_pct"] < 0
    assert set(body["routes"][0]["segments"][0]) == {"from", "to", "eaqi_level"}


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"origin": {"lat": 44.4, "lon": 26.1}, "destination": {"lat": 47.16, "lon": 27.6}}, "origin"),
        (
            {"origin": {"lat": 47.16, "lon": 27.6}, "destination": {"lat": 47.1601, "lon": 27.6001}},
            "destination",
        ),
    ],
)
async def test_routes_validation(client, payload, field):
    response = await client.post("/routes", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["field"] == field


async def test_routes_reject_departure_beyond_forecast(client):
    late = (datetime.now(UTC) + timedelta(hours=80)).isoformat()
    response = await client.post(
        "/routes",
        json={
            "origin": {"lat": 47.16, "lon": 27.58},
            "destination": {"lat": 47.16, "lon": 27.60},
            "depart_at": late,
        },
    )
    assert response.status_code == 422


async def test_routing_unavailable_is_503(client, app):
    original = app.state.http
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    try:
        response = await client.post(
            "/routes",
            json={"origin": {"lat": 47.16, "lon": 27.58}, "destination": {"lat": 47.16, "lon": 27.60}},
        )
    finally:
        await app.state.http.aclose()
        app.state.http = original
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "routing_unavailable"


async def test_routes_with_small_construction_polygons(client, session, fake_gh):
    from datetime import UTC, datetime

    from app.air.ingest import store_osm
    from app.air.providers.records import OsmRecord

    tiny = [[27.5900, 47.1600], [27.59001, 47.1600], [27.59001, 47.16001], [27.5900, 47.1600]]
    await store_osm(
        session,
        [
            OsmRecord("way/1", "construction", None, {"type": "Polygon", "coordinates": [tiny]}, {}),
            OsmRecord("way/2", "construction", None, {"type": "LineString", "coordinates": tiny[:2]}, {}),
        ],
        datetime.now(UTC),
    )
    await session.commit()
    response = await client.post(
        "/routes", json={"origin": {"lat": 47.16, "lon": 27.58}, "destination": {"lat": 47.16, "lon": 27.60}}
    )
    assert response.status_code == 200, response.text
