import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.user import User

TOKEN_KIND = "access"
_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 100_000
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        iterations,
    )
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algorithm, iteration_text, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    calculated = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        int(iteration_text),
    ).hex()
    return hmac.compare_digest(calculated, digest)


def _sign(message: bytes) -> str:
    signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        message,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def create_access_token(user: User) -> str:
    payload = {
        "sub": user.id,
        "type": TOKEN_KIND,
        "exp": int(
            (
                datetime.now(timezone.utc)
                + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
            ).timestamp()
        ),
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode(
        "ascii"
    ).rstrip("=")
    signature = _sign(encoded.encode("ascii"))
    return f"{encoded}.{signature}"


def _decode_token(token: str) -> dict:
    try:
        payload_part, signature = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
        ) from exc

    expected_signature = _sign(payload_part.encode("ascii"))
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
        )

    padding = "=" * (-len(payload_part) % 4)
    payload = json.loads(
        base64.urlsafe_b64decode(f"{payload_part}{padding}").decode("utf-8")
    )
    if payload.get("type") != TOKEN_KIND or payload.get("exp", 0) < int(
        datetime.now(timezone.utc).timestamp()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token expired.",
        )
    return payload


def _resolve_bearer_user(
    credentials: HTTPAuthorizationCredentials | None,
    db: Session,
) -> User | None:
    """Shared logic: decode Bearer credentials and look up the user.
    Returns ``None`` when *credentials* is ``None``."""
    if credentials is None:
        return None
    payload = _decode_token(credentials.credentials)
    user = db.get(User, int(payload["sub"]))
    if user is None or user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user no longer exists.",
        )
    return user


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    user = _resolve_bearer_user(credentials, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return user


def require_producer(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "producer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only producers can perform this action.",
        )
    return current_user


def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User | None:
    """Same as ``get_current_user`` but returns ``None`` instead of raising
    when no Bearer header is present.  Used by endpoints that also accept a
    ``?token=`` query-param fallback (e.g. audio streaming)."""
    return _resolve_bearer_user(credentials, db)


def get_user_from_token_param(
    token: str | None = Query(default=None, alias="token"),
    db: Session = Depends(get_db),
) -> User | None:
    """Resolve a user from a ``?token=`` query parameter (for ``<audio>`` src URLs)."""
    if token is None:
        return None
    payload = _decode_token(token)
    user = db.get(User, int(payload["sub"]))
    if user is None or user.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")
    return user
