"""remove mastering_engineer global role

Revision ID: a1b2c3d4e5f6
Revises: 10fe2b812550
Create Date: 2026-04-06 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '10fe2b812550'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Convert all users with role='mastering_engineer' to role='member'.
    # The mastering engineer identity is now determined solely at the
    # circle/album level (CircleMember.role and Album.mastering_engineer_id).
    op.execute(
        sa.text("UPDATE users SET role = 'member' WHERE role = 'mastering_engineer'")
    )


def downgrade() -> None:
    # Cannot reliably restore which users were mastering_engineer, so no-op.
    pass
