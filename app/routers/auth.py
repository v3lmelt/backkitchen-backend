import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.email_verification import EmailVerificationToken
from app.models.user import User
from app.schemas.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    UserRead,
    UserUpdateProfile,
)
from app.security import create_access_token, get_current_user, hash_password, verify_password
from app.services.email import send_verification_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Computed once at startup; used to prevent timing-based email enumeration.
_DUMMY_HASH = hash_password("__dummy_timing_guard__")

_VERIFICATION_TOKEN_EXPIRE_MINUTES = 30


def _build_auth_response(user: User) -> AuthResponse:
    return AuthResponse(access_token=create_access_token(user), user=UserRead.model_validate(user))


def _create_verification_token(email: str, db: Session) -> str:
    # Invalidate any existing unused tokens for this email
    existing = db.scalars(
        select(EmailVerificationToken).where(
            EmailVerificationToken.email == email,
            EmailVerificationToken.used.is_(False),
        )
    ).all()
    for t in existing:
        t.used = True

    token = secrets.token_urlsafe(48)
    record = EmailVerificationToken(
        token=token,
        email=email,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=_VERIFICATION_TOKEN_EXPIRE_MINUTES),
    )
    db.add(record)
    db.commit()
    return token


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> AuthResponse:
    user = db.scalars(select(User).where(User.email == payload.email)).first()
    stored = user.password if user is not None else _DUMMY_HASH
    if not verify_password(payload.password, stored):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Please check your inbox.",
        )
    return _build_auth_response(user)


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> RegisterResponse:
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
        email_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = _create_verification_token(payload.email, db)
    send_verification_email(payload.email, token)

    return RegisterResponse(email=payload.email)


@router.post("/verify-email", response_model=AuthResponse)
def verify_email(token: str, db: Session = Depends(get_db)) -> AuthResponse:
    record = db.scalars(
        select(EmailVerificationToken).where(EmailVerificationToken.token == token)
    ).first()

    if record is None or record.used:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired verification link.")

    now = datetime.now(timezone.utc)
    expires = record.expires_at
    # Ensure both are offset-aware for comparison
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now > expires:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Verification link has expired.")

    user = db.scalars(select(User).where(User.email == record.email)).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    user.email_verified = True
    record.used = True
    db.commit()
    db.refresh(user)

    return _build_auth_response(user)


@router.post("/resend-verification", status_code=status.HTTP_204_NO_CONTENT)
def resend_verification(email: str, db: Session = Depends(get_db)) -> None:
    user = db.scalars(select(User).where(User.email == email)).first()
    # Always return 204 to avoid email enumeration
    if user is None or user.email_verified:
        return
    token = _create_verification_token(email, db)
    send_verification_email(email, token)


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


@router.post("/me/avatar", response_model=UserRead)
async def upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserRead:
    from app.config import MAX_IMAGE_UPLOAD_SIZE, settings
    from app.services.upload import stream_upload

    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    if file.content_type and file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only JPEG, PNG, WebP, and GIF images are allowed.",
        )

    ext = Path(file.filename).suffix.lower() if file.filename else ".jpg"
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported image extension: {ext}",
        )

    filename = f"{current_user.id}_{uuid.uuid4().hex}{ext}"
    avatar_dir = settings.get_upload_path() / "avatars"
    avatar_dir.mkdir(parents=True, exist_ok=True)

    dest = avatar_dir / filename
    await stream_upload(file, dest, MAX_IMAGE_UPLOAD_SIZE)

    # Update DB first, then clean up old file (safe ordering)
    old_avatar = current_user.avatar_image
    current_user.avatar_image = f"avatars/{filename}"
    db.commit()
    db.refresh(current_user)
    if old_avatar:
        old_path = settings.get_upload_path() / old_avatar
        old_path.unlink(missing_ok=True)
    return UserRead.model_validate(current_user)


@router.delete("/me/avatar", response_model=UserRead)
def delete_avatar(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserRead:
    from app.config import settings

    if current_user.avatar_image:
        old_path = settings.get_upload_path() / current_user.avatar_image
        if old_path.exists():
            old_path.unlink(missing_ok=True)
        current_user.avatar_image = None
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
