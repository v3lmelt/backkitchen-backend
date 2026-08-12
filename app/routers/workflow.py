"""Global workflow metadata endpoints.

Serves the built-in default workflow configuration so clients no longer need
to keep a verbatim local copy (previously duplicated in the frontend's
``src/utils/workflowConfig.ts``).  Steps are annotated with server-computed
metadata (``is_mastering_related``) via
``app.track_serializers.annotate_workflow_step_metadata``.
"""

import copy

from fastapi import APIRouter, Depends

from app.models.user import User
from app.schemas.schemas import WorkflowConfigSchema
from app.security import get_current_user
from app.track_serializers import annotate_workflow_step_metadata
from app.workflow_defaults import DEFAULT_WORKFLOW_CONFIG

router = APIRouter(prefix="/api/workflow", tags=["workflow"])


@router.get("/default-config", response_model=WorkflowConfigSchema)
def get_default_workflow_config(
    current_user: User = Depends(get_current_user),
) -> WorkflowConfigSchema:
    """Return the built-in default workflow config for new albums."""
    return annotate_workflow_step_metadata(
        WorkflowConfigSchema(**copy.deepcopy(DEFAULT_WORKFLOW_CONFIG))
    )
