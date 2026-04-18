import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.album import Album
from app.models.checklist import ChecklistItem
from app.models.track import Track
from app.models.track_source_version import TrackSourceVersion
from app.models.user import User
from app.schemas.schemas import (
    ChecklistDraftRead,
    ChecklistItemRead,
    ChecklistSubmit,
    ChecklistTemplateItem,
    ChecklistTemplateRead,
    ChecklistTemplateUpdate,
)
from app.security import get_current_user
from app.workflow import build_checklist_read, current_source_version, ensure_album_producer, ensure_album_visibility, ensure_track_visibility
from app.workflow_engine import get_current_step, parse_workflow_config, user_matches_role_or_assignment

router = APIRouter(tags=["checklists"])

DEFAULT_CHECKLIST_LABELS = ["Arrangement", "Balance", "Low-End", "Stereo Image", "Technical Cleanliness"]


def _resolve_source_version(
    track: Track,
    db: Session,
    *,
    source_version_id: int | None = None,
) -> TrackSourceVersion | None:
    if source_version_id is None:
        return current_source_version(track)
    source_version = db.get(TrackSourceVersion, source_version_id)
    if source_version is None or source_version.track_id != track.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source version not found.")
    return source_version


def _get_checklist_items(
    *,
    db: Session,
    track_id: int,
    source_version_id: int,
    reviewer_id: int | None = None,
) -> list[ChecklistItem]:
    stmt = (
        select(ChecklistItem)
        .where(
            ChecklistItem.track_id == track_id,
            ChecklistItem.source_version_id == source_version_id,
        )
        .order_by(ChecklistItem.id)
    )
    if reviewer_id is not None:
        stmt = stmt.where(ChecklistItem.reviewer_id == reviewer_id)
    return list(db.scalars(stmt).all())


@router.post(
    "/api/tracks/{track_id}/checklist",
    response_model=list[ChecklistItemRead],
    status_code=status.HTTP_201_CREATED,
)
def submit_checklist(
    track_id: int,
    payload: ChecklistSubmit,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ChecklistItemRead]:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if not album.checklist_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Checklist is disabled for this album.",
        )

    config = parse_workflow_config(album)
    step = get_current_step(config, track)
    if step is None or step.type != "review":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Checklist can only be submitted in a review step.",
        )
    if not user_matches_role_or_assignment(current_user, album, track, step, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assigned reviewer can submit the checklist.",
        )

    source_version = current_source_version(track)
    if source_version is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No source version is available for this track.",
        )

    # Validate checklist submission against album template
    album = db.get(Album, track.album_id)
    if album and album.checklist_template:
        template_items = json.loads(album.checklist_template)
        submitted_labels = {item.label for item in payload.items}
        required_labels = {item["label"] for item in template_items if item.get("required", True)}
        missing = required_labels - submitted_labels
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Missing required checklist items: {', '.join(missing)}",
            )

    existing = db.scalars(
        select(ChecklistItem).where(
            ChecklistItem.track_id == track_id,
            ChecklistItem.reviewer_id == current_user.id,
            ChecklistItem.source_version_id == source_version.id,
        )
    ).all()
    for item in existing:
        db.delete(item)
    db.flush()

    created: list[ChecklistItem] = []
    for item_data in payload.items:
        item = ChecklistItem(
            track_id=track_id,
            reviewer_id=current_user.id,
            source_version_id=source_version.id,
            workflow_cycle=track.workflow_cycle,
            label=item_data.label,
            passed=item_data.passed,
            note=item_data.note,
        )
        db.add(item)
        created.append(item)

    db.commit()
    for item in created:
        db.refresh(item)
    return [build_checklist_read(item) for item in created]


@router.get(
    "/api/tracks/{track_id}/checklist",
    response_model=list[ChecklistItemRead],
)
def get_checklist(
    track_id: int,
    source_version_id: int | None = Query(default=None, ge=1),
    reviewer_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ChecklistItemRead]:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    source_version = _resolve_source_version(track, db, source_version_id=source_version_id)
    if source_version is None:
        return []

    items = _get_checklist_items(
        db=db,
        track_id=track_id,
        source_version_id=source_version.id,
        reviewer_id=reviewer_id,
    )
    return [build_checklist_read(item) for item in items]


@router.get(
    "/api/tracks/{track_id}/checklist/draft",
    response_model=ChecklistDraftRead,
)
def get_checklist_draft(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChecklistDraftRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)
    if not album.checklist_enabled:
        return ChecklistDraftRead()

    current_version = current_source_version(track)
    if current_version is None:
        return ChecklistDraftRead()

    current_items = _get_checklist_items(
        db=db,
        track_id=track.id,
        source_version_id=current_version.id,
        reviewer_id=current_user.id,
    )
    if current_items:
        return ChecklistDraftRead(
            items=[build_checklist_read(item) for item in current_items],
            current_source_version_id=current_version.id,
            current_source_version_number=current_version.version_number,
            prefilled_from_source_version_id=current_version.id,
            prefilled_from_source_version_number=current_version.version_number,
            prefilled_from_current_version=True,
        )

    prior_versions = sorted(
        [
            version
            for version in track.source_versions
            if version.workflow_cycle == track.workflow_cycle
            and version.version_number < current_version.version_number
        ],
        key=lambda version: version.version_number,
        reverse=True,
    )
    for version in prior_versions:
        prior_items = _get_checklist_items(
            db=db,
            track_id=track.id,
            source_version_id=version.id,
            reviewer_id=current_user.id,
        )
        if prior_items:
            return ChecklistDraftRead(
                items=[build_checklist_read(item) for item in prior_items],
                current_source_version_id=current_version.id,
                current_source_version_number=current_version.version_number,
                prefilled_from_source_version_id=version.id,
                prefilled_from_source_version_number=version.version_number,
                prefilled_from_current_version=False,
            )

    return ChecklistDraftRead(
        current_source_version_id=current_version.id,
        current_source_version_number=current_version.version_number,
    )


@router.get("/api/albums/{album_id}/checklist-template", response_model=ChecklistTemplateRead)
def get_checklist_template(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChecklistTemplateRead:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    ensure_album_visibility(album, current_user, db)
    if album.checklist_template:
        items = json.loads(album.checklist_template)
        return ChecklistTemplateRead(
            items=[ChecklistTemplateItem(**i) for i in items],
            is_default=False,
        )
    return ChecklistTemplateRead(
        items=[
            ChecklistTemplateItem(label=label, sort_order=i)
            for i, label in enumerate(DEFAULT_CHECKLIST_LABELS)
        ],
        is_default=True,
    )


@router.put("/api/albums/{album_id}/checklist-template", response_model=ChecklistTemplateRead)
def update_checklist_template(
    album_id: int,
    payload: ChecklistTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChecklistTemplateRead:
    album = ensure_album_producer(album_id, current_user, db)
    if not payload.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Template must have at least one item.",
        )
    album.checklist_template = json.dumps([item.model_dump() for item in payload.items])
    db.commit()
    db.refresh(album)
    return ChecklistTemplateRead(items=payload.items, is_default=False)


@router.delete("/api/albums/{album_id}/checklist-template", status_code=status.HTTP_204_NO_CONTENT)
def reset_checklist_template(
    album_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    album = ensure_album_producer(album_id, current_user, db)
    album.checklist_template = None
    db.commit()
