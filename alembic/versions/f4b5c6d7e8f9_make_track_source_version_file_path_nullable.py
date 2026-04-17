"""make track source version file_path nullable

Revision ID: f4b5c6d7e8f9
Revises: f3a2b1c4d5e6
Create Date: 2026-04-16 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "f3a2b1c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("track_source_versions") as batch_op:
        batch_op.alter_column(
            "file_path",
            existing_type=sa.String(length=500),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("track_source_versions") as batch_op:
        batch_op.alter_column(
            "file_path",
            existing_type=sa.String(length=500),
            nullable=False,
        )
