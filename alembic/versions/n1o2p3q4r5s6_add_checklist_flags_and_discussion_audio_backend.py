"""add checklist flags and discussion audio backend

Revision ID: n1o2p3q4r5s6
Revises: m9n0o1p2q3r4
Create Date: 2026-04-18 12:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "n1o2p3q4r5s6"
down_revision: Union[str, Sequence[str], None] = "m9n0o1p2q3r4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    with op.batch_alter_table("circles") as batch_op:
        batch_op.add_column(
            sa.Column("default_checklist_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )

    with op.batch_alter_table("albums") as batch_op:
        batch_op.add_column(
            sa.Column("checklist_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )

    if inspector.has_table("track_discussion_audios"):
        with op.batch_alter_table("track_discussion_audios") as batch_op:
            batch_op.add_column(
                sa.Column("storage_backend", sa.String(length=10), nullable=False, server_default="local")
            )

    op.execute(sa.text("UPDATE circles SET default_checklist_enabled = 1 WHERE default_checklist_enabled IS NULL"))
    op.execute(sa.text("UPDATE albums SET checklist_enabled = 1 WHERE checklist_enabled IS NULL"))
    if inspector.has_table("track_discussion_audios"):
        op.execute(sa.text("UPDATE track_discussion_audios SET storage_backend = 'local' WHERE storage_backend IS NULL"))

    with op.batch_alter_table("circles") as batch_op:
        batch_op.alter_column("default_checklist_enabled", server_default=sa.false())

    with op.batch_alter_table("albums") as batch_op:
        batch_op.alter_column("checklist_enabled", server_default=sa.false())


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("track_discussion_audios"):
        with op.batch_alter_table("track_discussion_audios") as batch_op:
            batch_op.drop_column("storage_backend")

    with op.batch_alter_table("albums") as batch_op:
        batch_op.drop_column("checklist_enabled")

    with op.batch_alter_table("circles") as batch_op:
        batch_op.drop_column("default_checklist_enabled")
