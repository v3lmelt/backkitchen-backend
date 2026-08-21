from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.invitation import Invitation, InvitationStatus
from app.models.stage_assignment import StageAssignment, StageAssignmentStatus
from app.models.track import Track
from app.services.track_queries import engaged_assignment_status_clause

ASSIGNMENT_CANCEL_REASON_CIRCLE_MEMBERSHIP_REVOKED = "circle_membership_revoked"


def revoke_circle_member_resource_access(db: Session, circle_id: int, user_id: int) -> None:
    """Remove access derived from membership in a circle without committing."""
    album_ids = select(Album.id).where(Album.circle_id == circle_id)
    track_ids = select(Track.id).where(Track.album_id.in_(album_ids))

    db.execute(
        delete(AlbumMember).where(
            AlbumMember.album_id.in_(album_ids),
            AlbumMember.user_id == user_id,
        )
    )
    db.execute(
        update(Album)
        .where(
            Album.circle_id == circle_id,
            Album.mastering_engineer_id == user_id,
        )
        .values(mastering_engineer_id=None)
    )
    db.execute(
        update(Track)
        .where(
            Track.album_id.in_(album_ids),
            Track.peer_reviewer_id == user_id,
        )
        .values(peer_reviewer_id=None)
    )
    db.execute(
        update(StageAssignment)
        .where(
            StageAssignment.track_id.in_(track_ids),
            StageAssignment.user_id == user_id,
            engaged_assignment_status_clause(),
        )
        .values(
            status=StageAssignmentStatus.CANCELLED.value,
            cancellation_reason=ASSIGNMENT_CANCEL_REASON_CIRCLE_MEMBERSHIP_REVOKED,
        )
    )
    db.execute(
        delete(Invitation).where(
            Invitation.album_id.in_(album_ids),
            Invitation.user_id == user_id,
            Invitation.status == InvitationStatus.PENDING.value,
        )
    )
