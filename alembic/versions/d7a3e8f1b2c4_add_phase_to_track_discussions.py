"""add phase to track_discussions

Revision ID: d7a3e8f1b2c4
Revises: c68eb640ff61
Create Date: 2026-04-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7a3e8f1b2c4'
down_revision: Union[str, Sequence[str], None] = 'c68eb640ff61'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('track_discussions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('phase', sa.String(20), nullable=False, server_default='general'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('track_discussions', schema=None) as batch_op:
        batch_op.drop_column('phase')
