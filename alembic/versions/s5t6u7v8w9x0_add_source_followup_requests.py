"""add source follow-up requests

Revision ID: s5t6u7v8w9x0
Revises: r4s5t6u7v8w9
Create Date: 2026-05-19 20:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "s5t6u7v8w9x0"
down_revision: Union[str, Sequence[str], None] = "r4s5t6u7v8w9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("albums") and not _has_column(inspector, "albums", "quick_followup_enabled"):
        with op.batch_alter_table("albums") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "quick_followup_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default="0",
                )
            )

    required_tables = {"tracks", "users", "track_source_versions"}
    if not required_tables.issubset(set(inspector.get_table_names())):
        return

    if not inspector.has_table("source_followup_requests"):
        op.create_table(
            "source_followup_requests",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("track_id", sa.Integer(), nullable=False),
            sa.Column("requested_by_id", sa.Integer(), nullable=False),
            sa.Column("decided_by_id", sa.Integer(), nullable=True),
            sa.Column("applied_source_version_id", sa.Integer(), nullable=True),
            sa.Column("previous_status", sa.String(length=50), nullable=False),
            sa.Column("target_stage_id", sa.String(length=50), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("staged_file_path", sa.String(length=500), nullable=False),
            sa.Column("staged_storage_backend", sa.String(length=10), nullable=False),
            sa.Column("staged_duration", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("decided_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["applied_source_version_id"], ["track_source_versions.id"]),
            sa.ForeignKeyConstraint(["decided_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["requested_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["track_id"], ["tracks.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(op.get_bind())
    if not _has_index(inspector, "source_followup_requests", "ix_source_followup_requests_id"):
        op.create_index(op.f("ix_source_followup_requests_id"), "source_followup_requests", ["id"], unique=False)
    if not _has_index(inspector, "source_followup_requests", "ix_source_followup_requests_requested_by_id"):
        op.create_index(op.f("ix_source_followup_requests_requested_by_id"), "source_followup_requests", ["requested_by_id"], unique=False)
    if not _has_index(inspector, "source_followup_requests", "ix_source_followup_requests_decided_by_id"):
        op.create_index(op.f("ix_source_followup_requests_decided_by_id"), "source_followup_requests", ["decided_by_id"], unique=False)
    if not _has_index(inspector, "source_followup_requests", "ix_source_followup_requests_status"):
        op.create_index(op.f("ix_source_followup_requests_status"), "source_followup_requests", ["status"], unique=False)
    if not _has_index(inspector, "source_followup_requests", "ix_source_followup_requests_track_id"):
        op.create_index(op.f("ix_source_followup_requests_track_id"), "source_followup_requests", ["track_id"], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("source_followup_requests"):
        if _has_index(inspector, "source_followup_requests", "ix_source_followup_requests_track_id"):
            op.drop_index(op.f("ix_source_followup_requests_track_id"), table_name="source_followup_requests")
        if _has_index(inspector, "source_followup_requests", "ix_source_followup_requests_status"):
            op.drop_index(op.f("ix_source_followup_requests_status"), table_name="source_followup_requests")
        if _has_index(inspector, "source_followup_requests", "ix_source_followup_requests_decided_by_id"):
            op.drop_index(op.f("ix_source_followup_requests_decided_by_id"), table_name="source_followup_requests")
        if _has_index(inspector, "source_followup_requests", "ix_source_followup_requests_requested_by_id"):
            op.drop_index(op.f("ix_source_followup_requests_requested_by_id"), table_name="source_followup_requests")
        if _has_index(inspector, "source_followup_requests", "ix_source_followup_requests_id"):
            op.drop_index(op.f("ix_source_followup_requests_id"), table_name="source_followup_requests")
        op.drop_table("source_followup_requests")

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("albums") and _has_column(inspector, "albums", "quick_followup_enabled"):
        with op.batch_alter_table("albums") as batch_op:
            batch_op.drop_column("quick_followup_enabled")
