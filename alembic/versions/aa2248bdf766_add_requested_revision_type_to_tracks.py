"""add requested_revision_type to tracks

Revision ID: aa2248bdf766
Revises: s5t6u7v8w9x0
Create Date: 2026-06-08 22:20:59.660146

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'aa2248bdf766'
down_revision: Union[str, Sequence[str], None] = 's5t6u7v8w9x0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
