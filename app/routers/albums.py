import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func as sqlfunc, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.circle import CircleMember
from app.models.issue import Issue, IssueStatus
from app.models.track import Track, TrackStatus
from app.models.user import User
from app.models.workflow_event import WorkflowEvent
from app.schemas.schemas import AlbumCreate, AlbumDeadlineUpdate, AlbumMetadataUpdate, AlbumRead, AlbumStats, AlbumTeamUpdate, TrackOrderUpdate, TrackRead, UserRead, WebhookConfig
from app.security import get_current_user
from app.services.upload import stream_upload
from app.services.webhook import build_webhook_payload, post_webhook
from app.workflow import build_event_read, build_track_read, current_master_delivery, ensure_album_producer, ensure_album_visibility, get_album_member_ids, get_all_album_member_ids

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
        created_at=album.created_at,
        updated_at=album.updated_at,
        track_count=len(album.tracks),
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
    current_user: User = Depends(get_current_user),
) -> AlbumRead:
    album_data = payload.model_dump()
    genres = album_data.pop("genres", None)
    album = Album(**album_data, producer_id=current_user.id)
    if genres:
        album.genres = json.dumps(genres, ensure_ascii=False)
    db.add(album)
    db.flush()
    db.add(AlbumMember(album_id=album.id, user_id=current_user.id))
    db.commit()
    db.refresh(album)
    return _album_to_read(album, db)


@router.get("", response_model=list[AlbumRead])
def list_albums(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AlbumRead]:
    albums = list(db.scalars(select(Album).order_by(Album.id)).all())
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

    tracks = list(db.scalars(select(Track).where(Track.album_id == album_id)).all())

    by_status: dict[str, int] = {}
    for track in tracks:
        key = track.status.value
        by_status[key] = by_status.get(key, 0) + 1

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
                if track.status.value in statuses and phase_key in deadlines:
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
        total_tracks=len(tracks),
        by_status=by_status,
        open_issues=open_issues,
        recent_events=[build_event_read(e, db, users_cache=actors_by_id) for e in recent_events],
        deadline=album.deadline,
        overdue_track_count=overdue_count,
    )


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

    tracks = list(db.scalars(
        select(Track).where(Track.album_id == album_id)
        .order_by(Track.track_number.asc().nulls_last(), Track.id)
    ).all())
    return [build_track_read(track, current_user, album) for track in tracks]


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
    return [build_track_read(t, current_user, album) for t in ordered]


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
    success = await post_webhook(config["url"], payload)
    return {"success": success}


@router.get("/{album_id}/export")
def export_album(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
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

    upload_dir = settings.get_upload_path()
    buffer = io.BytesIO()
    manifest_entries: list[dict] = []

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for track in completed_tracks:
            delivery = current_master_delivery(track)
            if delivery is None:
                continue
            file_path = Path(delivery.file_path)
            if not file_path.is_absolute():
                file_path = upload_dir / file_path.name
            if not file_path.exists():
                continue
            ext = file_path.suffix
            num = track.track_number or 0
            safe_title = track.title.replace("/", "_").replace("\\", "_")
            safe_artist = track.artist.replace("/", "_").replace("\\", "_")
            zip_name = f"{num:02d} - {safe_artist} - {safe_title}{ext}"
            zf.write(str(file_path), zip_name)
            manifest_entries.append(
                {
                    "track_number": num,
                    "title": track.title,
                    "artist": track.artist,
                    "duration": track.duration,
                    "bpm": track.bpm,
                    "file": zip_name,
                }
            )

        manifest = json.dumps(
            {"album": album.title, "tracks": manifest_entries},
            indent=2,
            ensure_ascii=False,
        )
        zf.writestr("manifest.json", manifest)

    buffer.seek(0)
    safe_album_title = album.title.replace(" ", "_").replace("/", "_").replace("\\", "_")
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_album_title}.zip"'},
    )
