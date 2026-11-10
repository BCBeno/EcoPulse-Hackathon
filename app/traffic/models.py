from datetime import datetime
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import Index, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow
from app.ids import uuid7


class TrafficSegment(Base):
    """The latest HERE flow snapshot; each sync replaces the whole table."""

    __tablename__ = "traffic_segments"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    geometry = mapped_column(Geometry("LINESTRING", srid=4326, spatial_index=False), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200))
    functional_class: Mapped[int | None] = mapped_column(SmallInteger)
    length_m: Mapped[float]
    jam_factor: Mapped[float]
    speed_kmh: Mapped[float | None]
    free_flow_kmh: Mapped[float | None]
    confidence: Mapped[float | None]
    closed: Mapped[bool]
    observed_at: Mapped[datetime]
    retrieved_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_traffic_segments_geometry", "geometry", postgresql_using="gist"),)
