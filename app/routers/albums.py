from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.album import Album
from app.models.track import Track
from app.schemas.schemas import AlbumCreate, AlbumRead, TrackRead

router = APIRouter(prefix="/api/albums", tags=["albums"])


def _album_to_read(album: Album, track_count: int) -> AlbumRead:
    return AlbumRead(
        id=album.id,
        title=album.title,
        description=album.description,
        cover_color=album.cover_color,
        created_at=album.created_at,
        updated_at=album.updated_at,
        track_count=track_count,
    )


@router.post("", response_model=AlbumRead, status_code=status.HTTP_201_CREATED)
def create_album(payload: AlbumCreate, db: Session = Depends(get_db)) -> AlbumRead:
    album = Album(**payload.model_dump())
    db.add(album)
    db.commit()
    db.refresh(album)
    return _album_to_read(album, 0)


@router.get("", response_model=list[AlbumRead])
def list_albums(db: Session = Depends(get_db)) -> list[AlbumRead]:
    # Sub-query to count tracks per album
    track_count_sub = (
        select(Track.album_id, func.count(Track.id).label("cnt"))
        .group_by(Track.album_id)
        .subquery()
    )
    stmt = select(Album, func.coalesce(track_count_sub.c.cnt, 0)).outerjoin(
        track_count_sub, Album.id == track_count_sub.c.album_id
    ).order_by(Album.id)

    results: list[AlbumRead] = []
    for album, cnt in db.execute(stmt).all():
        results.append(_album_to_read(album, int(cnt)))
    return results


@router.get("/{album_id}", response_model=AlbumRead)
def get_album(album_id: int, db: Session = Depends(get_db)) -> AlbumRead:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    track_count = db.scalar(
        select(func.count(Track.id)).where(Track.album_id == album_id)
    ) or 0
    return _album_to_read(album, track_count)


@router.get("/{album_id}/tracks", response_model=list[TrackRead])
def list_album_tracks(album_id: int, db: Session = Depends(get_db)) -> list[TrackRead]:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")

    stmt = select(Track).where(Track.album_id == album_id).order_by(Track.id)
    tracks = list(db.scalars(stmt).all())

    result: list[TrackRead] = []
    for t in tracks:
        issue_count = len(t.issues)
        open_issue_count = sum(1 for i in t.issues if i.status.value == "open")
        result.append(
            TrackRead(
                id=t.id,
                title=t.title,
                artist=t.artist,
                album_id=t.album_id,
                file_path=t.file_path,
                duration=t.duration,
                bpm=t.bpm,
                status=t.status,
                version=t.version,
                created_at=t.created_at,
                updated_at=t.updated_at,
                issue_count=issue_count,
                open_issue_count=open_issue_count,
            )
        )
    return result
