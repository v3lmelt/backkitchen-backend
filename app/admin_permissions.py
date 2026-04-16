from collections.abc import Callable

from fastapi import Depends, HTTPException, status

from app.models.user import User
from app.security import get_current_user

ADMIN_ROLE_NONE = "none"
ADMIN_ROLE_VIEWER = "viewer"
ADMIN_ROLE_OPERATOR = "operator"
ADMIN_ROLE_SUPERADMIN = "superadmin"

ADMIN_ROLE_ORDER = {
    ADMIN_ROLE_NONE: 0,
    ADMIN_ROLE_VIEWER: 1,
    ADMIN_ROLE_OPERATOR: 2,
    ADMIN_ROLE_SUPERADMIN: 3,
}


def normalize_admin_role(user: User | None) -> str:
    if user is None:
        return ADMIN_ROLE_NONE
    role = (user.admin_role or "").strip().lower()
    if role not in ADMIN_ROLE_ORDER:
        role = ADMIN_ROLE_SUPERADMIN if user.is_admin else ADMIN_ROLE_NONE
    return role


def sync_admin_role_flags(user: User) -> str:
    role = normalize_admin_role(user)
    user.admin_role = role
    user.is_admin = role != ADMIN_ROLE_NONE
    return role


def has_admin_role(user: User | None, minimum_role: str = ADMIN_ROLE_VIEWER) -> bool:
    return ADMIN_ROLE_ORDER[normalize_admin_role(user)] >= ADMIN_ROLE_ORDER[minimum_role]


def require_admin(minimum_role: str = ADMIN_ROLE_VIEWER) -> Callable[..., User]:
    def dependency(current_user: User = Depends(get_current_user)) -> User:
        if not has_admin_role(current_user, minimum_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin access required.",
            )
        return current_user

    return dependency
