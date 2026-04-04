from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.schemas import AuthResponse, ChangePasswordRequest, LoginRequest, RegisterRequest, UserRead, UserUpdateProfile
from app.security import create_access_token, get_current_user, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Computed once at startup; used to prevent timing-based email enumeration.
_DUMMY_HASH = hash_password("__dummy_timing_guard__")


def _build_auth_response(user: User) -> AuthResponse:
    return AuthResponse(access_token=create_access_token(user), user=UserRead.model_validate(user))


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> AuthResponse:
    user = db.scalars(select(User).where(User.email == payload.email)).first()
    stored = user.password if user is not None else _DUMMY_HASH
    if not verify_password(payload.password, stored):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    return _build_auth_response(user)


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> AuthResponse:
    if db.scalars(select(User).where(User.email == payload.email)).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email is already registered.",
        )
    if db.scalars(select(User).where(User.username == payload.username)).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username is already taken.",
        )

    user = User(
        username=payload.username,
        display_name=payload.display_name,
        email=payload.email,
        password=hash_password(payload.password),
        role="member",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _build_auth_response(user)


@router.get("/me", response_model=UserRead)
def get_me(current_user: User = Depends(get_current_user)) -> UserRead:
    return UserRead.model_validate(current_user)


@router.patch("/me", response_model=UserRead)
def update_me(
    payload: UserUpdateProfile,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserRead:
    changed = False
    if payload.display_name is not None:
        current_user.display_name = payload.display_name
        changed = True
    if payload.email is not None:
        existing = db.scalars(select(User).where(User.email == payload.email, User.id != current_user.id)).first()
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered.")
        current_user.email = payload.email
        changed = True
    if changed:
        db.commit()
        db.refresh(current_user)
    return UserRead.model_validate(current_user)


@router.post("/me/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePasswordRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not verify_password(payload.current_password, current_user.password or ""):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect.")
    current_user.password = hash_password(payload.new_password)
    db.commit()
