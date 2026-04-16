"""add admin governance fields and audit logs

Revision ID: 18c2d4e6f8a0
Revises: f4b5c6d7e8f9
Create Date: 2026-04-16 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "18c2d4e6f8a0"
down_revision: str | None = "f4b5c6d7e8f9"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("admin_role", sa.String(length=20), nullable=False, server_default="none"))
    op.add_column("users", sa.Column("suspended_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("suspension_reason", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"))

    op.execute(
        "UPDATE users SET admin_role = CASE WHEN is_admin = 1 THEN 'superadmin' ELSE 'none' END"
    )
    op.execute("UPDATE users SET session_version = 1 WHERE session_version IS NULL OR session_version < 1")

    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("summary", sa.String(length=500), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("before_state", sa.Text(), nullable=True),
        sa.Column("after_state", sa.Text(), nullable=True),
        sa.Column("target_user_id", sa.Integer(), nullable=True),
        sa.Column("album_id", sa.Integer(), nullable=True),
        sa.Column("track_id", sa.Integer(), nullable=True),
        sa.Column("circle_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["album_id"], ["albums.id"]),
        sa.ForeignKeyConstraint(["circle_id"], ["circles.id"]),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["track_id"], ["tracks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_admin_audit_logs_id"), "admin_audit_logs", ["id"], unique=False)
    op.create_index(op.f("ix_admin_audit_logs_actor_user_id"), "admin_audit_logs", ["actor_user_id"], unique=False)
    op.create_index(op.f("ix_admin_audit_logs_action"), "admin_audit_logs", ["action"], unique=False)
    op.create_index(op.f("ix_admin_audit_logs_album_id"), "admin_audit_logs", ["album_id"], unique=False)
    op.create_index(op.f("ix_admin_audit_logs_circle_id"), "admin_audit_logs", ["circle_id"], unique=False)
    op.create_index(op.f("ix_admin_audit_logs_created_at"), "admin_audit_logs", ["created_at"], unique=False)
    op.create_index(op.f("ix_admin_audit_logs_entity_id"), "admin_audit_logs", ["entity_id"], unique=False)
    op.create_index(op.f("ix_admin_audit_logs_entity_type"), "admin_audit_logs", ["entity_type"], unique=False)
    op.create_index(op.f("ix_admin_audit_logs_target_user_id"), "admin_audit_logs", ["target_user_id"], unique=False)
    op.create_index(op.f("ix_admin_audit_logs_track_id"), "admin_audit_logs", ["track_id"], unique=False)

    op.alter_column("users", "admin_role", server_default=None)
    op.alter_column("users", "session_version", server_default=None)


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_audit_logs_track_id"), table_name="admin_audit_logs")
    op.drop_index(op.f("ix_admin_audit_logs_target_user_id"), table_name="admin_audit_logs")
    op.drop_index(op.f("ix_admin_audit_logs_entity_type"), table_name="admin_audit_logs")
    op.drop_index(op.f("ix_admin_audit_logs_entity_id"), table_name="admin_audit_logs")
    op.drop_index(op.f("ix_admin_audit_logs_created_at"), table_name="admin_audit_logs")
    op.drop_index(op.f("ix_admin_audit_logs_circle_id"), table_name="admin_audit_logs")
    op.drop_index(op.f("ix_admin_audit_logs_album_id"), table_name="admin_audit_logs")
    op.drop_index(op.f("ix_admin_audit_logs_action"), table_name="admin_audit_logs")
    op.drop_index(op.f("ix_admin_audit_logs_actor_user_id"), table_name="admin_audit_logs")
    op.drop_index(op.f("ix_admin_audit_logs_id"), table_name="admin_audit_logs")
    op.drop_table("admin_audit_logs")

    op.drop_column("users", "session_version")
    op.drop_column("users", "suspension_reason")
    op.drop_column("users", "suspended_at")
    op.drop_column("users", "admin_role")
