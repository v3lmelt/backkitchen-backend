"""add mastering_notes to reopen_requests

Revision ID: c68eb640ff61
Revises: 242c5f629989
Create Date: 2026-04-12 21:43:00.610014

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c68eb640ff61'
down_revision: Union[str, Sequence[str], None] = '242c5f629989'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('reopen_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mastering_notes', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('reopen_requests', schema=None) as batch_op:
        batch_op.drop_column('mastering_notes')
