from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import UserRead



class DiscussionImageRead(BaseModel):
    id: int
    discussion_id: int
    image_url: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DiscussionAudioRead(BaseModel):
    id: int
    discussion_id: int
    audio_url: str
    original_filename: str
    duration: float | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DiscussionRead(BaseModel):
    id: int
    track_id: int
    author_id: int
    visibility: str = "public"
    phase: str = "general"
    content: str
    created_at: datetime
    edited_at: datetime | None = None
    author: UserRead | None = None
    images: list[DiscussionImageRead] = []
    audios: list[DiscussionAudioRead] = []

    model_config = ConfigDict(from_attributes=True)


class MentionCandidatesRead(BaseModel):
    general: list[UserRead] = Field(default_factory=list)
    mastering: list[UserRead] = Field(default_factory=list)
    issue_public: list[UserRead] = Field(default_factory=list)
    issue_internal: list[UserRead] = Field(default_factory=list)


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
