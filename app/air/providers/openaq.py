"""OpenAQ v3: official monitoring stations (and other networks) near the city.

Needs a free API key (X-API-Key). Licences differ per original provider; they are kept in `meta`.
Only µg/m³ values are used: gas mixing ratios (ppm/ppb) need temperature and pressure to convert.
"""

from datetime import datetime

import httpx

from app.air.providers.records import StationReading, concentration
from app.config import Bounds
from app.http import SourceError, request_json

NAME = "openaq"
BASE = "https://api.openaq.org/v3"
PARAMETERS = {"pm25": "pm25", "pm10": "pm10", "no2": "no2", "o3": "o3", "so2": "so2"}
UG_M3 = {"µg/m³", "μg/m³", "ug/m3", "µg/m3", "μg/m3"}
RADIUS_M = 25_000  # the API maximum


async def fetch(
    client: httpx.AsyncClient, api_key: str, center: tuple[float, float], bounds: Bounds
) -> list[StationReading]:
    headers = {"X-API-Key": api_key}
    locations = await request_json(
        client,
        "GET",
        f"{BASE}/locations",
        params={"coordinates": f"{center[0]:.4f},{center[1]:.4f}", "radius": RADIUS_M, "limit": 100},
        headers=headers,
    )
    if not isinstance(locations, dict) or not isinstance(locations.get("results"), list):
        raise SourceError("unexpected_schema")
    readings: list[StationReading] = []
    for location in locations["results"]:
        coordinates = location.get("coordinates") or {}
        lat, lon = coordinates.get("latitude"), coordinates.get("longitude")
        if lat is None or lon is None or not bounds.contains(lat, lon):
            continue
        latest = await request_json(
            client, "GET", f"{BASE}/locations/{location['id']}/latest", headers=headers
        )
        readings.extend(parse(location, latest.get("results", []) if isinstance(latest, dict) else []))
    return readings


def parse(location: dict, latest: list[dict]) -> list[StationReading]:
    sensors = {}
    for sensor in location.get("sensors") or []:
        parameter = (sensor.get("parameter") or {}).get("name")
        units = (sensor.get("parameter") or {}).get("units")
        if parameter in PARAMETERS and units in UG_M3:
            sensors[sensor.get("id")] = PARAMETERS[parameter]
    coordinates = location["coordinates"]
    meta = {
        "name": location.get("name"),
        "owner": (location.get("owner") or {}).get("name"),
        "provider": (location.get("provider") or {}).get("name"),
        "licenses": [item.get("name") for item in location.get("licenses") or []],
        "location_id": location.get("id"),
    }
    output = []
    for row in latest:
        parameter = sensors.get(row.get("sensorsId"))
        value = concentration(row.get("value"))
        stamp = (row.get("datetime") or {}).get("utc")
        if parameter is None or value is None or not stamp:
            continue
        try:
            observed_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        output.append(
            StationReading(
                provider=NAME,
                external_id=str(location["id"]),
                # Unknown monitor status is treated conservatively as low-cost.
                kind="official" if location.get("isMonitor") is True else "low_cost",
                lat=coordinates["latitude"],
                lon=coordinates["longitude"],
                parameter=parameter,
                observed_at=observed_at,
                value=value,
                meta=meta,
            )
        )
    return output
