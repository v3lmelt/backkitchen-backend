"""harden track delete integrity

Revision ID: p2q3r4s5t6u7
Revises: n1o2p3q4r5s6
Create Date: 2026-05-11 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "p2q3r4s5t6u7"
down_revision: Union[str, Sequence[str], None] = "n1o2p3q4r5s6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _track_link_condition(
    inspector: sa.Inspector,
    table_name: str,
    track_id_column: str,
    timestamp_column: str,
) -> str:
    condition = (
        f"NOT EXISTS (SELECT 1 FROM tracks WHERE tracks.id = {table_name}.{track_id_column})"
    )
    if _has_column(inspector, "tracks", "created_at") and _has_column(
        inspector, table_name, timestamp_column
    ):
        condition += f"""
                OR EXISTS (
                    SELECT 1
                    FROM tracks
                    WHERE tracks.id = {table_name}.{track_id_column}
                      AND {table_name}.{timestamp_column} < tracks.created_at
                )
        """
    return condition


def _nullable_parent_link_condition(
    inspector: sa.Inspector,
    child_table: str,
    child_fk_column: str,
    parent_table: str,
    parent_timestamp_column: str,
) -> str:
    condition = (
        f"NOT EXISTS (SELECT 1 FROM {parent_table} "
        f"WHERE {parent_table}.id = {child_table}.{child_fk_column})"
    )
    if _has_column(inspector, child_table, "created_at") and _has_column(
        inspector, parent_table, parent_timestamp_column
    ):
        condition += f"""
                    OR EXISTS (
                        SELECT 1
                        FROM {parent_table}
                        WHERE {parent_table}.id = {child_table}.{child_fk_column}
                          AND {child_table}.created_at < {parent_table}.{parent_timestamp_column}
                    )
        """
    return condition


def _cleanup_stale_track_links(inspector: sa.Inspector) -> None:
    """Remove impossible child rows left by old SQLite FK behavior."""

    if inspector.has_table("stage_assignments"):
        condition = _track_link_condition(
            inspector, "stage_assignments", "track_id", "assigned_at"
        )
        op.execute(
            sa.text(
                f"""
                DELETE FROM stage_assignments
                WHERE {condition}
                """
            )
        )
    if inspector.has_table("reopen_requests"):
        condition = _track_link_condition(
            inspector, "reopen_requests", "track_id", "created_at"
        )
        op.execute(
            sa.text(
                f"""
                DELETE FROM reopen_requests
                WHERE {condition}
                """
            )
        )
    if inspector.has_table("track_playback_preferences"):
        condition = _track_link_condition(
            inspector, "track_playback_preferences", "track_id", "created_at"
        )
        op.execute(
            sa.text(
                f"""
                DELETE FROM track_playback_preferences
                WHERE {condition}
                """
            )
        )
    if inspector.has_table("notifications"):
        condition = _nullable_parent_link_condition(
            inspector, "notifications", "related_track_id", "tracks", "created_at"
        )
        op.execute(
            sa.text(
                f"""
                UPDATE notifications
                SET related_track_id = NULL
                WHERE related_track_id IS NOT NULL
                  AND ({condition})
                """
            )
        )
        if inspector.has_table("issues"):
            condition = _nullable_parent_link_condition(
                inspector, "notifications", "related_issue_id", "issues", "created_at"
            )
            op.execute(
                sa.text(
                    f"""
                    UPDATE notifications
                    SET related_issue_id = NULL
                    WHERE related_issue_id IS NOT NULL
                      AND ({condition})
                    """
                )
            )
        if inspector.has_table("albums"):
            condition = _nullable_parent_link_condition(
                inspector, "notifications", "related_album_id", "albums", "created_at"
            )
            op.execute(
                sa.text(
                    f"""
                    UPDATE notifications
                    SET related_album_id = NULL
                    WHERE related_album_id IS NOT NULL
                      AND ({condition})
                    """
                )
            )
    if inspector.has_table("admin_audit_logs"):
        condition = _track_link_condition(
            inspector, "admin_audit_logs", "track_id", "created_at"
        )
        op.execute(
            sa.text(
                f"""
                UPDATE admin_audit_logs
                SET track_id = NULL
                WHERE action = 'track_deleted'
                   OR (
                    track_id IS NOT NULL
                    AND ({condition})
                  )
                """
            )
        )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("tracks"):
        _cleanup_stale_track_links(inspector)


def downgrade() -> None:
    pass
