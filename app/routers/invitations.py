from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.invitation import Invitation
from app.models.user import User
from app.schemas.schemas import (
    AlbumSummary,
    InvitationCreate,
    InvitationRead,
    UserRead,
)
from app.security import get_current_user
from app.workflow import ensure_album_producer, ensure_album_visibility, get_album_member_ids

router = APIRouter(tags=["invitations"])


def _invitation_to_read(invitation: Invitation, db: Session, include_album: bool = False) -> InvitationRead:
    data = {
        "id": invitation.id,
        "album_id": invitation.album_id,
        "user_id": invitation.user_id,
        "invited_by_user_id": invitation.invited_by_user_id,
        "status": invitation.status,
        "created_at": invitation.created_at,
        "user": UserRead.model_validate(invitation.user) if invitation.user else None,
        "invited_by_user": UserRead.model_validate(invitation.invited_by_user) if invitation.invited_by_user else None,
    }
    if include_album and invitation.album:
        data["album"] = AlbumSummary.model_validate(invitation.album)
    return InvitationRead(**data)


@router.post("/api/albums/{album_id}/invitations", response_model=InvitationRead, status_code=status.HTTP_201_CREATED)
def create_invitation(
    album_id: int,
    payload: InvitationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> InvitationRead:
    album = ensure_album_producer(album_id, current_user, db)

    invited_user = db.get(User, payload.user_id)
    if invited_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    member_ids = get_album_member_ids(db, album_id)
    if payload.user_id in member_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already a member of this album.",
        )

    existing = db.scalars(
        select(Invitation).where(
            Invitation.album_id == album_id,
            Invitation.user_id == payload.user_id,
            Invitation.status == "pending",
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="There is already a pending invitation for this user.",
        )

    invitation = Invitation(
        album_id=album_id,
        user_id=payload.user_id,
        invited_by_user_id=current_user.id,
        status="pending",
    )
    db.add(invitation)
    db.commit()
    db.refresh(invitation)
    return _invitation_to_read(invitation, db, include_album=True)


@router.get("/api/albums/{album_id}/invitations", response_model=list[InvitationRead])
def list_album_invitations(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[InvitationRead]:
    ensure_album_producer(album_id, current_user, db)

    invitations = list(
        db.scalars(
            select(Invitation)
            .where(Invitation.album_id == album_id, Invitation.status == "pending")
            .order_by(Invitation.created_at.desc())
        ).all()
    )
    return [_invitation_to_read(inv, db, include_album=True) for inv in invitations]


@router.get("/api/invitations", response_model=list[InvitationRead])
def list_my_invitations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[InvitationRead]:
    invitations = list(
        db.scalars(
            select(Invitation)
            .where(Invitation.user_id == current_user.id, Invitation.status == "pending")
            .order_by(Invitation.created_at.desc())
        ).all()
    )
    return [_invitation_to_read(inv, db, include_album=True) for inv in invitations]


@router.post("/api/invitations/{invitation_id}/accept", response_model=InvitationRead)
def accept_invitation(
    invitation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> InvitationRead:
    invitation = db.get(Invitation, invitation_id)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found.")
    if invitation.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not the invited user.",
        )
    if invitation.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This invitation is no longer pending.",
        )

    db.add(AlbumMember(album_id=invitation.album_id, user_id=current_user.id))
    invitation.status = "accepted"
    db.commit()
    db.refresh(invitation)
    return _invitation_to_read(invitation, db, include_album=True)


@router.post("/api/invitations/{invitation_id}/decline", response_model=InvitationRead)
def decline_invitation(
    invitation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> InvitationRead:
    invitation = db.get(Invitation, invitation_id)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found.")
    if invitation.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not the invited user.",
        )
    if invitation.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This invitation is no longer pending.",
        )

    invitation.status = "declined"
    db.commit()
    db.refresh(invitation)
    return _invitation_to_read(invitation, db, include_album=True)


@router.delete("/api/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_invitation(
    invitation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    invitation = db.get(Invitation, invitation_id)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found.")
    ensure_album_producer(invitation.album_id, current_user, db)

    db.delete(invitation)
    db.commit()
