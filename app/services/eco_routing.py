"""The three routes of the map tab (fastest, cleanest, balanced), computed on the server.

1. OSRM (routing.openstreetmap.de, no key) proposes candidates: its own alternatives plus routes forced
   through via points to the side of the straight line and through the cleanest / least congested areas.
2. Each candidate is cut into its OSRM segments and scored on:
   - pollution: AQI interpolated (IDW) between current sensors, the same values as the map heatmap;
   - traffic: estimated congestion of the major road under the segment (app.services.traffic). For cars it
     lengthens the travel time; for every mode it raises exposure, since busy roads are more polluted;
   - obstacles: construction zones and open community reports the route passes through.
3. fastest = least travel time, cleanest = least exposure (x1.3 per obstacle),
   balanced = best compromise of the two, preferring a route strictly between them.

Scores are relative heuristics for ranking routes, not measured personal exposure.
"""

import asyncio
import math
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from shapely.geometry import LineString, Point, Polygon, box, shape
from sqlalchemy import select

from app.integrations.http import ProviderError
from app.models.entities import MappedFeature, Observation, PlannedWork, Report, RoadSegment
from app.services.scoring import aware, projected
from app.services.traffic import (
    DEMAND_SOURCE,
    LEVEL_RANK,
    TrafficModel,
    demand_at,
    level,
    time_factor,
)

OSRM = {
    "foot": "https://routing.openstreetmap.de/routed-foot/route/v1/driving",
    "bike": "https://routing.openstreetmap.de/routed-bike/route/v1/driving",
    "car": "https://routing.openstreetmap.de/routed-car/route/v1/driving",
}
GOOGLE_MODE = {"foot": "walking", "bike": "bicycling", "car": "driving"}
ISSUE_RADIUS_M = 100
TRAFFIC_EXPOSURE = 0.4  # exposure x1.4 on a fully congested road
OBSTACLE_PENALTY = 0.3
MAX_DETOUR = 1.6

# US EPA breakpoints [concentration, AQI], NO2 in µg/m³: identical to the frontend map.
BREAKPOINTS = {
    "pm25": ((9, 50), (35.4, 100), (55.4, 150), (125.4, 200), (225.4, 300)),
    "pm10": ((54, 50), (154, 100), (254, 150), (354, 200), (424, 300)),
    "no2": ((100, 50), (188, 100), (677, 150), (1220, 200), (2349, 300)),
}


def sub_index(parameter, value):
    low = (0, 0)
    for high in BREAKPOINTS[parameter]:
        if value <= high[0]:
            return low[1] + (value - low[0]) / (high[0] - low[0]) * (high[1] - low[1])
        low = high
    return 300


def haversine(a, b):
    """Metres between two (lat, lng) points."""
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- map context


def fresh(observation, now):
    if observation.category == "MODEL_FORECAST":
        return (now - aware(observation.retrieved_at)).total_seconds() <= 21600
    return (now - aware(observation.observed_at)).total_seconds() <= 7200


def sensors_from(observations, now):
    """Current AQI per sensor location, as the map shows it (ground sensors first, CAMS as fallback)."""
    measured = [
        o
        for o in observations
        if o.value is not None
        and o.parameter in BREAKPOINTS
        and o.unit == "µg/m³"
        and o.category != "WEATHER_CONTEXT"
        and fresh(o, now)
    ]
    ground = [o for o in measured if o.category != "MODEL_FORECAST"]
    groups = {}
    for o in ground or measured:
        groups.setdefault((o.provider, round(o.latitude, 4), round(o.longitude, 4)), []).append(o)
    sensors = []
    for (_, lat, lng), items in groups.items():
        latest = {}
        for o in items:
            age = abs((aware(o.observed_at) - now).total_seconds())
            if o.parameter not in latest or age < latest[o.parameter][0]:
                latest[o.parameter] = (age, o)
        aqi = round(max(sub_index(p, o.value) for p, (_, o) in latest.items()))
        sensors.append({"lat": items[0].latitude, "lng": items[0].longitude, "aqi": aqi})
    return sensors


def aqi_at(point, sensors):
    """IDW interpolation (power 3), as the frontend heatmap; 50 when there is no sensor."""
    num = den = 0.0
    for sensor in sensors:
        distance = max(haversine(point, (sensor["lat"], sensor["lng"])), 60)
        if distance < 80:
            return sensor["aqi"]
        weight = 1 / distance**3
        num += weight * sensor["aqi"]
        den += weight
    return num / den if den else 50


def feature_zone(feature, latitude):
    """Construction footprint as drawn on the map: polygon, ~20 m corridor or ~50 m square."""
    geometry = feature.geometry or {}
    tags = feature.tags or {}
    if geometry.get("type") == "LineString" and len(geometry.get("coordinates") or []) >= 2:
        line = projected(shape(geometry), latitude)
        coordinates = geometry["coordinates"]
        if (
            len(coordinates) >= 4
            and coordinates[0] == coordinates[-1]
            and tags.get("highway") != "construction"
        ):
            polygon = Polygon(line.coords)
            if polygon.is_valid and polygon.area > 0:
                return polygon
        return line.buffer(10, cap_style="flat")
    point = projected(Point(feature.longitude, feature.latitude), latitude)
    return box(point.x - 25, point.y - 25, point.x + 25, point.y + 25)


def feature_title(tags):
    tags = tags or {}
    what = (
        "Drum în lucru"
        if tags.get("highway") == "construction"
        else "Construcție"
        if tags.get("building") or tags.get("construction") == "yes"
        else "Șantier"
    )
    return f"{what} · {tags['name']}" if tags.get("name") else what


async def load_context(db, now, latitude):
    window = (
        ("LOW_COST_SENSOR_MEASUREMENT", timedelta(hours=-2), timedelta(minutes=15)),
        ("OFFICIAL_MEASUREMENT", timedelta(hours=-3), timedelta(minutes=15)),
        ("MODEL_FORECAST", timedelta(hours=-1.5), timedelta(hours=1.5)),
    )
    observations = []
    for category, before, after in window:
        observations += (
            await db.scalars(
                select(Observation).where(
                    Observation.category == category,
                    Observation.observed_at >= now + before,
                    Observation.observed_at <= now + after,
                )
            )
        ).all()
    reports = [
        r
        for r in (await db.scalars(select(Report).where(Report.status.in_(["pending", "confirmed"])))).all()
        if aware(r.expires_at) > now
    ]
    features = (await db.scalars(select(MappedFeature))).all()
    works = (
        await db.scalars(
            select(PlannedWork).where(
                PlannedWork.verification == "verified",
                PlannedWork.status.in_(["scheduled", "active"]),
                PlannedWork.ends_at >= now,
                PlannedWork.latitude.is_not(None),
                PlannedWork.longitude.is_not(None),
            )
        )
    ).all()
    roads = (await db.scalars(select(RoadSegment))).all()

    zones = [(f.id, feature_zone(f, latitude), feature_title(f.tags)) for f in features]
    for work in works:
        point = projected(Point(work.longitude, work.latitude), latitude)
        zones.append((work.id, box(point.x - 40, point.y - 40, point.x + 40, point.y + 40), work.title))
    issues = [
        (r.id, projected(Point(r.longitude, r.latitude), latitude), r.category, r.description)
        for r in reports
    ]
    return {
        "sensors": sensors_from(observations, now),
        "zones": zones,
        "issues": issues,
        "traffic_reports": [r for r in reports if r.category == "traffic"],
        "roads": roads,
    }


def traffic_model(context, when, latitude, timezone):
    return TrafficModel(
        context["roads"],
        demand_at(when, timezone),
        latitude,
        context["traffic_reports"],
        [(zone_id, polygon) for zone_id, polygon, _ in context["zones"]],
    )


# ---------------------------------------------------------------- OSRM


async def osrm_routes(http, profile, points, alternatives):
    coordinates = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in points)
    data = await http.get_json(
        f"{OSRM[profile]}/{coordinates}",
        {
            "overview": "full",
            "geometries": "geojson",
            "alternatives": "3" if alternatives else "false",
            "steps": "false",
            "annotations": "duration",
        },
        ttl=300,
    )
    if not isinstance(data, dict) or data.get("code") != "Ok":
        raise ProviderError("no_route_found")
    routes = []
    for route in data.get("routes") or []:
        try:
            coords = [(float(lat), float(lng)) for lng, lat in route["geometry"]["coordinates"]]
            durations = [float(x) for leg in route.get("legs", []) for x in leg["annotation"]["duration"]]
        except (KeyError, TypeError, ValueError):
            durations = []
            coords = []
        if len(coords) >= 2 and float(route.get("duration", 0)) > 0 and float(route.get("distance", 0)) > 0:
            routes.append(
                {
                    "coords": coords,
                    "distance": float(route["distance"]),
                    "duration": float(route["duration"]),
                    "segment_durations": durations if len(durations) == len(coords) - 1 else None,
                }
            )
    return routes


def perpendicular_offset(a, b, factor):
    """Point beside the middle of a -> b, `factor` x |ab| to the left (negative: right)."""
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    k = math.cos(math.radians(mid[0]))
    dx, dy = (b[1] - a[1]) * k, b[0] - a[0]
    return (mid[0] + dx * factor, mid[1] - dy * factor / k)


def via_candidates(a, b, context, model, latitude):
    vias = [perpendicular_offset(a, b, f) for f in (0.22, -0.22, 0.4, -0.4)]
    total = haversine(a, b)
    half = ((b[0] - a[0]) / 2, (b[1] - a[1]) / 2)
    scored = []
    for i in range(1, 14):
        t = i / 14
        base = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        for j in range(-6, 7):
            p = perpendicular_offset(
                (base[0] - half[0], base[1] - half[1]), (base[0] + half[0], base[1] + half[1]), j * 0.07
            )
            if haversine(a, p) + haversine(p, b) > total * 1.45:
                continue
            # Clean air first; heavy traffic nearby makes a point less attractive.
            point = projected(Point(p[1], p[0]), latitude)
            congestion = 0.0
            if model.tree is not None:
                near = model.tree.query(point, predicate="dwithin", distance=150)
                congestion = max((model.values[k] for k in near), default=0.0)
            scored.append((aqi_at(p, context["sensors"]) * (1 + 0.5 * congestion), p))
    scored.sort(key=lambda x: x[0])
    picked = []
    for _, p in scored:
        if all(haversine(q, p) > total * 0.25 for q in picked):
            picked.append(p)
        if len(picked) == 2:
            break
    return picked + vias


# ---------------------------------------------------------------- scoring


def evaluate(route, profile, context, model, latitude, route_id):
    coords = route["coords"]
    projected_points = [projected(Point(lng, lat), latitude).coords[0] for lat, lng in coords]
    lengths = [haversine(p, q) for p, q in zip(coords, coords[1:])]
    total_length = sum(lengths) or 1
    raw = route["segment_durations"]
    if raw and sum(raw) > 0:
        # Segment annotations leave out turn penalties: scale them to the route total.
        scale = route["duration"] / sum(raw)
        free_flow = [d * scale for d in raw]
    else:
        free_flow = [route["duration"] * length / total_length for length in lengths]

    exposure = weighted = duration = 0.0
    congested_m = 0.0
    congestion_weighted = 0.0
    worst = None
    local = model.local_congestion()
    for i, length in enumerate(lengths):
        if length == 0:
            duration += free_flow[i]
            continue
        a, b = coords[i], coords[i + 1]
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        aqi = aqi_at(mid, context["sensors"])
        road = model.at_segment(projected_points[i], projected_points[i + 1])
        congestion = local if road is None else road
        exposure += aqi * length / 1000 * (1 + TRAFFIC_EXPOSURE * congestion)
        weighted += aqi * length
        congestion_weighted += congestion * length
        if road is not None:
            if LEVEL_RANK[level(road)] >= LEVEL_RANK["aglomerat"]:
                congested_m += length
            worst = road if worst is None else max(worst, road)
        duration += free_flow[i] * (time_factor(congestion) if profile == "car" else 1)

    line = LineString(projected_points)
    obstacles = [
        {"id": zone_id, "kind": "construction", "title": title}
        for zone_id, polygon, title in context["zones"]
        if line.intersects(polygon)
    ]
    obstacles += [
        {"id": issue_id, "kind": "issue", "category": category, "title": description[:120]}
        for issue_id, point, category, description in context["issues"]
        if line.distance(point) < ISSUE_RADIUS_M
    ]
    return {
        "id": route_id,
        "coordinates": [[round(lat, 6), round(lng, 6)] for lat, lng in coords],
        "distance_m": round(route["distance"], 1),
        "duration_s": round(duration, 1),
        "free_flow_duration_s": round(route["duration"], 1),
        "traffic_delay_s": round(duration - route["duration"], 1),
        "avg_aqi": round(weighted / total_length, 1),
        "exposure": round(exposure, 3),
        "obstacles": len(obstacles),
        "obstacle_details": obstacles,
        "traffic": {
            "avg_congestion": round(congestion_weighted / total_length, 3),
            "congested_m": round(congested_m),
            "worst_level": level(worst) if worst is not None else None,
        },
    }


def eco_cost(route):
    return route["exposure"] * (1 + OBSTACLE_PENALTY * route["obstacles"])


def duplicate(a, b):
    return (
        abs(a["distance_m"] - b["distance_m"]) / b["distance_m"] < 0.02
        and abs(a["duration_s"] - b["duration_s"]) / b["duration_s"] < 0.02
    )


def choose(candidates):
    fastest = min(candidates, key=lambda c: c["duration_s"])
    cleanest = min(candidates, key=eco_cost)
    min_duration = fastest["duration_s"] or 1
    min_cost = eco_cost(cleanest) or 1

    def balanced_score(c):
        return 0.5 * c["duration_s"] / min_duration + 0.5 * eco_cost(c) / min_cost

    # A real compromise: cleaner than the fastest and faster than the cleanest. Otherwise the best mix.
    between = [
        c
        for c in candidates
        if c is not fastest
        and c is not cleanest
        and c["duration_s"] <= cleanest["duration_s"]
        and eco_cost(c) <= eco_cost(fastest)
    ]
    balanced = min(between or candidates, key=balanced_score)
    return {"fastest": fastest, "cleanest": cleanest, "balanced": balanced}


# ---------------------------------------------------------------- Google Maps


def shape_waypoints(coords, count=3):
    """Interior points that best preserve the route shape (top-down Douglas-Peucker), in route order."""
    if len(coords) < 3:
        return []
    latitude = coords[0][0]
    points = [projected(Point(lng, lat), latitude).coords[0] for lat, lng in coords]
    total = sum(math.dist(p, q) for p, q in zip(points, points[1:])) or 1
    along, running = [0.0], 0.0
    for p, q in zip(points, points[1:]):
        running += math.dist(p, q)
        along.append(running / total)
    chosen = [0, len(points) - 1]
    for _ in range(count):
        best = None
        for left, right in zip(chosen, chosen[1:]):
            chord = LineString([points[left], points[right]]) if points[left] != points[right] else None
            for k in range(left + 1, right):
                # Points next to the ends add nothing and can snap Google to the wrong side street.
                if not 0.08 <= along[k] <= 0.92:
                    continue
                d = chord.distance(Point(points[k])) if chord else math.dist(points[k], points[left])
                if best is None or d > best[0]:
                    best = (d, k)
        if best is None or best[0] < 30:
            break
        chosen = sorted({*chosen, best[1]})
    if len(chosen) == 2:
        # Straight route: keep the middle so Google does not pick a different street.
        chosen = [0, len(points) // 2, len(points) - 1]
    return [coords[k] for k in chosen[1:-1]]


def google_maps_url(coords, profile):
    """Google Maps directions link that follows the route (max 3 waypoints: the mobile app limit)."""

    def fmt(p):
        return f"{p[0]:.6f},{p[1]:.6f}"

    params = {
        "api": "1",
        "origin": fmt(coords[0]),
        "destination": fmt(coords[-1]),
        "travelmode": GOOGLE_MODE[profile],
    }
    waypoints = shape_waypoints(coords)
    if waypoints:
        params["waypoints"] = "|".join(fmt(p) for p in waypoints)
    return "https://www.google.com/maps/dir/?" + urlencode(params, safe=",|")


# ---------------------------------------------------------------- entry point


async def compute_routes(db, http, settings, request):
    now = datetime.now(UTC)
    when = request.departure_at
    latitude = settings.city_latitude
    origin = (request.origin.latitude, request.origin.longitude)
    destination = (request.destination.latitude, request.destination.longitude)
    context = await load_context(db, now, latitude)
    model = traffic_model(context, when, latitude, settings.city_timezone)

    requests = [osrm_routes(http, request.profile, [origin, destination], True)]
    requests += [
        osrm_routes(http, request.profile, [origin, via, destination], False)
        for via in via_candidates(origin, destination, context, model, latitude)
    ]
    settled = await asyncio.gather(*requests, return_exceptions=True)
    unexpected = [r for r in settled if isinstance(r, BaseException) and not isinstance(r, ProviderError)]
    if unexpected:
        raise unexpected[0]
    routes = [route for result in settled if not isinstance(result, BaseException) for route in result]
    if not routes:
        failure = next((r for r in settled if isinstance(r, ProviderError)), None)
        raise failure or ProviderError("no_route_found")

    shortest = min(r["duration"] for r in routes)
    candidates = []
    for index, route in enumerate(routes):
        if route["duration"] > shortest * MAX_DETOUR:
            continue  # absurd detours forced by a via point
        scored = evaluate(route, request.profile, context, model, latitude, f"r{index}")
        if not any(duplicate(c, scored) for c in candidates):
            candidates.append(scored)
    chosen = choose(candidates)
    for route in candidates:
        route["google_maps_url"] = google_maps_url(route["coordinates"], request.profile)

    warnings = []
    if not context["sensors"]:
        warnings.append("Nu există măsurători recente de poluare; expunerea folosește o valoare implicită.")
    if not context["roads"]:
        warnings.append(
            "Rețeaua de drumuri nu a fost sincronizată încă; traficul se estimează doar din raportări."
        )
    if request.profile != "car":
        warnings.append("Traficul nu schimbă durata pe jos sau cu bicicleta, doar expunerea la poluare.")
    return {
        "profile": request.profile,
        "departure_at": when,
        "routes": chosen,
        "candidates": len(candidates),
        "traffic": {
            "source": "estimate",
            "demand": round(model.demand["value"], 3),
            "day_type": model.demand["day_type"],
            "demand_source": DEMAND_SOURCE,
            "roads": len(model.roads),
            "affects_duration": request.profile == "car",
        },
        "data": {
            "sensors": len(context["sensors"]),
            "construction_zones": len(context["zones"]),
            "issues": len(context["issues"]),
        },
        "warnings": warnings,
        "attribution": "Rutare © OpenStreetMap contributors / OSRM (routing.openstreetmap.de)",
    }


async def congestion(db, settings, when, minimum):
    now = datetime.now(UTC)
    context = await load_context(db, now, settings.city_latitude)
    model = traffic_model(context, when, settings.city_latitude, settings.city_timezone)
    return {
        "at": when,
        "source": "estimate",
        "demand": round(model.demand["value"], 3),
        "day_type": model.demand["day_type"],
        "demand_source": DEMAND_SOURCE,
        "roads": len(model.roads),
        "items": model.snapshot(minimum),
        "note": "Estimare relativă (clasa drumului, semafoare, benzi, profil orar NO la IS-1, raportări); "
        "nu este trafic măsurat în timp real.",
    }
