from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.track import RejectionMode
from app.schemas.checklist import ChecklistItemRead
from app.schemas.discussion import DiscussionRead, MentionCandidatesRead
from app.schemas.issue import IssueRead
from app.schemas.user import UserRead
from app.schemas.workflow import (
    WorkflowConfigSchema,
    WorkflowEventRead,
    WorkflowStepDefSchema,
    WorkflowTransitionOption,
)



class TrackSourceVersionRead(BaseModel):
    id: int
    workflow_cycle: int
    version_number: int
    file_path: str | None = None
    source_kind: str = "file"
    duration: float | None = None
    uploaded_by_id: int | None = None
    revision_notes: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MasterDeliveryRead(BaseModel):
    id: int
    workflow_cycle: int
    delivery_number: int
    file_path: str | None = None
    delivery_kind: str = "file"
    delivery_message: str | None = None
    uploaded_by_id: int | None = None
    confirmed_at: datetime | None = None
    producer_approved_at: datetime | None = None
    submitter_approved_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SourceFollowupRequestRead(BaseModel):
    id: int
    track_id: int
    requested_by_id: int
    decided_by_id: int | None = None
    applied_source_version_id: int | None = None
    previous_status: str
    target_stage_id: str | None = None
    reason: str
    status: str
    staged_storage_backend: str
    staged_duration: float | None = None
    created_at: datetime
    decided_at: datetime | None = None
    requested_by: UserRead | None = None
    decided_by: UserRead | None = None

    model_config = ConfigDict(from_attributes=True)


class TrackBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    artist: str = Field(..., min_length=1, max_length=100)
    album_id: int
    bpm: str | None = Field(default=None, max_length=100)
    original_title: str | None = Field(default=None, max_length=200)
    original_artist: str | None = Field(default=None, max_length=200)
    author_notes: str | None = Field(default=None, max_length=5000)
    composer_ids: list[int] = Field(default_factory=list)
    external_composer_names: list[str] = Field(default_factory=list)


class TrackMetadataUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    artist: str | None = Field(default=None, min_length=1, max_length=100)
    bpm: str | None = Field(default=None, max_length=100)
    original_title: str | None = Field(default=None, max_length=200)
    original_artist: str | None = Field(default=None, max_length=200)


class AuthorNotesUpdate(BaseModel):
    author_notes: str | None = Field(default=None, max_length=5000)


class TrackComposerUpdate(BaseModel):
    composer_ids: list[int] = Field(default_factory=list)
    external_composer_names: list[str] = Field(default_factory=list)


class TrackExternalComposerRead(BaseModel):
    id: int
    name: str
    sort_order: int = 0
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MasteringNotesUpdate(BaseModel):
    mastering_notes: str | None = Field(default=None, max_length=5000)


class TrackOrderUpdate(BaseModel):
    track_ids: list[int]


class TrackReviewStateRead(BaseModel):
    """Server-computed review progress for the track's current review step.

    Present only when the track's current workflow step is a ``review`` step;
    replaces client-side quorum/progress derivation from raw assignments.
    """

    step_id: str
    assignment_mode: str
    required_review_count: int
    active_assignment_count: int
    completed_review_count: int
    quorum_reached: bool
    requires_group_finalization: bool


class TrackRead(TrackBase):
    # artist overrides TrackBase — None when the track is shown anonymised to the viewer
    artist: str | None = None
    # composer_ids overrides TrackBase — None when the track is shown anonymised to the viewer
    composer_ids: list[int] | None = None
    id: int
    album_checklist_enabled: bool | None = None
    track_number: int | None = None
    file_path: str | None = None
    duration: float | None = None
    status: str
    rejection_mode: RejectionMode | None = None
    workflow_variant: str = "standard"
    version: int
    workflow_cycle: int
    submitter_id: int | None = None
    proxy_uploader_id: int | None = None
    peer_reviewer_id: int | None = None
    producer_id: int | None = None
    mastering_engineer_id: int | None = None
    viewer_is_album_manager: bool = False
    # Server-computed viewer-context flags (replace client-side re-derivation).
    viewer_is_composer_actor: bool = False
    viewer_is_mastering_participant: bool = False
    # None when the track has no current workflow step.
    viewer_is_step_assignee: bool | None = None
    review_state: TrackReviewStateRead | None = None
    external_submitter_name: str | None = None
    is_proxy_submission: bool = False
    author_notes: str | None = None
    mastering_notes: str | None = None
    requested_revision_type: str | None = None
    is_public: bool = False
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    issue_count: int = 0
    open_issue_count: int = 0
    submitter: UserRead | None = None
    composers: list[UserRead] = Field(default_factory=list)
    external_composers: list[TrackExternalComposerRead] = Field(default_factory=list)
    proxy_uploader: UserRead | None = None
    peer_reviewer: UserRead | None = None
    current_source_version: TrackSourceVersionRead | None = None
    current_master_delivery: MasterDeliveryRead | None = None
    pending_source_followup_request: SourceFollowupRequestRead | None = None
    allowed_actions: list[str] = []
    workflow_step: "WorkflowStepDefSchema | None" = None
    workflow_transitions: list["WorkflowTransitionOption"] | None = None

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


class TrackDetailResponse(BaseModel):
    track: TrackRead
    issues: list[IssueRead]
    checklist_items: list[ChecklistItemRead]
    events: list[WorkflowEventRead]
    source_versions: list[TrackSourceVersionRead] = []
    master_deliveries: list[MasterDeliveryRead] = []
    discussions: list["DiscussionRead"] = []
    workflow_config: "WorkflowConfigSchema | None" = None
    mention_candidates: "MentionCandidatesRead" = Field(default_factory=lambda: MentionCandidatesRead())
