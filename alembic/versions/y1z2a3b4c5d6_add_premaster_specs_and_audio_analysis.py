"""Add pre-master specifications and audio analysis state.

Revision ID: y1z2a3b4c5d6
Revises: x0y1z2a3b4c5
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "y1z2a3b4c5d6"
down_revision: str | None = "x0y1z2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_analysis_columns(table_name: str) -> None:
    existing = _column_names(table_name)
    if not existing:
        return
    with op.batch_alter_table(table_name) as batch_op:
        if "audio_analysis_status" not in existing:
            batch_op.add_column(sa.Column("audio_analysis_status", sa.String(length=20), nullable=False, server_default="pending"))
        if "audio_analysis" not in existing:
            batch_op.add_column(sa.Column("audio_analysis", sa.Text(), nullable=True))
        if "audio_analysis_error" not in existing:
            batch_op.add_column(sa.Column("audio_analysis_error", sa.Text(), nullable=True))
        if "audio_analysis_attempts" not in existing:
            batch_op.add_column(sa.Column("audio_analysis_attempts", sa.Integer(), nullable=False, server_default="0"))
        if "audio_analysis_started_at" not in existing:
            batch_op.add_column(sa.Column("audio_analysis_started_at", sa.DateTime(), nullable=True))
        if "audio_analyzed_at" not in existing:
            batch_op.add_column(sa.Column("audio_analyzed_at", sa.DateTime(), nullable=True))


def _drop_analysis_columns(table_name: str) -> None:
    existing = _column_names(table_name)
    if not existing:
        return
    with op.batch_alter_table(table_name) as batch_op:
        for column_name in (
            "audio_analyzed_at",
            "audio_analysis_started_at",
            "audio_analysis_attempts",
            "audio_analysis_error",
            "audio_analysis",
            "audio_analysis_status",
        ):
            if column_name in existing:
                batch_op.drop_column(column_name)


def upgrade() -> None:
    if _column_names("albums") and "premaster_spec" not in _column_names("albums"):
        with op.batch_alter_table("albums") as batch_op:
            batch_op.add_column(sa.Column("premaster_spec", sa.Text(), nullable=True))
    if _column_names("tracks") and "premaster_spec_override" not in _column_names("tracks"):
        with op.batch_alter_table("tracks") as batch_op:
            batch_op.add_column(sa.Column("premaster_spec_override", sa.Text(), nullable=True))
    if _column_names("track_source_versions") and "purpose" not in _column_names("track_source_versions"):
        with op.batch_alter_table("track_source_versions") as batch_op:
            batch_op.add_column(sa.Column("purpose", sa.String(length=20), nullable=False, server_default="source"))
    _add_analysis_columns("track_source_versions")
    _add_analysis_columns("master_deliveries")
    source_columns = _column_names("track_source_versions")
    if {"audio_analysis_status", "file_path", "source_kind"}.issubset(source_columns):
        op.execute(
            "UPDATE track_source_versions SET audio_analysis_status = 'not_applicable' "
            "WHERE file_path IS NULL OR source_kind != 'file'"
        )
    delivery_columns = _column_names("master_deliveries")
    if {"audio_analysis_status", "file_path", "delivery_kind"}.issubset(delivery_columns):
        op.execute(
            "UPDATE master_deliveries SET audio_analysis_status = 'not_applicable' "
            "WHERE file_path IS NULL OR delivery_kind != 'file'"
        )
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("premaster_handoffs"):
        op.create_table(
            "premaster_handoffs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("track_id", sa.Integer(), nullable=False),
            sa.Column("workflow_cycle", sa.Integer(), nullable=False),
            sa.Column("source_version_id", sa.Integer(), nullable=True),
            sa.Column("requested_by_id", sa.Integer(), nullable=False),
            sa.Column("cancelled_by_id", sa.Integer(), nullable=True),
            sa.Column("mode", sa.String(length=24), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("request_note", sa.Text(), nullable=True),
            sa.Column("validation_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("cancelled_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["cancelled_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["requested_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["source_version_id"], ["track_source_versions.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["track_id"], ["tracks.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_premaster_handoffs_id", "premaster_handoffs", ["id"])
        op.create_index("ix_premaster_handoffs_track_id", "premaster_handoffs", ["track_id"])
        op.create_index("ix_premaster_handoffs_workflow_cycle", "premaster_handoffs", ["workflow_cycle"])
        op.create_index("ix_premaster_handoffs_source_version_id", "premaster_handoffs", ["source_version_id"])
        op.create_index("ix_premaster_handoffs_requested_by_id", "premaster_handoffs", ["requested_by_id"])
        op.create_index("ix_premaster_handoffs_cancelled_by_id", "premaster_handoffs", ["cancelled_by_id"])
        op.create_index("ix_premaster_handoffs_status", "premaster_handoffs", ["status"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("premaster_handoffs"):
        op.drop_table("premaster_handoffs")
    _drop_analysis_columns("master_deliveries")
    _drop_analysis_columns("track_source_versions")
    if "purpose" in _column_names("track_source_versions"):
        with op.batch_alter_table("track_source_versions") as batch_op:
            batch_op.drop_column("purpose")
    if "premaster_spec_override" in _column_names("tracks"):
        with op.batch_alter_table("tracks") as batch_op:
            batch_op.drop_column("premaster_spec_override")
    if "premaster_spec" in _column_names("albums"):
        with op.batch_alter_table("albums") as batch_op:
            batch_op.drop_column("premaster_spec")
