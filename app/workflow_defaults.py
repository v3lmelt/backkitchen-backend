"""Default workflow configuration for new albums.

When an album has ``workflow_config = NULL`` the system falls back to the
legacy hard-coded endpoints.  This constant is used:

* As the default workflow for newly created albums.
* As a template for the workflow builder UI (frontend).
* To populate ``AlbumRead.workflow_config`` for legacy albums so the
  frontend can render the progress bar from a single source of truth.

Version history
---------------
* **v1** – original gate-based config (legacy, still accepted for parsing).
* **v2** – renamed ``gate`` → ``approval``; added ``producer_revision``
  stage; new per-step config fields.  Originally shipped with a broken
  ``final_revision`` stage (assigned to mastering_engineer but uploaded
  via ``/source-versions``); this has been removed.  Backward
  ``reject_to_*`` transitions were added to ``producer_gate``,
  ``mastering`` and ``final_review`` so each approval stage can send
  the track back to an earlier stage for re-review without requiring
  a brand-new source upload.
"""

STEP_TYPES = ("approval", "review", "revision", "delivery")

# Legacy alias so that v1 configs still validate
STEP_TYPE_ALIASES = {"gate": "approval"}

SPECIAL_TARGETS = {"__completed", "__rejected", "__rejected_resubmittable"}

ASSIGNEE_ROLES = {"producer", "mastering_engineer", "peer_reviewer", "submitter"}

ASSIGNMENT_MODES = ("manual", "auto")

DEFAULT_WORKFLOW_CONFIG: dict = {
    "version": 2,
    "steps": [
        {
            "id": "intake",
            "label": "Intake",
            "type": "approval",
            "ui_variant": "intake",
            "assignee_role": "producer",
            "order": 0,
            "transitions": {
                "accept": "peer_review",
                "accept_producer_direct": "producer_gate",
                "reject_final": "__rejected",
                "reject_resubmittable": "__rejected_resubmittable",
            },
            "allow_permanent_reject": True,
        },
        {
            "id": "peer_review",
            "label": "Peer Review",
            "type": "review",
            "ui_variant": "peer_review",
            "assignee_role": "peer_reviewer",
            "order": 1,
            "transitions": {
                "pass": "producer_gate",
                "needs_revision": "peer_revision",
            },
            "revision_step": "peer_revision",
            "assignment_mode": "auto",
            "required_reviewer_count": 1,
        },
        {
            "id": "peer_revision",
            "label": "Peer Revision",
            "type": "revision",
            "assignee_role": "submitter",
            "order": 2,
            "return_to": "peer_review",
            "transitions": {},
        },
        {
            "id": "producer_gate",
            "label": "Producer Review",
            "type": "approval",
            "ui_variant": "producer_gate",
            "assignee_role": "producer",
            "order": 3,
            "transitions": {
                "approve": "mastering",
                "reject": "producer_revision",
                "reject_to_peer_review": "peer_review",
            },
            "allow_permanent_reject": False,
        },
        {
            "id": "producer_revision",
            "label": "Producer Revision",
            "type": "revision",
            "assignee_role": "submitter",
            "order": 4,
            "return_to": "producer_gate",
            "transitions": {},
        },
        {
            "id": "mastering",
            "label": "Mastering",
            "type": "delivery",
            "ui_variant": "mastering",
            "assignee_role": "mastering_engineer",
            "order": 5,
            "transitions": {
                "deliver": "final_review",
                "request_revision": "mastering_revision",
                "reject_to_producer_gate": "producer_gate",
            },
            "revision_step": "mastering_revision",
            "require_confirmation": True,
        },
        {
            "id": "mastering_revision",
            "label": "Mastering Revision",
            "type": "revision",
            "assignee_role": "submitter",
            "order": 6,
            "return_to": "mastering",
            "transitions": {},
        },
        {
            "id": "final_review",
            "label": "Final Review",
            "type": "approval",
            "ui_variant": "final_review",
            "assignee_role": "producer",
            "actor_roles": ["submitter"],
            "order": 7,
            "transitions": {
                "reject_to_mastering": "mastering",
            },
            "allow_permanent_reject": False,
        },
    ],
}

# V1 config (legacy) — kept so that parse_workflow_config can upgrade on the fly.
# Not exported; only used internally by the engine's v1→v2 migration path.
_LEGACY_V1_CONFIG: dict = {
    "version": 1,
    "steps": [
        {
            "id": "submitted",
            "label": "Submitted",
            "type": "gate",
            "assignee_role": "producer",
            "order": 0,
            "transitions": {
                "accept": "peer_review",
                "reject_final": "__rejected",
                "reject_resubmittable": "__rejected_resubmittable",
            },
        },
        {
            "id": "peer_review",
            "label": "Peer Review",
            "type": "review",
            "assignee_role": "peer_reviewer",
            "order": 1,
            "transitions": {
                "pass": "producer_mastering_gate",
                "needs_revision": "peer_revision",
            },
            "revision_step": "peer_revision",
        },
        {
            "id": "peer_revision",
            "label": "Peer Revision",
            "type": "revision",
            "assignee_role": "submitter",
            "order": 2,
            "return_to": "peer_review",
            "transitions": {},
        },
        {
            "id": "producer_mastering_gate",
            "label": "Producer Gate",
            "type": "gate",
            "assignee_role": "producer",
            "order": 3,
            "transitions": {
                "send_to_mastering": "mastering",
                "request_peer_revision": "peer_revision",
            },
        },
        {
            "id": "mastering",
            "label": "Mastering",
            "type": "delivery",
            "assignee_role": "mastering_engineer",
            "order": 4,
            "transitions": {
                "deliver": "final_review",
                "request_revision": "mastering_revision",
            },
            "revision_step": "mastering_revision",
        },
        {
            "id": "mastering_revision",
            "label": "Mastering Revision",
            "type": "revision",
            "assignee_role": "submitter",
            "order": 5,
            "return_to": "mastering",
            "transitions": {},
        },
        {
            "id": "final_review",
            "label": "Final Review",
            "type": "review",
            "assignee_role": "producer",
            "order": 6,
            "transitions": {
                "approve": "__completed",
                "return": "mastering",
            },
        },
    ],
}
