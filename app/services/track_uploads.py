"""Track upload orchestration: source-version finalization and delivery status.

Multi-step domain flows moved out of ``app.routers.tracks`` so the router
stays thin (auth dep → validate → call service → serialize).
"""

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.circle_permissions import album_manager_user_ids
from app.models.album import Album
from app.models.comment import Comment
from app.models.issue import Issue, IssueStatus
from app.models.track import RejectionMode, Track, TrackStatus, WorkflowVariant
from app.models.track_source_version import TrackSourceVersion
from app.models.user import User
from app.notifications import notify
from app.services.track_queries import log_track_event, track_composer_actor_ids_for_notify
from app.workflow_engine import (
    execute_delivery_upload,
    execute_revision_upload,
    prepare_review_assignments_for_stage_entry,
)


def create_source_version(
    track: Track,
    user: User,
    file_path: str | None,
    duration: float | None,
    *,
    revision_notes: str | None = None,
    storage_backend: str = "local",
    source_kind: str = "file",
) -> TrackSourceVersion:
    return TrackSourceVersion(
        track_id=track.id,
        workflow_cycle=track.workflow_cycle,
        version_number=track.version,
        file_path=file_path,
        storage_backend=storage_backend,
        source_kind=source_kind,
        duration=duration,
        uploaded_by_id=user.id,
        revision_notes=revision_notes,
    )


def _dedupe_ints(values: list[int]) -> list[int]:
    return list(dict.fromkeys(values))


def _status_note_visibility_for_resolution(status_value: IssueStatus) -> str:
    if status_value in {IssueStatus.PENDING_DISCUSSION, IssueStatus.INTERNAL_RESOLVED}:
        return "internal"
    return "public"


def resolve_issues_for_revision_upload(
    db: Session,
    *,
    track: Track,
    actor: User,
    issue_ids: list[int],
    issue_cycle: int,
    resolution_note: str | None,
) -> list[int]:
    deduped_issue_ids = _dedupe_ints(issue_ids)
    if not deduped_issue_ids:
        return []

    issues = list(
        db.scalars(
            select(Issue)
            .where(Issue.track_id == track.id, Issue.id.in_(deduped_issue_ids))
            .order_by(Issue.id)
        ).all()
    )
    issue_map = {issue.id: issue for issue in issues}
    missing_ids = [issue_id for issue_id in deduped_issue_ids if issue_id not in issue_map]
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Resolved issues do not belong to this track: {missing_ids}",
        )

    wrong_cycle_ids = [
        issue.id for issue in issues
        if issue.workflow_cycle != issue_cycle
    ]
    if wrong_cycle_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Resolved issues must belong to workflow cycle {issue_cycle}: {wrong_cycle_ids}",
        )

    note = (resolution_note or "").strip()
    resolved_ids: list[int] = []
    for issue_id in deduped_issue_ids:
        issue = issue_map[issue_id]
        previous_status = issue.status
        if previous_status != IssueStatus.RESOLVED:
            issue.status = IssueStatus.RESOLVED
        if note and previous_status != IssueStatus.RESOLVED:
            db.add(
                Comment(
                    issue_id=issue.id,
                    author_id=actor.id,
                    content=note,
                    visibility=_status_note_visibility_for_resolution(previous_status),
                    is_status_note=True,
                    old_status=previous_status.value,
                    new_status=IssueStatus.RESOLVED.value,
                )
            )
        resolved_ids.append(issue.id)

    return resolved_ids


def finalize_source_version_upload(
    db: Session,
    *,
    album: Album,
    track: Track,
    current_user: User,
    background_tasks: BackgroundTasks,
    file_path: str | None,
    storage_backend: str,
    duration: float | None,
    revision_notes: str | None,
    resolved_issue_ids: list[int],
    resolution_note: str | None,
    source_kind: str = "file",
    replace_current_audio: bool = True,
) -> None:
    issue_cycle = track.workflow_cycle

    # Resolve the next step *before* mutating rejection_mode so that the
    # engine can recognise a resubmit on a rejected+resubmittable track.
    next_status = execute_revision_upload(album, track)

    resolved_ids = resolve_issues_for_revision_upload(
        db,
        track=track,
        actor=current_user,
        issue_ids=resolved_issue_ids,
        issue_cycle=issue_cycle,
        resolution_note=resolution_note,
    )

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
    track.version += 1
    if replace_current_audio:
        if file_path is None:
            raise HTTPException(status_code=500, detail="File source upload produced no audio file.")
        track.file_path = file_path
        track.storage_backend = storage_backend
        track.duration = duration
    track.status = next_status
    track.requested_revision_type = None  # Clear after upload
    prepare_review_assignments_for_stage_entry(
        db,
        album,
        track,
        next_status,
        background_tasks,
    )
    db.add(
        create_source_version(
            track,
            current_user,
            file_path,
            duration,
            revision_notes=revision_notes or None,
            storage_backend=storage_backend,
            source_kind=source_kind,
        )
    )

    event_payload: dict[str, object] = {"version": track.version, "workflow_cycle": track.workflow_cycle}
    if source_kind != "file":
        event_payload["source_kind"] = source_kind
    if resolved_ids:
        event_payload["resolved_issue_ids"] = resolved_ids
    if resolution_note:
        event_payload["resolution_note"] = resolution_note

    log_track_event(
        db,
        track,
        current_user,
        "source_version_uploaded",
        from_status=previous_status,
        to_status=next_status,
        payload=event_payload,
    )


def handle_delivery_status(
    db: Session,
    album: Album,
    track: Track,
    current_user: User,
    delivery_number: int,
    background_tasks: BackgroundTasks,
) -> None:
    """Advance track status after a master delivery submission.

    When the current step has ``require_confirmation=True`` the track stays
    put until the mastering engineer explicitly confirms the delivery.
    Otherwise it advances via the workflow engine.
    """
    previous_status = track.status
    next_status = execute_delivery_upload(album, track)
    if next_status is None:
        log_track_event(
            db, track, current_user, "master_delivery_uploaded",
            from_status=previous_status, to_status=track.status,
            payload={"delivery_number": delivery_number, "awaiting_confirmation": True},
        )
        # Notify mastering engineer that delivery needs confirmation
        notify(db, [album.mastering_engineer_id], "delivery_awaiting_confirmation",
               "母带交付待确认",
               f"「{track.title}」的母带交付已提交，请确认后继续流程",
               related_track_id=track.id,
               background_tasks=background_tasks, album_id=track.album_id,
               webhook_context={"actor_id": current_user.id, "actor_name": current_user.display_name})
        return
    track.status = next_status
    # If the delivery step advances into a review step, make sure reviewers are
    # assigned so the next stage does not stall waiting for an empty assignment
    # set. Idempotent: re-entering an existing review stage reopens the previous
    # assignment set instead of creating duplicates.
    prepare_review_assignments_for_stage_entry(
        db,
        album,
        track,
        next_status,
        background_tasks,
    )
    log_track_event(
        db, track, current_user, "master_delivery_uploaded",
        from_status=previous_status, to_status=track.status,
        payload={"delivery_number": delivery_number},
    )
    notify_targets = [
        *album_manager_user_ids(db, album),
        *track_composer_actor_ids_for_notify(track, album, db, skip_user_id=current_user.id),
    ]
    notify(db, notify_targets, "track_status_changed", "母带交付已提交",
           f"「{track.title}」母带交付已提交，等待审核", related_track_id=track.id,
           background_tasks=background_tasks, album_id=track.album_id,
           webhook_context={"actor_id": current_user.id, "actor_name": current_user.display_name})
