import os
import sqlite3
import subprocess
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PRE_ADMIN_GOVERNANCE_REVISION = "f3a2b1c4d5e6"
PRE_AUDIT_LOG_REVISION = "f4b5c6d7e8f9"
PRE_TRACK_DELETE_INTEGRITY_REVISION = "n1o2p3q4r5s6"
HEAD_REVISION = "p2q3r4s5t6u7"


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


def _create_track_delete_integrity_schema(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            f"""
            CREATE TABLE users (
                id INTEGER NOT NULL PRIMARY KEY,
                created_at DATETIME NOT NULL
            );
            INSERT INTO users (id, created_at) VALUES (1, '2026-05-09 14:00:00');

            CREATE TABLE albums (
                id INTEGER NOT NULL PRIMARY KEY,
                created_at DATETIME NOT NULL
            );
            INSERT INTO albums (id, created_at) VALUES (1, '2026-05-09 14:00:00');

            CREATE TABLE tracks (
                id INTEGER NOT NULL PRIMARY KEY,
                created_at DATETIME NOT NULL
            );
            INSERT INTO tracks (id, created_at) VALUES (1, '2026-05-09 14:23:03');

            CREATE TABLE issues (
                id INTEGER NOT NULL PRIMARY KEY,
                track_id INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(track_id) REFERENCES tracks(id)
            );
            INSERT INTO issues (id, track_id, created_at)
            VALUES (1, 1, '2026-05-09 14:24:00');

            CREATE TABLE stage_assignments (
                id INTEGER NOT NULL PRIMARY KEY,
                track_id INTEGER NOT NULL,
                stage_id VARCHAR(50) NOT NULL,
                user_id INTEGER NOT NULL,
                status VARCHAR(20) NOT NULL,
                assigned_at DATETIME NOT NULL,
                completed_at DATETIME,
                decision VARCHAR(50),
                cancellation_reason VARCHAR(30),
                FOREIGN KEY(track_id) REFERENCES tracks(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            INSERT INTO stage_assignments (
                id, track_id, stage_id, user_id, status, assigned_at, completed_at, decision, cancellation_reason
            ) VALUES
                (1, 1, 'peer_review', 1, 'completed', '2026-05-09 14:13:31', NULL, 'needs_revision', NULL),
                (2, 99, 'peer_review', 1, 'pending', '2026-05-09 14:29:14', NULL, NULL, NULL),
                (3, 1, 'peer_review', 1, 'completed', '2026-05-09 14:29:14', NULL, 'pass', NULL);

            CREATE TABLE track_playback_preferences (
                id INTEGER NOT NULL PRIMARY KEY,
                track_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                scope VARCHAR(32) NOT NULL,
                gain_db FLOAT NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(track_id) REFERENCES tracks(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            INSERT INTO track_playback_preferences (
                id, track_id, user_id, scope, gain_db, created_at, updated_at
            ) VALUES
                (1, 1, 1, 'track', 0.0, '2026-05-09 14:20:00', '2026-05-09 14:20:00'),
                (2, 99, 1, 'track', 0.0, '2026-05-09 14:29:14', '2026-05-09 14:29:14'),
                (3, 1, 1, 'track', 0.0, '2026-05-09 14:29:14', '2026-05-09 14:29:14');

            CREATE TABLE notifications (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                type VARCHAR(50) NOT NULL,
                title VARCHAR(200) NOT NULL,
                body TEXT NOT NULL,
                related_track_id INTEGER,
                related_issue_id INTEGER,
                is_read BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL,
                related_album_id INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(related_track_id) REFERENCES tracks(id) ON DELETE SET NULL,
                FOREIGN KEY(related_issue_id) REFERENCES issues(id) ON DELETE SET NULL,
                FOREIGN KEY(related_album_id) REFERENCES albums(id) ON DELETE SET NULL
            );
            INSERT INTO notifications (
                id, user_id, type, title, body, related_track_id, related_issue_id, is_read, created_at, related_album_id
            ) VALUES
                (1, 1, 'track', 'stale track', '', 1, NULL, 0, '2026-05-09 14:20:00', NULL),
                (2, 1, 'track', 'missing track', '', 99, NULL, 0, '2026-05-09 14:29:14', NULL),
                (3, 1, 'issue', 'missing issue', '', NULL, 99, 0, '2026-05-09 14:29:14', NULL),
                (4, 1, 'issue', 'stale issue', '', NULL, 1, 0, '2026-05-09 14:23:30', NULL),
                (5, 1, 'issue', 'valid issue', '', NULL, 1, 0, '2026-05-09 14:25:00', NULL),
                (6, 1, 'album', 'missing album', '', NULL, NULL, 0, '2026-05-09 14:29:14', 99),
                (7, 1, 'album', 'stale album', '', NULL, NULL, 0, '2026-05-09 13:59:00', 1),
                (8, 1, 'album', 'valid album', '', NULL, NULL, 0, '2026-05-09 14:05:00', 1);

            CREATE TABLE admin_audit_logs (
                id INTEGER NOT NULL PRIMARY KEY,
                action VARCHAR(100) NOT NULL,
                track_id INTEGER,
                entity_id INTEGER,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(track_id) REFERENCES tracks(id)
            );
            INSERT INTO admin_audit_logs (id, action, track_id, entity_id, created_at)
            VALUES
                (1, 'track_deleted', 1, 1, '2026-05-09 14:15:06'),
                (2, 'track_force_status', 1, 1, '2026-05-09 14:20:00'),
                (3, 'track_force_status', 99, 99, '2026-05-09 14:29:14'),
                (4, 'track_force_status', 1, 1, '2026-05-09 14:30:00');

            CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
            INSERT INTO alembic_version (version_num) VALUES ('{PRE_TRACK_DELETE_INTEGRITY_REVISION}');
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
        album_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(albums)").fetchall()
        }
        circle_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(circles)").fetchall()
        }
        assert "checklist_enabled" in album_columns
        assert "default_checklist_enabled" in circle_columns
        discussion_audio_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'track_discussion_audios'"
        ).fetchone()
        if discussion_audio_table:
            discussion_audio_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(track_discussion_audios)").fetchall()
            }
            assert "storage_backend" in discussion_audio_columns

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


def test_track_delete_integrity_migration_cleans_stale_links(tmp_path: Path) -> None:
    db_path = tmp_path / "track-delete-integrity.db"
    _create_track_delete_integrity_schema(db_path)

    result = _run_upgrade(db_path)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            HEAD_REVISION,
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT id FROM stage_assignments ORDER BY id").fetchall() == [
            (3,)
        ]
        assert conn.execute(
            "SELECT id FROM track_playback_preferences ORDER BY id"
        ).fetchall() == [(3,)]
        assert conn.execute(
            """
            SELECT id, related_track_id, related_issue_id, related_album_id
            FROM notifications
            ORDER BY id
            """
        ).fetchall() == [
            (1, None, None, None),
            (2, None, None, None),
            (3, None, None, None),
            (4, None, None, None),
            (5, None, 1, None),
            (6, None, None, None),
            (7, None, None, None),
            (8, None, None, 1),
        ]
        assert conn.execute(
            "SELECT id, track_id FROM admin_audit_logs ORDER BY id"
        ).fetchall() == [
            (1, None),
            (2, None),
            (3, None),
            (4, 1),
        ]
    finally:
        conn.close()
