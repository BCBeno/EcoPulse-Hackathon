import math
import re
from datetime import UTC, datetime

import httpx
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from app.integrations.http import ProviderError
from app.services.scoring import projected


class OpenStreetMapFeaturesProvider:
    name = "osm"

    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    def query(self):
        box = self.settings.city_bbox
        return f'[out:json][timeout:25];(nwr["highway"="construction"]({box});nwr["landuse"="construction"]({box});nwr["construction"]["construction"!="no"]({box}););out body center geom;'

    async def fetch(self):
        try:
            response = await self.http.client.post(
                "https://overpass-api.de/api/interpreter", data={"data": self.query()}, timeout=60
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise ProviderError("overpass_unavailable") from None
        if not isinstance(data, dict) or not isinstance(data.get("elements"), list) or data.get("remark"):
            raise ProviderError("overpass_incomplete_response")
        return self.parse(data)

    def parse(self, data):
        results = {}
        s, w, n, e = self.settings.bounds
        for item in data.get("elements", []):
            try:
                center = item.get("center") or item
                lat, lon = float(center["lat"]), float(center["lon"])
                if not (s <= lat <= n and w <= lon <= e):
                    continue
                points = [
                    [float(p["lon"]), float(p["lat"])]
                    for p in item.get("geometry", [])
                    if "lat" in p and "lon" in p
                ]
                if any(not (-90 <= p[1] <= 90 and -180 <= p[0] <= 180) for p in points):
                    continue
                geometry = (
                    {"type": "LineString", "coordinates": points}
                    if len(points) >= 2
                    else {"type": "Point", "coordinates": [lon, lat]}
                )
                key = f"{item['type']}/{item['id']}"
                results[key] = {
                    "source_id": key,
                    "latitude": lat,
                    "longitude": lon,
                    "tags": item.get("tags", {}),
                    "geometry": geometry,
                    "retrieved_at": datetime.now(UTC),
                    "provenance": {
                        "category": "OSM_MAPPED_FEATURE",
                        "attribution": "© OpenStreetMap contributors",
                        "license": "ODbL-1.0",
                        "license_url": "https://www.openstreetmap.org/copyright",
                        "query": self.query(),
                        "limitation": "Mapped construction; activity, dates and closures are unverified",
                    },
                }
            except (KeyError, TypeError, ValueError):
                continue
        return list(results.values())


ROAD_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary")
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def metres(a, b):
    """Haversine distance between two [lon, lat] points."""
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def first_int(value):
    match = re.match(r"\s*(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


class OpenStreetMapRoadsProvider:
    """Major roads and traffic signals, the static input of the traffic congestion estimate."""

    name = "osm_roads"

    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    def query(self):
        box = self.settings.city_bbox
        classes = "|".join(ROAD_CLASSES)
        return (
            f'[out:json][timeout:120];(way["highway"~"^({classes})(_link)?$"]({box}););out tags geom;'
            f'node["highway"="traffic_signals"]({box});out;'
        )

    async def fetch(self):
        data = None
        for url in OVERPASS_MIRRORS:
            try:
                response = await self.http.client.post(url, data={"data": self.query()}, timeout=180)
                response.raise_for_status()
                data = response.json()
                break
            except (httpx.HTTPError, ValueError):
                continue
        if data is None:
            raise ProviderError("overpass_unavailable")
        if not isinstance(data, dict) or not isinstance(data.get("elements"), list) or data.get("remark"):
            raise ProviderError("overpass_incomplete_response")
        records = self.parse(data)
        # A partial answer must not replace the stored network (the sync deletes roads missing from it).
        if not records:
            raise ProviderError("overpass_no_roads")
        return records

    def parse(self, data):
        s, w, n, e = self.settings.bounds
        signals = [
            (float(x["lon"]), float(x["lat"]))
            for x in data["elements"]
            if x.get("type") == "node" and "lat" in x and "lon" in x
        ]
        latitude = (s + n) / 2
        signal_tree = STRtree([projected(Point(p), latitude) for p in signals]) if signals else None
        retrieved = datetime.now(UTC)
        records = []
        for item in data["elements"]:
            try:
                if item.get("type") != "way":
                    continue
                tags = item.get("tags") or {}
                highway = str(tags.get("highway", ""))
                if highway.removesuffix("_link") not in ROAD_CLASSES:
                    continue
                points = [[float(p["lon"]), float(p["lat"])] for p in item.get("geometry", []) if "lat" in p]
                if len(points) < 2 or any(not (-90 <= p[1] <= 90 and -180 <= p[0] <= 180) for p in points):
                    continue
                length = sum(metres(a, b) for a, b in zip(points, points[1:]))
                if length <= 0:
                    continue
                line = projected(LineString(points), latitude)
                count = 0
                if signal_tree is not None:
                    count = len(signal_tree.query(line, predicate="dwithin", distance=12))
                records.append(
                    {
                        "id": f"way/{int(item['id'])}",
                        "name": (tags.get("name") or tags.get("ref") or None),
                        "highway": highway,
                        "lanes": first_int(tags.get("lanes")),
                        "oneway": tags.get("oneway") in {"yes", "1", "true"}
                        or tags.get("junction") == "roundabout",
                        "signals": count,
                        "length_m": round(length, 1),
                        "geometry": {"type": "LineString", "coordinates": points},
                        "retrieved_at": retrieved,
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return records
