"""Shared track/album query and event-logging helpers.

These helpers sit at the bottom of the import graph: they depend only on the
ORM models and SQLAlchemy, so both ``app.workflow_engine`` and the
permission/serializer modules can import them at module top level without
creating an import cycle.
"""

import json
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.issue import Issue
from app.models.master_delivery import MasterDelivery
from app.models.source_followup_request import SourceFollowupRequest, SourceFollowupRequestStatus
from app.models.stage_assignment import StageAssignment, StageAssignmentStatus
from app.models.track import Track, TrackStatus
from app.models.track_composer import TrackComposer, TrackExternalComposer
from app.models.track_source_version import TrackSourceVersion
from app.models.user import User
from app.models.workflow_event import WorkflowEvent

ASSIGNMENT_ACTIVE_STATUSES = (StageAssignmentStatus.PENDING.value, StageAssignmentStatus.COMPLETED.value)
ASSIGNMENT_CANCEL_REASON_QUORUM_MET = "quorum_met"
ASSIGNMENT_CANCEL_REASON_REASSIGNED = "reassigned"
ASSIGNMENT_CANCEL_REASON_SUPERSEDED = "superseded"
ASSIGNMENT_CANCEL_REASON_REVISION_REQUESTED = "revision_requested"


def engaged_assignment_status_clause():
    """SQL clause matching stage assignments that still count as participants.

    Covers active (pending/completed) assignments plus assignments that were
    cancelled only because a revision was requested (the reviewer stays
    involved with the track).
    """
    return or_(
        StageAssignment.status.in_(ASSIGNMENT_ACTIVE_STATUSES),
        (StageAssignment.status == StageAssignmentStatus.CANCELLED.value)
        & (StageAssignment.cancellation_reason == ASSIGNMENT_CANCEL_REASON_REVISION_REQUESTED),
    )


def next_issue_local_number(db: Session, track_id: int) -> int:
    """Allocate the next per-track issue number (1-based, monotonic, gaps preserved)."""
    current = db.scalar(
        select(func.coalesce(func.max(Issue.local_number), 0)).where(
            Issue.track_id == track_id
        )
    )
    return (current or 0) + 1


def get_album_member_ids(db: Session, album_id: int) -> set[int]:
    rows = db.scalars(select(AlbumMember).where(AlbumMember.album_id == album_id)).all()
    return {row.user_id for row in rows}


def get_all_album_member_ids(db: Session, album_id: int | None = None) -> dict[int, set[int]]:
    """Batch-fetch album→member mappings in a single query.

    When ``album_id`` is provided only that album's members are loaded,
    avoiding a full-table scan when the caller only needs one album.
    """
    stmt = select(AlbumMember)
    if album_id is not None:
        stmt = stmt.where(AlbumMember.album_id == album_id)
    result: dict[int, set[int]] = {}
    for m in db.scalars(stmt).all():
        result.setdefault(m.album_id, set()).add(m.user_id)
    return result


def album_tracks_completed(total: int, completed: int) -> bool:
    """Evaluate completion using counts of non-archived, non-rejected tracks."""
    return total > 0 and total == completed


def is_album_completed(db: Session, album_id: int) -> bool:
    """True when every active (non-archived, non-rejected) track is completed."""
    total, completed = db.execute(
        select(
            func.count(Track.id),
            func.count(case((Track.status == TrackStatus.COMPLETED, Track.id))),
        ).where(
            Track.album_id == album_id,
            Track.archived_at.is_(None),
            Track.status != TrackStatus.REJECTED,
        )
    ).one()
    return album_tracks_completed(total, completed)


def track_composer_ordered_ids(track: Track, db: Session | None = None) -> list[int]:
    ordered: list[int] = []
    seen: set[int] = set()

    def add(user_id: int | None) -> None:
        if user_id is None or user_id in seen:
            return
        seen.add(user_id)
        ordered.append(user_id)

    # Reuse the relationship when it is already loaded (e.g. selectin-loaded
    # by a list endpoint) instead of re-querying per call. ``track.__dict__``
    # only holds the collection when it has actually been loaded.
    links = track.__dict__.get("composer_links")
    if links is not None:
        # The relationship has no order_by; match the query ordering.
        links = sorted(links, key=lambda link: (link.created_at, link.id))
    elif db is not None and track.id is not None:
        links = db.scalars(
            select(TrackComposer)
            .where(TrackComposer.track_id == track.id)
            .order_by(TrackComposer.created_at, TrackComposer.id)
        ).all()
    else:
        links = getattr(track, "composer_links", None)
    if links:
        for link in links:
            add(link.user_id)
    return ordered


def track_composer_ids(track: Track, db: Session | None = None) -> set[int]:
    return set(track_composer_ordered_ids(track, db))


def is_track_composer(track: Track, user_id: int | None, db: Session | None = None) -> bool:
    return user_id is not None and user_id in track_composer_ids(track, db)


def track_external_composer_links(
    track: Track,
    db: Session | None = None,
) -> list[TrackExternalComposer]:
    # Reuse the relationship when it is already loaded (e.g. selectin-loaded
    # by a list endpoint) instead of re-querying per call.
    loaded = track.__dict__.get("external_composer_links")
    if loaded is not None:
        # The relationship orders by sort_order only; match the query ordering.
        return sorted(loaded, key=lambda link: (link.sort_order, link.id))
    if db is not None and track.id is not None:
        return list(
            db.scalars(
                select(TrackExternalComposer)
                .where(TrackExternalComposer.track_id == track.id)
                .order_by(TrackExternalComposer.sort_order, TrackExternalComposer.id)
            ).all()
        )
    return list(getattr(track, "external_composer_links", None) or [])


def track_external_composer_names(
    track: Track,
    db: Session | None = None,
) -> list[str]:
    names = [link.name for link in track_external_composer_links(track, db) if link.name]
    if names:
        return names
    legacy_name = (track.external_submitter_name or "").strip()
    return [legacy_name] if legacy_name else []


def track_composer_actor_ordered_ids(
    track: Track,
    album: Album,
    db: Session | None = None,
) -> list[int]:
    platform_ids = track_composer_ordered_ids(track, db)
    if platform_ids:
        return platform_ids
    if track_external_composer_names(track, db):
        actor_id = track.proxy_uploader_id
        if actor_id is None:
            actor_id = album.producer_id
        if actor_id is None:
            actor_id = track.submitter_id
        return [actor_id] if actor_id is not None else []
    return [track.submitter_id] if track.submitter_id is not None else []


def is_track_composer_actor(
    track: Track,
    album: Album,
    user_id: int | None,
    db: Session | None = None,
) -> bool:
    return user_id is not None and user_id in track_composer_actor_ordered_ids(track, album, db)


def track_composer_actor_ids_for_notify(
    track: Track,
    album: Album,
    db: Session | None = None,
    *,
    skip_user_id: int | None = None,
) -> list[int]:
    ids = track_composer_actor_ordered_ids(track, album, db)
    return [user_id for user_id in ids if user_id != skip_user_id]


def track_composer_ids_for_notify(
    track: Track,
    db: Session | None = None,
    *,
    skip_user_id: int | None = None,
) -> list[int]:
    ids = track_composer_ordered_ids(track, db)
    return [user_id for user_id in ids if user_id != skip_user_id]


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
    if current_cycle_deliveries:
        return max(current_cycle_deliveries, key=lambda item: item.delivery_number)
    latest_source = current_source_version(track)
    if latest_source is not None and latest_source.workflow_cycle == track.workflow_cycle:
        return None
    # Fallback: after a reopen the workflow_cycle increments and the new cycle
    # has no deliveries yet.  Return the latest delivery from any previous
    # cycle so the old mastering audio remains playable and viewable.
    return max(track.master_deliveries, key=lambda item: (item.workflow_cycle, item.delivery_number))


def pending_source_followup_request(db: Session, track_id: int) -> SourceFollowupRequest | None:
    return db.scalar(
        select(SourceFollowupRequest)
        .where(
            SourceFollowupRequest.track_id == track_id,
            SourceFollowupRequest.status == SourceFollowupRequestStatus.PENDING.value,
        )
        .order_by(SourceFollowupRequest.created_at.desc())
    )


def log_track_event(
    db: Session,
    track: Track,
    actor: User | None,
    event_type: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
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
        from_status=from_status.value if hasattr(from_status, "value") else from_status,
        to_status=to_status.value if hasattr(to_status, "value") else to_status,
        payload=json.dumps(payload, default=_serialize) if payload else None,
    )
    db.add(event)
    return event
