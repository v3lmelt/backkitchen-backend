from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import UserRead
from app.schemas.workflow import WorkflowConfigSchema, WorkflowEventRead
from app.schemas.audio_analysis import AudioSpecs



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
    checklist_enabled: bool | None = None
    quick_followup_enabled: bool = False
    genres: list[str] | None = None
    mastering_engineer_id: int | None = None
    member_ids: list[int] = Field(default_factory=list)
    deadline: datetime | None = None
    phase_deadlines: dict[str, str] | None = None
    workflow_config: "WorkflowConfigSchema | None" = None
    workflow_template_id: int | None = None


class AlbumMetadataUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    release_date: date | None = None
    catalog_number: str | None = Field(default=None, max_length=50)
    circle_name: str | None = Field(default=None, max_length=200)
    checklist_enabled: bool | None = None
    quick_followup_enabled: bool | None = None
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
    checklist_enabled: bool
    quick_followup_enabled: bool
    genres: list[str] | None = None
    cover_image: str | None = None
    producer_id: int | None = None
    mastering_engineer_id: int | None = None
    viewer_is_album_manager: bool = False
    viewer_can_force_track_status: bool = False
    viewer_circle_role: str | None = None
    deadline: datetime | None = None
    phase_deadlines: dict[str, str] | None = None
    workflow_config: "WorkflowConfigSchema | None" = None
    audio_specs: AudioSpecs = Field(default_factory=AudioSpecs)
    workflow_template_id: int | None = None
    workflow_template_name: str | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    is_completed: bool = False
    track_count: int = 0
    total_tracks: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    open_issues: int = 0
    recent_events: list["WorkflowEventRead"] = Field(default_factory=list)
    overdue_track_count: int = 0
    producer: UserRead | None = None
    mastering_engineer: UserRead | None = None
    members: list[AlbumMemberRead] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class AlbumStats(BaseModel):
    is_completed: bool = False
    total_tracks: int
    by_status: dict[str, int]
    open_issues: int
    recent_events: list[WorkflowEventRead]
    deadline: datetime | None = None
    overdue_track_count: int = 0


class WebhookConfig(BaseModel):
    url: str = ""
    enabled: bool = False
    events: list[str] = []
    type: str = "generic"
    secret: str = ""
    app_id: str = ""
    app_secret: str = ""
    filter_user_ids: list[int] = []
    email_enabled: bool = False
    email_events: list[str] = []


class WebhookDeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    success: bool
    status_code: int | None
    target_url: str
    error_detail: str | None
    created_at: datetime
