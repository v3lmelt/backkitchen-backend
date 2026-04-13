"""add track_discussion_audios table

Revision ID: l8m9n0o1p2q3
Revises: k7l8m9n0o1p2
Create Date: 2026-04-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'l8m9n0o1p2q3'
down_revision: Union[str, None] = 'k7l8m9n0o1p2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "track_discussion_audios",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discussion_id", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["discussion_id"], ["track_discussions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_track_discussion_audios_id"), "track_discussion_audios", ["id"], unique=False)
    op.create_index(op.f("ix_track_discussion_audios_discussion_id"), "track_discussion_audios", ["discussion_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_track_discussion_audios_discussion_id"), table_name="track_discussion_audios")
    op.drop_index(op.f("ix_track_discussion_audios_id"), table_name="track_discussion_audios")
    op.drop_table("track_discussion_audios")
