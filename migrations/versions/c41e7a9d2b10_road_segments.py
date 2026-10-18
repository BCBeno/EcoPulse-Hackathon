"""Major OpenStreetMap roads for the traffic congestion estimate."""

import sqlalchemy as sa
from alembic import op

revision = "c41e7a9d2b10"
down_revision = "8b184b2ae303"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "road_segments",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("highway", sa.String(30), nullable=False),
        sa.Column("lanes", sa.Integer(), nullable=True),
        sa.Column("oneway", sa.Boolean(), nullable=False),
        sa.Column("signals", sa.Integer(), nullable=False),
        sa.Column("length_m", sa.Float(), nullable=False),
        sa.Column("geometry", sa.JSON(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_road_segments_highway"), "road_segments", ["highway"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_road_segments_highway"), table_name="road_segments")
    op.drop_table("road_segments")
