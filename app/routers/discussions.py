import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import ALLOWED_AUDIO_TYPES, AUDIO_EXT_MAP, MAX_AUDIOS_PER_UPLOAD
from app.config import settings
from app.database import get_db
from app.models.album import Album
from app.models.discussion import TrackDiscussion, TrackDiscussionAudio, TrackDiscussionImage
from app.models.edit_history import EditHistory
from app.models.stage_assignment import StageAssignment
from app.models.track import Track
from app.models.user import User
from app.notifications import notify
from app.realtime import broadcast_discussion_event, broadcast_track_updated
from app.schemas.schemas import (
    DiscussionAudioRead,
    DiscussionImageRead,
    DiscussionRead,
    DiscussionUpdate,
    EditHistoryRead,
    PresignedCommentAudioResponse,
    PresignedUploadResponse,
    RequestCommentAudioUploadParams,
    UserRead,
)
from app.security import get_current_user, get_current_user_optional, get_user_from_token_param
from app.services.upload import stream_upload
from app.workflow import ensure_track_visibility, is_mastering_participant
from app.workflow import mask_user_read_if_needed, peer_identity_anonymize_user_ids_for_viewer
from app.workflow import discussion_audio_file_url

router = APIRouter(tags=["discussions"])
logger = logging.getLogger(__name__)


def _build_discussion_read(
    discussion: TrackDiscussion,
    anonymize_user_ids: set[int] | None = None,
) -> DiscussionRead:
    author = discussion.author
    images = [
        DiscussionImageRead(
            id=img.id,
            discussion_id=img.discussion_id,
            image_url=f"/uploads/{img.file_path}",
            created_at=img.created_at,
        )
        for img in discussion.images
    ]
    audios = [
        DiscussionAudioRead(
            id=a.id,
            discussion_id=a.discussion_id,
            audio_url=discussion_audio_file_url(a.id),
            original_filename=a.original_filename,
            duration=a.duration,
            created_at=a.created_at,
        )
        for a in discussion.audios
    ]
    return DiscussionRead(
        id=discussion.id,
        track_id=discussion.track_id,
        author_id=discussion.author_id,
        visibility=discussion.visibility,
        phase=discussion.phase,
        content=discussion.content,
        created_at=discussion.created_at,
        edited_at=discussion.edited_at,
        author=mask_user_read_if_needed(
            UserRead.model_validate(author) if author else None,
            anonymize_user_ids,
        ),
        images=images,
        audios=audios,
    )


def _resolve_discussion_user(
    bearer_user: User | None,
    token_user: User | None,
) -> User:
    user = bearer_user or token_user
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    return user


def _ensure_discussion_visible_to_user(
    discussion: TrackDiscussion,
    track: Track,
    user: User,
    db: Session,
) -> None:
    album = db.get(Album, track.album_id)
    if discussion.phase == "mastering":
        if album is None or not is_mastering_participant(user, track, album):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to mastering discussions.")
    if discussion.visibility == "internal" and user.id == track.submitter_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to internal discussions.")


def _verify_r2_audio_keys(object_keys: list[str], *, expected_prefix: str) -> None:
    from app.services.r2 import object_exists

    normalized_prefix = expected_prefix.strip("/") + "/"
    for key in object_keys:
        normalized_key = key.strip("/")
        if not normalized_key.startswith(normalized_prefix):
            raise HTTPException(status_code=400, detail=f"Upload key does not match the expected target: {key}")
        if not object_exists(normalized_key):
            raise HTTPException(status_code=400, detail=f"Upload not found in R2: {key}")


def _build_audio_response(audio: TrackDiscussionAudio, resolve: str | None):
    if audio.storage_backend == "r2":
        from app.services.r2 import public_url

        url = public_url(audio.file_path)
        if resolve == "json":
            return JSONResponse({"url": url})
        return RedirectResponse(url, status_code=302)

    if resolve == "json":
        return {"url": None}

    file_path = settings.get_upload_path() / audio.file_path
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
    return FileResponse(path=str(file_path), media_type=media_type, filename=audio.original_filename)


MAX_DISCUSSION_PAGE_SIZE = 50


@router.get(
    "/api/tracks/{track_id}/discussions",
    response_model=list[DiscussionRead],
)
def list_discussions(
    track_id: int,
    phase: Optional[str] = None,
    before_id: Optional[int] = Query(default=None, ge=1),
    limit: Optional[int] = Query(default=None, ge=1, le=MAX_DISCUSSION_PAGE_SIZE),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DiscussionRead]:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    album = db.get(Album, track.album_id)
    can_see_mastering = album is not None and is_mastering_participant(current_user, track, album)

    if phase == "mastering" and not can_see_mastering:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to mastering discussions.")

    stmt = (
        select(TrackDiscussion)
        .where(TrackDiscussion.track_id == track_id)
        .options(selectinload(TrackDiscussion.images), selectinload(TrackDiscussion.audios), selectinload(TrackDiscussion.author))
    )
    if phase is not None:
        stmt = stmt.where(TrackDiscussion.phase == phase)
    elif not can_see_mastering:
        stmt = stmt.where(TrackDiscussion.phase != "mastering")

    if limit is not None:
        # Cursor pagination: take newest-first slice, optionally older than before_id,
        # then reverse to ascending so the client renders oldest-at-top.
        stmt = stmt.order_by(TrackDiscussion.id.desc())
        if before_id is not None:
            stmt = stmt.where(TrackDiscussion.id < before_id)
        stmt = stmt.limit(limit)
        discussions = list(reversed(db.scalars(stmt).all()))
    else:
        stmt = stmt.order_by(TrackDiscussion.id.asc())
        discussions = list(db.scalars(stmt).all())

    anonymize_user_ids: set[int] = set()
    if album is not None:
        anonymize_user_ids = peer_identity_anonymize_user_ids_for_viewer(db, track, album, current_user)
    return [_build_discussion_read(d, anonymize_user_ids) for d in discussions]


@router.post("/api/tracks/{track_id}/discussions/request-audio-upload")
def request_discussion_audio_upload(
    track_id: int,
    params: RequestCommentAudioUploadParams,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PresignedCommentAudioResponse:
    from app.config import MAX_AUDIO_UPLOAD_SIZE
    from app.routers.tracks import ALLOWED_AUDIO_EXTENSIONS

    if not settings.R2_ENABLED:
        raise HTTPException(status_code=501, detail="R2 storage is not enabled.")

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if not is_mastering_participant(current_user, track, album):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to mastering discussions.")

    if len(params.files) > MAX_AUDIOS_PER_UPLOAD:
        raise HTTPException(status_code=422, detail=f"Maximum {MAX_AUDIOS_PER_UPLOAD} audio files per discussion.")

    from app.services.r2 import generate_upload_url, make_object_key

    uploads: list[PresignedUploadResponse] = []
    for file_params in params.files:
        ext = Path(file_params.filename).suffix.lower()
        if ext not in ALLOWED_AUDIO_EXTENSIONS:
            raise HTTPException(status_code=422, detail=f"Unsupported audio format: {ext}")
        if file_params.file_size > MAX_AUDIO_UPLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size is {MAX_AUDIO_UPLOAD_SIZE // (1024 * 1024)} MB.",
            )

        object_key = make_object_key(f"discussions/{track_id}", 0, file_params.filename)
        uploads.append(
            PresignedUploadResponse(
                upload_url=generate_upload_url(object_key, file_params.content_type),
                object_key=object_key,
                upload_id=uuid.uuid4().hex,
                expires_in=settings.R2_PRESIGNED_UPLOAD_EXPIRY,
            )
        )

    logger.info(
        "discussion_audio_request_upload track_id=%s user_id=%s file_count=%s",
        track_id,
        current_user.id,
        len(params.files),
    )
    return PresignedCommentAudioResponse(uploads=uploads)


@router.post(
    "/api/tracks/{track_id}/discussions",
    response_model=DiscussionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_discussion(
    track_id: int,
    background_tasks: BackgroundTasks,
    content: str = Form(default=""),
    phase: str = Form(default="general"),
    images: Optional[list[UploadFile]] = File(default=None),
    audios: Optional[list[UploadFile]] = File(default=None),
    audio_object_keys: Optional[str] = Form(default=None),
    audio_original_filenames: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DiscussionRead:
    r2_audio_keys: list[str] = []
    r2_audio_names: list[str] = []
    if audio_object_keys:
        r2_audio_keys = [key.strip() for key in audio_object_keys.split("\n") if key.strip()]
        r2_audio_names = [name.strip() for name in (audio_original_filenames or "").split("\n")]
        while len(r2_audio_names) < len(r2_audio_keys):
            r2_audio_names.append(Path(r2_audio_keys[len(r2_audio_names)]).name)

    if not content.strip() and not images and not audios and not r2_audio_keys:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Content or attachments required.")
    if phase not in ("general", "mastering"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid phase.")
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if phase == "mastering" and not is_mastering_participant(current_user, track, album):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to mastering discussions.")
    if r2_audio_keys and phase != "mastering":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Audio uploads are only allowed in mastering discussions.")
    if r2_audio_keys:
        _verify_r2_audio_keys(r2_audio_keys, expected_prefix=f"discussions/{track_id}/0")
    anonymize_user_ids = peer_identity_anonymize_user_ids_for_viewer(db, track, album, current_user)

    discussion = TrackDiscussion(
        track_id=track_id,
        author_id=current_user.id,
        phase=phase,
        content=content,
    )
    db.add(discussion)
    db.flush()

    if images:
        from app.config import MAX_IMAGE_UPLOAD_SIZE

        allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        upload_dir = settings.get_upload_path() / "discussion_images"
        upload_dir.mkdir(parents=True, exist_ok=True)
        for img_file in images:
            if not img_file.content_type or not img_file.content_type.startswith("image/"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"File must be an image, got: {img_file.content_type}",
                )
            ext = (Path(img_file.filename or "image.png").suffix or ".png").lower()
            if ext not in allowed_extensions:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unsupported image extension: {ext}",
                )
            filename = f"{uuid.uuid4().hex}{ext}"
            file_path = upload_dir / filename
            await stream_upload(img_file, file_path, MAX_IMAGE_UPLOAD_SIZE)
            db.add(
                TrackDiscussionImage(
                    discussion_id=discussion.id,
                    file_path=f"discussion_images/{filename}",
                )
            )

    if (audios or r2_audio_keys) and phase == "mastering":
        from app.config import MAX_AUDIO_UPLOAD_SIZE
        from app.services.audio import extract_audio_metadata

        audio_uploads = audios or []
        total_audio_count = len(audio_uploads) + len(r2_audio_keys)
        if total_audio_count > MAX_AUDIOS_PER_UPLOAD:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Maximum {MAX_AUDIOS_PER_UPLOAD} audio files per discussion.")
        audio_dir = settings.get_upload_path() / "discussion_audios"
        audio_dir.mkdir(parents=True, exist_ok=True)
        for audio_file in audio_uploads:
            if not audio_file.content_type or audio_file.content_type not in ALLOWED_AUDIO_TYPES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unsupported audio type: {audio_file.content_type}",
                )
            ext = AUDIO_EXT_MAP.get(audio_file.content_type, ".mp3")
            filename = f"{uuid.uuid4().hex}{ext}"
            dest = audio_dir / filename
            await stream_upload(audio_file, dest, MAX_AUDIO_UPLOAD_SIZE)
            duration = extract_audio_metadata(dest).duration
            db.add(
                TrackDiscussionAudio(
                    discussion_id=discussion.id,
                    file_path=f"discussion_audios/{filename}",
                    storage_backend="local",
                    original_filename=audio_file.filename or filename,
                    duration=duration,
                )
            )
        if r2_audio_keys:
            from app.services.r2 import download_to_temp

            for key, original_name in zip(r2_audio_keys, r2_audio_names):
                tmp_path = download_to_temp(key)
                try:
                    duration = extract_audio_metadata(tmp_path).duration
                finally:
                    tmp_path.unlink(missing_ok=True)
                db.add(
                    TrackDiscussionAudio(
                        discussion_id=discussion.id,
                        file_path=key,
                        storage_backend="r2",
                        original_filename=original_name,
                        duration=duration,
                    )
                )
    elif audios and phase != "mastering":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Audio uploads are only allowed in mastering discussions.")

    # Notify participants — mastering discussions only notify submitter, mastering engineer, and producer
    if phase == "mastering":
        participant_ids: set[int | None] = {track.submitter_id, album.producer_id}
        if album.mastering_engineer_id:
            participant_ids.add(album.mastering_engineer_id)
        notify_body = f"「{track.title}」有新的母带讨论"
    else:
        reviewer_ids = set(
            db.scalars(
                select(StageAssignment.user_id).where(
                    StageAssignment.track_id == track.id,
                    StageAssignment.status.in_(["pending", "completed"]),
                )
            ).all()
        )
        participant_ids = {track.submitter_id, track.peer_reviewer_id, album.producer_id, *reviewer_ids}
        if album.mastering_engineer_id:
            participant_ids.add(album.mastering_engineer_id)
        notify_body = f"「{track.title}」有新的讨论"
    participant_ids.discard(current_user.id)
    participant_ids.discard(None)
    notify(
        db,
        list(participant_ids),
        "new_discussion",
        "新讨论",
        notify_body,
        related_track_id=track.id,
        background_tasks=background_tasks,
        album_id=track.album_id,
    )
    logger.info(
        "discussion_created track_id=%s discussion_id=%s user_id=%s phase=%s image_count=%s audio_count=%s r2_audio_count=%s",
        track.id,
        discussion.id,
        current_user.id,
        phase,
        len(images or []),
        len(audios or []),
        len(r2_audio_keys),
    )

    db.commit()
    db.refresh(discussion)
    broadcast_track_updated(background_tasks, track.id)
    broadcast_discussion_event(background_tasks, track.id, "created", discussion.id)
    return _build_discussion_read(discussion, anonymize_user_ids)


@router.patch(
    "/api/discussions/{discussion_id}",
    response_model=DiscussionRead,
)
def update_discussion(
    discussion_id: int,
    payload: DiscussionUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DiscussionRead:
    discussion = db.get(TrackDiscussion, discussion_id)
    if discussion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Discussion not found.")
    track = db.get(Track, discussion.track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    album = db.get(Album, track.album_id)
    if discussion.phase == "mastering" and album is not None and not is_mastering_participant(current_user, track, album):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to mastering discussions.")
    anonymize_user_ids: set[int] = set()
    if album is not None:
        anonymize_user_ids = peer_identity_anonymize_user_ids_for_viewer(db, track, album, current_user)
    if discussion.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the author can edit this discussion.")

    if discussion.content != payload.content:
        db.add(EditHistory(
            entity_type="discussion",
            entity_id=discussion.id,
            old_content=discussion.content,
            edited_by_id=current_user.id,
        ))
        discussion.content = payload.content
        discussion.edited_at = datetime.now(timezone.utc)
        logger.info("discussion_updated discussion_id=%s user_id=%s", discussion.id, current_user.id)
    db.commit()
    db.refresh(discussion)
    broadcast_track_updated(background_tasks, discussion.track_id)
    broadcast_discussion_event(background_tasks, discussion.track_id, "updated", discussion.id)
    return _build_discussion_read(discussion, anonymize_user_ids)


@router.delete(
    "/api/discussions/{discussion_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_discussion(
    discussion_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    discussion = db.get(TrackDiscussion, discussion_id)
    if discussion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Discussion not found.")
    track = db.get(Track, discussion.track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    if discussion.phase == "mastering":
        album = db.get(Album, track.album_id)
        if album is not None and not is_mastering_participant(current_user, track, album):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to mastering discussions.")
    if discussion.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the author can delete this discussion.")

    track_id = discussion.track_id
    disc_id = discussion.id
    db.delete(discussion)
    logger.info("discussion_deleted discussion_id=%s track_id=%s user_id=%s", disc_id, track_id, current_user.id)
    db.commit()
    broadcast_track_updated(background_tasks, track_id)
    broadcast_discussion_event(background_tasks, track_id, "deleted", disc_id)


@router.get("/api/discussions/{discussion_id}/history", response_model=list[EditHistoryRead])
def get_discussion_history(
    discussion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[EditHistoryRead]:
    discussion = db.get(TrackDiscussion, discussion_id)
    if discussion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Discussion not found.")
    track = db.get(Track, discussion.track_id)
    if track:
        ensure_track_visibility(track, current_user, db)
        if discussion.phase == "mastering":
            album = db.get(Album, track.album_id)
            if album is not None and not is_mastering_participant(current_user, track, album):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to mastering discussions.")

    histories = list(db.scalars(
        select(EditHistory)
        .where(EditHistory.entity_type == "discussion", EditHistory.entity_id == discussion_id)
        .order_by(EditHistory.created_at.desc())
    ).all())
    return [EditHistoryRead.model_validate(h) for h in histories]


@router.get("/api/discussion-audios/{audio_id}/file")
def serve_discussion_audio(
    audio_id: int,
    resolve: str | None = Query(default=None),
    db: Session = Depends(get_db),
    bearer_user: User | None = Depends(get_current_user_optional),
    token_user: User | None = Depends(get_user_from_token_param),
):
    user = _resolve_discussion_user(bearer_user, token_user)

    audio = db.get(TrackDiscussionAudio, audio_id)
    if audio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Discussion audio not found.")

    discussion = db.get(TrackDiscussion, audio.discussion_id)
    track = db.get(Track, discussion.track_id) if discussion else None
    if discussion is None or track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Associated discussion not found.")

    ensure_track_visibility(track, user, db)
    _ensure_discussion_visible_to_user(discussion, track, user, db)
    return _build_audio_response(audio, resolve)
