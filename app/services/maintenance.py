"""Startup backfill jobs and periodic cleanup/maintenance tasks.

Moved out of ``app.main`` so the application module only wires up the app;
these jobs run from the lifespan hook and the hourly cleanup loop.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import exists, or_, select, text

from app.config import settings
from app.database import SessionLocal
from app.models.album import ALBUM_ARCHIVE_RETENTION_DAYS, Album
from app.models.album_member import AlbumMember
from app.models.checklist import ChecklistItem
from app.models.circle import Circle, CircleMember
from app.models.issue import Issue
from app.models.track import ARCHIVE_RETENTION_DAYS, RejectionMode, Track, TrackStatus
from app.models.track_composer import TrackComposer, TrackExternalComposer
from app.models.track_source_version import TrackSourceVersion
from app.models.user import User
from app.security import hash_password
from app.services.track_queries import log_track_event

_BACKFILL_BATCH_SIZE = 100


def _backfill_workflow_data() -> None:
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


        # --- Proxy tracks missing an external composer record ---
        external_composer_exists = exists(
            select(TrackExternalComposer.id).where(
                TrackExternalComposer.track_id == Track.id,
            )
        )
        proxy_tracks_missing_external = list(db.scalars(
            select(Track)
            .where(
                Track.external_submitter_name != None,  # noqa: E711
                ~external_composer_exists,
            )
            .limit(_BACKFILL_BATCH_SIZE)
        ).all())
        for track in proxy_tracks_missing_external:
            external_name = (track.external_submitter_name or "").strip()
            if external_name:
                db.add(TrackExternalComposer(track_id=track.id, name=external_name, sort_order=0))

        # --- Tracks missing composer membership for their primary submitter ---
        has_primary_composer = exists(
            select(TrackComposer.id).where(
                TrackComposer.track_id == Track.id,
                TrackComposer.user_id == Track.submitter_id,
            )
        )
        tracks_missing_composer = list(db.scalars(
            select(Track)
            .where(
                Track.submitter_id != None,  # noqa: E711
                Track.external_submitter_name == None,  # noqa: E711
                ~has_primary_composer,
            )
            .limit(_BACKFILL_BATCH_SIZE)
        ).all())
        for track in tracks_missing_composer:
            db.add(TrackComposer(track_id=track.id, user_id=track.submitter_id))
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

        db.execute(
            text("UPDATE albums SET checklist_enabled = 1 WHERE checklist_enabled IS NULL")
        )
        db.execute(
            text(
                "UPDATE circles SET default_checklist_enabled = 1 "
                "WHERE default_checklist_enabled IS NULL"
            )
        )
        db.execute(text("UPDATE track_discussion_audios SET storage_backend = 'local' WHERE storage_backend IS NULL"))

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
        db.add(TrackComposer(track_id=track.id, user_id=submitter.id, created_at=now))
        log_track_event(db, track, submitter, "track_submitted", to_status="intake")

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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
    # Lazy import: tests monkeypatch these names on the cleanup module.
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
    # Lazy import: tests monkeypatch these names on the cleanup module.
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
