"""add issue_markers table, migrate data from issues

Revision ID: h3i4j5k6l7m8
Revises: g2h3i4j5k6l7
Create Date: 2026-04-09 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'h3i4j5k6l7m8'
down_revision: str = 'g2h3i4j5k6l7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create issue_markers table
    op.create_table(
        'issue_markers',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('issue_id', sa.Integer(), sa.ForeignKey('issues.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('marker_type', sa.String(10), nullable=False, server_default='point'),
        sa.Column('time_start', sa.Float(), nullable=False),
        sa.Column('time_end', sa.Float(), nullable=True),
    )

    # Migrate existing data: each issue with time_start → one marker row
    conn = op.get_bind()

    # Check if issues table has the old columns (they may already be gone)
    inspector = sa.inspect(conn)
    columns = {col['name'] for col in inspector.get_columns('issues')}

    if 'time_start' in columns:
        conn.execute(sa.text("""
            INSERT INTO issue_markers (issue_id, marker_type, time_start, time_end)
            SELECT id,
                   COALESCE(issue_type, 'point'),
                   time_start,
                   time_end
            FROM issues
            WHERE time_start IS NOT NULL
        """))

        # Remove old columns from issues (batch mode for SQLite)
        with op.batch_alter_table('issues') as batch_op:
            batch_op.drop_column('issue_type')
            batch_op.drop_column('time_start')
            batch_op.drop_column('time_end')


def downgrade() -> None:
    # Re-add old columns
    with op.batch_alter_table('issues') as batch_op:
        batch_op.add_column(sa.Column('issue_type', sa.String(10), server_default='point'))
        batch_op.add_column(sa.Column('time_start', sa.Float(), server_default='0'))
        batch_op.add_column(sa.Column('time_end', sa.Float(), nullable=True))

    # Migrate data back: take the first marker per issue
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE issues SET
            issue_type = (SELECT marker_type FROM issue_markers WHERE issue_markers.issue_id = issues.id LIMIT 1),
            time_start = (SELECT time_start FROM issue_markers WHERE issue_markers.issue_id = issues.id LIMIT 1),
            time_end = (SELECT time_end FROM issue_markers WHERE issue_markers.issue_id = issues.id LIMIT 1)
    """))

    op.drop_table('issue_markers')
