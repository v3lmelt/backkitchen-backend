from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field



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


class ChecklistDraftRead(BaseModel):
    items: list[ChecklistItemRead] = []
    current_source_version_id: int | None = None
    current_source_version_number: int | None = None
    prefilled_from_source_version_id: int | None = None
    prefilled_from_source_version_number: int | None = None
    prefilled_from_current_version: bool = False
