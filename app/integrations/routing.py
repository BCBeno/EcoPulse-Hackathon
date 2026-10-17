import asyncio
import time
from typing import Protocol
from zoneinfo import ZoneInfo

from app.integrations.http import ProviderError


class RoutingProvider(Protocol):
    async def routes(self, request): ...


class GeocodingProvider(Protocol):
    async def search(self, query: str): ...


def decode_polyline(value, precision=6):
    index, lat, lon, points = 0, 0, 0, []
    while index < len(value):
        deltas = []
        for _ in range(2):
            shift, result = 0, 0
            while True:
                if index >= len(value) or shift > 35:
                    raise ValueError("Invalid polyline")
                byte = ord(value[index]) - 63
                index += 1
                if byte < 0 or byte > 63:
                    raise ValueError("Invalid polyline")
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat, lon = lat + deltas[0], lon + deltas[1]
        if not (-90 <= lat / 10**precision <= 90 and -180 <= lon / 10**precision <= 180):
            raise ValueError("Invalid route coordinates")
        points.append([lon / 10**precision, lat / 10**precision])
    return points


class ValhallaRoutingProvider:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    async def routes(self, request, *, exclude_polygons=None):
        if request.mode == "motorcycle" and not self.settings.motorcycle_enabled:
            raise ProviderError("motorcycle_beta_profile_disabled")
        if not self.settings.valhalla_base_url:
            raise ProviderError("missing_VALHALLA_BASE_URL")
        payload = {
            "locations": [
                {"lat": p.latitude, "lon": p.longitude} for p in (request.origin, request.destination)
            ],
            "costing": {"walk": "pedestrian", "bike": "bicycle", "motorcycle": "motorcycle"}[request.mode],
            "alternates": 2,
            "units": "kilometers",
            "date_time": {
                "type": 1,
                "value": request.departure_at.astimezone(ZoneInfo(self.settings.city_timezone)).strftime(
                    "%Y-%m-%dT%H:%M"
                ),
            },
        }
        if exclude_polygons:
            # Exclusions describe an advisory avoidance scenario, not an official road closure.
            payload["exclude_polygons"] = exclude_polygons
        import json

        data = await self.http.get_json(
            self.settings.valhalla_base_url.rstrip("/") + "/route",
            {"json": json.dumps(payload, separators=(",", ":"))},
            ttl=300,
        )
        trips = [data.get("trip")] + [x.get("trip") for x in data.get("alternates", [])]
        routes, seen = [], set()
        for trip in trips:
            if not trip:
                continue
            try:
                coordinates = []
                instructions = []
                for leg in trip["legs"]:
                    points = decode_polyline(leg["shape"])
                    coordinates.extend(points[1:] if coordinates and coordinates[-1] == points[0] else points)
                    instructions.extend(
                        {
                            "instruction": m.get("instruction", ""),
                            "distance_m": float(m.get("length", 0)) * 1000,
                            "duration_s": m.get("time", 0),
                        }
                        for m in leg.get("maneuvers", [])
                    )
                if len(coordinates) < 2:
                    continue
                fingerprint = tuple(tuple(p) for p in coordinates)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                distance, duration = float(trip["summary"]["length"]) * 1000, float(trip["summary"]["time"])
                if distance <= 0 or duration <= 0:
                    continue
                routes.append(
                    {
                        "id": f"route-{len(routes) + 1}",
                        "distance_m": distance,
                        "duration_s": duration,
                        "geometry": {"type": "LineString", "coordinates": coordinates},
                        "instructions": instructions,
                        "provider": "Valhalla",
                        "provider_metadata": {
                            "costing": payload["costing"],
                            "motorcycle_beta": request.mode == "motorcycle",
                            "traffic": "No live traffic feed configured",
                            "avoidance_requested": bool(exclude_polygons),
                        },
                    }
                )
            except (KeyError, TypeError, ValueError):
                raise ProviderError("invalid_routing_response") from None
        if not routes:
            raise ProviderError("no_route_found")
        return routes


class NominatimGeocodingProvider:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http
        self.cache = {}

    async def search(self, query):
        key = query.strip().casefold()
        if key in self.cache:
            return self.cache[key]
        async with self.http.geocode_lock:
            await asyncio.sleep(max(0, 1.1 - (time.monotonic() - self.http.last_geocode)))
            s, w, n, e = self.settings.bounds
            try:
                data = await self.http.get_json(
                    self.settings.geocoding_base_url.rstrip("/") + "/search",
                    {"q": query, "format": "jsonv2", "limit": 5, "viewbox": f"{w},{n},{e},{s}", "bounded": 1},
                    ttl=86400,
                )
            finally:
                self.http.last_geocode = time.monotonic()
        if not isinstance(data, list):
            raise ProviderError("invalid_geocoder_response")
        results = [
            {
                "label": x["display_name"],
                "latitude": float(x["lat"]),
                "longitude": float(x["lon"]),
                "attribution": "© OpenStreetMap contributors",
            }
            for x in data
        ]
        if len(self.cache) >= 500:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = results
        return results
