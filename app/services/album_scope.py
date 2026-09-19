"""Personal album relationships, independent of global access permissions."""

from typing import Literal

from sqlalchemy import select, true
from sqlalchemy.sql.elements import ColumnElement

from app.circle_permissions import CIRCLE_MANAGER_ROLES
from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.circle import Circle, CircleMember
from app.models.stage_assignment import StageAssignment
from app.models.track import Track
from app.models.track_composer import TrackComposer

AlbumScope = Literal["all", "managed", "participating"]


def album_scope_condition(scope: AlbumScope, user_id: int) -> ColumnElement[bool]:
    """Filter Album rows by relationship; callers must still enforce visibility."""
    if scope == "all":
        return true()

    managed = (Album.producer_id == user_id) | Album.circle_id.in_(
        select(Circle.id).where(
            (Circle.created_by == user_id)
            | Circle.id.in_(select(CircleMember.circle_id).where(
                CircleMember.user_id == user_id,
                CircleMember.role.in_(CIRCLE_MANAGER_ROLES),
            ))
        )
    )
    if scope == "managed":
        return managed

    current_reviews = select(StageAssignment.track_id).join(
        Track, Track.id == StageAssignment.track_id,
    ).where(
        StageAssignment.user_id == user_id,
        StageAssignment.stage_id == Track.status,
        StageAssignment.status.in_(("pending", "completed")),
        Track.archived_at.is_(None),
    )
    related_tracks = select(Track.album_id).where(
        (Track.submitter_id == user_id)
        | Track.id.in_(select(TrackComposer.track_id).where(TrackComposer.user_id == user_id))
        | Track.id.in_(current_reviews)
    )
    return (
        managed
        | (Album.mastering_engineer_id == user_id)
        | Album.id.in_(select(AlbumMember.album_id).where(AlbumMember.user_id == user_id))
        | Album.id.in_(related_tracks)
    )
