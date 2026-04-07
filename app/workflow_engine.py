"""Generic workflow engine for custom per-album workflows.

This module handles albums that have a non-NULL ``workflow_config``.
Legacy albums (``workflow_config IS NULL``) continue to use the
hardcoded endpoints in ``routers/tracks.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy.orm import Session

from app.models.album import Album
from app.models.track import Track, TrackStatus, RejectionMode
from app.models.user import User
from app.notifications import notify
from app.workflow import assign_random_peer_reviewer, log_track_event
from app.workflow_defaults import DEFAULT_WORKFLOW_CONFIG, SPECIAL_TARGETS


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class StepDef:
    id: str
    label: str
    type: str  # gate | review | revision | delivery
    assignee_role: str
    order: int
    transitions: dict[str, str]
    return_to: str | None = None
    revision_step: str | None = None


@dataclass
class TransitionOption:
    decision: str
    target: str
    label: str


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def parse_workflow_config(album: Album) -> dict:
    """Return the parsed workflow config dict for an album."""
    if album.workflow_config:
        return json.loads(album.workflow_config)
    return DEFAULT_WORKFLOW_CONFIG


def get_steps(config: dict) -> list[StepDef]:
    """Parse step definitions from a workflow config dict."""
    return [
        StepDef(
            id=s["id"],
            label=s["label"],
            type=s["type"],
            assignee_role=s["assignee_role"],
            order=s["order"],
            transitions=s.get("transitions", {}),
            return_to=s.get("return_to"),
            revision_step=s.get("revision_step"),
        )
        for s in config["steps"]
    ]


def get_step_by_id(steps: list[StepDef], step_id: str) -> StepDef | None:
    """Find a step by its ID."""
    for s in steps:
        if s.id == step_id:
            return s
    return None


def get_current_step(config: dict, track: Track) -> StepDef | None:
    """Get the step definition matching the track's current status."""
    steps = get_steps(config)
    return get_step_by_id(steps, track.status)


# ---------------------------------------------------------------------------
# Role resolution
# ---------------------------------------------------------------------------

def resolve_assignee(album: Album, track: Track, role_spec: str) -> int | None:
    """Map a role string to a concrete user ID.

    Supported role specs:
    - ``"producer"`` → album.producer_id
    - ``"mastering_engineer"`` → album.mastering_engineer_id
    - ``"peer_reviewer"`` → track.peer_reviewer_id
    - ``"submitter"`` → track.submitter_id
    - ``"member:<user_id>"`` → the literal user ID
    """
    if role_spec == "producer":
        return album.producer_id
    if role_spec == "mastering_engineer":
        return album.mastering_engineer_id
    if role_spec == "peer_reviewer":
        return track.peer_reviewer_id
    if role_spec == "submitter":
        return track.submitter_id
    if role_spec.startswith("member:"):
        try:
            return int(role_spec.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
    return None


def user_matches_role(user: User, album: Album, track: Track, role_spec: str) -> bool:
    """Check whether a user satisfies a role spec."""
    assignee_id = resolve_assignee(album, track, role_spec)
    return assignee_id is not None and user.id == assignee_id


# ---------------------------------------------------------------------------
# Allowed transitions
# ---------------------------------------------------------------------------

def get_allowed_transitions(
    config: dict, track: Track, user: User, album: Album,
) -> list[TransitionOption]:
    """Return the transitions the current user may take on this track."""
    step = get_current_step(config, track)
    if step is None:
        return []

    if not user_matches_role(user, album, track, step.assignee_role):
        return []

    options: list[TransitionOption] = []
    for decision, target in step.transitions.items():
        label = decision.replace("_", " ").title()
        options.append(TransitionOption(decision=decision, target=target, label=label))

    # Revision steps: the implicit action is "upload" which is handled by
    # the source-version upload endpoint, not by this transition engine.
    # So we don't add transitions for revision steps here.

    return options


def get_allowed_action_names(
    config: dict, track: Track, user: User, album: Album,
) -> list[str]:
    """Return action names for the ``allowed_actions`` field on TrackRead."""
    transitions = get_allowed_transitions(config, track, user, album)
    actions = [t.decision for t in transitions]

    # For revision steps, add the implicit "upload_revision" action
    step = get_current_step(config, track)
    if step and step.type == "revision":
        is_submitter = track.submitter_id == user.id
        if is_submitter:
            actions.append("upload_revision")

    # For rejected + resubmittable tracks
    if (
        track.status == TrackStatus.REJECTED
        and track.rejection_mode == RejectionMode.RESUBMITTABLE
        and track.submitter_id == user.id
    ):
        actions.append("resubmit")

    return actions


# ---------------------------------------------------------------------------
# Transition execution
# ---------------------------------------------------------------------------

def _assign_peer_reviewer_if_needed(
    db: Session, album: Album, track: Track, target_step: StepDef,
) -> None:
    """If the target step needs a peer_reviewer and none is assigned, pick one."""
    if target_step.assignee_role == "peer_reviewer" and track.peer_reviewer_id is None:
        assign_random_peer_reviewer(db, album, track)


def execute_transition(
    db: Session,
    album: Album,
    track: Track,
    user: User,
    decision: str,
    background_tasks: BackgroundTasks,
) -> None:
    """Execute a workflow transition on a track with a custom workflow.

    Validates the decision, updates ``track.status``, logs the event,
    and sends notifications.

    Raises ``HTTPException`` if the transition is invalid.
    """
    config = parse_workflow_config(album)
    step = get_current_step(config, track)

    if step is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Track is in unknown state '{track.status}'.",
        )

    if not user_matches_role(user, album, track, step.assignee_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorised to act on this step.",
        )

    if decision not in step.transitions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid decision '{decision}' for step '{step.id}'. "
            f"Valid: {list(step.transitions.keys())}",
        )

    target = step.transitions[decision]
    previous_status = track.status
    steps = get_steps(config)

    # Handle special targets
    if target == "__completed":
        track.status = "completed"
    elif target == "__rejected":
        track.status = "rejected"
        track.rejection_mode = RejectionMode.FINAL
    elif target == "__rejected_resubmittable":
        track.status = "rejected"
        track.rejection_mode = RejectionMode.RESUBMITTABLE
    else:
        target_step = get_step_by_id(steps, target)
        if target_step is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Workflow config error: target step '{target}' not found.",
            )
        track.status = target
        # Auto-assign peer reviewer if needed
        _assign_peer_reviewer_if_needed(db, album, track, target_step)

    log_track_event(
        db, track, user,
        f"workflow_transition_{decision}",
        from_status=previous_status,
        to_status=track.status,
        payload={"step": step.id, "decision": decision, "target": target},
    )

    # Send notification to the assignee of the target step
    _notify_transition(db, album, track, step, target, steps, background_tasks)


def execute_revision_upload(
    album: Album,
    track: Track,
) -> str:
    """Resolve the next status after a source version upload for custom workflows.

    Returns the target step ID. Does NOT update track.status (caller does that).
    """
    config = parse_workflow_config(album)
    step = get_current_step(config, track)

    if step is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Track is in unknown state '{track.status}'.",
        )

    if step.type != "revision":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Track is not in a revision step (current: '{step.id}').",
        )

    if not step.return_to:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Revision step '{step.id}' has no return_to target.",
        )

    return step.return_to


def execute_delivery_upload(
    album: Album,
    track: Track,
) -> str:
    """Resolve the next status after a master delivery upload for custom workflows.

    Returns the target step ID by following the 'deliver' transition.
    """
    config = parse_workflow_config(album)
    step = get_current_step(config, track)

    if step is None or step.type != "delivery":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Track is not in a delivery step.",
        )

    deliver_target = step.transitions.get("deliver")
    if not deliver_target:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Delivery step '{step.id}' has no 'deliver' transition.",
        )

    if deliver_target == "__completed":
        return "completed"
    return deliver_target


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _notify_transition(
    db: Session,
    album: Album,
    track: Track,
    from_step: StepDef,
    target: str,
    steps: list[StepDef],
    background_tasks: BackgroundTasks,
) -> None:
    """Send a notification when a track transitions between steps."""
    if target in SPECIAL_TARGETS:
        # Notify submitter about completion/rejection
        target_label = {
            "__completed": "completed",
            "__rejected": "rejected",
            "__rejected_resubmittable": "rejected (resubmittable)",
        }.get(target, target)
        notify(
            db,
            [track.submitter_id],
            "track_status_changed",
            f"Track status: {target_label}",
            f"「{track.title}」has been {target_label}.",
            related_track_id=track.id,
            background_tasks=background_tasks,
            album_id=track.album_id,
        )
        return

    target_step = get_step_by_id(steps, target)
    if target_step is None:
        return

    # Notify the assignee of the target step
    assignee_id = resolve_assignee(album, track, target_step.assignee_role)
    if assignee_id:
        notify(
            db,
            [assignee_id],
            "track_status_changed",
            f"Track moved to: {target_step.label}",
            f"「{track.title}」has moved to '{target_step.label}'.",
            related_track_id=track.id,
            background_tasks=background_tasks,
            album_id=track.album_id,
        )
