"""Idempotent assistant reporting actions."""

import sqlalchemy as sa
from alembic import op

revision = "8b184b2ae303"
down_revision = "3af40f774be4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "assistant_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("client_message_id", sa.String(36), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_id", "client_message_id"),
    )


def downgrade():
    op.drop_table("assistant_actions")
