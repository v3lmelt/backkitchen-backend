import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.album import Album
from app.models.comment import Comment
from app.models.comment_image import CommentImage
from app.models.issue import Issue, IssuePhase
from app.models.track import Track, TrackStatus
from app.models.user import User
from app.notifications import notify
from app.schemas.schemas import CommentRead, IssueBatchUpdate, IssueCreate, IssueDetail, IssueRead, IssueUpdate
from app.security import get_current_user
from app.workflow import (
    build_comment_read,
    build_issue_detail,
    build_issue_read,
    current_master_delivery,
    current_source_version,
    ensure_track_visibility,
    log_track_event,
)

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
IMAGE_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

router = APIRouter(tags=["issues"])


def _album_for_track(track: Track, db: Session) -> Album:
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    return album


def _ensure_issue_permission(track: Track, album: Album, user: User, phase: IssuePhase) -> None:
    if phase == IssuePhase.PEER:
        if track.status != TrackStatus.PEER_REVIEW or track.peer_reviewer_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the assigned peer reviewer can create peer review issues.",
            )
        return
    if phase == IssuePhase.PRODUCER:
        if track.status != TrackStatus.PRODUCER_MASTERING_GATE or album.producer_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the album producer can create producer review issues.",
            )
        return
    if phase == IssuePhase.MASTERING:
        if track.status != TrackStatus.MASTERING or album.mastering_engineer_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the album mastering engineer can create mastering issues.",
            )
        return
    if phase == IssuePhase.FINAL_REVIEW:
        if track.status != TrackStatus.FINAL_REVIEW or user.id not in {
            album.producer_id,
            track.submitter_id,
        }:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the producer or submitter can create final review issues.",
            )
        return


def _ensure_issue_update_permission(issue: Issue, track: Track, album: Album, user: User) -> None:
    if issue.phase == IssuePhase.PEER and user.id not in {track.submitter_id, track.peer_reviewer_id}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot update this peer review issue.")
    if issue.phase == IssuePhase.PRODUCER and user.id not in {
        track.submitter_id,
        album.producer_id,
    }:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot update this producer review issue.")
    if issue.phase == IssuePhase.MASTERING and user.id not in {
        track.submitter_id,
        album.mastering_engineer_id,
    }:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot update this mastering issue.")
    if issue.phase == IssuePhase.FINAL_REVIEW and user.id not in {
        track.submitter_id,
        album.producer_id,
    }:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot update this final review issue.")


@router.post(
    "/api/tracks/{track_id}/issues",
    response_model=IssueRead,
    status_code=status.HTTP_201_CREATED,
)
def create_issue(
    track_id: int,
    payload: IssueCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> IssueRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    _ensure_issue_permission(track, album, current_user, payload.phase)

    if payload.issue_type.value == "range" and payload.time_end is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="time_end is required for RANGE issues.",
        )
    if payload.time_end is not None and payload.time_end <= payload.time_start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="time_end must be greater than time_start.",
        )

    source_version_id = None
    master_delivery_id = None
    if payload.phase in {IssuePhase.PEER, IssuePhase.PRODUCER, IssuePhase.MASTERING}:
        source_version = current_source_version(track)
        if source_version is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No source version available.")
        source_version_id = source_version.id
    if payload.phase == IssuePhase.FINAL_REVIEW:
        delivery = current_master_delivery(track)
        if delivery is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No master delivery available.")
        master_delivery_id = delivery.id

    issue = Issue(
        track_id=track_id,
        author_id=current_user.id,
        phase=payload.phase,
        workflow_cycle=track.workflow_cycle,
        source_version_id=source_version_id,
        master_delivery_id=master_delivery_id,
        title=payload.title,
        description=payload.description,
        issue_type=payload.issue_type,
        severity=payload.severity,
        time_start=payload.time_start,
        time_end=payload.time_end,
    )
    db.add(issue)
    db.flush()  # assign issue.id before logging
    log_track_event(
        db,
        track,
        current_user,
        "issue_created",
        payload={"phase": payload.phase.value, "title": payload.title, "issue_id": issue.id},
    )
    if current_user.id != track.submitter_id:
        notify(db, [track.submitter_id], "new_issue", f"新问题：{issue.title}",
               f"「{track.title}」上有新的审核问题",
               related_track_id=track.id, related_issue_id=issue.id)
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

    issues = list(db.scalars(select(Issue).where(Issue.track_id == track_id).order_by(Issue.created_at)).all())
    return [build_issue_read(issue, db) for issue in issues]


@router.get("/api/issues/{issue_id}", response_model=IssueDetail)
def get_issue(
    issue_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> IssueDetail:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found.")
    track = db.get(Track, issue.track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    return build_issue_detail(issue, db)


@router.patch("/api/issues/{issue_id}", response_model=IssueRead)
def update_issue(
    issue_id: int,
    payload: IssueUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> IssueRead:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found.")
    track = db.get(Track, issue.track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = _album_for_track(track, db)
    ensure_track_visibility(track, current_user, db)
    _ensure_issue_update_permission(issue, track, album, current_user)

    old_status = issue.status
    update_data = payload.model_dump(exclude_unset=True, exclude={"status_note"})
    for field, value in update_data.items():
        setattr(issue, field, value)

    new_status = issue.status
    if payload.status_note and old_status != new_status:
        db.add(Comment(issue_id=issue.id, author_id=current_user.id, content=payload.status_note, is_status_note=True))

    if old_status != new_status and current_user.id != issue.author_id:
        notify(db, [issue.author_id], "issue_status_changed", "问题状态已更新",
               f"「{issue.title}」被标记为 {new_status.value}",
               related_issue_id=issue.id)

    log_track_event(
        db,
        track,
        current_user,
        "issue_updated",
        payload={"issue_id": issue.id, **update_data},
    )
    db.commit()
    db.refresh(issue)
    return build_issue_read(issue, db)


@router.post(
    "/api/issues/{issue_id}/comments",
    response_model=CommentRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_comment(
    issue_id: int,
    content: Optional[str] = Form(default=None),
    images: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CommentRead:
    # Normalise: pydantic v2 + python-multipart may deliver empty fields as None
    effective_content = (content or '').strip()

    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found.")
    track = db.get(Track, issue.track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)

    if not effective_content and not images:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A comment must have text content or at least one image.",
        )

    for img_file in images:
        if img_file.content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported file type: {img_file.content_type}. Allowed: jpeg, png, gif, webp.",
            )

    comment = Comment(issue_id=issue_id, author_id=current_user.id, content=content or '')
    db.add(comment)
    db.flush()

    if images:
        comment_images_dir = settings.get_upload_path() / "comment_images"
        comment_images_dir.mkdir(parents=True, exist_ok=True)
        for img_file in images:
            ext = IMAGE_EXT_MAP.get(img_file.content_type or "", ".jpg")
            filename = f"{uuid.uuid4()}{ext}"
            file_path = f"comment_images/{filename}"
            dest = comment_images_dir / filename
            data = await img_file.read()
            dest.write_bytes(data)
            db.add(CommentImage(comment_id=comment.id, file_path=file_path))

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
               f"「{issue.title}」有新评论", related_issue_id=issue.id)

    db.commit()
    db.refresh(comment)
    return build_comment_read(comment, db)


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
        _ensure_issue_update_permission(issue, track, album, current_user)
        old_status = issue.status
        issue.status = payload.status
        if payload.status_note and old_status != payload.status:
            db.add(Comment(
                issue_id=issue.id,
                author_id=current_user.id,
                content=payload.status_note,
                is_status_note=True,
            ))

    db.commit()
    for issue in issues:
        db.refresh(issue)
    return [build_issue_read(issue, db) for issue in issues]
