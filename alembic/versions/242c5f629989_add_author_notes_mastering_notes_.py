"""add author_notes mastering_notes revision_notes

Revision ID: 242c5f629989
Revises: 67295d620f82
Create Date: 2026-04-12 20:51:47.184130

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '242c5f629989'
down_revision: Union[str, Sequence[str], None] = '67295d620f82'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('track_source_versions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('revision_notes', sa.Text(), nullable=True))

    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('author_notes', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('mastering_notes', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.drop_column('mastering_notes')
        batch_op.drop_column('author_notes')

    with op.batch_alter_table('track_source_versions', schema=None) as batch_op:
        batch_op.drop_column('revision_notes')
