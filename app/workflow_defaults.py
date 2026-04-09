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
* **v2** – renamed ``gate`` → ``approval``; added ``producer_revision``,
  ``final_revision`` stages; new per-step config fields.
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
            "assignee_role": "producer",
            "order": 0,
            "transitions": {
                "accept": "peer_review",
                "reject_final": "__rejected",
                "reject_resubmittable": "__rejected_resubmittable",
            },
            "allow_permanent_reject": True,
        },
        {
            "id": "peer_review",
            "label": "Peer Review",
            "type": "review",
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
            "assignee_role": "producer",
            "order": 3,
            "transitions": {
                "approve": "mastering",
                "reject": "producer_revision",
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
            "assignee_role": "mastering_engineer",
            "order": 5,
            "transitions": {
                "deliver": "final_review",
                "request_revision": "mastering_revision",
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
            "assignee_role": "producer",
            "order": 7,
            "transitions": {
                "approve": "__completed",
                "reject": "final_revision",
            },
            "allow_permanent_reject": True,
        },
        {
            "id": "final_revision",
            "label": "Final Revision",
            "type": "revision",
            "assignee_role": "mastering_engineer",
            "order": 8,
            "return_to": "final_review",
            "transitions": {},
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
