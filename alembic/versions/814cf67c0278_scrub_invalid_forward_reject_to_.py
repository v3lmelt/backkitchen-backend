"""scrub invalid forward reject_to transitions

Revision ID: 814cf67c0278
Revises: afc2010d83d9
Create Date: 2026-04-09 17:37:57.581608

Background
----------
Older workflow-editor versions could emit ``reject_to_<stage>`` transitions
whose target was *not* an earlier step (e.g. intake's
``reject_to_peer_review -> peer_review``). The current
``WorkflowConfigSchema`` validator rejects this — reject-to semantics are
strictly a rollback. Any album/template still holding such a legacy entry
crashes ``build_track_detail`` / related read paths.

This migration removes *only* the provably invalid entries: a transition is
dropped when its decision key starts with ``reject_to_`` **and** its target
step has ``order >= source.order``. Everything else — including older
decision names (``approve`` vs ``accept``), unknown fields, etc. — is left
untouched so user-authored configurations are preserved as much as possible.

Downgrade is a no-op: the removed entries were invalid and cannot be
reconstructed.
"""
import json
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '814cf67c0278'
down_revision: Union[str, Sequence[str], None] = 'afc2010d83d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


logger = logging.getLogger("alembic.runtime.migration")


def _scrub_config(raw: str) -> tuple[str | None, list[tuple[str, str, str]]]:
    """Return (new_json, removed) or (None, []) if nothing to change.

    ``removed`` is a list of ``(step_id, decision, target)`` tuples, for
    logging only.
    """
    try:
        config = json.loads(raw)
    except (TypeError, ValueError):
        return None, []

    steps = config.get("steps")
    if not isinstance(steps, list):
        return None, []

    removed: list[tuple[str, str, str]] = []
    for step in steps:
        transitions = step.get("transitions")
        if not isinstance(transitions, dict):
            continue
        source_order = step.get("order", 0)
        step_id = step.get("id", "?")
        for decision, target in list(transitions.items()):
            if not decision.startswith("reject_to_"):
                continue
            target_step = next(
                (s for s in steps if s.get("id") == target),
                None,
            )
            if target_step is None:
                # Unknown target — leave alone; that is a different kind of
                # bug and outside this migration's narrow scope.
                continue
            if target_step.get("order", 0) >= source_order:
                del transitions[decision]
                removed.append((step_id, decision, target))

    if not removed:
        return None, []
    return json.dumps(config, ensure_ascii=False), removed


def upgrade() -> None:
    bind = op.get_bind()

    # Albums
    rows = bind.execute(
        sa.text("SELECT id, workflow_config FROM albums WHERE workflow_config IS NOT NULL")
    ).fetchall()
    for row in rows:
        album_id, raw = row[0], row[1]
        new_json, removed = _scrub_config(raw)
        if new_json is None:
            continue
        for step_id, decision, target in removed:
            logger.info(
                "album %s: dropping invalid transition %s.%s -> %s",
                album_id, step_id, decision, target,
            )
        bind.execute(
            sa.text("UPDATE albums SET workflow_config = :cfg WHERE id = :id"),
            {"cfg": new_json, "id": album_id},
        )

    # Workflow templates
    rows = bind.execute(
        sa.text("SELECT id, workflow_config FROM workflow_templates")
    ).fetchall()
    for row in rows:
        template_id, raw = row[0], row[1]
        new_json, removed = _scrub_config(raw)
        if new_json is None:
            continue
        for step_id, decision, target in removed:
            logger.info(
                "workflow_template %s: dropping invalid transition %s.%s -> %s",
                template_id, step_id, decision, target,
            )
        bind.execute(
            sa.text("UPDATE workflow_templates SET workflow_config = :cfg WHERE id = :id"),
            {"cfg": new_json, "id": template_id},
        )


def downgrade() -> None:
    """The scrubbed entries were invalid and cannot be reconstructed."""
    pass
