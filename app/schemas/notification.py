from datetime import datetime

from pydantic import BaseModel, ConfigDict



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
