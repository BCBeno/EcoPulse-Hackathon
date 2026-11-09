from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Bounds, Settings, get_settings
from app.db import get_session
from app.devices.models import Device
from app.errors import ErrorResponse, invalid
from app.reports import service
from app.reports.categories import CategoryId
from app.reports.schemas import DraftPublish, FlagIn, ReportCreate, ReportOut, ReportPage, VoteIn
from app.security import current_device, optional_device, read_limit, write_limit

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)

Session = Annotated[AsyncSession, Depends(get_session)]
OptionalDevice = Annotated[Device | None, Depends(optional_device)]
CurrentDevice = Annotated[Device, Depends(current_device)]


def _bbox(south, west, north, east) -> Bounds | None:
    values = (south, west, north, east)
    if all(v is None for v in values):
        return None
    if any(v is None for v in values) or not (south < north and west < east):
        raise invalid("bbox", "Zona trebuie dată complet: south < north și west < east.")
    return Bounds(south, west, north, east)


@router.get("", response_model=ReportPage, dependencies=[Depends(read_limit)])
async def list_reports(
    session: Session,
    device: OptionalDevice,
    status: Literal["active", "expired", "resolved"] = "active",
    category: CategoryId | None = None,
    south: Annotated[float | None, Query(ge=-90, le=90)] = None,
    west: Annotated[float | None, Query(ge=-180, le=180)] = None,
    north: Annotated[float | None, Query(ge=-90, le=90)] = None,
    east: Annotated[float | None, Query(ge=-180, le=180)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> ReportPage:
    return await service.list_reports(
        session,
        status=status,
        category=category,
        bbox=_bbox(south, west, north, east),
        limit=limit,
        cursor=cursor,
        device=device,
    )


@router.get("/{report_id}", response_model=ReportOut, dependencies=[Depends(read_limit)])
async def get_report(report_id: UUID, session: Session, device: OptionalDevice) -> ReportOut:
    return await service.one_out(session, await service.visible_report(session, report_id), device)


@router.post("", status_code=201, response_model=ReportOut, dependencies=[Depends(write_limit)])
async def create_report(
    payload: ReportCreate,
    session: Session,
    device: CurrentDevice,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReportOut:
    report = await service.create_report(session, device, payload, settings)
    await session.commit()
    return await service.one_out(session, report, device)


@router.post(
    "/drafts/{draft_id}/publish",
    status_code=201,
    response_model=ReportOut,
    dependencies=[Depends(write_limit)],
)
async def publish_draft(
    draft_id: UUID,
    payload: DraftPublish,
    session: Session,
    device: CurrentDevice,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReportOut:
    report = await service.publish_draft(session, device, draft_id, payload, settings)
    await session.commit()
    return await service.one_out(session, report, device)


@router.delete("/drafts/{draft_id}", status_code=204, dependencies=[Depends(write_limit)])
async def cancel_draft(draft_id: UUID, session: Session, device: CurrentDevice) -> Response:
    await service.cancel_draft(session, device, draft_id)
    await session.commit()
    return Response(status_code=204)


@router.post("/{report_id}/votes", response_model=ReportOut, dependencies=[Depends(write_limit)])
async def vote(report_id: UUID, payload: VoteIn, session: Session, device: CurrentDevice) -> ReportOut:
    report = await service.vote(session, device, report_id, payload.kind)
    await session.commit()
    return await service.one_out(session, report, device)


@router.post("/{report_id}/resolve", response_model=ReportOut, dependencies=[Depends(write_limit)])
async def resolve(report_id: UUID, session: Session, device: CurrentDevice) -> ReportOut:
    report = await service.resolve(session, device, report_id)
    await session.commit()
    return await service.one_out(session, report, device)


@router.post("/{report_id}/flags", status_code=204, dependencies=[Depends(write_limit)])
async def flag(report_id: UUID, payload: FlagIn, session: Session, device: CurrentDevice) -> Response:
    await service.flag(session, device, report_id, payload.reason)
    await session.commit()
    return Response(status_code=204)
