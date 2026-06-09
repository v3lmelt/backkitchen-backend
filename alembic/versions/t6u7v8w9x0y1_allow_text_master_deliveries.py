"""allow text master deliveries

Revision ID: t6u7v8w9x0y1
Revises: d98c001d9130
Create Date: 2026-06-07 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "t6u7v8w9x0y1"
down_revision: Union[str, Sequence[str], None] = "d98c001d9130"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("master_deliveries"):
        return

    has_file_path = _has_column(inspector, "master_deliveries", "file_path")
    has_delivery_kind = _has_column(inspector, "master_deliveries", "delivery_kind")
    has_delivery_message = _has_column(inspector, "master_deliveries", "delivery_message")

    with op.batch_alter_table("master_deliveries") as batch_op:
        if has_file_path:
            batch_op.alter_column(
                "file_path",
                existing_type=sa.String(length=500),
                nullable=True,
            )
        if not has_delivery_kind:
            batch_op.add_column(
                sa.Column(
                    "delivery_kind",
                    sa.String(length=20),
                    nullable=False,
                    server_default="file",
                )
            )
        if not has_delivery_message:
            batch_op.add_column(sa.Column("delivery_message", sa.Text(), nullable=True))

    bind.execute(sa.text("UPDATE master_deliveries SET delivery_kind = 'file' WHERE delivery_kind IS NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("master_deliveries"):
        return

    if _has_column(inspector, "master_deliveries", "file_path"):
        bind.execute(sa.text("UPDATE master_deliveries SET file_path = '' WHERE file_path IS NULL"))

    with op.batch_alter_table("master_deliveries") as batch_op:
        if _has_column(inspector, "master_deliveries", "delivery_message"):
            batch_op.drop_column("delivery_message")
        if _has_column(inspector, "master_deliveries", "delivery_kind"):
            batch_op.drop_column("delivery_kind")
        if _has_column(inspector, "master_deliveries", "file_path"):
            batch_op.alter_column(
                "file_path",
                existing_type=sa.String(length=500),
                nullable=False,
            )
