from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.errors import ErrorResponse, outside_city
from app.geo import LatLon
from app.places import service
from app.places.service import Place
from app.security import limit_ip, read_limit

router = APIRouter(prefix="/places", tags=["places"], responses={422: {"model": ErrorResponse}})
Session = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
geocode_limit = limit_ip("geocode", "geocode_per_minute_per_ip", 60)


class PlaceList(BaseModel):
    items: list[Place]


class ReverseOut(BaseModel):
    label: str | None


@router.get("/search", response_model=PlaceList, dependencies=[Depends(read_limit), Depends(geocode_limit)])
async def search(
    request: Request,
    session: Session,
    settings: SettingsDep,
    q: Annotated[str, Query(min_length=2, max_length=120)],
    lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
) -> PlaceList:
    near = LatLon(lat=lat, lon=lon) if lat is not None and lon is not None else None
    items = await service.search(session, request.app.state.http, settings.city_bbox, q, near)
    return PlaceList(items=items)


@router.get("/reverse", response_model=ReverseOut, dependencies=[Depends(read_limit), Depends(geocode_limit)])
async def reverse(
    request: Request,
    session: Session,
    settings: SettingsDep,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lon: Annotated[float, Query(ge=-180, le=180)],
) -> ReverseOut:
    if not settings.city_bbox.contains(lat, lon):
        raise outside_city()
    return ReverseOut(label=await service.reverse(session, request.app.state.http, lat, lon))
