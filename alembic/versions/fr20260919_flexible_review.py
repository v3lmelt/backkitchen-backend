"""Persist per-track flexible review modes and optimistic review revisions."""
from alembic import op
import sqlalchemy as sa

revision = "fr20260919"
down_revision = "x0y1z2a3b4c5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tracks", sa.Column("flexible_review_stages", sa.Text(), nullable=True))
    op.add_column("tracks", sa.Column("review_revision", sa.Integer(), server_default="0", nullable=False))


def downgrade():
    with op.batch_alter_table("tracks") as batch:
        batch.drop_column("review_revision")
        batch.drop_column("flexible_review_stages")
