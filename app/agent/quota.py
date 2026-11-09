from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import LlmUsage
from app.config import Settings


class Quota(BaseModel):
    limit: int
    used: int
    resets_at: datetime


def local_day(settings: Settings, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(settings.tz).date()


def next_reset(settings: Settings, now: datetime | None = None) -> datetime:
    tomorrow = local_day(settings, now) + timedelta(days=1)
    return datetime.combine(tomorrow, time(0), tzinfo=settings.tz).astimezone(UTC)


async def device_quota(session: AsyncSession, settings: Settings, device_id: UUID) -> Quota:
    used = await session.scalar(
        select(LlmUsage.messages).where(LlmUsage.day == local_day(settings), LlmUsage.device_id == device_id)
    )
    return Quota(limit=settings.agent_daily_messages, used=used or 0, resets_at=next_reset(settings))
