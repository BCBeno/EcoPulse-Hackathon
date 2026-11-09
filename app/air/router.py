import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

import numpy as np
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.air import eaqi, grid, service
from app.air.ingest import last_success
from app.air.models import OsmFeature
from app.config import Settings, get_settings
from app.db import get_session
from app.devices.models import Device
from app.errors import ErrorResponse, invalid, outside_city
from app.geo import LatLon
from app.reports.models import Report
from app.reports.schemas import ReportOut
from app.reports.service import active_filter, to_out
from app.security import optional_device, read_limit

router = APIRouter(tags=["air"], responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}})
Session = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

LIMITATION = (
    "Estimare din senzorii din apropiere și modelul CAMS (~11 km); nu e o măsurătoare exactă la stradă."
)
FORECAST_SOURCE = "CAMS Europe (Open-Meteo), corectat cu senzorii locali"
HUMIDITY_NOTE = "Umiditate ridicată: senzorii ieftini pot supraestima particulele."
MAX_REPORTS_ON_MAP = 500


def _cache(request: Request) -> service.ForecastCache:
    if not hasattr(request.app.state, "forecast_cache"):
        request.app.state.forecast_cache = service.ForecastCache()
    return request.app.state.forecast_cache


def _check_at(at: datetime | None, settings: Settings) -> datetime | None:
    if at is None:
        return None
    now = datetime.now(UTC)
    if at < now - timedelta(hours=1) or at > now + timedelta(hours=settings.forecast_horizon_h):
        raise invalid(
            "at", f"Ora trebuie să fie între acum și următoarele {settings.forecast_horizon_h} de ore."
        )
    return at


# --- /map -----------------------------------------------------------------------------------------


class GridOut(BaseModel):
    valid_at: datetime
    south: float
    west: float
    d_lat: float
    d_lon: float
    rows: int
    cols: int
    layers: dict[str, list[int | float | None]]


class Reading(BaseModel):
    value: float
    observed_at: datetime


class SensorOut(BaseModel):
    id: UUID
    provider: str
    kind: Literal["low_cost", "official"]
    location: LatLon
    readings: dict[str, Reading]
    eaqi_level: int | None
    status: Literal["ok", "stale", "outlier"]
    qa_flags: list[str]


class ConstructionOut(BaseModel):
    id: str
    geometry: dict
    name: str | None
    source: Literal["osm"] = "osm"


class SourcesSummary(BaseModel):
    sensor_count: int
    official_count: int
    newest_reading_at: datetime | None
    model: str
    last_success: dict[str, datetime]


class MapOut(BaseModel):
    generated_at: datetime
    at: datetime
    kind: Literal["now", "forecast"]
    grid: GridOut | None
    sensors: list[SensorOut]
    reports: list[ReportOut]
    construction: list[ConstructionOut]
    sources: SourcesSummary


def _layer(values: np.ndarray, digits: int, as_int: bool = False) -> list[int | float | None]:
    flat = values.reshape(-1)
    if as_int:
        return [int(v) if v > 0 else None for v in flat]
    return [round(float(v), digits) if np.isfinite(v) else None for v in flat]


def _grid_out(result: service.GridResult) -> GridOut | None:
    if "pm25" not in result.layers and not np.any(result.layers.get("eaqi", np.zeros(1))):
        return None
    spec = result.spec
    layers = {"eaqi": _layer(result.layers["eaqi"], 0, as_int=True)}
    if "pm25" in result.layers:
        layers["pm25"] = _layer(result.layers["pm25"], 1)
    layers["confidence"] = _layer(result.layers["confidence"], 2)
    return GridOut(
        valid_at=result.valid_at,
        south=spec.south,
        west=spec.west,
        d_lat=spec.d_lat,
        d_lon=spec.d_lon,
        rows=spec.rows,
        cols=spec.cols,
        layers=layers,
    )


def _shows_air(station) -> bool:
    """Weather-only stations (e.g. a lone BME280) are not air sensors; stale ones stay visible."""
    return not station.values or any(p in station.values for p in eaqi.POLLUTANTS)


def _sensor_out(station, verdict) -> SensorOut:
    level, _ = eaqi.overall({p: station.values.get(p) for p in eaqi.POLLUTANTS})
    return SensorOut(
        id=station.id,
        provider=station.provider,
        kind=station.kind,
        location=LatLon(lat=station.lat, lon=station.lon),
        readings={
            parameter: Reading(value=round(value, 1), observed_at=station.observed_at[parameter])
            for parameter, value in station.values.items()
        },
        eaqi_level=level if verdict.status == "ok" else None,
        status=verdict.status,
        qa_flags=verdict.flags,
    )


@router.get(
    "/map",
    response_model=MapOut,
    dependencies=[Depends(read_limit)],
    responses={304: {"description": "Nothing changed since the ETag in If-None-Match"}},
)
async def map_data(
    request: Request,
    session: Session,
    settings: SettingsDep,
    device: Annotated[Device | None, Depends(optional_device)],
    at: AwareDatetime | None = None,
):
    now = datetime.now(UTC)
    result = await service.grid_for(session, settings, _check_at(at, settings), _cache(request))
    stations = await service.load_stations(session, now)
    weather = await service.weather_at(session, now)
    verdicts = service.assessments_for(stations, weather, now)
    shown = [s for s in stations if _shows_air(s)]
    reports = list(
        await session.scalars(
            select(Report)
            .where(active_filter(now))
            .order_by(Report.created_at.desc())
            .limit(MAX_REPORTS_ON_MAP)
        )
    )
    features = await session.execute(
        select(OsmFeature.osm_id, OsmFeature.name, func.ST_AsGeoJSON(OsmFeature.geometry, 6))
    )
    body = MapOut(
        generated_at=now,
        at=result.valid_at,
        kind=result.kind,
        grid=_grid_out(result),
        sensors=[_sensor_out(s, verdicts[s.id]) for s in shown],
        reports=await to_out(session, reports, device, now),
        construction=[
            ConstructionOut(id=osm_id, name=name, geometry=json.loads(geometry))
            for osm_id, name, geometry in features
        ],
        sources=SourcesSummary(
            sensor_count=sum(1 for s in shown if s.kind == "low_cost"),
            official_count=sum(1 for s in shown if s.kind == "official"),
            newest_reading_at=max((s.newest() for s in shown if s.newest()), default=None),
            model="CAMS Europe (~11 km)",
            last_success=await last_success(session),
        ),
    )
    # The ETag ignores generated_at so an unchanged map really answers 304.
    payload = body.model_dump(mode="json", exclude={"generated_at"})
    etag = '"' + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        content=body.model_dump_json(),
        media_type="application/json",
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )


# --- /air/point -----------------------------------------------------------------------------------


class Pollutant(BaseModel):
    value: float
    unit: Literal["µg/m³"] = "µg/m³"
    level: int | None
    source: Literal["model"] | None = None


class EaqiOut(BaseModel):
    level: int | None
    dominant: str | None


class NearestSensor(BaseModel):
    id: UUID
    kind: Literal["low_cost", "official"]
    distance_m: int
    pm25: float | None
    observed_at: datetime | None
    qa_flags: list[str]


class WeatherOut(BaseModel):
    temperature_c: float | None
    humidity_pct: float | None
    wind_speed_ms: float | None
    wind_dir_deg: float | None
    precipitation_mm: float | None


class AirPoint(BaseModel):
    location: LatLon
    place_label: str | None
    at: datetime
    kind: Literal["now", "forecast"]
    eaqi: EaqiOut
    pollutants: dict[str, Pollutant]
    confidence: float
    nearest_sensors: list[NearestSensor]
    weather: WeatherOut | None
    notes: list[str]
    limitation: str


def _in_city(lat: float, lon: float, settings: Settings) -> None:
    if not settings.city_bbox.contains(lat, lon):
        raise outside_city()


async def air_point(
    session: AsyncSession, settings: Settings, cache: service.ForecastCache, lat: float, lon: float, at
) -> AirPoint:
    _in_city(lat, lon, settings)
    now = datetime.now(UTC)
    result = await service.grid_for(session, settings, _check_at(at, settings), cache)
    pollutants: dict[str, Pollutant] = {}
    for parameter in eaqi.POLLUTANTS:
        layer = result.layers.get(parameter)
        value = grid.sample(result.spec, layer, lat, lon) if layer is not None else None
        if value is not None:
            pollutants[parameter] = Pollutant(
                value=round(value, 1),
                level=eaqi.sub_index(parameter, value),
                # Low-cost sensors only measure particles; gases come from the model (and official stations).
                source=None if parameter in ("pm25", "pm10") else "model",
            )
    level, dominant = eaqi.overall({p: v.value for p, v in pollutants.items()})
    stations = await service.load_stations(session, now)
    weather = await service.weather_at(session, result.valid_at)
    verdicts = service.assessments_for(stations, weather, now)
    nearest = service.nearest_stations(stations, verdicts, lat, lon)
    notes = []
    if (weather and (weather.humidity_pct or 0) >= 90) or any(
        "high_humidity" in v.flags for _, v, _ in nearest
    ):
        notes.append(HUMIDITY_NOTE)
    confidence = grid.sample(result.spec, result.layers["confidence"], lat, lon) or 0.0
    return AirPoint(
        location=LatLon(lat=lat, lon=lon),
        place_label=None,
        at=result.valid_at,
        kind=result.kind,
        eaqi=EaqiOut(level=level, dominant=dominant),
        pollutants=pollutants,
        confidence=round(confidence, 2),
        nearest_sensors=[
            NearestSensor(
                id=station.id,
                kind=station.kind,
                distance_m=round(distance),
                pm25=round(station.values["pm25"], 1) if "pm25" in station.values else None,
                observed_at=station.newest(),
                qa_flags=verdict.flags,
            )
            for station, verdict, distance in nearest
        ],
        weather=WeatherOut(
            temperature_c=weather.temperature_c,
            humidity_pct=weather.humidity_pct,
            wind_speed_ms=weather.wind_speed_ms,
            wind_dir_deg=weather.wind_dir_deg,
            precipitation_mm=weather.precipitation_mm,
        )
        if weather
        else None,
        notes=notes,
        limitation=LIMITATION,
    )


@router.get("/air/point", response_model=AirPoint, dependencies=[Depends(read_limit)])
async def get_air_point(
    request: Request,
    session: Session,
    settings: SettingsDep,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lon: Annotated[float, Query(ge=-180, le=180)],
    at: AwareDatetime | None = None,
) -> AirPoint:
    return await air_point(session, settings, _cache(request), lat, lon, at)


# --- /air/forecast --------------------------------------------------------------------------------


class ForecastHour(BaseModel):
    at: datetime
    eaqi_level: int | None
    pm25: float | None
    pm10: float | None
    no2: float | None
    o3: float | None


class BestWindow(BaseModel):
    start: datetime
    end: datetime
    eaqi_level: int


class AirForecast(BaseModel):
    location: LatLon
    hourly: list[ForecastHour]
    best_window: BestWindow | None
    source: str
    limitation: str


async def air_forecast(
    session: AsyncSession,
    settings: Settings,
    cache: service.ForecastCache,
    lat: float,
    lon: float,
    hours: int,
) -> AirForecast:
    _in_city(lat, lon, settings)
    series = await service.point_series(session, settings, lat, lon, hours, cache)
    hourly = []
    for moment, values in series:
        level, _ = eaqi.overall(values)

        def rounded(parameter: str, values=values) -> float | None:
            return round(values[parameter], 1) if parameter in values else None

        hourly.append(
            ForecastHour(
                at=moment,
                eaqi_level=level,
                pm25=rounded("pm25"),
                pm10=rounded("pm10"),
                no2=rounded("no2"),
                o3=rounded("o3"),
            )
        )
    window = service.best_window([(h.at, h.eaqi_level) for h in hourly])
    return AirForecast(
        location=LatLon(lat=lat, lon=lon),
        hourly=hourly,
        best_window=BestWindow(start=window[0], end=window[1], eaqi_level=window[2]) if window else None,
        source=FORECAST_SOURCE,
        limitation=LIMITATION,
    )


@router.get("/air/forecast", response_model=AirForecast, dependencies=[Depends(read_limit)])
async def get_air_forecast(
    request: Request,
    session: Session,
    settings: SettingsDep,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lon: Annotated[float, Query(ge=-180, le=180)],
    hours: Annotated[int, Query(ge=1, le=72)] = 24,
) -> AirForecast:
    return await air_forecast(session, settings, _cache(request), lat, lon, hours)
