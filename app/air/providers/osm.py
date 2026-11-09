"""OpenStreetMap construction sites via Overpass. © OpenStreetMap contributors, ODbL.

Mapped construction says nothing about current activity; it is shown as context and used as a mild
routing penalty. Overpass is not always available, so a failed run keeps the previous features.
"""

import httpx

from app.air.providers.records import OsmRecord
from app.config import Bounds
from app.http import SourceError, request_json

MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def query(bounds: Bounds) -> str:
    box = f"{bounds.south},{bounds.west},{bounds.north},{bounds.east}"
    return (
        "[out:json][timeout:60];("
        f'nwr["landuse"="construction"]({box});'
        f'nwr["highway"="construction"]({box});'
        f'nwr["building"="construction"]({box});'
        ");out tags geom center;"
    )


async def fetch(client: httpx.AsyncClient, bounds: Bounds) -> list[OsmRecord]:
    last = SourceError("overpass_unavailable")
    for mirror in MIRRORS:
        try:
            data = await request_json(
                client, "POST", mirror, data={"data": query(bounds)}, timeout=90, attempts=1
            )
        except SourceError as error:
            last = error
            continue
        if not isinstance(data, dict) or not isinstance(data.get("elements"), list) or data.get("remark"):
            last = SourceError("overpass_incomplete")
            continue
        return parse(data["elements"], bounds)
    raise last


def parse(elements: list[dict], bounds: Bounds) -> list[OsmRecord]:
    output = {}
    for item in elements:
        tags = item.get("tags") or {}
        geometry = _geometry(item)
        if geometry is None:
            continue
        lon, lat = _anchor(geometry)
        if not bounds.contains(lat, lon):
            continue
        osm_id = f"{item.get('type')}/{item.get('id')}"
        output[osm_id] = OsmRecord(
            osm_id=osm_id,
            kind="construction",
            name=tags.get("name") or tags.get("description"),
            geometry=geometry,
            tags=tags,
        )
    return list(output.values())


def _geometry(item: dict) -> dict | None:
    points = [[p["lon"], p["lat"]] for p in item.get("geometry") or [] if "lat" in p and "lon" in p]
    if item.get("type") == "way" and len(points) >= 4 and points[0] == points[-1]:
        return {"type": "Polygon", "coordinates": [points]}
    if item.get("type") == "way" and len(points) >= 2:
        return {"type": "LineString", "coordinates": points}
    center = item.get("center") or (item if "lat" in item and "lon" in item else None)
    if center is None:
        return None
    return {"type": "Point", "coordinates": [center["lon"], center["lat"]]}


def _anchor(geometry: dict) -> tuple[float, float]:
    coordinates = geometry["coordinates"]
    if geometry["type"] == "Point":
        return coordinates[0], coordinates[1]
    first = coordinates[0][0] if geometry["type"] == "Polygon" else coordinates[0]
    return first[0], first[1]
