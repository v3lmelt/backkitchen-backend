import asyncio
import io
import json
import logging
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func as sqlfunc, func, select
from sqlalchemy.orm import Session

from app.admin_permissions import has_admin_role
from app.config import settings
from app.database import get_db
from app.models.album import ALBUM_ARCHIVE_RETENTION_DAYS, Album
from app.models.album_member import AlbumMember
from app.models.circle import CircleMember
from app.models.issue import Issue, IssueStatus
from app.models.track import Track, TrackStatus
from app.models.user import User
from app.models.workflow_event import WorkflowEvent
from app.notifications import notify
from app.schemas.schemas import AlbumCreate, AlbumDeadlineUpdate, AlbumMetadataUpdate, AlbumRead, AlbumStats, AlbumTeamUpdate, TrackOrderUpdate, TrackRead, UserRead, WebhookConfig, WebhookDeliveryRead, WorkflowConfigSchema, WorkflowEventRead
from app.security import get_current_user, require_producer
from app.services.upload import stream_upload
from app.services.webhook import build_webhook_payload, post_webhook
from app.workflow import build_event_read, build_track_read, current_master_delivery, ensure_album_producer, ensure_album_visibility, get_album_member_ids, get_all_album_member_ids, is_album_completed, peer_identity_anonymize_user_ids_for_viewer
from app.workflow_defaults import DEFAULT_WORKFLOW_CONFIG

router = APIRouter(prefix="/api/albums", tags=["albums"])


def _album_to_read(album: Album, db: Session) -> AlbumRead:
    members = [
        {
            "id": member.id,
            "user_id": member.user_id,
            "created_at": member.created_at,
            "user": UserRead.model_validate(member.user),
        }
        for member in album.members
    ]
    phase_deadlines = json.loads(album.phase_deadlines) if album.phase_deadlines else None
    genres = json.loads(album.genres) if album.genres else None
    workflow_config: WorkflowConfigSchema | None = None
    try:
        workflow_config = WorkflowConfigSchema(**json.loads(album.workflow_config))
    except Exception:
        logger.warning(
            "Album %d has an invalid workflow_config and will be read without it.",
            album.id,
        )
    track_count = db.scalar(
        select(func.count()).select_from(Track).where(
            Track.album_id == album.id,
            Track.archived_at.is_(None),
            Track.status != TrackStatus.REJECTED,
        )
    ) or 0
    template_name = None
    if album.workflow_template_id and album.workflow_template:
        template_name = album.workflow_template.name

    return AlbumRead(
        id=album.id,
        title=album.title,
        description=album.description,
        cover_color=album.cover_color,
        release_date=album.release_date,
        catalog_number=album.catalog_number,
        circle_id=album.circle_id,
        circle_name=album.circle_name,
        genres=genres,
        cover_image=album.cover_image,
        producer_id=album.producer_id,
        mastering_engineer_id=album.mastering_engineer_id,
        deadline=album.deadline,
        phase_deadlines=phase_deadlines,
        workflow_config=workflow_config,
        workflow_template_id=album.workflow_template_id,
        workflow_template_name=template_name,
        created_at=album.created_at,
        updated_at=album.updated_at,
        archived_at=album.archived_at,
        track_count=track_count,
        producer=UserRead.model_validate(album.producer) if album.producer else None,
        mastering_engineer=(
            UserRead.model_validate(album.mastering_engineer)
            if album.mastering_engineer
            else None
        ),
        members=members,
    )


@router.post("", response_model=AlbumRead, status_code=status.HTTP_201_CREATED)
def create_album(
    payload: AlbumCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_producer),
) -> AlbumRead:
    album_data = payload.model_dump()
    genres = album_data.pop("genres", None)
    wf_config = album_data.pop("workflow_config", None)
    wf_template_id = album_data.pop("workflow_template_id", None)
    album = Album(**album_data, producer_id=current_user.id)
    if genres:
        album.genres = json.dumps(genres, ensure_ascii=False)
    effective_workflow = wf_config if wf_config is not None else DEFAULT_WORKFLOW_CONFIG
    album.workflow_config = json.dumps(effective_workflow, ensure_ascii=False)
    if wf_template_id is not None:
        album.workflow_template_id = wf_template_id
    db.add(album)
    db.flush()
    db.add(AlbumMember(album_id=album.id, user_id=current_user.id))
    db.commit()
    db.refresh(album)
    return _album_to_read(album, db)


@router.get("", response_model=list[AlbumRead])
def list_albums(
    include_archived: bool = Query(False),
    archived_only: bool = Query(False),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AlbumRead]:
    stmt = select(Album).order_by(Album.id)
    if archived_only:
        stmt = stmt.where(Album.archived_at.isnot(None))
    elif not include_archived:
        stmt = stmt.where(Album.archived_at.is_(None))
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(Album.title.ilike(pattern) | Album.description.ilike(pattern))
    albums = list(db.scalars(stmt).all())
    if current_user.is_admin:
        return [_album_to_read(album, db) for album in albums]
    members_by_album = get_all_album_member_ids(db)
    visible: list[AlbumRead] = []
    for album in albums:
        member_ids = members_by_album.get(album.id, set())
        if current_user.id in {album.producer_id, album.mastering_engineer_id} | member_ids:
            visible.append(_album_to_read(album, db))
    return visible


@router.get("/{album_id}", response_model=AlbumRead)
def get_album(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlbumRead:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)
    return _album_to_read(album, db)


@router.patch("/{album_id}/team", response_model=AlbumRead)
def update_album_team(
    album_id: int,
    payload: AlbumTeamUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlbumRead:
    album = ensure_album_producer(album_id, current_user, db)

    # --- validate mastering engineer exists ---
    if payload.mastering_engineer_id is not None:
        mastering_engineer = db.get(User, payload.mastering_engineer_id)
        if mastering_engineer is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Mastering engineer not found.",
            )

    # --- validate members ---
    desired_member_ids = set(payload.member_ids)
    desired_member_ids.add(current_user.id)

    if album.circle_id is not None:
        circle_member_ids = set(
            db.scalars(
                select(CircleMember.user_id).where(CircleMember.circle_id == album.circle_id)
            ).all()
        )
        invalid_ids = desired_member_ids - circle_member_ids
        if invalid_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Users {sorted(invalid_ids)} are not members of this album's circle.",
            )
        if payload.mastering_engineer_id is not None and payload.mastering_engineer_id not in circle_member_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Mastering engineer is not a member of this album's circle.",
            )
    else:
        for user_id in desired_member_ids:
            if db.get(User, user_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User {user_id} not found.",
                )

    # --- apply changes (all validation passed) ---
    album.mastering_engineer_id = payload.mastering_engineer_id

    existing_members = {member.user_id: member for member in album.members}
    for user_id, member in list(existing_members.items()):
        if user_id not in desired_member_ids:
            db.delete(member)
    for user_id in desired_member_ids:
        if user_id not in existing_members:
            db.add(AlbumMember(album_id=album.id, user_id=user_id))

    db.commit()
    db.refresh(album)
    return _album_to_read(album, db)


@router.delete("/{album_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_album_member(
    album_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found.")
    if current_user.id != album.producer_id and not has_admin_role(current_user, "operator"):
        raise HTTPException(status_code=403, detail="Only the album producer can remove members.")
    if user_id == album.producer_id:
        raise HTTPException(status_code=400, detail="Cannot remove the album producer.")
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot remove yourself. Use the leave endpoint instead.")

    member = db.execute(
        select(AlbumMember).where(
            AlbumMember.album_id == album_id,
            AlbumMember.user_id == user_id,
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found in this album.")

    if album.mastering_engineer_id == user_id:
        album.mastering_engineer_id = None
    db.delete(member)
    db.commit()


@router.post("/{album_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave_album(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found.")
    if current_user.id == album.producer_id:
        raise HTTPException(
            status_code=400,
            detail="The album producer cannot leave. Transfer ownership or archive the album instead.",
        )

    member = db.execute(
        select(AlbumMember).where(
            AlbumMember.album_id == album_id,
            AlbumMember.user_id == current_user.id,
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=404, detail="You are not a member of this album.")

    if album.mastering_engineer_id == current_user.id:
        album.mastering_engineer_id = None
    db.delete(member)
    db.commit()


@router.patch("/{album_id}/deadlines", response_model=AlbumRead)
def update_deadlines(
    album_id: int,
    payload: AlbumDeadlineUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlbumRead:
    album = ensure_album_producer(album_id, current_user, db)
    album.deadline = payload.deadline
    album.phase_deadlines = json.dumps(payload.phase_deadlines) if payload.phase_deadlines else None
    db.commit()
    db.refresh(album)
    return _album_to_read(album, db)


@router.patch("/{album_id}/metadata", response_model=AlbumRead)
def update_album_metadata(
    album_id: int,
    payload: AlbumMetadataUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlbumRead:
    album = ensure_album_producer(album_id, current_user, db)
    if payload.title is not None:
        album.title = payload.title
    album.description = payload.description
    album.release_date = payload.release_date
    album.catalog_number = payload.catalog_number
    album.circle_name = payload.circle_name
    album.genres = json.dumps(payload.genres, ensure_ascii=False) if payload.genres else None
    db.commit()
    db.refresh(album)
    return _album_to_read(album, db)


@router.post("/{album_id}/cover", response_model=AlbumRead)
async def upload_album_cover(
    album_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlbumRead:
    album = ensure_album_producer(album_id, current_user, db)

    from app.config import MAX_IMAGE_UPLOAD_SIZE

    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only JPEG, PNG, WebP, and GIF images are allowed.",
        )

    ext = Path(file.filename).suffix.lower() if file.filename else ".jpg"
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported image extension: {ext}",
        )
    filename = f"{album_id}_{uuid.uuid4().hex}{ext}"
    cover_dir = settings.get_upload_path() / "covers"
    cover_dir.mkdir(parents=True, exist_ok=True)

    dest = cover_dir / filename
    await stream_upload(file, dest, MAX_IMAGE_UPLOAD_SIZE)

    # Remove old cover file
    if album.cover_image:
        old_path = settings.get_upload_path() / album.cover_image
        if old_path.exists():
            old_path.unlink(missing_ok=True)

    album.cover_image = f"covers/{filename}"
    db.commit()
    db.refresh(album)
    return _album_to_read(album, db)


@router.get("/{album_id}/stats", response_model=AlbumStats)
def get_album_stats(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlbumStats:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)

    # Aggregate track counts by status in a single query
    status_rows = db.execute(
        select(Track.status, sqlfunc.count(Track.id))
        .where(
            Track.album_id == album_id,
            Track.archived_at.is_(None),
            Track.status != TrackStatus.REJECTED,
        )
        .group_by(Track.status)
    ).all()
    by_status: dict[str, int] = {row[0]: row[1] for row in status_rows}
    total_tracks = sum(by_status.values())

    # Load tracks only for deadline overdue calculation (only if phase_deadlines is set)
    tracks: list[Track] = []
    if album.phase_deadlines:
        tracks = list(db.scalars(
            select(Track).where(
                Track.album_id == album_id,
                Track.archived_at.is_(None),
                Track.status != TrackStatus.REJECTED,
            )
        ).all())

    open_issues = db.scalar(
        select(sqlfunc.count(Issue.id))
        .join(Track, Issue.track_id == Track.id)
        .where(Track.album_id == album_id, Issue.status == IssueStatus.OPEN)
    ) or 0

    recent_events = list(db.scalars(
        select(WorkflowEvent)
        .where(WorkflowEvent.album_id == album_id)
        .order_by(WorkflowEvent.created_at.desc())
        .limit(10)
    ).all())

    # Pre-fetch actors for recent events
    actor_ids = {e.actor_user_id for e in recent_events if e.actor_user_id}
    actors_by_id: dict[int, User] = {}
    if actor_ids:
        actors_by_id = {u.id: u for u in db.scalars(select(User).where(User.id.in_(actor_ids))).all()}

    anonymize_user_ids: set[int] = set()
    for track in db.scalars(select(Track).where(Track.album_id == album_id)).all():
        anonymize_user_ids.update(
            peer_identity_anonymize_user_ids_for_viewer(db, track, album, current_user)
        )

    overdue_count = 0
    if album.phase_deadlines:
        deadlines = json.loads(album.phase_deadlines)
        now = datetime.now(timezone.utc)
        phase_status_map = {
            "peer_review": {"peer_review", "peer_revision"},
            "mastering": {"mastering", "mastering_revision"},
            "final_review": {"final_review"},
        }
        for track in tracks:
            for phase_key, statuses in phase_status_map.items():
                if track.status in statuses and phase_key in deadlines:
                    try:
                        dl = datetime.fromisoformat(deadlines[phase_key])
                        if dl.tzinfo is None:
                            dl = dl.replace(tzinfo=timezone.utc)
                        if now > dl:
                            overdue_count += 1
                            break
                    except (ValueError, TypeError):
                        pass

    return AlbumStats(
        total_tracks=total_tracks,
        by_status=by_status,
        open_issues=open_issues,
        recent_events=[
            build_event_read(
                e,
                db,
                users_cache=actors_by_id,
                anonymize_user_ids=anonymize_user_ids,
            )
            for e in recent_events
        ],
        deadline=album.deadline,
        overdue_track_count=overdue_count,
    )


@router.get("/{album_id}/activity", response_model=list[WorkflowEventRead])
def get_album_activity(
    album_id: int,
    event_type: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[WorkflowEventRead]:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)

    stmt = select(WorkflowEvent).where(WorkflowEvent.album_id == album_id)
    if event_type:
        stmt = stmt.where(WorkflowEvent.event_type == event_type)
    stmt = stmt.order_by(WorkflowEvent.created_at.desc()).offset(offset).limit(limit)

    events = list(db.scalars(stmt).all())
    actor_ids = {e.actor_user_id for e in events if e.actor_user_id}
    actors_by_id: dict[int, User] = {}
    if actor_ids:
        actors_by_id = {u.id: u for u in db.scalars(select(User).where(User.id.in_(actor_ids))).all()}

    anonymize_user_ids: set[int] = set()
    for track in db.scalars(select(Track).where(Track.album_id == album_id)).all():
        anonymize_user_ids.update(
            peer_identity_anonymize_user_ids_for_viewer(db, track, album, current_user)
        )

    return [
        build_event_read(
            e,
            db,
            users_cache=actors_by_id,
            anonymize_user_ids=anonymize_user_ids,
        )
        for e in events
    ]


@router.get("/{album_id}/tracks", response_model=list[TrackRead])
def list_album_tracks(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TrackRead]:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)

    filters = [
        Track.album_id == album_id,
        Track.archived_at.is_(None),
        Track.status != TrackStatus.REJECTED,
    ]
    is_privileged = current_user.id in (album.producer_id, album.mastering_engineer_id)
    if not is_privileged and not is_album_completed(db, album_id):
        filters.append(
            (Track.submitter_id == current_user.id) | (Track.peer_reviewer_id == current_user.id)
        )

    tracks = list(db.scalars(
        select(Track).where(*filters)
        .order_by(Track.track_number.asc().nulls_last(), Track.id)
    ).all())
    return [build_track_read(track, current_user, album, db=db) for track in tracks]


@router.get("/{album_id}/archived-tracks", response_model=list[TrackRead])
def list_archived_tracks(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TrackRead]:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)
    if current_user.id != album.producer_id and not has_admin_role(current_user, "viewer"):
        raise HTTPException(status_code=403, detail="Only the album producer can view archived tracks.")

    tracks = list(db.scalars(
        select(Track).where(
            Track.album_id == album_id,
            Track.archived_at.isnot(None),
        )
        .order_by(Track.archived_at.desc())
    ).all())
    return [build_track_read(track, current_user, album, db=db) for track in tracks]


@router.patch("/{album_id}/track-order", response_model=list[TrackRead])
def reorder_tracks(
    album_id: int,
    payload: TrackOrderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TrackRead]:
    album = ensure_album_producer(album_id, current_user, db)

    tracks = list(db.scalars(select(Track).where(Track.album_id == album_id)).all())
    track_map = {t.id: t for t in tracks}
    for i, tid in enumerate(payload.track_ids, 1):
        if tid not in track_map:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Track {tid} not in this album.")
        track_map[tid].track_number = i
    for t in tracks:
        if t.id not in payload.track_ids:
            t.track_number = None
    db.commit()
    for t in tracks:
        db.refresh(t)
    ordered = sorted(tracks, key=lambda x: (x.track_number is None, x.track_number or 0, x.id))
    return [build_track_read(t, current_user, album, db=db) for t in ordered]


@router.get("/{album_id}/webhook", response_model=WebhookConfig)
def get_webhook_config(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WebhookConfig:
    album = ensure_album_producer(album_id, current_user, db)
    if album.webhook_config:
        config = json.loads(album.webhook_config)
        return WebhookConfig(**config)
    return WebhookConfig()


@router.patch("/{album_id}/webhook", response_model=WebhookConfig)
def update_webhook_config(
    album_id: int,
    payload: WebhookConfig,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WebhookConfig:
    album = ensure_album_producer(album_id, current_user, db)
    album.webhook_config = json.dumps(payload.model_dump())
    db.commit()
    db.refresh(album)
    return WebhookConfig(**payload.model_dump())


@router.post("/{album_id}/webhook/test")
async def test_webhook(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, bool]:
    album = ensure_album_producer(album_id, current_user, db)
    if not album.webhook_config:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No webhook configured.")
    config = json.loads(album.webhook_config)
    if not config.get("url"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No webhook URL configured.")
    payload = build_webhook_payload("test", "Webhook Test", f"Test from album: {album.title}", album_id=album.id)
    success = await post_webhook(
        config["url"], payload, db=db, album_id=album.id, event_type="test",
        webhook_type=config.get("type", "generic"),
        webhook_secret=config.get("secret", ""),
        feishu_app_id=config.get("app_id", ""),
        feishu_app_secret=config.get("app_secret", ""),
    )
    return {"success": success}


@router.get("/{album_id}/webhook/deliveries", response_model=list[WebhookDeliveryRead])
def get_webhook_deliveries(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[WebhookDeliveryRead]:
    album = ensure_album_producer(album_id, current_user, db)
    from app.models.webhook_delivery import WebhookDelivery
    records = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.album_id == album_id)
        .order_by(WebhookDelivery.id.desc())
        .limit(50)
        .all()
    )
    return [WebhookDeliveryRead.model_validate(r) for r in records]


@router.get("/{album_id}/workflow", response_model=WorkflowConfigSchema)
def get_workflow_config(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkflowConfigSchema:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)
    return WorkflowConfigSchema(**json.loads(album.workflow_config))


@router.put("/{album_id}/workflow", response_model=dict)
def update_workflow_config(
    album_id: int,
    payload: WorkflowConfigSchema,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    from app.workflow_engine import migrate_tracks_on_workflow_change, parse_workflow_config

    album = ensure_album_producer(album_id, current_user, db)

    old_config = parse_workflow_config(album)
    new_config = payload.model_dump()

    album.workflow_config = json.dumps(new_config, ensure_ascii=False)
    migrations = migrate_tracks_on_workflow_change(db, album, old_config, new_config, background_tasks)

    db.commit()
    db.refresh(album)
    return {"ok": True, "migrations": migrations}


# ---------------------------------------------------------------------------
# Album export – SSE progress stream + temp-file download
# ---------------------------------------------------------------------------

# In-memory store for completed export temp files: download_id -> (path, created_ts)
_export_temp_store: dict[str, tuple[str, float]] = {}
_EXPORT_TTL_SECONDS = 600  # 10 minutes


def _cleanup_expired_exports() -> None:
    now = time.time()
    expired = [k for k, (_, ts) in _export_temp_store.items() if now - ts > _EXPORT_TTL_SECONDS]
    for k in expired:
        p, _ = _export_temp_store.pop(k, ("", 0))
        Path(p).unlink(missing_ok=True)


def _resolve_delivery_file(delivery, upload_dir: Path) -> tuple[Path, bool]:
    """Resolve a MasterDelivery to a local file path.

    Returns (path, is_temp).  When ``is_temp`` is True the caller must
    delete the file after use.
    """
    if delivery.storage_backend == "r2":
        from app.services.r2 import download_to_temp
        return download_to_temp(delivery.file_path), True

    file_path = Path(delivery.file_path)
    if not file_path.is_absolute():
        file_path = upload_dir / file_path.name
    return file_path, False


def _sse_event(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/{album_id}/export/stream")
async def export_album_stream(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SSE endpoint that exports album tracks with metadata, streaming progress."""
    album = ensure_album_producer(album_id, current_user, db)

    completed_tracks = list(
        db.scalars(
            select(Track)
            .where(Track.album_id == album_id, Track.status == TrackStatus.COMPLETED)
            .order_by(Track.track_number.asc().nulls_last(), Track.id)
        ).all()
    )

    if not completed_tracks:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No completed tracks to export.",
        )

    # Pre-load album metadata needed inside the generator
    album_title = album.title
    album_circle_name = album.circle_name
    album_cover_image = album.cover_image
    album_release_date = album.release_date
    album_genres_raw = album.genres
    album_catalog_number = album.catalog_number

    # Parse genres (JSON array string)
    genres_str: str | None = None
    if album_genres_raw:
        try:
            genres_list = json.loads(album_genres_raw)
            if isinstance(genres_list, list) and genres_list:
                genres_str = "; ".join(str(g) for g in genres_list)
        except (json.JSONDecodeError, TypeError):
            pass

    release_date_str: str | None = None
    if album_release_date:
        release_date_str = str(album_release_date.year)

    # Pre-load cover image bytes
    cover_data: bytes | None = None
    if album_cover_image:
        cover_path = settings.get_upload_path() / album_cover_image
        if cover_path.exists():
            cover_data = cover_path.read_bytes()

    # Snapshot track data to avoid lazy-load issues inside the async generator
    track_snapshots: list[dict] = []
    for track in completed_tracks:
        delivery = current_master_delivery(track)
        if delivery is None:
            continue
        track_snapshots.append({
            "title": track.title,
            "artist": track.artist,
            "track_number": track.track_number,
            "duration": track.duration,
            "bpm": track.bpm,
            "delivery_file_path": delivery.file_path,
            "delivery_storage_backend": delivery.storage_backend,
        })

    total_tracks = len(track_snapshots)
    upload_dir = settings.get_upload_path()

    async def event_generator() -> AsyncGenerator[str, None]:
        from app.services.audio import embed_audio_metadata

        yield _sse_event({"type": "start", "total": total_tracks})

        _cleanup_expired_exports()

        manifest_entries: list[dict] = []
        temp_files: list[Path] = []
        # Collect (zip_name, local_path, is_temp) for building the ZIP at the end
        zip_entries: list[tuple[str, Path, bool]] = []

        try:
            for idx, snap in enumerate(track_snapshots):
                num = snap["track_number"] or 0
                safe_title = snap["title"].replace("/", "_").replace("\\", "_")
                safe_artist = snap["artist"].replace("/", "_").replace("\\", "_")

                # --- Step 1: resolve file ---
                step = "downloading" if snap["delivery_storage_backend"] == "r2" else "reading"
                yield _sse_event({
                    "type": "track_progress",
                    "index": idx + 1,
                    "total": total_tracks,
                    "title": snap["title"],
                    "step": step,
                })

                class _FakeDelivery:
                    file_path = snap["delivery_file_path"]
                    storage_backend = snap["delivery_storage_backend"]

                try:
                    src_path, is_temp_src = await asyncio.to_thread(
                        _resolve_delivery_file, _FakeDelivery(), upload_dir
                    )
                except Exception:
                    logger.warning("Failed to resolve file for track %s", snap["title"], exc_info=True)
                    yield _sse_event({
                        "type": "track_skipped",
                        "index": idx + 1,
                        "total": total_tracks,
                        "title": snap["title"],
                    })
                    continue

                if not src_path.exists():
                    yield _sse_event({
                        "type": "track_skipped",
                        "index": idx + 1,
                        "total": total_tracks,
                        "title": snap["title"],
                    })
                    continue

                # --- Step 2: copy to temp + embed metadata ---
                yield _sse_event({
                    "type": "track_progress",
                    "index": idx + 1,
                    "total": total_tracks,
                    "title": snap["title"],
                    "step": "metadata",
                })

                ext = src_path.suffix
                zip_name = f"{num:02d} - {safe_artist} - {safe_title}{ext}"

                if is_temp_src:
                    # Already a temp copy (R2) – embed in-place
                    work_path = src_path
                else:
                    # Local original – copy to temp to avoid mutating the source
                    import shutil
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                    tmp.close()
                    work_path = Path(tmp.name)
                    await asyncio.to_thread(shutil.copy2, str(src_path), str(work_path))
                    temp_files.append(work_path)

                await asyncio.to_thread(
                    embed_audio_metadata,
                    work_path,
                    title=snap["title"],
                    artist=snap["artist"],
                    album=album_title,
                    album_artist=album_circle_name,
                    track_number=snap["track_number"],
                    total_tracks=total_tracks,
                    genre=genres_str,
                    date=release_date_str,
                    cover_data=cover_data,
                )

                zip_entries.append((zip_name, work_path, is_temp_src or (work_path in temp_files)))
                manifest_entries.append({
                    "track_number": num,
                    "title": snap["title"],
                    "artist": snap["artist"],
                    "duration": snap["duration"],
                    "bpm": snap["bpm"],
                    "file": zip_name,
                })

                yield _sse_event({
                    "type": "track_done",
                    "index": idx + 1,
                    "total": total_tracks,
                    "title": snap["title"],
                })

            # --- Step 3: build ZIP ---
            yield _sse_event({"type": "zipping", "total": total_tracks})

            def _build_zip() -> str:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for zip_name, local_path, _ in zip_entries:
                        zf.write(str(local_path), zip_name)
                    manifest = json.dumps(
                        {"album": album_title, "tracks": manifest_entries},
                        indent=2, ensure_ascii=False,
                    )
                    zf.writestr("manifest.json", manifest)

                download_id = uuid.uuid4().hex
                safe_album = album_title.replace(" ", "_").replace("/", "_").replace("\\", "_")
                tmp_path = Path(tempfile.gettempdir()) / f"export_{download_id}_{safe_album}.zip"
                tmp_path.write_bytes(buf.getvalue())
                _export_temp_store[download_id] = (str(tmp_path), time.time())
                return download_id

            download_id = await asyncio.to_thread(_build_zip)

            yield _sse_event({
                "type": "complete",
                "download_id": download_id,
                "total": total_tracks,
                "processed": len(zip_entries),
            })
        except Exception:
            logger.error("Export stream error for album %s", album_id, exc_info=True)
            yield _sse_event({"type": "error", "message": "Export failed unexpectedly."})
        finally:
            # Clean up all temp working copies
            for _, local_path, is_temp in zip_entries:
                if is_temp:
                    Path(local_path).unlink(missing_ok=True)
            for p in temp_files:
                p.unlink(missing_ok=True)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{album_id}/export/download/{download_id}")
async def export_album_download(
    album_id: int,
    download_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download a completed export ZIP by its temporary download ID."""
    # Auth check – must be album producer
    ensure_album_producer(album_id, current_user, db)

    entry = _export_temp_store.get(download_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Export not found or expired.")

    file_path, _ = entry
    if not Path(file_path).exists():
        _export_temp_store.pop(download_id, None)
        raise HTTPException(status_code=404, detail="Export file not found.")

    album = db.get(Album, album_id)
    safe_title = (album.title if album else "album").replace(" ", "_").replace("/", "_").replace("\\", "_")

    async def _stream_and_cleanup():
        try:
            with open(file_path, "rb") as f:
                while chunk := f.read(64 * 1024):
                    yield chunk
        finally:
            _export_temp_store.pop(download_id, None)
            Path(file_path).unlink(missing_ok=True)

    return StreamingResponse(
        _stream_and_cleanup(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.zip"'},
    )


@router.post("/{album_id}/archive", response_model=AlbumRead)
def archive_album(
    album_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlbumRead:
    album = ensure_album_producer(album_id, current_user, db)
    if album.archived_at is not None:
        raise HTTPException(status_code=409, detail="Album is already archived.")
    album.archived_at = datetime.now(timezone.utc)
    member_ids = list(get_album_member_ids(db, album.id))
    notify(
        db,
        member_ids,
        "album_archived",
        "专辑已归档",
        f"「{album.title}」已被制作人归档",
        album_id=album.id,
        background_tasks=background_tasks,
    )
    db.commit()
    db.refresh(album)
    return _album_to_read(album, db)


@router.post("/{album_id}/restore", response_model=AlbumRead)
def restore_album(
    album_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlbumRead:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    if album.producer_id != current_user.id and not has_admin_role(current_user, "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the album producer can restore albums.",
        )
    if album.archived_at is None:
        raise HTTPException(status_code=409, detail="Album is not archived.")
    album.archived_at = None
    member_ids = list(get_album_member_ids(db, album.id))
    notify(
        db,
        member_ids,
        "album_restored",
        "专辑已恢复",
        f"「{album.title}」已被制作人恢复",
        album_id=album.id,
        background_tasks=background_tasks,
    )
    db.commit()
    db.refresh(album)
    return _album_to_read(album, db)
