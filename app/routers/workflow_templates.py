import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.album import Album
from app.models.circle import Circle, CircleMember
from app.models.user import User
from app.models.workflow_template import WorkflowTemplate
from app.schemas.schemas import (
    UserRead,
    WorkflowConfigSchema,
    WorkflowTemplateCreate,
    WorkflowTemplateRead,
    WorkflowTemplateUpdate,
)
from app.security import get_current_user

router = APIRouter(
    prefix="/api/circles/{circle_id}/workflow-templates",
    tags=["workflow-templates"],
)


def _get_circle_membership(
    circle_id: int, user: User, db: Session
) -> tuple[Circle, CircleMember | None]:
    circle = db.get(Circle, circle_id)
    if not circle:
        raise HTTPException(status_code=404, detail="Circle not found")
    membership = db.execute(
        select(CircleMember).where(
            CircleMember.circle_id == circle_id,
            CircleMember.user_id == user.id,
        )
    ).scalar_one_or_none()
    is_creator = circle.created_by == user.id
    if not membership and not is_creator:
        raise HTTPException(status_code=403, detail="Not a member of this circle")
    return circle, membership


def _ensure_circle_owner(user: User, circle: Circle) -> None:
    if circle.created_by != user.id:
        raise HTTPException(
            status_code=403, detail="Only the circle owner can manage workflow templates"
        )


def _template_to_read(template: WorkflowTemplate, album_count: int) -> WorkflowTemplateRead:
    config = WorkflowConfigSchema(**json.loads(template.workflow_config))
    return WorkflowTemplateRead(
        id=template.id,
        circle_id=template.circle_id,
        name=template.name,
        description=template.description,
        workflow_config=config,
        created_by=template.created_by,
        created_by_user=(
            UserRead.model_validate(template.created_by_user)
            if template.created_by_user
            else None
        ),
        album_count=album_count,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def _count_albums(template_id: int, db: Session) -> int:
    return db.scalar(
        select(func.count()).select_from(Album).where(
            Album.workflow_template_id == template_id
        )
    ) or 0


@router.get("", response_model=list[WorkflowTemplateRead])
def list_templates(
    circle_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_circle_membership(circle_id, current_user, db)
    templates = db.execute(
        select(WorkflowTemplate)
        .where(WorkflowTemplate.circle_id == circle_id)
        .order_by(WorkflowTemplate.updated_at.desc())
    ).scalars().all()

    # Batch count albums per template
    template_ids = [t.id for t in templates]
    counts: dict[int, int] = {}
    if template_ids:
        rows = db.execute(
            select(Album.workflow_template_id, func.count())
            .where(Album.workflow_template_id.in_(template_ids))
            .group_by(Album.workflow_template_id)
        ).all()
        counts = {row[0]: row[1] for row in rows}

    return [_template_to_read(t, counts.get(t.id, 0)) for t in templates]


@router.get("/{template_id}", response_model=WorkflowTemplateRead)
def get_template(
    circle_id: int,
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_circle_membership(circle_id, current_user, db)
    template = db.get(WorkflowTemplate, template_id)
    if not template or template.circle_id != circle_id:
        raise HTTPException(status_code=404, detail="Template not found")
    return _template_to_read(template, _count_albums(template_id, db))


@router.post("", response_model=WorkflowTemplateRead, status_code=status.HTTP_201_CREATED)
def create_template(
    circle_id: int,
    data: WorkflowTemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    circle, _ = _get_circle_membership(circle_id, current_user, db)
    _ensure_circle_owner(current_user, circle)

    template = WorkflowTemplate(
        circle_id=circle_id,
        name=data.name,
        description=data.description,
        workflow_config=json.dumps(
            data.workflow_config.model_dump(), ensure_ascii=False
        ),
        created_by=current_user.id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return _template_to_read(template, 0)


@router.put("/{template_id}", response_model=WorkflowTemplateRead)
def update_template(
    circle_id: int,
    template_id: int,
    data: WorkflowTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    circle, _ = _get_circle_membership(circle_id, current_user, db)
    _ensure_circle_owner(current_user, circle)

    template = db.get(WorkflowTemplate, template_id)
    if not template or template.circle_id != circle_id:
        raise HTTPException(status_code=404, detail="Template not found")

    if data.name is not None:
        template.name = data.name
    if data.description is not None:
        template.description = data.description
    if data.workflow_config is not None:
        template.workflow_config = json.dumps(
            data.workflow_config.model_dump(), ensure_ascii=False
        )

    db.commit()
    db.refresh(template)
    return _template_to_read(template, _count_albums(template_id, db))


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    circle_id: int,
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    circle, _ = _get_circle_membership(circle_id, current_user, db)
    _ensure_circle_owner(current_user, circle)

    template = db.get(WorkflowTemplate, template_id)
    if not template or template.circle_id != circle_id:
        raise HTTPException(status_code=404, detail="Template not found")

    # Clear FK references on albums before deleting
    db.execute(
        Album.__table__.update()
        .where(Album.workflow_template_id == template_id)
        .values(workflow_template_id=None)
    )

    db.delete(template)
    db.commit()
