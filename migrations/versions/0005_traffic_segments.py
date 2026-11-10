"""HERE traffic flow snapshot.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10 11:13:59.151786
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "traffic_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "geometry",
            geoalchemy2.types.Geometry(
                geometry_type="LINESTRING",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
                nullable=False,
            ),
            nullable=False,
        ),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column("functional_class", sa.SmallInteger(), nullable=True),
        sa.Column("length_m", sa.Double(), nullable=False),
        sa.Column("jam_factor", sa.Double(), nullable=False),
        sa.Column("speed_kmh", sa.Double(), nullable=True),
        sa.Column("free_flow_kmh", sa.Double(), nullable=True),
        sa.Column("confidence", sa.Double(), nullable=True),
        sa.Column("closed", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_traffic_segments")),
    )
    op.create_index(
        "ix_traffic_segments_geometry",
        "traffic_segments",
        ["geometry"],
        unique=False,
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("ix_traffic_segments_geometry", table_name="traffic_segments", postgresql_using="gist")
    op.drop_table("traffic_segments")
