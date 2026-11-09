from datetime import datetime
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import CheckConstraint, ForeignKey, Index, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow
from app.ids import uuid7
from app.reports.categories import CATEGORIES


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


CATEGORY_CHECK = _in("category", CATEGORIES)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(20))
    severity: Mapped[int] = mapped_column(SmallInteger)
    description: Mapped[str] = mapped_column(Text)
    original_text: Mapped[str | None] = mapped_column(Text)
    location = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    location_label: Mapped[str | None] = mapped_column(String(120))
    radius_m: Mapped[int]
    observed_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(10), default="active")
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[str | None] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(10))
    extraction: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        CheckConstraint(CATEGORY_CHECK, name="category"),
        CheckConstraint("severity BETWEEN 1 AND 3", name="severity"),
        CheckConstraint(_in("status", ("active", "resolved", "hidden")), name="status"),
        CheckConstraint(_in("resolved_by", ("author", "community", "admin")), name="resolved_by"),
        CheckConstraint(_in("source", ("agent", "form")), name="source"),
        CheckConstraint(_in("extraction", ("llm", "rules", "manual")), name="extraction"),
        Index("ix_reports_location", "location", postgresql_using="gist"),
        Index("ix_reports_status_expires", "status", "expires_at"),
        Index("ix_reports_created", "created_at", "id"),
    )


class ReportVote(Base):
    __tablename__ = "report_votes"

    report_id: Mapped[UUID] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True)
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (CheckConstraint(_in("kind", ("still_there", "gone")), name="kind"),)


class ReportFlag(Base):
    __tablename__ = "report_flags"

    report_id: Mapped[UUID] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True)
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    reason: Mapped[str] = mapped_column(String(15))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        CheckConstraint(_in("reason", ("spam", "offensive", "personal_data", "wrong")), name="reason"),
    )


class ReportDraft(Base):
    """A report prepared by the agent; published only when the user presses the button."""

    __tablename__ = "report_drafts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[UUID | None]
    category: Mapped[str] = mapped_column(String(20))
    severity: Mapped[int] = mapped_column(SmallInteger)
    description: Mapped[str] = mapped_column(Text)
    original_text: Mapped[str | None] = mapped_column(Text)
    location = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=True)
    location_label: Mapped[str | None] = mapped_column(String(120))
    location_source: Mapped[str] = mapped_column(String(10))
    extraction: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10), default="pending")
    report_id: Mapped[UUID | None] = mapped_column(ForeignKey("reports.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        CheckConstraint(CATEGORY_CHECK, name="category"),
        CheckConstraint("severity BETWEEN 1 AND 3", name="severity"),
        CheckConstraint(_in("location_source", ("place", "gps", "map", "none")), name="location_source"),
        CheckConstraint(_in("extraction", ("llm", "rules")), name="extraction"),
        CheckConstraint(_in("status", ("pending", "published", "cancelled")), name="status"),
    )
