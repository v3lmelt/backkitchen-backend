"""Track/album permission, visibility and identity-anonymization logic."""

import logging

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin_permissions import has_admin_role
from app.anon import fnv1a_anon_token
from app.circle_permissions import album_manager_user_ids, is_album_manager, require_album_manager
from app.models.album import Album
from app.models.comment import Comment
from app.models.issue import Issue, IssueStatus
from app.models.stage_assignment import StageAssignment
from app.models.track import Track, TrackStatus
from app.models.user import User
from app.schemas.schemas import UserRead
from app.services.track_queries import (
    engaged_assignment_status_clause,
    get_album_member_ids,
    is_track_composer,
    track_composer_ids,
)
from app.workflow_engine import (
    compute_allowed_actions,
    get_current_step,
    get_step_by_id,
    get_steps,
    infer_issue_phase_for_step,
    parse_workflow_config,
)

logger = logging.getLogger(__name__)


def ensure_album_manager(album_id: int, user: User, db: Session) -> Album:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Album not found.",
        )
    require_album_manager(album, user, db)
    return album


def ensure_album_visibility(album: Album, user: User, db: Session) -> None:
    if has_admin_role(user, "viewer"):
        return
    member_ids = get_album_member_ids(db, album.id)
    visible_ids = {album.mastering_engineer_id}
    visible_ids.update(album_manager_user_ids(db, album))
    visible_ids.update(member_ids)
    if user.id not in visible_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this album.",
        )


def should_anonymize_track(track: Track, user: User, album: Album) -> bool:
    """Return True if the user should see an anonymized view of this track.

    Full info is shown to: the album producer, the mastering engineer, and every
    composer on the track. Everyone else sees artist/composer identities redacted.
    """
    if user.id == album.mastering_engineer_id:
        return False
    if db := Session.object_session(album):
        if is_album_manager(album, user, db):
            return False
    elif user.id == album.producer_id:
        return False
    if is_track_composer(track, user.id):
        return False
    return True


def _is_identity_privileged_viewer(user: User, album: Album) -> bool:
    if user.id == album.mastering_engineer_id:
        return True
    db = Session.object_session(album)
    return is_album_manager(album, user, db) if db is not None else user.id == album.producer_id


def is_mastering_participant(
    user: User,
    track: Track,
    album: Album,
    *,
    viewer_is_album_manager: bool | None = None,
) -> bool:
    """Return True if user is composer, producer, or mastering engineer for this track.

    ``viewer_is_album_manager`` lets callers that already know the album-manager
    status pass it in to avoid recomputing it here.
    """
    if is_track_composer(track, user.id) or user.id == album.mastering_engineer_id:
        return True
    if viewer_is_album_manager is not None:
        return viewer_is_album_manager
    db = Session.object_session(album)
    return is_album_manager(album, user, db) if db is not None else user.id == album.producer_id


def _hash_user_id(user_id: int) -> str:
    # Canonical v2 algorithm shared with the frontend (see app.anon).
    return fnv1a_anon_token(user_id)


def _masked_user_read(user_read: UserRead) -> UserRead:
    token = _hash_user_id(user_read.id)
    return user_read.model_copy(
        update={
            "username": f"anon_{token.lower()}",
            "display_name": f"#{token}",
            "email": None,
        }
    )


def user_read(user: User | None) -> UserRead | None:
    if user is None:
        return None
    return UserRead.model_validate(user)


def mask_user_read_if_needed(
    user_read: UserRead | None,
    anonymize_user_ids: set[int] | None,
) -> UserRead | None:
    if user_read is None:
        return None
    if anonymize_user_ids and user_read.id in anonymize_user_ids:
        return _masked_user_read(user_read)
    return user_read


def _is_peer_identity_anonymous_phase(track: Track, album: Album) -> bool:
    if track.status in {"peer_review", "peer_revision"}:
        return True

    try:
        config = parse_workflow_config(album)
        step = get_current_step(config, track)
        if step is None:
            return False
        if infer_issue_phase_for_step(step) == "peer":
            return True
        if step.type == "revision" and step.return_to:
            return_to = get_step_by_id(get_steps(config), step.return_to)
            return bool(return_to and infer_issue_phase_for_step(return_to) == "peer")
    except Exception:
        return False

    return False


def _peer_identity_user_ids(db: Session, track: Track, album: Album) -> set[int]:
    user_ids = set(track_composer_ids(track, db))
    user_ids.add(track.peer_reviewer_id)

    stage_ids: list[str] = []
    try:
        config = parse_workflow_config(album)
        stage_ids = [
            step.id
            for step in get_steps(config)
            if step.type == "review" and infer_issue_phase_for_step(step) == "peer"
        ]
    except Exception:
        stage_ids = []

    if not stage_ids:
        stage_ids = ["peer_review"]

    reviewer_ids = db.scalars(
        select(StageAssignment.user_id).where(
            StageAssignment.track_id == track.id,
            StageAssignment.stage_id.in_(stage_ids),
        )
    ).all()
    user_ids.update(reviewer_ids)
    user_ids.discard(None)
    return {uid for uid in user_ids if uid is not None}


def peer_identity_anonymize_user_ids_for_viewer(
    db: Session,
    track: Track,
    album: Album,
    viewer: User,
) -> set[int]:
    if _is_identity_privileged_viewer(viewer, album):
        return set()
    if not _is_peer_identity_anonymous_phase(track, album):
        return set()
    user_ids = _peer_identity_user_ids(db, track, album)
    if is_track_composer(track, viewer.id, db):
        user_ids -= track_composer_ids(track, db)
    else:
        user_ids.discard(viewer.id)
    return user_ids


def ensure_track_visibility(track: Track, user: User, db: Session) -> Album:
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    # Admins bypass all visibility checks
    if has_admin_role(user, "viewer"):
        return album
    if user.id == album.mastering_engineer_id or is_album_manager(album, user, db):
        return album
    # Composers and peer reviewer of this specific track always have access
    if is_track_composer(track, user.id, db) or user.id == track.peer_reviewer_id:
        return album
    assignment_id = db.scalar(
        select(StageAssignment.id).where(
            StageAssignment.track_id == track.id,
            StageAssignment.user_id == user.id,
            engaged_assignment_status_clause(),
        )
    )
    if assignment_id is not None:
        return album
    # Must be an album member
    member_ids = get_album_member_ids(db, album.id)
    if user.id not in member_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this album.",
        )
    # Regular members can only see tracks that are completed or marked public
    if track.status != TrackStatus.COMPLETED and not track.is_public:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this track.",
        )
    return album


def track_allowed_actions(
    track: Track, user: User, album: Album, *,
    _wf_config: dict | None = None, db: Session | None = None,
    review_assignments: list[StageAssignment] | None = None,
) -> list[str]:
    config = _wf_config or parse_workflow_config(album)
    return compute_allowed_actions(
        config, track, user, album, db=db, review_assignments=review_assignments,
    )


def issue_visible_to_user(issue: Issue, track: Track, user: User) -> bool:
    album = track.album
    db = Session.object_session(album) if album is not None else None
    if album is not None and db is not None and is_album_manager(album, user, db):
        return True
    if album is not None and user.id == album.producer_id:
        return True
    return not (
        is_track_composer(track, user.id)
        and issue.status in {IssueStatus.PENDING_DISCUSSION, IssueStatus.INTERNAL_RESOLVED}
    )


def comment_visible_to_user(comment: Comment, issue: Issue, track: Track, user: User) -> bool:
    album = track.album
    db = Session.object_session(album) if album is not None else None
    if album is not None and db is not None and is_album_manager(album, user, db):
        return issue_visible_to_user(issue, track, user)
    if album is not None and user.id == album.producer_id:
        return issue_visible_to_user(issue, track, user)
    if comment.visibility == "internal" and is_track_composer(track, user.id):
        return False
    return issue_visible_to_user(issue, track, user)


def issue_unresolved(issue: Issue) -> bool:
    return issue.status in {
        IssueStatus.OPEN,
        IssueStatus.PENDING_DISCUSSION,
    }
