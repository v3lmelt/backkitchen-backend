from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy.orm import Session

from app.models.album import Album
from app.models.track import Track
from app.models.user import User
from app.workflow import log_track_event
from app.workflow_engine import (
    get_step_by_id,
    get_steps,
    parse_workflow_config,
    prepare_review_assignments_for_stage_entry,
)


def force_track_status(
    db: Session,
    album: Album,
    track: Track,
    actor: User,
    new_status: str,
    reason: str | None,
    background_tasks: BackgroundTasks,
    *,
    allowed_terminal_statuses: set[str],
    event_type: str,
) -> None:
    config = parse_workflow_config(album)
    valid_step_ids = {step["id"] for step in config.get("steps", [])}
    valid_statuses = valid_step_ids | allowed_terminal_statuses
    if new_status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Valid: {sorted(valid_statuses)}",
        )

    old_status = track.status
    track.status = new_status
    target_step = get_step_by_id(get_steps(config), new_status)
    if target_step is not None and target_step.type == "review":
        prepare_review_assignments_for_stage_entry(
            db,
            album,
            track,
            target_step.id,
            background_tasks,
            actor=actor,
        )
    log_track_event(
        db,
        track,
        actor,
        event_type,
        from_status=old_status,
        to_status=new_status,
        payload={"reason": reason},
    )
