import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from app.ws_manager import manager as track_manager
from app.ws_manager import notification_manager
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, select, text

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import (  # noqa: F401
    Album,
    AlbumMember,
    ChecklistItem,
    Circle,
    CircleInviteCode,
    CircleMember,
    Comment,
    CommentImage,
    EmailVerificationToken,
    Invitation,
    Issue,
    IssuePhase,
    IssueSeverity,
    IssueStatus,
    MasterDelivery,
    Notification,
    RejectionMode,
    Track,
    TrackPlaybackPreference,
    TrackSourceVersion,
    TrackStatus,
    User,
)
from app.routers import admin as admin_router
from app.routers import albums, auth, checklists, circles, discussions, issues, invitations, notifications, tracks, users, workflow_templates
from app.security import _decode_token, hash_password
from app.workflow import is_album_completed, log_track_event


def _run_sqlite_compat_migrations() -> None:
    if not settings.DATABASE_URL.startswith("sqlite"):
        return

    inspector = inspect(engine)
    columns_by_table = {
        table_name: {column["name"] for column in inspector.get_columns(table_name)}
        for table_name in inspector.get_table_names()
    }

    def add_column(table_name: str, column_name: str, definition: str) -> None:
        if table_name not in columns_by_table or column_name in columns_by_table[table_name]:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {definition}"))

    add_column("albums", "producer_id", "producer_id INTEGER")
    add_column("albums", "mastering_engineer_id", "mastering_engineer_id INTEGER")
    add_column("tracks", "submitter_id", "submitter_id INTEGER")
    add_column("tracks", "peer_reviewer_id", "peer_reviewer_id INTEGER")
    add_column("tracks", "rejection_mode", "rejection_mode VARCHAR(20)")
    add_column("tracks", "workflow_cycle", "workflow_cycle INTEGER NOT NULL DEFAULT 1")
    add_column("issues", "phase", "phase VARCHAR(20) NOT NULL DEFAULT 'peer'")
    add_column("issues", "workflow_cycle", "workflow_cycle INTEGER NOT NULL DEFAULT 1")
    add_column("issues", "source_version_id", "source_version_id INTEGER")
    add_column("issues", "master_delivery_id", "master_delivery_id INTEGER")
    add_column("checklist_items", "source_version_id", "source_version_id INTEGER")
    add_column("checklist_items", "workflow_cycle", "workflow_cycle INTEGER NOT NULL DEFAULT 1")
    add_column("comments", "is_status_note", "is_status_note BOOLEAN NOT NULL DEFAULT 0")
    add_column("tracks", "track_number", "track_number INTEGER")
    add_column("albums", "checklist_template", "checklist_template TEXT")
    add_column("albums", "deadline", "deadline DATETIME")
    add_column("albums", "phase_deadlines", "phase_deadlines TEXT")
    add_column("albums", "webhook_config", "webhook_config TEXT")
    add_column("albums", "release_date", "release_date DATE")
    add_column("albums", "catalog_number", "catalog_number VARCHAR(50)")
    add_column("albums", "circle_name", "circle_name VARCHAR(200)")
    add_column("albums", "genres", "genres TEXT")
    add_column("albums", "cover_image", "cover_image VARCHAR(500)")
    add_column("albums", "circle_id", "circle_id INTEGER REFERENCES circles(id)")
    # Existing users (before email verification feature) are grandfathered as verified
    add_column("users", "email_verified", "email_verified BOOLEAN NOT NULL DEFAULT 1")
    add_column("users", "is_admin", "is_admin BOOLEAN NOT NULL DEFAULT 0")

    with engine.begin() as conn:
        if "users" in columns_by_table:
            conn.execute(text("UPDATE users SET role = 'member' WHERE lower(role) IN ('author', 'reviewer')"))

        if "tracks" in columns_by_table:
            conn.execute(text("UPDATE tracks SET status = 'submitted' WHERE lower(status) = 'submitted' OR status = 'SUBMITTED'"))
            conn.execute(text("UPDATE tracks SET status = 'peer_review' WHERE lower(status) = 'in_review' OR status = 'IN_REVIEW'"))
            conn.execute(text("UPDATE tracks SET status = 'peer_revision' WHERE lower(status) = 'revision' OR status = 'REVISION'"))
            conn.execute(text("UPDATE tracks SET status = 'completed' WHERE lower(status) = 'approved' OR status = 'APPROVED'"))

        if "issues" in columns_by_table:
            conn.execute(text("UPDATE issues SET severity = 'critical' WHERE lower(severity) = 'critical' OR severity = 'CRITICAL'"))
            conn.execute(text("UPDATE issues SET severity = 'major' WHERE lower(severity) = 'major' OR severity = 'MAJOR'"))
            conn.execute(text("UPDATE issues SET severity = 'minor' WHERE lower(severity) = 'minor' OR severity = 'MINOR'"))
            conn.execute(text("UPDATE issues SET severity = 'suggestion' WHERE lower(severity) = 'suggestion' OR severity = 'SUGGESTION'"))
            conn.execute(text("UPDATE issues SET status = 'open' WHERE lower(status) = 'open' OR status = 'OPEN'"))
            conn.execute(text("UPDATE issues SET status = 'open' WHERE lower(status) = 'will_fix' OR status = 'WILL_FIX'"))
            conn.execute(text("UPDATE issues SET status = 'disagreed' WHERE lower(status) = 'disagreed' OR status = 'DISAGREED'"))
            conn.execute(text("UPDATE issues SET status = 'resolved' WHERE lower(status) = 'resolved' OR status = 'RESOLVED'"))


_BACKFILL_BATCH_SIZE = 100


def _backfill_workflow_data() -> None:
    from sqlalchemy import exists, or_

    db = SessionLocal()
    try:
        # --- Albums missing producer_id ---
        albums_needing_backfill = list(db.scalars(
            select(Album).where(Album.producer_id == None).limit(_BACKFILL_BATCH_SIZE)  # noqa: E711
        ).all())
        if albums_needing_backfill:
            first_producer = db.scalars(
                select(User).where(User.role == "producer").order_by(User.id).limit(1)
            ).first()
            fallback_user = db.scalars(select(User).order_by(User.id).limit(1)).first()
            if not fallback_user:
                return
            all_users: list | None = None  # lazy-loaded once if needed
            for album in albums_needing_backfill:
                album.producer_id = (first_producer or fallback_user).id
                if album.mastering_engineer_id is None:
                    other = db.scalars(
                        select(User).where(User.id != album.producer_id).order_by(User.id).limit(1)
                    ).first()
                    album.mastering_engineer_id = (other or fallback_user).id
                if not album.members:
                    if all_users is None:
                        all_users = list(db.scalars(select(User).order_by(User.id)).all())
                    for user in all_users:
                        db.add(AlbumMember(album_id=album.id, user_id=user.id))

        # --- Tracks missing submitter_id ---
        tracks_needing_backfill = list(db.scalars(
            select(Track).where(Track.submitter_id == None).limit(_BACKFILL_BATCH_SIZE)  # noqa: E711
        ).all())
        if tracks_needing_backfill:
            all_users_list = list(db.scalars(select(User).order_by(User.id)).all())
            for track in tracks_needing_backfill:
                album = db.get(Album, track.album_id)
                if album is None:
                    continue
                matching_user = next(
                    (u for u in all_users_list if u.display_name.lower() == track.artist.lower() or u.username.lower() == track.artist.lower()),
                    None,
                )
                fallback = next((u for u in all_users_list if u.id != album.producer_id), all_users_list[0] if all_users_list else None)
                track.submitter_id = (matching_user or fallback).id if (matching_user or fallback) else None

        # --- Tracks missing peer_reviewer_id ---
        tracks_no_reviewer = list(db.scalars(
            select(Track).where(Track.peer_reviewer_id == None, Track.submitter_id != None).limit(_BACKFILL_BATCH_SIZE)  # noqa: E711
        ).all())
        for track in tracks_no_reviewer:
            issue_author = next((issue.author_id for issue in track.issues if issue.author_id != track.submitter_id), None)
            checklist_reviewer = next((item.reviewer_id for item in track.checklist_items if item.reviewer_id != track.submitter_id), None)
            track.peer_reviewer_id = issue_author or checklist_reviewer

        # --- Issues missing workflow_cycle ---
        issues_needing = list(db.scalars(
            select(Issue).where(or_(Issue.workflow_cycle == None, Issue.workflow_cycle == 0)).limit(_BACKFILL_BATCH_SIZE)  # noqa: E711
        ).all())
        if issues_needing:
            for issue in issues_needing:
                track = db.get(Track, issue.track_id)
                if track:
                    issue.workflow_cycle = track.workflow_cycle

        # --- ChecklistItems missing workflow_cycle ---
        checklist_items_needing = list(db.scalars(
            select(ChecklistItem).where(or_(ChecklistItem.workflow_cycle == None, ChecklistItem.workflow_cycle == 0)).limit(_BACKFILL_BATCH_SIZE)  # noqa: E711
        ).all())
        if checklist_items_needing:
            for item in checklist_items_needing:
                track = db.get(Track, item.track_id)
                if track:
                    item.workflow_cycle = track.workflow_cycle

        # --- Tracks missing source_versions ---
        has_versions = exists(
            select(TrackSourceVersion.id).where(TrackSourceVersion.track_id == Track.id)
        )
        tracks_no_versions = list(db.scalars(
            select(Track).where(Track.file_path != None, ~has_versions).limit(_BACKFILL_BATCH_SIZE)  # noqa: E711
        ).all())
        for track in tracks_no_versions:
            db.add(TrackSourceVersion(
                track_id=track.id,
                workflow_cycle=track.workflow_cycle,
                version_number=track.version,
                file_path=track.file_path,
                duration=track.duration,
                uploaded_by_id=track.submitter_id,
            ))

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _seed_demo_data() -> None:
    db = SessionLocal()
    try:
        if db.query(User).first() is not None:
            return

        now = datetime.now(timezone.utc)
        producer = User(
            username="kira",
            display_name="Kira",
            role="producer",
            avatar_color="#f43f5e",
            email="kira@example.com",
            password=hash_password("password123"),
            email_verified=True,
            created_at=now,
        )
        submitter = User(
            username="nova",
            display_name="Nova",
            role="member",
            avatar_color="#3b82f6",
            email="nova@example.com",
            password=hash_password("password123"),
            email_verified=True,
            created_at=now,
        )
        mastering_engineer = User(
            username="echo",
            display_name="Echo",
            role="member",
            avatar_color="#10b981",
            email="echo@example.com",
            password=hash_password("password123"),
            email_verified=True,
            created_at=now,
        )
        db.add_all([producer, submitter, mastering_engineer])
        db.flush()

        circle = Circle(
            name="Back Kitchen",
            description="Demo doujin circle. All demo members belong here.",
            created_by=producer.id,
            created_at=now,
        )
        db.add(circle)
        db.flush()

        db.add_all(
            [
                CircleMember(circle_id=circle.id, user_id=producer.id, role="owner", joined_at=now),
                CircleMember(circle_id=circle.id, user_id=submitter.id, role="member", joined_at=now),
                CircleMember(circle_id=circle.id, user_id=mastering_engineer.id, role="mastering_engineer", joined_at=now),
            ]
        )

        album = Album(
            title="BACK KITCHEN Vol.1",
            description="Demo workflow album for reviewing doujin submissions.",
            cover_color="#8b5cf6",
            circle_id=circle.id,
            producer_id=producer.id,
            mastering_engineer_id=mastering_engineer.id,
            created_at=now,
            updated_at=now,
        )
        db.add(album)
        db.flush()

        db.add_all(
            [
                AlbumMember(album_id=album.id, user_id=producer.id, created_at=now),
                AlbumMember(album_id=album.id, user_id=submitter.id, created_at=now),
                AlbumMember(album_id=album.id, user_id=mastering_engineer.id, created_at=now),
            ]
        )

        track = Track(
            title="Neon Drizzle",
            artist="Nova",
            album_id=album.id,
            submitter_id=submitter.id,
            status="intake",
            version=1,
            workflow_cycle=1,
            created_at=now,
            updated_at=now,
        )
        db.add(track)
        db.flush()
        log_track_event(db, track, submitter, "track_submitted", to_status="intake")

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _run_alembic_upgrade() -> None:
    """Run alembic upgrade head to apply pending migrations."""
    import os
    alembic_cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    alembic_cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    alembic_command.upgrade(alembic_cfg, "head")


_CLEANUP_INTERVAL = 3600  # seconds (1 hour)
_cleanup_logger = logging.getLogger("cleanup")


def _delete_file(file_path: str, storage_backend: str) -> None:
    """Delete a single audio file from local disk or R2."""
    if storage_backend == "r2":
        from app.services.r2 import delete_object
        delete_object(file_path)
    else:
        p = Path(file_path)
        if not p.is_absolute():
            p = settings.get_upload_path() / p
        p.unlink(missing_ok=True)


def _run_expired_source_cleanup() -> int:
    """Delete audio files for expired TrackSourceVersion records.

    Also cleans up the parent track's file_path for finally-rejected tracks
    once all their source versions have been cleaned.  Returns count.
    """
    from app.models.track import RejectionMode, Track, TrackStatus
    from app.models.track_source_version import TrackSourceVersion

    db = SessionLocal()
    cleaned = 0
    try:
        now = datetime.now(timezone.utc)
        expired = list(db.scalars(
            select(TrackSourceVersion).where(
                TrackSourceVersion.expires_at.isnot(None),
                TrackSourceVersion.expires_at < now,
                TrackSourceVersion.file_path.isnot(None),
            )
        ).all())

        # Track IDs whose source versions we cleaned — check parent track too
        affected_track_ids: set[int] = set()

        for sv in expired:
            try:
                _delete_file(sv.file_path, sv.storage_backend)
                sv.file_path = None
                cleaned += 1
                affected_track_ids.add(sv.track_id)
            except Exception:
                _cleanup_logger.warning("Failed to clean up %s", sv.file_path, exc_info=True)

        # Clean up track.file_path for finally-rejected tracks
        if affected_track_ids:
            tracks = list(db.scalars(
                select(Track).where(
                    Track.id.in_(affected_track_ids),
                    Track.status == TrackStatus.REJECTED,
                    Track.rejection_mode == RejectionMode.FINAL,
                    Track.file_path.isnot(None),
                )
            ).all())
            for track in tracks:
                try:
                    _delete_file(track.file_path, track.storage_backend)
                    track.file_path = None
                    cleaned += 1
                except Exception:
                    _cleanup_logger.warning("Failed to clean up track %d file", track.id, exc_info=True)

        if cleaned:
            db.commit()
    finally:
        db.close()
    return cleaned


_ARCHIVE_CLEANUP_BATCH = 50


def _run_archived_track_cleanup() -> int:
    """Hard-delete tracks whose archived_at exceeded the retention period.

    Deletes all associated DB records (cascade) and audio files on disk/R2.
    Each track is committed individually so a failure doesn't poison the session.
    Returns the number of tracks deleted.
    """
    from app.models.track import ARCHIVE_RETENTION_DAYS, Track
    from app.services.cleanup import cleanup_files, collect_track_files

    db = SessionLocal()
    deleted = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_RETENTION_DAYS)
        expired_ids = list(db.scalars(
            select(Track.id).where(
                Track.archived_at.isnot(None),
                Track.archived_at < cutoff,
            ).limit(_ARCHIVE_CLEANUP_BATCH)
        ).all())

        for track_id in expired_ids:
            try:
                track = db.get(Track, track_id)
                if track is None or track.archived_at is None:
                    continue
                local_paths, r2_keys = collect_track_files(track)
                db.delete(track)
                db.commit()
                cleanup_files(local_paths, r2_keys)
                deleted += 1
            except Exception:
                db.rollback()
                _cleanup_logger.warning("Failed to hard-delete archived track %d", track_id, exc_info=True)
    finally:
        db.close()
    return deleted


def _run_archived_album_cleanup() -> int:
    """Hard-delete albums whose archived_at exceeded the retention period.

    Cascade-deletes all tracks, members, invitations, etc., and cleans up
    files from disk/R2. Returns the number of albums deleted.
    """
    from app.models.album import ALBUM_ARCHIVE_RETENTION_DAYS
    from app.services.cleanup import cleanup_files, collect_album_files

    db = SessionLocal()
    deleted = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ALBUM_ARCHIVE_RETENTION_DAYS)
        expired_ids = list(db.scalars(
            select(Album.id).where(
                Album.archived_at.isnot(None),
                Album.archived_at < cutoff,
            ).limit(_ARCHIVE_CLEANUP_BATCH)
        ).all())

        for album_id in expired_ids:
            try:
                album = db.get(Album, album_id)
                if album is None or album.archived_at is None:
                    continue
                local_paths, r2_keys = collect_album_files(album)
                db.delete(album)
                db.commit()
                cleanup_files(local_paths, r2_keys)
                deleted += 1
            except Exception:
                db.rollback()
                _cleanup_logger.warning("Failed to hard-delete archived album %d", album_id, exc_info=True)
    finally:
        db.close()
    return deleted


async def _periodic_cleanup() -> None:
    """Background loop that cleans up expired source versions and archived tracks/albums every hour."""
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL)
        try:
            count = _run_expired_source_cleanup()
            if count:
                _cleanup_logger.info("Cleaned up %d expired source version files", count)
        except Exception:
            _cleanup_logger.warning("Periodic source cleanup failed", exc_info=True)
        try:
            count = _run_archived_track_cleanup()
            if count:
                _cleanup_logger.info("Hard-deleted %d expired archived tracks", count)
        except Exception:
            _cleanup_logger.warning("Periodic archive cleanup failed", exc_info=True)
        try:
            count = _run_archived_album_cleanup()
            if count:
                _cleanup_logger.info("Hard-deleted %d expired archived albums", count)
        except Exception:
            _cleanup_logger.warning("Periodic album archive cleanup failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _run_alembic_upgrade()
    _backfill_workflow_data()
    upload_path = settings.get_upload_path()
    (upload_path / "comment_images").mkdir(parents=True, exist_ok=True)
    (upload_path / "covers").mkdir(parents=True, exist_ok=True)
    if settings.SEED_DEMO_DATA:
        _seed_demo_data()
    if settings.INITIAL_ADMIN_EMAIL:
        db = SessionLocal()
        try:
            user = db.scalars(select(User).where(User.email == settings.INITIAL_ADMIN_EMAIL)).first()
            if user and not user.is_admin:
                user.is_admin = True
                db.commit()
        finally:
            db.close()
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title=settings.APP_NAME, version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# Catch any non-HTTPException thrown from a handler, log the full traceback,
# and return a structured JSON 500.  Starlette's default ServerErrorMiddleware
# returns a bare "Internal Server Error" plain-text body that is easy to miss
# in logs; this handler guarantees the stack trace lands in journald so we
# can diagnose intermittent failures (e.g. SQLite lock contention).
_unhandled_logger = logging.getLogger("app.unhandled")


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    _unhandled_logger.exception(
        "Unhandled exception on %s %s",
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error.", "path": request.url.path},
    )


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(admin_router.router)
app.include_router(circles.router)
app.include_router(albums.router)
app.include_router(tracks.router)
app.include_router(issues.router)
app.include_router(checklists.router)
app.include_router(invitations.router)
app.include_router(notifications.router)
app.include_router(discussions.router)
app.include_router(workflow_templates.router)

try:
    upload_path = settings.get_upload_path()
    app.mount("/uploads", StaticFiles(directory=str(upload_path)), name="uploads")
except Exception:
    logging.getLogger(__name__).warning(
        "Failed to mount /uploads static files — uploaded audio will be inaccessible",
        exc_info=True,
    )


@app.get("/api/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def app_config():
    return {"r2_enabled": settings.R2_ENABLED}


@app.websocket("/ws/tracks/{track_id}")
async def websocket_track(websocket: WebSocket, track_id: int, token: str | None = None) -> None:
    if token is None:
        await websocket.close(code=4001)
        return
    try:
        payload = _decode_token(token)
    except HTTPException:
        await websocket.close(code=4001)
        return

    # Verify user has access to this track's album.
    user_id = int(payload["sub"])
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None or user.deleted_at is not None:
            await websocket.close(code=4001)
            return
        track = db.get(Track, track_id)
        if track is None:
            await websocket.close(code=4001)
            return
        album = db.get(Album, track.album_id)
        if album is None:
            await websocket.close(code=4001)
            return
        # Query 2: fetch album member ids
        member_ids: set[int] = set(
            db.scalars(
                select(AlbumMember.user_id).where(AlbumMember.album_id == album.id)
            ).all()
        )
        is_privileged = user.id in (album.producer_id, album.mastering_engineer_id)
        is_track_stakeholder = user.id in (track.submitter_id, track.peer_reviewer_id)
        is_member = user.id in member_ids
        if is_privileged or is_track_stakeholder:
            has_access = True
        elif is_member:
            has_access = is_album_completed(db, album.id)
        else:
            has_access = False
        if not has_access:
            await websocket.close(code=4003)
            return
    finally:
        db.close()

    connected = await track_manager.connect(track_id, websocket)
    if not connected:
        return
    try:
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                message = {"type": "message", "content": data}
            message["track_id"] = track_id
            await track_manager.broadcast(track_id, message)
    except WebSocketDisconnect:
        track_manager.disconnect(track_id, websocket)


@app.websocket("/ws/notifications")
async def websocket_notifications(websocket: WebSocket, token: str | None = None) -> None:
    if token is None:
        await websocket.close(code=4001)
        return
    try:
        payload = _decode_token(token)
    except HTTPException:
        await websocket.close(code=4001)
        return

    user_id = int(payload["sub"])
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None or user.deleted_at is not None:
            await websocket.close(code=4001)
            return
    finally:
        db.close()

    connected = await notification_manager.connect(user_id, websocket)
    if not connected:
        return
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        notification_manager.disconnect(user_id, websocket)
