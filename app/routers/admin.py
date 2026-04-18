import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload, load_only, selectinload

from app.admin_audit import record_admin_audit
from app.admin_permissions import (
    ADMIN_ROLE_OPERATOR,
    ADMIN_ROLE_SUPERADMIN,
    has_admin_role,
    normalize_admin_role,
    require_admin,
    sync_admin_role_flags,
)
from app.database import get_db
from app.models.admin_audit_log import AdminAuditLog
from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.circle import Circle, CircleInviteCode, CircleMember
from app.models.issue import Issue, IssueStatus
from app.models.reopen_request import ReopenRequest
from app.models.stage_assignment import StageAssignment
from app.models.track import RejectionMode, Track, TrackStatus
from app.models.user import User
from app.models.webhook_delivery import WebhookDelivery
from app.models.workflow_event import WorkflowEvent
from app.models.workflow_template import WorkflowTemplate
from app.routers.albums import _album_to_read
from app.schemas.schemas import (
    AdminActivityLogEntry,
    AdminAuditLogRead,
    AdminDashboardStats,
    AdminForceStatus,
    AdminReasonPayload,
    AdminReassign,
    AdminReopenDecision,
    AdminReopenRequestEntry,
    AdminTrackReopen,
    AdminTransferOwnershipRequest,
    AdminUserUpdate,
    AlbumRead,
    CircleSummary,
    TrackRead,
    UserRead,
    WorkflowEventRead,
)
from app.services.cleanup import cleanup_files, collect_track_files
from app.workflow import build_track_read, log_track_event
from app.workflow_engine import (
    ASSIGNMENT_CANCEL_REASON_REASSIGNED,
    _cancel_pending_review_assignments,
    parse_workflow_config,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _user_snapshot(user: User) -> dict[str, Any]:
    sync_admin_role_flags(user)
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "role": user.role,
        "email_verified": user.email_verified,
        "is_admin": user.is_admin,
        "admin_role": user.admin_role,
        "suspended_at": user.suspended_at,
        "suspension_reason": user.suspension_reason,
        "deleted_at": user.deleted_at,
    }


def _track_snapshot(track: Track) -> dict[str, Any]:
    return {
        "id": track.id,
        "title": track.title,
        "status": track.status,
        "peer_reviewer_id": track.peer_reviewer_id,
        "submitter_id": track.submitter_id,
        "archived_at": track.archived_at,
    }


def _deserialize_json_object(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    parsed = json.loads(value)
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


def _audit_to_read(entry: AdminAuditLog) -> AdminAuditLogRead:
    actor = None
    target_user = None
    if entry.actor is not None:
        sync_admin_role_flags(entry.actor)
        actor = UserRead.model_validate(entry.actor)
    if entry.target_user is not None:
        sync_admin_role_flags(entry.target_user)
        target_user = UserRead.model_validate(entry.target_user)
    return AdminAuditLogRead(
        id=entry.id,
        action=entry.action,
        entity_type=entry.entity_type,
        entity_id=entry.entity_id,
        summary=entry.summary,
        reason=entry.reason,
        before_state=_deserialize_json_object(entry.before_state),
        after_state=_deserialize_json_object(entry.after_state),
        target_user_id=entry.target_user_id,
        album_id=entry.album_id,
        track_id=entry.track_id,
        circle_id=entry.circle_id,
        created_at=entry.created_at,
        actor=actor,
        target_user=target_user,
    )


def _reopen_request_to_read(request_obj: ReopenRequest, album_titles: dict[int, str]) -> AdminReopenRequestEntry:
    requested_by = None
    decided_by = None
    if request_obj.requested_by is not None:
        sync_admin_role_flags(request_obj.requested_by)
        requested_by = UserRead.model_validate(request_obj.requested_by)
    if request_obj.decided_by is not None:
        sync_admin_role_flags(request_obj.decided_by)
        decided_by = UserRead.model_validate(request_obj.decided_by)
    album_id = request_obj.track.album_id if request_obj.track else None
    return AdminReopenRequestEntry(
        id=request_obj.id,
        track_id=request_obj.track_id,
        track_title=request_obj.track.title if request_obj.track else None,
        album_id=album_id,
        album_title=album_titles.get(album_id) if album_id is not None else None,
        requested_by_id=request_obj.requested_by_id,
        target_stage_id=request_obj.target_stage_id,
        reason=request_obj.reason,
        mastering_notes=request_obj.mastering_notes,
        status=request_obj.status,
        decided_by_id=request_obj.decided_by_id,
        created_at=request_obj.created_at,
        decided_at=request_obj.decided_at,
        requested_by=requested_by,
        decided_by=decided_by,
    )


def _ensure_target_user_mutable(actor: User, target: User) -> None:
    if normalize_admin_role(target) == ADMIN_ROLE_SUPERADMIN and not has_admin_role(actor, ADMIN_ROLE_SUPERADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a superadmin can manage another superadmin account.",
        )


def _active_track_filter() -> Any:
    return or_(
        Track.status.notin_([TrackStatus.COMPLETED.value, TrackStatus.REJECTED.value]),
        and_(
            Track.status == TrackStatus.REJECTED.value,
            Track.rejection_mode == RejectionMode.RESUBMITTABLE,
        ),
    )


def _assert_user_can_be_deactivated(db: Session, user: User) -> None:
    owned_circle = db.scalar(
        select(Circle.id).where(Circle.created_by == user.id).limit(1)
    )
    if owned_circle is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User still owns one or more circles. Transfer ownership first.",
        )

    produced_album = db.scalar(
        select(Album.id).where(
            Album.producer_id == user.id,
            Album.archived_at.is_(None),
        ).limit(1)
    )
    if produced_album is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User still produces one or more active albums. Transfer ownership first.",
        )

    authored_track = db.scalar(
        select(Track.id).where(
            Track.submitter_id == user.id,
            Track.archived_at.is_(None),
            _active_track_filter(),
        ).limit(1)
    )
    if authored_track is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User still owns one or more active tracks. Transfer track ownership first.",
        )

    mastering_track = db.scalar(
        select(Track.id)
        .join(Album, Album.id == Track.album_id)
        .where(
            Album.mastering_engineer_id == user.id,
            Track.archived_at.is_(None),
            _active_track_filter(),
        )
        .limit(1)
    )
    if mastering_track is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is still responsible for active mastering work. Transfer ownership first.",
        )


def _ensure_album_membership(db: Session, album_id: int, user_id: int) -> None:
    existing = db.scalar(
        select(AlbumMember.id).where(
            AlbumMember.album_id == album_id,
            AlbumMember.user_id == user_id,
        )
    )
    if existing is None:
        db.add(AlbumMember(album_id=album_id, user_id=user_id))


def _ensure_circle_owner_membership(db: Session, circle_id: int, user_id: int) -> None:
    membership = db.scalar(
        select(CircleMember).where(
            CircleMember.circle_id == circle_id,
            CircleMember.user_id == user_id,
        )
    )
    if membership is None:
        db.add(CircleMember(circle_id=circle_id, user_id=user_id, role="owner"))
        return
    membership.role = "owner"


def _transfer_user_ownership(
    db: Session,
    *,
    source_user: User,
    target_user: User,
) -> dict[str, int]:
    counts = {
        "circles": 0,
        "albums": 0,
        "mastering_albums": 0,
        "active_tracks": 0,
        "pending_assignments": 0,
        "review_tracks": 0,
    }

    circles = list(db.scalars(select(Circle).where(Circle.created_by == source_user.id)).all())
    for circle in circles:
        circle.created_by = target_user.id
        _ensure_circle_owner_membership(db, circle.id, target_user.id)
        counts["circles"] += 1

    if counts["circles"]:
        db.execute(
            CircleInviteCode.__table__.update()
            .where(CircleInviteCode.created_by == source_user.id)
            .values(created_by=target_user.id)
        )
        db.execute(
            WorkflowTemplate.__table__.update()
            .where(WorkflowTemplate.created_by == source_user.id)
            .values(created_by=target_user.id)
        )

    albums = list(db.scalars(select(Album).where(Album.producer_id == source_user.id)).all())
    for album in albums:
        album.producer_id = target_user.id
        _ensure_album_membership(db, album.id, target_user.id)
        counts["albums"] += 1

    mastering_albums = list(
        db.scalars(select(Album).where(Album.mastering_engineer_id == source_user.id)).all()
    )
    for album in mastering_albums:
        album.mastering_engineer_id = target_user.id
        _ensure_album_membership(db, album.id, target_user.id)
        counts["mastering_albums"] += 1

    active_tracks = list(
        db.scalars(
            select(Track).where(
                Track.submitter_id == source_user.id,
                Track.archived_at.is_(None),
                _active_track_filter(),
            )
        ).all()
    )
    for track in active_tracks:
        track.submitter_id = target_user.id
        counts["active_tracks"] += 1

    review_tracks = list(
        db.scalars(
            select(Track).where(
                Track.peer_reviewer_id == source_user.id,
                Track.archived_at.is_(None),
                _active_track_filter(),
            )
        ).all()
    )
    for track in review_tracks:
        track.peer_reviewer_id = target_user.id
        counts["review_tracks"] += 1

    assignments = list(
        db.scalars(
            select(StageAssignment).where(
                StageAssignment.user_id == source_user.id,
                StageAssignment.status == "pending",
            )
        ).all()
    )
    for assignment in assignments:
        assignment.user_id = target_user.id
        counts["pending_assignments"] += 1

    return counts


@router.get("/users", response_model=list[UserRead])
def list_users(
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin()),
) -> list[User]:
    stmt = select(User).order_by(User.id)
    if not include_deleted:
        stmt = stmt.where(User.deleted_at.is_(None))
    users = list(db.scalars(stmt.limit(limit).offset(offset)).all())
    for user in users:
        sync_admin_role_flags(user)
    return users


@router.patch("/users/{user_id}", response_model=UserRead)
def update_user(
    user_id: int,
    payload: AdminUserUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_OPERATOR)),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    _ensure_target_user_mutable(admin, user)

    before = _user_snapshot(user)

    if payload.admin_role is not None or payload.is_admin is not None:
        if not has_admin_role(admin, ADMIN_ROLE_SUPERADMIN):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only a superadmin can change admin access.",
            )
        if payload.admin_role is not None:
            user.admin_role = payload.admin_role
        else:
            user.admin_role = "viewer" if payload.is_admin else "none"

    if payload.role is not None:
        user.role = payload.role
    if payload.email_verified is not None:
        user.email_verified = payload.email_verified

    sync_admin_role_flags(user)
    record_admin_audit(
        db,
        actor=admin,
        action="user_updated",
        entity_type="user",
        entity_id=user.id,
        summary=f"Updated user {user.display_name}",
        before=before,
        after=_user_snapshot(user),
        target_user_id=user.id,
    )
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/suspend", response_model=UserRead)
def suspend_user(
    user_id: int,
    payload: AdminReasonPayload,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_OPERATOR)),
) -> User:
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot suspend your own account.")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    _ensure_target_user_mutable(admin, user)

    before = _user_snapshot(user)
    user.suspended_at = datetime.now(timezone.utc)
    user.suspension_reason = payload.reason
    user.session_version = max(int(user.session_version or 1), 1) + 1

    record_admin_audit(
        db,
        actor=admin,
        action="user_suspended",
        entity_type="user",
        entity_id=user.id,
        summary=f"Suspended user {user.display_name}",
        reason=payload.reason,
        before=before,
        after=_user_snapshot(user),
        target_user_id=user.id,
    )
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/restore", response_model=UserRead)
def restore_user(
    user_id: int,
    payload: AdminReasonPayload,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_OPERATOR)),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    _ensure_target_user_mutable(admin, user)

    before = _user_snapshot(user)
    user.deleted_at = None
    user.suspended_at = None
    user.suspension_reason = None
    user.session_version = max(int(user.session_version or 1), 1) + 1

    record_admin_audit(
        db,
        actor=admin,
        action="user_restored",
        entity_type="user",
        entity_id=user.id,
        summary=f"Restored user {user.display_name}",
        reason=payload.reason,
        before=before,
        after=_user_snapshot(user),
        target_user_id=user.id,
    )
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/revoke-sessions", response_model=UserRead)
def revoke_user_sessions(
    user_id: int,
    payload: AdminReasonPayload,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_OPERATOR)),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    _ensure_target_user_mutable(admin, user)

    before = _user_snapshot(user)
    user.session_version = max(int(user.session_version or 1), 1) + 1
    record_admin_audit(
        db,
        actor=admin,
        action="user_sessions_revoked",
        entity_type="user",
        entity_id=user.id,
        summary=f"Revoked sessions for {user.display_name}",
        reason=payload.reason,
        before=before,
        after=_user_snapshot(user),
        target_user_id=user.id,
    )
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/transfer-ownership")
def transfer_user_ownership(
    user_id: int,
    payload: AdminTransferOwnershipRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_OPERATOR)),
) -> dict[str, int]:
    if user_id == payload.target_user_id:
        raise HTTPException(status_code=400, detail="Source and target users must be different.")
    source_user = db.get(User, user_id)
    if source_user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    target_user = db.get(User, payload.target_user_id)
    if target_user is None or target_user.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Target user not found.")
    _ensure_target_user_mutable(admin, source_user)

    counts = _transfer_user_ownership(db, source_user=source_user, target_user=target_user)
    record_admin_audit(
        db,
        actor=admin,
        action="user_ownership_transferred",
        entity_type="user",
        entity_id=source_user.id,
        summary=f"Transferred ownership from {source_user.display_name} to {target_user.display_name}",
        reason=payload.reason,
        before={"source_user_id": source_user.id},
        after={"target_user_id": target_user.id, **counts},
        target_user_id=source_user.id,
    )
    db.commit()
    return counts


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    reason: str = Query(default="Deactivated by admin", min_length=1, max_length=500),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_SUPERADMIN)),
) -> None:
    if user_id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account.",
        )
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    _ensure_target_user_mutable(admin, user)
    _assert_user_can_be_deactivated(db, user)

    before = _user_snapshot(user)
    now = datetime.now(timezone.utc)
    user.deleted_at = now
    user.suspended_at = now
    user.suspension_reason = reason
    user.session_version = max(int(user.session_version or 1), 1) + 1

    record_admin_audit(
        db,
        actor=admin,
        action="user_deleted",
        entity_type="user",
        entity_id=user.id,
        summary=f"Soft-deleted user {user.display_name}",
        reason=reason,
        before=before,
        after=_user_snapshot(user),
        target_user_id=user.id,
    )
    db.commit()


@router.get("/albums", response_model=list[AlbumRead])
def admin_list_albums(
    include_archived: bool = Query(False),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin()),
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
            | Album.circle_name.ilike(pattern, escape="\\")
        )
    albums = list(db.scalars(stmt.limit(limit).offset(offset)).unique().all())
    return [_album_to_read(album, db) for album in albums]


@router.get("/circles", response_model=list[CircleSummary])
def admin_list_circles(
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin()),
) -> list[CircleSummary]:
    stmt = select(Circle).options(selectinload(Circle.members)).order_by(Circle.id.desc())
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            Circle.name.ilike(pattern, escape="\\")
            | Circle.description.ilike(pattern, escape="\\")
        )
    circles = list(db.scalars(stmt.limit(limit).offset(offset)).all())
    return [
        CircleSummary(
            id=circle.id,
            name=circle.name,
            description=circle.description,
            logo_url=circle.logo_url,
            default_checklist_enabled=circle.default_checklist_enabled,
            created_by=circle.created_by,
            member_count=len(circle.members),
        )
        for circle in circles
    ]


@router.get("/albums/{album_id}/tracks", response_model=list[TrackRead])
def admin_list_album_tracks(
    album_id: int,
    include_archived: bool = Query(default=True),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin()),
) -> list[TrackRead]:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")

    stmt = select(Track).where(Track.album_id == album_id)
    if not include_archived:
        stmt = stmt.where(Track.archived_at.is_(None))
    if status_filter:
        stmt = stmt.where(Track.status == status_filter)
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            Track.title.ilike(pattern, escape="\\")
            | Track.artist.ilike(pattern, escape="\\")
        )
    tracks = list(
        db.scalars(
            stmt.order_by(Track.archived_at.asc().nulls_first(), Track.track_number.asc().nulls_last(), Track.id)
        ).all()
    )
    return [build_track_read(track, admin, album, db=db) for track in tracks]


@router.get("/reopen-requests", response_model=list[AdminReopenRequestEntry])
def list_reopen_requests(
    album_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin()),
) -> list[AdminReopenRequestEntry]:
    stmt = (
        select(ReopenRequest)
        .options(
            joinedload(ReopenRequest.requested_by),
            joinedload(ReopenRequest.decided_by),
            joinedload(ReopenRequest.track).load_only(Track.id, Track.title, Track.album_id),
        )
        .order_by(ReopenRequest.created_at.desc())
    )
    if status_filter is not None:
        stmt = stmt.where(ReopenRequest.status == status_filter)
    if album_id is not None:
        stmt = stmt.join(Track, Track.id == ReopenRequest.track_id).where(Track.album_id == album_id)
    requests = list(db.scalars(stmt.limit(limit).offset(offset)).unique().all())
    album_ids = {request_obj.track.album_id for request_obj in requests if request_obj.track is not None}
    album_titles = {
        album_row.id: album_row.title
        for album_row in db.scalars(select(Album).where(Album.id.in_(album_ids))).all()
    } if album_ids else {}
    return [_reopen_request_to_read(request_obj, album_titles) for request_obj in requests]


@router.get("/dashboard", response_model=AdminDashboardStats)
def admin_dashboard(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin()),
) -> AdminDashboardStats:
    users_by_role: dict[str, int] = {}
    for role, cnt in db.execute(
        select(User.role, func.count(User.id))
        .where(User.deleted_at.is_(None))
        .group_by(User.role)
    ):
        users_by_role[role] = cnt
    total_users = sum(users_by_role.values())

    total_albums = db.scalar(select(func.count(Album.id))) or 0
    active_albums = db.scalar(
        select(func.count(Album.id)).where(Album.archived_at.is_(None))
    ) or 0
    archived_albums = db.scalar(
        select(func.count(Album.id)).where(Album.archived_at.isnot(None))
    ) or 0

    tracks_by_status: dict[str, int] = {}
    for st, cnt in db.execute(
        select(Track.status, func.count(Track.id))
        .where(Track.archived_at.is_(None))
        .group_by(Track.status)
    ):
        tracks_by_status[st] = cnt
    total_tracks = sum(tracks_by_status.values())
    archived_tracks = db.scalar(
        select(func.count(Track.id)).where(Track.archived_at.isnot(None))
    ) or 0

    open_issues = db.scalar(
        select(func.count(Issue.id)).where(Issue.status == IssueStatus.OPEN)
    ) or 0
    pending_reopen_requests = db.scalar(
        select(func.count(ReopenRequest.id)).where(ReopenRequest.status == "pending")
    ) or 0
    failed_webhook_deliveries = db.scalar(
        select(func.count(WebhookDelivery.id)).where(WebhookDelivery.success.is_(False))
    ) or 0
    unverified_users = db.scalar(
        select(func.count(User.id)).where(User.deleted_at.is_(None), User.email_verified.is_(False))
    ) or 0
    suspended_users = db.scalar(
        select(func.count(User.id)).where(User.deleted_at.is_(None), User.suspended_at.isnot(None))
    ) or 0
    stalled_tracks = db.scalar(
        select(func.count(Track.id)).where(
            Track.archived_at.is_(None),
            Track.status.notin_([TrackStatus.COMPLETED.value, TrackStatus.REJECTED.value]),
            Track.updated_at < datetime.now(timezone.utc) - timedelta(days=7),
        )
    ) or 0

    recent_events_rows = list(
        db.scalars(
            select(WorkflowEvent)
            .options(joinedload(WorkflowEvent.actor))
            .order_by(WorkflowEvent.created_at.desc())
            .limit(12)
        ).unique().all()
    )
    recent_events = []
    for event in recent_events_rows:
        actor = None
        if event.actor is not None:
            sync_admin_role_flags(event.actor)
            actor = UserRead.model_validate(event.actor)
        recent_events.append(
            WorkflowEventRead(
                id=event.id,
                event_type=event.event_type,
                from_status=event.from_status,
                to_status=event.to_status,
                payload=json.loads(event.payload) if event.payload else None,
                created_at=event.created_at,
                actor=actor,
            )
        )

    recent_audits_rows = list(
        db.scalars(
            select(AdminAuditLog)
            .options(joinedload(AdminAuditLog.actor), joinedload(AdminAuditLog.target_user))
            .order_by(AdminAuditLog.created_at.desc())
            .limit(12)
        ).unique().all()
    )

    return AdminDashboardStats(
        total_users=total_users,
        users_by_role=users_by_role,
        total_albums=total_albums,
        active_albums=active_albums,
        archived_albums=archived_albums,
        total_tracks=total_tracks,
        tracks_by_status=tracks_by_status,
        archived_tracks=archived_tracks,
        open_issues=open_issues,
        pending_reopen_requests=pending_reopen_requests,
        failed_webhook_deliveries=failed_webhook_deliveries,
        unverified_users=unverified_users,
        suspended_users=suspended_users,
        stalled_tracks=stalled_tracks,
        recent_events=recent_events,
        recent_audits=[_audit_to_read(entry) for entry in recent_audits_rows],
    )


@router.get("/activity-log", response_model=list[AdminActivityLogEntry])
def admin_activity_log(
    album_id: int | None = Query(default=None),
    event_type: str | None = Query(default=None),
    actor_user_id: int | None = Query(default=None),
    from_time: datetime | None = Query(default=None),
    to_time: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin()),
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
    if from_time is not None:
        stmt = stmt.where(WorkflowEvent.created_at >= from_time)
    if to_time is not None:
        stmt = stmt.where(WorkflowEvent.created_at <= to_time)
    events = list(db.scalars(stmt.limit(limit).offset(offset)).unique().all())

    album_ids = {event.album_id for event in events if event.album_id}
    album_map: dict[int, str] = {}
    if album_ids:
        for album_id_value, title in db.execute(
            select(Album.id, Album.title).where(Album.id.in_(album_ids))
        ):
            album_map[album_id_value] = title

    results: list[AdminActivityLogEntry] = []
    for event in events:
        actor = None
        if event.actor is not None:
            sync_admin_role_flags(event.actor)
            actor = UserRead.model_validate(event.actor)
        results.append(
            AdminActivityLogEntry(
                id=event.id,
                event_type=event.event_type,
                from_status=event.from_status,
                to_status=event.to_status,
                payload=json.loads(event.payload) if event.payload else None,
                created_at=event.created_at,
                actor=actor,
                track_id=event.track_id,
                track_title=event.track.title if event.track else None,
                album_id=event.album_id,
                album_title=album_map.get(event.album_id) if event.album_id else None,
            )
        )
    return results


@router.get("/audit-log", response_model=list[AdminAuditLogRead])
def admin_audit_log(
    action: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    actor_user_id: int | None = Query(default=None),
    target_user_id: int | None = Query(default=None),
    from_time: datetime | None = Query(default=None),
    to_time: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin()),
) -> list[AdminAuditLogRead]:
    stmt = (
        select(AdminAuditLog)
        .options(joinedload(AdminAuditLog.actor), joinedload(AdminAuditLog.target_user))
        .order_by(AdminAuditLog.created_at.desc())
    )
    if action is not None:
        stmt = stmt.where(AdminAuditLog.action == action)
    if entity_type is not None:
        stmt = stmt.where(AdminAuditLog.entity_type == entity_type)
    if actor_user_id is not None:
        stmt = stmt.where(AdminAuditLog.actor_user_id == actor_user_id)
    if target_user_id is not None:
        stmt = stmt.where(AdminAuditLog.target_user_id == target_user_id)
    if from_time is not None:
        stmt = stmt.where(AdminAuditLog.created_at >= from_time)
    if to_time is not None:
        stmt = stmt.where(AdminAuditLog.created_at <= to_time)
    entries = list(db.scalars(stmt.limit(limit).offset(offset)).unique().all())
    return [_audit_to_read(entry) for entry in entries]


@router.post("/tracks/{track_id}/force-status", response_model=TrackRead)
def admin_force_status(
    track_id: int,
    payload: AdminForceStatus,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_OPERATOR)),
) -> TrackRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")

    config = parse_workflow_config(album)
    valid_step_ids = {step["id"] for step in config.get("steps", [])}
    terminal = {status_value.value for status_value in TrackStatus}
    valid_statuses = valid_step_ids | terminal
    if payload.new_status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Valid: {sorted(valid_statuses)}",
        )

    before = _track_snapshot(track)
    old_status = track.status
    track.status = payload.new_status
    log_track_event(
        db,
        track,
        admin,
        "admin_force_status",
        from_status=old_status,
        to_status=payload.new_status,
        payload={"reason": payload.reason},
    )
    record_admin_audit(
        db,
        actor=admin,
        action="track_force_status",
        entity_type="track",
        entity_id=track.id,
        summary=f"Force-set track {track.title} to {payload.new_status}",
        reason=payload.reason,
        before=before,
        after=_track_snapshot(track),
        album_id=album.id,
        track_id=track.id,
    )
    db.commit()
    db.refresh(track)
    return build_track_read(track, admin, album, db=db)


@router.post("/tracks/{track_id}/reassign", response_model=TrackRead)
def admin_reassign(
    track_id: int,
    payload: AdminReassign,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_OPERATOR)),
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
    for user_id in payload.user_ids:
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User {user_id} not found.")
        if user_id not in album_member_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"User {user.display_name} is not a member of this album.",
            )
        new_users.append(user)

    before = _track_snapshot(track)
    old_reviewer_id = track.peer_reviewer_id
    track.peer_reviewer_id = payload.user_ids[0]

    _cancel_pending_review_assignments(
        db,
        track.id,
        track.status,
        reason=ASSIGNMENT_CANCEL_REASON_REASSIGNED,
    )
    for user in new_users:
        db.add(
            StageAssignment(
                track_id=track.id,
                stage_id=track.status,
                user_id=user.id,
                status="pending",
            )
        )

    log_track_event(
        db,
        track,
        admin,
        "admin_reassign",
        payload={
            "reason": payload.reason,
            "old_reviewer_id": old_reviewer_id,
            "new_user_ids": payload.user_ids,
            "new_user_names": [user.display_name for user in new_users],
            "stage": track.status,
        },
    )
    record_admin_audit(
        db,
        actor=admin,
        action="track_reassigned",
        entity_type="track",
        entity_id=track.id,
        summary=f"Reassigned track {track.title}",
        reason=payload.reason,
        before=before,
        after=_track_snapshot(track),
        album_id=album.id,
        track_id=track.id,
    )
    db.commit()
    db.refresh(track)
    return build_track_read(track, admin, album, db=db)


@router.post("/tracks/{track_id}/reopen", response_model=TrackRead)
def admin_reopen_track(
    track_id: int,
    payload: AdminTrackReopen,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_OPERATOR)),
) -> TrackRead:
    from app.workflow_engine import execute_reopen

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found.")
    if track.status != TrackStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="Only completed tracks can be reopened.")

    before = _track_snapshot(track)
    execute_reopen(db, album, track, admin, payload.target_stage_id, background_tasks)
    record_admin_audit(
        db,
        actor=admin,
        action="track_reopened",
        entity_type="track",
        entity_id=track.id,
        summary=f"Reopened track {track.title}",
        reason=payload.reason,
        before=before,
        after=_track_snapshot(track),
        album_id=album.id,
        track_id=track.id,
    )
    db.commit()
    db.refresh(track)
    return build_track_read(track, admin, album, db=db)


@router.post("/reopen-requests/{request_id}/decide", response_model=AdminReopenRequestEntry)
def admin_decide_reopen_request(
    request_id: int,
    payload: AdminReopenDecision,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_OPERATOR)),
) -> AdminReopenRequestEntry:
    from app.notifications import notify
    from app.workflow_engine import execute_reopen

    request_obj = db.get(ReopenRequest, request_id)
    if request_obj is None:
        raise HTTPException(status_code=404, detail="Reopen request not found.")
    if request_obj.status != "pending":
        raise HTTPException(status_code=409, detail="Request already decided.")

    track = db.get(Track, request_obj.track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found.")

    before = {
        "request_status": request_obj.status,
        "track": _track_snapshot(track),
    }
    request_obj.status = "approved" if payload.decision == "approve" else "rejected"
    request_obj.decided_by_id = admin.id
    request_obj.decided_at = datetime.now(timezone.utc)

    if payload.decision == "approve":
        if request_obj.mastering_notes:
            track.mastering_notes = request_obj.mastering_notes
        execute_reopen(db, album, track, admin, request_obj.target_stage_id, background_tasks)
        notify(
            db,
            [request_obj.requested_by_id],
            "reopen_approved",
            "Reopen request approved",
            f"Your reopen request for {track.title} has been approved.",
            related_track_id=track.id,
            background_tasks=background_tasks,
            album_id=track.album_id,
            webhook_context={"actor_id": admin.id, "actor_name": admin.display_name},
        )
    else:
        notify(
            db,
            [request_obj.requested_by_id],
            "reopen_rejected",
            "Reopen request rejected",
            f"Your reopen request for {track.title} has been rejected.",
            related_track_id=track.id,
            background_tasks=background_tasks,
            album_id=track.album_id,
            webhook_context={"actor_id": admin.id, "actor_name": admin.display_name},
        )

    record_admin_audit(
        db,
        actor=admin,
        action="reopen_request_decided",
        entity_type="reopen_request",
        entity_id=request_obj.id,
        summary=f"{payload.decision.title()} reopen request for {track.title}",
        reason=payload.reason,
        before=before,
        after={
            "request_status": request_obj.status,
            "track": _track_snapshot(track),
        },
        album_id=album.id,
        track_id=track.id,
    )
    db.commit()
    db.refresh(request_obj)
    return _reopen_request_to_read(request_obj, {album.id: album.title})


@router.post("/tracks/{track_id}/archive", response_model=TrackRead)
def admin_archive_track(
    track_id: int,
    payload: AdminReasonPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_OPERATOR)),
) -> TrackRead:
    from app.notifications import notify

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found.")
    if track.archived_at is not None:
        raise HTTPException(status_code=409, detail="Track is already archived.")

    before = _track_snapshot(track)
    track.archived_at = datetime.now(timezone.utc)
    log_track_event(db, track, admin, "track_archived", payload={"previous_status": track.status})
    notify(
        db,
        [track.submitter_id],
        "track_archived",
        "Track archived",
        f"{track.title} has been archived by an administrator.",
        related_track_id=track.id,
        background_tasks=background_tasks,
        album_id=track.album_id,
        webhook_context={"actor_id": admin.id, "actor_name": admin.display_name},
    )
    record_admin_audit(
        db,
        actor=admin,
        action="track_archived",
        entity_type="track",
        entity_id=track.id,
        summary=f"Archived track {track.title}",
        reason=payload.reason,
        before=before,
        after=_track_snapshot(track),
        album_id=album.id,
        track_id=track.id,
    )
    db.commit()
    db.refresh(track)
    return build_track_read(track, admin, album, db=db)


@router.post("/tracks/{track_id}/restore", response_model=TrackRead)
def admin_restore_track(
    track_id: int,
    payload: AdminReasonPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_OPERATOR)),
) -> TrackRead:
    from app.notifications import notify

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found.")
    if track.archived_at is None:
        raise HTTPException(status_code=409, detail="Track is not archived.")

    before = _track_snapshot(track)
    track.archived_at = None
    log_track_event(db, track, admin, "track_restored")
    notify(
        db,
        [track.submitter_id],
        "track_restored",
        "Track restored",
        f"{track.title} has been restored by an administrator.",
        related_track_id=track.id,
        background_tasks=background_tasks,
        album_id=track.album_id,
        webhook_context={"actor_id": admin.id, "actor_name": admin.display_name},
    )
    record_admin_audit(
        db,
        actor=admin,
        action="track_restored",
        entity_type="track",
        entity_id=track.id,
        summary=f"Restored track {track.title}",
        reason=payload.reason,
        before=before,
        after=_track_snapshot(track),
        album_id=album.id,
        track_id=track.id,
    )
    db.commit()
    db.refresh(track)
    return build_track_read(track, admin, album, db=db)


@router.delete("/tracks/{track_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_track(
    track_id: int,
    reason: str = Query(default="Deleted by admin", min_length=1, max_length=500),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin(ADMIN_ROLE_OPERATOR)),
) -> None:
    track = db.get(
        Track,
        track_id,
        options=[
            selectinload(Track.issues),
            selectinload(Track.source_versions),
            selectinload(Track.master_deliveries),
            selectinload(Track.discussions),
        ],
    )
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found.")

    before = _track_snapshot(track)
    local_paths, r2_keys = collect_track_files(track)
    record_admin_audit(
        db,
        actor=admin,
        action="track_deleted",
        entity_type="track",
        entity_id=track.id,
        summary=f"Deleted track {track.title}",
        reason=reason,
        before=before,
        after=None,
        album_id=album.id,
        track_id=track.id,
    )
    db.delete(track)
    db.commit()
    cleanup_files(local_paths, r2_keys)
