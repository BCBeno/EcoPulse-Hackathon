"""Places cache for Photon search and Nominatim reverse geocoding.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-09 22:46:10.061237
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "places_cache",
        sa.Column("query", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("query", "provider", name=op.f("pk_places_cache")),
    )


def downgrade() -> None:
    op.drop_table("places_cache")
