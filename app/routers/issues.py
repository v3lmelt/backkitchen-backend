import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.config import settings
from app.database import get_db
from app.models.album import Album
from app.models.comment import Comment
from app.models.comment_audio import CommentAudio
from app.models.edit_history import EditHistory
from app.models.comment_image import CommentImage
from app.models.issue_audio import IssueAudio
from app.models.issue_image import IssueImage
from app.services.audio import extract_audio_metadata
from app.models.issue import Issue, IssueMarker, IssuePhase, IssueStatus, MarkerType
from app.models.stage_assignment import StageAssignment
from app.models.track import Track
from app.models.user import User
from app.notifications import notify
from app.schemas.schemas import (
    CommentRead, CommentUpdate, EditHistoryRead, IssueBatchUpdate, IssueCreate, IssueDetail, IssueRead,
    PresignedCommentAudioResponse, PresignedUploadResponse, RequestCommentAudioUploadParams,
)
from app.security import get_current_user, get_current_user_optional, get_user_from_token_param
from app.services.upload import stream_upload
from app.workflow import (
    build_comment_read,
    build_issue_detail,
    build_issue_read,
    current_master_delivery,
    current_source_version,
    ensure_track_visibility,
    log_track_event,
)
from app.workflow_engine import (
    get_current_step,
    infer_issue_phase_for_step,
    parse_workflow_config,
    user_matches_role_or_assignment,
)

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
IMAGE_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

ALLOWED_AUDIO_TYPES = {"audio/mpeg", "audio/wav", "audio/flac", "audio/aac", "audio/ogg", "audio/x-flac", "audio/x-wav"}
AUDIO_EXT_MAP = {
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
}
MAX_AUDIOS_PER_COMMENT = 3
MAX_AUDIOS_PER_ISSUE = 3
MAX_IMAGES_PER_ISSUE = 3

router = APIRouter(tags=["issues"])


def _album_for_track(track: Track, db: Session) -> Album:
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    return album


def _match_custom_step_phase(track: Track, album: Album, phase: str):
    """Return current custom workflow step if issue phase is valid.

    ``phase`` must match either the canonical phase mapped from step metadata
    or the step id itself.
    """
    config = parse_workflow_config(album)
    step = get_current_step(config, track)
    if step is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Track is in unknown workflow step.")

    expected_phase = infer_issue_phase_for_step(step)
    if phase not in {expected_phase, step.id}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Issue phase must match current workflow step ('{expected_phase}' or '{step.id}').",
        )
    return step


def _ensure_custom_issue_permission(track: Track, album: Album, user: User, phase: str, db: Session) -> str:
    step = _match_custom_step_phase(track, album, phase)

    if not user_matches_role_or_assignment(user, album, track, step, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assigned user can create issues for this step.",
        )

    return infer_issue_phase_for_step(step)


def _resolve_step_assignee_id(step, track: Track, album: Album) -> int | None:
    if step.assignee_user_id:
        return step.assignee_user_id
    if step.assignee_role == "producer":
        return album.producer_id
    if step.assignee_role == "mastering_engineer":
        return album.mastering_engineer_id
    if step.assignee_role == "submitter":
        return track.submitter_id
    if step.assignee_role == "peer_reviewer":
        return track.peer_reviewer_id
    if step.assignee_role.startswith("member:"):
        try:
            return int(step.assignee_role.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
    return None


def _effective_issue_phase(issue: Issue, track: Track, album: Album) -> str:
    """Normalize canonical issue phases to the active workflow step."""
    phase = issue.phase
    if phase in {IssuePhase.PEER.value, IssuePhase.PRODUCER.value, IssuePhase.MASTERING.value, IssuePhase.FINAL_REVIEW.value}:
        config = parse_workflow_config(album)
        step = get_current_step(config, track)
        return infer_issue_phase_for_step(step) if step else phase
    return phase


def _phase_reviewer_id(issue: Issue, track: Track, album: Album) -> int | None:
    """Return a fallback reviewer user-id for the issue's phase."""
    phase = _effective_issue_phase(issue, track, album)

    config = parse_workflow_config(album)
    step = get_current_step(config, track)
    if step and infer_issue_phase_for_step(step) == phase:
        return _resolve_step_assignee_id(step, track, album)

    if phase == IssuePhase.PEER:
        return track.peer_reviewer_id
    if phase in (IssuePhase.PRODUCER, IssuePhase.FINAL_REVIEW):
        return album.producer_id
    if phase == IssuePhase.MASTERING:
        return album.mastering_engineer_id
    return None


def _phase_reviewer_ids(issue: Issue, track: Track, album: Album, db: Session) -> set[int]:
    """Return all reviewer user IDs for the issue's phase.

    For active review steps with StageAssignment records, include every assigned
    reviewer (pending/completed). Otherwise fall back to role-based resolution.
    """
    phase = _effective_issue_phase(issue, track, album)
    config = parse_workflow_config(album)
    step = get_current_step(config, track)
    if step and infer_issue_phase_for_step(step) == phase and step.type == "review":
        reviewer_ids = set(
            db.scalars(
                select(StageAssignment.user_id).where(
                    StageAssignment.track_id == track.id,
                    StageAssignment.stage_id == step.id,
                    StageAssignment.status.in_(["pending", "completed"]),
                )
            ).all()
        )
        if reviewer_ids:
            return reviewer_ids

    fallback = _phase_reviewer_id(issue, track, album)
    return {fallback} if fallback is not None else set()


def _ensure_issue_visible_to_user(issue: Issue, track: Track, user: User) -> None:
    # Pending-discussion issues are hidden from submitters until discussion ends.
    if user.id == track.submitter_id and issue.status == IssueStatus.PENDING_DISCUSSION:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found.")


def _ensure_issue_update_permission(issue: Issue, track: Track, album: Album, user: User, db: Session) -> None:
    reviewer_ids = _phase_reviewer_ids(issue, track, album, db)
    allowed = {track.submitter_id, issue.author_id} | reviewer_ids
    allowed.discard(None)
    if user.id not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update this issue.")


def _validate_status_transition(
    issue: Issue,
    new_status: IssueStatus,
    track: Track,
    album: Album,
    user: User,
    db: Session,
) -> None:
    """Enforce role-based status transition rules.

    Submitter may:  open → resolved|disagreed, disagreed → resolved
    Reviewer may:   open → resolved|pending_discussion
                   pending_discussion → open|resolved
                   resolved|disagreed → open
                   disagreed → resolved|pending_discussion
    """
    old = issue.status
    if old == new_status:
        return

    is_submitter = user.id == track.submitter_id
    reviewer_ids = _phase_reviewer_ids(issue, track, album, db)
    is_reviewer = user.id in reviewer_ids or user.id == issue.author_id

    # Submitter actions
    if is_submitter and old == IssueStatus.OPEN and new_status in (IssueStatus.RESOLVED, IssueStatus.DISAGREED):
        return
    if is_submitter and old == IssueStatus.DISAGREED and new_status == IssueStatus.RESOLVED:
        return

    # Reviewer actions
    if is_reviewer and old == IssueStatus.OPEN and new_status in (IssueStatus.RESOLVED, IssueStatus.PENDING_DISCUSSION):
        return
    # Only the issue creator can publish an internal issue (pending_discussion → open)
    is_creator = user.id == issue.author_id
    if is_creator and old == IssueStatus.PENDING_DISCUSSION and new_status in (IssueStatus.OPEN, IssueStatus.RESOLVED):
        return
    if is_reviewer and old == IssueStatus.PENDING_DISCUSSION and new_status == IssueStatus.RESOLVED:
        return
    if is_reviewer and old in (IssueStatus.RESOLVED, IssueStatus.DISAGREED) and new_status == IssueStatus.OPEN:
        return
    if is_reviewer and old == IssueStatus.DISAGREED and new_status in (IssueStatus.RESOLVED, IssueStatus.PENDING_DISCUSSION):
        return

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot perform this status transition.")


@router.post(
    "/api/tracks/{track_id}/issues",
    response_model=IssueRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_issue(
    track_id: int,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> IssueRead:
    import json as _json

    from app.schemas.schemas import IssueMarkerCreate
    from app.models.issue import IssueSeverity as _IssueSeverity

    issue_title: str
    issue_description: str
    issue_phase: str
    severity_enum: _IssueSeverity
    parsed_markers: list[IssueMarkerCreate]
    issue_visibility: str = "public"
    audios: list[StarletteUploadFile] = []
    images: list[StarletteUploadFile] = []
    audio_object_keys: str | None = None
    audio_original_filenames: str | None = None

    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("application/json"):
        try:
            payload = IssueCreate.model_validate(await request.json())
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=jsonable_encoder(exc.errors()),
            )
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid request body.",
            )

        issue_title = payload.title
        issue_description = payload.description
        issue_phase = payload.phase
        severity_enum = payload.severity
        parsed_markers = payload.markers
        issue_visibility = payload.visibility
    else:
        form = await request.form()
        title = str(form.get("title") or "").strip()
        description = str(form.get("description") or "").strip()
        phase = str(form.get("phase") or "").strip()
        severity = str(form.get("severity") or "major").strip() or "major"
        visibility = str(form.get("visibility") or "public").strip() or "public"
        markers_json = str(form.get("markers_json") or "[]")

        if not title or not description or not phase:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="title, description and phase are required.",
            )
        issue_title = title
        issue_description = description
        issue_phase = phase
        issue_visibility = visibility

        audios = [item for item in form.getlist("audios") if isinstance(item, StarletteUploadFile)]
        images = [item for item in form.getlist("images") if isinstance(item, StarletteUploadFile)]
        raw_audio_keys = form.get("audio_object_keys")
        raw_audio_names = form.get("audio_original_filenames")
        audio_object_keys = str(raw_audio_keys) if raw_audio_keys is not None else None
        audio_original_filenames = str(raw_audio_names) if raw_audio_names is not None else None

        try:
            markers_raw = _json.loads(markers_json)
        except (ValueError, TypeError):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid markers JSON.")

        if not isinstance(markers_raw, list):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="markers must be a JSON array.")

        parsed_markers = []
        for m in markers_raw:
            try:
                parsed_markers.append(IssueMarkerCreate(**m))
            except Exception:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid marker data.")

        try:
            severity_enum = _IssueSeverity(severity)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid severity: {severity}")

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    effective_phase = _ensure_custom_issue_permission(
        track, album, current_user, issue_phase, db,
    )
    initial_status = IssueStatus.PENDING_DISCUSSION if issue_visibility == "internal" else IssueStatus.OPEN

    for m in parsed_markers:
        if m.marker_type == MarkerType.RANGE and m.time_end is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="time_end is required for range markers.",
            )
        if m.time_end is not None and m.time_end <= m.time_start:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="time_end must be greater than time_start.",
            )

    # Validate audio files
    if len(audios) > MAX_AUDIOS_PER_ISSUE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"An issue may contain at most {MAX_AUDIOS_PER_ISSUE} audio files.",
        )
    for audio_file in audios:
        if audio_file.content_type not in ALLOWED_AUDIO_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported audio type: {audio_file.content_type}. Allowed: mp3, wav, flac, aac, ogg.",
            )

    # Validate image files
    if len(images) > MAX_IMAGES_PER_ISSUE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"An issue may contain at most {MAX_IMAGES_PER_ISSUE} image files.",
        )
    for img_file in images:
        if img_file.content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported image type: {img_file.content_type}. Allowed: jpeg, png, gif, webp.",
            )

    # Parse R2 audio keys if provided
    r2_audio_keys: list[str] = []
    r2_audio_names: list[str] = []
    if audio_object_keys:
        r2_audio_keys = [k.strip() for k in audio_object_keys.split("\n") if k.strip()]
        r2_audio_names = [n.strip() for n in (audio_original_filenames or "").split("\n")]
        while len(r2_audio_names) < len(r2_audio_keys):
            r2_audio_names.append(Path(r2_audio_keys[len(r2_audio_names)]).name)

    source_version_id = None
    _master_delivery_id = None
    if effective_phase in {IssuePhase.PEER, IssuePhase.PRODUCER, IssuePhase.MASTERING}:
        source_version = current_source_version(track)
        if source_version is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No source version available.")
        source_version_id = source_version.id
    if effective_phase == IssuePhase.FINAL_REVIEW:
        delivery = current_master_delivery(track)
        if delivery is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No master delivery available.")
        _master_delivery_id = delivery.id

    issue = Issue(
        track_id=track_id,
        author_id=current_user.id,
        phase=effective_phase,
        workflow_cycle=track.workflow_cycle,
        source_version_id=source_version_id,
        master_delivery_id=_master_delivery_id,
        title=issue_title,
        description=issue_description,
        severity=severity_enum,
        status=initial_status,
        markers=[
            IssueMarker(
                marker_type=m.marker_type,
                time_start=m.time_start,
                time_end=m.time_end,
            )
            for m in parsed_markers
        ],
    )
    db.add(issue)
    db.flush()  # assign issue.id before logging

    # Handle direct audio uploads
    if audios:
        from app.config import MAX_AUDIO_UPLOAD_SIZE

        issue_audios_dir = settings.get_upload_path() / "issue_audios"
        issue_audios_dir.mkdir(parents=True, exist_ok=True)
        for audio_file in audios:
            ext = AUDIO_EXT_MAP.get(audio_file.content_type or "", ".mp3")
            filename = f"{uuid.uuid4()}{ext}"
            file_path = f"issue_audios/{filename}"
            dest = issue_audios_dir / filename
            await stream_upload(audio_file, dest, MAX_AUDIO_UPLOAD_SIZE)
            duration = extract_audio_metadata(dest).duration
            original_filename = audio_file.filename or filename
            db.add(IssueAudio(
                issue_id=issue.id,
                file_path=file_path,
                original_filename=original_filename,
                duration=duration,
            ))

    # Handle R2 audio attachments
    if r2_audio_keys:
        from botocore.exceptions import ClientError
        from app.services.r2 import download_to_temp

        for key, orig_name in zip(r2_audio_keys, r2_audio_names):
            try:
                tmp = download_to_temp(key)
            except ClientError:
                raise HTTPException(status_code=400, detail=f"R2 object not found: {key}")
            try:
                duration = extract_audio_metadata(tmp).duration
            finally:
                tmp.unlink(missing_ok=True)
            db.add(IssueAudio(
                issue_id=issue.id,
                file_path=key,
                storage_backend="r2",
                original_filename=orig_name,
                duration=duration,
            ))

    # Handle image uploads
    if images:
        from app.config import MAX_IMAGE_UPLOAD_SIZE

        issue_images_dir = settings.get_upload_path() / "issue_images"
        issue_images_dir.mkdir(parents=True, exist_ok=True)
        for img_file in images:
            ext = IMAGE_EXT_MAP.get(img_file.content_type or "", ".jpg")
            filename = f"{uuid.uuid4()}{ext}"
            file_path = f"issue_images/{filename}"
            dest = issue_images_dir / filename
            await stream_upload(img_file, dest, MAX_IMAGE_UPLOAD_SIZE)
            db.add(IssueImage(issue_id=issue.id, file_path=file_path))

    log_track_event(
        db,
        track,
        current_user,
        "issue_created",
        payload={"phase": effective_phase, "title": issue_title, "issue_id": issue.id},
    )
    if current_user.id != track.submitter_id and issue.status != IssueStatus.PENDING_DISCUSSION:
        notify(db, [track.submitter_id], "new_issue", f"新问题：{issue.title}",
               f"「{track.title}」上有新的审核问题",
               related_track_id=track.id, related_issue_id=issue.id,
               background_tasks=background_tasks, album_id=track.album_id)
    db.commit()
    db.refresh(issue)
    return build_issue_read(issue, db)


@router.get("/api/tracks/{track_id}/issues", response_model=list[IssueRead])
def list_issues(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[IssueRead]:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)

    issues = list(db.scalars(
        select(Issue)
        .where(Issue.track_id == track_id)
        .order_by(Issue.created_at)
        .options(selectinload(Issue.markers), selectinload(Issue.audios), selectinload(Issue.images))
    ).all())
    if current_user.id == track.submitter_id:
        issues = [issue for issue in issues if issue.status != IssueStatus.PENDING_DISCUSSION]
    # Pre-fetch all issue authors
    author_ids = {i.author_id for i in issues}
    users_cache = {u.id: u for u in db.scalars(select(User).where(User.id.in_(author_ids))).all()} if author_ids else {}
    return [build_issue_read(issue, db, users_cache=users_cache) for issue in issues]


@router.get("/api/issues/{issue_id}", response_model=IssueDetail)
def get_issue(
    issue_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> IssueDetail:
    issue = db.scalar(
        select(Issue)
        .where(Issue.id == issue_id)
        .options(selectinload(Issue.markers), selectinload(Issue.audios), selectinload(Issue.images))
    )
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found.")
    track = db.get(Track, issue.track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    _ensure_issue_visible_to_user(issue, track, current_user)
    return build_issue_detail(issue, db)


@router.patch("/api/issues/{issue_id}", response_model=IssueDetail)
async def update_issue(
    issue_id: int,
    background_tasks: BackgroundTasks,
    status_field: Optional[str] = Form(default=None, alias="status"),
    title: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    severity: Optional[str] = Form(default=None),
    status_note: Optional[str] = Form(default=None),
    images: list[UploadFile] = File(default=[]),
    audios: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> IssueDetail:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found.")
    track = db.get(Track, issue.track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = _album_for_track(track, db)
    ensure_track_visibility(track, current_user, db)
    _ensure_issue_visible_to_user(issue, track, current_user)
    _ensure_issue_update_permission(issue, track, album, current_user, db)

    # Filter out sentinel empty UploadFile entries (no real file selected)
    images = [f for f in images if f.filename]
    audios = [f for f in audios if f.filename]

    # Validate uploaded files
    for img_file in images:
        if img_file.content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported image type: {img_file.content_type}.",
            )
    if len(audios) > MAX_AUDIOS_PER_COMMENT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"A status note may contain at most {MAX_AUDIOS_PER_COMMENT} audio files.",
        )
    for audio_file in audios:
        if audio_file.content_type not in ALLOWED_AUDIO_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported audio type: {audio_file.content_type}.",
            )

    # Parse and validate enum fields
    new_status_enum: Optional[IssueStatus] = None
    if status_field:
        try:
            new_status_enum = IssueStatus(status_field)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid status: {status_field}")
        _validate_status_transition(issue, new_status_enum, track, album, current_user, db)

    new_severity_enum = None
    if severity:
        try:
            from app.models.issue import IssueSeverity
            new_severity_enum = IssueSeverity(severity)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid severity: {severity}")

    old_status = issue.status
    update_data: dict = {}
    if new_status_enum is not None:
        issue.status = new_status_enum
        update_data["status"] = new_status_enum
    if title is not None:
        issue.title = title
        update_data["title"] = title
    if description is not None:
        issue.description = description
        update_data["description"] = description
    if new_severity_enum is not None:
        issue.severity = new_severity_enum
        update_data["severity"] = new_severity_enum

    new_status = issue.status
    effective_note = (status_note or "").strip()
    if (effective_note or images or audios) and old_status != new_status:
        comment = Comment(
            issue_id=issue.id,
            author_id=current_user.id,
            content=effective_note,
            is_status_note=True,
            old_status=old_status.value,
            new_status=new_status.value,
        )
        db.add(comment)
        db.flush()

        if images:
            from app.config import MAX_IMAGE_UPLOAD_SIZE
            comment_images_dir = settings.get_upload_path() / "comment_images"
            comment_images_dir.mkdir(parents=True, exist_ok=True)
            for img_file in images:
                ext = IMAGE_EXT_MAP.get(img_file.content_type or "", ".jpg")
                filename = f"{uuid.uuid4()}{ext}"
                file_path = f"comment_images/{filename}"
                dest = comment_images_dir / filename
                await stream_upload(img_file, dest, MAX_IMAGE_UPLOAD_SIZE)
                db.add(CommentImage(comment_id=comment.id, file_path=file_path))

        if audios:
            from app.config import MAX_AUDIO_UPLOAD_SIZE
            comment_audios_dir = settings.get_upload_path() / "comment_audios"
            comment_audios_dir.mkdir(parents=True, exist_ok=True)
            for audio_file in audios:
                ext = AUDIO_EXT_MAP.get(audio_file.content_type or "", ".mp3")
                filename = f"{uuid.uuid4()}{ext}"
                file_path = f"comment_audios/{filename}"
                dest = comment_audios_dir / filename
                await stream_upload(audio_file, dest, MAX_AUDIO_UPLOAD_SIZE)
                duration = extract_audio_metadata(dest).duration
                original_filename = audio_file.filename or filename
                db.add(CommentAudio(
                    comment_id=comment.id,
                    file_path=file_path,
                    original_filename=original_filename,
                    duration=duration,
                ))

    if old_status != new_status and current_user.id != issue.author_id:
        track = db.get(Track, issue.track_id)
        notify(db, [issue.author_id], "issue_status_changed", "问题状态已更新",
               f"「{issue.title}」被标记为 {new_status.value}",
               related_issue_id=issue.id,
               background_tasks=background_tasks, album_id=track.album_id if track else None)

    if (
        old_status == IssueStatus.PENDING_DISCUSSION
        and new_status != IssueStatus.PENDING_DISCUSSION
        and current_user.id != track.submitter_id
    ):
        notify(
            db,
            [track.submitter_id],
            "new_issue",
            f"新问题：{issue.title}",
            f"「{track.title}」上有新的审核问题",
            related_track_id=track.id,
            related_issue_id=issue.id,
            background_tasks=background_tasks,
            album_id=track.album_id,
        )

    log_track_event(
        db,
        track,
        current_user,
        "issue_updated",
        payload={"issue_id": issue.id, **update_data},
    )
    db.commit()
    db.refresh(issue)
    return build_issue_detail(issue, db)


@router.post(
    "/api/issues/{issue_id}/comments",
    response_model=CommentRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_comment(
    issue_id: int,
    background_tasks: BackgroundTasks,
    content: Optional[str] = Form(default=None),
    images: list[UploadFile] = File(default=[]),
    audios: list[UploadFile] = File(default=[]),
    audio_object_keys: Optional[str] = Form(default=None),
    audio_original_filenames: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CommentRead:
    # Normalise: pydantic v2 + python-multipart may deliver empty fields as None
    effective_content = (content or '').strip()

    # Parse R2 audio keys (comma-separated) if provided
    r2_audio_keys: list[str] = []
    r2_audio_names: list[str] = []
    if audio_object_keys:
        r2_audio_keys = [k.strip() for k in audio_object_keys.split("\n") if k.strip()]
        r2_audio_names = [n.strip() for n in (audio_original_filenames or "").split("\n")]
        # Pad names to match keys
        while len(r2_audio_names) < len(r2_audio_keys):
            r2_audio_names.append(Path(r2_audio_keys[len(r2_audio_names)]).name)

    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found.")
    track = db.get(Track, issue.track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    _ensure_issue_visible_to_user(issue, track, current_user)

    if not effective_content and not images and not audios and not r2_audio_keys:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A comment must have text content, at least one image, or at least one audio file.",
        )

    for img_file in images:
        if img_file.content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported file type: {img_file.content_type}. Allowed: jpeg, png, gif, webp.",
            )

    if len(audios) > MAX_AUDIOS_PER_COMMENT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"A comment may contain at most {MAX_AUDIOS_PER_COMMENT} audio files.",
        )
    for audio_file in audios:
        if audio_file.content_type not in ALLOWED_AUDIO_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported audio type: {audio_file.content_type}. Allowed: mp3, wav, flac, aac, ogg.",
            )

    comment = Comment(issue_id=issue_id, author_id=current_user.id, content=content or '')
    db.add(comment)
    db.flush()

    if images:
        from app.config import MAX_IMAGE_UPLOAD_SIZE

        comment_images_dir = settings.get_upload_path() / "comment_images"
        comment_images_dir.mkdir(parents=True, exist_ok=True)
        for img_file in images:
            ext = IMAGE_EXT_MAP.get(img_file.content_type or "", ".jpg")
            filename = f"{uuid.uuid4()}{ext}"
            file_path = f"comment_images/{filename}"
            dest = comment_images_dir / filename
            await stream_upload(img_file, dest, MAX_IMAGE_UPLOAD_SIZE)
            db.add(CommentImage(comment_id=comment.id, file_path=file_path))

    if audios:
        from app.config import MAX_AUDIO_UPLOAD_SIZE

        comment_audios_dir = settings.get_upload_path() / "comment_audios"
        comment_audios_dir.mkdir(parents=True, exist_ok=True)
        for audio_file in audios:
            ext = AUDIO_EXT_MAP.get(audio_file.content_type or "", ".mp3")
            filename = f"{uuid.uuid4()}{ext}"
            file_path = f"comment_audios/{filename}"
            dest = comment_audios_dir / filename
            await stream_upload(audio_file, dest, MAX_AUDIO_UPLOAD_SIZE)
            duration = extract_audio_metadata(dest).duration
            original_filename = audio_file.filename or filename
            db.add(CommentAudio(
                comment_id=comment.id,
                file_path=file_path,
                original_filename=original_filename,
                duration=duration,
            ))

    # R2 audio attachments
    if r2_audio_keys:
        from botocore.exceptions import ClientError
        from app.services.r2 import download_to_temp

        for key, orig_name in zip(r2_audio_keys, r2_audio_names):
            try:
                tmp = download_to_temp(key)
            except ClientError:
                raise HTTPException(status_code=400, detail=f"R2 object not found: {key}")
            try:
                duration = extract_audio_metadata(tmp).duration
            finally:
                tmp.unlink(missing_ok=True)
            db.add(CommentAudio(
                comment_id=comment.id,
                file_path=key,
                storage_backend="r2",
                original_filename=orig_name,
                duration=duration,
            ))

    log_track_event(
        db,
        track,
        current_user,
        "issue_comment_added",
        payload={"issue_id": issue.id},
    )

    participant_ids = [issue.author_id] + [c.author_id for c in issue.comments if c.id != comment.id]
    notify_ids = [uid for uid in dict.fromkeys(participant_ids) if uid != current_user.id]
    if notify_ids:
        notify(db, notify_ids, "new_comment", "新评论",
               f"「{issue.title}」有新评论", related_issue_id=issue.id,
               background_tasks=background_tasks, album_id=track.album_id)

    db.commit()
    db.refresh(comment)
    return build_comment_read(comment, db)


# ── R2 presigned upload for comment audio ────────────────────────────────────

@router.post("/api/issues/{issue_id}/comments/request-audio-upload")
def request_comment_audio_upload(
    issue_id: int,
    params: RequestCommentAudioUploadParams,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PresignedCommentAudioResponse:
    from app.config import MAX_AUDIO_UPLOAD_SIZE

    if not settings.R2_ENABLED:
        raise HTTPException(status_code=501, detail="R2 storage is not enabled.")

    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue not found.")
    track = db.get(Track, issue.track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    _ensure_issue_visible_to_user(issue, track, current_user)

    if len(params.files) > MAX_AUDIOS_PER_COMMENT:
        raise HTTPException(status_code=422, detail=f"A comment may contain at most {MAX_AUDIOS_PER_COMMENT} audio files.")

    from app.services.r2 import generate_upload_url, make_object_key

    from app.routers.tracks import ALLOWED_AUDIO_EXTENSIONS

    uploads = []
    for f in params.files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_AUDIO_EXTENSIONS:
            raise HTTPException(status_code=422, detail=f"Unsupported audio format: {ext}")
        if f.file_size > MAX_AUDIO_UPLOAD_SIZE:
            raise HTTPException(status_code=413, detail=f"File too large. Maximum size is {MAX_AUDIO_UPLOAD_SIZE // (1024 * 1024)} MB.")

        object_key = make_object_key(f"comments/{issue_id}", 0, f.filename)
        upload_url = generate_upload_url(object_key, f.content_type)
        uploads.append(PresignedUploadResponse(
            upload_url=upload_url,
            object_key=object_key,
            upload_id=uuid.uuid4().hex,
            expires_in=settings.R2_PRESIGNED_UPLOAD_EXPIRY,
        ))

    return PresignedCommentAudioResponse(uploads=uploads)


@router.patch("/api/tracks/{track_id}/issues/batch", response_model=list[IssueRead])
def batch_update_issues(
    track_id: int,
    payload: IssueBatchUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[IssueRead]:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = _album_for_track(track, db)
    ensure_track_visibility(track, current_user, db)

    issues = list(db.scalars(
        select(Issue).where(Issue.id.in_(payload.issue_ids), Issue.track_id == track_id)
    ).all())

    for issue in issues:
        _ensure_issue_visible_to_user(issue, track, current_user)
        _ensure_issue_update_permission(issue, track, album, current_user, db)
        _validate_status_transition(issue, payload.status, track, album, current_user, db)
        old_status = issue.status
        issue.status = payload.status
        if payload.status_note and old_status != payload.status:
            db.add(Comment(
                issue_id=issue.id,
                author_id=current_user.id,
                content=payload.status_note,
                is_status_note=True,
                old_status=old_status.value,
                new_status=payload.status.value,
            ))

    db.commit()
    for issue in issues:
        db.refresh(issue)
    return [build_issue_read(issue, db) for issue in issues]


# ── issue audio file serving ─────────────────────────────────────────────────


@router.get("/api/issue-audios/{audio_id}/file")
def serve_issue_audio(
    audio_id: int,
    resolve: str | None = Query(default=None),
    db: Session = Depends(get_db),
    bearer_user: User | None = Depends(get_current_user_optional),
    token_user: User | None = Depends(get_user_from_token_param),
):
    user = bearer_user or token_user
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")

    audio = db.get(IssueAudio, audio_id)
    if audio is None:
        raise HTTPException(status_code=404, detail="Issue audio not found.")

    issue = db.get(Issue, audio.issue_id)
    track = db.get(Track, issue.track_id) if issue else None
    if not track:
        raise HTTPException(status_code=404, detail="Associated track not found.")
    ensure_track_visibility(track, user, db)
    _ensure_issue_visible_to_user(issue, track, user)

    if audio.storage_backend == "r2":
        from app.services.r2 import public_url

        url = public_url(audio.file_path)
        if resolve == "json":
            return {"url": url}
        return RedirectResponse(url, status_code=302)

    if resolve == "json":
        return {"url": None}

    file_path = settings.get_upload_path() / audio.file_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file missing from disk.")
    mime_map = {
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
        ".ogg": "audio/ogg", ".aac": "audio/aac", ".m4a": "audio/mp4",
    }
    media_type = mime_map.get(file_path.suffix.lower(), "audio/octet-stream")
    return FileResponse(path=str(file_path), media_type=media_type, filename=audio.original_filename)


# ── comment audio file serving ───────────────────────────────────────────────


@router.get("/api/comment-audios/{audio_id}/file")
def serve_comment_audio(
    audio_id: int,
    resolve: str | None = Query(default=None),
    db: Session = Depends(get_db),
    bearer_user: User | None = Depends(get_current_user_optional),
    token_user: User | None = Depends(get_user_from_token_param),
):
    user = bearer_user or token_user
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")

    audio = db.get(CommentAudio, audio_id)
    if audio is None:
        raise HTTPException(status_code=404, detail="Comment audio not found.")

    comment = db.get(Comment, audio.comment_id)
    issue = db.get(Issue, comment.issue_id) if comment else None
    track = db.get(Track, issue.track_id) if issue else None
    if not track:
        raise HTTPException(status_code=404, detail="Associated track not found.")
    ensure_track_visibility(track, user, db)
    _ensure_issue_visible_to_user(issue, track, user)

    if audio.storage_backend == "r2":
        from app.services.r2 import public_url

        url = public_url(audio.file_path)
        if resolve == "json":
            return {"url": url}
        return RedirectResponse(url, status_code=302)

    if resolve == "json":
        return {"url": None}

    # Local file
    file_path = settings.get_upload_path() / audio.file_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file missing from disk.")
    mime_map = {
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
        ".ogg": "audio/ogg", ".aac": "audio/aac", ".m4a": "audio/mp4",
    }
    media_type = mime_map.get(file_path.suffix.lower(), "audio/octet-stream")
    return FileResponse(path=str(file_path), media_type=media_type, filename=audio.original_filename)


# ── comment edit / delete ──────────────────────────────────────────────────


@router.patch("/api/comments/{comment_id}", response_model=CommentRead)
def update_comment(
    comment_id: int,
    payload: CommentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CommentRead:
    comment = db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found.")
    if comment.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the author can edit this comment.")
    if comment.is_status_note:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Status notes cannot be edited.")

    if comment.content != payload.content:
        db.add(EditHistory(
            entity_type="comment",
            entity_id=comment.id,
            old_content=comment.content,
            edited_by_id=current_user.id,
        ))
        comment.content = payload.content
        comment.edited_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(comment)
    return build_comment_read(comment, db)


@router.delete("/api/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_comment(
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    comment = db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found.")
    if comment.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the author can delete this comment.")
    if comment.is_status_note:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Status notes cannot be deleted.")

    db.delete(comment)
    db.commit()


@router.get("/api/comments/{comment_id}/history", response_model=list[EditHistoryRead])
def get_comment_history(
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[EditHistoryRead]:
    comment = db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found.")
    issue = db.get(Issue, comment.issue_id)
    if issue:
        track = db.get(Track, issue.track_id)
        if track:
            ensure_track_visibility(track, current_user, db)

    histories = list(db.scalars(
        select(EditHistory)
        .where(EditHistory.entity_type == "comment", EditHistory.entity_id == comment_id)
        .order_by(EditHistory.created_at.desc())
    ).all())
    return [EditHistoryRead.model_validate(h) for h in histories]
