from datetime import date, datetime
from uuid import UUID

from sqlalchemy import JSON, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow
from app.ids import uuid7


class LlmUsage(Base):
    """Agent usage per device and local (Europe/Bucharest) day."""

    __tablename__ = "llm_usage"

    day: Mapped[date] = mapped_column(primary_key=True)
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    messages: Mapped[int] = mapped_column(default=0)
    requests: Mapped[int] = mapped_column(default=0)
    tokens: Mapped[int] = mapped_column(default=0)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    title: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    client_message_id: Mapped[UUID | None]
    role: Mapped[str] = mapped_column(String(10))
    text: Mapped[str] = mapped_column(Text, default="")
    cards: Mapped[list] = mapped_column(JSON, default=list)
    # Compact record of tool calls and results, used to rebuild the model's context.
    tool_trace: Mapped[list] = mapped_column(JSON, default=list)
    engine: Mapped[str | None] = mapped_column(String(10))
    provider: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(80))
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        UniqueConstraint("conversation_id", "client_message_id", "role"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )
