import csv
import io
from datetime import UTC, datetime

from pydantic import ValidationError

from app.integrations.http import ProviderError
from app.schemas.domain import Category, ObservationData, normalize_value

FIELDS = {
    "P1": ("pm10", "µg/m³"),
    "P2": ("pm25", "µg/m³"),
    "temperature": ("temperature", "°C"),
    "humidity": ("humidity", "%"),
    "pressure": ("pressure", "Pa"),
}


class SensorCommunityProvider:
    name = "sensor_community"

    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    async def fetch(self):
        data = await self.http.get_json(
            f"https://data.sensor.community/airrohr/v1/filter/box={self.settings.city_bbox}", ttl=600
        )
        if not isinstance(data, list):
            raise ProviderError("unexpected_response_schema")
        return self.parse(data)

    def parse(self, data):
        results = {}
        s, w, n, e = self.settings.bounds
        for row in data:
            try:
                lat, lon = float(row["location"]["latitude"]), float(row["location"]["longitude"])
                if not (s <= lat <= n and w <= lon <= e):
                    continue
                if row["sensor"]["id"] is None:
                    continue
                sensor = str(row["sensor"]["id"])
                stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)  # Source documents UTC timestamps.
                for item in row["sensordatavalues"]:
                    if item.get("value_type") not in FIELDS:
                        continue
                    parameter, unit = FIELDS[item["value_type"]]
                    original, value, normalized_unit = normalize_value(item.get("value"), unit, parameter)
                    observation = ObservationData(
                        provider=self.name,
                        source_record_id=str(row.get("id", f"{sensor}:{stamp.isoformat()}")),
                        sensor_id=sensor,
                        parameter=parameter,
                        original_value=original,
                        original_unit=unit,
                        value=value,
                        unit=normalized_unit,
                        observed_at=stamp,
                        latitude=lat,
                        longitude=lon,
                        category=Category.WEATHER
                        if parameter in {"temperature", "humidity", "pressure"}
                        else Category.SENSOR,
                        quality={
                            "valid": value is not None,
                            "device": row["sensor"].get("sensor_type"),
                            "qualification": "Uncalibrated low-cost community sensor",
                        },
                        provenance={
                            "attribution": "Sensor.Community",
                            "license": "ODbL-1.0",
                            "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
                            "contents_license": "DbCL-1.0",
                            "contents_license_url": "https://opendatacommons.org/licenses/dbcl/1-0/",
                            "product": "measurement",
                        },
                    )
                    results[observation.id] = observation
            except (KeyError, TypeError, ValueError, ValidationError):
                continue
        return list(results.values())

    def parse_csv(self, content: str):
        records = []
        for row in csv.DictReader(io.StringIO(content), delimiter=";"):
            records.append(
                {
                    "sensor": {"id": row.get("sensor_id")},
                    "location": {"latitude": row.get("lat"), "longitude": row.get("lon")},
                    "timestamp": row.get("timestamp", ""),
                    "sensordatavalues": [
                        {"value_type": key, "value": row[key]} for key in FIELDS if key in row
                    ],
                }
            )
        return self.parse(records)
