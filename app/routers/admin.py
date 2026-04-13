from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.database import get_db
from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.track import Track, TrackStatus
from app.models.user import User
from app.routers.albums import _album_to_read
from app.schemas.schemas import AdminUserUpdate, AlbumRead, TrackRead, UserRead
from app.security import get_current_user
from app.workflow import build_track_read

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return current_user


@router.get("/users", response_model=list[UserRead])
def list_users(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[User]:
    return list(db.scalars(select(User).order_by(User.id).limit(limit).offset(offset)).all())


@router.patch("/users/{user_id}", response_model=UserRead)
def update_user(
    user_id: int,
    payload: AdminUserUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    if payload.role is not None:
        user.role = payload.role
    if payload.is_admin is not None:
        user.is_admin = payload.is_admin
    if payload.email_verified is not None:
        user.email_verified = payload.email_verified
    db.commit()
    db.refresh(user)
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    if user_id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account.",
        )
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    db.delete(user)
    db.commit()


@router.get("/albums", response_model=list[AlbumRead])
def admin_list_albums(
    include_archived: bool = Query(False),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[AlbumRead]:
    stmt = (
        select(Album)
        .options(
            selectinload(Album.members).joinedload(AlbumMember.user),
            joinedload(Album.producer),
            joinedload(Album.mastering_engineer),
            joinedload(Album.workflow_template),
        )
        .order_by(Album.id.desc())
    )
    if not include_archived:
        stmt = stmt.where(Album.archived_at.is_(None))
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            Album.title.ilike(pattern, escape="\\")
            | Album.description.ilike(pattern, escape="\\")
        )
    stmt = stmt.limit(limit).offset(offset)
    albums = list(db.scalars(stmt).unique().all())
    return [_album_to_read(album, db) for album in albums]


@router.get("/albums/{album_id}/tracks", response_model=list[TrackRead])
def admin_list_album_tracks(
    album_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[TrackRead]:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    tracks = list(
        db.scalars(
            select(Track)
            .where(
                Track.album_id == album_id,
                Track.archived_at.is_(None),
                Track.status != TrackStatus.REJECTED,
            )
            .order_by(Track.track_number.asc().nulls_last(), Track.id)
        ).all()
    )
    return [build_track_read(track, admin, album, db=db) for track in tracks]
