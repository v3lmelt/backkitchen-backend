"""add_track_playback_preferences

Revision ID: 9c4b9d6a7e21
Revises: 5a044024dc37
Create Date: 2026-04-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9c4b9d6a7e21'
down_revision: Union[str, Sequence[str], None] = '5a044024dc37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'track_playback_preferences',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('track_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('scope', sa.String(length=32), nullable=False),
        sa.Column('gain_db', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['track_id'], ['tracks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('track_id', 'user_id', 'scope', name='uq_track_playback_preferences_track_user_scope'),
    )
    with op.batch_alter_table('track_playback_preferences', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_track_playback_preferences_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_track_playback_preferences_track_id'), ['track_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_track_playback_preferences_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('track_playback_preferences', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_track_playback_preferences_user_id'))
        batch_op.drop_index(batch_op.f('ix_track_playback_preferences_track_id'))
        batch_op.drop_index(batch_op.f('ix_track_playback_preferences_id'))

    op.drop_table('track_playback_preferences')
