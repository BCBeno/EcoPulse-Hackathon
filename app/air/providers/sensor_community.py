"""Sensor.Community (community low-cost sensors). Data: ODbL / DbCL, attribution "Sensor.Community"."""

from datetime import UTC, datetime

import httpx

from app.air.providers.records import StationReading, concentration, number
from app.config import Bounds
from app.http import SourceError, request_json

NAME = "sensor_community"
# value_type -> (parameter, is_concentration)
FIELDS = {
    "P2": ("pm25", True),
    "P1": ("pm10", True),
    "temperature": ("temperature", False),
    "humidity": ("humidity", False),
}


def url(bounds: Bounds) -> str:
    # The filter is part of the path: box=south,west,north,east.
    return (
        "https://data.sensor.community/airrohr/v1/filter/"
        f"box={bounds.south},{bounds.west},{bounds.north},{bounds.east}"
    )


async def fetch(client: httpx.AsyncClient, bounds: Bounds) -> list[StationReading]:
    data = await request_json(client, "GET", url(bounds))
    if not isinstance(data, list):
        raise SourceError("unexpected_schema")
    return parse(data, bounds)


def parse(rows: list[dict], bounds: Bounds) -> list[StationReading]:
    readings: dict[tuple, StationReading] = {}
    for row in rows:
        try:
            location, sensor = row["location"], row["sensor"]
            if str(location.get("indoor", 0)) == "1":
                continue
            lat, lon = float(location["latitude"]), float(location["longitude"])
            if not bounds.contains(lat, lon):
                continue
            observed_at = datetime.fromisoformat(row["timestamp"]).replace(tzinfo=UTC)
            station = str(location["id"])
            sensor_type = (sensor.get("sensor_type") or {}).get("name")
        except (KeyError, TypeError, ValueError):
            continue
        for item in row.get("sensordatavalues", []):
            spec = FIELDS.get(item.get("value_type"))
            if spec is None:
                continue
            parameter, is_concentration = spec
            value = concentration(item.get("value")) if is_concentration else number(item.get("value"))
            if value is None or (parameter == "humidity" and not 0 <= value <= 100):
                continue
            reading = StationReading(
                provider=NAME,
                external_id=station,
                kind="low_cost",
                lat=lat,
                lon=lon,
                parameter=parameter,
                observed_at=observed_at,
                value=value,
                meta={"sensor_type": sensor_type, "location_id": station},
            )
            readings[(station, parameter, observed_at)] = reading
    return list(readings.values())
