"""City traffic estimate for a given time: OSM road class x Iași time-of-day profile, corrected by
active community traffic reports and, when TOMTOM_API_KEY is set, by real-time TomTom speeds."""

import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.integrations.http import ProviderError
from app.integrations.traffic import OsmMajorRoadsProvider, TomTomFlowProvider
from app.models.entities import Report
from app.services.scoring import aware

# Share of a road class's peak congestion; links behave like the road they connect.
CLASS_WEIGHT = {"motorway": 0.55, "trunk": 0.85, "primary": 0.85, "secondary": 0.65, "tertiary": 0.4}
# A typical arterial road; the city-wide label describes that, not the worst boulevard.
CITY_WEIGHT = 0.75
# Hourly demand (0-23, local time) relative to the weekday peak.
WEEKDAY = (
    0.08, 0.05, 0.05, 0.05, 0.08, 0.15, 0.35, 0.75, 1.0, 0.8, 0.6, 0.6,
    0.65, 0.65, 0.7, 0.8, 0.95, 1.0, 0.9, 0.65, 0.45, 0.3, 0.2, 0.12,
)  # fmt: skip
WEEKEND = (
    0.1, 0.08, 0.05, 0.05, 0.05, 0.08, 0.1, 0.15, 0.22, 0.32, 0.4, 0.45,
    0.48, 0.48, 0.45, 0.42, 0.42, 0.42, 0.4, 0.35, 0.28, 0.2, 0.15, 0.12,
)  # fmt: skip
BANDS = (
    (0.3, "low", "fluent"),
    (0.55, "moderate", "moderat"),
    (0.8, "heavy", "aglomerat"),
    (2, "severe", "blocat"),
)
REPORT_RADIUS_M = 250
LIMITATION = "Estimare după tipul drumului și ora din zi; nu este o măsurătoare în timp real."


def band(level):
    return next((key, label) for limit, key, label in BANDS if level < limit)


def demand(at, timezone):
    local = aware(at).astimezone(ZoneInfo(timezone))
    profile = WEEKEND if local.weekday() >= 5 else WEEKDAY
    # Interpolate between hours so 08:30 differs from 08:00.
    hour = local.hour + local.minute / 60
    low, high = profile[int(hour) % 24], profile[(int(hour) + 1) % 24]
    return low + (high - low) * (hour - int(hour))


def meters(a, b):
    """Equirectangular distance between [lon, lat] points; accurate enough within a city."""
    x = math.radians(b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
    y = math.radians(b[1] - a[1])
    return 6371000 * math.hypot(x, y)


def length_m(points):
    return sum(meters(a, b) for a, b in zip(points, points[1:]))


def segment(road, level, source, **extra):
    key, label = band(level)
    return {
        "id": road["id"],
        "name": road["name"],
        "road_class": road["highway"],
        "level": round(level, 2),
        "band": key,
        "label": label,
        "source": source,
        "coordinates": road["coordinates"],
        **extra,
    }


CLASS_PRIORITY = {"trunk": 0, "primary": 0, "motorway": 1, "secondary": 1, "tertiary": 2}
TOMTOM_MATCH_M = 30


def distance_to_line(point, line):
    """Metres from a [lon, lat] point to a [lon, lat] polyline (local projection)."""
    k = math.cos(math.radians(point[1])) * 111320
    best = math.inf
    for a, b in zip(line, line[1:]):
        ax, ay = (a[0] - point[0]) * k, (a[1] - point[1]) * 111320
        bx, by = (b[0] - point[0]) * k, (b[1] - point[1]) * 111320
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / length)) if length else 0.0
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def flow_level(flow):
    if flow["closed"]:
        return 1.0
    ratio = flow["current_speed_kmh"] / flow["free_flow_speed_kmh"] if flow["free_flow_speed_kmh"] else 1
    # Driving at a quarter of free-flow speed or less counts as blocked.
    return min(1.0, max(0.0, (1 - ratio) / 0.75))


async def apply_tomtom(segments, settings, http):
    """Measure distinct main roads with TomTom, spread each answer over the stretch it describes and
    rescale the remaining estimates by how busy TomTom finds the city. Returns that scale, or None."""
    provider = TomTomFlowProvider(settings, http)
    longest = {}
    for item in segments:
        size = length_m(item["coordinates"])
        if size >= 200:
            key = item["name"] or item["id"]
            if key not in longest or size > longest[key][0]:
                longest[key] = (size, item)
    candidates = sorted(
        (item for _, item in longest.values()),
        key=lambda s: (
            CLASS_PRIORITY.get(s["road_class"].removesuffix("_link"), 3),
            -length_m(s["coordinates"]),
        ),
    )
    estimated, measured = [], []
    for item in candidates:
        if len(measured) >= settings.tomtom_max_segments:
            break
        if item["source"] == "tomtom":  # already covered by an earlier stretch
            continue
        middle = item["coordinates"][len(item["coordinates"]) // 2]
        try:
            flow = await provider.flow(middle[1], middle[0])
        except ProviderError:
            break
        level = flow_level(flow)
        measured.append(level)
        estimated.append(item["level"])
        stretch = flow["coordinates"] or item["coordinates"]
        extra = {
            "current_speed_kmh": flow["current_speed_kmh"],
            "free_flow_speed_kmh": flow["free_flow_speed_kmh"],
        }
        for other in segments:
            # A road lies along the stretch only if its ends and middle do; crossing streets don't.
            ends = (
                other["coordinates"][0],
                other["coordinates"][len(other["coordinates"]) // 2],
                other["coordinates"][-1],
            )
            if other is item or (
                other["source"] == "profile"
                and all(distance_to_line(p, stretch) <= TOMTOM_MATCH_M for p in ends)
            ):
                other.update(segment(other | {"highway": other["road_class"]}, level, "tomtom", **extra))
    if not measured:
        return None
    scale = min(1.5, max(0.15, sum(measured) / sum(estimated))) if sum(estimated) else 1.0
    for item in segments:
        if item["source"] == "profile":
            item.update(
                segment(
                    item | {"highway": item["road_class"]},
                    min(1.0, item["level"] * scale),
                    "profile_calibrated",
                )
            )
    return scale


async def traffic_at(db, at, settings, http, now=None):
    now = now or datetime.now(UTC)
    factor = demand(at, settings.city_timezone)
    try:
        roads = await OsmMajorRoadsProvider(settings, http).roads()
    except ProviderError:
        # Without road geometry the city-wide time profile is still a useful answer.
        roads = []
    segments = [
        segment(road, min(1.0, CLASS_WEIGHT[road["highway"].removesuffix("_link")] * factor), "profile")
        for road in roads
    ]
    sources = ["osm_time_profile"] if roads else ["time_profile"]

    city = factor * CITY_WEIGHT
    # Real-time speeds only describe the present; never apply them to a future departure.
    if settings.tomtom_api_key and segments and abs((aware(at) - now).total_seconds()) <= 3600:
        scale = await apply_tomtom(segments, settings, http)
        if scale is not None:
            sources.append("tomtom_realtime")
            city *= scale

    reports = (
        await db.scalars(
            select(Report).where(
                Report.category == "traffic",
                Report.status.in_(["pending", "confirmed"]),
                Report.observed_at <= at,
                Report.expires_at > at,
            )
        )
    ).all()
    if reports:
        sources.append("community_reports")
    for report in reports:
        here = [report.longitude, report.latitude]
        for item in segments:
            if item["level"] < 0.85 and any(meters(here, p) <= REPORT_RADIUS_M for p in item["coordinates"]):
                item.update(segment(item | {"highway": item["road_class"]}, 0.85, "community_report"))

    return {
        "at": aware(at).isoformat(),
        "city_level": round(city, 2),
        "city_band": band(city)[0],
        "city_label": band(city)[1],
        "sources": sources,
        "segments": segments,
        "attribution": "© OpenStreetMap contributors (ODbL)"
        + (", TomTom Traffic" if "tomtom_realtime" in sources else ""),
        "limitation": LIMITATION,
    }


def nearby(traffic, longitude, latitude, radius_m=800, limit=3):
    """The most congested named roads near a point, for the assistant's answer."""
    here = [longitude, latitude]
    close = [
        s
        for s in traffic["segments"]
        if s["name"] and any(meters(here, p) <= radius_m for p in s["coordinates"])
    ]
    worst = {}
    for s in sorted(close, key=lambda s: -s["level"]):
        worst.setdefault(s["name"], s)
    return list(worst.values())[:limit]
