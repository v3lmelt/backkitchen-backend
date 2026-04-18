import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.notification import Notification
from app.realtime import broadcast_notifications_updated
from app.models.user import User
from app.schemas.schemas import NotificationRead
from app.security import get_current_user

router = APIRouter(tags=["notifications"])
logger = logging.getLogger(__name__)


@router.get("/api/notifications", response_model=list[NotificationRead])
def list_notifications(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[NotificationRead]:
    return list(db.scalars(
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all())


@router.patch("/api/notifications/read-all")
def mark_all_read(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    result = db.execute(
        update(Notification)
        .where(Notification.user_id == current_user.id, Notification.is_read == False)  # noqa: E712
        .values(is_read=True)
    )
    db.commit()
    logger.info("notifications_mark_all_read user_id=%s updated=%s", current_user.id, result.rowcount)
    if result.rowcount:
        broadcast_notifications_updated(background_tasks, [current_user.id])
    return {"updated": result.rowcount}


@router.patch("/api/notifications/{notification_id}/read", response_model=NotificationRead)
def mark_read(
    notification_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> NotificationRead:
    notif = db.get(Notification, notification_id)
    if notif is None or notif.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found.")
    notif.is_read = True
    db.commit()
    db.refresh(notif)
    logger.info("notifications_mark_read user_id=%s notification_id=%s", current_user.id, notification_id)
    broadcast_notifications_updated(background_tasks, [current_user.id])
    return notif
