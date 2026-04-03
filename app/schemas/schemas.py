from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.issue import IssueSeverity, IssueStatus, IssueType
from app.models.track import TrackStatus


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------
class UserBase(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    display_name: str = Field(..., min_length=1, max_length=100)
    role: str = Field(default="producer", pattern=r"^(producer|author|reviewer)$")
    avatar_color: str = Field(default="#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")


class UserCreate(UserBase):
    pass


class UserRead(UserBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Album
# ---------------------------------------------------------------------------
class AlbumBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    cover_color: str = Field(default="#8b5cf6", pattern=r"^#[0-9a-fA-F]{6}$")


class AlbumCreate(AlbumBase):
    pass


class AlbumRead(AlbumBase):
    id: int
    created_at: datetime
    updated_at: datetime
    track_count: int = 0

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Track
# ---------------------------------------------------------------------------
class TrackBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    artist: str = Field(..., min_length=1, max_length=100)
    album_id: int
    bpm: int | None = None


class TrackCreate(TrackBase):
    pass


class TrackRead(TrackBase):
    id: int
    file_path: str | None = None
    duration: float | None = None
    status: TrackStatus
    version: int
    created_at: datetime
    updated_at: datetime
    issue_count: int = 0
    open_issue_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class TrackStatusUpdate(BaseModel):
    status: TrackStatus


class TrackListItem(BaseModel):
    id: int
    title: str
    artist: str
    album_id: int
    album_title: str = ""
    file_path: str | None = None
    duration: float | None = None
    bpm: int | None = None
    status: TrackStatus
    version: int
    created_at: datetime
    updated_at: datetime
    issue_count: int = 0
    open_issue_count: int = 0

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------
class IssueBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    issue_type: IssueType = IssueType.POINT
    severity: IssueSeverity = IssueSeverity.MAJOR
    time_start: float = Field(..., ge=0)
    time_end: float | None = Field(default=None, ge=0)


class IssueCreate(IssueBase):
    author_id: int


class IssueUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: IssueStatus | None = None
    severity: IssueSeverity | None = None


class IssueRead(IssueBase):
    id: int
    track_id: int
    author_id: int
    status: IssueStatus
    created_at: datetime
    updated_at: datetime
    comment_count: int = 0

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Comment
# ---------------------------------------------------------------------------
class CommentImageRead(BaseModel):
    id: int
    comment_id: int
    image_url: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CommentBase(BaseModel):
    content: str = Field(..., min_length=1)


class CommentCreate(CommentBase):
    author_id: int


class CommentRead(CommentBase):
    id: int
    issue_id: int
    author_id: int
    created_at: datetime
    images: list[CommentImageRead] = []

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------
class ChecklistItemBase(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    passed: bool = False
    note: str | None = None


class ChecklistItemCreate(ChecklistItemBase):
    reviewer_id: int


class ChecklistItemRead(ChecklistItemBase):
    id: int
    track_id: int
    reviewer_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChecklistSubmit(BaseModel):
    reviewer_id: int
    items: list[ChecklistItemBase]


# ---------------------------------------------------------------------------
# Enriched responses (with nested relations)
# ---------------------------------------------------------------------------
class CommentWithAuthor(CommentRead):
    author: UserRead | None = None


class IssueDetail(IssueRead):
    comments: list[CommentWithAuthor] = []
    author: UserRead | None = None
