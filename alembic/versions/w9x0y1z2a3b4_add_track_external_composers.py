"""add track external composers

Revision ID: w9x0y1z2a3b4
Revises: v8w9x0y1z2a3
Create Date: 2026-06-08 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "w9x0y1z2a3b4"
down_revision: Union[str, Sequence[str], None] = "v8w9x0y1z2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("track_external_composers"):
        op.create_table(
            "track_external_composers",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("track_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["track_id"], ["tracks.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("track_id", "name", name="uq_track_external_composer_name"),
        )

    inspector = sa.inspect(bind)
    if not _has_index(inspector, "track_external_composers", "ix_track_external_composers_track_id"):
        op.create_index(
            "ix_track_external_composers_track_id",
            "track_external_composers",
            ["track_id"],
        )

    if (
        inspector.has_table("tracks")
        and _has_column(inspector, "tracks", "external_submitter_name")
    ):
        bind.execute(
            sa.text(
                """
                INSERT INTO track_external_composers (track_id, name, sort_order, created_at)
                SELECT tracks.id, TRIM(tracks.external_submitter_name), 0, CURRENT_TIMESTAMP
                FROM tracks
                WHERE tracks.external_submitter_name IS NOT NULL
                  AND TRIM(tracks.external_submitter_name) != ''
                  AND NOT EXISTS (
                    SELECT 1
                    FROM track_external_composers
                    WHERE track_external_composers.track_id = tracks.id
                      AND track_external_composers.name = TRIM(tracks.external_submitter_name)
                  )
                """
            )
        )

        if (
            inspector.has_table("track_composers")
            and _has_column(inspector, "tracks", "proxy_uploader_id")
            and _has_column(inspector, "tracks", "submitter_id")
        ):
            bind.execute(
                sa.text(
                    """
                    DELETE FROM track_composers
                    WHERE EXISTS (
                        SELECT 1
                        FROM tracks
                        WHERE tracks.id = track_composers.track_id
                          AND tracks.external_submitter_name IS NOT NULL
                          AND tracks.proxy_uploader_id IS NOT NULL
                          AND tracks.submitter_id = track_composers.user_id
                          AND tracks.proxy_uploader_id = track_composers.user_id
                    )
                    """
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("track_external_composers"):
        op.drop_table("track_external_composers")
