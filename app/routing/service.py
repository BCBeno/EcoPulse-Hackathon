"""Three routes (cleanest, balanced, fastest) for walking or cycling, scored on the pollution grid."""

import asyncio
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, Field
from shapely.geometry import LineString, Point, shape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.air import service as air
from app.air.models import OsmFeature
from app.config import Settings
from app.errors import FieldError, invalid, outside_city
from app.geo import GeoLineString, LatLon, to_latlon
from app.reports.categories import CATEGORIES
from app.reports.models import Report
from app.reports.service import active_filter
from app.routing import areas, graphhopper
from app.routing.exposure import REPORT_BOOST, AreaBoost, Boost, Candidate, choose, measure, segments
from app.routing.graphhopper import GhPath
from app.traffic import service as traffic

MIN_DISTANCE_M = 100
MAX_DISTANCE_M = 30_000
MAX_WAYPOINTS = 8
LOW_CONFIDENCE_WARNING = 0.5
REPORT_MARGIN_M = 25
# Real-time traffic only matters for trips starting about now.
TRAFFIC_WINDOW = timedelta(hours=1)
MAX_TRAFFIC_WARNINGS = 2
TRAVEL_MODES = {"walk": "walking", "bike": "bicycling"}


class Endpoint(LatLon):
    label: str | None = Field(default=None, max_length=120)


class RouteRequest(BaseModel):
    origin: Endpoint
    destination: Endpoint
    mode: Literal["walk", "bike"] = "walk"
    depart_at: AwareDatetime | None = None


class RouteExposure(BaseModel):
    index: float
    avg_pm25: float | None
    max_eaqi_level: int | None
    minutes_poor_or_worse: float
    vs_fastest_pct: int | None


class Segment(BaseModel):
    from_: int = Field(alias="from", serialization_alias="from")
    to: int
    eaqi_level: int | None

    model_config = {"populate_by_name": True}


class RouteOut(BaseModel):
    id: str
    kinds: list[Literal["cleanest", "balanced", "fastest"]]
    distance_m: int
    duration_s: int
    geometry: GeoLineString
    exposure: RouteExposure
    segments: list[Segment]
    reports_nearby: list[UUID]
    warnings: list[str]
    google_maps_url: str


class RoutesOut(BaseModel):
    depart_at: datetime
    mode: Literal["walk", "bike"]
    routes: list[RouteOut]
    summary: str
    notes: list[str]


def distance_m(a: LatLon, b: LatLon) -> float:
    x = math.radians(b.lon - a.lon) * math.cos(math.radians((a.lat + b.lat) / 2))
    return 6_371_000 * math.hypot(x, math.radians(b.lat - a.lat))


def validate(request: RouteRequest, settings: Settings, now: datetime) -> datetime:
    for name, point in (("origin", request.origin), ("destination", request.destination)):
        if not settings.city_bbox.contains(point.lat, point.lon):
            error = outside_city()
            error.fields = [FieldError(field=name, message=error.message)]
            raise error
    span = distance_m(request.origin, request.destination)
    if span < MIN_DISTANCE_M:
        raise invalid("destination", "Destinația e prea aproape de plecare (sub 100 m).")
    if span > MAX_DISTANCE_M:
        raise invalid(
            "destination", "Distanța e prea mare pentru un traseu pe jos sau cu bicicleta (peste 30 km)."
        )
    depart = request.depart_at or now
    if depart < now - timedelta(minutes=15) or depart > now + timedelta(hours=settings.forecast_horizon_h):
        raise invalid(
            "depart_at",
            f"Plecarea trebuie să fie între acum și următoarele {settings.forecast_horizon_h} de ore.",
        )
    return depart


def google_maps_url(path: GhPath, mode: str) -> str:
    coords = path.coordinates
    inner = coords[1:-1]
    step = max(1, len(inner) // (MAX_WAYPOINTS + 1))
    waypoints = inner[step::step][:MAX_WAYPOINTS]
    params = {
        "api": 1,
        "origin": f"{coords[0][1]:.6f},{coords[0][0]:.6f}",
        "destination": f"{coords[-1][1]:.6f},{coords[-1][0]:.6f}",
        "travelmode": TRAVEL_MODES[mode],
    }
    if waypoints:
        params["waypoints"] = "|".join(f"{lat:.6f},{lon:.6f}" for lon, lat in waypoints)
    return "https://www.google.com/maps/dir/?" + urlencode(params, safe=",|")


async def _penalties(
    session: AsyncSession, result: air.GridResult, now: datetime
) -> tuple[dict, list[Report]]:
    masks = areas.hotspot_masks(result.layers.get("pm25"), result.layers["eaqi"])
    tiers: dict[str, list] = {name: areas.mask_polygons(result.spec, mask) for name, mask in masks.items()}
    reports = list(await session.scalars(select(Report).where(active_filter(now))))
    air_circles, obstacle_circles = [], []
    for report in reports:
        where = to_latlon(report.location)
        affects = CATEGORIES[report.category].affects
        circle = areas.circle(where.lat, where.lon, report.radius_m)
        if affects in ("air", "air_obstacle"):
            air_circles.append(circle)
        if affects in ("obstacle", "air_obstacle"):
            obstacle_circles.append(areas.circle(where.lat, where.lon, min(report.radius_m, 60)))
    tiers["report_air"] = areas.union_tier(air_circles)
    tiers["obstacle"] = areas.union_tier(obstacle_circles)
    rows = await session.scalars(select(func.ST_AsGeoJSON(func.ST_Simplify(OsmFeature.geometry, 0.0001))))
    construction = [p for row in rows for p in areas.buffered(shape(json.loads(row)), 30)]
    tiers["construction"] = areas.union_tier(construction)
    return tiers, reports


@dataclass
class CongestedRoad:
    polygons: list
    extra: float
    label: str


async def _congestion(session: AsyncSession, now: datetime, depart: datetime) -> list[CongestedRoad]:
    if abs(depart - now) > TRAFFIC_WINDOW:
        return []
    heavy = next(level for level in traffic.LEVELS if level.key == "heavy")
    roads = []
    for row in await traffic.current(session, now, min_jam=heavy.min_jam):
        key = traffic.level(row.jam_factor, row.closed).key
        polygons = areas.buffered(shape(row.geometry), traffic.NEAR_ROAD_M)
        extra = traffic.POLLUTION_BOOST.get(key, traffic.POLLUTION_BOOST["jammed"])
        roads.append(CongestedRoad(polygons, extra, row.description or "un drum aglomerat"))
    return roads


def _roads_near(path: GhPath, roads: list[CongestedRoad]) -> list[str]:
    line = LineString(path.coordinates)
    names: list[str] = []
    for road in roads:
        if road.label not in names and any(polygon.intersects(line) for polygon in road.polygons):
            names.append(road.label)
    return names


def _boost(report: Report) -> Boost:
    where = to_latlon(report.location)
    return Boost(where.lat, where.lon, report.radius_m, REPORT_BOOST[report.severity])


def _reports_near(path: GhPath, reports: list[Report]) -> list[Report]:
    line = LineString(path.coordinates)
    k = math.cos(math.radians(line.centroid.y))
    metric = LineString([(x * k * 111_320, y * 111_320) for x, y in line.coords])
    near = []
    for report in reports:
        where = to_latlon(report.location)
        point = Point(where.lon * k * 111_320, where.lat * 111_320)
        if metric.distance(point) <= report.radius_m + REPORT_MARGIN_M:
            near.append(report)
    return near


async def plan(
    session: AsyncSession,
    client: httpx.AsyncClient,
    settings: Settings,
    request: RouteRequest,
    cache: air.ForecastCache,
) -> RoutesOut:
    now = datetime.now(UTC)
    depart = validate(request, settings, now)
    grid = await air.grid_for(session, settings, None if request.depart_at is None else depart, cache)
    tiers, reports = await _penalties(session, grid, now)
    roads = await _congestion(session, now, depart)
    tiers["traffic"] = areas.union_tier([p for road in roads for p in road.polygons])
    origin = (request.origin.lat, request.origin.lon)
    destination = (request.destination.lat, request.destination.lon)
    calls = [
        graphhopper.route(
            client, settings.graphhopper_url, request.mode, origin, destination, alternatives=True
        )
    ]
    for kind in ("balanced", "cleanest"):
        model = areas.custom_model(kind, tiers)
        if model is not None:
            calls.append(
                graphhopper.route(
                    client,
                    settings.graphhopper_url,
                    request.mode,
                    origin,
                    destination,
                    custom_model=model,
                    alternatives=True,
                )
            )
    results = await asyncio.gather(*calls, return_exceptions=True)
    if isinstance(results[0], BaseException):
        raise results[0]  # without the plain route there is nothing to compare against
    paths = [path for result in results if not isinstance(result, BaseException) for path in result]
    if not paths:
        raise graphhopper.no_route()
    boosts = tuple(
        _boost(report) for report in reports if CATEGORIES[report.category].affects in ("air", "air_obstacle")
    )
    area_boosts = tuple(AreaBoost(p, road.extra) for road in roads for p in road.polygons)
    candidates = [
        Candidate(path, measure(path, grid.spec, grid.layers, boosts, area_boosts)) for path in paths
    ]
    chosen = choose(candidates)
    fastest = next(c for c in chosen if "fastest" in c.kinds)
    routes = [_route_out(f"r{i + 1}", c, fastest, reports, roads, request.mode) for i, c in enumerate(chosen)]
    return RoutesOut(
        depart_at=depart,
        mode=request.mode,
        routes=routes,
        summary=_summary(chosen, fastest),
        notes=_notes(grid, traffic_applied=abs(depart - now) <= TRAFFIC_WINDOW),
    )


def _route_out(
    route_id: str,
    candidate: Candidate,
    fastest: Candidate,
    reports: list[Report],
    roads: list[CongestedRoad],
    mode: str,
) -> RouteOut:
    exposure, path = candidate.exposure, candidate.path
    near = _reports_near(path, reports)
    warnings = []
    for report in near:
        if CATEGORIES[report.category].affects in ("air", "air_obstacle"):
            warnings.append(f"Trece pe lângă un raport activ: {CATEGORIES[report.category].label.lower()}.")
    for name in _roads_near(path, roads)[:MAX_TRAFFIC_WARNINGS]:
        warnings.append(f"Trece pe lângă trafic intens: {name}.")
    if exposure.low_confidence_share > LOW_CONFIDENCE_WARNING:
        warnings.append("Date puține pe o porțiune a traseului: estimarea se bazează mai mult pe model.")
    vs_fastest = (
        round((exposure.index - fastest.exposure.index) / fastest.exposure.index * 100)
        if fastest.exposure.index > 0 and candidate is not fastest
        else None
    )
    return RouteOut(
        id=route_id,
        kinds=candidate.kinds,
        distance_m=round(path.distance_m),
        duration_s=round(path.duration_s),
        geometry={
            "type": "LineString",
            "coordinates": [[round(x, 6), round(y, 6)] for x, y in path.coordinates],
        },
        exposure=RouteExposure(
            index=round(exposure.index, 2),
            avg_pm25=round(exposure.avg_pm25, 1) if exposure.avg_pm25 is not None else None,
            max_eaqi_level=exposure.max_level,
            minutes_poor_or_worse=round(exposure.minutes_poor_or_worse, 1),
            vs_fastest_pct=vs_fastest,
        ),
        segments=[
            Segment(from_=a, to=b, eaqi_level=level) for a, b, level in segments(exposure.vertex_levels)
        ],
        reports_nearby=[r.id for r in near],
        warnings=list(dict.fromkeys(warnings)),
        google_maps_url=google_maps_url(path, mode),
    )


def _summary(chosen: list[Candidate], fastest: Candidate) -> str:
    cleanest = next(c for c in chosen if "cleanest" in c.kinds)
    if cleanest is fastest:
        return "Aerul e similar pe toate variantele: traseul cel mai rapid e și cel mai curat."
    gain = (
        round((1 - cleanest.exposure.index / fastest.exposure.index) * 100) if fastest.exposure.index else 0
    )
    extra = max(0, round((cleanest.path.duration_s - fastest.path.duration_s) / 60))
    minutes = "un minut" if extra == 1 else f"{extra} minute"
    return f"Traseul cel mai curat are cu {gain} % mai puține particule, pentru {minutes} în plus."


def _notes(grid: air.GridResult, *, traffic_applied: bool) -> list[str]:
    notes = []
    if grid.kind == "forecast":
        notes.append("Pentru plecare mai târziu, scorul folosește prognoza calității aerului.")
    if not traffic_applied:
        notes.append("Traficul în timp real contează doar pentru plecările din următoarea oră.")
    if "pm25" not in grid.layers:
        notes.append("Nu avem date despre aer acum; traseele sunt ordonate doar după timp.")
    return notes
