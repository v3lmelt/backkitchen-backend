import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.album import Album
from app.models.checklist import ChecklistItem
from app.models.issue import Issue, IssuePhase, IssueStatus
from app.models.master_delivery import MasterDelivery
from app.models.track import RejectionMode, Track, TrackStatus
from app.models.track_source_version import TrackSourceVersion
from app.models.user import User
from app.schemas.schemas import (
    IntakeDecisionRequest,
    ProducerGateDecisionRequest,
    TrackDetailResponse,
    TrackListItem,
    TrackRead,
    PeerReviewDecisionRequest,
)
from app.notifications import notify
from app.security import get_current_user
from app.services.audio import extract_audio_metadata
from app.workflow import (
    assign_random_peer_reviewer,
    build_track_detail,
    build_track_read,
    current_master_delivery,
    current_source_version,
    ensure_album_visibility,
    ensure_track_visibility,
    get_album_member_ids,
    log_track_event,
)

router = APIRouter(prefix="/api/tracks", tags=["tracks"])

# Resolved once at startup to avoid a mkdir syscall on every file-serve request.
_UPLOAD_BASE = Path(settings.UPLOAD_DIR).resolve()

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


def _save_upload(file: UploadFile, stem: str | None = None) -> tuple[str, float | None]:
    upload_dir = settings.get_upload_path()
    ext = Path(file.filename).suffix.lower() if file.filename else ".bin"
    unique_name = f"{stem or uuid.uuid4().hex}{ext}"
    dest = upload_dir / unique_name
    content = file.file.read()
    dest.write_bytes(content)
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


def _track_list_item(track: Track, user: User, album: Album) -> TrackListItem:
    return TrackListItem(**build_track_read(track, user, album).model_dump(), album_title=album.title)


def _serve_path(path_str: str, filename_prefix: str) -> FileResponse:
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
    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=f"{filename_prefix}{file_path.suffix}",
    )


@router.post("", response_model=TrackRead, status_code=status.HTTP_201_CREATED)
def create_track(
    title: str = Form(...),
    artist: str = Form(...),
    album_id: int = Form(...),
    bpm: int | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)

    file_path, duration = _save_upload(file, f"{sanitize_filename(title)}_v1")
    track = Track(
        title=title,
        artist=artist,
        album_id=album_id,
        submitter_id=current_user.id,
        bpm=bpm,
        file_path=file_path,
        duration=duration,
        status=TrackStatus.SUBMITTED,
        version=1,
        workflow_cycle=1,
    )
    db.add(track)
    db.flush()
    db.add(_source_version_create(track, current_user, file_path, duration))
    log_track_event(db, track, current_user, "track_submitted", to_status=TrackStatus.SUBMITTED)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album)


@router.get("", response_model=list[TrackListItem])
def list_tracks(
    status_filter: TrackStatus | None = Query(default=None, alias="status"),
    album_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TrackListItem]:
    albums = list(db.scalars(select(Album)).all())
    visible_album_ids = {
        album.id
        for album in albums
        if current_user.id
        in ({album.producer_id, album.mastering_engineer_id} | get_album_member_ids(db, album.id))
    }
    stmt = select(Track).order_by(Track.id)
    if status_filter is not None:
        stmt = stmt.where(Track.status == status_filter)
    if album_id is not None:
        stmt = stmt.where(Track.album_id == album_id)
    tracks = list(db.scalars(stmt).all())
    results: list[TrackListItem] = []
    albums_by_id = {album.id: album for album in albums}
    for track in tracks:
        if track.submitter_id != current_user.id and track.album_id not in visible_album_ids and track.peer_reviewer_id != current_user.id:
            continue
        album = albums_by_id.get(track.album_id)
        if album is None:
            continue
        results.append(_track_list_item(track, current_user, album))
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


@router.post("/{track_id}/source-versions", response_model=TrackRead)
def upload_source_version(
    track_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if track.submitter_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the submitter can upload a new source version.")

    if track.status == TrackStatus.REJECTED and track.rejection_mode == RejectionMode.RESUBMITTABLE:
        next_status = TrackStatus.SUBMITTED
        track.workflow_cycle += 1
        track.peer_reviewer_id = None
        track.rejection_mode = None
    elif track.status == TrackStatus.PEER_REVISION:
        next_status = TrackStatus.PEER_REVIEW
    elif track.status == TrackStatus.MASTERING_REVISION:
        next_status = TrackStatus.MASTERING
    else:
        raise HTTPException(status_code=409, detail="This track is not waiting for a new source version.")

    previous_status = track.status
    file_path, duration = _save_upload(file, f"{sanitize_filename(track.title)}_v{track.version + 1}")
    track.version += 1
    track.file_path = file_path
    track.duration = duration
    track.status = next_status
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
    return build_track_read(track, current_user, album)


@router.post("/{track_id}/intake-decision", response_model=TrackRead)
def intake_decision(
    track_id: int,
    payload: IntakeDecisionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if album.producer_id != current_user.id or track.status != TrackStatus.SUBMITTED:
        raise HTTPException(status_code=403, detail="Only the producer can intake submitted tracks.")

    previous_status = track.status
    if payload.decision == "accept":
        selected = assign_random_peer_reviewer(db, album, track)
        track.status = TrackStatus.PEER_REVIEW
        track.rejection_mode = None
        log_track_event(
            db,
            track,
            current_user,
            "submission_accepted",
            from_status=previous_status,
            to_status=track.status,
            payload={"peer_reviewer_id": selected},
        )
        notify(db, [track.submitter_id], "track_status_changed", "曲目进入审核",
               f"「{track.title}」已进入同行审核阶段", related_track_id=track.id)
    else:
        track.status = TrackStatus.REJECTED
        track.peer_reviewer_id = None
        track.rejection_mode = (
            RejectionMode.FINAL if payload.decision == "reject_final" else RejectionMode.RESUBMITTABLE
        )
        log_track_event(
            db,
            track,
            current_user,
            "submission_rejected",
            from_status=previous_status,
            to_status=track.status,
            payload={"rejection_mode": track.rejection_mode.value},
        )
        notify(db, [track.submitter_id], "track_status_changed", "曲目被退回",
               f"「{track.title}」已被退回", related_track_id=track.id)

    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album)


@router.post("/{track_id}/peer-review/finish", response_model=TrackRead)
def finish_peer_review(
    track_id: int,
    payload: PeerReviewDecisionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if track.status != TrackStatus.PEER_REVIEW or track.peer_reviewer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the assigned peer reviewer can finish peer review.")
    source_version = current_source_version(track)
    if source_version is None:
        raise HTTPException(status_code=409, detail="No source version found.")
    checklist_count = db.scalar(
        select(func.count(ChecklistItem.id)).where(
            ChecklistItem.track_id == track.id,
            ChecklistItem.reviewer_id == current_user.id,
            ChecklistItem.source_version_id == source_version.id,
        )
    )
    if not checklist_count:
        raise HTTPException(status_code=409, detail="Submit the peer review checklist before finishing the review.")

    previous_status = track.status
    track.status = (
        TrackStatus.PEER_REVISION
        if payload.decision == "needs_revision"
        else TrackStatus.PRODUCER_MASTERING_GATE
    )
    log_track_event(
        db,
        track,
        current_user,
        "peer_review_finished",
        from_status=previous_status,
        to_status=track.status,
        payload={"decision": payload.decision, "source_version_id": source_version.id},
    )
    if payload.decision == "needs_revision":
        notify(db, [track.submitter_id], "track_status_changed", "需要修改",
               f"「{track.title}」需要修改", related_track_id=track.id)
        notify(db, [track.peer_reviewer_id], "track_status_changed", "已发送修改请求",
               f"「{track.title}」的修改请求已发送给作者", related_track_id=track.id)
    else:
        notify(db, [album.producer_id], "track_status_changed", "同行审核通过",
               f"「{track.title}」同行审核已通过", related_track_id=track.id)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album)


@router.post("/{track_id}/producer-gate", response_model=TrackRead)
def producer_gate(
    track_id: int,
    payload: ProducerGateDecisionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if album.producer_id != current_user.id or track.status != TrackStatus.PRODUCER_MASTERING_GATE:
        raise HTTPException(status_code=403, detail="Only the producer can decide the mastering gate.")

    previous_status = track.status
    track.status = (
        TrackStatus.MASTERING
        if payload.decision == "send_to_mastering"
        else TrackStatus.PEER_REVISION
    )
    log_track_event(
        db,
        track,
        current_user,
        "producer_mastering_gate_decided",
        from_status=previous_status,
        to_status=track.status,
        payload={"decision": payload.decision},
    )
    if payload.decision == "send_to_mastering":
        notify(db, [album.mastering_engineer_id], "track_status_changed", "曲目进入混音阶段",
               f"「{track.title}」已进入混音阶段", related_track_id=track.id)
    else:
        notify(db, [track.submitter_id, track.peer_reviewer_id], "track_status_changed",
               "制作人要求重新审核", f"「{track.title}」制作人要求重新进行同行审核", related_track_id=track.id)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album)


@router.post("/{track_id}/mastering/request-revision", response_model=TrackRead)
def request_mastering_revision(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if album.mastering_engineer_id != current_user.id or track.status != TrackStatus.MASTERING:
        raise HTTPException(status_code=403, detail="Only the mastering engineer can request source revisions.")

    previous_status = track.status
    track.status = TrackStatus.MASTERING_REVISION
    log_track_event(
        db,
        track,
        current_user,
        "mastering_revision_requested",
        from_status=previous_status,
        to_status=track.status,
    )
    notify(db, [track.submitter_id], "track_status_changed", "混音师请求源文件修改",
           f"「{track.title}」混音师请求修改源文件", related_track_id=track.id)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album)


@router.post("/{track_id}/master-deliveries", response_model=TrackRead)
def upload_master_delivery(
    track_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if album.mastering_engineer_id != current_user.id or track.status != TrackStatus.MASTERING:
        raise HTTPException(status_code=403, detail="Only the mastering engineer can upload a master delivery.")

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
    previous_status = track.status
    track.status = TrackStatus.FINAL_REVIEW
    db.add(delivery)
    log_track_event(
        db,
        track,
        current_user,
        "master_delivery_uploaded",
        from_status=previous_status,
        to_status=track.status,
        payload={"delivery_number": delivery_number},
    )
    notify(db, [album.producer_id, track.submitter_id], "track_status_changed", "主控文件已上传",
           f"「{track.title}」主控文件已上传，等待审核", related_track_id=track.id)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album)


@router.post("/{track_id}/final-review/approve", response_model=TrackRead)
def approve_final_review(
    track_id: int,
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

    if current_user.id == album.producer_id:
        delivery.producer_approved_at = delivery.producer_approved_at or datetime.now(timezone.utc)
        event_type = "final_review_approved_by_producer"
    else:
        delivery.submitter_approved_at = delivery.submitter_approved_at or datetime.now(timezone.utc)
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
               "曲目已完成！", f"「{track.title}」已完成所有审核流程！", related_track_id=track.id)
    db.commit()
    db.refresh(track)
    return build_track_read(track, current_user, album)


@router.post("/{track_id}/final-review/return", response_model=TrackRead)
def return_to_mastering(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if track.status != TrackStatus.FINAL_REVIEW or current_user.id not in {album.producer_id, track.submitter_id}:
        raise HTTPException(status_code=403, detail="Only the producer or submitter can return the track to mastering.")
    delivery = current_master_delivery(track)
    if delivery is None:
        raise HTTPException(status_code=409, detail="No master delivery available.")

    open_final_review_issues = [
        issue
        for issue in track.issues
        if issue.phase == IssuePhase.FINAL_REVIEW
        and issue.workflow_cycle == track.workflow_cycle
        and issue.master_delivery_id == delivery.id
        and issue.status != IssueStatus.RESOLVED
    ]
    if not open_final_review_issues:
        raise HTTPException(
            status_code=409,
            detail="Create at least one unresolved final review issue before returning to mastering.",
        )

    delivery.producer_approved_at = None
    delivery.submitter_approved_at = None
    previous_status = track.status
    track.status = TrackStatus.MASTERING
    log_track_event(
        db,
        track,
        current_user,
        "returned_to_mastering",
        from_status=previous_status,
        to_status=track.status,
        payload={"delivery_id": delivery.id, "issue_count": len(open_final_review_issues)},
    )
    notify(db, [album.mastering_engineer_id], "track_status_changed", "曲目退回混音阶段",
           f"「{track.title}」已退回混音阶段", related_track_id=track.id)
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
    if track.file_path:
        file_path = Path(track.file_path)
        if file_path.exists():
            file_path.unlink()
    db.delete(track)
    db.commit()


@router.get("/{track_id}/audio")
def serve_audio(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    if not track.file_path:
        raise HTTPException(status_code=404, detail="No source audio is available for this track.")
    return _serve_path(track.file_path, track.title)


@router.get("/{track_id}/source-versions/{version_id}/audio")
def get_source_version_audio(
    track_id: int,
    version_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)

    version = db.get(TrackSourceVersion, version_id)
    if version is None or version.track_id != track_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")

    return _serve_path(version.file_path, f"{track.title}-v{version.version_number}")


@router.get("/{track_id}/master-audio")
def serve_master_audio(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    delivery = current_master_delivery(track)
    if delivery is None:
        raise HTTPException(status_code=404, detail="No master delivery is available for this track.")
    return _serve_path(delivery.file_path, f"{track.title}-master")
