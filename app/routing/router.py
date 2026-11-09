from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.air.router import forecast_cache
from app.config import Settings, get_settings
from app.db import get_session
from app.errors import ErrorResponse
from app.routing.service import RouteRequest, RoutesOut, plan
from app.security import limit_ip

router = APIRouter(
    tags=["routes"],
    responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
routes_limit = limit_ip("routes", "routes_per_minute_per_ip", 60)


@router.post(
    "/routes", response_model=RoutesOut, response_model_by_alias=True, dependencies=[Depends(routes_limit)]
)
async def routes(
    payload: RouteRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RoutesOut:
    return await plan(session, request.app.state.http, settings, payload, forecast_cache(request))
