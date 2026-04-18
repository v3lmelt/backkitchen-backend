import json
import logging

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.realtime import broadcast_notifications_updated
from app.models.album import Album
from app.models.notification import Notification
from app.services.webhook import build_webhook_payload, post_webhook


async def _deliver_webhook_background(
    url: str,
    payload: dict,
    album_id: int,
    event_type: str,
    webhook_type: str = "generic",
    webhook_secret: str = "",
    feishu_app_id: str = "",
    feishu_app_secret: str = "",
    mention_users: list[dict] | None = None,
) -> None:
    """Background-task wrapper that opens its own DB session for logging."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        await post_webhook(
            url, payload, db=db, album_id=album_id, event_type=event_type,
            webhook_type=webhook_type, webhook_secret=webhook_secret,
            feishu_app_id=feishu_app_id, feishu_app_secret=feishu_app_secret,
            mention_users=mention_users,
        )
    finally:
        db.close()


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
    webhook_context: dict | None = None,
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
            related_album_id=album_id,
        ))

    if background_tasks and seen:
        broadcast_notifications_updated(background_tasks, list(seen))

    # Dispatch webhook if configured for this album
    if background_tasks and album_id:
        _try_dispatch_webhook(
            db, background_tasks, album_id, type, title, body,
            related_track_id, related_issue_id, list(seen),
            webhook_context=webhook_context,
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
    notified_user_ids: list[int] | None = None,
    webhook_context: dict | None = None,
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

    # Build the set of *involved* users (notified + actor) for filter checks
    ctx = dict(webhook_context) if webhook_context else {}
    involved_ids: set[int] = set(notified_user_ids or [])
    actor_id = ctx.get("actor_id")
    if actor_id:
        involved_ids.add(actor_id)

    # User filter: only dispatch if event involves at least one watched user
    filter_uids = config.get("filter_user_ids")
    if filter_uids:
        if not involved_ids or not (involved_ids & set(filter_uids)):
            return

    # Enrich context with data we can look up here
    ctx.setdefault("album_title", album.title)
    resolved_track_id = track_id
    if resolved_track_id is None and issue_id:
        from app.models.issue import Issue

        issue = db.get(Issue, issue_id)
        if issue:
            resolved_track_id = issue.track_id
            ctx.setdefault("issue_title", issue.title)

    if resolved_track_id:
        from app.models.track import Track

        track = db.get(Track, resolved_track_id)
        if track:
            ctx.setdefault("track_title", track.title)
            from app.config import settings
            ctx.setdefault("track_url", f"{settings.FRONTEND_URL}/tracks/{resolved_track_id}")

    # Look up notified user display names for action_required_by
    if notified_user_ids and "action_required_by" not in ctx:
        from app.models.user import User
        users = db.query(User.display_name).filter(User.id.in_(notified_user_ids)).all()
        if users:
            ctx["action_required_by"] = "、".join(u.display_name for u in users)

    payload = build_webhook_payload(
        event_type, title, body,
        track_id=resolved_track_id, album_id=album_id, issue_id=issue_id,
        context=ctx,
    )
    url = config["url"]
    wh_type = config.get("type", "generic")
    wh_secret = config.get("secret", "")

    # Collect Feishu contact info for @mentions
    mention_users: list[dict] = []
    if wh_type == "feishu" and notified_user_ids:
        from app.models.user import User
        feishu_users = db.query(User).filter(
            User.id.in_(notified_user_ids),
            User.feishu_contact.isnot(None),
        ).all()
        mention_users = [
            {"name": u.display_name, "feishu_contact": u.feishu_contact}
            for u in feishu_users
        ]

    payload_for_dedupe = dict(payload)
    payload_for_dedupe.pop("timestamp", None)

    dedupe_key = json.dumps(
        {
            "url": url,
            "webhook_type": wh_type,
            "payload": payload_for_dedupe,
            "mention_users": mention_users,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    scheduled_webhooks = getattr(background_tasks, "_scheduled_webhook_keys", None)
    if scheduled_webhooks is None:
        scheduled_webhooks = set()
        setattr(background_tasks, "_scheduled_webhook_keys", scheduled_webhooks)
    if dedupe_key in scheduled_webhooks:
        return
    scheduled_webhooks.add(dedupe_key)

    background_tasks.add_task(
        _deliver_webhook_background, url, payload, album_id, event_type,
        webhook_type=wh_type, webhook_secret=wh_secret,
        feishu_app_id=config.get("app_id", ""),
        feishu_app_secret=config.get("app_secret", ""),
        mention_users=mention_users,
    )
