"""Open-Meteo: CAMS Europe air-quality forecast (~11 km) and the weather forecast. CC BY 4.0."""

import math
from datetime import UTC, datetime

import httpx

from app.air.providers.records import ForecastValue, WeatherRecord, concentration, number
from app.config import Bounds
from app.http import SourceError, request_json

AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
AQ_VARIABLES = {
    "pm2_5": "pm25",
    "pm10": "pm10",
    "nitrogen_dioxide": "no2",
    "ozone": "o3",
    "sulphur_dioxide": "so2",
}
WEATHER_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "precipitation",
)
STEP = 0.1  # CAMS Europe grid spacing in degrees


def lattice(bounds: Bounds) -> list[tuple[float, float]]:
    """CAMS grid points that enclose the city bbox, so every cell can be interpolated bilinearly."""
    lats = _axis(bounds.south, bounds.north)
    lons = _axis(bounds.west, bounds.east)
    return [(lat, lon) for lat in lats for lon in lons]


def _axis(low: float, high: float) -> list[float]:
    start, end = math.floor(low / STEP), math.ceil(high / STEP)
    return [round(i * STEP, 2) for i in range(start, end + 1)]


async def fetch_cams(client: httpx.AsyncClient, bounds: Bounds, days: int = 4) -> list[ForecastValue]:
    points = lattice(bounds)
    params = {
        "latitude": ",".join(f"{lat:.2f}" for lat, _ in points),
        "longitude": ",".join(f"{lon:.2f}" for _, lon in points),
        "hourly": ",".join(AQ_VARIABLES),
        "domains": "cams_europe",
        "past_days": 1,
        "forecast_days": days,
        "timeformat": "unixtime",
        "timezone": "GMT",
    }
    data = await request_json(client, "GET", AQ_URL, params=params)
    blocks = data if isinstance(data, list) else [data]
    if len(blocks) != len(points):
        raise SourceError("unexpected_schema")
    output: list[ForecastValue] = []
    for (lat, lon), block in zip(points, blocks, strict=True):
        output.extend(parse_cams(block, lat, lon))
    return output


def parse_cams(block: dict, lat: float, lon: float) -> list[ForecastValue]:
    """`lat`/`lon` are the requested lattice point (Open-Meteo reports the snapped cell centre)."""
    hourly = block.get("hourly") if isinstance(block, dict) else None
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise SourceError("unexpected_schema")
    output = []
    for index, stamp in enumerate(hourly["time"]):
        valid_at = datetime.fromtimestamp(stamp, UTC)
        for variable, parameter in AQ_VARIABLES.items():
            series = hourly.get(variable) or []
            value = concentration(series[index]) if index < len(series) else None
            if value is not None:
                output.append(ForecastValue(lat, lon, parameter, valid_at, value))
    return output


async def fetch_weather(
    client: httpx.AsyncClient, lat: float, lon: float, days: int = 4
) -> list[WeatherRecord]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(WEATHER_VARIABLES),
        "past_days": 1,
        "forecast_days": days,
        "timeformat": "unixtime",
        "timezone": "GMT",
        "wind_speed_unit": "ms",
    }
    return parse_weather(await request_json(client, "GET", WEATHER_URL, params=params))


def parse_weather(data: dict) -> list[WeatherRecord]:
    hourly = data.get("hourly") if isinstance(data, dict) else None
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise SourceError("unexpected_schema")

    def at(variable: str, index: int) -> float | None:
        series = hourly.get(variable) or []
        return number(series[index]) if index < len(series) else None

    return [
        WeatherRecord(
            valid_at=datetime.fromtimestamp(stamp, UTC),
            temperature_c=at("temperature_2m", i),
            humidity_pct=at("relative_humidity_2m", i),
            wind_speed_ms=at("wind_speed_10m", i),
            wind_dir_deg=at("wind_direction_10m", i),
            precipitation_mm=at("precipitation", i),
        )
        for i, stamp in enumerate(hourly["time"])
    ]
