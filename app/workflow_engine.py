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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.album import Album
from app.models.checklist import ChecklistItem
from app.models.stage_assignment import StageAssignment
from app.models.track import Track, TrackStatus, RejectionMode, WorkflowVariant
from app.models.user import User
from app.notifications import notify
from app.workflow import assign_random_peer_reviewer, current_source_version, log_track_event
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

    Handles v1→v2 migration on the fly (normalises ``gate`` → ``approval``).
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
    if album.workflow_config:
        config = parse_workflow_config(album)
        return get_first_step(config).id
    return TrackStatus.SUBMITTED


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
    """
    if step.assignee_user_id:
        return user.id == step.assignee_user_id
    assignee_id = resolve_assignee(album, track, step.assignee_role)
    return assignee_id is not None and user.id == assignee_id


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
        return selected

    # Manual mode — no auto-assignment
    return []


def assign_peer_reviewer_for_step(
    db: Session, album: Album, track: Track, step: StepDef,
    background_tasks: BackgroundTasks | None = None,
) -> None:
    """Handle reviewer assignment when entering a review step.

    For legacy ``peer_reviewer`` role, uses the existing random assignment.
    For custom review steps, uses the new assignment system.
    """
    if step.type != "review":
        return

    # Step-level assignee_user_id override takes precedence over all other modes.
    if step.assignee_user_id:
        track.peer_reviewer_id = step.assignee_user_id
        return

    if step.assignee_role == "peer_reviewer" and step.assignment_mode != "auto":
        # Legacy-compatible: use random assignment if no pool configured
        if track.peer_reviewer_id is None:
            assign_random_peer_reviewer(db, album, track)
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
# One-time config upgrade for stored workflow configs
# ---------------------------------------------------------------------------


def upgrade_config_with_backward_transitions(config: dict) -> tuple[dict, list[str]]:
    """Surgically upgrade a stored workflow config with compatibility fixes.

    Adds ``reject_to_*`` transitions so each approval/delivery stage can
    send the track back to an earlier stage for re-review without requiring
    a new source upload. Also backfills the intake-stage
    ``accept_producer_direct`` transition for the producer-direct path and
    normalises older ``approve`` naming on the first intake step. Finally,
    fixes the broken ``final_revision`` stage
    (assigned to mastering_engineer but uploaded via ``/source-versions``)
    by rewriting ``final_review.reject`` to ``mastering_revision`` and
    removing ``final_revision`` entirely.

    Only steps with well-known default IDs are touched; custom stages are
    preserved as-is. Returns the (possibly mutated) config dict and a
    list of human-readable descriptions of the changes applied.
    """
    changes: list[str] = []
    steps = config.get("steps", [])
    step_ids = {s.get("id") for s in steps}

    # 1. Intake: normalise the forward transition and backfill
    #    producer-direct intake on classic/default workflows.
    for step in steps:
        step_id = step.get("id")
        if step_id not in {"intake", "submitted"}:
            continue

        transitions = step.setdefault("transitions", {})
        accept_target = transitions.get("accept")
        approve_target = transitions.get("approve")
        if accept_target is None and approve_target:
            transitions["accept"] = approve_target
            del transitions["approve"]
            changes.append(f"{step_id}: renamed approve to accept")
            accept_target = approve_target

        if accept_target != "peer_review":
            continue

        producer_gate_target = None
        if "producer_gate" in step_ids:
            producer_gate_target = "producer_gate"
        elif "producer_mastering_gate" in step_ids:
            producer_gate_target = "producer_mastering_gate"

        if producer_gate_target and "accept_producer_direct" not in transitions:
            transitions["accept_producer_direct"] = producer_gate_target
            changes.append(
                f"{step_id}: added accept_producer_direct to {producer_gate_target}"
            )

    # 2. producer_gate: add reject_to_peer_review
    if "producer_gate" in step_ids and "peer_review" in step_ids:
        producer_gate = next(s for s in steps if s.get("id") == "producer_gate")
        transitions = producer_gate.setdefault("transitions", {})
        if "reject_to_peer_review" not in transitions:
            transitions["reject_to_peer_review"] = "peer_review"
            changes.append("producer_gate: added reject_to_peer_review")

    # 3. mastering: add reject_to_producer_gate
    if "mastering" in step_ids and "producer_gate" in step_ids:
        mastering = next(s for s in steps if s.get("id") == "mastering")
        transitions = mastering.setdefault("transitions", {})
        if "reject_to_producer_gate" not in transitions:
            transitions["reject_to_producer_gate"] = "producer_gate"
            changes.append("mastering: added reject_to_producer_gate")

    # 4. final_review: add reject_to_mastering for backward rollback, and
    #    fix broken final_revision wiring. The legacy 'approve → __completed'
    #    transition is left intact — removing it was an old mistake that
    #    stranded workflows without any completion path (runtime schema
    #    validator still requires one of: __completed transition, or a
    #    final_review step). final_review completes via the dedicated
    #    /final-review/approve dual-confirmation endpoint regardless.
    if "final_review" in step_ids and "mastering" in step_ids:
        final_review = next(s for s in steps if s.get("id") == "final_review")
        transitions = final_review.setdefault("transitions", {})
        if "reject_to_mastering" not in transitions:
            transitions["reject_to_mastering"] = "mastering"
            changes.append("final_review: added reject_to_mastering")
        # Repoint final_review.reject → mastering_revision (was final_revision)
        if (
            transitions.get("reject") == "final_revision"
            and "mastering_revision" in step_ids
        ):
            transitions["reject"] = "mastering_revision"
            changes.append("final_review: rewrote reject → mastering_revision")

    # 5. Drop the broken final_revision step (it was assigned to
    #    mastering_engineer but uploaded via /source-versions, which is
    #    semantically nonsense — source uploads are done by the submitter).
    if "final_revision" in step_ids:
        config["steps"] = [s for s in steps if s.get("id") != "final_revision"]
        changes.append("removed broken final_revision step")

    return config, changes


def _album_has_active_tracks_in_step(db: Session, album_id: int, step_id: str) -> bool:
    """True if any active (non-archived) track is currently in ``step_id``."""
    row = db.scalar(
        select(Track.id).where(
            Track.album_id == album_id,
            Track.status == step_id,
            Track.archived_at.is_(None),
        ).limit(1)
    )
    return row is not None


def backfill_album_workflow_configs(db: Session) -> int:
    """Apply stored-workflow compatibility upgrades to existing albums.

    Scans all albums with a stored ``workflow_config`` and applies the
    surgical upgrades from ``upgrade_config_with_backward_transitions``.
    Skips albums where a track is currently sitting in ``final_revision``
    (dropping the step would strand the track); those need manual editing
    in the workflow builder.

    Returns the number of albums that were actually updated.
    """
    albums = db.scalars(
        select(Album).where(Album.workflow_config.isnot(None))
    ).all()

    updated_count = 0
    for album in albums:
        try:
            original_config = json.loads(album.workflow_config)
        except (ValueError, TypeError):
            logger.warning("Album %d has invalid workflow_config JSON; skipping.", album.id)
            continue

        # Defensive copy so we can bail out cleanly if anything goes wrong
        config_copy = json.loads(json.dumps(original_config))

        # Skip the final_revision cleanup if any track is currently stuck there
        if _album_has_active_tracks_in_step(db, album.id, "final_revision"):
            logger.warning(
                "Album %d has active tracks in 'final_revision'; skipping config upgrade.",
                album.id,
            )
            continue

        upgraded, changes = upgrade_config_with_backward_transitions(config_copy)
        if not changes:
            continue

        album.workflow_config = json.dumps(upgraded)
        updated_count += 1
        logger.info(
            "Album %d workflow_config upgraded: %s",
            album.id,
            "; ".join(changes),
        )

    if updated_count:
        db.flush()

    return updated_count


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
