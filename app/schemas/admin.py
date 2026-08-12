from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.user import UserRead
from app.schemas.workflow import WorkflowEventRead



class AdminUserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern=r"^(member|producer)$")
    is_admin: bool | None = None
    admin_role: str | None = Field(default=None, pattern=r"^(none|viewer|operator|superadmin)$")
    email_verified: bool | None = None


class AdminDashboardStats(BaseModel):
    total_users: int
    users_by_role: dict[str, int]
    total_albums: int
    active_albums: int
    archived_albums: int = 0
    total_tracks: int
    tracks_by_status: dict[str, int]
    archived_tracks: int = 0
    open_issues: int
    pending_reopen_requests: int = 0
    failed_webhook_deliveries: int = 0
    unverified_users: int = 0
    suspended_users: int = 0
    stalled_tracks: int = 0
    recent_events: list["WorkflowEventRead"] = []
    recent_audits: list["AdminAuditLogRead"] = []


class AdminActivityLogEntry(BaseModel):
    id: int
    event_type: str
    from_status: str | None = None
    to_status: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime
    actor: UserRead | None = None
    track_id: int | None = None
    track_title: str | None = None
    album_id: int | None = None
    album_title: str | None = None


class AdminOptionalReasonMixin(BaseModel):
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class AdminForceStatus(AdminOptionalReasonMixin):
    new_status: str = Field(..., min_length=1, max_length=50)


class AdminReassign(AdminOptionalReasonMixin):
    user_ids: list[int] = Field(..., min_length=1)


class AdminReasonPayload(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class AdminTransferOwnershipRequest(BaseModel):
    target_user_id: int
    reason: str = Field(..., min_length=1, max_length=500)


class AdminTrackReopen(AdminOptionalReasonMixin):
    target_stage_id: str = Field(..., min_length=1, max_length=50)


class AdminOptionalReasonPayload(AdminOptionalReasonMixin):
    pass


class AdminReopenDecision(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(..., min_length=1, max_length=500)


class AdminReopenRequestEntry(BaseModel):
    id: int
    track_id: int
    track_title: str | None = None
    album_id: int | None = None
    album_title: str | None = None
    requested_by_id: int
    target_stage_id: str
    reason: str
    mastering_notes: str | None = None
    status: str
    decided_by_id: int | None = None
    created_at: datetime
    decided_at: datetime | None = None
    requested_by: UserRead | None = None
    decided_by: UserRead | None = None


class AdminAuditLogRead(BaseModel):
    id: int
    action: str
    entity_type: str
    entity_id: int | None = None
    summary: str | None = None
    reason: str | None = None
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    target_user_id: int | None = None
    album_id: int | None = None
    track_id: int | None = None
    circle_id: int | None = None
    created_at: datetime
    actor: UserRead | None = None
    target_user: UserRead | None = None


class TrackForceStatusRequest(AdminOptionalReasonMixin):
    new_status: str = Field(..., min_length=1, max_length=50)
