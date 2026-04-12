"""add_cancellation_reason_to_stage_assignments

Revision ID: f3b1c2d4e5f6
Revises: 968731a990b3
Create Date: 2026-04-12 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3b1c2d4e5f6'
down_revision: Union[str, Sequence[str], None] = '968731a990b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('stage_assignments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cancellation_reason', sa.String(length=30), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('stage_assignments', schema=None) as batch_op:
        batch_op.drop_column('cancellation_reason')
