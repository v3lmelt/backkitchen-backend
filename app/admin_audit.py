import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.admin_audit_log import AdminAuditLog
from app.models.user import User


def _json_default(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _serialize_state(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def record_admin_audit(
    db: Session,
    *,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    summary: str | None = None,
    reason: str | None = None,
    before: Any = None,
    after: Any = None,
    target_user_id: int | None = None,
    album_id: int | None = None,
    track_id: int | None = None,
    circle_id: int | None = None,
) -> AdminAuditLog:
    entry = AdminAuditLog(
        actor_user_id=actor.id if actor else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
        reason=reason,
        before_state=_serialize_state(before),
        after_state=_serialize_state(after),
        target_user_id=target_user_id,
        album_id=album_id,
        track_id=track_id,
        circle_id=circle_id,
    )
    db.add(entry)
    return entry
