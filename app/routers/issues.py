import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.comment import Comment
from app.models.comment_image import CommentImage
from app.models.issue import Issue
from app.models.track import Track
from app.models.user import User
from app.schemas.schemas import (
    CommentImageRead,
    CommentRead,
    CommentWithAuthor,
    IssueCreate,
    IssueDetail,
    IssueRead,
    IssueUpdate,
    UserRead,
)

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
IMAGE_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

router = APIRouter(tags=["issues"])


def _validate_issue_timing(issue_type: str, time_start: float, time_end: float | None) -> float | None:
    if issue_type == "range":
        if time_end is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="time_end is required for RANGE issues.",
            )
        if time_end <= time_start:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="time_end must be greater than time_start for RANGE issues.",
            )
        return time_end

    return None


def _issue_to_read(issue: Issue) -> IssueRead:
    return IssueRead(
        id=issue.id,
        track_id=issue.track_id,
        author_id=issue.author_id,
        title=issue.title,
        description=issue.description,
        issue_type=issue.issue_type,
        severity=issue.severity,
        status=issue.status,
        time_start=issue.time_start,
        time_end=issue.time_end,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        comment_count=len(issue.comments),
    )


@router.post(
    "/api/tracks/{track_id}/issues",
    response_model=IssueRead,
    status_code=status.HTTP_201_CREATED,
)
def create_issue(
    track_id: int,
    payload: IssueCreate,
    db: Session = Depends(get_db),
) -> IssueRead:
    # Validate track exists
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")

    # Validate author exists
    author = db.get(User, payload.author_id)
    if author is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Author not found.")

    time_end = _validate_issue_timing(payload.issue_type.value, payload.time_start, payload.time_end)

    issue = Issue(
        track_id=track_id,
        author_id=payload.author_id,
        title=payload.title,
        description=payload.description,
        issue_type=payload.issue_type,
        severity=payload.severity,
        time_start=payload.time_start,
        time_end=time_end,
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return _issue_to_read(issue)


@router.get("/api/tracks/{track_id}/issues", response_model=list[IssueRead])
def list_issues(track_id: int, db: Session = Depends(get_db)) -> list[IssueRead]:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found.")

    stmt = select(Issue).where(Issue.track_id == track_id).order_by(Issue.time_start)
    issues = list(db.scalars(stmt).all())
    return [_issue_to_read(i) for i in issues]


@router.get("/api/issues/{issue_id}", response_model=IssueDetail)
def get_issue(issue_id: int, db: Session = Depends(get_db)) -> IssueDetail:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found.")

    # Load author
    author = db.get(User, issue.author_id)
    author_read = UserRead.model_validate(author) if author else None

    # Load comments with authors
    comments_with_authors: list[CommentWithAuthor] = []
    for c in issue.comments:
        c_author = db.get(User, c.author_id)
        c_author_read = UserRead.model_validate(c_author) if c_author else None
        images = [
            CommentImageRead(
                id=img.id,
                comment_id=img.comment_id,
                image_url=f"/uploads/{img.file_path}",
                created_at=img.created_at,
            )
            for img in c.images
        ]
        comments_with_authors.append(
            CommentWithAuthor(
                id=c.id,
                issue_id=c.issue_id,
                author_id=c.author_id,
                content=c.content,
                created_at=c.created_at,
                author=c_author_read,
                images=images,
            )
        )

    return IssueDetail(
        id=issue.id,
        track_id=issue.track_id,
        author_id=issue.author_id,
        title=issue.title,
        description=issue.description,
        issue_type=issue.issue_type,
        severity=issue.severity,
        status=issue.status,
        time_start=issue.time_start,
        time_end=issue.time_end,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        comment_count=len(issue.comments),
        author=author_read,
        comments=comments_with_authors,
    )


@router.patch("/api/issues/{issue_id}", response_model=IssueRead)
def update_issue(
    issue_id: int,
    payload: IssueUpdate,
    db: Session = Depends(get_db),
) -> IssueRead:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found.")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(issue, field, value)

    db.commit()
    db.refresh(issue)
    return _issue_to_read(issue)


@router.post(
    "/api/issues/{issue_id}/comments",
    response_model=CommentRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_comment(
    issue_id: int,
    author_id: int = Form(...),
    content: str = Form(..., min_length=1),
    images: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
) -> CommentRead:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found.")

    author = db.get(User, author_id)
    if author is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Author not found.")

    # Validate image MIME types
    for img_file in images:
        if img_file.content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported file type: {img_file.content_type}. Allowed: jpeg, png, gif, webp.",
            )

    comment = Comment(
        issue_id=issue_id,
        author_id=author_id,
        content=content,
    )
    db.add(comment)
    db.flush()

    # Save uploaded images
    image_reads: list[CommentImageRead] = []
    if images:
        comment_images_dir = settings.get_upload_path() / "comment_images"
        comment_images_dir.mkdir(parents=True, exist_ok=True)
        for img_file in images:
            ext = IMAGE_EXT_MAP.get(img_file.content_type or "", ".jpg")
            filename = f"{uuid.uuid4()}{ext}"
            file_path = f"comment_images/{filename}"
            dest = comment_images_dir / filename
            data = await img_file.read()
            dest.write_bytes(data)
            ci = CommentImage(comment_id=comment.id, file_path=file_path)
            db.add(ci)
            db.flush()
            image_reads.append(
                CommentImageRead(
                    id=ci.id,
                    comment_id=ci.comment_id,
                    image_url=f"/uploads/{file_path}",
                    created_at=ci.created_at,
                )
            )

    db.commit()
    db.refresh(comment)
    return CommentRead(
        id=comment.id,
        issue_id=comment.issue_id,
        author_id=comment.author_id,
        content=comment.content,
        created_at=comment.created_at,
        images=image_reads,
    )
