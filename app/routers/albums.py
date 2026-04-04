from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func as sqlfunc, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.issue import Issue, IssueStatus
from app.models.track import Track
from app.models.user import User
from app.models.workflow_event import WorkflowEvent
from app.schemas.schemas import AlbumCreate, AlbumRead, AlbumStats, AlbumTeamUpdate, TrackRead, UserRead
from app.security import get_current_user
from app.workflow import build_track_read, build_workflow_event_read, ensure_album_visibility, get_album_member_ids

router = APIRouter(prefix="/api/albums", tags=["albums"])


def _album_to_read(album: Album, db: Session) -> AlbumRead:
    members = [
        {
            "id": member.id,
            "user_id": member.user_id,
            "created_at": member.created_at,
            "user": UserRead.model_validate(member.user),
        }
        for member in album.members
    ]
    return AlbumRead(
        id=album.id,
        title=album.title,
        description=album.description,
        cover_color=album.cover_color,
        producer_id=album.producer_id,
        mastering_engineer_id=album.mastering_engineer_id,
        created_at=album.created_at,
        updated_at=album.updated_at,
        track_count=len(album.tracks),
        producer=UserRead.model_validate(album.producer) if album.producer else None,
        mastering_engineer=(
            UserRead.model_validate(album.mastering_engineer)
            if album.mastering_engineer
            else None
        ),
        members=members,
    )


@router.post("", response_model=AlbumRead, status_code=status.HTTP_201_CREATED)
def create_album(
    payload: AlbumCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlbumRead:
    album = Album(**payload.model_dump(), producer_id=current_user.id)
    db.add(album)
    db.flush()
    db.add(AlbumMember(album_id=album.id, user_id=current_user.id))
    db.commit()
    db.refresh(album)
    return _album_to_read(album, db)


@router.get("", response_model=list[AlbumRead])
def list_albums(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AlbumRead]:
    albums = list(db.scalars(select(Album).order_by(Album.id)).all())
    visible: list[AlbumRead] = []
    for album in albums:
        member_ids = get_album_member_ids(db, album.id)
        if current_user.id in {album.producer_id, album.mastering_engineer_id} | member_ids:
            visible.append(_album_to_read(album, db))
    return visible


@router.get("/{album_id}", response_model=AlbumRead)
def get_album(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlbumRead:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)
    return _album_to_read(album, db)


@router.patch("/{album_id}/team", response_model=AlbumRead)
def update_album_team(
    album_id: int,
    payload: AlbumTeamUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlbumRead:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    if album.producer_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the album producer can manage the team.",
        )

    if payload.mastering_engineer_id is not None:
        mastering_engineer = db.get(User, payload.mastering_engineer_id)
        if mastering_engineer is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Mastering engineer not found.",
            )
        album.mastering_engineer_id = mastering_engineer.id
    else:
        album.mastering_engineer_id = None

    desired_member_ids = set(payload.member_ids)
    desired_member_ids.add(current_user.id)
    for user_id in desired_member_ids:
        if db.get(User, user_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {user_id} not found.",
            )

    existing_members = {member.user_id: member for member in album.members}
    for user_id, member in list(existing_members.items()):
        if user_id not in desired_member_ids:
            db.delete(member)
    for user_id in desired_member_ids:
        if user_id not in existing_members:
            db.add(AlbumMember(album_id=album.id, user_id=user_id))

    db.commit()
    db.refresh(album)
    return _album_to_read(album, db)


@router.get("/{album_id}/stats", response_model=AlbumStats)
def get_album_stats(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlbumStats:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)

    tracks = list(db.scalars(select(Track).where(Track.album_id == album_id)).all())

    by_status: dict[str, int] = {}
    for track in tracks:
        key = track.status.value
        by_status[key] = by_status.get(key, 0) + 1

    open_issues = db.scalar(
        select(sqlfunc.count(Issue.id))
        .join(Track, Issue.track_id == Track.id)
        .where(Track.album_id == album_id, Issue.status == IssueStatus.OPEN)
    ) or 0

    recent_events = list(db.scalars(
        select(WorkflowEvent)
        .where(WorkflowEvent.album_id == album_id)
        .order_by(WorkflowEvent.created_at.desc())
        .limit(10)
    ).all())

    return AlbumStats(
        total_tracks=len(tracks),
        by_status=by_status,
        open_issues=open_issues,
        recent_events=[build_workflow_event_read(e, db) for e in recent_events],
    )


@router.get("/{album_id}/tracks", response_model=list[TrackRead])
def list_album_tracks(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TrackRead]:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)

    tracks = list(db.scalars(select(Track).where(Track.album_id == album_id).order_by(Track.id)).all())
    return [build_track_read(track, current_user, album) for track in tracks]
