import hashlib
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.air import eaqi
from app.config import Settings, get_settings
from app.db import get_session
from app.geo import LatLon
from app.reports.categories import (
    CATEGORIES,
    DESCRIPTION_MAX,
    GONE_VOTES_TO_RESOLVE,
    ORIGINAL_TEXT_MAX,
    SEVERITIES,
)

router = APIRouter(tags=["system"])


class Health(BaseModel):
    status: Literal["ok"]


class Ready(BaseModel):
    status: Literal["ok", "degraded", "down"]
    checks: dict[str, str | dict]


@router.get("/health", response_model=Health)
async def health() -> Health:
    return Health(status="ok")


@router.get("/health/ready", response_model=Ready)
async def ready(session: Annotated[AsyncSession, Depends(get_session)], response: Response) -> Ready:
    checks: dict[str, str | dict] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        checks["db"] = "down"
    status = "ok" if checks["db"] == "ok" else "down"
    if status == "down":
        response.status_code = 503
    return Ready(status=status, checks=checks)


class BBox(BaseModel):
    south: float
    west: float
    north: float
    east: float


class City(BaseModel):
    name: str
    timezone: str
    center: LatLon
    bbox: BBox


class AqiLevel(BaseModel):
    level: int
    key: str
    label: str
    color: str
    advice: str


class ReportCategoryOut(BaseModel):
    id: str
    label: str
    icon: str
    affects: Literal["air", "obstacle", "air_obstacle", "info"]
    duration_h: int
    radius_m: int


class Severity(BaseModel):
    value: int
    label: str


class ReportRules(BaseModel):
    gone_votes_to_resolve: int
    description_max: int
    text_max: int


class Labelled(BaseModel):
    id: str
    label: str


class MapConfig(BaseModel):
    poll_interval_s: int


class AgentConfig(BaseModel):
    enabled: bool
    daily_messages: int


class Attribution(BaseModel):
    label: str
    url: str


class PublicConfig(BaseModel):
    city: City
    aqi_levels: list[AqiLevel]
    aqi_note: str
    report_categories: list[ReportCategoryOut]
    severities: list[Severity]
    report_rules: ReportRules
    modes: list[Labelled]
    route_kinds: list[Labelled]
    forecast_horizon_h: int
    map: MapConfig
    agent: AgentConfig
    attributions: list[Attribution]


def build_config(settings: Settings) -> PublicConfig:
    bbox = settings.city_bbox
    return PublicConfig(
        city=City(
            name=settings.city_name,
            timezone=settings.city_timezone,
            center=LatLon(lat=settings.city_center_lat, lon=settings.city_center_lon),
            bbox=BBox(south=bbox.south, west=bbox.west, north=bbox.north, east=bbox.east),
        ),
        aqi_levels=[AqiLevel(**vars(level)) for level in eaqi.LEVELS],
        aqi_note=eaqi.NOTE,
        report_categories=[ReportCategoryOut(**vars(c)) for c in CATEGORIES.values()],
        severities=[Severity(value=v, label=label) for v, label in SEVERITIES.items()],
        report_rules=ReportRules(
            gone_votes_to_resolve=GONE_VOTES_TO_RESOLVE,
            description_max=DESCRIPTION_MAX,
            text_max=ORIGINAL_TEXT_MAX,
        ),
        modes=[Labelled(id="walk", label="Pe jos"), Labelled(id="bike", label="Bicicletă")],
        route_kinds=[
            Labelled(id="cleanest", label="Cel mai curat"),
            Labelled(id="balanced", label="Echilibrat"),
            Labelled(id="fastest", label="Cel mai rapid"),
        ],
        forecast_horizon_h=settings.forecast_horizon_h,
        map=MapConfig(poll_interval_s=settings.map_poll_interval_s),
        agent=AgentConfig(enabled=True, daily_messages=settings.agent_daily_messages),
        attributions=[
            Attribution(label="© OpenStreetMap contributors", url="https://www.openstreetmap.org/copyright"),
            Attribution(label="Sensor.Community", url="https://sensor.community/"),
            Attribution(label="OpenAQ și furnizorii originali", url="https://openaq.org/"),
            Attribution(label="Copernicus CAMS / Open-Meteo (CC BY 4.0)", url="https://open-meteo.com/"),
        ],
    )


@router.get("/config", response_model=PublicConfig)
async def public_config(
    request: Request, response: Response, settings: Annotated[Settings, Depends(get_settings)]
):
    config = build_config(settings)
    body = config.model_dump_json()
    etag = '"' + hashlib.sha256(body.encode()).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return config
