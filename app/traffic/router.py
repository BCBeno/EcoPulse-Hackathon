import hashlib
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.geo import GeoLineString
from app.security import read_limit
from app.traffic import service

router = APIRouter(tags=["traffic"])

LevelKey = Literal["free", "moderate", "heavy", "jammed", "closed"]
ATTRIBUTION = "Date de trafic: HERE"


class TrafficSegmentOut(BaseModel):
    id: str
    geometry: GeoLineString
    description: str | None
    level: LevelKey
    jam_factor: float
    speed_kmh: float | None
    free_flow_kmh: float | None


class TrafficOut(BaseModel):
    status: Literal["ok", "not_configured", "no_data"]
    observed_at: datetime | None
    segments: list[TrafficSegmentOut]
    attribution: str


@router.get(
    "/traffic",
    response_model=TrafficOut,
    dependencies=[Depends(read_limit)],
    responses={304: {"description": "Nothing changed since the ETag in If-None-Match"}},
)
async def traffic(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    rows = await service.current(session, datetime.now(UTC))
    status = "ok" if rows else ("not_configured" if not settings.here_api_key else "no_data")
    body = TrafficOut(
        status=status,
        observed_at=rows[0].observed_at if rows else None,
        segments=[
            TrafficSegmentOut(
                id=row.id,
                geometry=row.geometry,
                description=row.description,
                level=service.level(row.jam_factor, row.closed).key,
                jam_factor=round(row.jam_factor, 1),
                speed_kmh=row.speed_kmh,
                free_flow_kmh=row.free_flow_kmh,
            )
            for row in rows
        ],
        attribution=ATTRIBUTION,
    )
    payload = body.model_dump_json()
    etag = '"' + hashlib.sha256(payload.encode()).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        content=payload, media_type="application/json", headers={"ETag": etag, "Cache-Control": "no-cache"}
    )
