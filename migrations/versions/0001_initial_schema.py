"""Initial schema: devices, reports (drafts, votes, flags) and agent usage.

Revision ID: 0001
Revises:
Create Date: 2026-11-09 18:08:32.508597
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.create_table(
        "devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('user', 'admin')", name=op.f("ck_devices_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_devices")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_devices_token_hash")),
    )
    op.create_table(
        "llm_usage",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("messages", sa.Integer(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name=op.f("fk_llm_usage_device_id_devices"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("day", "device_id", name=op.f("pk_llm_usage")),
    )
    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("severity", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=True),
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
        sa.Column("location_label", sa.String(length=120), nullable=True),
        sa.Column("radius_m", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=10), nullable=True),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("extraction", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('smoke', 'dust', 'industrial', 'construction', 'machinery', 'traffic', 'obstruction', 'flooding', 'infrastructure', 'lighting', 'other')",
            name=op.f("ck_reports_category"),
        ),
        sa.CheckConstraint("extraction IN ('llm', 'rules', 'manual')", name=op.f("ck_reports_extraction")),
        sa.CheckConstraint(
            "resolved_by IN ('author', 'community', 'admin')", name=op.f("ck_reports_resolved_by")
        ),
        sa.CheckConstraint("source IN ('agent', 'form')", name=op.f("ck_reports_source")),
        sa.CheckConstraint("status IN ('active', 'resolved', 'hidden')", name=op.f("ck_reports_status")),
        sa.CheckConstraint("severity BETWEEN 1 AND 3", name=op.f("ck_reports_severity")),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name=op.f("fk_reports_device_id_devices"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
    )
    op.create_index("ix_reports_created", "reports", ["created_at", "id"], unique=False)
    op.create_index(op.f("ix_reports_device_id"), "reports", ["device_id"], unique=False)
    op.create_index("ix_reports_location", "reports", ["location"], unique=False, postgresql_using="gist")
    op.create_index("ix_reports_status_expires", "reports", ["status", "expires_at"], unique=False)
    op.create_table(
        "report_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("severity", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=True),
        sa.Column(
            "location",
            geoalchemy2.types.Geometry(
                geometry_type="POINT",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.Column("location_label", sa.String(length=120), nullable=True),
        sa.Column("location_source", sa.String(length=10), nullable=False),
        sa.Column("extraction", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('smoke', 'dust', 'industrial', 'construction', 'machinery', 'traffic', 'obstruction', 'flooding', 'infrastructure', 'lighting', 'other')",
            name=op.f("ck_report_drafts_category"),
        ),
        sa.CheckConstraint("extraction IN ('llm', 'rules')", name=op.f("ck_report_drafts_extraction")),
        sa.CheckConstraint(
            "location_source IN ('place', 'gps', 'map', 'none')",
            name=op.f("ck_report_drafts_location_source"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'published', 'cancelled')", name=op.f("ck_report_drafts_status")
        ),
        sa.CheckConstraint("severity BETWEEN 1 AND 3", name=op.f("ck_report_drafts_severity")),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name=op.f("fk_report_drafts_device_id_devices"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name=op.f("fk_report_drafts_report_id_reports"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_drafts")),
    )
    op.create_index(op.f("ix_report_drafts_device_id"), "report_drafts", ["device_id"], unique=False)
    op.create_table(
        "report_flags",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=15), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reason IN ('spam', 'offensive', 'personal_data', 'wrong')", name=op.f("ck_report_flags_reason")
        ),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name=op.f("fk_report_flags_device_id_devices"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["report_id"], ["reports.id"], name=op.f("fk_report_flags_report_id_reports"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("report_id", "device_id", name=op.f("pk_report_flags")),
    )
    op.create_table(
        "report_votes",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('still_there', 'gone')", name=op.f("ck_report_votes_kind")),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name=op.f("fk_report_votes_device_id_devices"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["report_id"], ["reports.id"], name=op.f("fk_report_votes_report_id_reports"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("report_id", "device_id", name=op.f("pk_report_votes")),
    )


def downgrade() -> None:
    op.drop_table("report_votes")
    op.drop_table("report_flags")
    op.drop_index(op.f("ix_report_drafts_device_id"), table_name="report_drafts")
    op.drop_table("report_drafts")
    op.drop_index("ix_reports_status_expires", table_name="reports")
    op.drop_index("ix_reports_location", table_name="reports", postgresql_using="gist")
    op.drop_index(op.f("ix_reports_device_id"), table_name="reports")
    op.drop_index("ix_reports_created", table_name="reports")
    op.drop_table("reports")
    op.drop_table("llm_usage")
    op.drop_table("devices")
