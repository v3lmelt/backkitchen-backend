import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.email_verification import EmailVerificationToken
from app.models.password_reset import PasswordResetToken
from app.models.user import User
from app.schemas.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    DeleteAccountRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    ResetPasswordRequest,
    UserRead,
    UserUpdateProfile,
)
from app.security import create_access_token, get_current_user, hash_password, verify_password
from app.services.email import send_password_reset_email, send_verification_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Computed once at startup; used to prevent timing-based email enumeration.
_DUMMY_HASH = hash_password("__dummy_timing_guard__")

_VERIFICATION_TOKEN_EXPIRE_MINUTES = 30
_PASSWORD_RESET_TOKEN_EXPIRE_MINUTES = 60


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
    user = db.scalars(
        select(User).where(User.email == payload.email, User.deleted_at.is_(None))
    ).first()
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
    if db.scalars(
        select(User).where(User.email == payload.email, User.deleted_at.is_(None))
    ).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email is already registered.",
        )
    if db.scalars(
        select(User).where(User.username == payload.username, User.deleted_at.is_(None))
    ).first() is not None:
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


def _create_password_reset_token(email: str, db: Session) -> str:
    # Invalidate any existing unused tokens for this email
    existing = db.scalars(
        select(PasswordResetToken).where(
            PasswordResetToken.email == email,
            PasswordResetToken.used.is_(False),
        )
    ).all()
    for t in existing:
        t.used = True

    token = secrets.token_urlsafe(48)
    record = PasswordResetToken(
        token=token,
        email=email,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=_PASSWORD_RESET_TOKEN_EXPIRE_MINUTES),
    )
    db.add(record)
    db.commit()
    return token


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)) -> None:
    """Request a password reset email.

    Always returns 204 regardless of whether the email exists, to avoid enumeration.
    """
    user = db.scalars(
        select(User).where(User.email == payload.email, User.deleted_at.is_(None))
    ).first()
    if user is None or not user.email_verified:
        return
    token = _create_password_reset_token(payload.email, db)
    send_password_reset_email(payload.email, token)


@router.post("/reset-password", response_model=AuthResponse)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)) -> AuthResponse:
    record = db.scalars(
        select(PasswordResetToken).where(PasswordResetToken.token == payload.token)
    ).first()
    if record is None or record.used:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset link.",
        )
    now = datetime.now(timezone.utc)
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now > expires:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset link has expired.",
        )

    user = db.scalars(
        select(User).where(User.email == record.email, User.deleted_at.is_(None))
    ).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    user.password = hash_password(payload.new_password)
    record.used = True
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
    verification_email: str | None = None
    if payload.display_name is not None:
        current_user.display_name = payload.display_name
        changed = True
    if payload.email is not None:
        existing = db.scalars(select(User).where(User.email == payload.email, User.id != current_user.id)).first()
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered.")
        if payload.email != current_user.email:
            current_user.email = payload.email
            current_user.email_verified = False
            verification_email = payload.email
            changed = True
    if payload.feishu_contact is not None:
        current_user.feishu_contact = payload.feishu_contact or None
        changed = True
    if changed:
        db.commit()
        db.refresh(current_user)
    if verification_email:
        token = _create_verification_token(verification_email, db)
        send_verification_email(verification_email, token)
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


@router.post("/me/delete-account", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    payload: DeleteAccountRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Soft-delete the current user's account.

    Refuses if the user still owns circles or produces non-archived albums.
    Anonymizes display_name and suffixes username/email to free up uniqueness.
    """
    from app.models.album import Album
    from app.models.circle import Circle
    from app.models.track import RejectionMode, Track, TrackStatus

    if not verify_password(payload.password, current_user.password or ""):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password is incorrect.",
        )

    # Block if the user still owns circles
    owned_circle = db.scalars(
        select(Circle.id).where(Circle.created_by == current_user.id).limit(1)
    ).first()
    if owned_circle is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You still own one or more circles. Delete them before closing your account.",
        )

    # Block if the user still produces non-archived albums
    produced_album = db.scalars(
        select(Album.id).where(
            Album.producer_id == current_user.id,
            Album.archived_at.is_(None),
        ).limit(1)
    ).first()
    if produced_album is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You still produce one or more active albums. Archive or transfer them before closing your account.",
        )

    active_track_filter = or_(
        Track.status.notin_([TrackStatus.COMPLETED.value, TrackStatus.REJECTED.value]),
        and_(
            Track.status == TrackStatus.REJECTED.value,
            Track.rejection_mode == RejectionMode.RESUBMITTABLE,
        ),
    )

    authored_track = db.scalars(
        select(Track.id).where(
            Track.submitter_id == current_user.id,
            Track.archived_at.is_(None),
            active_track_filter,
        ).limit(1)
    ).first()
    if authored_track is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You still own one or more active tracks. Complete, archive, or transfer them before closing your account.",
        )

    mastering_track = db.scalars(
        select(Track.id)
        .join(Album, Album.id == Track.album_id)
        .where(
            Album.mastering_engineer_id == current_user.id,
            Track.archived_at.is_(None),
            active_track_filter,
        )
        .limit(1)
    ).first()
    if mastering_track is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You are still responsible for one or more active mastering tracks. Reassign them before closing your account.",
        )

    now = datetime.now(timezone.utc)
    original_email = current_user.email
    current_user.deleted_at = now
    current_user.display_name = "[deleted user]"
    # Suffix username/email with deleted-<id>-<ts> to free the unique constraints
    suffix = f".deleted-{current_user.id}-{int(now.timestamp())}"
    if current_user.username:
        current_user.username = f"{current_user.username}{suffix}"
    if current_user.email:
        current_user.email = f"{current_user.email}{suffix}"
    # Invalidate any outstanding password reset tokens tied to the original email
    if original_email:
        db.execute(
            PasswordResetToken.__table__.update()
            .where(PasswordResetToken.email == original_email, PasswordResetToken.used.is_(False))
            .values(used=True)
        )
    db.commit()
