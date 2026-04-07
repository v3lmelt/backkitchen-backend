"""Default workflow configuration matching the legacy hardcoded state machine.

When an album has ``workflow_config = NULL`` the system falls back to the
legacy hard-coded endpoints.  This constant is used:

* As a template for the workflow builder UI (frontend).
* To populate ``AlbumRead.workflow_config`` for legacy albums so the
  frontend can render the progress bar from a single source of truth.
"""

STEP_TYPES = ("gate", "review", "revision", "delivery")

SPECIAL_TARGETS = {"__completed", "__rejected", "__rejected_resubmittable"}

ASSIGNEE_ROLES = {"producer", "mastering_engineer", "peer_reviewer", "submitter"}

DEFAULT_WORKFLOW_CONFIG: dict = {
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
