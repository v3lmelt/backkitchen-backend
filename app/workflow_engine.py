"""Generic workflow engine for custom per-album workflows.

This module handles albums that have a non-NULL ``workflow_config``.
Legacy albums (``workflow_config IS NULL``) continue to use the
hardcoded endpoints in ``routers/tracks.py``.

Version 2 of the config schema introduces:
* ``approval`` step type (replaces ``gate``)
* ``producer_revision`` / ``final_revision`` loop stages
* Per-step config: ``assignment_mode``, ``required_reviewer_count``,
  ``allow_permanent_reject``, ``require_confirmation``, etc.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.checklist import ChecklistItem
from app.models.stage_assignment import StageAssignment
from app.models.track import Track, TrackStatus, RejectionMode, WorkflowVariant
from app.models.user import User
from app.notifications import notify
from app.workflow import current_source_version, log_track_event
from app.workflow_defaults import DEFAULT_WORKFLOW_CONFIG, SPECIAL_TARGETS, STEP_TYPE_ALIASES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class StepDef:
    id: str
    label: str
    type: str  # approval | review | revision | delivery
    ui_variant: str | None
    assignee_role: str
    order: int
    transitions: dict[str, str] = field(default_factory=dict)
    return_to: str | None = None
    revision_step: str | None = None
    # Approval-specific
    allow_permanent_reject: bool = False
    # Review-specific
    assignment_mode: str = "manual"  # "manual" | "auto"
    reviewer_pool: list[int] | None = None
    required_reviewer_count: int = 1
    # Approval/delivery assignee override
    assignee_user_id: int | None = None
    # Delivery-specific
    require_confirmation: bool = False
    # Additional roles that may act on this step (beyond assignee_role)
    actor_roles: list[str] | None = None


@dataclass
class TransitionOption:
    decision: str
    target: str
    label: str


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def parse_workflow_config(album: Album) -> dict:
    """Return the parsed workflow config dict for an album.

    Every album is expected to have a non-NULL ``workflow_config`` (the
    album create endpoint always assigns :data:`DEFAULT_WORKFLOW_CONFIG`
    when the caller does not provide one). The built-in default is used as
    a last-resort fallback for any lingering edge case. Handles v1→v2
    migration on the fly (normalises ``gate`` → ``approval``).
    """
    if album.workflow_config:
        config = json.loads(album.workflow_config)
    else:
        config = DEFAULT_WORKFLOW_CONFIG

    # Transparently upgrade v1 configs
    if config.get("version", 1) < 2:
        for step in config.get("steps", []):
            raw_type = step.get("type", "")
            step["type"] = STEP_TYPE_ALIASES.get(raw_type, raw_type)
        config["version"] = 2

    return config


def get_steps(config: dict) -> list[StepDef]:
    """Parse step definitions from a workflow config dict."""
    return [
        StepDef(
            id=s["id"],
            label=s["label"],
            type=STEP_TYPE_ALIASES.get(s["type"], s["type"]),
            ui_variant=s.get("ui_variant"),
            assignee_role=s["assignee_role"],
            order=s["order"],
            transitions=s.get("transitions", {}),
            return_to=s.get("return_to"),
            revision_step=s.get("revision_step"),
            allow_permanent_reject=s.get("allow_permanent_reject", False),
            assignment_mode=s.get("assignment_mode", "manual"),
            reviewer_pool=s.get("reviewer_pool"),
            required_reviewer_count=s.get("required_reviewer_count", 1),
            assignee_user_id=s.get("assignee_user_id"),
            require_confirmation=s.get("require_confirmation", False),
            actor_roles=s.get("actor_roles"),
        )
        for s in config["steps"]
    ]


def get_step_by_id(steps: list[StepDef], step_id: str) -> StepDef | None:
    for s in steps:
        if s.id == step_id:
            return s
    return None


def infer_issue_phase_for_step(step: StepDef) -> str:
    """Map workflow step metadata to a canonical issue phase.

    Falls back to step id for unknown custom stages so that issue records
    can still be scoped to the active workflow step.
    """
    if step.ui_variant == "peer_review" or step.id == "peer_review":
        return "peer"
    if step.ui_variant == "producer_gate" or step.id == "producer_gate":
        return "producer"
    if step.ui_variant == "mastering" or step.id == "mastering":
        return "mastering"
    if step.ui_variant == "final_review" or step.id == "final_review":
        return "final_review"
    return step.id


def get_current_step(config: dict, track: Track) -> StepDef | None:
    steps = get_steps(config)
    return get_step_by_id(steps, track.status)


def get_first_step(config: dict) -> StepDef:
    """Return the first step (lowest order) of the workflow."""
    steps = get_steps(config)
    return min(steps, key=lambda s: s.order)


def get_initial_track_status(album: Album) -> str:
    """Return the initial status for a new track in this album."""
    config = parse_workflow_config(album)
    return get_first_step(config).id


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


def user_matches_role(user: User, album: Album, track: Track, step: StepDef) -> bool:
    """Check whether a user may act on a step.

    For steps with ``assignee_user_id`` override, use that directly.
    For review steps, check ``StageAssignment`` (handled at call site).
    Otherwise, fall back to role-based matching.
    Also checks ``actor_roles`` for steps that allow multiple roles to act
    (e.g. final_review where both producer and submitter can reject).
    """
    if step.assignee_user_id:
        if user.id == step.assignee_user_id:
            return True
    assignee_id = resolve_assignee(album, track, step.assignee_role)
    if assignee_id is not None and user.id == assignee_id:
        return True
    if step.actor_roles:
        for role in step.actor_roles:
            role_id = resolve_assignee(album, track, role)
            if role_id is not None and user.id == role_id:
                return True
    return False


def user_matches_role_or_assignment(
    user: User, album: Album, track: Track, step: StepDef, db: Session,
) -> bool:
    """Like user_matches_role but also checks StageAssignment for review steps."""
    if step.type == "review":
        # Check if user has an active assignment for this track+stage
        assignment = db.scalar(
            select(StageAssignment).where(
                StageAssignment.track_id == track.id,
                StageAssignment.stage_id == step.id,
                StageAssignment.user_id == user.id,
                StageAssignment.status == "pending",
            )
        )
        if assignment:
            return True
    return user_matches_role(user, album, track, step)


# ---------------------------------------------------------------------------
# Review assignment
# ---------------------------------------------------------------------------


def assign_reviewers(
    db: Session,
    album: Album,
    track: Track,
    step: StepDef,
    background_tasks: BackgroundTasks | None = None,
) -> list[int]:
    """Assign reviewers for a review step.

    Returns list of assigned user IDs.  For ``auto`` mode, uses load-balanced
    selection from the reviewer pool.  Falls back to manual (notify producer)
    if the pool is insufficient.
    """
    if step.type != "review":
        return []

    count = step.required_reviewer_count

    if step.assignment_mode == "auto":
        pool = step.reviewer_pool or []
        # Exclude track author from pool
        candidates = [uid for uid in pool if uid != track.submitter_id]

        if len(candidates) < count:
            # Fallback to manual — notify producer
            logger.warning(
                "Auto-assign pool insufficient for track %d step '%s' "
                "(need %d, have %d). Falling back to manual.",
                track.id, step.id, count, len(candidates),
            )
            if background_tasks and album.producer_id:
                step_label = _step_label_zh(step)
                notify(
                    db, [album.producer_id],
                    "reviewer_assignment_needed",
                    "需要手动指派评审人",
                    f"「{track.title}」在「{step_label}」阶段无法自动分配评审人"
                    "，请手动指派。",
                    related_track_id=track.id,
                    background_tasks=background_tasks,
                    album_id=track.album_id,
                )
            return []

        # Load-balanced: pick candidates with fewest pending assignments
        pending_counts = dict(
            db.execute(
                select(
                    StageAssignment.user_id,
                    func.count(StageAssignment.id),
                ).where(
                    StageAssignment.user_id.in_(candidates),
                    StageAssignment.status == "pending",
                ).group_by(StageAssignment.user_id)
            ).all()
        )
        # Sort by load (ascending), then pick top N
        candidates.sort(key=lambda uid: pending_counts.get(uid, 0))
        selected = candidates[:count]

        now = datetime.now(timezone.utc)
        for uid in selected:
            db.add(StageAssignment(
                track_id=track.id,
                stage_id=step.id,
                user_id=uid,
                status="pending",
                assigned_at=now,
            ))
        # Notify assigned reviewers
        step_label = _step_label_zh(step)
        notify(
            db, selected,
            "reviewer_assigned",
            "你被指派为评审人",
            f"你已被自动分配评审「{track.title}」（{step_label}）",
            related_track_id=track.id,
            background_tasks=background_tasks,
            album_id=track.album_id,
        )
        return selected

    # Manual mode — no auto-assignment
    return []


def _assign_random_peer_reviewer(db: Session, album: Album, track: Track) -> int:
    """Fallback reviewer picker for manual-mode peer_review steps.

    Chooses a random album member (excluding the submitter and the mastering
    engineer) and sets it as ``track.peer_reviewer_id``. Used when a review
    step is wired to ``peer_reviewer`` role in manual mode and no reviewer
    was explicitly assigned by the producer yet.
    """
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


def assign_peer_reviewer_for_step(
    db: Session, album: Album, track: Track, step: StepDef,
    background_tasks: BackgroundTasks | None = None,
) -> None:
    """Handle reviewer assignment when entering a review step.

    For ``peer_reviewer`` role in manual mode, picks a random album member
    as a fallback if no reviewer has been explicitly assigned. For other
    configurations, uses the custom reviewer assignment system.
    """
    if step.type != "review":
        return

    # Step-level assignee_user_id override takes precedence over all other modes.
    if step.assignee_user_id:
        track.peer_reviewer_id = step.assignee_user_id
        notify(
            db, [step.assignee_user_id],
            "reviewer_assigned", "你被指派为评审人",
            f"你已被指派评审「{track.title}」",
            related_track_id=track.id,
            background_tasks=background_tasks,
            album_id=track.album_id,
        )
        return

    if step.assignee_role == "peer_reviewer" and step.assignment_mode != "auto":
        if track.peer_reviewer_id is None:
            assigned_id = _assign_random_peer_reviewer(db, album, track)
            notify(
                db, [assigned_id],
                "reviewer_assigned", "你被指派为评审人",
                f"你已被指派评审「{track.title}」",
                related_track_id=track.id,
                background_tasks=background_tasks,
                album_id=track.album_id,
            )
        return

    assigned = assign_reviewers(db, album, track, step, background_tasks)
    # For peer_reviewer role, also set the first assignee on the track
    # for backward compatibility with existing UI
    if step.assignee_role == "peer_reviewer" and assigned and not track.peer_reviewer_id:
        track.peer_reviewer_id = assigned[0]


# ---------------------------------------------------------------------------
# Allowed transitions
# ---------------------------------------------------------------------------


def get_allowed_transitions(
    config: dict, track: Track, user: User, album: Album,
    db: Session | None = None,
) -> list[TransitionOption]:
    """Return the transitions the current user may take on this track."""
    step = get_current_step(config, track)
    if step is None:
        return []

    # Check role — with assignment awareness for review steps
    if db and step.type == "review":
        if not user_matches_role_or_assignment(user, album, track, step, db):
            return []
    elif not user_matches_role(user, album, track, step):
        return []

    options: list[TransitionOption] = []
    for decision, target in step.transitions.items():
        label = decision.replace("_", " ").title()
        options.append(TransitionOption(decision=decision, target=target, label=label))

    return options


def get_allowed_action_names(
    config: dict, track: Track, user: User, album: Album,
    db: Session | None = None,
) -> list[str]:
    """Return action names for the ``allowed_actions`` field on TrackRead."""
    transitions = get_allowed_transitions(config, track, user, album, db=db)
    actions = [t.decision for t in transitions]

    # For revision steps, add the implicit "upload_revision" action
    step = get_current_step(config, track)
    if step and step.type == "revision":
        is_submitter = track.submitter_id == user.id
        # For mastering engineer revision steps
        is_me = album.mastering_engineer_id == user.id
        if step.assignee_role == "submitter" and is_submitter:
            actions.append("upload_revision")
        elif step.assignee_role == "mastering_engineer" and is_me:
            actions.append("upload_revision")

    # For delivery steps with unconfirmed delivery, show confirm action
    if step and step.type == "delivery" and step.require_confirmation:
        assignee_id = step.assignee_user_id or resolve_assignee(album, track, step.assignee_role)
        if user.id == assignee_id:
            from app.models.master_delivery import MasterDelivery
            # Check for unconfirmed delivery
            if db:
                unconfirmed = db.scalar(
                    select(MasterDelivery).where(
                        MasterDelivery.track_id == track.id,
                        MasterDelivery.workflow_cycle == track.workflow_cycle,
                        MasterDelivery.confirmed_at.is_(None),
                    ).order_by(MasterDelivery.delivery_number.desc())
                )
                if unconfirmed:
                    actions.append("confirm_delivery")

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
    """
    config = parse_workflow_config(album)
    step = get_current_step(config, track)

    if step is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Track is in unknown state '{track.status}'.",
        )

    # Validate permissions — with assignment-aware check for review steps
    if step.type == "review":
        if not user_matches_role_or_assignment(user, album, track, step, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not authorised to act on this step.",
            )
    elif not user_matches_role(user, album, track, step):
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

    if step.type == "review" and (step.ui_variant == "peer_review" or step.id == "peer_review"):
        source_version = current_source_version(track)
        if source_version is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No source version found.",
            )
        checklist_count = db.scalar(
            select(func.count(ChecklistItem.id)).where(
                ChecklistItem.track_id == track.id,
                ChecklistItem.reviewer_id == user.id,
                ChecklistItem.source_version_id == source_version.id,
            )
        )
        if not checklist_count:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Submit the peer review checklist before finishing the review.",
            )

    target = step.transitions[decision]
    previous_status = track.status
    steps = get_steps(config)

    # Producer-direct intake: skip peer review, mark variant accordingly
    if decision == "accept_producer_direct":
        track.workflow_variant = WorkflowVariant.PRODUCER_DIRECT.value
        track.peer_reviewer_id = None
        track.rejection_mode = None

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
        # Backward ``reject_to_*`` transitions should preserve the original
        # reviewer instead of re-running auto-assignment: reopen any
        # StageAssignment records for the target step so the same user(s)
        # can re-review without load-balancing picking a different reviewer.
        # Only fall through to fresh assignment if the target step never had
        # an assignment history (edge case, shouldn't happen in practice).
        if decision.startswith("reject_to_") and target_step.type == "review":
            existing_assignments = db.scalars(
                select(StageAssignment).where(
                    StageAssignment.track_id == track.id,
                    StageAssignment.stage_id == target,
                )
            ).all()
            if existing_assignments:
                for assignment in existing_assignments:
                    if assignment.status == "completed":
                        assignment.status = "pending"
                        assignment.completed_at = None
            else:
                assign_peer_reviewer_for_step(db, album, track, target_step, background_tasks)
        else:
            # Forward: auto-assign reviewers if entering a review step
            assign_peer_reviewer_for_step(db, album, track, target_step, background_tasks)

    # Mark the user's stage assignment as completed (for review steps)
    if step.type == "review":
        assignment = db.scalar(
            select(StageAssignment).where(
                StageAssignment.track_id == track.id,
                StageAssignment.stage_id == step.id,
                StageAssignment.user_id == user.id,
                StageAssignment.status == "pending",
            )
        )
        if assignment:
            assignment.status = "completed"
            assignment.completed_at = datetime.now(timezone.utc)

    log_track_event(
        db, track, user,
        f"workflow_transition_{decision}",
        from_status=previous_status,
        to_status=track.status,
        payload={"step": step.id, "decision": decision, "target": target},
    )

    _notify_transition(db, album, track, step, target, steps, background_tasks)


def execute_revision_upload(
    album: Album,
    track: Track,
) -> str:
    """Resolve the next status after a source version upload.

    Returns the target step ID. Does NOT update track.status (caller does that).

    Two valid cases:
    1. Track is in a ``revision`` step → return ``step.return_to``.
    2. Track is in ``rejected`` with ``RESUBMITTABLE`` mode → return the first
       step (caller is responsible for resetting rejection_mode/cycle).
    """
    # Resubmit path: a finally-rejected-but-resubmittable track re-enters the
    # workflow at the first step.
    if (
        track.status == TrackStatus.REJECTED
        and track.rejection_mode == RejectionMode.RESUBMITTABLE
    ):
        config = parse_workflow_config(album)
        return get_first_step(config).id

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
            detail="This track is not waiting for a new source version.",
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
) -> str | None:
    """Resolve the next status after a master delivery upload.

    Returns the target step ID, or ``None`` if the delivery step has
    ``require_confirmation=True`` (track stays at current step until confirmed).
    """
    config = parse_workflow_config(album)
    step = get_current_step(config, track)

    if step is None or step.type != "delivery":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Track is not in a delivery step.",
        )

    # If confirmation required, don't advance yet
    if step.require_confirmation:
        return None

    deliver_target = step.transitions.get("deliver")
    if not deliver_target:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Delivery step '{step.id}' has no 'deliver' transition.",
        )

    if deliver_target == "__completed":
        return "completed"
    return deliver_target


def execute_delivery_confirm(
    album: Album,
    track: Track,
) -> str:
    """Resolve the next status after delivery confirmation.

    Called when the mastering engineer confirms their upload.
    Returns the target step ID.
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
# Track migration on workflow change
# ---------------------------------------------------------------------------


def migrate_tracks_on_workflow_change(
    db: Session,
    album: Album,
    old_config: dict,
    new_config: dict,
    background_tasks: BackgroundTasks | None = None,
) -> list[dict[str, Any]]:
    """Migrate active tracks when an album's workflow config changes.

    For each track whose current step no longer exists in the new config,
    rolls it back to the nearest preceding step that still exists.
    Returns a list of migration actions taken (for logging/response).
    """
    old_steps = get_steps(old_config)
    new_steps = get_steps(new_config)
    new_step_ids = {s.id for s in new_steps}

    # Build order map for old config
    old_order_map = {s.id: s.order for s in old_steps}

    # Active tracks: not completed, not rejected, not archived
    tracks = db.scalars(
        select(Track).where(
            Track.album_id == album.id,
            Track.archived_at.is_(None),
            Track.status.notin_(["completed", "rejected"]),
        )
    ).all()

    migrations: list[dict[str, Any]] = []

    for track in tracks:
        if track.status in new_step_ids:
            continue  # Step still exists, no migration needed

        old_order = old_order_map.get(track.status)
        if old_order is None:
            # Status doesn't match any old step either — reset to first step
            target = min(new_steps, key=lambda s: s.order)
        else:
            # Find the highest-order new step that comes before the old position
            candidates = [s for s in new_steps if s.order < old_order]
            if candidates:
                target = max(candidates, key=lambda s: s.order)
            else:
                target = min(new_steps, key=lambda s: s.order)

        previous_status = track.status
        track.status = target.id

        log_track_event(
            db, track, None,
            "workflow_migration",
            from_status=previous_status,
            to_status=track.status,
            payload={
                "reason": "workflow_config_changed",
                "removed_step": previous_status,
                "migrated_to": target.id,
            },
        )

        migrations.append({
            "track_id": track.id,
            "track_title": track.title,
            "from_step": previous_status,
            "to_step": target.id,
        })

    # Re-attempt assignment for any track currently in a review step whose
    # effective assignee is inconsistent with the new config.  This covers:
    #   - auto mode: pool was empty before; producer just added reviewers
    #   - assignee_user_id override: producer switched to a specific reviewer
    #   - manual/legacy: peer_reviewer_id not yet set
    new_steps_by_id = {s.id: s for s in new_steps}
    for track in tracks:
        current_step = new_steps_by_id.get(track.status)
        if current_step is None or current_step.type != "review":
            continue

        if current_step.assignment_mode == "auto":
            existing = db.scalar(
                select(func.count(StageAssignment.id)).where(
                    StageAssignment.track_id == track.id,
                    StageAssignment.stage_id == current_step.id,
                    StageAssignment.status == "pending",
                )
            )
            needs_reassignment = not existing
        elif current_step.assignee_user_id:
            needs_reassignment = track.peer_reviewer_id != current_step.assignee_user_id
        else:
            needs_reassignment = track.peer_reviewer_id is None

        if needs_reassignment:
            assign_peer_reviewer_for_step(db, album, track, current_step, background_tasks)

    return migrations


# ---------------------------------------------------------------------------
# Reopen
# ---------------------------------------------------------------------------


def execute_reopen(
    db: Session,
    album: Album,
    track: Track,
    actor: User,
    target_stage_id: str,
    background_tasks: BackgroundTasks | None = None,
) -> None:
    """Reopen a completed track to a specific stage.

    Increments ``workflow_cycle`` and sets ``track.status`` to the target stage.
    """
    config = parse_workflow_config(album)
    steps = get_steps(config)
    target_step = get_step_by_id(steps, target_stage_id)

    if target_step is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Target stage '{target_stage_id}' not found in workflow.",
        )

    previous_status = track.status
    track.status = target_stage_id
    track.workflow_cycle += 1
    track.rejection_mode = None

    log_track_event(
        db, track, actor,
        "track_reopened",
        from_status=previous_status,
        to_status=track.status,
        payload={
            "target_stage": target_stage_id,
            "new_cycle": track.workflow_cycle,
        },
    )

    # Auto-assign if entering a review step
    assign_peer_reviewer_for_step(db, album, track, target_step, background_tasks)

    # Notify relevant parties
    notify_targets = {track.submitter_id, album.producer_id}
    if album.mastering_engineer_id:
        notify_targets.add(album.mastering_engineer_id)
    notify_targets.discard(actor.id)

    if background_tasks:
        target_label = _step_label_zh(target_step)
        notify(
            db, list(notify_targets),
            "track_reopened",
            "曲目已重新开启",
            f"「{track.title}」已被重新开启到「{target_label}」（第 {track.workflow_cycle} 轮）。",
            related_track_id=track.id,
            background_tasks=background_tasks,
            album_id=track.album_id,
        )


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


# Default workflow step IDs → Chinese labels for in-app notifications.
# Custom (user-defined) steps fall back to their stored ``label``.
_DEFAULT_STEP_LABELS_ZH: dict[str, str] = {
    "intake": "接收审核",
    "peer_review": "同行评审",
    "peer_revision": "同行修订",
    "producer_gate": "制作人审核",
    "producer_revision": "制作人修订",
    "mastering": "母带制作",
    "mastering_revision": "母带修订",
    "final_review": "终审",
    "final_revision": "终审修订",
}


def _step_label_zh(step: StepDef) -> str:
    """Translate a step label to Chinese for notifications.

    Falls back to the raw ``step.label`` for custom steps that aren't part
    of the default workflow.
    """
    return _DEFAULT_STEP_LABELS_ZH.get(step.id, step.label)


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
        target_title, target_body = {
            "__completed": ("曲目已完成", f"「{track.title}」已通过所有审核。"),
            "__rejected": ("曲目已被拒绝", f"「{track.title}」已被拒绝。"),
            "__rejected_resubmittable": (
                "曲目已被退回",
                f"「{track.title}」已被退回，可以重新提交。",
            ),
        }.get(target, ("曲目状态变更", f"「{track.title}」状态已更新。"))
        notify(
            db,
            [track.submitter_id],
            "track_status_changed",
            target_title,
            target_body,
            related_track_id=track.id,
            background_tasks=background_tasks,
            album_id=track.album_id,
        )
        return

    target_step = get_step_by_id(steps, target)
    if target_step is None:
        return

    # Notify the assignee of the target step
    assignee_id = (
        target_step.assignee_user_id
        or resolve_assignee(album, track, target_step.assignee_role)
    )
    if assignee_id:
        target_label = _step_label_zh(target_step)
        notify(
            db,
            [assignee_id],
            "track_status_changed",
            f"曲目进入「{target_label}」",
            f"「{track.title}」已进入「{target_label}」阶段。",
            related_track_id=track.id,
            background_tasks=background_tasks,
            album_id=track.album_id,
        )
