import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.models.album import Album
from app.models.discussion import TrackDiscussion, TrackDiscussionImage
from app.models.edit_history import EditHistory
from app.models.track import Track
from app.models.user import User
from app.notifications import notify
from app.schemas.schemas import DiscussionImageRead, DiscussionRead, DiscussionUpdate, EditHistoryRead, UserRead
from app.security import get_current_user
from app.services.upload import stream_upload
from app.workflow import ensure_track_visibility

router = APIRouter(tags=["discussions"])


def _build_discussion_read(discussion: TrackDiscussion) -> DiscussionRead:
    author = discussion.author
    images = [
        DiscussionImageRead(
            id=img.id,
            discussion_id=img.discussion_id,
            image_url=f"/uploads/{img.file_path}",
            created_at=img.created_at,
        )
        for img in discussion.images
    ]
    return DiscussionRead(
        id=discussion.id,
        track_id=discussion.track_id,
        author_id=discussion.author_id,
        content=discussion.content,
        created_at=discussion.created_at,
        edited_at=discussion.edited_at,
        author=UserRead.model_validate(author) if author else None,
        images=images,
    )


@router.get(
    "/api/tracks/{track_id}/discussions",
    response_model=list[DiscussionRead],
)
def list_discussions(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DiscussionRead]:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    ensure_track_visibility(track, current_user, db)
    discussions = list(
        db.scalars(
            select(TrackDiscussion)
            .where(TrackDiscussion.track_id == track_id)
            .order_by(TrackDiscussion.created_at.asc())
            .options(selectinload(TrackDiscussion.images), selectinload(TrackDiscussion.author))
        ).all()
    )
    return [_build_discussion_read(d) for d in discussions]


@router.post(
    "/api/tracks/{track_id}/discussions",
    response_model=DiscussionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_discussion(
    track_id: int,
    background_tasks: BackgroundTasks,
    content: str = Form(...),
    images: Optional[list[UploadFile]] = File(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DiscussionRead:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")
    album = ensure_track_visibility(track, current_user, db)

    discussion = TrackDiscussion(
        track_id=track_id,
        author_id=current_user.id,
        content=content,
    )
    db.add(discussion)
    db.flush()

    if images:
        from app.config import MAX_IMAGE_UPLOAD_SIZE

        allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        upload_dir = settings.get_upload_path() / "discussion_images"
        upload_dir.mkdir(parents=True, exist_ok=True)
        for img_file in images:
            if not img_file.content_type or not img_file.content_type.startswith("image/"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"File must be an image, got: {img_file.content_type}",
                )
            ext = (Path(img_file.filename or "image.png").suffix or ".png").lower()
            if ext not in allowed_extensions:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unsupported image extension: {ext}",
                )
            filename = f"{uuid.uuid4().hex}{ext}"
            file_path = upload_dir / filename
            await stream_upload(img_file, file_path, MAX_IMAGE_UPLOAD_SIZE)
            db.add(
                TrackDiscussionImage(
                    discussion_id=discussion.id,
                    file_path=f"discussion_images/{filename}",
                )
            )

    # Notify track participants
    participant_ids = {track.submitter_id, track.peer_reviewer_id, album.producer_id}
    if album.mastering_engineer_id:
        participant_ids.add(album.mastering_engineer_id)
    participant_ids.discard(current_user.id)
    participant_ids.discard(None)
    notify(
        db,
        list(participant_ids),
        "new_discussion",
        "新讨论",
        f"「{track.title}」有新的讨论",
        related_track_id=track.id,
        background_tasks=background_tasks,
        album_id=track.album_id,
    )

    db.commit()
    db.refresh(discussion)
    return _build_discussion_read(discussion)


@router.patch(
    "/api/discussions/{discussion_id}",
    response_model=DiscussionRead,
)
def update_discussion(
    discussion_id: int,
    payload: DiscussionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DiscussionRead:
    discussion = db.get(TrackDiscussion, discussion_id)
    if discussion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Discussion not found.")
    if discussion.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the author can edit this discussion.")

    if discussion.content != payload.content:
        db.add(EditHistory(
            entity_type="discussion",
            entity_id=discussion.id,
            old_content=discussion.content,
            edited_by_id=current_user.id,
        ))
        discussion.content = payload.content
        discussion.edited_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(discussion)
    return _build_discussion_read(discussion)


@router.delete(
    "/api/discussions/{discussion_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_discussion(
    discussion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    discussion = db.get(TrackDiscussion, discussion_id)
    if discussion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Discussion not found.")
    if discussion.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the author can delete this discussion.")

    db.delete(discussion)
    db.commit()


@router.get("/api/discussions/{discussion_id}/history", response_model=list[EditHistoryRead])
def get_discussion_history(
    discussion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[EditHistoryRead]:
    discussion = db.get(TrackDiscussion, discussion_id)
    if discussion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Discussion not found.")
    track = db.get(Track, discussion.track_id)
    if track:
        ensure_track_visibility(track, current_user, db)

    histories = list(db.scalars(
        select(EditHistory)
        .where(EditHistory.entity_type == "discussion", EditHistory.entity_id == discussion_id)
        .order_by(EditHistory.created_at.desc())
    ).all())
    return [EditHistoryRead.model_validate(h) for h in histories]
