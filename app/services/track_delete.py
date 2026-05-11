from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.issue import Issue
from app.models.notification import Notification


def prepare_track_hard_delete(db: Session, track_id: int) -> None:
    """Break references that should not block hard deletion of a track."""

    db.execute(
        update(Issue)
        .where(Issue.track_id == track_id)
        .values(source_version_id=None, master_delivery_id=None)
        .execution_options(synchronize_session="fetch")
    )
    db.execute(
        update(Notification)
        .where(Notification.related_track_id == track_id)
        .values(related_track_id=None)
        .execution_options(synchronize_session="fetch")
    )
