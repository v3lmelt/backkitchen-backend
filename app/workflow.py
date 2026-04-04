import json
import random
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.checklist import ChecklistItem
from app.models.comment import Comment
from app.models.comment_image import CommentImage
from app.models.issue import Issue, IssueStatus
from app.models.master_delivery import MasterDelivery
from app.models.track import RejectionMode, Track, TrackStatus
from app.models.track_source_version import TrackSourceVersion
from app.models.user import User
from app.models.workflow_event import WorkflowEvent
from app.schemas.schemas import (
    AlbumMemberRead,
    ChecklistItemRead,
    CommentImageRead,
    CommentRead,
    IssueDetail,
    IssueRead,
    MasterDeliveryRead,
    TrackDetailResponse,
    TrackRead,
    TrackSourceVersionRead,
    UserRead,
    WorkflowEventRead,
)


def get_album_member_ids(db: Session, album_id: int) -> set[int]:
    rows = db.scalars(select(AlbumMember).where(AlbumMember.album_id == album_id)).all()
    return {row.user_id for row in rows}


def ensure_album_visibility(album: Album, user: User, db: Session) -> None:
    member_ids = get_album_member_ids(db, album.id)
    visible_ids = {album.producer_id, album.mastering_engineer_id}
    visible_ids.update(member_ids)
    if user.id not in visible_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this album.",
        )


def ensure_track_visibility(track: Track, user: User, db: Session) -> Album:
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    if track.submitter_id == user.id or track.peer_reviewer_id == user.id:
        return album
    ensure_album_visibility(album, user, db)
    return album


def current_source_version(track: Track) -> TrackSourceVersion | None:
    if not track.source_versions:
        return None
    return max(track.source_versions, key=lambda item: item.version_number)


def current_master_delivery(track: Track) -> MasterDelivery | None:
    if not track.master_deliveries:
        return None
    current_cycle_deliveries = [
        item for item in track.master_deliveries if item.workflow_cycle == track.workflow_cycle
    ]
    deliveries = current_cycle_deliveries or track.master_deliveries
    return max(deliveries, key=lambda item: item.delivery_number)


def track_allowed_actions(track: Track, user: User, album: Album) -> list[str]:
    actions: list[str] = []
    is_submitter = track.submitter_id == user.id
    is_peer_reviewer = track.peer_reviewer_id == user.id
    is_producer = album.producer_id == user.id
    is_mastering_engineer = album.mastering_engineer_id == user.id

    if is_producer and track.status == TrackStatus.SUBMITTED:
        actions.append("intake")
    if is_peer_reviewer and track.status == TrackStatus.PEER_REVIEW:
        actions.append("peer_review")
    if is_submitter and track.status in {
        TrackStatus.PEER_REVISION,
        TrackStatus.MASTERING_REVISION,
    }:
        actions.append("upload_revision")
    if (
        is_submitter
        and track.status == TrackStatus.REJECTED
        and track.rejection_mode == RejectionMode.RESUBMITTABLE
    ):
        actions.append("resubmit")
    if is_producer and track.status == TrackStatus.PRODUCER_MASTERING_GATE:
        actions.append("producer_gate")
    if is_mastering_engineer and track.status == TrackStatus.MASTERING:
        actions.append("mastering")
    if track.status == TrackStatus.FINAL_REVIEW and (is_submitter or is_producer):
        actions.append("final_review")
    return actions


def _user_read(user: User | None) -> UserRead | None:
    if user is None:
        return None
    return UserRead.model_validate(user)


def _source_version_read(version: TrackSourceVersion | None) -> TrackSourceVersionRead | None:
    if version is None:
        return None
    return TrackSourceVersionRead.model_validate(version)


def _master_delivery_read(delivery: MasterDelivery | None) -> MasterDeliveryRead | None:
    if delivery is None:
        return None
    return MasterDeliveryRead.model_validate(delivery)


def build_track_read(track: Track, user: User, album: Album) -> TrackRead:
    current_source = current_source_version(track)
    current_master = current_master_delivery(track)
    open_issue_count = sum(1 for i in track.issues if i.status == IssueStatus.OPEN)
    return TrackRead(
        id=track.id,
        title=track.title,
        artist=track.artist,
        album_id=track.album_id,
        bpm=track.bpm,
        file_path=track.file_path,
        duration=track.duration,
        status=track.status,
        rejection_mode=track.rejection_mode,
        version=track.version,
        workflow_cycle=track.workflow_cycle,
        submitter_id=track.submitter_id,
        peer_reviewer_id=track.peer_reviewer_id,
        producer_id=album.producer_id,
        mastering_engineer_id=album.mastering_engineer_id,
        created_at=track.created_at,
        updated_at=track.updated_at,
        issue_count=len(track.issues),
        open_issue_count=open_issue_count,
        submitter=_user_read(track.submitter),
        peer_reviewer=_user_read(track.peer_reviewer),
        current_source_version=_source_version_read(current_source),
        current_master_delivery=_master_delivery_read(current_master),
        allowed_actions=track_allowed_actions(track, user, album),
    )


def build_issue_read(issue: Issue, db: Session) -> IssueRead:
    author = db.get(User, issue.author_id)
    return IssueRead(
        id=issue.id,
        track_id=issue.track_id,
        author_id=issue.author_id,
        phase=issue.phase,
        workflow_cycle=issue.workflow_cycle,
        source_version_id=issue.source_version_id,
        master_delivery_id=issue.master_delivery_id,
        title=issue.title,
        description=issue.description,
        issue_type=issue.issue_type,
        severity=issue.severity,
        status=issue.status,
        time_start=issue.time_start,
        time_end=issue.time_end,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        comment_count=len(issue.comments),
        author=_user_read(author),
    )


def build_comment_read(comment: Comment, db: Session) -> CommentRead:
    author = db.get(User, comment.author_id)
    images = [
        CommentImageRead(
            id=image.id,
            comment_id=image.comment_id,
            image_url=f"/uploads/{image.file_path}",
            created_at=image.created_at,
        )
        for image in comment.images
    ]
    return CommentRead(
        id=comment.id,
        issue_id=comment.issue_id,
        author_id=comment.author_id,
        content=comment.content,
        is_status_note=comment.is_status_note,
        created_at=comment.created_at,
        author=_user_read(author),
        images=images,
    )


def build_issue_detail(issue: Issue, db: Session) -> IssueDetail:
    issue_read = build_issue_read(issue, db)
    comments = [build_comment_read(comment, db) for comment in issue.comments]
    return IssueDetail(**issue_read.model_dump(), comments=comments)


def build_checklist_read(item: ChecklistItem) -> ChecklistItemRead:
    return ChecklistItemRead.model_validate(item)


def build_source_version_read(version: TrackSourceVersion) -> TrackSourceVersionRead:
    return TrackSourceVersionRead.model_validate(version)


def build_event_read(event: WorkflowEvent, db: Session) -> WorkflowEventRead:
    actor = db.get(User, event.actor_user_id) if event.actor_user_id else None
    payload = json.loads(event.payload) if event.payload else None
    return WorkflowEventRead(
        id=event.id,
        event_type=event.event_type,
        from_status=event.from_status,
        to_status=event.to_status,
        payload=payload,
        created_at=event.created_at,
        actor=_user_read(actor),
    )


build_workflow_event_read = build_event_read


def build_track_detail(track: Track, user: User, db: Session) -> TrackDetailResponse:
    album = ensure_track_visibility(track, user, db)
    issues = [
        build_issue_read(issue, db)
        for issue in sorted(track.issues, key=lambda row: (row.created_at, row.id))
    ]
    current_source = current_source_version(track)
    checklist_items = [
        build_checklist_read(item)
        for item in track.checklist_items
        if current_source is None or item.source_version_id == current_source.id
    ]
    events = [build_event_read(event, db) for event in track.workflow_events]
    return TrackDetailResponse(
        track=build_track_read(track, user, album),
        issues=issues,
        checklist_items=checklist_items,
        events=events,
        source_versions=[build_source_version_read(v) for v in track.source_versions],
    )


def log_track_event(
    db: Session,
    track: Track,
    actor: User | None,
    event_type: str,
    *,
    from_status: TrackStatus | None = None,
    to_status: TrackStatus | None = None,
    payload: dict[str, Any] | None = None,
) -> WorkflowEvent:
    def _serialize(value: Any) -> Any:
        if hasattr(value, "value"):
            return value.value
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    event = WorkflowEvent(
        track_id=track.id,
        album_id=track.album_id,
        actor_user_id=actor.id if actor else None,
        event_type=event_type,
        from_status=from_status.value if from_status else None,
        to_status=to_status.value if to_status else None,
        payload=json.dumps(payload, default=_serialize) if payload else None,
    )
    db.add(event)
    return event


def assign_random_peer_reviewer(db: Session, album: Album, track: Track) -> int:
    members = db.scalars(select(AlbumMember).where(AlbumMember.album_id == album.id)).all()
    candidates = [
        member.user_id
        for member in members
        if member.user_id != track.submitter_id and member.user_id != album.mastering_engineer_id
    ]
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No eligible peer reviewer is available for this track.",
        )
    selected = random.choice(candidates)
    track.peer_reviewer_id = selected
    return selected
