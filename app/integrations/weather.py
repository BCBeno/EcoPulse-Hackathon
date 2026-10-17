from datetime import UTC, date, datetime, timedelta

from app.integrations.http import ProviderError
from app.schemas.domain import Category, ObservationData, normalize_value

WEATHER_VARIABLES = (
    "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,precipitation,surface_pressure"
)


class OpenMeteoWeatherProvider:
    name = "weather"
    endpoint = "https://api.open-meteo.com/v1/forecast"
    variables = WEATHER_VARIABLES
    model = "best_match"

    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    async def fetch(self):
        return await self.forecast(self.settings.city_latitude, self.settings.city_longitude)

    async def forecast(self, latitude, longitude):
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": self.variables,
            "forecast_days": self.settings.forecast_days,
            "timezone": "GMT",
            "timeformat": "unixtime",
        }
        if self.name == "cams":
            params["domains"] = "cams_europe"
        else:
            params["wind_speed_unit"] = "ms"
        data = await self.http.get_json(self.endpoint, params, ttl=3600 if self.name == "weather" else 10800)
        return self.parse(data)

    async def history(self, start: date, end: date):
        if start > end or (end - start).days > 31 or end > date.today() - timedelta(days=5):
            raise ProviderError("archive_requires_past_dates_with_5_day_delay_max_31_days")
        data = await self.http.get_json(
            "https://archive-api.open-meteo.com/v1/archive",
            {
                "latitude": self.settings.city_latitude,
                "longitude": self.settings.city_longitude,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "hourly": WEATHER_VARIABLES,
                "models": "era5",
                "timezone": "GMT",
                "timeformat": "unixtime",
                "wind_speed_unit": "ms",
            },
            ttl=86400,
        )
        return self.parse(data, product="reanalysis", model="era5")

    def parse(self, data, product="forecast", model=None):
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("hourly"), dict)
            or "time" not in data["hourly"]
        ):
            raise ProviderError("unexpected_response_schema")
        results = []
        aliases = {"pm2_5": "pm25", "nitrogen_dioxide": "no2", "ozone": "o3"}
        for index, stamp in enumerate(data["hourly"]["time"]):
            for variable in self.variables.split(","):
                values = data["hourly"].get(variable, [])
                raw = values[index] if index < len(values) else None
                parameter = aliases.get(variable, variable)
                unit = data.get("hourly_units", {}).get(variable, "")
                original, value, normalized_unit = normalize_value(raw, unit, parameter)
                try:
                    when = (
                        datetime.fromtimestamp(stamp, UTC)
                        if isinstance(stamp, (int, float))
                        else datetime.fromisoformat(stamp).replace(tzinfo=UTC)
                    )
                    results.append(
                        ObservationData(
                            provider=self.name,
                            source_record_id=f"{data['latitude']}:{data['longitude']}:{stamp}:{variable}",
                            sensor_id=f"grid:{data['latitude']}:{data['longitude']}",
                            parameter=parameter,
                            original_value=original,
                            original_unit=unit,
                            value=value,
                            unit=normalized_unit,
                            observed_at=when,
                            latitude=data["latitude"],
                            longitude=data["longitude"],
                            category=Category.MODEL if self.name == "cams" else Category.WEATHER,
                            quality={
                                "valid": value is not None,
                                "resolution_km": 11 if self.name == "cams" else None,
                            },
                            provenance={
                                "model": model or self.model,
                                "product": product,
                                "attribution": "Copernicus Atmosphere Monitoring Service (CAMS), Open-Meteo"
                                if self.name == "cams"
                                else "Open-Meteo",
                                "license": "CC BY 4.0",
                                "license_url": "https://creativecommons.org/licenses/by/4.0/",
                                "limitation": "Regional grid estimate; not a street measurement"
                                if self.name == "cams"
                                else "Weather context; not a pollution concentration",
                            },
                        )
                    )
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
        return results
