import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, load_only, selectinload

from app.database import get_db
from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.issue import Issue, IssueStatus
from app.models.stage_assignment import StageAssignment
from app.models.track import Track, TrackStatus
from app.models.user import User
from app.models.workflow_event import WorkflowEvent
from app.routers.albums import _album_to_read
from app.schemas.schemas import (
    AdminActivityLogEntry,
    AdminDashboardStats,
    AdminForceStatus,
    AdminReassign,
    AdminUserUpdate,
    AlbumRead,
    TrackRead,
    UserRead,
    WorkflowEventRead,
)
from app.security import get_current_user
from app.workflow import build_track_read, log_track_event
from app.workflow_engine import (
    ASSIGNMENT_CANCEL_REASON_REASSIGNED,
    _cancel_pending_review_assignments,
    parse_workflow_config,
)

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


# ── Dashboard stats ──────────────────────────────────────────────────────────


@router.get("/dashboard", response_model=AdminDashboardStats)
def admin_dashboard(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> AdminDashboardStats:
    users_by_role: dict[str, int] = {}
    for role, cnt in db.execute(select(User.role, func.count(User.id)).group_by(User.role)):
        users_by_role[role] = cnt
    total_users = sum(users_by_role.values())

    total_albums = db.scalar(select(func.count(Album.id))) or 0
    active_albums = db.scalar(
        select(func.count(Album.id)).where(Album.archived_at.is_(None))
    ) or 0

    tracks_by_status: dict[str, int] = {}
    for st, cnt in db.execute(
        select(Track.status, func.count(Track.id))
        .where(Track.archived_at.is_(None))
        .group_by(Track.status)
    ):
        tracks_by_status[st] = cnt
    total_tracks = sum(tracks_by_status.values())

    open_issues = db.scalar(
        select(func.count(Issue.id)).where(Issue.status == IssueStatus.OPEN)
    ) or 0

    recent_events_rows = list(
        db.scalars(
            select(WorkflowEvent)
            .options(joinedload(WorkflowEvent.actor))
            .order_by(WorkflowEvent.created_at.desc())
            .limit(20)
        ).unique().all()
    )
    recent_events = [
        WorkflowEventRead(
            id=e.id,
            event_type=e.event_type,
            from_status=e.from_status,
            to_status=e.to_status,
            payload=json.loads(e.payload) if e.payload else None,
            created_at=e.created_at,
            actor=None if e.actor is None else UserRead.model_validate(e.actor),
        )
        for e in recent_events_rows
    ]

    return AdminDashboardStats(
        total_users=total_users,
        users_by_role=users_by_role,
        total_albums=total_albums,
        active_albums=active_albums,
        total_tracks=total_tracks,
        tracks_by_status=tracks_by_status,
        open_issues=open_issues,
        recent_events=recent_events,
    )


# ── Activity log ─────────────────────────────────────────────────────────────


@router.get("/activity-log", response_model=list[AdminActivityLogEntry])
def admin_activity_log(
    album_id: int | None = Query(default=None),
    event_type: str | None = Query(default=None),
    actor_user_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[AdminActivityLogEntry]:
    stmt = (
        select(WorkflowEvent)
        .options(
            joinedload(WorkflowEvent.actor),
            joinedload(WorkflowEvent.track).load_only(Track.id, Track.title),
        )
        .order_by(WorkflowEvent.created_at.desc())
    )
    if album_id is not None:
        stmt = stmt.where(WorkflowEvent.album_id == album_id)
    if event_type is not None:
        stmt = stmt.where(WorkflowEvent.event_type == event_type)
    if actor_user_id is not None:
        stmt = stmt.where(WorkflowEvent.actor_user_id == actor_user_id)
    stmt = stmt.limit(limit).offset(offset)

    events = list(db.scalars(stmt).unique().all())

    album_ids = {e.album_id for e in events if e.album_id}
    album_map: dict[int, str] = {}
    if album_ids:
        for aid, title in db.execute(
            select(Album.id, Album.title).where(Album.id.in_(album_ids))
        ):
            album_map[aid] = title

    return [
        AdminActivityLogEntry(
            id=e.id,
            event_type=e.event_type,
            from_status=e.from_status,
            to_status=e.to_status,
            payload=json.loads(e.payload) if e.payload else None,
            created_at=e.created_at,
            actor=None if e.actor is None else UserRead.model_validate(e.actor),
            track_id=e.track_id,
            track_title=e.track.title if e.track else None,
            album_id=e.album_id,
            album_title=album_map.get(e.album_id) if e.album_id else None,
        )
        for e in events
    ]


# ── Workflow intervention ────────────────────────────────────────────────────


@router.post("/tracks/{track_id}/force-status")
def admin_force_status(
    track_id: int,
    payload: AdminForceStatus,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")

    config = parse_workflow_config(album)
    valid_step_ids = {s["id"] for s in config.get("steps", [])}
    terminal = {s.value for s in TrackStatus}
    valid_statuses = valid_step_ids | terminal
    if payload.new_status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Valid: {sorted(valid_statuses)}",
        )

    old_status = track.status
    track.status = payload.new_status
    log_track_event(
        db, track, admin, "admin_force_status",
        from_status=old_status,
        to_status=payload.new_status,
        payload={"reason": payload.reason},
    )
    db.commit()
    db.refresh(track)
    return build_track_read(track, admin, album, db=db)


@router.post("/tracks/{track_id}/reassign")
def admin_reassign(
    track_id: int,
    payload: AdminReassign,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    album_member_ids = set(
        db.scalars(
            select(AlbumMember.user_id).where(AlbumMember.album_id == album.id)
        ).all()
    )
    new_users: list[User] = []
    for uid in payload.user_ids:
        u = db.get(User, uid)
        if u is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {uid} not found.",
            )
        if uid not in album_member_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"User {u.display_name} is not a member of this album.",
            )
        new_users.append(u)

    old_reviewer_id = track.peer_reviewer_id
    track.peer_reviewer_id = payload.user_ids[0]

    # Cancel existing pending assignments for the current stage so they are
    # filtered out by the frontend (which hides status == "cancelled").
    _cancel_pending_review_assignments(
        db,
        track.id,
        track.status,
        reason=ASSIGNMENT_CANCEL_REASON_REASSIGNED,
    )

    # Create new assignments for each user
    for u in new_users:
        db.add(StageAssignment(
            track_id=track.id,
            stage_id=track.status,
            user_id=u.id,
            status="pending",
        ))

    log_track_event(
        db, track, admin, "admin_reassign",
        payload={
            "reason": payload.reason,
            "old_reviewer_id": old_reviewer_id,
            "new_user_ids": payload.user_ids,
            "new_user_names": [u.display_name for u in new_users],
            "stage": track.status,
        },
    )
    db.commit()
    db.refresh(track)
    return build_track_read(track, admin, album, db=db)
