from datetime import UTC, datetime, timedelta

from app.integrations.http import ProviderError
from app.schemas.domain import Category, ObservationData, normalize_value

PARAMETERS = {"pm25": "pm25", "pm10": "pm10", "no2": "no2", "o3": "o3", "so2": "so2", "co": "co"}


class OpenAQProvider:
    name = "openaq"
    base = "https://api.openaq.org/v3"

    def __init__(self, settings, http):
        self.settings, self.http = settings, http
        self.stations = []

    async def pages(self, path, params=None):
        if not self.settings.openaq_api_key:
            raise ProviderError("missing_OPENAQ_API_KEY")
        output = []
        for page in range(1, 101):
            data = await self.http.get_json(
                self.base + path,
                {**(params or {}), "limit": 100, "page": page},
                {"X-API-Key": self.settings.openaq_api_key},
                ttl=3600,
            )
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ProviderError("unexpected_response_schema")
            rows = data["results"]
            output.extend(rows)
            if len(rows) < 100:
                return output
            found = data.get("meta", {}).get("found")
            if isinstance(found, int) and len(output) >= found:
                return output
        raise ProviderError("pagination_limit_exceeded")

    async def discover(self):
        configured = [x.strip() for x in self.settings.openaq_location_ids.split(",") if x.strip()]
        if configured:
            locations = []
            for location_id in configured:
                if not location_id.isdigit():
                    raise ProviderError("invalid_location_inventory")
                locations.extend(await self.pages(f"/locations/{location_id}"))
        else:
            locations = await self.pages(
                "/locations",
                {
                    "coordinates": f"{self.settings.city_latitude:.4f},{self.settings.city_longitude:.4f}",
                    "radius": 15000,
                },
            )
        self.stations = locations
        return locations

    async def fetch(self):
        return await self.history(datetime.now(UTC) - timedelta(hours=6), datetime.now(UTC))

    async def history(self, start, end):
        if end <= start or end - start > timedelta(days=7):
            raise ProviderError("history_window_must_be_1_to_7_days")
        output = {}
        for location in await self.discover():
            sensors = await self.pages(f"/locations/{location['id']}/sensors")
            for sensor in sensors:
                if sensor.get("parameter", {}).get("name") not in PARAMETERS:
                    continue
                rows = await self.pages(
                    f"/sensors/{sensor['id']}/hours",
                    {"datetime_from": start.isoformat(), "datetime_to": end.isoformat()},
                )
                for observation in self.parse(rows, location, sensor):
                    output[observation.id] = observation
        return list(output.values())

    def parse(self, rows, location, sensor):
        output = []
        for row in rows:
            try:
                parameter_data = row.get("parameter") or sensor["parameter"]
                parameter = PARAMETERS.get(parameter_data["name"])
                if not parameter:
                    continue
                coord = row.get("coordinates") or location["coordinates"]
                stamp = row.get("period", {}).get("datetimeFrom", {}).get("utc") or row.get(
                    "datetime", {}
                ).get("utc")
                unit = parameter_data.get("units", "")
                original, value, normalized_unit = normalize_value(row.get("value"), unit, parameter)
                category = Category.OFFICIAL if location.get("isMonitor") is True else Category.SENSOR
                output.append(
                    ObservationData(
                        provider=self.name,
                        source_record_id=str(row.get("id", f"{sensor['id']}:{stamp}")),
                        sensor_id=str(sensor["id"]),
                        parameter=parameter,
                        original_value=original,
                        original_unit=unit,
                        value=value,
                        unit=normalized_unit,
                        latitude=coord["latitude"],
                        longitude=coord["longitude"],
                        observed_at=stamp,
                        category=category,
                        quality={
                            "valid": value is not None,
                            "coverage": row.get("coverage"),
                            "is_reference_monitor": location.get("isMonitor"),
                        },
                        provenance={
                            "attribution": "OpenAQ / original data provider",
                            "owner": location.get("owner"),
                            "provider": location.get("provider"),
                            "licenses": location.get("licenses", []),
                            "location_id": location["id"],
                            "location_name": location.get("name"),
                            "parameter": parameter_data,
                            "product": "hourly_aggregate",
                            "period": row.get("period"),
                        },
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return output
