"""add track composers

Revision ID: u7v8w9x0y1z2
Revises: t6u7v8w9x0y1
Create Date: 2026-06-07 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "u7v8w9x0y1z2"
down_revision: Union[str, Sequence[str], None] = "t6u7v8w9x0y1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("track_composers"):
        op.create_table(
            "track_composers",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("track_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["track_id"], ["tracks.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("track_id", "user_id", name="uq_track_composer"),
        )

    inspector = sa.inspect(bind)
    if not _has_index(inspector, "track_composers", "ix_track_composers_track_id"):
        op.create_index("ix_track_composers_track_id", "track_composers", ["track_id"])
    if not _has_index(inspector, "track_composers", "ix_track_composers_user_id"):
        op.create_index("ix_track_composers_user_id", "track_composers", ["user_id"])

    if inspector.has_table("tracks") and _has_column(inspector, "tracks", "submitter_id"):
        bind.execute(
            sa.text(
                """
                INSERT INTO track_composers (track_id, user_id, created_at)
                SELECT tracks.id, tracks.submitter_id, CURRENT_TIMESTAMP
                FROM tracks
                WHERE tracks.submitter_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM track_composers
                    WHERE track_composers.track_id = tracks.id
                      AND track_composers.user_id = tracks.submitter_id
                  )
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("track_composers"):
        op.drop_table("track_composers")
