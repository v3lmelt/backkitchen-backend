"""add workflow_config to albums

Revision ID: f7a8b9c0d1e2
Revises: 164c3d17c3e1
Create Date: 2026-04-07 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, None] = '164c3d17c3e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("albums") as batch_op:
        batch_op.add_column(sa.Column("workflow_config", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("albums") as batch_op:
        batch_op.drop_column("workflow_config")
