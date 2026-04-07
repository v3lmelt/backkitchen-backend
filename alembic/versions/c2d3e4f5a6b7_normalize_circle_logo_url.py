"""normalize circle logo_url to relative path

Revision ID: c2d3e4f5a6b7
Revises: b7c8d9e0f1a2
Create Date: 2026-04-06 23:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, Sequence[str], None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Legacy logo_url values were stored as "/uploads/<filename>".
    # Strip the prefix so it becomes just "<filename>" — a relative path under
    # the uploads directory, consistent with how cover_image is stored.
    # New uploads will go to "logos/<filename>".
    op.execute(
        sa.text(
            "UPDATE circles SET logo_url = substr(logo_url, 10) "
            "WHERE logo_url LIKE '/uploads/%'"
        )
    )


def downgrade() -> None:
    # Only restore the prefix for entries that look like bare filenames (no slash).
    op.execute(
        sa.text(
            "UPDATE circles SET logo_url = '/uploads/' || logo_url "
            "WHERE logo_url IS NOT NULL AND logo_url NOT LIKE '/%' AND logo_url NOT LIKE '%/%'"
        )
    )
