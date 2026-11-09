"""Air data: sensors, measurements, CAMS forecasts, weather, grid snapshots, OSM features, source runs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-09 22:29:47.508357
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "grid_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("valid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("south", sa.Double(), nullable=False),
        sa.Column("west", sa.Double(), nullable=False),
        sa.Column("d_lat", sa.Double(), nullable=False),
        sa.Column("d_lon", sa.Double(), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("cols", sa.Integer(), nullable=False),
        sa.Column("layers", sa.LargeBinary(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grid_snapshots")),
    )
    op.create_index(op.f("ix_grid_snapshots_valid_at"), "grid_snapshots", ["valid_at"], unique=False)
    op.create_table(
        "model_forecasts",
        sa.Column("point_id", sa.String(length=20), nullable=False),
        sa.Column("parameter", sa.String(length=12), nullable=False),
        sa.Column("valid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lat", sa.Double(), nullable=False),
        sa.Column("lon", sa.Double(), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("point_id", "parameter", "valid_at", name=op.f("pk_model_forecasts")),
    )
    op.create_table(
        "osm_features",
        sa.Column("osm_id", sa.String(length=30), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column(
            "geometry",
            geoalchemy2.types.Geometry(
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
                nullable=False,
            ),
            nullable=False,
        ),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("osm_id", name=op.f("pk_osm_features")),
    )
    op.create_index(
        "ix_osm_features_geometry", "osm_features", ["geometry"], unique=False, postgresql_using="gist"
    )
    op.create_table(
        "sensors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column(
            "location",
            geoalchemy2.types.Geometry(
                geometry_type="POINT",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
                nullable=False,
            ),
            nullable=False,
        ),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('low_cost', 'official')", name=op.f("ck_sensors_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sensors")),
        sa.UniqueConstraint("provider", "external_id", name=op.f("uq_sensors_provider")),
    )
    op.create_index("ix_sensors_location", "sensors", ["location"], unique=False, postgresql_using="gist")
    op.create_table(
        "source_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("records", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=200), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'ok', 'failed', 'skipped')", name=op.f("ck_source_runs_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_runs")),
    )
    op.create_index("ix_source_runs_source_started", "source_runs", ["source", "started_at"], unique=False)
    op.create_table(
        "weather_hourly",
        sa.Column("valid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("temperature_c", sa.Double(), nullable=True),
        sa.Column("humidity_pct", sa.Double(), nullable=True),
        sa.Column("wind_speed_ms", sa.Double(), nullable=True),
        sa.Column("wind_dir_deg", sa.Double(), nullable=True),
        sa.Column("precipitation_mm", sa.Double(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("valid_at", name=op.f("pk_weather_hourly")),
    )
    op.create_table(
        "measurements",
        sa.Column("sensor_id", sa.Uuid(), nullable=False),
        sa.Column("parameter", sa.String(length=12), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["sensor_id"], ["sensors.id"], name=op.f("fk_measurements_sensor_id_sensors"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("sensor_id", "parameter", "observed_at", name=op.f("pk_measurements")),
    )
    op.create_index("ix_measurements_observed", "measurements", ["observed_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_measurements_observed", table_name="measurements")
    op.drop_table("measurements")
    op.drop_table("weather_hourly")
    op.drop_index("ix_source_runs_source_started", table_name="source_runs")
    op.drop_table("source_runs")
    op.drop_index("ix_sensors_location", table_name="sensors", postgresql_using="gist")
    op.drop_table("sensors")
    op.drop_index("ix_osm_features_geometry", table_name="osm_features", postgresql_using="gist")
    op.drop_table("osm_features")
    op.drop_table("model_forecasts")
    op.drop_index(op.f("ix_grid_snapshots_valid_at"), table_name="grid_snapshots")
    op.drop_table("grid_snapshots")
