"""Build precautionary scenarios from actual incidents and verify the returned road geometries."""

import math
from datetime import UTC, datetime, timedelta

from shapely.geometry import LineString, Point

from app.services.scoring import aware, projected


def incident_zones(reports, works, departure, radius_m):
    zones = []
    departure = aware(departure)
    for report in reports:
        if report.status not in {"pending", "confirmed"} or not (
            aware(report.observed_at) <= departure < aware(report.expires_at)
        ):
            continue
        zones.append(
            {
                "id": f"report:{report.id}",
                "record_id": report.id,
                "category": "COMMUNITY_REPORT",
                "incident_category": report.category,
                "latitude": report.latitude,
                "longitude": report.longitude,
                "radius_m": radius_m,
                "status": report.status,
                "weight": report.severity * (1 if report.status == "confirmed" else 0.6),
                "timestamp": aware(report.observed_at).isoformat(),
                "qualification": "Community observation; not a confirmed road closure",
            }
        )
    for work in works:
        if (
            work.verification != "verified"
            or work.status not in {"active", "scheduled"}
            or work.latitude is None
            or work.longitude is None
            or not work.starts_at
            or not work.ends_at
            or not work.last_verified_at
        ):
            continue
        if not (aware(work.starts_at) <= departure <= aware(work.ends_at)) or datetime.now(UTC) - aware(
            work.last_verified_at
        ) > timedelta(days=7):
            continue
        zones.append(
            {
                "id": f"work:{work.id}",
                "record_id": work.id,
                "category": "OFFICIAL_PLANNED_WORK",
                "incident_category": "construction",
                "latitude": work.latitude,
                "longitude": work.longitude,
                "radius_m": radius_m,
                "status": "verified",
                "weight": 5,
                "timestamp": aware(work.last_verified_at).isoformat(),
                "qualification": "Verified published work; avoidance does not imply an official road closure",
            }
        )
    return zones


def distance_to_route(route, zone):
    line = LineString(route["geometry"]["coordinates"])
    return projected(line, zone["latitude"]).distance(
        projected(Point(zone["longitude"], zone["latitude"]), zone["latitude"])
    )


def mapped_construction_zones(features, radius_m):
    zones = []
    for feature in features:
        tags = feature.tags
        if not (
            str(tags.get("construction", "")).lower() not in {"", "no", "false", "0"}
            or any(tags.get(key) == "construction" for key in ("building", "landuse", "highway"))
        ):
            continue
        if datetime.now(UTC) - aware(feature.retrieved_at) > timedelta(days=30):
            continue
        zones.append(
            {
                "id": f"osm:{feature.id}",
                "record_id": feature.id,
                "category": "OSM_MAPPED_FEATURE",
                "incident_category": "construction",
                "latitude": feature.latitude,
                "longitude": feature.longitude,
                "radius_m": radius_m,
                "status": "unverified",
                "weight": 1,
                "timestamp": aware(feature.retrieved_at).isoformat(),
                "qualification": "OSM mapped construction; current activity and dates unconfirmed; optional precaution only",
            }
        )
    return zones


def intersects(route, zone):
    return distance_to_route(route, zone) <= zone["radius_m"]


def exclusion_ring(zone, vertices=24):
    # A small additional margin avoids round-off and road-edge snapping on the boundary.
    radius = (zone["radius_m"] + 20) / math.cos(math.pi / vertices)
    latitude, longitude = zone["latitude"], zone["longitude"]
    longitude_scale = max(0.01, math.cos(math.radians(latitude)))
    ring = [
        [
            round(longitude + radius * math.cos(2 * math.pi * i / vertices) / (111320 * longitude_scale), 7),
            round(latitude + radius * math.sin(2 * math.pi * i / vertices) / 111320, 7),
        ]
        for i in range(vertices)
    ]
    return ring + [ring[0]]


def endpoint_in_zone(request, zone):
    center = projected(Point(zone["longitude"], zone["latitude"]), zone["latitude"])
    return any(
        center.distance(projected(Point(p.longitude, p.latitude), zone["latitude"])) <= zone["radius_m"] + 25
        for p in (request.origin, request.destination)
    )


def select_exclusions(routes, zones, request, limit):
    relevant = [z for z in zones if any(intersects(r, z) for r in routes)]
    relevant.sort(key=lambda z: (-z["weight"], z["id"]))
    endpoint = [z for z in relevant if endpoint_in_zone(request, z)]
    eligible = [z for z in relevant if z not in endpoint]
    return eligible[:limit], endpoint, len(eligible) > limit


def distinct_routes(routes):
    result, seen = [], set()
    for route in routes:
        fingerprint = tuple((round(lon, 6), round(lat, 6)) for lon, lat in route["geometry"]["coordinates"])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append({**route, "id": f"route-{len(result) + 1}"})
    return result


def annotate_routes(routes, zones):
    for route in routes:
        remaining = [z for z in zones if intersects(route, z)]
        route["avoidance"] = {
            "intersecting_zone_ids": [z["id"] for z in remaining],
            "remaining_count": len(remaining),
            "remaining_weight": round(sum(z["weight"] for z in remaining), 2),
        }
    return routes


def recommend(routes, relevant, enabled, max_detour_ratio):
    fastest = min(routes, key=lambda r: r["duration_s"])
    recommendation = fastest
    if enabled and relevant:
        practical = [r for r in routes if r["duration_s"] <= fastest["duration_s"] * max_detour_ratio]
        safer = min(practical, key=lambda r: (r["avoidance"]["remaining_weight"], r["duration_s"]))
        if safer["avoidance"]["remaining_weight"] < fastest["avoidance"]["remaining_weight"]:
            recommendation = safer
        for route in routes:
            avoided = [z for z in relevant if z["id"] not in route["avoidance"]["intersecting_zone_ids"]]
            route["avoidance"]["avoided_zone_ids"] = [z["id"] for z in avoided]
            route["avoidance"]["avoided_count"] = len(avoided)
            if route["avoidance"]["remaining_weight"] < fastest["avoidance"]["remaining_weight"]:
                route["perspectives"].append("avoids_incidents")
            if route["id"] != fastest["id"] and avoided:
                remaining = route["avoidance"]["remaining_count"]
                if remaining:
                    route["perspectives"].append("partial_avoidance")
                route["explanation"] = (
                    (
                        f"Ocol parțial: evită {len(avoided)} zone, dar trece încă prin {remaining} zone semnalate. "
                        if remaining
                        else f"Ocolește {len(avoided)} zone semnalate. "
                    )
                    + f"+{route['additional_time_s'] / 60:.0f} minute față de ruta cea mai rapidă. Semnalările nu confirmă închiderea drumului."
                )
    else:
        recommendation = next((r for r in routes if "balanced" in r["perspectives"]), fastest)
    for route in routes:
        route["recommended"] = route["id"] == recommendation["id"]
        if route["avoidance"]["remaining_count"]:
            route["warnings"].append(
                "Route passes within the precautionary radius of active reported incidents."
            )
    return recommendation
