"""Pure air logic: EAQI bands, provider parsing (live samples, 2026-11-09), quality checks, grid."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from app.air import eaqi, grid
from app.air.providers import open_meteo, openaq, osm, sensor_community
from app.air.quality import Station, assess
from app.air.service import best_window
from app.config import Bounds

FIXTURES = Path(__file__).parent / "fixtures"
CITY = Bounds(47.05, 27.40, 47.26, 27.75)
NOW = datetime(2026, 11, 9, 20, 30, tzinfo=UTC)


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- EAQI ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parameter", "value", "level"),
    [
        ("pm25", 0, 1),
        ("pm25", 5.4, 1),  # rounds to 5: still "good"
        ("pm25", 5.5, 2),
        ("pm25", 15, 2),
        ("pm25", 140, 5),
        ("pm25", 141, 6),
        ("pm10", 46, 3),
        ("no2", 150, 5),
        ("o3", 181, 6),
        ("so2", 20, 1),
        ("pm25", None, None),
        ("pm25", -1, None),
        ("co", 10, None),
    ],
)
def test_sub_index_follows_eea_bands(parameter, value, level):
    assert eaqi.sub_index(parameter, value) == level


def test_overall_is_the_poorest_pollutant():
    assert eaqi.overall({"pm25": 12, "no2": 70, "o3": None}) == (4, "no2")
    assert eaqi.overall({}) == (None, None)


# --- providers -------------------------------------------------------------------------------------


def test_sensor_community_parses_live_sample():
    readings = sensor_community.parse(load("sensor_community.json"), CITY)
    assert {r.parameter for r in readings} == {"pm25", "pm10", "temperature", "humidity"}
    assert all(CITY.contains(r.lat, r.lon) for r in readings)
    assert all(r.observed_at.tzinfo is UTC and r.kind == "low_cost" for r in readings)
    pm = [r for r in readings if r.parameter == "pm25"]
    assert pm and all(0 <= r.value <= 1000 for r in pm)


def test_sensor_community_skips_indoor_invalid_and_outside():
    row = {
        "location": {"id": 1, "latitude": "47.16", "longitude": "27.59", "indoor": 0},
        "sensor": {"id": 9, "sensor_type": {"name": "SDS011"}},
        "timestamp": "2026-11-09 20:00:00",
        "sensordatavalues": [
            {"value_type": "P2", "value": "12.5"},
            {"value_type": "P1", "value": "-3"},
            {"value_type": "humidity", "value": "140"},
            {"value_type": "noise", "value": "50"},
        ],
    }
    indoor = json.loads(json.dumps(row))
    indoor["location"]["indoor"] = 1
    outside = json.loads(json.dumps(row))
    outside["location"]["latitude"] = "44.4"
    readings = sensor_community.parse([row, indoor, outside], CITY)
    assert [(r.parameter, r.value, r.external_id) for r in readings] == [("pm25", 12.5, "1")]


def test_cams_lattice_encloses_the_city():
    points = open_meteo.lattice(CITY)
    lats, lons = {p[0] for p in points}, {p[1] for p in points}
    assert min(lats) <= CITY.south and max(lats) >= CITY.north
    assert min(lons) <= CITY.west and max(lons) >= CITY.east


def test_cams_and_weather_parse_live_samples():
    block = load("open_meteo_aq.json")[0]
    values = open_meteo.parse_cams(block, 47.1, 27.5)
    assert {v.parameter for v in values} == {"pm25", "pm10", "no2", "o3", "so2"}
    assert values[0].point_id == "47.10,27.50"
    weather = open_meteo.parse_weather(load("open_meteo_weather.json"))
    assert len(weather) == 24 and weather[0].humidity_pct is not None


def test_openaq_keeps_only_mass_concentrations():
    location = {
        "id": 7,
        "name": "IS-1",
        "isMonitor": True,
        "coordinates": {"latitude": 47.16, "longitude": 27.58},
        "sensors": [
            {"id": 1, "parameter": {"name": "pm25", "units": "µg/m³"}},
            {"id": 2, "parameter": {"name": "no2", "units": "ppm"}},
        ],
        "provider": {"name": "EEA"},
        "licenses": [{"name": "CC BY 4.0"}],
    }
    latest = [
        {"sensorsId": 1, "value": 21.0, "datetime": {"utc": "2026-11-09T20:00:00Z"}},
        {"sensorsId": 2, "value": 0.02, "datetime": {"utc": "2026-11-09T20:00:00Z"}},
    ]
    [reading] = openaq.parse(location, latest)
    assert (reading.parameter, reading.value, reading.kind) == ("pm25", 21.0, "official")
    assert reading.meta["licenses"] == ["CC BY 4.0"]


def test_osm_geometry_types():
    square = [{"lat": 47.16, "lon": 27.59}, {"lat": 47.16, "lon": 27.60}, {"lat": 47.17, "lon": 27.60}]
    elements = [
        {"type": "way", "id": 1, "tags": {"landuse": "construction"}, "geometry": [*square, square[0]]},
        {"type": "way", "id": 2, "tags": {"highway": "construction"}, "geometry": square[:2]},
        {"type": "relation", "id": 3, "tags": {"name": "Pasaj"}, "center": {"lat": 47.15, "lon": 27.6}},
        {"type": "node", "id": 4, "lat": 44.0, "lon": 26.0, "tags": {}},
    ]
    records = {r.osm_id: r for r in osm.parse(elements, CITY)}
    assert records["way/1"].geometry["type"] == "Polygon"
    assert records["way/2"].geometry["type"] == "LineString"
    assert records["relation/3"].name == "Pasaj"
    assert "node/4" not in records


# --- quality ---------------------------------------------------------------------------------------


def station(pm25: float, lat: float = 47.16, lon: float = 27.59, age_min: int = 5, **values) -> Station:
    observed = NOW - timedelta(minutes=age_min)
    values = {"pm25": pm25, **values}
    return Station(uuid4(), "sensor_community", "low_cost", lat, lon, values, dict.fromkeys(values, observed))


def test_quality_flags():
    stale = station(10, age_min=180)
    humid = station(10, humidity=95)
    odd = station(30, pm10=20)
    verdicts = assess([stale, humid, odd], NOW, fallback_humidity=50)
    assert verdicts[stale.id].status == "stale" and verdicts[stale.id].weight == 0
    assert verdicts[humid.id].flags == ["high_humidity"] and verdicts[humid.id].weight == pytest.approx(0.3)
    assert "inconsistent" in verdicts[odd.id].flags


def test_outlier_is_excluded_but_reported():
    group = [station(v, lon=27.59 + i * 0.001) for i, v in enumerate((10, 11, 12, 10.5))]
    rogue = station(400, lon=27.592)
    verdicts = assess([*group, rogue], NOW, fallback_humidity=None)
    assert verdicts[rogue.id].status == "outlier" and verdicts[rogue.id].weight == 0
    assert all(verdicts[s.id].status == "ok" for s in group)


# --- grid ------------------------------------------------------------------------------------------

LATTICE = {
    (lat, lon): 10.0 + (lat - 47.0) * 10
    for lat in (47.0, 47.1, 47.2, 47.3)
    for lon in (27.3, 27.5, 27.7, 27.9)
}


def test_bilinear_matches_lattice_and_interpolates():
    values = grid.bilinear(LATTICE, np.array([47.1, 47.15]), np.array([27.5, 27.6]))
    assert values == pytest.approx([11.0, 11.5])


def test_grid_is_background_far_from_stations_and_station_value_nearby():
    spec = grid.GridSpec.for_bounds(CITY)
    near = grid.StationInput(lat=47.1601, lon=27.5901, value=40.0, weight=1.0)
    layers = grid.compute(spec, {"pm25": LATTICE}, {"pm25": [near]})
    at_station = grid.sample(spec, layers["pm25"], 47.1601, 27.5901)
    far = grid.sample(spec, layers["pm25"], 47.06, 27.74)
    assert at_station == pytest.approx(40.0, abs=4)
    assert far == pytest.approx(10.0 + (47.06 - 47.0) * 10, abs=0.1)
    assert grid.sample(spec, layers["confidence"], 47.06, 27.74) == 0
    assert grid.sample(spec, layers["eaqi"], 47.1601, 27.5901) == 3


def test_forecast_correction_fades():
    spec = grid.GridSpec.for_bounds(CITY)
    near = [grid.StationInput(47.16, 27.59, 40.0, 1.0)]
    now = grid.compute(spec, {"pm25": LATTICE}, {"pm25": near})
    later = grid.compute(spec, {"pm25": LATTICE}, {"pm25": near}, hours_ahead=12)
    background = 10.0 + 0.16 * 10
    assert (
        grid.sample(spec, later["pm25"], 47.16, 27.59) - background
        < (grid.sample(spec, now["pm25"], 47.16, 27.59) - background) * 0.2
    )


def test_grid_without_any_data_has_no_levels():
    spec = grid.GridSpec.for_bounds(CITY)
    layers = grid.compute(spec, {}, {})
    assert not np.any(layers["eaqi"])
    assert "pm25" not in layers


def test_pack_roundtrip():
    layers = {"pm25": np.array([[1.5, np.nan]]), "eaqi": np.array([[2.0, 0.0]])}
    restored = grid.unpack(grid.pack(layers))
    assert restored["pm25"][0, 0] == pytest.approx(1.5) and np.isnan(restored["pm25"][0, 1])


def test_best_window_picks_longest_run_at_best_level():
    start = datetime(2026, 11, 10, 6, tzinfo=UTC)
    levels = [3, 2, 2, 3, 2, 2, 2, 4]
    window = best_window([(start + timedelta(hours=i), level) for i, level in enumerate(levels)])
    assert window == (start + timedelta(hours=4), start + timedelta(hours=7), 2)
    assert best_window([(start, None)]) is None
