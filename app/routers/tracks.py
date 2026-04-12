import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, func as sqlfunc, select, update
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.master_delivery import MasterDelivery
from app.models.reopen_request import ReopenRequest
from app.models.stage_assignment import StageAssignment
from app.models.track import RejectionMode, Track, TrackStatus, WorkflowVariant
from app.models.track_playback_preference import TrackPlaybackPreference
from app.models.track_source_version import TrackSourceVersion
from app.models.user import User
from app.schemas.schemas import (
    AssignReviewerRequest,
    ReassignReviewerRequest,
    TrackPlaybackPreferenceRead,
    TrackPlaybackPreferenceUpdate,
    ConfirmTrackUploadParams,
    ConfirmUploadParams,
    DirectReopenRequest,
    PresignedUploadResponse,
    ReopenDecisionRequest,
    ReopenRequestCreate,
    ReopenRequestRead,
    RequestTrackUploadParams,
    RequestUploadParams,
    SetPublicRequest,
    StageAssignmentRead,
    TrackDetailResponse,
    TrackListItem,
    TrackMetadataUpdate,
    TrackRead,
    WorkflowTransitionRequest,
)
from app.workflow_engine import (
    prepare_review_assignments_for_stage_entry,
    execute_transition as engine_execute_transition,
    execute_revision_upload as engine_revision_upload,
    execute_delivery_upload as engine_delivery_upload,
    get_initial_track_status,
    get_current_step as engine_get_current_step,
    parse_workflow_config as engine_parse_workflow_config,
    resolve_assignee as engine_resolve_assignee,
)
from app.notifications import notify
from app.realtime import broadcast_track_updated
from app.security import get_current_user, get_current_user_optional, get_user_from_token_param
from app.services.audio import extract_audio_metadata
from app.services.upload import stream_upload_sync
from app.workflow import (
    build_track_detail,
    build_track_read,
    current_master_delivery,
    current_source_version,
    ensure_album_visibility,
    ensure_track_visibility,
    get_all_album_member_ids,
    get_album_member_ids,
    log_track_event,
    should_anonymize_track,
)

router = APIRouter(prefix="/api/tracks", tags=["tracks"])

# Resolved once at startup to avoid a mkdir syscall on every file-serve request.
_UPLOAD_BASE = Path(settings.UPLOAD_DIR).resolve()


# Used only for truly immutable URLs (source-version snapshots addressed by numeric ID).
_AUDIO_CACHE_MAX_AGE = 86400


def _validate_playback_scope(scope: str) -> str:
    if scope not in {"source", "master"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playback preference scope not found.")
    return scope



_UNSAFE_CHARS = re.compile(r'[^\w\s\-]', re.UNICODE)
_COLLAPSE = re.compile(r'[\s_]+')


def sanitize_filename(name: str) -> str:
    """Return a filesystem-safe stem from an arbitrary Unicode string.

    Unsafe characters are replaced with underscores; consecutive underscores
    and whitespace are collapsed.  Truncated to 200 chars to leave room for
    a version suffix and file extension.
    """
    s = _UNSAFE_CHARS.sub('_', name)
    s = _COLLAPSE.sub('_', s)
    s = s.strip('_')
    return s[:200] or 'untitled'


ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".aac", ".m4a", ".wma"}


def _save_upload(file: UploadFile, stem: str | None = None) -> tuple[str, float | None]:
    from app.config import MAX_AUDIO_UPLOAD_SIZE

    ext = Path(file.filename).suffix.lower() if file.filename else ".bin"
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported audio format: {ext}. Allowed: {', '.join(sorted(ALLOWED_AUDIO_EXTENSIONS))}",
        )
    upload_dir = settings.get_upload_path()
    unique_name = f"{stem or uuid.uuid4().hex}{ext}"
    dest = upload_dir / unique_name
    stream_upload_sync(file, dest, MAX_AUDIO_UPLOAD_SIZE)
    meta = extract_audio_metadata(dest)
    return str(dest), meta.duration


def _source_version_create(track: Track, user: User, file_path: str, duration: float | None) -> TrackSourceVersion:
    return TrackSourceVersion(
        track_id=track.id,
        workflow_cycle=track.workflow_cycle,
        version_number=track.version,
        file_path=file_path,
        duration=duration,
        uploaded_by_id=user.id,
    )


def _handle_delivery_status(
    db: Session,
    album: Album,
    track: Track,
    current_user: User,
    delivery_number: int,
    background_tasks: BackgroundTasks,
) -> None:
    """Advance track status after a master delivery upload.

    When the current step has ``require_confirmation=True`` the track stays
    put until the mastering engineer explicitly confirms the delivery.
    Otherwise it advances via the workflow engine.
    """
    previous_status = track.status
    next_status = engine_delivery_upload(album, track)
    if next_status is None:
        log_track_event(
            db, track, current_user, "master_delivery_uploaded",
            from_status=previous_status, to_status=track.status,
            payload={"delivery_number": delivery_number, "awaiting_confirmation": True},
        )
        # Notify mastering engineer that delivery needs confirmation
        notify(db, [album.mastering_engineer_id], "delivery_awaiting_confirmation",
               "母带文件待确认",
               f"「{track.title}」的母带文件已上传，请确认后继续流程",
               related_track_id=track.id,
               background_tasks=background_tasks, album_id=track.album_id)
        return
    track.status = next_status
    log_track_event(
        db, track, current_user, "master_delivery_uploaded",
        from_status=previous_status, to_status=track.status,
        payload={"delivery_number": delivery_number},
    )
    notify(db, [album.producer_id, track.submitter_id], "track_status_changed", "母带文件已上传",
           f"「{track.title}」母带文件已上传，等待审核", related_track_id=track.id,
           background_tasks=background_tasks, album_id=track.album_id)


def _track_list_item(track: Track, user: User, album: Album, *, anonymize: bool = False) -> TrackListItem:
    return TrackListItem(**build_track_read(track, user, album, anonymize=anonymize).model_dump(), album_title=album.title)


def _ensure_revision_upload_permission(track: Track, album: Album, current_user: User) -> None:
    # Resubmit path: a finally-rejected-but-resubmittable track may always be
    # re-uploaded by its original submitter, regardless of the current step.
    if (
        track.status == TrackStatus.REJECTED
        and track.rejection_mode == RejectionMode.RESUBMITTABLE
    ):
        if track.submitter_id != current_user.id:
            raise HTTPException(status_code=403, detail="Only the submitter can resubmit this track.")
        return

    config = engine_parse_workflow_config(album)
    step = engine_get_current_step(config, track)
    if step is None or step.type != "revision":
        raise HTTPException(status_code=409, detail="This track is not waiting for a new source version.")
    assignee_id = step.assignee_user_id or engine_resolve_assignee(album, track, step.assignee_role)
    if assignee_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the assigned user can upload a new source version.")


def _ensure_delivery_upload_permission(track: Track, album: Album, current_user: User) -> None:
    config = engine_parse_workflow_config(album)
    step = engine_get_current_step(config, track)
    if step is None or step.type != "delivery":
        raise HTTPException(status_code=409, detail="Track is not in a delivery stage.")
    assignee_id = step.assignee_user_id or engine_resolve_assignee(album, track, step.assignee_role)
    if assignee_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the assigned user can upload a master delivery.")
    if step.assignee_role == "mastering_engineer" and album.mastering_engineer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the album mastering engineer can upload this delivery.")


def _ensure_delivery_confirm_permission(track: Track, album: Album, current_user: User) -> None:
    config = engine_parse_workflow_config(album)
    step = engine_get_current_step(config, track)
    if step is None or step.type != "delivery":
        raise HTTPException(status_code=409, detail="Track is not in a delivery stage.")
    assignee_id = step.assignee_user_id or engine_resolve_assignee(album, track, step.assignee_role)
    if assignee_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the assigned user can confirm delivery.")
    if step.assignee_role == "mastering_engineer" and album.mastering_engineer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the album mastering engineer can confirm delivery.")


def _serve_path(path_str: str, filename_prefix: str, *, immutable: bool = False) -> FileResponse:
    file_path = Path(path_str).resolve()
    try:
        file_path.relative_to(_UPLOAD_BASE)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio file missing from disk.")
    mime_map = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
    }
    media_type = mime_map.get(file_path.suffix.lower(), "audio/octet-stream")

    # Build a stable ETag from file path + size + mtime so browsers can cache.
    stat = file_path.stat()
    etag_raw = f"{file_path.name}-{stat.st_size}-{stat.st_mtime_ns}"
    etag = hashlib.md5(etag_raw.encode()).hexdigest()

    if immutable:
        cache_control = f"private, max-age={_AUDIO_CACHE_MAX_AGE}, immutable"
    else:
        cache_control = "private, max-age=0, must-revalidate"

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=f"{filename_prefix}{file_path.suffix}",
        headers={
            "Cache-Control": cache_control,
            "ETag": f'"{etag}"',
            "Accept-Ranges": "bytes",
        },
    )


def _serve_audio(
    file_path: str, storage_backend: str, filename_prefix: str, resolve: str | None = None,
    *, immutable: bool = False,
) -> FileResponse | RedirectResponse | dict:
    """Serve an audio file from local disk or redirect to R2 presigned URL.

    When ``resolve='json'`` and storage is R2, return a JSON dict with the
    presigned URL instead of a 307 redirect.  This lets the frontend obtain
    the direct R2 URL without hitting cross-origin redirect issues (e.g.
    wavesurfer.js ``fetch`` cannot follow a cross-origin 307).

    Set ``immutable=True`` only for versioned URLs whose content can never
    change (e.g. source-version snapshots addressed by their numeric ID).
    Mutable "current audio" endpoints must use the default ``immutable=False``
    so that browsers always revalidate after a new file is uploaded.
    """
    if storage_backend == "r2":
        from app.services.r2 import public_url

        url = public_url(file_path)
        if resolve == "json":
            return JSONResponse({"url": url})
        return RedirectResponse(url, status_code=302)
    if resolve == "json":
        return {"url": None}
    return _serve_path(file_path, filename_prefix, immutable=immutable)


@router.post("", response_model=TrackRead, status_code=status.HTTP_201_CREATED)
def create_track(
    title: str = Form(...),
    artist: str = Form(...),
    album_id: int = Form(...),
    bpm: str | None = Form(default=None),
    original_title: str | None = Form(default=None),
    original_artist: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)

    file_path, duration = _save_upload(file, f"{sanitize_filename(title)}_v1")
    max_num = db.scalar(
        select(sqlfunc.max(Track.track_number)).where(Track.album_id == album_id)
    ) or 0
    initial_status = get_initial_track_status(album)

    track = Track(
        title=title,
        artist=artist,
        album_id=album_id,
        submitter_id=current_user.id,
        bpm=bpm or None,
        original_title=original_title or None,
        original_artist=original_artist or None,
        track_number=max_num + 1,
        file_path=file_path,
        duration=duration,
        status=initial_status,
        version=1,
        workflow_cycle=1,
    )
    db.add(track)
    db.flush()
    db.add(_source_version_create(track, current_user, file_path, duration))
    log_track_event(db, track, current_user, "track_submitted", to_status=initial_status)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album, db=db)


# ── R2 presigned upload endpoints ────────────────────────────────────────────

def _validate_audio_extension(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported audio format: {ext}. Allowed: {', '.join(sorted(ALLOWED_AUDIO_EXTENSIONS))}",
        )
    return ext


def _validate_audio_size(file_size: int) -> None:
    from app.config import MAX_AUDIO_UPLOAD_SIZE

    if file_size > MAX_AUDIO_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {MAX_AUDIO_UPLOAD_SIZE // (1024 * 1024)} MB.",
        )


def _ensure_r2_enabled() -> None:
    if not settings.R2_ENABLED:
        raise HTTPException(status_code=501, detail="R2 storage is not enabled.")


def _presign_upload(object_key: str, content_type: str) -> PresignedUploadResponse:
    from app.services.r2 import generate_upload_url

    upload_url = generate_upload_url(object_key, content_type)
    return PresignedUploadResponse(
        upload_url=upload_url,
        object_key=object_key,
        upload_id=uuid.uuid4().hex,
        expires_in=settings.R2_PRESIGNED_UPLOAD_EXPIRY,
    )


def _extract_r2_metadata(object_key: str) -> tuple[float | None, int | None, int | None]:
    """Download R2 object to temp file, extract audio metadata, clean up."""
    from app.services.r2 import download_to_temp

    tmp_path = download_to_temp(object_key)
    try:
        meta = extract_audio_metadata(tmp_path)
        return meta.duration, meta.bitrate, meta.sample_rate
    finally:
        tmp_path.unlink(missing_ok=True)


def _verify_r2_object(object_key: str) -> None:
    from app.services.r2 import object_exists

    if not object_exists(object_key):
        raise HTTPException(status_code=400, detail="Upload not found in R2. The file may not have been uploaded yet.")


@router.post("/request-upload", response_model=PresignedUploadResponse)
def request_track_upload(
    params: RequestTrackUploadParams,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PresignedUploadResponse:
    _ensure_r2_enabled()
    _validate_audio_extension(params.filename)
    _validate_audio_size(params.file_size)

    album = db.get(Album, params.album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)

    from app.services.r2 import make_object_key

    # Use a temp ID of 0 — the real track_id is assigned on confirm
    object_key = make_object_key("tracks/new/source", current_user.id, params.filename)
    return _presign_upload(object_key, params.content_type)


@router.post("/confirm-upload", response_model=TrackRead)
def confirm_track_upload(
    params: ConfirmTrackUploadParams,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    _ensure_r2_enabled()

    album = db.get(Album, params.album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)

    duration, _bitrate, _sample_rate = _extract_r2_metadata(params.object_key)
    if duration is None and params.duration is not None:
        duration = params.duration

    initial_status = get_initial_track_status(album)

    max_num = db.scalar(
        select(sqlfunc.max(Track.track_number)).where(Track.album_id == params.album_id)
    ) or 0
    track = Track(
        title=params.title,
        artist=params.artist,
        album_id=params.album_id,
        submitter_id=current_user.id,
        bpm=params.bpm or None,
        original_title=params.original_title or None,
        original_artist=params.original_artist or None,
        track_number=max_num + 1,
        file_path=params.object_key,
        storage_backend="r2",
        duration=duration,
        status=initial_status,
        version=1,
        workflow_cycle=1,
    )
    db.add(track)
    db.flush()
    sv = TrackSourceVersion(
        track_id=track.id,
        workflow_cycle=track.workflow_cycle,
        version_number=track.version,
        file_path=params.object_key,
        storage_backend="r2",
        duration=duration,
        uploaded_by_id=current_user.id,
    )
    db.add(sv)
    log_track_event(db, track, current_user, "track_submitted", to_status=initial_status)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album, db=db)


@router.post("/{track_id}/source-versions/request-upload", response_model=PresignedUploadResponse)
def request_source_version_upload(
    track_id: int,
    params: RequestUploadParams,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PresignedUploadResponse:
    _ensure_r2_enabled()
    _validate_audio_extension(params.filename)
    _validate_audio_size(params.file_size)

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    _ensure_revision_upload_permission(track, album, current_user)

    from app.services.r2 import make_object_key

    object_key = make_object_key(f"tracks/{track_id}/source", track.version + 1, params.filename)
    return _presign_upload(object_key, params.content_type)


@router.post("/{track_id}/source-versions/confirm-upload", response_model=TrackRead)
def confirm_source_version_upload(
    track_id: int,
    params: ConfirmUploadParams,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    _ensure_r2_enabled()

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    _ensure_revision_upload_permission(track, album, current_user)

    # Resolve the next step *before* mutating rejection_mode so that the
    # engine can recognise a resubmit on a rejected+resubmittable track.
    next_status = engine_revision_upload(album, track)

    # Resubmit path: rejected+resubmittable tracks re-enter the workflow from
    # the first step with a fresh cycle and no reviewer assignment.
    if (
        track.status == TrackStatus.REJECTED
        and track.rejection_mode == RejectionMode.RESUBMITTABLE
    ):
        track.workflow_cycle += 1
        track.peer_reviewer_id = None
        track.rejection_mode = None
        track.workflow_variant = WorkflowVariant.STANDARD.value

    duration, _bitrate, _sample_rate = _extract_r2_metadata(params.object_key)
    if params.duration is not None and duration is None:
        duration = params.duration

    previous_status = track.status
    track.version += 1
    track.file_path = params.object_key
    track.storage_backend = "r2"
    track.duration = duration
    track.status = next_status
    prepare_review_assignments_for_stage_entry(
        db,
        album,
        track,
        next_status,
        background_tasks,
    )
    sv = TrackSourceVersion(
        track_id=track.id,
        workflow_cycle=track.workflow_cycle,
        version_number=track.version,
        file_path=params.object_key,
        storage_backend="r2",
        duration=duration,
        uploaded_by_id=current_user.id,
    )
    db.add(sv)
    log_track_event(
        db, track, current_user, "source_version_uploaded",
        from_status=previous_status, to_status=next_status,
        payload={"version": track.version, "workflow_cycle": track.workflow_cycle},
    )
    db.commit()
    db.refresh(track)
    broadcast_track_updated(background_tasks, track_id)
    return build_track_read(track, current_user, album)


@router.post("/{track_id}/master-deliveries/request-upload", response_model=PresignedUploadResponse)
def request_master_delivery_upload(
    track_id: int,
    params: RequestUploadParams,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PresignedUploadResponse:
    _ensure_r2_enabled()
    _validate_audio_extension(params.filename)
    _validate_audio_size(params.file_size)

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    _ensure_delivery_upload_permission(track, album, current_user)

    from app.services.r2 import make_object_key

    delivery_number = 1
    delivery = current_master_delivery(track)
    if delivery and delivery.workflow_cycle == track.workflow_cycle:
        delivery_number = delivery.delivery_number + 1
    object_key = make_object_key(f"tracks/{track_id}/master", delivery_number, params.filename)
    return _presign_upload(object_key, params.content_type)


@router.post("/{track_id}/master-deliveries/confirm-upload", response_model=TrackRead)
def confirm_master_delivery_upload(
    track_id: int,
    params: ConfirmUploadParams,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    _ensure_r2_enabled()
    _verify_r2_object(params.object_key)

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    _ensure_delivery_upload_permission(track, album, current_user)

    delivery_number = 1
    current_del = current_master_delivery(track)
    if current_del and current_del.workflow_cycle == track.workflow_cycle:
        delivery_number = current_del.delivery_number + 1
    delivery = MasterDelivery(
        track_id=track.id,
        workflow_cycle=track.workflow_cycle,
        delivery_number=delivery_number,
        file_path=params.object_key,
        storage_backend="r2",
        uploaded_by_id=current_user.id,
    )
    db.add(delivery)
    _handle_delivery_status(db, album, track, current_user, delivery_number, background_tasks)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album, db=db)


@router.post("/{track_id}/master-deliveries/{delivery_id}/confirm", response_model=TrackRead)
def confirm_delivery(
    track_id: int,
    delivery_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    """Mastering engineer confirms an uploaded delivery after previewing it."""
    from app.workflow_engine import execute_delivery_confirm

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)

    delivery = db.get(MasterDelivery, delivery_id)
    if delivery is None or delivery.track_id != track_id:
        raise HTTPException(status_code=404, detail="Delivery not found.")
    if delivery.confirmed_at is not None:
        raise HTTPException(status_code=409, detail="Delivery already confirmed.")
    _ensure_delivery_confirm_permission(track, album, current_user)

    delivery.confirmed_at = datetime.now(timezone.utc)

    previous_status = track.status
    next_status = execute_delivery_confirm(album, track)
    track.status = next_status

    log_track_event(
        db, track, current_user, "delivery_confirmed",
        from_status=previous_status, to_status=track.status,
        payload={"delivery_id": delivery.id, "delivery_number": delivery.delivery_number},
    )
    notify(db, [album.producer_id, track.submitter_id], "track_status_changed", "母带文件已确认",
           f"「{track.title}」母带文件已确认，等待审核", related_track_id=track.id,
           background_tasks=background_tasks, album_id=track.album_id)
    db.commit()
    db.refresh(track)
    broadcast_track_updated(background_tasks, track_id)
    return build_track_read(track, current_user, album, db=db)


@router.get("", response_model=list[TrackListItem])
def list_tracks(
    status_filter: str | None = Query(default=None, alias="status"),
    album_id: int | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TrackListItem]:
    if album_id is not None:
        # Single-album path: only load that album and its members
        album = db.get(Album, album_id)
        if album is None:
            return []
        members_by_album = get_all_album_member_ids(db, album_id=album_id)
        albums_by_id = {album.id: album}
        visible_album_ids = {
            album.id
            for album in albums_by_id.values()
            if current_user.id
            in ({album.producer_id, album.mastering_engineer_id} | members_by_album.get(album.id, set()))
        }
    else:
        albums = list(db.scalars(select(Album)).all())
        members_by_album = get_all_album_member_ids(db)
        visible_album_ids = {
            alb.id
            for alb in albums
            if current_user.id
            in ({alb.producer_id, alb.mastering_engineer_id} | members_by_album.get(alb.id, set()))
        }
        albums_by_id = {alb.id: alb for alb in albums}

    stmt = (
        select(Track)
        .where(Track.archived_at.is_(None), Track.status != TrackStatus.REJECTED)
        .order_by(Track.id)
        .limit(limit)
        .offset(offset)
    )
    if status_filter is not None:
        stmt = stmt.where(Track.status == status_filter)
    if album_id is not None:
        stmt = stmt.where(Track.album_id == album_id)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            Track.title.ilike(pattern)
            | Track.artist.ilike(pattern)
            | Track.original_title.ilike(pattern)
            | Track.original_artist.ilike(pattern)
        )
    tracks = list(db.scalars(stmt).all())

    results: list[TrackListItem] = []
    for track in tracks:
        alb = albums_by_id.get(track.album_id)
        if alb is None:
            continue
        is_privileged = current_user.id in (alb.producer_id, alb.mastering_engineer_id)
        is_submitter = track.submitter_id == current_user.id
        is_reviewer = track.peer_reviewer_id == current_user.id
        if track.album_id not in visible_album_ids:
            # Not a member of this album: only own tracks are accessible
            if not is_submitter and not is_reviewer:
                continue
        else:
            # Album member: privileged roles and direct participants see all tracks;
            # regular members only see completed or public tracks
            if not is_privileged and not is_submitter and not is_reviewer:
                if track.status != TrackStatus.COMPLETED and not track.is_public:
                    continue
        anonymize = should_anonymize_track(track, current_user, alb)
        results.append(_track_list_item(track, current_user, alb, anonymize=anonymize))
    return results


@router.get("/{track_id}", response_model=TrackDetailResponse)
def get_track(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackDetailResponse:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    return build_track_detail(track, current_user, db)


@router.get("/{track_id}/playback-preferences/{scope}", response_model=TrackPlaybackPreferenceRead)
def get_track_playback_preference(
    track_id: int,
    scope: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackPlaybackPreferenceRead:
    scope = _validate_playback_scope(scope)

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)

    preference = db.scalar(
        select(TrackPlaybackPreference).where(
            TrackPlaybackPreference.track_id == track_id,
            TrackPlaybackPreference.user_id == current_user.id,
            TrackPlaybackPreference.scope == scope,
        )
    )
    if preference is None:
        return TrackPlaybackPreferenceRead(
            track_id=track_id,
            user_id=current_user.id,
            scope=scope,
            gain_db=0.0,
            updated_at=None,
        )
    return TrackPlaybackPreferenceRead.model_validate(preference)


@router.put("/{track_id}/playback-preferences/{scope}", response_model=TrackPlaybackPreferenceRead)
def upsert_track_playback_preference(
    track_id: int,
    scope: str,
    payload: TrackPlaybackPreferenceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackPlaybackPreferenceRead:
    scope = _validate_playback_scope(scope)

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)

    preference = db.scalar(
        select(TrackPlaybackPreference).where(
            TrackPlaybackPreference.track_id == track_id,
            TrackPlaybackPreference.user_id == current_user.id,
            TrackPlaybackPreference.scope == scope,
        )
    )
    if preference is None:
        preference = TrackPlaybackPreference(
            track_id=track_id,
            user_id=current_user.id,
            scope=scope,
            gain_db=payload.gain_db,
        )
        db.add(preference)
    else:
        preference.gain_db = payload.gain_db

    db.commit()
    db.refresh(preference)
    return TrackPlaybackPreferenceRead.model_validate(preference)


@router.patch("/{track_id}/visibility", response_model=TrackRead)
def set_track_visibility(
    track_id: int,
    payload: SetPublicRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    """Toggle per-track public visibility. Only the album producer may call this."""
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    if album.producer_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the album producer can change track visibility.",
        )
    track.is_public = payload.is_public
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album, db=db)


@router.patch("/{track_id}/metadata", response_model=TrackRead)
def update_track_metadata(
    track_id: int,
    payload: TrackMetadataUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    """Update track metadata (title, artist, bpm, original_title, original_artist).

    Only the track submitter or the album producer may call this endpoint.
    """
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    if current_user.id not in {track.submitter_id, album.producer_id}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the track submitter or album producer can edit track metadata.",
        )

    changes: dict[str, dict[str, str | None]] = {}
    for field in ("title", "artist", "bpm", "original_title", "original_artist"):
        new_value = getattr(payload, field)
        if new_value is not None:
            old_value = getattr(track, field)
            if new_value != old_value:
                changes[field] = {"old": old_value, "new": new_value}
                setattr(track, field, new_value)

    if not changes:
        return build_track_read(track, current_user, album, db=db)

    log_track_event(
        db, track, current_user, "metadata_updated",
        payload={"changes": changes},
    )
    db.commit()
    db.refresh(track)
    broadcast_track_updated(background_tasks, track_id)
    return build_track_read(track, current_user, album, db=db)


@router.post("/{track_id}/workflow/transition", response_model=TrackRead)
def workflow_transition(
    track_id: int,
    payload: WorkflowTransitionRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    """Generic workflow transition driven by the album's workflow config."""
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    engine_execute_transition(db, album, track, current_user, payload.decision, background_tasks)
    db.commit()
    db.refresh(track)
    broadcast_track_updated(background_tasks, track_id)
    return build_track_read(track, current_user, album, db=db)


# ── Stage assignment endpoints ──────────────────────────────────────────────


@router.post("/{track_id}/assign-reviewer", response_model=list[StageAssignmentRead])
def assign_reviewer(
    track_id: int,
    payload: AssignReviewerRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[StageAssignmentRead]:
    """Producer manually assigns reviewers for a review stage."""
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if album.producer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the album producer can assign reviewers.")

    from app.workflow_engine import get_current_step, parse_workflow_config
    config = parse_workflow_config(album)
    step = get_current_step(config, track)
    if step is None or step.type != "review":
        raise HTTPException(status_code=409, detail="Track is not in a review stage.")

    if track.submitter_id in payload.user_ids:
        raise HTTPException(status_code=400, detail="Cannot assign the track author as reviewer.")

    valid_member_ids = get_album_member_ids(db, album.id)
    invalid_user_ids = [uid for uid in payload.user_ids if uid not in valid_member_ids]
    if invalid_user_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Users {sorted(invalid_user_ids)} are not members of this album.",
        )

    # Batch-fetch existing pending assignments to avoid N+1
    already_assigned = set(db.scalars(
        select(StageAssignment.user_id).where(
            StageAssignment.track_id == track_id,
            StageAssignment.stage_id == step.id,
            StageAssignment.user_id.in_(payload.user_ids),
            StageAssignment.status == "pending",
        )
    ).all())

    now = datetime.now(timezone.utc)
    created: list[StageAssignment] = []
    for uid in payload.user_ids:
        if uid in already_assigned:
            continue
        sa = StageAssignment(
            track_id=track_id,
            stage_id=step.id,
            user_id=uid,
            status="pending",
            cancellation_reason=None,
            assigned_at=now,
        )
        db.add(sa)
        created.append(sa)

    # Also set peer_reviewer_id for backward compat if not set
    if created and not track.peer_reviewer_id:
        track.peer_reviewer_id = created[0].user_id

    # Notify assigned reviewers
    for sa in created:
        notify(db, [sa.user_id], "reviewer_assigned", "你被指派为评审人",
               f"你已被指派评审「{track.title}」",
               related_track_id=track.id,
               album_id=track.album_id)

    db.commit()
    for sa in created:
        db.refresh(sa)
    return [StageAssignmentRead.model_validate(sa) for sa in created]


@router.get("/{track_id}/assignments", response_model=list[StageAssignmentRead])
def list_assignments(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[StageAssignmentRead]:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    assignments = db.scalars(
        select(StageAssignment)
        .where(StageAssignment.track_id == track_id)
        .options(selectinload(StageAssignment.user))
        .order_by(StageAssignment.assigned_at.desc())
    ).all()
    return [StageAssignmentRead.model_validate(a) for a in assignments]


@router.post("/{track_id}/reassign-reviewer", response_model=TrackRead)
def reassign_reviewer(
    track_id: int,
    payload: ReassignReviewerRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    """Producer reassigns the reviewer for the current review stage.

    Cancels all pending stage assignments, resets peer_reviewer_id, then
    either assigns the specified user (if payload.user_id is set) or
    re-runs the normal assignment logic for the current stage.
    """
    from app.workflow_engine import assign_peer_reviewer_for_step, get_current_step, parse_workflow_config

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if album.producer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the album producer can reassign reviewers.")

    config = parse_workflow_config(album)
    step = get_current_step(config, track)
    if step is None or step.type != "review":
        raise HTTPException(status_code=409, detail="Track is not in a review stage.")

    requested_user_ids: list[int] = []
    if payload.user_ids is not None:
        requested_user_ids = payload.user_ids
    elif payload.user_id is not None:
        requested_user_ids = [payload.user_id]

    deduped_user_ids: list[int] = []
    seen: set[int] = set()
    for uid in requested_user_ids:
        if uid in seen:
            continue
        seen.add(uid)
        deduped_user_ids.append(uid)

    if track.submitter_id in deduped_user_ids:
        raise HTTPException(status_code=400, detail="Cannot assign the track author as reviewer.")

    if deduped_user_ids:
        valid_member_ids = get_album_member_ids(db, album.id)
        invalid_ids = [uid for uid in deduped_user_ids if uid not in valid_member_ids]
        if invalid_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Users {sorted(invalid_ids)} are not members of this album.",
            )

    track.peer_reviewer_id = None

    if deduped_user_ids:
        # Cancel assignments for users NOT in the new list (any status).
        # Keep completed assignments for users who ARE in the new list.
        db.execute(
            update(StageAssignment)
            .where(
                StageAssignment.track_id == track_id,
                StageAssignment.stage_id == step.id,
                StageAssignment.status.in_(["pending", "completed"]),
                StageAssignment.user_id.not_in(deduped_user_ids),
            )
            .values(status="cancelled", cancellation_reason="reassigned")
        )
        # Cancel pending assignments for users who ARE in the new list
        # (they'll get a fresh pending, or keep completed).
        db.execute(
            update(StageAssignment)
            .where(
                StageAssignment.track_id == track_id,
                StageAssignment.stage_id == step.id,
                StageAssignment.status == "pending",
                StageAssignment.user_id.in_(deduped_user_ids),
            )
            .values(status="cancelled", cancellation_reason="reassigned")
        )

        now = datetime.now(timezone.utc)
        track.peer_reviewer_id = deduped_user_ids[0]
        already_completed = set(db.scalars(
            select(StageAssignment.user_id).where(
                StageAssignment.track_id == track_id,
                StageAssignment.stage_id == step.id,
                StageAssignment.status == "completed",
                StageAssignment.user_id.in_(deduped_user_ids),
            )
        ).all())
        newly_assigned: list[int] = []
        for uid in deduped_user_ids:
            if uid in already_completed:
                continue
            db.add(StageAssignment(
                track_id=track_id,
                stage_id=step.id,
                user_id=uid,
                status="pending",
                cancellation_reason=None,
                assigned_at=now,
            ))
            newly_assigned.append(uid)
        if newly_assigned:
            notify(db, newly_assigned, "reviewer_assigned", "你被指派为评审人",
                   f"你已被指派评审「{track.title}」",
                   related_track_id=track.id,
                   album_id=track.album_id)
    else:
        assign_peer_reviewer_for_step(db, album, track, step, background_tasks)

    log_track_event(
        db, track, current_user,
        "reviewer_reassigned",
        payload={"stage": step.id, "new_reviewer_ids": deduped_user_ids},
    )

    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album, db=db)


# ── Track reopen endpoints ──────────────────────────────────────────────────


@router.post("/{track_id}/reopen-request", response_model=ReopenRequestRead, status_code=201)
def create_reopen_request(
    track_id: int,
    payload: ReopenRequestCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReopenRequestRead:
    """Author requests to reopen a completed track (needs producer approval)."""
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if track.status != "completed":
        raise HTTPException(status_code=409, detail="Only completed tracks can be reopened.")
    if track.submitter_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the track author can request a reopen.")

    # Check for pending requests
    existing = db.scalar(
        select(ReopenRequest).where(
            ReopenRequest.track_id == track_id,
            ReopenRequest.status == "pending",
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail="A reopen request is already pending.")

    req = ReopenRequest(
        track_id=track_id,
        requested_by_id=current_user.id,
        target_stage_id=payload.target_stage_id,
        reason=payload.reason,
        status="pending",
    )
    db.add(req)
    notify(db, [album.producer_id], "reopen_request", "请求重新开启曲目",
           f"{current_user.display_name} 请求将「{track.title}」重新开启。",
           related_track_id=track.id, background_tasks=background_tasks, album_id=track.album_id)
    db.commit()
    db.refresh(req)
    return ReopenRequestRead.model_validate(req)


@router.post("/{track_id}/reopen", response_model=TrackRead)
def reopen_track(
    track_id: int,
    payload: DirectReopenRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    """Producer or mastering engineer directly reopens a completed track."""
    from app.workflow_engine import execute_reopen

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if track.status != "completed":
        raise HTTPException(status_code=409, detail="Only completed tracks can be reopened.")
    if current_user.id not in (album.producer_id, album.mastering_engineer_id):
        raise HTTPException(status_code=403, detail="Only the producer or mastering engineer can directly reopen.")

    execute_reopen(db, album, track, current_user, payload.target_stage_id, background_tasks)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album, db=db)


@router.post("/reopen-requests/{request_id}/decide", response_model=ReopenRequestRead)
def decide_reopen_request(
    request_id: int,
    payload: ReopenDecisionRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReopenRequestRead:
    """Producer approves or rejects a reopen request."""
    from app.workflow_engine import execute_reopen

    req = db.get(ReopenRequest, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Reopen request not found.")
    if req.status != "pending":
        raise HTTPException(status_code=409, detail="Request already decided.")

    track = db.get(Track, req.track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if album.producer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the album producer can decide reopen requests.")

    req.status = "approved" if payload.decision == "approve" else "rejected"
    req.decided_by_id = current_user.id
    req.decided_at = datetime.now(timezone.utc)

    if payload.decision == "approve":
        execute_reopen(db, album, track, current_user, req.target_stage_id, background_tasks)
        notify(db, [req.requested_by_id], "reopen_approved", "重新开启已批准",
               f"「{track.title}」的重新开启请求已获批准。",
               related_track_id=track.id, background_tasks=background_tasks, album_id=track.album_id)
    else:
        notify(db, [req.requested_by_id], "reopen_rejected", "重新开启已拒绝",
               f"「{track.title}」的重新开启请求已被拒绝。",
               related_track_id=track.id, background_tasks=background_tasks, album_id=track.album_id)

    db.commit()
    db.refresh(req)
    return ReopenRequestRead.model_validate(req)


@router.post("/{track_id}/source-versions", response_model=TrackRead)
def upload_source_version(
    track_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    _ensure_revision_upload_permission(track, album, current_user)

    # Resolve the next step *before* mutating rejection_mode so that the
    # engine can recognise a resubmit on a rejected+resubmittable track.
    next_status = engine_revision_upload(album, track)

    # Resubmit path: rejected+resubmittable tracks re-enter the workflow from
    # the first step with a fresh cycle and no reviewer assignment.
    if (
        track.status == TrackStatus.REJECTED
        and track.rejection_mode == RejectionMode.RESUBMITTABLE
    ):
        track.workflow_cycle += 1
        track.peer_reviewer_id = None
        track.rejection_mode = None
        track.workflow_variant = WorkflowVariant.STANDARD.value

    previous_status = track.status
    file_path, duration = _save_upload(file, f"{sanitize_filename(track.title)}_v{track.version + 1}")
    track.version += 1
    track.file_path = file_path
    track.storage_backend = "local"
    track.duration = duration
    track.status = next_status
    prepare_review_assignments_for_stage_entry(
        db,
        album,
        track,
        next_status,
        background_tasks,
    )
    source_version = _source_version_create(track, current_user, file_path, duration)
    db.add(source_version)
    log_track_event(
        db,
        track,
        current_user,
        "source_version_uploaded",
        from_status=previous_status,
        to_status=next_status,
        payload={"version": track.version, "workflow_cycle": track.workflow_cycle},
    )
    db.commit()
    db.refresh(track)
    broadcast_track_updated(background_tasks, track_id)
    return build_track_read(track, current_user, album)


@router.post("/{track_id}/master-deliveries", response_model=TrackRead)
def upload_master_delivery(
    track_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    _ensure_delivery_upload_permission(track, album, current_user)

    delivery_number = 1
    current_delivery = current_master_delivery(track)
    if current_delivery and current_delivery.workflow_cycle == track.workflow_cycle:
        delivery_number = current_delivery.delivery_number + 1
    file_path, _duration = _save_upload(file, f"{sanitize_filename(track.title)}_master_v{delivery_number}")
    delivery = MasterDelivery(
        track_id=track.id,
        workflow_cycle=track.workflow_cycle,
        delivery_number=delivery_number,
        file_path=file_path,
        uploaded_by_id=current_user.id,
    )
    db.add(delivery)
    _handle_delivery_status(db, album, track, current_user, delivery_number, background_tasks)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album, db=db)


@router.post("/{track_id}/final-review/approve", response_model=TrackRead)
def approve_final_review(
    track_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if track.status != TrackStatus.FINAL_REVIEW or current_user.id not in {album.producer_id, track.submitter_id}:
        raise HTTPException(status_code=403, detail="Only the producer or submitter can approve final review.")
    delivery = current_master_delivery(track)
    if delivery is None:
        raise HTTPException(status_code=409, detail="No master delivery available.")

    now = datetime.now(timezone.utc)
    is_producer = current_user.id == album.producer_id
    is_submitter = current_user.id == track.submitter_id

    if is_producer and is_submitter:
        delivery.producer_approved_at = delivery.producer_approved_at or now
        delivery.submitter_approved_at = delivery.submitter_approved_at or now
        event_type = "final_review_approved_by_producer"
    elif is_producer:
        delivery.producer_approved_at = delivery.producer_approved_at or now
        event_type = "final_review_approved_by_producer"
    else:
        delivery.submitter_approved_at = delivery.submitter_approved_at or now
        event_type = "final_review_approved_by_submitter"

    previous_status = track.status
    if delivery.producer_approved_at and delivery.submitter_approved_at:
        track.status = TrackStatus.COMPLETED
    log_track_event(
        db,
        track,
        current_user,
        event_type,
        from_status=previous_status,
        to_status=track.status,
        payload={"delivery_id": delivery.id},
    )
    if track.status == TrackStatus.COMPLETED:
        notify(db, [track.submitter_id, album.mastering_engineer_id], "track_status_changed",
               "曲目已完成！", f"「{track.title}」已完成所有审核流程！", related_track_id=track.id,
               background_tasks=background_tasks, album_id=track.album_id)
        # Schedule old source versions for cleanup
        expiry = now + timedelta(days=settings.OLD_VERSION_RETENTION_DAYS)
        for sv in track.source_versions:
            if sv.version_number != track.version:
                sv.expires_at = expiry
    db.commit()
    db.refresh(track)
    broadcast_track_updated(background_tasks, track_id)
    return build_track_read(track, current_user, album)


from app.services.cleanup import cleanup_files, collect_track_files


@router.post("/{track_id}/archive", response_model=TrackRead)
def archive_track(
    track_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if current_user.id != album.producer_id:
        raise HTTPException(status_code=403, detail="Only the album producer can archive tracks.")
    if track.archived_at is not None:
        raise HTTPException(status_code=409, detail="Track is already archived.")
    track.archived_at = datetime.now(timezone.utc)
    log_track_event(db, track, current_user, "track_archived", payload={"previous_status": track.status})
    notify(db, [track.submitter_id], "track_archived", "曲目已归档",
           f"「{track.title}」已被制作人归档", related_track_id=track.id,
           background_tasks=background_tasks, album_id=track.album_id)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album)


@router.post("/{track_id}/restore", response_model=TrackRead)
def restore_track(
    track_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if current_user.id != album.producer_id:
        raise HTTPException(status_code=403, detail="Only the album producer can restore tracks.")
    if track.archived_at is None:
        raise HTTPException(status_code=409, detail="Track is not archived.")
    track.archived_at = None
    log_track_event(db, track, current_user, "track_restored")
    notify(db, [track.submitter_id], "track_restored", "曲目已恢复",
           f"「{track.title}」已被制作人恢复", related_track_id=track.id,
           background_tasks=background_tasks, album_id=track.album_id)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album)


@router.delete("/{track_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_track(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if current_user.id not in {track.submitter_id, album.producer_id}:
        raise HTTPException(status_code=403, detail="Only the submitter or producer can delete this track.")
    # Collect all file paths before cascade-deleting DB records
    local_paths, r2_keys = collect_track_files(track)
    db.delete(track)
    db.commit()
    cleanup_files(local_paths, r2_keys)


def _resolve_audio_user(
    bearer_user: User | None,
    token_user: User | None,
) -> User:
    """Pick the authenticated user from either Bearer header or ?token= param."""
    user = bearer_user or token_user
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    return user


@router.get("/{track_id}/audio")
def serve_audio(
    track_id: int,
    resolve: str | None = Query(default=None),
    db: Session = Depends(get_db),
    bearer_user: User | None = Depends(get_current_user_optional),
    token_user: User | None = Depends(get_user_from_token_param),
):
    current_user = _resolve_audio_user(bearer_user, token_user)
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    if not track.file_path:
        raise HTTPException(status_code=404, detail="No source audio is available for this track.")
    # immutable=False: same URL serves different files as new versions are uploaded
    return _serve_audio(track.file_path, track.storage_backend, track.title, resolve, immutable=False)


@router.get("/{track_id}/source-versions/{version_id}/audio")
def get_source_version_audio(
    track_id: int,
    version_id: int,
    resolve: str | None = Query(default=None),
    db: Session = Depends(get_db),
    bearer_user: User | None = Depends(get_current_user_optional),
    token_user: User | None = Depends(get_user_from_token_param),
):
    current_user = _resolve_audio_user(bearer_user, token_user)
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)

    version = db.get(TrackSourceVersion, version_id)
    if version is None or version.track_id != track_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")

    # immutable=True: version_id in URL guarantees content never changes
    return _serve_audio(version.file_path, version.storage_backend, f"{track.title}-v{version.version_number}", resolve, immutable=True)


@router.get("/{track_id}/master-audio")
def serve_master_audio(
    track_id: int,
    resolve: str | None = Query(default=None),
    db: Session = Depends(get_db),
    bearer_user: User | None = Depends(get_current_user_optional),
    token_user: User | None = Depends(get_user_from_token_param),
):
    current_user = _resolve_audio_user(bearer_user, token_user)
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    delivery = current_master_delivery(track)
    if delivery is None:
        raise HTTPException(status_code=404, detail="No master delivery is available for this track.")
    # immutable=False: same URL serves new master deliveries across workflow cycles
    return _serve_audio(delivery.file_path, delivery.storage_backend, f"{track.title}-master", resolve, immutable=False)


@router.get("/{track_id}/master-deliveries/{delivery_id}/audio")
def get_master_delivery_audio(
    track_id: int,
    delivery_id: int,
    resolve: str | None = Query(default=None),
    db: Session = Depends(get_db),
    bearer_user: User | None = Depends(get_current_user_optional),
    token_user: User | None = Depends(get_user_from_token_param),
):
    current_user = _resolve_audio_user(bearer_user, token_user)
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)

    delivery = db.get(MasterDelivery, delivery_id)
    if delivery is None or delivery.track_id != track_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found.")

    filename = f"{track.title}-master-v{delivery.delivery_number}"
    if delivery.workflow_cycle != track.workflow_cycle:
        filename = f"{filename}-cycle-{delivery.workflow_cycle}"
    return _serve_audio(delivery.file_path, delivery.storage_backend, filename, resolve, immutable=True)
