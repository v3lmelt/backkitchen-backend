from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import UserRead



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
    default_checklist_enabled: bool = False



class CircleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    website: str | None = Field(default=None, max_length=200)
    default_checklist_enabled: bool | None = None


class CircleMemberRoleUpdate(BaseModel):
    role: str = Field(pattern=r"^(member|mastering_engineer|co_producer)$")


class CircleRead(CircleBase):
    id: int
    logo_url: str | None = None
    default_checklist_enabled: bool
    created_by: int
    created_at: datetime
    members: list[CircleMemberRead] = []

    model_config = ConfigDict(from_attributes=True)


class CircleSummary(BaseModel):
    id: int
    name: str
    description: str | None = None
    logo_url: str | None = None
    default_checklist_enabled: bool
    created_by: int
    member_count: int = 0
    viewer_can_create_album: bool = False

    model_config = ConfigDict(from_attributes=True)


class InviteCodeCreate(BaseModel):
    role: str = Field(default="member", pattern=r"^(member|mastering_engineer)$")
    expires_in_days: int = Field(default=7, ge=1, le=365)


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
