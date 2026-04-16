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


def _get_table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def _get_column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _get_index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = _get_table_names(inspector)
    user_columns = _get_column_names(inspector, "users") if "users" in table_names else set()

    new_user_columns = [
        ("admin_role", sa.Column("admin_role", sa.String(length=20), nullable=False, server_default="none")),
        ("suspended_at", sa.Column("suspended_at", sa.DateTime(), nullable=True)),
        ("suspension_reason", sa.Column("suspension_reason", sa.Text(), nullable=True)),
        ("session_version", sa.Column("session_version", sa.Integer(), nullable=False, server_default="1")),
    ]
    for column_name, column in new_user_columns:
        if column_name not in user_columns:
            op.add_column("users", column)
            user_columns.add(column_name)

    if "users" in table_names:
        op.execute(
            """
            UPDATE users
            SET admin_role = CASE
                WHEN is_admin = 1 THEN 'superadmin'
                ELSE 'none'
            END
            WHERE admin_role IS NULL OR admin_role = '' OR (is_admin = 1 AND admin_role = 'none')
            """
        )
        op.execute("UPDATE users SET session_version = 1 WHERE session_version IS NULL OR session_version < 1")

    if "admin_audit_logs" not in table_names:
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

    inspector = sa.inspect(bind)
    audit_index_names = _get_index_names(inspector, "admin_audit_logs")
    for index_name, columns in [
        (op.f("ix_admin_audit_logs_id"), ["id"]),
        (op.f("ix_admin_audit_logs_actor_user_id"), ["actor_user_id"]),
        (op.f("ix_admin_audit_logs_action"), ["action"]),
        (op.f("ix_admin_audit_logs_album_id"), ["album_id"]),
        (op.f("ix_admin_audit_logs_circle_id"), ["circle_id"]),
        (op.f("ix_admin_audit_logs_created_at"), ["created_at"]),
        (op.f("ix_admin_audit_logs_entity_id"), ["entity_id"]),
        (op.f("ix_admin_audit_logs_entity_type"), ["entity_type"]),
        (op.f("ix_admin_audit_logs_target_user_id"), ["target_user_id"]),
        (op.f("ix_admin_audit_logs_track_id"), ["track_id"]),
    ]:
        if index_name not in audit_index_names:
            op.create_index(index_name, "admin_audit_logs", columns, unique=False)

    if bind.dialect.name != "sqlite":
        op.alter_column("users", "admin_role", server_default=None)
        op.alter_column("users", "session_version", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = _get_table_names(inspector)

    if "admin_audit_logs" in table_names:
        audit_index_names = _get_index_names(inspector, "admin_audit_logs")
        for index_name in [
            op.f("ix_admin_audit_logs_track_id"),
            op.f("ix_admin_audit_logs_target_user_id"),
            op.f("ix_admin_audit_logs_entity_type"),
            op.f("ix_admin_audit_logs_entity_id"),
            op.f("ix_admin_audit_logs_created_at"),
            op.f("ix_admin_audit_logs_circle_id"),
            op.f("ix_admin_audit_logs_album_id"),
            op.f("ix_admin_audit_logs_action"),
            op.f("ix_admin_audit_logs_actor_user_id"),
            op.f("ix_admin_audit_logs_id"),
        ]:
            if index_name in audit_index_names:
                op.drop_index(index_name, table_name="admin_audit_logs")
        op.drop_table("admin_audit_logs")

    user_columns = _get_column_names(inspector, "users") if "users" in table_names else set()
    if "session_version" in user_columns:
        op.drop_column("users", "session_version")
    if "suspension_reason" in user_columns:
        op.drop_column("users", "suspension_reason")
    if "suspended_at" in user_columns:
        op.drop_column("users", "suspended_at")
    if "admin_role" in user_columns:
        op.drop_column("users", "admin_role")
