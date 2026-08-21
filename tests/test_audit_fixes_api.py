"""API-level regression tests for the workflow/API boundary audit fixes.

These cover:
- request_return_in_final_review no longer 500s on config-driven albums
- approve_final_review accepts a custom workflow identified by step id alone
- confirm_delivery rejects stale deliveries in the current workflow cycle
- delivery -> review transitions auto-assign reviewers
- the generic workflow/transition endpoint rejects unauthorized actors
"""

from sqlalchemy import select

from app.models.stage_assignment import StageAssignment


def test_final_review_request_return_works_for_config_driven_album(client, db_session, factory, auth_headers):
    """request_return must not crash when the album's workflow_config is a
    JSON string (it is parsed by the engine, not passed through raw)."""
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter],
        workflow_config={
            "version": 2,
            "steps": [
                {
                    "id": "intake",
                    "label": "Intake",
                    "type": "approval",
                    "ui_variant": "intake",
                    "assignee_role": "producer",
                    "order": 0,
                    "transitions": {"accept": "final_review"},
                },
                {
                    "id": "final_review",
                    "label": "Final Review",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 1,
                    "transitions": {"approve": "__completed"},
                },
            ],
        },
    )
    track = factory.track(album=album, submitter=submitter, status="final_review")

    response = client.post(
        f"/api/tracks/{track.id}/final-review/request-return",
        headers=auth_headers(submitter),
    )

    assert response.status_code == 204, response.text


def test_approve_final_review_works_for_custom_step_without_ui_variant(client, db_session, factory, auth_headers):
    """approve_final_review must accept a custom workflow whose final review
    step is identified by id alone (no ui_variant set)."""
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter],
        workflow_config={
            "version": 2,
            "steps": [
                {
                    "id": "intake",
                    "label": "Intake",
                    "type": "approval",
                    "ui_variant": "intake",
                    "assignee_role": "producer",
                    "order": 0,
                    "transitions": {"accept": "final_review"},
                },
                {
                    "id": "final_review",
                    "label": "Final Review",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 1,
                    "transitions": {"approve": "__completed"},
                },
            ],
        },
    )
    track = factory.track(album=album, submitter=submitter, status="final_review")
    factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/final-review/approve",
        headers=auth_headers(producer),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "final_review"


def test_confirm_delivery_rejects_stale_delivery_in_current_cycle(client, db_session, factory, auth_headers):
    """A mastering engineer must not be able to confirm an old delivery that
    is not the latest one in the current workflow cycle."""
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")
    delivery1 = factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)
    delivery2 = factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=2)
    db_session.commit()

    stale = client.post(
        f"/api/tracks/{track.id}/master-deliveries/{delivery1.id}/confirm",
        headers=auth_headers(mastering),
    )
    assert stale.status_code == 409
    assert "latest" in stale.text

    # The latest delivery can still be confirmed.
    latest = client.post(
        f"/api/tracks/{track.id}/master-deliveries/{delivery2.id}/confirm",
        headers=auth_headers(mastering),
    )
    assert latest.status_code == 200, latest.text
    assert latest.json()["status"] == "final_review"


def test_upload_master_delivery_to_review_step_auto_assigns_reviewers(client, db_session, factory, auth_headers):
    """Advancing out of a delivery step into a review step must auto-assign
    reviewers so the review stage does not stall."""
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer],
        workflow_config={
            "version": 2,
            "steps": [
                {
                    "id": "intake",
                    "label": "Intake",
                    "type": "approval",
                    "ui_variant": "intake",
                    "assignee_role": "producer",
                    "order": 0,
                    "transitions": {"accept": "custom_delivery"},
                },
                {
                    "id": "custom_delivery",
                    "label": "Custom Delivery",
                    "type": "delivery",
                    "ui_variant": "generic",
                    "assignee_role": "mastering_engineer",
                    "order": 1,
                    "transitions": {"deliver": "custom_review"},
                    "require_confirmation": False,
                },
                {
                    "id": "custom_review",
                    "label": "Custom Review",
                    "type": "review",
                    "ui_variant": "generic",
                    "assignee_role": "peer_reviewer",
                    "order": 2,
                    "transitions": {"pass": "__completed"},
                    "assignment_mode": "auto",
                    "reviewer_pool": [reviewer.id],
                    "required_reviewer_count": 1,
                },
            ],
        },
    )
    track = factory.track(album=album, submitter=submitter, status="custom_delivery")

    response = client.post(
        f"/api/tracks/{track.id}/master-deliveries",
        headers=auth_headers(mastering),
        files={"file": ("master.mp3", b"ID3master", "audio/mpeg")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "custom_review"
    assignments = db_session.scalars(
        select(StageAssignment).where(StageAssignment.track_id == track.id)
    ).all()
    assert len(assignments) == 1
    assert assignments[0].user_id == reviewer.id
    assert assignments[0].status == "pending"


def test_workflow_transition_rejects_unauthorized_album_member(client, db_session, factory, auth_headers):
    """The generic workflow transition endpoint must reject a user who is not
    entitled to act on the current step (unified can_act pre-check)."""
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    outsider = factory.user(username="outsider")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, outsider],
    )
    track = factory.track(album=album, submitter=submitter, status="intake")

    response = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(outsider),
        json={"decision": "accept"},
    )

    assert response.status_code == 403
    db_session.refresh(track)
    assert track.status == "intake"
