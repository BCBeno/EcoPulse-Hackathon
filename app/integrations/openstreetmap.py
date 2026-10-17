from datetime import UTC, datetime

import httpx

from app.integrations.http import ProviderError


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
