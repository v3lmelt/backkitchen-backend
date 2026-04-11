"""realign issue status and severity columns to enum

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-04-11 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("issues", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.VARCHAR(length=9),
            type_=sa.Enum(
                "open", "pending_discussion", "disagreed", "resolved",
                name="issuestatus",
            ),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "severity",
            existing_type=sa.VARCHAR(length=10),
            type_=sa.Enum(
                "critical", "major", "minor", "suggestion",
                name="issueseverity",
            ),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("issues", schema=None) as batch_op:
        batch_op.alter_column(
            "severity",
            existing_type=sa.Enum(
                "critical", "major", "minor", "suggestion",
                name="issueseverity",
            ),
            type_=sa.VARCHAR(length=10),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum(
                "open", "pending_discussion", "disagreed", "resolved",
                name="issuestatus",
            ),
            type_=sa.VARCHAR(length=9),
            existing_nullable=False,
        )
