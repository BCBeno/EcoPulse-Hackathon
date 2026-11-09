"""Loads air data from the database and turns it into grids, point answers and forecasts."""

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import mean

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.air import eaqi, grid
from app.air.models import GridSnapshot, Measurement, ModelForecast, Sensor, WeatherHour
from app.air.quality import Assessment, Station, assess, distance_m
from app.config import Settings

READING_WINDOW = timedelta(hours=2)
AVERAGE_SPAN = timedelta(minutes=20)
SNAPSHOT_FRESH = timedelta(minutes=15)
NOW_TOLERANCE = timedelta(minutes=30)
FORECAST_CACHE_S = 600


def floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


@dataclass
class GridResult:
    spec: grid.GridSpec
    layers: dict[str, np.ndarray]
    valid_at: datetime
    kind: str  # now | forecast
    inputs: dict


async def load_stations(session: AsyncSession, now: datetime) -> list[Station]:
    """Stations seen in the last day; each value is the mean of the last 20 min before its newest reading."""
    rows = await session.execute(
        select(
            Sensor.id, Sensor.provider, Sensor.kind, func.ST_Y(Sensor.location), func.ST_X(Sensor.location)
        ).where(Sensor.last_seen_at >= now - timedelta(days=1))
    )
    stations = {
        sensor_id: Station(id=sensor_id, provider=provider, kind=kind, lat=lat, lon=lon)
        for sensor_id, provider, kind, lat, lon in rows
    }
    if not stations:
        return []
    readings = await session.execute(
        select(
            Measurement.sensor_id, Measurement.parameter, Measurement.observed_at, Measurement.value
        ).where(Measurement.sensor_id.in_(stations), Measurement.observed_at >= now - READING_WINDOW)
    )
    series: dict[tuple, list[tuple[datetime, float]]] = defaultdict(list)
    for sensor_id, parameter, observed_at, value in readings:
        series[(sensor_id, parameter)].append((observed_at, value))
    for (sensor_id, parameter), points in series.items():
        newest = max(t for t, _ in points)
        recent = [v for t, v in points if t >= newest - AVERAGE_SPAN]
        stations[sensor_id].values[parameter] = mean(recent)
        stations[sensor_id].observed_at[parameter] = newest
    return list(stations.values())


async def weather_at(session: AsyncSession, moment: datetime) -> WeatherHour | None:
    return await session.scalar(
        select(WeatherHour)
        .where(WeatherHour.valid_at.between(moment - timedelta(hours=1), moment + timedelta(hours=1)))
        .order_by(func.abs(func.extract("epoch", WeatherHour.valid_at - moment)))
        .limit(1)
    )


async def backgrounds_at(
    session: AsyncSession, moment: datetime
) -> dict[str, dict[tuple[float, float], float]]:
    """CAMS values per pollutant and lattice point, linearly interpolated between the two nearest hours."""
    start = floor_hour(moment)
    rows = await session.execute(
        select(
            ModelForecast.parameter,
            ModelForecast.lat,
            ModelForecast.lon,
            ModelForecast.valid_at,
            ModelForecast.value,
        ).where(ModelForecast.valid_at.in_([start, start + timedelta(hours=1)]))
    )
    pairs: dict[tuple[str, float, float], dict[datetime, float]] = defaultdict(dict)
    for parameter, lat, lon, valid_at, value in rows:
        pairs[(parameter, lat, lon)][valid_at] = value
    share = (moment - start).total_seconds() / 3600
    output: dict[str, dict[tuple[float, float], float]] = defaultdict(dict)
    for (parameter, lat, lon), by_hour in pairs.items():
        before, after = by_hour.get(start), by_hour.get(start + timedelta(hours=1))
        if before is not None and after is not None:
            value = before + (after - before) * share
        else:
            value = before if before is not None else after
        output[parameter][(lat, lon)] = value
    return dict(output)


def station_inputs(stations: list[Station], verdicts: dict) -> dict[str, list[grid.StationInput]]:
    inputs: dict[str, list[grid.StationInput]] = defaultdict(list)
    for station in stations:
        verdict = verdicts[station.id]
        if verdict.weight <= 0:
            continue
        for parameter in eaqi.POLLUTANTS:
            if parameter in station.values:
                inputs[parameter].append(
                    grid.StationInput(station.lat, station.lon, station.values[parameter], verdict.weight)
                )
    return dict(inputs)


async def compute_grid(session: AsyncSession, settings: Settings, at: datetime, now: datetime) -> GridResult:
    stations = await load_stations(session, now)
    weather = await weather_at(session, now)
    verdicts = assess(stations, now, weather.humidity_pct if weather else None)
    backgrounds = await backgrounds_at(session, at)
    hours_ahead = max(0.0, (at - now).total_seconds() / 3600)
    spec = grid.GridSpec.for_bounds(settings.city_bbox)
    layers = grid.compute(spec, backgrounds, station_inputs(stations, verdicts), hours_ahead)
    inputs = {
        "stations_used": sum(1 for v in verdicts.values() if v.weight > 0),
        "stations_seen": len(stations),
        "model_points": len(backgrounds.get("pm25", {})),
    }
    return GridResult(spec, layers, at, "forecast" if hours_ahead > 0 else "now", inputs)


async def refresh_now_grid(session: AsyncSession, settings: Settings) -> GridSnapshot:
    now = datetime.now(UTC)
    result = await compute_grid(session, settings, now, now)
    spec = result.spec
    snapshot = GridSnapshot(
        valid_at=now,
        computed_at=now,
        south=spec.south,
        west=spec.west,
        d_lat=spec.d_lat,
        d_lon=spec.d_lon,
        rows=spec.rows,
        cols=spec.cols,
        layers=grid.pack(result.layers),
        inputs=result.inputs,
    )
    session.add(snapshot)
    await session.commit()
    return snapshot


class ForecastCache:
    """Forecast grids are computed on demand; keep a few for ten minutes."""

    def __init__(self, size: int = 8):
        self.size, self.items = size, {}

    def get(self, key):
        item = self.items.get(key)
        return item[1] if item and time.monotonic() - item[0] < FORECAST_CACHE_S else None

    def put(self, key, value) -> None:
        self.items[key] = (time.monotonic(), value)
        while len(self.items) > self.size:
            self.items.pop(next(iter(self.items)))


async def grid_for(
    session: AsyncSession, settings: Settings, at: datetime | None, cache: ForecastCache
) -> GridResult:
    now = datetime.now(UTC)
    if at is None or abs(at - now) <= NOW_TOLERANCE:
        snapshot = await session.scalar(
            select(GridSnapshot).order_by(GridSnapshot.computed_at.desc()).limit(1)
        )
        if snapshot is not None and now - snapshot.computed_at <= SNAPSHOT_FRESH:
            spec = grid.GridSpec(
                snapshot.south, snapshot.west, snapshot.d_lat, snapshot.d_lon, snapshot.rows, snapshot.cols
            )
            return GridResult(spec, grid.unpack(snapshot.layers), snapshot.valid_at, "now", snapshot.inputs)
        return await compute_grid(session, settings, now, now)
    hour = floor_hour(at)
    key = (hour, int(now.timestamp() // FORECAST_CACHE_S))
    cached = cache.get(key)
    if cached is None:
        cached = await compute_grid(session, settings, hour, now)
        cache.put(key, cached)
    return cached


def assessments_for(stations: list[Station], weather: WeatherHour | None, now: datetime) -> dict:
    return assess(stations, now, weather.humidity_pct if weather else None)


def nearest_stations(
    stations: list[Station], verdicts: dict[object, Assessment], lat: float, lon: float, limit: int = 3
) -> list[tuple[Station, Assessment, float]]:
    scored = [
        (station, verdicts[station.id], distance_m(lat, lon, station.lat, station.lon))
        for station in stations
        if "pm25" in station.values or "pm10" in station.values
    ]
    scored = [item for item in scored if item[2] <= grid.RADIUS_M]
    return sorted(scored, key=lambda item: item[2])[:limit]


async def point_series(
    session: AsyncSession, settings: Settings, lat: float, lon: float, hours: int, cache: ForecastCache
) -> list[tuple[datetime, dict[str, float]]]:
    """Hourly pollutant values at a point: CAMS at the point plus today's local correction, fading out."""
    now = datetime.now(UTC)
    current = await grid_for(session, settings, None, cache)
    background_now = await backgrounds_at(session, now)
    correction: dict[str, float] = {}
    for parameter in eaqi.POLLUTANTS:
        layer = current.layers.get(parameter)
        value_now = grid.sample(current.spec, layer, lat, lon) if layer is not None else None
        base = grid.bilinear(background_now.get(parameter, {}), np.array([lat]), np.array([lon]))
        if value_now is not None and base is not None and np.isfinite(base[0]):
            correction[parameter] = value_now - float(base[0])
    start = floor_hour(now) + timedelta(hours=1)
    rows = await session.execute(
        select(
            ModelForecast.valid_at,
            ModelForecast.parameter,
            ModelForecast.lat,
            ModelForecast.lon,
            ModelForecast.value,
        ).where(ModelForecast.valid_at.between(start, start + timedelta(hours=hours - 1)))
    )
    lattices: dict[datetime, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
    for valid_at, parameter, plat, plon, value in rows:
        lattices[valid_at][parameter][(plat, plon)] = value
    series = []
    for valid_at in sorted(lattices):
        decay = grid.decay_for((valid_at - now).total_seconds() / 3600)
        values = {}
        for parameter, lattice in lattices[valid_at].items():
            base = grid.bilinear(lattice, np.array([lat]), np.array([lon]))
            if base is not None and np.isfinite(base[0]):
                values[parameter] = max(0.0, float(base[0]) + correction.get(parameter, 0.0) * decay)
        series.append((valid_at, values))
    return series


def best_window(levels: list[tuple[datetime, int]]) -> tuple[datetime, datetime, int] | None:
    """The longest run of consecutive hours at the best (lowest) known level."""
    known = sorted((t, level) for t, level in levels if level)
    if not known:
        return None
    best_level = min(level for _, level in known)
    runs: list[list[datetime]] = []
    for moment, level in known:
        if level != best_level:
            continue
        if runs and runs[-1][1] == moment:
            runs[-1][1] = moment + timedelta(hours=1)
        else:
            runs.append([moment, moment + timedelta(hours=1)])
    start, end = max(runs, key=lambda run: run[1] - run[0])
    return start, end, best_level
