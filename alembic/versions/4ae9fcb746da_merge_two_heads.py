"""merge_two_heads

Revision ID: 4ae9fcb746da
Revises: 31dddf7c1876, h3i4j5k6l7m8
Create Date: 2026-04-09 10:19:50.224574

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4ae9fcb746da'
down_revision: Union[str, Sequence[str], None] = ('31dddf7c1876', 'h3i4j5k6l7m8')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
