from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow
from app.ids import uuid7


class Device(Base):
    """An anonymous identity: one per installed app/browser."""

    __tablename__ = "devices"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(10), default="user")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (CheckConstraint("role IN ('user', 'admin')", name="role"),)
