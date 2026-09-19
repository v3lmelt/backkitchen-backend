"""Join audio specification and flexible review migration branches.

Both branches may already have been applied in local worktrees. Preserve their
revision histories and converge without modifying existing workflow records.
"""

revision = "merge20260919"
down_revision = ("z2a3b4c5d6e7", "fr20260919")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
