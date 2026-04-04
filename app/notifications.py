from sqlalchemy.orm import Session

from app.models.notification import Notification


def notify(
    db: Session,
    user_ids: list[int | None],
    type: str,
    title: str,
    body: str,
    related_track_id: int | None = None,
    related_issue_id: int | None = None,
) -> None:
    """为多个用户创建通知，自动跳过 None 和重复 user_id。"""
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
