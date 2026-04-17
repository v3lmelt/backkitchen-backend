"""add local_number to issues

Revision ID: m9n0o1p2q3r4
Revises: 18c2d4e6f8a0
Create Date: 2026-04-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'm9n0o1p2q3r4'
down_revision: Union[str, None] = '18c2d4e6f8a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add as nullable first so existing rows don't violate NOT NULL during backfill.
    with op.batch_alter_table("issues") as batch_op:
        batch_op.add_column(sa.Column("local_number", sa.Integer(), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, track_id FROM issues "
            "ORDER BY track_id ASC, created_at ASC, id ASC"
        )
    ).fetchall()

    counters: dict[int, int] = {}
    for row in rows:
        issue_id = row[0]
        track_id = row[1]
        counters[track_id] = counters.get(track_id, 0) + 1
        bind.execute(
            sa.text("UPDATE issues SET local_number = :n WHERE id = :id"),
            {"n": counters[track_id], "id": issue_id},
        )

    with op.batch_alter_table("issues") as batch_op:
        batch_op.alter_column("local_number", existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint(
            "uq_issues_track_local_number", ["track_id", "local_number"]
        )


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_constraint("uq_issues_track_local_number", type_="unique")
        batch_op.drop_column("local_number")
