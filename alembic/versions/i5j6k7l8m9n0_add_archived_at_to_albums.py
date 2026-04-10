"""add archived_at to albums

Revision ID: i5j6k7l8m9n0
Revises: 814cf67c0278
Create Date: 2026-04-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'i5j6k7l8m9n0'
down_revision: Union[str, Sequence[str], None] = '814cf67c0278'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('albums', schema=None) as batch_op:
        batch_op.add_column(sa.Column('archived_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_albums_archived_at'), ['archived_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('albums', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_albums_archived_at'))
        batch_op.drop_column('archived_at')
