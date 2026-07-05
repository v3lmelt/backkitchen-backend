"""add source version kind

Revision ID: v8w9x0y1z2a3
Revises: u7v8w9x0y1z2
Create Date: 2026-06-07 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "v8w9x0y1z2a3"
down_revision: Union[str, Sequence[str], None] = "u7v8w9x0y1z2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("track_source_versions"):
        return

    if not _has_column(inspector, "track_source_versions", "source_kind"):
        with op.batch_alter_table("track_source_versions") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "source_kind",
                    sa.String(length=20),
                    nullable=False,
                    server_default="file",
                )
            )

    bind.execute(
        sa.text(
            "UPDATE track_source_versions SET source_kind = 'file' WHERE source_kind IS NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("track_source_versions"):
        return

    if _has_column(inspector, "track_source_versions", "source_kind"):
        with op.batch_alter_table("track_source_versions") as batch_op:
            batch_op.drop_column("source_kind")
