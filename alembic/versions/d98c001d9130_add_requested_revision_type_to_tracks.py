"""add requested_revision_type to tracks

Revision ID: d98c001d9130
Revises: aa2248bdf766
Create Date: 2026-06-08 22:21:07.473513

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd98c001d9130'
down_revision: Union[str, Sequence[str], None] = 'aa2248bdf766'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add requested_revision_type column to tracks table
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('requested_revision_type', sa.String(length=20), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Remove requested_revision_type column from tracks table
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.drop_column('requested_revision_type')
