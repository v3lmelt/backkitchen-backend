from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.issue import IssuePhase, IssueSeverity, IssueStatus, MarkerType
from app.models.track import RejectionMode, TrackStatus
from app.workflow_defaults import SPECIAL_TARGETS


class UserBase(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    display_name: str = Field(..., min_length=1, max_length=100)
    role: str = Field(default="member", pattern=r"^(member|producer)$")
    avatar_color: str = Field(default="#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")
    avatar_image: str | None = None


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
    email_verified: bool = False
    is_admin: bool = False
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminUserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern=r"^(member|producer)$")
    is_admin: bool | None = None
    email_verified: bool | None = None


class UserUpdateProfile(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., min_length=3)


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)


class DeleteAccountRequest(BaseModel):
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserRead


class RegisterResponse(BaseModel):
    email: str
    message: str = "Verification email sent. Please check your inbox."


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
    release_date: date | None = None
    catalog_number: str | None = Field(default=None, max_length=50)
    circle_id: int | None = None
    circle_name: str | None = Field(default=None, max_length=200)
    genres: list[str] | None = None
    workflow_config: "WorkflowConfigSchema | None" = None
    workflow_template_id: int | None = None


class AlbumMetadataUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    release_date: date | None = None
    catalog_number: str | None = Field(default=None, max_length=50)
    circle_name: str | None = Field(default=None, max_length=200)
    genres: list[str] | None = None


class AlbumTeamUpdate(BaseModel):
    mastering_engineer_id: int | None = None
    member_ids: list[int] = []


class AlbumSummary(BaseModel):
    id: int
    title: str
    cover_color: str
    cover_image: str | None = None
    circle_name: str | None = None
    catalog_number: str | None = None

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


class AlbumDeadlineUpdate(BaseModel):
    deadline: datetime | None = None
    phase_deadlines: dict[str, str] | None = None


class AlbumRead(AlbumBase):
    id: int
    release_date: date | None = None
    catalog_number: str | None = None
    circle_id: int | None = None
    circle_name: str | None = None
    genres: list[str] | None = None
    cover_image: str | None = None
    producer_id: int | None = None
    mastering_engineer_id: int | None = None
    deadline: datetime | None = None
    phase_deadlines: dict[str, str] | None = None
    workflow_config: "WorkflowConfigSchema | None" = None
    workflow_template_id: int | None = None
    workflow_template_name: str | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    track_count: int = 0
    producer: UserRead | None = None
    mastering_engineer: UserRead | None = None
    members: list[AlbumMemberRead] = []

    model_config = ConfigDict(from_attributes=True)


class CircleMemberRead(BaseModel):
    id: int
    circle_id: int
    user_id: int
    role: str
    joined_at: datetime
    user: UserRead

    model_config = ConfigDict(from_attributes=True)


class CircleBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    website: str | None = Field(default=None, max_length=200)


class CircleCreate(CircleBase):
    pass


class CircleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    website: str | None = Field(default=None, max_length=200)


class CircleRead(CircleBase):
    id: int
    logo_url: str | None = None
    created_by: int
    created_at: datetime
    members: list[CircleMemberRead] = []

    model_config = ConfigDict(from_attributes=True)


class CircleSummary(BaseModel):
    id: int
    name: str
    description: str | None = None
    logo_url: str | None = None
    created_by: int
    member_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class InviteCodeCreate(BaseModel):
    role: str = Field(default="member", pattern=r"^(member|mastering_engineer)$")
    expires_in_days: int = Field(default=7, ge=1, le=30)


class InviteCodeRead(BaseModel):
    id: int
    circle_id: int
    code: str
    role: str
    expires_at: datetime
    is_active: bool
    created_at: datetime
    created_by_user: UserRead

    model_config = ConfigDict(from_attributes=True)


class JoinCircleRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=16)


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
    confirmed_at: datetime | None = None
    producer_approved_at: datetime | None = None
    submitter_approved_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TrackBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    artist: str = Field(..., min_length=1, max_length=100)
    album_id: int
    bpm: str | None = Field(default=None, max_length=100)
    original_title: str | None = Field(default=None, max_length=200)
    original_artist: str | None = Field(default=None, max_length=200)


class TrackMetadataUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    artist: str | None = Field(default=None, min_length=1, max_length=100)
    bpm: str | None = Field(default=None, max_length=100)
    original_title: str | None = Field(default=None, max_length=200)
    original_artist: str | None = Field(default=None, max_length=200)


class TrackOrderUpdate(BaseModel):
    track_ids: list[int]


class TrackRead(TrackBase):
    # artist overrides TrackBase — None when the track is shown anonymised to the viewer
    artist: str | None = None
    id: int
    track_number: int | None = None
    file_path: str | None = None
    duration: float | None = None
    status: str
    rejection_mode: RejectionMode | None = None
    workflow_variant: str = "standard"
    version: int
    workflow_cycle: int
    submitter_id: int | None = None
    peer_reviewer_id: int | None = None
    producer_id: int | None = None
    mastering_engineer_id: int | None = None
    is_public: bool = False
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    issue_count: int = 0
    open_issue_count: int = 0
    submitter: UserRead | None = None
    peer_reviewer: UserRead | None = None
    current_source_version: TrackSourceVersionRead | None = None
    current_master_delivery: MasterDeliveryRead | None = None
    allowed_actions: list[str] = []
    workflow_step: "WorkflowStepDefSchema | None" = None
    workflow_transitions: list[dict[str, str]] | None = None

    model_config = ConfigDict(from_attributes=True)


class TrackListItem(TrackRead):
    album_title: str = ""


class TrackPlaybackPreferenceRead(BaseModel):
    track_id: int
    user_id: int
    scope: Literal["source", "master"]
    gain_db: float = Field(default=0.0, ge=-24, le=24)
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class TrackPlaybackPreferenceUpdate(BaseModel):
    gain_db: float = Field(..., ge=-24, le=24)


class SetPublicRequest(BaseModel):
    is_public: bool


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
    phase: str
    markers: list[IssueMarkerCreate] = []
    visibility: str = "public"  # "public" → open, "internal" → pending_discussion (reviewer-only)


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


class ChecklistTemplateItem(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    required: bool = True
    sort_order: int = 0


class ChecklistTemplateRead(BaseModel):
    items: list[ChecklistTemplateItem]
    is_default: bool = False


class ChecklistTemplateUpdate(BaseModel):
    items: list[ChecklistTemplateItem]


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
    master_deliveries: list[MasterDeliveryRead] = []
    discussions: list["DiscussionRead"] = []
    workflow_config: "WorkflowConfigSchema | None" = None


class NotificationRead(BaseModel):
    id: int
    user_id: int
    type: str
    title: str
    body: str
    related_track_id: int | None = None
    related_issue_id: int | None = None
    related_album_id: int | None = None
    is_read: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AlbumStats(BaseModel):
    total_tracks: int
    by_status: dict[str, int]
    open_issues: int
    recent_events: list[WorkflowEventRead]
    deadline: datetime | None = None
    overdue_track_count: int = 0


class DiscussionImageRead(BaseModel):
    id: int
    discussion_id: int
    image_url: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DiscussionRead(BaseModel):
    id: int
    track_id: int
    author_id: int
    visibility: str = "public"
    content: str
    created_at: datetime
    edited_at: datetime | None = None
    author: UserRead | None = None
    images: list[DiscussionImageRead] = []

    model_config = ConfigDict(from_attributes=True)


class DiscussionUpdate(BaseModel):
    content: str = Field(..., min_length=1, max_length=5000)


class CommentUpdate(BaseModel):
    content: str = Field(..., min_length=1, max_length=5000)


class EditHistoryRead(BaseModel):
    id: int
    entity_type: str
    entity_id: int
    old_content: str
    edited_by_id: int
    created_at: datetime
    editor: UserRead | None = None

    model_config = ConfigDict(from_attributes=True)


class WebhookConfig(BaseModel):
    url: str = ""
    enabled: bool = False
    events: list[str] = []


class WebhookDeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    success: bool
    status_code: int | None
    target_url: str
    error_detail: str | None
    created_at: datetime


class IssueBatchUpdate(BaseModel):
    issue_ids: list[int]
    status: IssueStatus
    status_note: str | None = None


# ── Workflow config schemas ──────────────────────────────────────────────────


class WorkflowTransitionRequest(BaseModel):
    decision: str = Field(..., min_length=1, max_length=50)


class WorkflowStepDefSchema(BaseModel):
    id: str = Field(..., pattern=r"^[a-z][a-z0-9_]{1,49}$")
    label: str = Field(..., min_length=1, max_length=100)
    type: Literal["approval", "gate", "review", "revision", "delivery"]
    ui_variant: Literal[
        "generic",
        "intake",
        "peer_review",
        "producer_gate",
        "mastering",
        "final_review",
    ] | None = None
    assignee_role: str
    order: int = Field(..., ge=0)
    transitions: dict[str, str] = {}
    return_to: str | None = None
    revision_step: str | None = None
    # Approval-specific
    allow_permanent_reject: bool | None = None
    # Review-specific
    assignment_mode: Literal["manual", "auto"] | None = None
    reviewer_pool: list[int] | None = None
    required_reviewer_count: int | None = Field(default=None, ge=1)
    # Approval/delivery override
    assignee_user_id: int | None = None
    # Delivery-specific
    require_confirmation: bool | None = None
    # Additional roles that may act on this step
    actor_roles: list[str] | None = None

    @model_validator(mode="after")
    def normalize_gate_to_approval(self) -> "WorkflowStepDefSchema":
        """Accept legacy ``gate`` type but normalise to ``approval``."""
        if self.type == "gate":
            self.type = "approval"
        return self


class WorkflowConfigSchema(BaseModel):
    version: int = 1
    steps: list[WorkflowStepDefSchema] = Field(..., min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_workflow(self) -> "WorkflowConfigSchema":
        step_by_id = {s.id: s for s in self.steps}
        step_ids = set(step_by_id.keys())
        seen_ids: set[str] = set()
        seen_orders: set[int] = set()

        for step in self.steps:
            # Unique IDs
            if step.id in seen_ids:
                raise ValueError(f"Duplicate step id: '{step.id}'")
            seen_ids.add(step.id)

            # Unique order index to keep step ordering deterministic
            if step.order in seen_orders:
                raise ValueError(f"Duplicate step order: '{step.order}'")
            seen_orders.add(step.order)

            # Validate transition targets
            for decision, target in step.transitions.items():
                if target not in step_ids and target not in SPECIAL_TARGETS:
                    raise ValueError(
                        f"Step '{step.id}' transition '{decision}' targets "
                        f"unknown step '{target}'"
                    )

                # reject_to_* must always be a rollback to an earlier stage.
                if decision.startswith("reject_to_"):
                    if target not in step_ids:
                        raise ValueError(
                            f"Step '{step.id}' transition '{decision}' must target a workflow step, "
                            f"not special target '{target}'"
                        )
                    target_step = step_by_id[target]
                    if target_step.id == step.id:
                        raise ValueError(
                            f"Step '{step.id}' transition '{decision}' cannot target itself"
                        )
                    if target_step.order >= step.order:
                        raise ValueError(
                            f"Step '{step.id}' transition '{decision}' must target an earlier step. "
                            f"Got order {target_step.order} >= {step.order}"
                        )

            # Revision steps must have return_to
            if step.type == "revision":
                if not step.return_to:
                    raise ValueError(
                        f"Revision step '{step.id}' must have 'return_to'"
                    )
                if step.return_to not in step_ids:
                    raise ValueError(
                        f"Revision step '{step.id}' return_to targets "
                        f"unknown step '{step.return_to}'"
                    )

            # Review/delivery steps with revision_step must reference a valid revision step
            if step.revision_step:
                if step.revision_step not in step_ids:
                    raise ValueError(
                        f"Step '{step.id}' revision_step targets "
                        f"unknown step '{step.revision_step}'"
                    )
                target_step = next(s for s in self.steps if s.id == step.revision_step)
                if target_step.type != "revision":
                    raise ValueError(
                        f"Step '{step.id}' revision_step '{step.revision_step}' "
                        f"must be of type 'revision'"
                    )

        # Completion path requirement: either a transition to __completed
        # (generic engine path) or a final_review step (which completes via
        # the dedicated /final-review/approve dual-confirmation endpoint).
        has_completed_transition = any(
            "__completed" in step.transitions.values() for step in self.steps
        )
        has_final_review_step = any(
            step.ui_variant == "final_review" or step.id == "final_review"
            for step in self.steps
        )
        if not (has_completed_transition or has_final_review_step):
            raise ValueError(
                "Workflow must have at least one path to completion "
                "(either a transition to '__completed' or a 'final_review' step)"
            )

        return self


# ── Stage assignment schemas ──────────────────────────────────────────────────


class StageAssignmentRead(BaseModel):
    id: int
    track_id: int
    stage_id: str
    user_id: int
    status: str
    decision: str | None = None
    cancellation_reason: str | None = None
    assigned_at: datetime
    completed_at: datetime | None = None
    user: UserRead | None = None

    model_config = ConfigDict(from_attributes=True)


class AssignReviewerRequest(BaseModel):
    user_ids: list[int] = Field(..., min_length=1)


class ReassignReviewerRequest(BaseModel):
    user_ids: list[int] | None = None
    user_id: int | None = None


# ── Reopen request schemas ────────────────────────────────────────────────────


class ReopenRequestCreate(BaseModel):
    target_stage_id: str = Field(..., min_length=1, max_length=50)
    reason: str = Field(..., min_length=1, max_length=2000)


class DirectReopenRequest(BaseModel):
    target_stage_id: str = Field(..., min_length=1, max_length=50)


class ReopenDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]


class ReopenRequestRead(BaseModel):
    id: int
    track_id: int
    requested_by_id: int
    target_stage_id: str
    reason: str
    status: str
    decided_by_id: int | None = None
    created_at: datetime
    decided_at: datetime | None = None
    requested_by: UserRead | None = None
    decided_by: UserRead | None = None

    model_config = ConfigDict(from_attributes=True)


# ── Workflow template schemas ────────────────────────────────────────────────


class WorkflowTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    workflow_config: WorkflowConfigSchema


class WorkflowTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    workflow_config: WorkflowConfigSchema | None = None


class WorkflowTemplateRead(BaseModel):
    id: int
    circle_id: int
    name: str
    description: str | None = None
    workflow_config: WorkflowConfigSchema
    created_by: int
    created_by_user: UserRead | None = None
    album_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ── R2 presigned upload schemas ──────────────────────────────────────────────

class RequestUploadParams(BaseModel):
    filename: str
    content_type: str
    file_size: int


class RequestTrackUploadParams(RequestUploadParams):
    album_id: int
    title: str
    artist: str
    bpm: str | None = None
    original_title: str | None = None
    original_artist: str | None = None


class PresignedUploadResponse(BaseModel):
    upload_url: str
    object_key: str
    upload_id: str
    expires_in: int


class ConfirmUploadParams(BaseModel):
    upload_id: str
    object_key: str
    duration: float | None = None


class ConfirmTrackUploadParams(ConfirmUploadParams):
    album_id: int
    title: str
    artist: str
    bpm: str | None = None
    original_title: str | None = None
    original_artist: str | None = None


class RequestCommentAudioUploadParams(BaseModel):
    files: list[RequestUploadParams]


class PresignedCommentAudioResponse(BaseModel):
    uploads: list[PresignedUploadResponse]


class AppConfigResponse(BaseModel):
    r2_enabled: bool
