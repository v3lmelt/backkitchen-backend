"""merge diverged heads

Revision ID: 621b76f1a4ba
Revises: d7a3e8f1b2c4, l8m9n0o1p2q3
Create Date: 2026-04-13 20:14:52.794014

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '621b76f1a4ba'
down_revision: Union[str, Sequence[str], None] = ('d7a3e8f1b2c4', 'l8m9n0o1p2q3')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
