"""convert track status and issue phase from enum to string

Revision ID: g2h3i4j5k6l7
Revises: f7a8b9c0d1e2
Create Date: 2026-04-07 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'g2h3i4j5k6l7'
down_revision: Union[str, None] = '84904cfaf7c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite stores enum values as plain strings already, so the data
    # is compatible.  We just need to change the column type metadata
    # from Enum to String so SQLAlchemy doesn't validate against the
    # fixed enum set.
    with op.batch_alter_table("tracks") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum(
                "submitted", "peer_review", "peer_revision",
                "producer_mastering_gate", "mastering",
                "mastering_revision", "final_review",
                "completed", "rejected",
            ),
            type_=sa.String(50),
            existing_nullable=False,
        )

    with op.batch_alter_table("issues") as batch_op:
        batch_op.alter_column(
            "phase",
            existing_type=sa.Enum("peer", "producer", "mastering", "final_review"),
            type_=sa.String(50),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("tracks") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(50),
            type_=sa.Enum(
                "submitted", "peer_review", "peer_revision",
                "producer_mastering_gate", "mastering",
                "mastering_revision", "final_review",
                "completed", "rejected",
            ),
            existing_nullable=False,
        )

    with op.batch_alter_table("issues") as batch_op:
        batch_op.alter_column(
            "phase",
            existing_type=sa.String(50),
            type_=sa.Enum("peer", "producer", "mastering", "final_review"),
            existing_nullable=False,
        )
