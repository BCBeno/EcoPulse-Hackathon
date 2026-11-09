from datetime import date
from uuid import UUID

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class LlmUsage(Base):
    """Agent usage per device and local (Europe/Bucharest) day."""

    __tablename__ = "llm_usage"

    day: Mapped[date] = mapped_column(primary_key=True)
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    messages: Mapped[int] = mapped_column(default=0)
    requests: Mapped[int] = mapped_column(default=0)
    tokens: Mapped[int] = mapped_column(default=0)
