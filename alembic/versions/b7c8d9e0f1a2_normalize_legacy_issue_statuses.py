"""normalize legacy issue statuses

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-04-06 23:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Older databases may still contain the removed will_fix status.
    # Normalize it so ORM enum loading and API serialization do not fail.
    op.execute(
        sa.text("UPDATE issues SET status = 'open' WHERE lower(status) = 'will_fix'")
    )


def downgrade() -> None:
    # This is a data cleanup migration; the original rows cannot be identified.
    pass
