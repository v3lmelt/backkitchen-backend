import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.album import Album
from app.models.issue import IssueStatus as IssueStatusEnum
from app.models.track import Track, TrackStatus
from app.schemas.schemas import TrackCreate, TrackListItem, TrackRead, TrackStatusUpdate
from app.services.audio import extract_audio_metadata

router = APIRouter(prefix="/api/tracks", tags=["tracks"])


def _track_to_read(track: Track) -> TrackRead:
    issue_count = len(track.issues)
    open_issue_count = sum(1 for i in track.issues if i.status == IssueStatusEnum.OPEN)
    return TrackRead(
        id=track.id,
        title=track.title,
        artist=track.artist,
        album_id=track.album_id,
        file_path=track.file_path,
        duration=track.duration,
        bpm=track.bpm,
        status=track.status,
        version=track.version,
        created_at=track.created_at,
        updated_at=track.updated_at,
        issue_count=issue_count,
        open_issue_count=open_issue_count,
    )


def _track_to_list_item(track: Track, album_title: str) -> TrackListItem:
    issue_count = len(track.issues)
    open_issue_count = sum(1 for i in track.issues if i.status == IssueStatusEnum.OPEN)
    return TrackListItem(
        id=track.id,
        title=track.title,
        artist=track.artist,
        album_id=track.album_id,
        album_title=album_title,
        file_path=track.file_path,
        duration=track.duration,
        bpm=track.bpm,
        status=track.status,
        version=track.version,
        created_at=track.created_at,
        updated_at=track.updated_at,
        issue_count=issue_count,
        open_issue_count=open_issue_count,
    )


@router.post("", response_model=TrackRead, status_code=status.HTTP_201_CREATED)
async def create_track(
    title: str = Form(...),
    artist: str = Form(...),
    album_id: int = Form(...),
    bpm: int | None = Form(default=None),
    file: UploadFile | None = None,
    db: Session = Depends(get_db),
) -> TrackRead:
    # Validate album exists
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Album not found."
        )

    file_path: str | None = None
    duration: float | None = None

    if file is not None:
        upload_dir = settings.get_upload_path()
        ext = Path(file.filename).suffix if file.filename else ".bin"
        unique_name = f"{uuid.uuid4().hex}{ext}"
        dest = upload_dir / unique_name

        content = await file.read()
        dest.write_bytes(content)

        file_path = str(dest)

        # Extract metadata
        meta = extract_audio_metadata(dest)
        duration = meta.duration

    track = Track(
        title=title,
        artist=artist,
        album_id=album_id,
        bpm=bpm,
        file_path=file_path,
        duration=duration,
    )
    db.add(track)
    db.commit()
    db.refresh(track)
    return _track_to_read(track)


@router.get("", response_model=list[TrackListItem])
def list_tracks(
    status_filter: TrackStatus | None = Query(default=None, alias="status"),
    album_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[TrackListItem]:
    stmt = select(Track).order_by(Track.id)
    if status_filter is not None:
        stmt = stmt.where(Track.status == status_filter)
    if album_id is not None:
        stmt = stmt.where(Track.album_id == album_id)

    tracks = list(db.scalars(stmt).all())

    # Collect album titles in bulk
    album_ids = {t.album_id for t in tracks}
    albums_by_id: dict[int, str] = {}
    if album_ids:
        album_rows = db.scalars(select(Album).where(Album.id.in_(album_ids))).all()
        albums_by_id = {a.id: a.title for a in album_rows}

    return [
        _track_to_list_item(t, albums_by_id.get(t.album_id, ""))
        for t in tracks
    ]


@router.get("/{track_id}", response_model=TrackRead)
def get_track(track_id: int, db: Session = Depends(get_db)) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    return _track_to_read(track)


@router.patch("/{track_id}/status", response_model=TrackRead)
def update_track_status(
    track_id: int,
    payload: TrackStatusUpdate,
    db: Session = Depends(get_db),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")

    track.status = payload.status
    # Bump version when moving to revision
    if payload.status == TrackStatus.REVISION:
        track.version += 1
    db.commit()
    db.refresh(track)
    return _track_to_read(track)


@router.delete("/{track_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_track(track_id: int, db: Session = Depends(get_db)) -> None:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")

    # Remove file from disk if it exists
    if track.file_path:
        p = Path(track.file_path)
        if p.exists():
            p.unlink()

    db.delete(track)
    db.commit()


@router.get("/{track_id}/audio")
def serve_audio(track_id: int, db: Session = Depends(get_db)) -> FileResponse:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    if not track.file_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No audio file for this track."
        )

    file_path = Path(track.file_path)
    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Audio file missing from disk."
        )

    mime_map = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
    }
    media_type = mime_map.get(file_path.suffix.lower(), "audio/octet-stream")

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=f"{track.title}{file_path.suffix}",
    )
