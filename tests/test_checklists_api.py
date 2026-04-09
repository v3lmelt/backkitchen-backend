import json

from app.models.stage_assignment import StageAssignment
from app.models.track import TrackStatus


def test_submit_checklist_replaces_existing_items_for_current_source_version(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.PEER_REVIEW,
        peer_reviewer=reviewer,
    )
    source_version = track.source_versions[-1]
    factory.checklist(track=track, reviewer=reviewer, source_version_id=source_version.id, label="Balance", passed=False)

    response = client.post(
        f"/api/tracks/{track.id}/checklist",
        headers=auth_headers(reviewer),
        json={"items": [{"label": "Balance", "passed": True, "note": "fixed"}]},
    )

    assert response.status_code == 201
    items = db_session.get(type(track), track.id).checklist_items
    assert len(items) == 1
    assert items[0].passed is True
    assert items[0].note == "fixed"


def test_get_checklist_only_returns_current_source_version_items(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.PEER_REVIEW,
        peer_reviewer=reviewer,
        version=2,
    )
    old_version = factory.source_version(track=track, uploaded_by=submitter, version_number=1)
    current_version = track.source_versions[-1]
    factory.checklist(track=track, reviewer=reviewer, source_version_id=old_version.id, label="Arrangement")
    factory.checklist(track=track, reviewer=reviewer, source_version_id=current_version.id, label="Balance")

    response = client.get(f"/api/tracks/{track.id}/checklist", headers=auth_headers(reviewer))

    assert response.status_code == 200
    assert [item["label"] for item in response.json()] == ["Balance"]


def test_submit_checklist_requires_assigned_reviewer(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer, outsider])
    track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.PEER_REVIEW,
        peer_reviewer=reviewer,
    )

    response = client.post(
        f"/api/tracks/{track.id}/checklist",
        headers=auth_headers(outsider),
        json={"items": [{"label": "Balance", "passed": True}]},
    )

    assert response.status_code == 403


def test_submit_checklist_custom_review_allows_stage_assignment_reviewer(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.SUBMITTED, peer_reviewer=None)

    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "custom_review",
                    "label": "Custom Review",
                    "type": "review",
                    "ui_variant": "generic",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "transitions": {"pass": "final_gate"},
                    "assignment_mode": "manual",
                    "required_reviewer_count": 1,
                },
                {
                    "id": "final_gate",
                    "label": "Final Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 1,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    track.status = "custom_review"
    db_session.add(
        StageAssignment(
            track_id=track.id,
            stage_id="custom_review",
            user_id=reviewer.id,
            status="pending",
        )
    )
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/checklist",
        headers=auth_headers(reviewer),
        json={"items": [{"label": "Balance", "passed": True}]},
    )

    assert response.status_code == 201


def test_submit_checklist_custom_review_rejects_unassigned_member(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer, outsider])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.SUBMITTED, peer_reviewer=None)

    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "custom_review",
                    "label": "Custom Review",
                    "type": "review",
                    "ui_variant": "generic",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "transitions": {"pass": "final_gate"},
                    "assignment_mode": "manual",
                    "required_reviewer_count": 1,
                },
                {
                    "id": "final_gate",
                    "label": "Final Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 1,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    track.status = "custom_review"
    db_session.add(
        StageAssignment(
            track_id=track.id,
            stage_id="custom_review",
            user_id=reviewer.id,
            status="pending",
        )
    )
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/checklist",
        headers=auth_headers(outsider),
        json={"items": [{"label": "Balance", "passed": True}]},
    )

    assert response.status_code == 403
