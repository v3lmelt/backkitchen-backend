from fastapi import BackgroundTasks

from app.ws_manager import manager as track_manager
from app.ws_manager import notification_manager


def broadcast_track_updated(background_tasks: BackgroundTasks, track_id: int) -> None:
    """Schedule a track-level refresh event for all subscribers."""
    background_tasks.add_task(
        track_manager.broadcast,
        track_id,
        {"type": "track_updated", "track_id": track_id},
    )


def broadcast_notifications_updated(background_tasks: BackgroundTasks, user_ids: list[int | None]) -> None:
    """Schedule a notification refresh event for one or more users."""
    target_ids = [user_id for user_id in dict.fromkeys(user_ids) if user_id is not None]
    if not target_ids:
        return
    background_tasks.add_task(
        notification_manager.broadcast_many,
        target_ids,
        {"type": "notifications_updated"},
    )
