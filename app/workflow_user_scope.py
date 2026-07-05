from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.album import Album
from app.models.circle import Circle, CircleMember
from app.workflow import get_album_member_ids


def circle_workflow_user_ids(db: Session, circle_id: int) -> set[int]:
    user_ids = set(
        db.scalars(
            select(CircleMember.user_id).where(CircleMember.circle_id == circle_id)
        ).all()
    )
    owner_id = db.scalar(select(Circle.created_by).where(Circle.id == circle_id))
    if owner_id is not None:
        user_ids.add(owner_id)
    return user_ids


def album_reviewer_scope_user_ids(db: Session, album: Album) -> set[int]:
    if album.circle_id is not None:
        return circle_workflow_user_ids(db, album.circle_id)

    user_ids = get_album_member_ids(db, album.id)
    if album.producer_id is not None:
        user_ids.add(album.producer_id)
    if album.mastering_engineer_id is not None:
        user_ids.add(album.mastering_engineer_id)
    return user_ids


def _workflow_steps(config: Any) -> list[Mapping[str, Any]]:
    if hasattr(config, "model_dump"):
        data = config.model_dump(mode="json")
    else:
        data = config
    if not isinstance(data, Mapping):
        return []
    steps = data.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, Mapping)]


def validate_circle_workflow_user_scope(
    config: Any,
    db: Session,
    circle_id: int | None,
) -> None:
    if circle_id is None:
        return

    valid_user_ids = circle_workflow_user_ids(db, circle_id)
    referenced_user_ids: set[int] = set()
    for step in _workflow_steps(config):
        reviewer_pool = step.get("reviewer_pool")
        if isinstance(reviewer_pool, list):
            referenced_user_ids.update(
                user_id for user_id in reviewer_pool if isinstance(user_id, int)
            )
        assignee_user_id = step.get("assignee_user_id")
        if isinstance(assignee_user_id, int):
            referenced_user_ids.add(assignee_user_id)

    invalid_user_ids = sorted(referenced_user_ids - valid_user_ids)
    if invalid_user_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Users {invalid_user_ids} are not members of this circle.",
        )
