"""add internal visibility to discussions and comments

Revision ID: a9b8c7d6e5f4
Revises: f3b1c2d4e5f6
Create Date: 2026-04-12 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, Sequence[str], None] = "f3b1c2d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("comments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("visibility", sa.String(length=20), nullable=False, server_default="public"))

    with op.batch_alter_table("track_discussions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("visibility", sa.String(length=20), nullable=False, server_default="public"))


def downgrade() -> None:
    with op.batch_alter_table("track_discussions", schema=None) as batch_op:
        batch_op.drop_column("visibility")

    with op.batch_alter_table("comments", schema=None) as batch_op:
        batch_op.drop_column("visibility")
