import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin_permissions import has_admin_role
from app.config import settings
from app.database import get_db
from app.models.album import Album
from app.models.circle import Circle, CircleInviteCode, CircleMember
from app.models.user import User
from app.models.workflow_template import WorkflowTemplate
from app.schemas.schemas import (
    CircleCreate,
    CircleMemberRead,
    CircleRead,
    CircleSummary,
    CircleUpdate,
    InviteCodeCreate,
    InviteCodeRead,
    JoinCircleRequest,
    UserRead,
)
from app.security import get_current_user, require_producer
from app.services.upload import stream_upload

router = APIRouter(prefix="/api/circles", tags=["circles"])


def _ensure_circle_producer(circle: Circle, current_user: User) -> None:
    if circle.created_by != current_user.id and not has_admin_role(current_user, "operator"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the circle creator can do this")


def _circle_to_read(circle: Circle) -> CircleRead:
    members = [
        CircleMemberRead(
            id=m.id,
            circle_id=m.circle_id,
            user_id=m.user_id,
            role=m.role,
            joined_at=m.joined_at,
            user=UserRead.model_validate(m.user),
        )
        for m in circle.members
    ]
    return CircleRead(
        id=circle.id,
        name=circle.name,
        description=circle.description,
        website=circle.website,
        logo_url=circle.logo_url,
        created_by=circle.created_by,
        created_at=circle.created_at,
        members=members,
    )


def _circle_to_summary(circle: Circle) -> CircleSummary:
    return CircleSummary(
        id=circle.id,
        name=circle.name,
        description=circle.description,
        logo_url=circle.logo_url,
        created_by=circle.created_by,
        member_count=len(circle.members),
    )


# ── list circles the current user belongs to ─────────────────────────────────
@router.get("", response_model=list[CircleSummary])
def list_circles(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.is_admin:
        all_circles = list(db.scalars(select(Circle)).all())
        return [_circle_to_summary(c) for c in all_circles]

    memberships = db.execute(
        select(CircleMember).where(CircleMember.user_id == current_user.id)
    ).scalars().all()
    created = db.execute(
        select(Circle).where(Circle.created_by == current_user.id)
    ).scalars().all()

    seen_ids: set[int] = set()
    circles: list[Circle] = []
    for c in created:
        if c.id not in seen_ids:
            seen_ids.add(c.id)
            circles.append(c)
    for m in memberships:
        if m.circle_id not in seen_ids:
            seen_ids.add(m.circle_id)
            if m.circle:
                circles.append(m.circle)
    return [_circle_to_summary(c) for c in circles]


# ── create circle (producer only) ────────────────────────────────────────────
@router.post("", response_model=CircleRead, status_code=status.HTTP_201_CREATED)
def create_circle(
    data: CircleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_producer),
):
    circle = Circle(
        name=data.name,
        description=data.description,
        website=data.website,
        created_by=current_user.id,
    )
    db.add(circle)
    db.flush()

    # creator is auto-added as owner
    owner_member = CircleMember(
        circle_id=circle.id,
        user_id=current_user.id,
        role="owner",
    )
    db.add(owner_member)
    db.commit()
    db.refresh(circle)
    return _circle_to_read(circle)


# ── get circle detail ─────────────────────────────────────────────────────────
@router.get("/{circle_id}", response_model=CircleRead)
def get_circle(
    circle_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    circle = db.get(Circle, circle_id)
    if not circle:
        raise HTTPException(status_code=404, detail="Circle not found")

    is_member = any(m.user_id == current_user.id for m in circle.members)
    is_creator = circle.created_by == current_user.id
    if not current_user.is_admin and not is_member and not is_creator:
        raise HTTPException(status_code=403, detail="Not a member of this circle")

    return _circle_to_read(circle)


# ── update circle info ────────────────────────────────────────────────────────
@router.patch("/{circle_id}", response_model=CircleRead)
def update_circle(
    circle_id: int,
    data: CircleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    circle = db.get(Circle, circle_id)
    if not circle:
        raise HTTPException(status_code=404, detail="Circle not found")
    _ensure_circle_producer(circle, current_user)

    if data.name is not None:
        circle.name = data.name
    if data.description is not None:
        circle.description = data.description
    if data.website is not None:
        circle.website = data.website

    db.commit()
    db.refresh(circle)
    return _circle_to_read(circle)


# ── upload circle logo ────────────────────────────────────────────────────────
@router.post("/{circle_id}/logo", response_model=CircleRead)
async def upload_logo(
    circle_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    circle = db.get(Circle, circle_id)
    if not circle:
        raise HTTPException(status_code=404, detail="Circle not found")
    _ensure_circle_producer(circle, current_user)

    from app.config import MAX_IMAGE_UPLOAD_SIZE

    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    ext = (Path(file.filename or "logo.jpg").suffix or ".jpg").lower()
    if ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Unsupported image extension: {ext}")
    filename = f"{uuid.uuid4()}{ext}"
    logo_dir = settings.get_upload_path() / "logos"
    logo_dir.mkdir(parents=True, exist_ok=True)
    dest = logo_dir / filename
    await stream_upload(file, dest, MAX_IMAGE_UPLOAD_SIZE)

    # Remove old logo file
    if circle.logo_url:
        old_rel = circle.logo_url.lstrip("/")
        # Handle both legacy "/uploads/x" and new "logos/x" formats
        if old_rel.startswith("uploads/"):
            old_rel = old_rel[len("uploads/"):]
        old_path = settings.get_upload_path() / old_rel
        if old_path.exists():
            old_path.unlink(missing_ok=True)

    circle.logo_url = f"logos/{filename}"
    db.commit()
    db.refresh(circle)
    return _circle_to_read(circle)


# ── create invite code ────────────────────────────────────────────────────────
@router.post("/{circle_id}/invite-codes", response_model=InviteCodeRead, status_code=status.HTTP_201_CREATED)
def create_invite_code(
    circle_id: int,
    data: InviteCodeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    circle = db.get(Circle, circle_id)
    if not circle:
        raise HTTPException(status_code=404, detail="Circle not found")
    _ensure_circle_producer(circle, current_user)

    code = secrets.token_urlsafe(10)[:12]
    expires_at = datetime.now(timezone.utc) + timedelta(days=data.expires_in_days)
    invite = CircleInviteCode(
        circle_id=circle_id,
        code=code,
        role=data.role,
        created_by=current_user.id,
        expires_at=expires_at,
        is_active=True,
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return invite


# ── list invite codes ─────────────────────────────────────────────────────────
@router.get("/{circle_id}/invite-codes", response_model=list[InviteCodeRead])
def list_invite_codes(
    circle_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    circle = db.get(Circle, circle_id)
    if not circle:
        raise HTTPException(status_code=404, detail="Circle not found")
    _ensure_circle_producer(circle, current_user)

    codes = db.execute(
        select(CircleInviteCode).where(CircleInviteCode.circle_id == circle_id)
    ).scalars().all()
    return list(codes)


# ── revoke invite code ────────────────────────────────────────────────────────
@router.delete("/{circle_id}/invite-codes/{code_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invite_code(
    circle_id: int,
    code_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    circle = db.get(Circle, circle_id)
    if not circle:
        raise HTTPException(status_code=404, detail="Circle not found")
    _ensure_circle_producer(circle, current_user)

    invite = db.get(CircleInviteCode, code_id)
    if not invite or invite.circle_id != circle_id:
        raise HTTPException(status_code=404, detail="Invite code not found")

    invite.is_active = False
    db.commit()


# ── join circle via invite code ───────────────────────────────────────────────
@router.post("/join", response_model=CircleSummary, status_code=status.HTTP_200_OK)
def join_circle(
    data: JoinCircleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    invite = db.execute(
        select(CircleInviteCode).where(CircleInviteCode.code == data.code)
    ).scalar_one_or_none()

    if not invite or not invite.is_active:
        raise HTTPException(status_code=400, detail="Invalid or expired invite code")

    now = datetime.now(timezone.utc)
    expires = invite.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now > expires:
        invite.is_active = False
        db.commit()
        raise HTTPException(status_code=400, detail="Invite code has expired")

    already = db.execute(
        select(CircleMember).where(
            CircleMember.circle_id == invite.circle_id,
            CircleMember.user_id == current_user.id,
        )
    ).scalar_one_or_none()
    if already:
        raise HTTPException(status_code=400, detail="Already a member of this circle")

    member = CircleMember(
        circle_id=invite.circle_id,
        user_id=current_user.id,
        role=invite.role,
    )
    db.add(member)
    db.commit()

    circle = db.get(Circle, invite.circle_id)
    db.refresh(circle)
    return _circle_to_summary(circle)


# ── remove member from circle ─────────────────────────────────────────────────
@router.delete("/{circle_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    circle_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    circle = db.get(Circle, circle_id)
    if not circle:
        raise HTTPException(status_code=404, detail="Circle not found")
    _ensure_circle_producer(circle, current_user)

    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot remove yourself")

    member = db.execute(
        select(CircleMember).where(
            CircleMember.circle_id == circle_id,
            CircleMember.user_id == user_id,
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    db.delete(member)
    db.commit()


# ── leave circle (self-service, non-owner) ───────────────────────────────────
@router.post("/{circle_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave_circle(
    circle_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    circle = db.get(Circle, circle_id)
    if not circle:
        raise HTTPException(status_code=404, detail="Circle not found")
    if circle.created_by == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The circle owner cannot leave. Delete the circle instead.",
        )
    member = db.execute(
        select(CircleMember).where(
            CircleMember.circle_id == circle_id,
            CircleMember.user_id == current_user.id,
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="You are not a member of this circle")

    db.delete(member)
    db.commit()


# ── delete circle (owner only) ───────────────────────────────────────────────
@router.delete("/{circle_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_circle(
    circle_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    circle = db.get(Circle, circle_id)
    if not circle:
        raise HTTPException(status_code=404, detail="Circle not found")
    _ensure_circle_producer(circle, current_user)

    # Refuse if any non-archived album is still linked to this circle.
    active_album = db.execute(
        select(Album.id).where(
            Album.circle_id == circle_id,
            Album.archived_at.is_(None),
        ).limit(1)
    ).scalar_one_or_none()
    if active_album is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete circle: there are active albums linked to it. Archive or unlink them first.",
        )

    # Detach archived albums from the circle (set circle_id and workflow_template_id = NULL)
    # so history is preserved and templates being deleted don't break FK references.
    template_ids = list(db.scalars(
        select(WorkflowTemplate.id).where(WorkflowTemplate.circle_id == circle_id)
    ).all())
    if template_ids:
        db.execute(
            Album.__table__.update()
            .where(Album.workflow_template_id.in_(template_ids))
            .values(workflow_template_id=None)
        )
    db.execute(
        Album.__table__.update()
        .where(Album.circle_id == circle_id)
        .values(circle_id=None)
    )

    # Delete workflow templates tied to this circle (no cascade relationship exists)
    if template_ids:
        db.execute(
            WorkflowTemplate.__table__.delete().where(WorkflowTemplate.circle_id == circle_id)
        )

    # Delete related invite codes and members via ORM cascade
    db.delete(circle)
    db.commit()
