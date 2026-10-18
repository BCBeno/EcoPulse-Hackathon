import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from app.integrations.http import ProviderError

ROAD_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary")
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
ROADS_TTL_S = 86400
log = logging.getLogger("ecopulse.traffic")


class OsmMajorRoadsProvider:
    """Major road geometry from OpenStreetMap; the basis of the time-of-day traffic estimate."""

    name = "osm_roads"
    # Road geometry changes rarely and public Overpass servers are often at capacity:
    # keep the last good result in memory and on disk, and reuse it when Overpass fails.
    memory: dict = {}
    lock = asyncio.Lock()

    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    def query(self):
        pattern = "|".join(ROAD_CLASSES)
        return f'[out:json][timeout:60];way["highway"~"^({pattern})(_link)?$"]({self.settings.city_bbox});out geom;'

    def cache_path(self):
        return Path(self.settings.traffic_roads_cache) if self.settings.traffic_roads_cache else None

    def load(self):
        path = self.cache_path()
        try:
            stored = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else None
        except (OSError, ValueError):
            return None
        if not isinstance(stored, dict) or stored.get("bbox") != self.settings.city_bbox:
            return None
        return stored

    def store(self, roads):
        stored = {"bbox": self.settings.city_bbox, "fetched_at": time.time(), "roads": roads}
        path = self.cache_path()
        if path:
            try:
                path.write_text(json.dumps(stored, separators=(",", ":")), encoding="utf-8")
            except OSError:
                log.warning("Could not write the road cache to %s", path)
        return stored

    async def download(self):
        last = ProviderError("overpass_unavailable")
        for url in OVERPASS_MIRRORS:
            try:
                response = await self.http.client.post(url, data={"data": self.query()}, timeout=90)
            except httpx.HTTPError:
                last = ProviderError("overpass_unavailable")
                continue
            if response.status_code >= 400:
                last = ProviderError(f"overpass_http_{response.status_code}")
                continue
            try:
                data = response.json()
            except ValueError:
                last = ProviderError("malformed_json")
                continue
            if isinstance(data, dict) and isinstance(data.get("elements"), list) and not data.get("remark"):
                return self.parse(data)
            last = ProviderError("overpass_incomplete_response")
        raise last

    async def roads(self, refresh=False):
        async with self.lock:
            stored = self.memory.get(self.settings.city_bbox) or self.load()
            fresh = stored and time.time() - stored["fetched_at"] < ROADS_TTL_S
            if fresh and not refresh:
                self.memory[self.settings.city_bbox] = stored
                return stored["roads"]
            try:
                stored = self.store(await self.download())
            except ProviderError:
                if not stored:
                    raise
                log.warning(
                    "Overpass unavailable; reusing road geometry from %.0f h ago",
                    (time.time() - stored["fetched_at"]) / 3600,
                )
            self.memory[self.settings.city_bbox] = stored
            return stored["roads"]

    async def fetch(self):
        return await self.roads(refresh=True)

    def parse(self, data):
        roads = []
        for item in data["elements"]:
            tags = item.get("tags") or {}
            highway = tags.get("highway", "")
            points = [
                [round(float(p["lon"]), 5), round(float(p["lat"]), 5)]
                for p in item.get("geometry") or []
                if isinstance(p, dict) and "lat" in p and "lon" in p
            ]
            if (
                item.get("type") != "way"
                or len(points) < 2
                or highway.removesuffix("_link") not in ROAD_CLASSES
            ):
                continue
            roads.append(
                {
                    "id": f"way/{item['id']}",
                    "name": tags.get("name") or tags.get("ref") or "",
                    "highway": highway,
                    "coordinates": points,
                }
            )
        return roads


class TomTomFlowProvider:
    """Real-time speeds from TomTom Traffic Flow (requires TOMTOM_API_KEY; free tier available)."""

    endpoint = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"

    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    # Keyed by point only: the API key must never become part of a cache key.
    cache: dict = {}

    async def flow(self, latitude, longitude):
        point = f"{latitude:.5f},{longitude:.5f}"
        cached = self.cache.get(point)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            response = await self.http.client.get(
                self.endpoint, params={"point": point, "unit": "KMPH", "key": self.settings.tomtom_api_key}
            )
        except httpx.HTTPError:
            raise ProviderError("network_error") from None
        if response.status_code in (401, 403):
            raise ProviderError("authentication_rejected")
        if response.status_code >= 400:
            raise ProviderError(f"upstream_http_{response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("malformed_json") from None
        segment = data.get("flowSegmentData") if isinstance(data, dict) else None
        if not isinstance(segment, dict):
            raise ProviderError("unexpected_response_schema")
        try:
            current, free = float(segment["currentSpeed"]), float(segment["freeFlowSpeed"])
        except (KeyError, TypeError, ValueError):
            raise ProviderError("unexpected_response_schema") from None
        # TomTom answers for a whole road stretch around the point; keep its geometry ([lon, lat]).
        points = (segment.get("coordinates") or {}).get("coordinate") or []
        result = {
            "current_speed_kmh": current,
            "free_flow_speed_kmh": free,
            "closed": bool(segment.get("roadClosure")),
            "confidence": segment.get("confidence"),
            "coordinates": [
                [float(c["longitude"]), float(c["latitude"])]
                for c in points
                if isinstance(c, dict) and "latitude" in c and "longitude" in c
            ],
        }
        self.cache[point] = (time.monotonic() + 900, result)
        while len(self.cache) > 512:
            self.cache.pop(next(iter(self.cache)))
        return result
