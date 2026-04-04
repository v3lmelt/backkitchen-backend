from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.issue import IssuePhase, IssueSeverity, IssueStatus, IssueType
from app.models.track import RejectionMode, TrackStatus


class UserBase(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    display_name: str = Field(..., min_length=1, max_length=100)
    role: str = Field(default="member", pattern=r"^(member|producer|mastering_engineer)$")
    avatar_color: str = Field(default="#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")


class UserCreate(UserBase):
    email: str | None = None
    password: str | None = Field(default=None, min_length=8)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    display_name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=8)


class UserRead(UserBase):
    id: int
    email: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserUpdateProfile(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserRead


class AlbumMemberRead(BaseModel):
    id: int
    user_id: int
    created_at: datetime
    user: UserRead


class AlbumBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    cover_color: str = Field(default="#8b5cf6", pattern=r"^#[0-9a-fA-F]{6}$")


class AlbumCreate(AlbumBase):
    pass


class AlbumTeamUpdate(BaseModel):
    mastering_engineer_id: int | None = None
    member_ids: list[int] = []


class AlbumSummary(BaseModel):
    id: int
    title: str
    cover_color: str

    model_config = ConfigDict(from_attributes=True)


class InvitationCreate(BaseModel):
    user_id: int


class InvitationRead(BaseModel):
    id: int
    album_id: int
    user_id: int
    invited_by_user_id: int
    status: str
    created_at: datetime
    album: AlbumSummary | None = None
    user: UserRead | None = None
    invited_by_user: UserRead | None = None

    model_config = ConfigDict(from_attributes=True)


class AlbumRead(AlbumBase):
    id: int
    producer_id: int | None = None
    mastering_engineer_id: int | None = None
    created_at: datetime
    updated_at: datetime
    track_count: int = 0
    producer: UserRead | None = None
    mastering_engineer: UserRead | None = None
    members: list[AlbumMemberRead] = []

    model_config = ConfigDict(from_attributes=True)


class TrackSourceVersionRead(BaseModel):
    id: int
    workflow_cycle: int
    version_number: int
    file_path: str
    duration: float | None = None
    uploaded_by_id: int | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MasterDeliveryRead(BaseModel):
    id: int
    workflow_cycle: int
    delivery_number: int
    file_path: str
    uploaded_by_id: int | None = None
    producer_approved_at: datetime | None = None
    submitter_approved_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TrackBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    artist: str = Field(..., min_length=1, max_length=100)
    album_id: int
    bpm: int | None = None


class TrackRead(TrackBase):
    id: int
    file_path: str | None = None
    duration: float | None = None
    status: TrackStatus
    rejection_mode: RejectionMode | None = None
    version: int
    workflow_cycle: int
    submitter_id: int | None = None
    peer_reviewer_id: int | None = None
    producer_id: int | None = None
    mastering_engineer_id: int | None = None
    created_at: datetime
    updated_at: datetime
    issue_count: int = 0
    open_issue_count: int = 0
    submitter: UserRead | None = None
    peer_reviewer: UserRead | None = None
    current_source_version: TrackSourceVersionRead | None = None
    current_master_delivery: MasterDeliveryRead | None = None
    allowed_actions: list[str] = []

    model_config = ConfigDict(from_attributes=True)


class TrackListItem(TrackRead):
    album_title: str = ""


class IntakeDecisionRequest(BaseModel):
    decision: Literal["accept", "reject_final", "reject_resubmittable"]


class PeerReviewDecisionRequest(BaseModel):
    decision: Literal["needs_revision", "pass"]


class ProducerGateDecisionRequest(BaseModel):
    decision: Literal["send_to_mastering", "request_peer_revision"]


class IssueBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    issue_type: IssueType = IssueType.POINT
    severity: IssueSeverity = IssueSeverity.MAJOR
    time_start: float = Field(..., ge=0)
    time_end: float | None = Field(default=None, ge=0)


class IssueCreate(IssueBase):
    phase: IssuePhase


class IssueUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: IssueStatus | None = None
    severity: IssueSeverity | None = None
    status_note: str | None = None


class IssueRead(IssueBase):
    id: int
    track_id: int
    author_id: int
    phase: IssuePhase
    workflow_cycle: int
    source_version_id: int | None = None
    master_delivery_id: int | None = None
    status: IssueStatus
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


class CommentRead(BaseModel):
    id: int
    issue_id: int
    author_id: int
    content: str
    is_status_note: bool = False
    created_at: datetime
    author: UserRead | None = None
    images: list[CommentImageRead] = []

    model_config = ConfigDict(from_attributes=True)


class IssueDetail(IssueRead):
    comments: list[CommentRead] = []


class ChecklistItemBase(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    passed: bool = False
    note: str | None = None


class ChecklistItemRead(ChecklistItemBase):
    id: int
    track_id: int
    reviewer_id: int
    source_version_id: int | None = None
    workflow_cycle: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChecklistSubmit(BaseModel):
    items: list[ChecklistItemBase]


class WorkflowEventRead(BaseModel):
    id: int
    event_type: str
    from_status: str | None = None
    to_status: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime
    actor: UserRead | None = None


class TrackDetailResponse(BaseModel):
    track: TrackRead
    issues: list[IssueRead]
    checklist_items: list[ChecklistItemRead]
    events: list[WorkflowEventRead]
    source_versions: list[TrackSourceVersionRead] = []


class NotificationRead(BaseModel):
    id: int
    user_id: int
    type: str
    title: str
    body: str
    related_track_id: int | None = None
    related_issue_id: int | None = None
    is_read: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AlbumStats(BaseModel):
    total_tracks: int
    by_status: dict[str, int]
    open_issues: int
    recent_events: list[WorkflowEventRead]


class IssueBatchUpdate(BaseModel):
    issue_ids: list[int]
    status: IssueStatus
    status_note: str | None = None
