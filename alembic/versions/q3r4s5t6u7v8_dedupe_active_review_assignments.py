"""dedupe active review assignments

Revision ID: q3r4s5t6u7v8
Revises: p2q3r4s5t6u7
Create Date: 2026-05-11 18:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "q3r4s5t6u7v8"
down_revision: Union[str, Sequence[str], None] = "p2q3r4s5t6u7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACTIVE_ASSIGNMENT_STATUSES = ("pending", "completed")
SUPERSEDED_REASON = "superseded"
ACTIVE_ASSIGNMENT_UNIQUE_INDEX = "uq_stage_assignments_active_track_stage_user"


def _has_columns(inspector: sa.Inspector, table_name: str, column_names: set[str]) -> bool:
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    return column_names.issubset(columns)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("stage_assignments"):
        return
    if not _has_columns(
        inspector,
        "stage_assignments",
        {"id", "track_id", "stage_id", "user_id", "status", "assigned_at", "cancellation_reason"},
    ):
        return

    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY track_id, stage_id, user_id
                        ORDER BY
                            CASE status
                                WHEN 'pending' THEN 0
                                ELSE 1
                            END,
                            assigned_at DESC,
                            id DESC
                    ) AS rn
                FROM stage_assignments
                WHERE status IN ('pending', 'completed')
            )
            UPDATE stage_assignments
            SET status = 'cancelled',
                cancellation_reason = 'superseded'
            WHERE id IN (
                SELECT id
                FROM ranked
                WHERE rn > 1
            )
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {ACTIVE_ASSIGNMENT_UNIQUE_INDEX}
            ON stage_assignments (track_id, stage_id, user_id)
            WHERE status IN ('pending', 'completed')
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {ACTIVE_ASSIGNMENT_UNIQUE_INDEX}"))
