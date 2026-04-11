"""add issue_audios table

Revision ID: d1e2f3a4b5c6
Revises: c8d9e0f1a2b3
Create Date: 2026-04-11 16:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'issue_audios',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('issue_id', sa.Integer(), nullable=False),
        sa.Column('file_path', sa.String(length=500), nullable=False),
        sa.Column('storage_backend', sa.String(length=10), nullable=False, server_default='local'),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('duration', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['issue_id'], ['issues.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('issue_audios', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_issue_audios_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_issue_audios_issue_id'), ['issue_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('issue_audios', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_issue_audios_issue_id'))
        batch_op.drop_index(batch_op.f('ix_issue_audios_id'))

    op.drop_table('issue_audios')
