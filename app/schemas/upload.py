from pydantic import BaseModel, Field, field_validator



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
    author_notes: str | None = Field(default=None, max_length=5000)
    proxy_submission: bool = False
    external_submitter_name: str | None = Field(default=None, max_length=100)
    composer_ids: list[int] = Field(default_factory=list)
    external_composer_names: list[str] = Field(default_factory=list)


class PresignedUploadResponse(BaseModel):
    upload_url: str
    object_key: str
    upload_id: str
    expires_in: int


class ConfirmUploadParams(BaseModel):
    upload_id: str
    object_key: str
    duration: float | None = None
    revision_notes: str | None = Field(default=None, max_length=5000)
    resolved_issue_ids: list[int] = Field(default_factory=list)
    resolution_note: str | None = Field(default=None, max_length=5000)


class SourceExternalLinkSubmission(BaseModel):
    revision_notes: str = Field(..., min_length=1, max_length=5000)
    resolved_issue_ids: list[int] = Field(default_factory=list)
    resolution_note: str | None = Field(default=None, max_length=5000)

    @field_validator("revision_notes")
    @classmethod
    def revision_notes_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("External source link notes cannot be blank.")
        return stripped


class ConfirmMasterDeliveryUploadParams(ConfirmUploadParams):
    delivery_message: str | None = Field(default=None, max_length=5000)


class ConfirmSourceFollowupUploadParams(BaseModel):
    upload_id: str
    object_key: str
    duration: float | None = None
    reason: str = Field(..., min_length=1, max_length=2000)


class ConfirmTrackUploadParams(ConfirmUploadParams):
    album_id: int
    title: str
    artist: str
    bpm: str | None = None
    original_title: str | None = None
    original_artist: str | None = None
    author_notes: str | None = Field(default=None, max_length=5000)
    proxy_submission: bool = False
    external_submitter_name: str | None = Field(default=None, max_length=100)
    composer_ids: list[int] = Field(default_factory=list)
    external_composer_names: list[str] = Field(default_factory=list)


class RequestCommentAudioUploadParams(BaseModel):
    files: list[RequestUploadParams]


class PresignedCommentAudioResponse(BaseModel):
    uploads: list[PresignedUploadResponse]


class AppConfigResponse(BaseModel):
    r2_enabled: bool
