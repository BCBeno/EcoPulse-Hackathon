from datetime import datetime
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import JSON, CheckConstraint, Float, ForeignKey, Index, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow
from app.ids import uuid7

PARAMETERS = ("pm25", "pm10", "no2", "o3", "so2", "temperature", "humidity")


class Sensor(Base):
    """A measuring station: one Sensor.Community location or one OpenAQ location."""

    __tablename__ = "sensors"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    provider: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(10))
    location = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        UniqueConstraint("provider", "external_id"),
        CheckConstraint("kind IN ('low_cost', 'official')", name="kind"),
        Index("ix_sensors_location", "location", postgresql_using="gist"),
    )


class Measurement(Base):
    __tablename__ = "measurements"

    sensor_id: Mapped[UUID] = mapped_column(ForeignKey("sensors.id", ondelete="CASCADE"), primary_key=True)
    parameter: Mapped[str] = mapped_column(String(12), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(primary_key=True)
    value: Mapped[float] = mapped_column(Float)
    retrieved_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_measurements_observed", "observed_at"),)


class ModelForecast(Base):
    """CAMS Europe values (via Open-Meteo) on a fixed lattice of points around the city."""

    __tablename__ = "model_forecasts"

    point_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    parameter: Mapped[str] = mapped_column(String(12), primary_key=True)
    valid_at: Mapped[datetime] = mapped_column(primary_key=True)
    lat: Mapped[float]
    lon: Mapped[float]
    value: Mapped[float]
    retrieved_at: Mapped[datetime] = mapped_column(default=utcnow)


class WeatherHour(Base):
    __tablename__ = "weather_hourly"

    valid_at: Mapped[datetime] = mapped_column(primary_key=True)
    temperature_c: Mapped[float | None]
    humidity_pct: Mapped[float | None]
    wind_speed_ms: Mapped[float | None]
    wind_dir_deg: Mapped[float | None]
    precipitation_mm: Mapped[float | None]
    retrieved_at: Mapped[datetime] = mapped_column(default=utcnow)


class GridSnapshot(Base):
    """The pollution grid "now"; layers are zlib-compressed float32 arrays (row-major from south-west)."""

    __tablename__ = "grid_snapshots"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    valid_at: Mapped[datetime] = mapped_column(index=True)
    computed_at: Mapped[datetime] = mapped_column(default=utcnow)
    south: Mapped[float]
    west: Mapped[float]
    d_lat: Mapped[float]
    d_lon: Mapped[float]
    rows: Mapped[int]
    cols: Mapped[int]
    layers: Mapped[bytes] = mapped_column(LargeBinary)
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)


class OsmFeature(Base):
    __tablename__ = "osm_features"

    osm_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str | None] = mapped_column(String(200))
    geometry = mapped_column(Geometry(srid=4326, spatial_index=False), nullable=False)
    tags: Mapped[dict] = mapped_column(JSON, default=dict)
    retrieved_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_osm_features_geometry", "geometry", postgresql_using="gist"),)


class SourceRun(Base):
    __tablename__ = "source_runs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    source: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(10), default="running")
    records: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (
        CheckConstraint("status IN ('running', 'ok', 'failed', 'skipped')", name="status"),
        Index("ix_source_runs_source_started", "source", "started_at"),
    )
