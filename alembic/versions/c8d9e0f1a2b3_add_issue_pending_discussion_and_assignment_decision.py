"""add issue pending_discussion and assignment decision

Revision ID: c8d9e0f1a2b3
Revises: 9c4b9d6a7e21
Create Date: 2026-04-11 15:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, Sequence[str], None] = '9c4b9d6a7e21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('stage_assignments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('decision', sa.String(length=50), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('stage_assignments', schema=None) as batch_op:
        batch_op.drop_column('decision')
