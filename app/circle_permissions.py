from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin_permissions import has_admin_role
from app.models.album import Album
from app.models.circle import Circle, CircleMember
from app.models.user import User

CIRCLE_ROLE_OWNER = "owner"
CIRCLE_ROLE_MEMBER = "member"
CIRCLE_ROLE_MASTERING_ENGINEER = "mastering_engineer"
CIRCLE_ROLE_CO_PRODUCER = "co_producer"

CIRCLE_MEMBER_ROLES = {
    CIRCLE_ROLE_OWNER,
    CIRCLE_ROLE_MEMBER,
    CIRCLE_ROLE_MASTERING_ENGINEER,
    CIRCLE_ROLE_CO_PRODUCER,
}
CIRCLE_MANAGER_ROLES = {CIRCLE_ROLE_OWNER, CIRCLE_ROLE_CO_PRODUCER}
CIRCLE_MUTABLE_MEMBER_ROLES = {
    CIRCLE_ROLE_MEMBER,
    CIRCLE_ROLE_MASTERING_ENGINEER,
    CIRCLE_ROLE_CO_PRODUCER,
}


def get_circle_membership(db: Session, circle_id: int, user_id: int) -> CircleMember | None:
    return db.scalar(
        select(CircleMember).where(
            CircleMember.circle_id == circle_id,
            CircleMember.user_id == user_id,
        )
    )


def is_circle_owner(circle: Circle, user: User) -> bool:
    return circle.created_by == user.id


def is_circle_manager(circle: Circle, user: User, db: Session) -> bool:
    if has_admin_role(user, "operator"):
        return True
    if is_circle_owner(circle, user):
        return True
    membership = get_circle_membership(db, circle.id, user.id)
    return membership is not None and membership.role in CIRCLE_MANAGER_ROLES


def require_circle_manager(circle: Circle, user: User, db: Session) -> None:
    if not is_circle_manager(circle, user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a circle manager can do this.",
        )


def require_circle_owner(circle: Circle, user: User) -> None:
    if not is_circle_owner(circle, user) and not has_admin_role(user, "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the circle owner can do this.",
        )


def is_album_manager(album: Album, user: User, db: Session) -> bool:
    if has_admin_role(user, "operator"):
        return True
    if album.producer_id == user.id:
        return True
    if album.circle_id is None:
        return False
    circle = album.circle or db.get(Circle, album.circle_id)
    return circle is not None and is_circle_manager(circle, user, db)


def require_album_manager(album: Album, user: User, db: Session) -> None:
    if not is_album_manager(album, user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the album manager can perform this action.",
        )

def is_circle_bound_album_manager(album: Album, user: User, db: Session) -> bool:
    if has_admin_role(user, "operator"):
        return True
    if album.circle_id is None:
        return False
    circle = album.circle or db.get(Circle, album.circle_id)
    return circle is not None and is_circle_manager(circle, user, db)


def require_circle_bound_album_manager(album: Album, user: User, db: Session) -> None:
    if not is_circle_bound_album_manager(album, user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a circle manager can adjust track progress.",
        )


def album_manager_user_ids(db: Session, album: Album) -> set[int]:
    ids: set[int] = set()
    if album.producer_id is not None:
        ids.add(album.producer_id)
    if album.circle_id is None:
        return ids
    circle = album.circle or db.get(Circle, album.circle_id)
    if circle is not None and circle.created_by is not None:
        ids.add(circle.created_by)
    ids.update(
        db.scalars(
            select(CircleMember.user_id).where(
                CircleMember.circle_id == album.circle_id,
                CircleMember.role.in_(CIRCLE_MANAGER_ROLES),
            )
        ).all()
    )
    ids.discard(None)
    return ids
