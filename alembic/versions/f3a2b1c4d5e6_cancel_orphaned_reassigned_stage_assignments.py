"""cancel orphaned reassigned stage assignments

Revision ID: f3a2b1c4d5e6
Revises: 621b76f1a4ba
Create Date: 2026-04-16 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a2b1c4d5e6'
down_revision: Union[str, Sequence[str], None] = '621b76f1a4ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Older versions of the admin reassign endpoint incorrectly marked
    # superseded pending stage assignments as status='completed' instead of
    # 'cancelled'. Those records still had cancellation_reason='reassigned'
    # and no decision / completed_at set, so they leaked into the UI as
    # duplicate "已完成" reviewer rows. Normalize them to 'cancelled' so the
    # frontend filter (which hides status == 'cancelled') excludes them.
    op.execute(
        sa.text(
            "UPDATE stage_assignments "
            "SET status = 'cancelled' "
            "WHERE status = 'completed' "
            "AND cancellation_reason = 'reassigned' "
            "AND decision IS NULL "
            "AND completed_at IS NULL"
        )
    )


def downgrade() -> None:
    # Data cleanup migration; original rows cannot be identified.
    pass
