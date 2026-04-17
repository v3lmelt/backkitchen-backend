import os
import sqlite3
import subprocess
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PRE_ADMIN_GOVERNANCE_REVISION = "f3a2b1c4d5e6"
PRE_AUDIT_LOG_REVISION = "f4b5c6d7e8f9"
HEAD_REVISION = "m9n0o1p2q3r4"


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.as_posix()}"


def _run_upgrade(db_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AUDIO_MGMT_DATABASE_URL"] = _sqlite_url(db_path)
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _create_master_like_schema(db_path: Path, revision: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            f"""
            CREATE TABLE users (
                id INTEGER NOT NULL PRIMARY KEY,
                username VARCHAR(50) NOT NULL,
                display_name VARCHAR(100) NOT NULL,
                role VARCHAR(20) NOT NULL,
                avatar_color VARCHAR(7) NOT NULL,
                avatar_image VARCHAR(500),
                email VARCHAR(254),
                password VARCHAR(255),
                email_verified BOOLEAN NOT NULL DEFAULT 1,
                is_admin BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                feishu_contact VARCHAR(100),
                deleted_at DATETIME
            );
            INSERT INTO users (
                id,
                username,
                display_name,
                role,
                avatar_color,
                avatar_image,
                email,
                password,
                email_verified,
                is_admin,
                created_at,
                feishu_contact,
                deleted_at
            ) VALUES (
                1,
                'admin',
                'Admin',
                'member',
                '#123456',
                NULL,
                'admin@example.com',
                'pw',
                1,
                1,
                '2026-04-17 00:00:00',
                NULL,
                NULL
            );
            CREATE TABLE albums (id INTEGER NOT NULL PRIMARY KEY);
            INSERT INTO albums (id) VALUES (1);
            CREATE TABLE circles (id INTEGER NOT NULL PRIMARY KEY);
            INSERT INTO circles (id) VALUES (1);
            CREATE TABLE tracks (id INTEGER NOT NULL PRIMARY KEY);
            INSERT INTO tracks (id) VALUES (1);
            CREATE TABLE track_source_versions (
                id INTEGER NOT NULL PRIMARY KEY,
                file_path VARCHAR(500) NOT NULL
            );
            CREATE TABLE issues (
                id INTEGER NOT NULL PRIMARY KEY,
                track_id INTEGER NOT NULL,
                created_at DATETIME NOT NULL
            );
            CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
            INSERT INTO alembic_version (version_num) VALUES ('{revision}');
            """
        )
        conn.commit()
    finally:
        conn.close()


def _seed_half_applied_admin_migration(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            ALTER TABLE users ADD COLUMN admin_role VARCHAR(20) NOT NULL DEFAULT 'none';
            ALTER TABLE users ADD COLUMN suspended_at DATETIME;
            ALTER TABLE users ADD COLUMN suspension_reason TEXT;
            ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1;
            CREATE TABLE admin_audit_logs (
                id INTEGER NOT NULL PRIMARY KEY,
                actor_user_id INTEGER,
                action VARCHAR(100) NOT NULL,
                entity_type VARCHAR(50) NOT NULL,
                entity_id INTEGER,
                summary VARCHAR(500),
                reason TEXT,
                before_state TEXT,
                after_state TEXT,
                target_user_id INTEGER,
                album_id INTEGER,
                track_id INTEGER,
                circle_id INTEGER,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(actor_user_id) REFERENCES users(id),
                FOREIGN KEY(album_id) REFERENCES albums(id),
                FOREIGN KEY(circle_id) REFERENCES circles(id),
                FOREIGN KEY(target_user_id) REFERENCES users(id),
                FOREIGN KEY(track_id) REFERENCES tracks(id)
            );
            CREATE INDEX ix_admin_audit_logs_id ON admin_audit_logs (id);
            CREATE INDEX ix_admin_audit_logs_actor_user_id ON admin_audit_logs (actor_user_id);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _assert_upgrade_succeeded(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version == (HEAD_REVISION,)

        admin_role, session_version = conn.execute(
            "SELECT admin_role, session_version FROM users WHERE id = 1"
        ).fetchone()
        assert admin_role == "superadmin"
        assert session_version == 1

        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'admin_audit_logs'"
        ).fetchone()
        assert table == ("admin_audit_logs",)

        index_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'admin_audit_logs'"
            ).fetchall()
        }
        assert {
            "ix_admin_audit_logs_id",
            "ix_admin_audit_logs_actor_user_id",
            "ix_admin_audit_logs_action",
            "ix_admin_audit_logs_album_id",
            "ix_admin_audit_logs_circle_id",
            "ix_admin_audit_logs_created_at",
            "ix_admin_audit_logs_entity_id",
            "ix_admin_audit_logs_entity_type",
            "ix_admin_audit_logs_target_user_id",
            "ix_admin_audit_logs_track_id",
        }.issubset(index_names)
    finally:
        conn.close()


def test_sqlite_upgrade_from_master_head_to_dev_head_succeeds(tmp_path: Path) -> None:
    db_path = tmp_path / "master-to-dev.db"
    _create_master_like_schema(db_path, PRE_ADMIN_GOVERNANCE_REVISION)

    result = _run_upgrade(db_path)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    _assert_upgrade_succeeded(db_path)


def test_sqlite_upgrade_recovers_from_half_applied_admin_governance_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "half-applied-admin-governance.db"
    _create_master_like_schema(db_path, PRE_AUDIT_LOG_REVISION)
    _seed_half_applied_admin_migration(db_path)

    result = _run_upgrade(db_path)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    _assert_upgrade_succeeded(db_path)
