"""add workflow_variant to tracks

Revision ID: afc2010d83d9
Revises: d7fbf1963f92
Create Date: 2026-04-09 16:56:13.937000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'afc2010d83d9'
down_revision: Union[str, Sequence[str], None] = 'd7fbf1963f92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'workflow_variant',
                sa.String(length=32),
                nullable=False,
                server_default='standard',
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.drop_column('workflow_variant')
