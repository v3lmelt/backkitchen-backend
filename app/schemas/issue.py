from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.issue import IssueSeverity, IssueStatus, MarkerType
from app.schemas.user import UserRead



class IssueMarkerCreate(BaseModel):
    marker_type: MarkerType = MarkerType.POINT
    time_start: float = Field(..., ge=0)
    time_end: float | None = Field(default=None, ge=0)


class IssueMarkerRead(BaseModel):
    id: int
    issue_id: int
    marker_type: MarkerType
    time_start: float
    time_end: float | None = None

    model_config = ConfigDict(from_attributes=True)


class IssueBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    severity: IssueSeverity = IssueSeverity.MAJOR



class IssueCreate(IssueBase):
    phase: str | None = Field(
        default=None,
        description=(
            "Issue phase. When omitted, the backend infers the canonical phase "
            "from the track's current workflow step. An explicit value still "
            "wins and must match the current step."
        ),
    )
    markers: list[IssueMarkerCreate] = []
    visibility: str = Field(
        default="public",
        description=(
            "Initial issue visibility. Explicit 'internal' is allowed only during an active multi-review "
            "step and creates a pending_discussion issue hidden from track composers; 'public' creates an open "
            "issue. When omitted, multi-review steps retain the submitter-hidden default and other steps "
            "default to public."
        ),
    )



class IssueUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: IssueStatus | None = None
    severity: IssueSeverity | None = None
    status_note: str | None = None


class IssueAudioRead(BaseModel):
    id: int
    issue_id: int
    audio_url: str
    original_filename: str
    duration: float | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class IssueImageRead(BaseModel):
    id: int
    issue_id: int
    image_url: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class IssueRead(IssueBase):
    id: int
    track_id: int
    local_number: int
    author_id: int
    phase: str
    workflow_cycle: int
    source_version_id: int | None = None
    source_version_number: int | None = None
    master_delivery_id: int | None = None
    status: IssueStatus
    markers: list[IssueMarkerRead] = []
    audios: list[IssueAudioRead] = []
    images: list[IssueImageRead] = []
    created_at: datetime
    updated_at: datetime
    comment_count: int = 0
    author: UserRead | None = None

    model_config = ConfigDict(from_attributes=True)


class CommentImageRead(BaseModel):
    id: int
    comment_id: int
    image_url: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CommentAudioRead(BaseModel):
    id: int
    comment_id: int
    audio_url: str
    original_filename: str
    duration: float | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CommentRead(BaseModel):
    id: int
    issue_id: int
    author_id: int
    content: str
    visibility: str = "public"
    is_status_note: bool = False
    old_status: str | None = None
    new_status: str | None = None
    created_at: datetime
    edited_at: datetime | None = None
    author: UserRead | None = None
    images: list[CommentImageRead] = []
    audios: list[CommentAudioRead] = []

    model_config = ConfigDict(from_attributes=True)


class IssueDetail(IssueRead):
    comments: list[CommentRead] = []


class IssueBatchUpdate(BaseModel):
    issue_ids: list[int]
    status: IssueStatus
    status_note: str | None = None
