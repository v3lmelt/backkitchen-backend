import copy
from datetime import datetime, timezone

import pytest

from app.models.stage_assignment import StageAssignment
from app.workflow_defaults import DEFAULT_WORKFLOW_CONFIG


def test_optional_actions_do_not_create_work(client, factory, auth_headers):
    producer = factory.user(role="producer")
    engineer = factory.user()
    composer = factory.user()
    member = factory.user()
    album = factory.album(producer=producer, mastering_engineer=engineer, members=[composer, member], quick_followup_enabled=True)
    completed = factory.track(album=album, submitter=composer, status="completed")
    intake = factory.track(album=album, submitter=composer, status="intake")
    for viewer, optional in [(producer, "direct_reopen"), (engineer, "direct_reopen"), (composer, "request_reopen")]:
        rows = client.get("/api/tracks", headers=auth_headers(viewer)).json()
        row = next(row for row in rows if row["id"] == completed.id)
        assert optional in row["allowed_actions"]
        assert row["viewer_has_pending_work"] is False
        assert row["viewer_can_open_workflow"] is False
    own = client.get(f"/api/tracks/{intake.id}", headers=auth_headers(composer)).json()["track"]
    assert own["allowed_actions"] == ["request_source_followup"]
    assert own["viewer_has_pending_work"] is False
    assert own["viewer_can_open_workflow"] is False
    manager = client.get(f"/api/tracks/{intake.id}", headers=auth_headers(producer)).json()["track"]
    assert manager["viewer_has_pending_work"] is True
    assert manager["viewer_can_open_workflow"] is True


@pytest.mark.parametrize("stage,actor,action", [
    ("peer_revision", "composer", "upload_revision"),
    ("peer_review", "producer", "assign_reviewer"),
    ("mastering", "engineer", None),
])
def test_dedicated_stage_work_without_transitions(client, factory, auth_headers, stage, actor, action):
    users = {"producer": factory.user(role="producer"), "engineer": factory.user(), "composer": factory.user()}
    config = copy.deepcopy(DEFAULT_WORKFLOW_CONFIG)
    if stage == "mastering":
        next(step for step in config["steps"] if step["id"] == stage)["transitions"] = {"deliver": "final_review"}
    album = factory.album(producer=users["producer"], mastering_engineer=users["engineer"], members=[users["composer"]], workflow_config=config)
    track = factory.track(album=album, submitter=users["composer"], status=stage)
    data = client.get(f"/api/tracks/{track.id}", headers=auth_headers(users[actor])).json()["track"]
    assert data["workflow_transitions"] is None
    assert data["allowed_actions"] == ([action] if action else [])
    assert data["viewer_has_pending_work"] is True
    assert data["viewer_can_open_workflow"] is True
    outsider = users["engineer"] if actor != "engineer" else users["composer"]
    other = client.get(f"/api/tracks/{track.id}", headers=auth_headers(outsider)).json()["track"]
    assert other["viewer_has_pending_work"] is False
    assert other["viewer_can_open_workflow"] is False


def test_submitted_review_waits_for_quorum_before_becoming_actionable_again(client, db_session, factory, auth_headers):
    producer, engineer, composer, first, second = [factory.user() for _ in range(5)]
    config = copy.deepcopy(DEFAULT_WORKFLOW_CONFIG)
    next(step for step in config["steps"] if step["id"] == "peer_review")["required_reviewer_count"] = 2
    album = factory.album(producer=producer, mastering_engineer=engineer, members=[composer, first, second], workflow_config=config)
    track = factory.track(album=album, submitter=composer, status="peer_review")
    assignments = [StageAssignment(track_id=track.id, stage_id="peer_review", user_id=user.id, status="pending") for user in (first, second)]
    db_session.add_all(assignments)
    db_session.commit()
    before = client.get(f"/api/tracks/{track.id}", headers=auth_headers(first)).json()["track"]
    assert before["viewer_has_pending_work"] is True
    assignments[0].status = "completed"
    assignments[0].decision = "pass"
    db_session.commit()
    waiting = client.get(f"/api/tracks/{track.id}", headers=auth_headers(first)).json()["track"]
    assert waiting["viewer_can_open_workflow"] is False
    assert waiting["viewer_has_pending_work"] is False
    assignments[1].status = "completed"
    assignments[1].decision = "pass"
    db_session.commit()
    ready = client.get(f"/api/tracks/{track.id}", headers=auth_headers(first)).json()["track"]
    assert ready["viewer_has_pending_work"] is True


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_reopen_request_creates_only_manager_work_until_decided(client, factory, auth_headers, decision):
    producer = factory.user(role="producer")
    engineer, composer, member = [factory.user() for _ in range(3)]
    album = factory.album(producer=producer, mastering_engineer=engineer, members=[composer, member])
    track = factory.track(album=album, submitter=composer, status="completed")
    created = client.post(f"/api/tracks/{track.id}/reopen-request", headers=auth_headers(composer), json={"target_stage_id": "mastering", "reason": "Restore dynamics"})
    assert created.status_code == 201
    request_id = created.json()["id"]
    for viewer, pending in [(producer, True), (engineer, False), (composer, False), (member, False)]:
        response = client.get("/api/tracks", headers=auth_headers(viewer))
        assert response.status_code == 200
        row = next(row for row in response.json() if row["id"] == track.id)
        assert row["viewer_has_pending_work"] is pending
        assert row["viewer_can_open_workflow"] is False
        if viewer == member:
            assert row["pending_reopen_request"] is None
        else:
            assert row["pending_reopen_request"]["id"] == request_id
    denied = client.post(f"/api/tracks/reopen-requests/{request_id}/decide", headers=auth_headers(engineer), json={"decision": decision})
    assert denied.status_code == 403
    decided = client.post(f"/api/tracks/reopen-requests/{request_id}/decide", headers=auth_headers(producer), json={"decision": decision})
    assert decided.status_code == 200
    after = client.get(f"/api/tracks/{track.id}", headers=auth_headers(producer)).json()["track"]
    assert after["pending_reopen_request"] is None
    assert after["viewer_has_pending_work"] is False
    engineer_after = client.get(f"/api/tracks/{track.id}", headers=auth_headers(engineer)).json()["track"]
    assert engineer_after["viewer_has_pending_work"] is (decision == "approve")


def test_pending_followup_is_work_for_deciders_only(client, factory, auth_headers):
    producer = factory.user(role="producer")
    engineer, composer = factory.user(), factory.user()
    album = factory.album(producer=producer, mastering_engineer=engineer, members=[composer], quick_followup_enabled=True)
    track = factory.track(album=album, submitter=composer, status="intake")
    created = client.post(f"/api/tracks/{track.id}/source-followups", headers=auth_headers(composer), data={"reason": "Fix clipping"}, files={"file": ("replacement.wav", b"RIFFdemo", "audio/wav")})
    assert created.status_code == 200
    for viewer, pending in [(producer, True), (engineer, True), (composer, False)]:
        row = client.get(f"/api/tracks/{track.id}", headers=auth_headers(viewer)).json()["track"]
        assert row["viewer_has_pending_work"] is pending
        assert row["viewer_can_open_workflow"] is False


@pytest.mark.parametrize("archive_album", [False, True])
def test_archived_work_has_no_pending_task(client, db_session, factory, auth_headers, archive_album):
    producer, engineer, composer = factory.user(role="producer"), factory.user(), factory.user()
    album = factory.album(producer=producer, mastering_engineer=engineer, members=[composer])
    track = factory.track(album=album, submitter=composer, status="intake")
    target = album if archive_album else track
    target.archived_at = datetime.now(timezone.utc)
    db_session.commit()
    row = client.get(f"/api/tracks/{track.id}", headers=auth_headers(producer)).json()["track"]
    assert row["viewer_has_pending_work"] is False
    assert row["viewer_can_open_workflow"] is False


def test_explicit_intake_assignment_remains_authoritative(client, factory, auth_headers):
    producer, engineer, composer = factory.user(role="producer"), factory.user(), factory.user()
    config = copy.deepcopy(DEFAULT_WORKFLOW_CONFIG)
    config["steps"][0]["assignee_user_id"] = composer.id
    album = factory.album(producer=producer, mastering_engineer=engineer, members=[composer], workflow_config=config, quick_followup_enabled=True)
    track = factory.track(album=album, submitter=composer, status="intake")
    for viewer, expected in [(producer, False), (composer, True)]:
        row = client.get(f"/api/tracks/{track.id}", headers=auth_headers(viewer)).json()["track"]
        assert row["viewer_has_pending_work"] is expected
        assert row["viewer_can_open_workflow"] is expected
