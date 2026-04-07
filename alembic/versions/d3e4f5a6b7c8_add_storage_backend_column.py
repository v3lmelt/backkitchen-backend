"""add storage_backend column to audio tables

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-04-06 24:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, Sequence[str], None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tracks") as batch_op:
        batch_op.add_column(
            sa.Column("storage_backend", sa.String(10), nullable=False, server_default="local")
        )
    with op.batch_alter_table("track_source_versions") as batch_op:
        batch_op.add_column(
            sa.Column("storage_backend", sa.String(10), nullable=False, server_default="local")
        )
    with op.batch_alter_table("master_deliveries") as batch_op:
        batch_op.add_column(
            sa.Column("storage_backend", sa.String(10), nullable=False, server_default="local")
        )
    with op.batch_alter_table("comment_audios") as batch_op:
        batch_op.add_column(
            sa.Column("storage_backend", sa.String(10), nullable=False, server_default="local")
        )


def downgrade() -> None:
    with op.batch_alter_table("comment_audios") as batch_op:
        batch_op.drop_column("storage_backend")
    with op.batch_alter_table("master_deliveries") as batch_op:
        batch_op.drop_column("storage_backend")
    with op.batch_alter_table("track_source_versions") as batch_op:
        batch_op.drop_column("storage_backend")
    with op.batch_alter_table("tracks") as batch_op:
        batch_op.drop_column("storage_backend")
