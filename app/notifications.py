import json
import logging

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.models.album import Album
from app.models.notification import Notification
from app.services.webhook import build_webhook_payload, post_webhook


def notify(
    db: Session,
    user_ids: list[int | None],
    type: str,
    title: str,
    body: str,
    related_track_id: int | None = None,
    related_issue_id: int | None = None,
    *,
    background_tasks: BackgroundTasks | None = None,
    album_id: int | None = None,
) -> None:
    """Create in-app notifications and optionally dispatch webhook."""
    seen: set[int] = set()
    for uid in user_ids:
        if uid is None or uid in seen:
            continue
        seen.add(uid)
        db.add(Notification(
            user_id=uid,
            type=type,
            title=title,
            body=body,
            related_track_id=related_track_id,
            related_issue_id=related_issue_id,
        ))

    # Dispatch webhook if configured for this album
    if background_tasks and album_id:
        _try_dispatch_webhook(
            db, background_tasks, album_id, type, title, body,
            related_track_id, related_issue_id,
        )


def _try_dispatch_webhook(
    db: Session,
    background_tasks: BackgroundTasks,
    album_id: int,
    event_type: str,
    title: str,
    body: str,
    track_id: int | None,
    issue_id: int | None,
) -> None:
    album = db.get(Album, album_id)
    if not album or not album.webhook_config:
        return
    try:
        config = json.loads(album.webhook_config)
    except (json.JSONDecodeError, TypeError):
        logger.error("Malformed webhook_config for album %s, skipping dispatch", album_id)
        return
    if not config.get("enabled") or not config.get("url"):
        return
    allowed_events = config.get("events")
    if allowed_events and event_type not in allowed_events:
        return

    payload = build_webhook_payload(
        event_type, title, body,
        track_id=track_id, album_id=album_id, issue_id=issue_id,
    )
    background_tasks.add_task(post_webhook, config["url"], payload)
