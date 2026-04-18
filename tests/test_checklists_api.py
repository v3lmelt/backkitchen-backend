import json

from app.models.stage_assignment import StageAssignment


def test_submit_checklist_replaces_existing_items_for_current_source_version(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
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


def test_submit_checklist_rejects_when_album_checklist_disabled(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer],
        checklist_enabled=False,
    )
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
        peer_reviewer=reviewer,
    )

    response = client.post(
        f"/api/tracks/{track.id}/checklist",
        headers=auth_headers(reviewer),
        json={"items": [{"label": "Balance", "passed": True}]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Checklist is disabled for this album."


def test_get_checklist_only_returns_current_source_version_items(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
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


def test_get_checklist_supports_history_filters(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer_a = factory.user(username="reviewer-a")
    reviewer_b = factory.user(username="reviewer-b")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer_a, reviewer_b])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
        peer_reviewer=reviewer_a,
        version=2,
    )
    prior_version = factory.source_version(track=track, uploaded_by=submitter, version_number=1)
    current_version = track.source_versions[-1]
    factory.checklist(track=track, reviewer=reviewer_a, source_version_id=prior_version.id, label="Prior A")
    factory.checklist(track=track, reviewer=reviewer_b, source_version_id=prior_version.id, label="Prior B")
    factory.checklist(track=track, reviewer=reviewer_a, source_version_id=current_version.id, label="Current A")

    response = client.get(
        f"/api/tracks/{track.id}/checklist",
        headers=auth_headers(reviewer_a),
        params={"source_version_id": prior_version.id, "reviewer_id": reviewer_b.id},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["label"] for item in body] == ["Prior B"]
    assert {item["reviewer_id"] for item in body} == {reviewer_b.id}
    assert {item["source_version_id"] for item in body} == {prior_version.id}


def test_get_checklist_draft_prefers_current_users_current_version_submission(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)
    current_version = track.source_versions[-1]
    factory.checklist(track=track, reviewer=reviewer, source_version_id=current_version.id, label="Balance", passed=True)

    response = client.get(f"/api/tracks/{track.id}/checklist/draft", headers=auth_headers(reviewer))

    assert response.status_code == 200
    body = response.json()
    assert body["prefilled_from_current_version"] is True
    assert body["current_source_version_id"] == current_version.id
    assert body["prefilled_from_source_version_id"] == current_version.id
    assert [item["label"] for item in body["items"]] == ["Balance"]


def test_get_checklist_draft_returns_empty_when_album_checklist_disabled(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer],
        checklist_enabled=False,
    )
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
        peer_reviewer=reviewer,
    )

    response = client.get(f"/api/tracks/{track.id}/checklist/draft", headers=auth_headers(reviewer))

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "current_source_version_id": None,
        "current_source_version_number": None,
        "prefilled_from_source_version_id": None,
        "prefilled_from_source_version_number": None,
        "prefilled_from_current_version": False,
    }


def test_get_checklist_draft_falls_back_to_latest_prior_submission_in_same_cycle(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
        peer_reviewer=reviewer,
        version=3,
    )
    version_one = factory.source_version(track=track, uploaded_by=submitter, version_number=1)
    version_two = factory.source_version(track=track, uploaded_by=submitter, version_number=2)
    current_version = track.source_versions[-1]
    factory.checklist(track=track, reviewer=reviewer, source_version_id=version_one.id, label="Old")
    factory.checklist(track=track, reviewer=reviewer, source_version_id=version_two.id, label="Latest prior")

    response = client.get(f"/api/tracks/{track.id}/checklist/draft", headers=auth_headers(reviewer))

    assert response.status_code == 200
    body = response.json()
    assert body["prefilled_from_current_version"] is False
    assert body["current_source_version_id"] == current_version.id
    assert body["prefilled_from_source_version_id"] == version_two.id
    assert [item["label"] for item in body["items"]] == ["Latest prior"]


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
        status="peer_review",
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
    track = factory.track(album=album, submitter=submitter, status="submitted", peer_reviewer=None)

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
    track = factory.track(album=album, submitter=submitter, status="submitted", peer_reviewer=None)

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
