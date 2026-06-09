"""ensure requested_revision_type column

Revision ID: x0y1z2a3b4c5
Revises: w9x0y1z2a3b4
Create Date: 2026-06-09 10:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "x0y1z2a3b4c5"
down_revision: Union[str, Sequence[str], None] = "w9x0y1z2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("tracks") and not _has_column(inspector, "tracks", "requested_revision_type"):
        with op.batch_alter_table("tracks", schema=None) as batch_op:
            batch_op.add_column(sa.Column("requested_revision_type", sa.String(length=20), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("tracks") and _has_column(inspector, "tracks", "requested_revision_type"):
        with op.batch_alter_table("tracks", schema=None) as batch_op:
            batch_op.drop_column("requested_revision_type")
