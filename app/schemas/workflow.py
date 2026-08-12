from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.user import UserRead
from app.workflow_defaults import SPECIAL_TARGETS



class WorkflowEventRead(BaseModel):
    id: int
    event_type: str
    from_status: str | None = None
    to_status: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime
    actor: UserRead | None = None


class WorkflowTransitionRequest(BaseModel):
    decision: str = Field(..., min_length=1, max_length=50)
    revision_type: str | None = Field(default=None)

    @field_validator("revision_type")
    @classmethod
    def validate_revision_type(cls, value: str | None) -> str | None:
        if value is not None and value not in ("source_audio", "stem_files"):
            raise ValueError("revision_type must be 'source_audio' or 'stem_files'")
        return value


class WorkflowTransitionOption(BaseModel):
    decision: str
    label: str
    kind: Literal["approve", "reject", "revision", "neutral"] = "neutral"
    requires_confirmation: bool = False


class WorkflowStepDefSchema(BaseModel):
    id: str = Field(..., pattern=r"^[a-z][a-z0-9_]{1,49}$")
    label: str = Field(..., min_length=1, max_length=100)
    type: Literal["approval", "gate", "review", "revision", "delivery"]
    ui_variant: Literal[
        "generic",
        "intake",
        "peer_review",
        "producer_gate",
        "mastering",
        "final_review",
    ] | None = None
    assignee_role: str
    order: int = Field(..., ge=0)
    transitions: dict[str, str] = {}
    return_to: str | None = None
    revision_step: str | None = None
    # Approval-specific
    allow_permanent_reject: bool | None = None
    # Review-specific
    assignment_mode: Literal["manual", "auto", "fixed"] | None = None
    reviewer_pool: list[int] | None = None
    required_reviewer_count: int | None = Field(default=None, ge=1)
    revision_decision_policy: Literal["quorum_final", "first_revision_request"] | None = None
    # Approval/delivery override
    assignee_user_id: int | None = None
    # Delivery-specific
    require_confirmation: bool | None = None
    # Additional roles that may act on this step
    actor_roles: list[str] | None = None
    # Server-computed output metadata (ignored on input): True when the step
    # belongs to the mastering flow (delivery type, mastering ui_variant, or a
    # master* step id) — mirrors workflow_engine.target_is_mastering_related.
    is_mastering_related: bool | None = None

    # Allows direct validation from the ``StepDef`` dataclass so serializers
    # cannot silently drop newly added fields.
    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def normalize_gate_to_approval(self) -> "WorkflowStepDefSchema":
        """Accept legacy ``gate`` type but normalise to ``approval``."""
        if self.type == "gate":
            self.type = "approval"
        return self


class WorkflowConfigSchema(BaseModel):
    version: int = 1
    steps: list[WorkflowStepDefSchema] = Field(..., min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_workflow(self) -> "WorkflowConfigSchema":
        step_by_id = {s.id: s for s in self.steps}
        step_ids = set(step_by_id.keys())
        seen_ids: set[str] = set()
        seen_orders: set[int] = set()

        for step in self.steps:
            # Unique IDs
            if step.id in seen_ids:
                raise ValueError(f"Duplicate step id: '{step.id}'")
            seen_ids.add(step.id)

            # Unique order index to keep step ordering deterministic
            if step.order in seen_orders:
                raise ValueError(f"Duplicate step order: '{step.order}'")
            seen_orders.add(step.order)

            # Validate transition targets
            for decision, target in step.transitions.items():
                if target not in step_ids and target not in SPECIAL_TARGETS:
                    raise ValueError(
                        f"Step '{step.id}' transition '{decision}' targets "
                        f"unknown step '{target}'"
                    )

                # reject_to_* must always be a rollback to an earlier stage.
                if decision.startswith("reject_to_"):
                    if target not in step_ids:
                        raise ValueError(
                            f"Step '{step.id}' transition '{decision}' must target a workflow step, "
                            f"not special target '{target}'"
                        )
                    target_step = step_by_id[target]
                    if target_step.id == step.id:
                        raise ValueError(
                            f"Step '{step.id}' transition '{decision}' cannot target itself"
                        )
                    if target_step.order >= step.order:
                        raise ValueError(
                            f"Step '{step.id}' transition '{decision}' must target an earlier step. "
                            f"Got order {target_step.order} >= {step.order}"
                        )

            # Revision steps must have return_to pointing to an earlier step
            if step.type == "revision":
                if not step.return_to:
                    raise ValueError(
                        f"Revision step '{step.id}' must have 'return_to'"
                    )
                if step.return_to not in step_ids:
                    raise ValueError(
                        f"Revision step '{step.id}' return_to targets "
                        f"unknown step '{step.return_to}'"
                    )
                return_target = step_by_id[step.return_to]
                if return_target.order >= step.order:
                    raise ValueError(
                        f"Revision step '{step.id}' return_to must target an earlier step. "
                        f"Got order {return_target.order} >= {step.order}"
                    )

            # Review/delivery steps with revision_step must reference a valid revision step
            if step.revision_step:
                if step.revision_step not in step_ids:
                    raise ValueError(
                        f"Step '{step.id}' revision_step targets "
                        f"unknown step '{step.revision_step}'"
                    )
                target_step = next(s for s in self.steps if s.id == step.revision_step)
                if target_step.type != "revision":
                    raise ValueError(
                        f"Step '{step.id}' revision_step '{step.revision_step}' "
                        f"must be of type 'revision'"
                    )
            if step.revision_decision_policy == "first_revision_request":
                if step.type != "review":
                    raise ValueError(
                        f"Step '{step.id}' can only use revision_decision_policy "
                        "on review steps"
                    )
                if not step.revision_step:
                    raise ValueError(
                        f"Step '{step.id}' first_revision_request policy requires "
                        "a revision_step"
                    )

        # Forward transitions must not target steps with a lower order.
        # The runtime engine silently hides these (workflow_engine.py
        # get_allowed_transitions), but catching them at save time prevents
        # workflows where a step appears to have an action but can never
        # advance.
        for step in self.steps:
            for decision, target in step.transitions.items():
                if decision.startswith("reject_to_"):
                    continue  # already validated above
                if target in SPECIAL_TARGETS:
                    continue
                target_step = step_by_id.get(target)
                if target_step and target_step.order < step.order:
                    raise ValueError(
                        f"Step '{step.id}' transition '{decision}' targets "
                        f"'{target}' which has a lower order ({target_step.order} < {step.order}). "
                        f"Forward transitions must not go backward — use a 'reject_to_' prefix "
                        f"for rollback transitions."
                    )

        # final_review uses the dedicated /final-review/approve endpoint
        # which sets status directly to "completed", skipping any
        # subsequent steps.  Ensure it is the last non-revision step.
        sorted_steps = sorted(self.steps, key=lambda s: s.order)
        # Collect non-revision steps in order — these are the "main" stages.
        main_steps = [s for s in sorted_steps if s.type != "revision"]
        for step in main_steps:
            is_final_review = step.ui_variant == "final_review" or step.id == "final_review"
            if not is_final_review:
                continue
            later_main = [s for s in main_steps if s.order > step.order]
            if later_main:
                later_labels = ", ".join(f"'{s.id}'" for s in later_main)
                raise ValueError(
                    f"Step '{step.id}' (final_review) must be the last main stage "
                    f"because it completes the track via dual-approval. "
                    f"The following stages would be unreachable: {later_labels}"
                )

        # final_review must have at least one rollback path (reject_to_*
        # transition or a revision step) so it is not a dead end when the
        # reviewer finds issues.
        for step in self.steps:
            is_final_review = step.ui_variant == "final_review" or step.id == "final_review"
            if not is_final_review:
                continue
            has_rollback = any(
                decision.startswith("reject_to_") for decision in step.transitions
            )
            has_revision = bool(step.revision_step)
            if not has_rollback and not has_revision:
                raise ValueError(
                    f"Step '{step.id}' (final_review) has no rollback path. "
                    f"Add a 'reject_to_*' transition or a revision step so "
                    f"reviewers can return the track when issues are found."
                )

        # Reachability: every non-revision step must be reachable from the
        # first step via forward transitions (or as a return_to target of a
        # reachable revision step).  Unreachable steps are dead config that
        # will never execute.
        first_step = min(self.steps, key=lambda s: s.order)
        reachable: set[str] = {first_step.id}
        queue = [first_step.id]
        while queue:
            current_id = queue.pop()
            current = step_by_id[current_id]
            for target in current.transitions.values():
                if target in SPECIAL_TARGETS:
                    continue
                if target not in reachable:
                    reachable.add(target)
                    queue.append(target)
            # Revision steps are also reachable from their parent via
            # revision_step reference
            if current.revision_step and current.revision_step not in reachable:
                reachable.add(current.revision_step)
                queue.append(current.revision_step)
            # A revision step's return_to makes its target reachable
            if current.type == "revision" and current.return_to and current.return_to not in reachable:
                reachable.add(current.return_to)
                queue.append(current.return_to)

        unreachable = step_ids - reachable
        if unreachable:
            labels = ", ".join(f"'{sid}'" for sid in sorted(unreachable))
            raise ValueError(
                f"The following steps are unreachable from the first step: {labels}. "
                f"Ensure every step is connected via transitions."
            )

        # Completion path requirement: either a transition to __completed
        # (generic engine path) or a final_review step (which completes via
        # the dedicated /final-review/approve dual-confirmation endpoint).
        has_completed_transition = any(
            "__completed" in step.transitions.values() for step in self.steps
        )
        has_final_review_step = any(
            step.ui_variant == "final_review" or step.id == "final_review"
            for step in self.steps
        )
        if not (has_completed_transition or has_final_review_step):
            raise ValueError(
                "Workflow must have at least one path to completion "
                "(either a transition to '__completed' or a 'final_review' step)"
            )

        return self


class StageAssignmentRead(BaseModel):
    id: int
    track_id: int
    stage_id: str
    user_id: int
    status: str
    decision: str | None = None
    cancellation_reason: str | None = None
    assigned_at: datetime
    completed_at: datetime | None = None
    user: UserRead | None = None

    model_config = ConfigDict(from_attributes=True)


class ReviewerCandidateRead(BaseModel):
    user_id: int
    user: UserRead


class AssignReviewerRequest(BaseModel):
    user_ids: list[int] = Field(..., min_length=1)


class ReassignReviewerRequest(BaseModel):
    user_ids: list[int] | None = None
    user_id: int | None = None


class ReopenRequestCreate(BaseModel):
    target_stage_id: str = Field(..., min_length=1, max_length=50)
    reason: str = Field(..., min_length=1, max_length=2000)
    mastering_notes: str | None = Field(default=None, max_length=5000)


class DirectReopenRequest(BaseModel):
    target_stage_id: str = Field(..., min_length=1, max_length=50)
    mastering_notes: str | None = Field(default=None, max_length=5000)


class ReopenDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]


class ReopenRequestRead(BaseModel):
    id: int
    track_id: int
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

    model_config = ConfigDict(from_attributes=True)


class SourceFollowupDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    target_stage_id: str | None = Field(default=None, max_length=50)


class WorkflowTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    workflow_config: WorkflowConfigSchema


class WorkflowTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    workflow_config: WorkflowConfigSchema | None = None


class WorkflowTemplateRead(BaseModel):
    id: int
    circle_id: int
    name: str
    description: str | None = None
    workflow_config: WorkflowConfigSchema
    created_by: int
    created_by_user: UserRead | None = None
    album_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
