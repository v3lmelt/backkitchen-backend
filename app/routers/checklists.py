from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.checklist import ChecklistItem
from app.models.track import Track
from app.models.user import User
from app.schemas.schemas import ChecklistItemRead, ChecklistSubmit

router = APIRouter(tags=["checklists"])


@router.post(
    "/api/tracks/{track_id}/checklist",
    response_model=list[ChecklistItemRead],
    status_code=status.HTTP_201_CREATED,
)
def submit_checklist(
    track_id: int,
    payload: ChecklistSubmit,
    db: Session = Depends(get_db),
) -> list[ChecklistItemRead]:
    # Validate track
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")

    # Validate reviewer
    reviewer = db.get(User, payload.reviewer_id)
    if reviewer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reviewer not found.")

    # Delete existing checklist items for this track + reviewer, then re-create
    existing = db.scalars(
        select(ChecklistItem).where(
            ChecklistItem.track_id == track_id,
            ChecklistItem.reviewer_id == payload.reviewer_id,
        )
    ).all()
    for item in existing:
        db.delete(item)
    db.flush()

    created: list[ChecklistItem] = []
    for item_data in payload.items:
        ci = ChecklistItem(
            track_id=track_id,
            reviewer_id=payload.reviewer_id,
            label=item_data.label,
            passed=item_data.passed,
            note=item_data.note,
        )
        db.add(ci)
        created.append(ci)

    db.commit()
    for ci in created:
        db.refresh(ci)

    return [
        ChecklistItemRead(
            id=ci.id,
            track_id=ci.track_id,
            reviewer_id=ci.reviewer_id,
            label=ci.label,
            passed=ci.passed,
            note=ci.note,
            created_at=ci.created_at,
        )
        for ci in created
    ]


@router.get(
    "/api/tracks/{track_id}/checklist",
    response_model=list[ChecklistItemRead],
)
def get_checklist(track_id: int, db: Session = Depends(get_db)) -> list[ChecklistItemRead]:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")

    stmt = (
        select(ChecklistItem)
        .where(ChecklistItem.track_id == track_id)
        .order_by(ChecklistItem.id)
    )
    items = list(db.scalars(stmt).all())

    return [
        ChecklistItemRead(
            id=ci.id,
            track_id=ci.track_id,
            reviewer_id=ci.reviewer_id,
            label=ci.label,
            passed=ci.passed,
            note=ci.note,
            created_at=ci.created_at,
        )
        for ci in items
    ]
