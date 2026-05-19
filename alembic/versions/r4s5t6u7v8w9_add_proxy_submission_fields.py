"""add proxy submission fields

Revision ID: r4s5t6u7v8w9
Revises: q3r4s5t6u7v8
Create Date: 2026-05-19 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "r4s5t6u7v8w9"
down_revision: Union[str, Sequence[str], None] = "q3r4s5t6u7v8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("tracks"):
        return

    with op.batch_alter_table("tracks") as batch_op:
        if not _has_column(inspector, "tracks", "proxy_uploader_id"):
            batch_op.add_column(sa.Column("proxy_uploader_id", sa.Integer(), nullable=True))
        if not _has_column(inspector, "tracks", "external_submitter_name"):
            batch_op.add_column(sa.Column("external_submitter_name", sa.String(length=100), nullable=True))

    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "tracks", "proxy_uploader_id"):
        with op.batch_alter_table("tracks") as batch_op:
            if not _has_index(inspector, "tracks", "ix_tracks_proxy_uploader_id"):
                batch_op.create_index("ix_tracks_proxy_uploader_id", ["proxy_uploader_id"])
            foreign_keys = {
                tuple(fk["constrained_columns"]): fk
                for fk in inspector.get_foreign_keys("tracks")
            }
            if ("proxy_uploader_id",) not in foreign_keys:
                batch_op.create_foreign_key(
                    "fk_tracks_proxy_uploader_id_users",
                    "users",
                    ["proxy_uploader_id"],
                    ["id"],
                )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("tracks"):
        return

    with op.batch_alter_table("tracks") as batch_op:
        if _has_index(inspector, "tracks", "ix_tracks_proxy_uploader_id"):
            batch_op.drop_index("ix_tracks_proxy_uploader_id")
        foreign_keys = {
            fk["name"]: fk
            for fk in inspector.get_foreign_keys("tracks")
        }
        if "fk_tracks_proxy_uploader_id_users" in foreign_keys:
            batch_op.drop_constraint("fk_tracks_proxy_uploader_id_users", type_="foreignkey")
        if _has_column(inspector, "tracks", "external_submitter_name"):
            batch_op.drop_column("external_submitter_name")
        if _has_column(inspector, "tracks", "proxy_uploader_id"):
            batch_op.drop_column("proxy_uploader_id")
