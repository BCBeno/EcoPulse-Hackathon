from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.quota import Quota, device_quota
from app.config import Settings, get_settings
from app.db import get_session
from app.devices.models import Device
from app.security import current_device, hash_token, limit_ip, new_token

router = APIRouter(prefix="/devices", tags=["devices"])


class DeviceCreated(BaseModel):
    device_id: UUID
    token: str
    created_at: datetime


class DeviceMe(BaseModel):
    device_id: UUID
    role: Literal["user", "admin"]
    created_at: datetime
    agent_quota: Quota


@router.post(
    "",
    status_code=201,
    response_model=DeviceCreated,
    dependencies=[Depends(limit_ip("devices", "devices_per_hour_per_ip", 3600))],
)
async def create_device(session: Annotated[AsyncSession, Depends(get_session)]) -> DeviceCreated:
    token = new_token()
    device = Device(token_hash=hash_token(token))
    session.add(device)
    await session.commit()
    return DeviceCreated(device_id=device.id, token=token, created_at=device.created_at)


@router.get("/me", response_model=DeviceMe)
async def me(
    device: Annotated[Device, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DeviceMe:
    return DeviceMe(
        device_id=device.id,
        role=device.role,
        created_at=device.created_at,
        agent_quota=await device_quota(session, settings, device.id),
    )
