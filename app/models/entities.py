import uuid
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.db.session import Base


def now():
    return datetime.now(UTC)


def identifier():
    return str(uuid.uuid4())


def point_type():
    return (
        Geometry("POINT", srid=4326, spatial_index=True)
        if get_settings().database_url.startswith("postgresql")
        else Text()
    )


class Identity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class User(Identity, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="user")
    active: Mapped[bool] = mapped_column(default=True)


class Report(Identity, Base):
    __tablename__ = "reports"
    creator_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    category: Mapped[str] = mapped_column(String(40), index=True)
    description: Mapped[str] = mapped_column(Text)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    location = mapped_column(point_type(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    severity: Mapped[int] = mapped_column(Integer, default=2)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    moderation: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint("severity BETWEEN 1 AND 5"),
        CheckConstraint("latitude BETWEEN -90 AND 90"),
        CheckConstraint("longitude BETWEEN -180 AND 180"),
        Index("ix_report_coords", "latitude", "longitude"),
    )


class Vote(Identity, Base):
    __tablename__ = "report_votes"
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(20))
    __table_args__ = (UniqueConstraint("report_id", "user_id"),)


class Station(Identity, Base):
    __tablename__ = "stations"
    provider: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(250))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    __table_args__ = (UniqueConstraint("provider", "external_id"),)


class Measurement:
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    source_record_id: Mapped[str] = mapped_column(String(180))
    sensor_id: Mapped[str] = mapped_column(String(180), index=True)
    canonical_sensor_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parameter: Mapped[str] = mapped_column(String(60), index=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    original_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    original_unit: Mapped[str] = mapped_column(String(40))
    unit: Mapped[str] = mapped_column(String(40))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    category: Mapped[str] = mapped_column(String(60), index=True)
    quality: Mapped[dict] = mapped_column(JSON)
    provenance: Mapped[dict] = mapped_column(JSON)


class Observation(Measurement, Base):
    __tablename__ = "air_observations"


class WeatherForecast(Measurement, Base):
    __tablename__ = "weather_forecasts"


class WeatherObservation(Measurement, Base):
    __tablename__ = "weather_reanalysis"


class RoadSegment(Base):
    """Major OpenStreetMap road (trunk ... tertiary), used to estimate traffic congestion."""

    __tablename__ = "road_segments"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # "way/<osm id>"
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    highway: Mapped[str] = mapped_column(String(30), index=True)
    lanes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oneway: Mapped[bool] = mapped_column(default=False)
    signals: Mapped[int] = mapped_column(Integer, default=0)
    length_m: Mapped[float] = mapped_column(Float)
    geometry: Mapped[dict] = mapped_column(JSON)  # GeoJSON LineString, longitude/latitude
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MappedFeature(Identity, Base):
    __tablename__ = "mapped_features"
    source_id: Mapped[str] = mapped_column(String(80), unique=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    geometry: Mapped[dict | None] = mapped_column(JSON)
    tags: Mapped[dict] = mapped_column(JSON)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provenance: Mapped[dict] = mapped_column(JSON)


class PlannedWork(Identity, Base):
    __tablename__ = "planned_works"
    source_id: Mapped[str] = mapped_column(String(100), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text)
    original_url: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    verification: Mapped[str] = mapped_column(String(30), default="needs_review")
    status: Mapped[str] = mapped_column(String(30), default="unknown")
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)


class DataSource(Base):
    __tablename__ = "data_sources"
    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    configuration_missing: Mapped[bool] = mapped_column(default=False)
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_observation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records_processed: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(250))


class IngestionRun(Identity, Base):
    __tablename__ = "ingestion_runs"
    source: Mapped[str] = mapped_column(String(60), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="running")
    discovered: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(250))


class Journey(Identity, Base):
    __tablename__ = "journeys"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    request: Mapped[dict] = mapped_column(JSON)


class Conversation(Identity, Base):
    __tablename__ = "conversations"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    messages: Mapped[list] = mapped_column(JSON, default=list)


class AssistantAction(Identity, Base):
    __tablename__ = "assistant_actions"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    client_message_id: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("owner_id", "client_message_id"),)
