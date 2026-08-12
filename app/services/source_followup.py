"""Source follow-up request orchestration.

Multi-step domain flows (request → decide → apply/cancel) moved out of
``app.routers.tracks`` so the router stays thin.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.circle_permissions import album_manager_user_ids, is_album_manager
from app.models.album import Album
from app.models.reopen_request import ReopenRequest, ReopenRequestStatus
from app.models.source_followup_request import SourceFollowupRequest, SourceFollowupRequestStatus
from app.models.track import Track, TrackStatus
from app.models.user import User
from app.notifications import notify
from app.realtime import broadcast_track_updated
from app.schemas.schemas import TrackRead
from app.services.track_queries import (
    is_track_composer_actor,
    log_track_event,
    pending_source_followup_request,
)
from app.services.track_uploads import create_source_version
from app.track_serializers import build_track_read
from app.workflow_engine import (
    execute_reopen,
    get_current_step,
    get_step_by_id,
    get_steps,
    parse_workflow_config,
    should_new_cycle,
    target_is_mastering_related,
)

logger = logging.getLogger(__name__)


def ensure_source_followup_request_allowed(
    db: Session,
    *,
    track: Track,
    album: Album,
    current_user: User,
) -> None:
    if not album.quick_followup_enabled:
        raise HTTPException(status_code=403, detail="Quick follow-up is not enabled for this album.")
    if track.archived_at is not None:
        raise HTTPException(status_code=409, detail="Archived tracks cannot request a source follow-up.")
    if not is_track_composer_actor(track, album, current_user.id, db):
        raise HTTPException(status_code=403, detail="Only a track composer can request a source follow-up.")
    if track.status == TrackStatus.REJECTED.value:
        raise HTTPException(status_code=409, detail="Rejected tracks must use the resubmit flow.")
    if track.status == TrackStatus.SOURCE_FOLLOWUP_PENDING.value:
        raise HTTPException(status_code=409, detail="A source follow-up request is already pending.")

    step = get_current_step(parse_workflow_config(album), track)
    if step is not None and step.type == "revision":
        raise HTTPException(status_code=409, detail="Revision stages already allow source uploads.")

    pending_reopen = db.scalar(
        select(ReopenRequest.id).where(
            ReopenRequest.track_id == track.id,
            ReopenRequest.status == ReopenRequestStatus.PENDING.value,
        )
    )
    if pending_reopen is not None:
        raise HTTPException(status_code=409, detail="A reopen request is already pending.")
    if pending_source_followup_request(db, track.id) is not None:
        raise HTTPException(status_code=409, detail="A source follow-up request is already pending.")


def delete_source_followup_draft(req: SourceFollowupRequest) -> None:
    try:
        if req.staged_storage_backend == "r2":
            from app.services.r2 import delete_object

            delete_object(req.staged_file_path)
        else:
            Path(req.staged_file_path).unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to delete source follow-up draft %s", req.staged_file_path, exc_info=True)


def create_source_followup_request(
    db: Session,
    *,
    album: Album,
    track: Track,
    current_user: User,
    background_tasks: BackgroundTasks,
    reason: str,
    file_path: str,
    storage_backend: str,
    duration: float | None,
) -> TrackRead:
    clean_reason = reason.strip()
    if not clean_reason:
        raise HTTPException(status_code=422, detail="A source follow-up reason is required.")

    ensure_source_followup_request_allowed(
        db,
        track=track,
        album=album,
        current_user=current_user,
    )

    previous_status = track.status
    req = SourceFollowupRequest(
        track_id=track.id,
        requested_by_id=current_user.id,
        previous_status=previous_status,
        reason=clean_reason,
        staged_file_path=file_path,
        staged_storage_backend=storage_backend,
        staged_duration=duration,
    )
    track.status = TrackStatus.SOURCE_FOLLOWUP_PENDING.value
    db.add(req)
    db.flush()

    log_track_event(
        db,
        track,
        current_user,
        "source_followup_requested",
        from_status=previous_status,
        to_status=track.status,
        payload={"request_id": req.id},
    )
    notify_targets = album_manager_user_ids(db, album)
    if album.mastering_engineer_id:
        notify_targets.add(album.mastering_engineer_id)
    notify_targets.discard(None)
    notify_targets.discard(current_user.id)
    if notify_targets:
        notify(
            db,
            list(notify_targets),
            "source_followup_request",
            "源音频补交申请",
            f"{current_user.display_name} 请求为「{track.title}」补交新的源音频。",
            related_track_id=track.id,
            background_tasks=background_tasks,
            album_id=track.album_id,
            webhook_context={"actor_id": current_user.id, "actor_name": current_user.display_name},
        )
    db.commit()
    db.refresh(track)
    broadcast_track_updated(background_tasks, track.id)
    return build_track_read(track, current_user, album, db=db)


def validate_source_followup_target(album: Album, target_stage_id: str):
    config = parse_workflow_config(album)
    steps = get_steps(config)
    target_step = get_step_by_id(steps, target_stage_id)
    if target_step is None:
        raise HTTPException(status_code=400, detail="Target stage is not part of this album workflow.")
    if target_step.type == "revision":
        raise HTTPException(status_code=400, detail="Source follow-up cannot target a revision stage.")
    if not should_new_cycle(steps, target_step):
        raise HTTPException(status_code=400, detail="Source follow-up must return before or at the first delivery stage.")
    return target_step


def ensure_source_followup_decider(
    *,
    album: Album,
    current_user: User,
    target_step: object | None = None,
) -> None:
    db = Session.object_session(album)
    if db is not None and is_album_manager(album, current_user, db):
        return
    if (
        target_step is not None
        and album.mastering_engineer_id == current_user.id
        and target_is_mastering_related(target_step)
    ):
        return
    if target_step is None and album.mastering_engineer_id == current_user.id:
        return
    raise HTTPException(status_code=403, detail="Only the producer or relevant mastering engineer can decide this request.")


def apply_source_followup_request(
    db: Session,
    *,
    album: Album,
    track: Track,
    req: SourceFollowupRequest,
    actor: User,
    target_stage_id: str,
    background_tasks: BackgroundTasks,
) -> None:
    target_step = validate_source_followup_target(album, target_stage_id)
    ensure_source_followup_decider(album=album, current_user=actor, target_step=target_step)

    execute_reopen(db, album, track, actor, target_stage_id, background_tasks)
    track.version += 1
    track.file_path = req.staged_file_path
    track.storage_backend = req.staged_storage_backend
    track.duration = req.staged_duration

    requester = db.get(User, req.requested_by_id) or actor
    source_version = create_source_version(
        track,
        requester,
        req.staged_file_path,
        req.staged_duration,
        revision_notes=req.reason,
        storage_backend=req.staged_storage_backend,
    )
    db.add(source_version)
    db.flush()

    req.status = SourceFollowupRequestStatus.APPLIED
    req.target_stage_id = target_stage_id
    req.decided_by_id = actor.id
    req.decided_at = datetime.now(timezone.utc)
    req.applied_source_version_id = source_version.id

    log_track_event(
        db,
        track,
        actor,
        "source_followup_applied",
        from_status=TrackStatus.SOURCE_FOLLOWUP_PENDING.value,
        to_status=track.status,
        payload={
            "request_id": req.id,
            "target_stage": target_stage_id,
            "version": track.version,
            "workflow_cycle": track.workflow_cycle,
        },
    )
