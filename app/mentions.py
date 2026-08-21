"""@user mention parsing and per-context mention-candidate addressing."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.circle_permissions import album_manager_user_ids
from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.stage_assignment import StageAssignment
from app.models.track import Track, TrackStatus
from app.models.user import User
from app.schemas.schemas import MentionCandidatesRead, UserRead
from app.services.track_queries import (
    engaged_assignment_status_clause,
    get_album_member_ids,
    track_composer_ids,
    track_composer_ordered_ids,
)
from app.track_permissions import (
    is_mastering_participant,
    mask_user_read_if_needed,
    peer_identity_anonymize_user_ids_for_viewer,
    user_read,
)

USER_MENTION_PATTERN = re.compile(r"(?<![\w])@user:(\d{1,9})(?![\w])")


def extract_user_mention_ids(text: str | None) -> list[int]:
    """Return unique @user:ID mentions in first-seen order."""
    if not text:
        return []
    seen: set[int] = set()
    result: list[int] = []
    for match in USER_MENTION_PATTERN.finditer(text):
        user_id = int(match.group(1))
        if user_id in seen:
            continue
        seen.add(user_id)
        result.append(user_id)
    return result


def _mention_candidate_ids(db: Session, track: Track, album: Album) -> list[int]:
    ordered: list[int] = []
    seen: set[int] = set()

    def add(user_id: int | None) -> None:
        if user_id is None or user_id in seen:
            return
        seen.add(user_id)
        ordered.append(user_id)

    for user_id in track_composer_ordered_ids(track, db):
        add(user_id)
    for user_id in album_manager_user_ids(db, album):
        add(user_id)
    add(album.mastering_engineer_id)
    add(track.peer_reviewer_id)

    for user_id in db.scalars(
        select(AlbumMember.user_id).where(AlbumMember.album_id == album.id)
    ).all():
        add(user_id)

    for user_id in db.scalars(
        select(StageAssignment.user_id).where(
            StageAssignment.track_id == track.id,
            engaged_assignment_status_clause(),
        )
    ).all():
        add(user_id)

    return ordered


def _active_users_by_id(db: Session, user_ids: list[int]) -> dict[int, User]:
    if not user_ids:
        return {}
    users = db.scalars(select(User).where(User.id.in_(user_ids))).all()
    return {
        user.id: user
        for user in users
        if user.deleted_at is None and user.suspended_at is None
    }


def _can_user_see_general_track_area(
    user_id: int,
    track: Track,
    album: Album,
    album_member_ids: set[int],
    assignment_ids: set[int],
    composer_ids: set[int],
    manager_ids: set[int],
) -> bool:
    if user_id in composer_ids or user_id in ({track.peer_reviewer_id, album.mastering_engineer_id} | manager_ids):
        return True
    if user_id in assignment_ids:
        return True
    return user_id in album_member_ids and (track.status == TrackStatus.COMPLETED or track.is_public)


def _candidate_user_reads(
    users_by_id: dict[int, User],
    ordered_ids: list[int],
    *,
    include_ids: set[int],
    anonymize_user_ids: set[int],
) -> list[UserRead]:
    result: list[UserRead] = []
    for user_id in ordered_ids:
        if user_id not in include_ids:
            continue
        user = users_by_id.get(user_id)
        if user is None:
            continue
        result.append(mask_user_read_if_needed(user_read(user), anonymize_user_ids))
    return [item for item in result if item is not None]


def build_mention_candidates(
    db: Session,
    track: Track,
    album: Album,
    viewer: User,
    anonymize_user_ids: set[int] | None = None,
) -> MentionCandidatesRead:
    """Build per-context @user candidates visible to the current viewer."""
    anonymize = anonymize_user_ids or peer_identity_anonymize_user_ids_for_viewer(db, track, album, viewer)
    ordered_ids = _mention_candidate_ids(db, track, album)
    album_member_ids = get_album_member_ids(db, album.id)
    assignment_ids = set(
        db.scalars(
            select(StageAssignment.user_id).where(
                StageAssignment.track_id == track.id,
                engaged_assignment_status_clause(),
            )
        ).all()
    )
    composer_ids = track_composer_ids(track, db)
    manager_ids = album_manager_user_ids(db, album)
    general_ids = {
        user_id
        for user_id in ordered_ids
        if _can_user_see_general_track_area(user_id, track, album, album_member_ids, assignment_ids, composer_ids, manager_ids)
    }

    viewer_can_see_mastering = is_mastering_participant(viewer, track, album)
    mastering_ids = (
        (composer_ids | manager_ids | {album.mastering_engineer_id}) & set(ordered_ids)
        if viewer_can_see_mastering
        else set()
    )
    mastering_ids.discard(None)

    viewer_can_see_internal_issue = viewer.id not in composer_ids
    issue_internal_ids = set(general_ids) - composer_ids
    if not viewer_can_see_internal_issue:
        issue_internal_ids.clear()

    # Fetch every candidate user once and reuse the mapping for all contexts.
    users_by_id = _active_users_by_id(db, ordered_ids)

    return MentionCandidatesRead(
        general=_candidate_user_reads(users_by_id, ordered_ids, include_ids=general_ids, anonymize_user_ids=anonymize),
        mastering=_candidate_user_reads(users_by_id, ordered_ids, include_ids=mastering_ids, anonymize_user_ids=anonymize),
        issue_public=_candidate_user_reads(users_by_id, ordered_ids, include_ids=general_ids, anonymize_user_ids=anonymize),
        issue_internal=_candidate_user_reads(users_by_id, ordered_ids, include_ids=issue_internal_ids, anonymize_user_ids=anonymize),
    )


def allowed_user_mention_ids(
    text: str | None,
    db: Session,
    track: Track,
    album: Album,
    viewer: User,
    *,
    context: str,
) -> list[int]:
    mentioned_ids = extract_user_mention_ids(text)
    if not mentioned_ids:
        return []
    candidates = build_mention_candidates(db, track, album, viewer)
    candidate_map = {
        "general": candidates.general,
        "mastering": candidates.mastering,
        "issue_public": candidates.issue_public,
        "issue_internal": candidates.issue_internal,
    }
    allowed_ids = {user.id for user in candidate_map.get(context, [])}
    return [user_id for user_id in mentioned_ids if user_id in allowed_ids and user_id != viewer.id]
