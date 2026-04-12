"""add internal_resolved issue status

Revision ID: b1c2d3e4f5a6
Revises: a9b8c7d6e5f4
Create Date: 2026-04-12 18:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a9b8c7d6e5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("issues", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum(
                "open", "pending_discussion", "disagreed", "resolved",
                name="issuestatus",
            ),
            type_=sa.Enum(
                "open", "pending_discussion", "internal_resolved", "disagreed", "resolved",
                name="issuestatus",
            ),
            existing_nullable=False,
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE issues SET status = 'resolved' WHERE status = 'internal_resolved'"
        )
    )
    with op.batch_alter_table("issues", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum(
                "open", "pending_discussion", "internal_resolved", "disagreed", "resolved",
                name="issuestatus",
            ),
            type_=sa.Enum(
                "open", "pending_discussion", "disagreed", "resolved",
                name="issuestatus",
            ),
            existing_nullable=False,
        )
