import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import select

from app.models.circle import Circle, CircleMember
from app.models.stage_assignment import StageAssignment
from app.models.track import Track
from app.models.workflow_event import WorkflowEvent
from app.workflow_engine import prepare_review_assignments_for_stage_entry


@pytest.fixture
def review_case(factory, db_session):
    producer = factory.user(role="producer")
    author = factory.user()
    reviewers = [factory.user() for _ in range(3)]
    album = factory.album(producer=producer, mastering_engineer=producer,
                          members=[author, *reviewers], checklist_enabled=False)
    track = factory.track(album=album, submitter=author, status="peer_review")
    assignments = [StageAssignment(track_id=track.id, stage_id="peer_review", user_id=u.id,
                                   status="pending") for u in reviewers[:2]]
    db_session.add_all(assignments)
    db_session.commit()
    return producer, author, reviewers, album, track, assignments


def snapshot(client, auth_headers, producer, track):
    result = client.get(f"/api/tracks/{track.id}", headers=auth_headers(producer))
    assert result.status_code == 200, result.text
    return result.json()["track"]["review_state"]


def manage(client, auth_headers, producer, track, ids, state=None):
    state = state or snapshot(client, auth_headers, producer, track)
    return client.put(f"/api/tracks/{track.id}/review-management", headers=auth_headers(producer),
                      json={"stage_id": state["step_id"], "user_ids": ids,
                            "flexible": True, "state_version": state["state_version"]})


def submit(client, auth_headers, track, user, decision="pass"):
    return client.post(f"/api/tracks/{track.id}/workflow/transition", headers=auth_headers(user),
                       json={"decision": decision})


@pytest.mark.parametrize("decision,target", [("pass", "producer_gate"), ("needs_revision", "peer_revision")])
def test_all_reviewers_finish_before_automatic_result(client, auth_headers, review_case, decision, target):
    producer, _, reviewers, _, track, _ = review_case
    enabled = manage(client, auth_headers, producer, track, [u.id for u in reviewers])
    assert enabled.status_code == 200, enabled.text
    state = enabled.json()["review_state"]
    assert state["flexible"] and state["required_review_count"] == 3
    assert not state["requires_group_finalization"]
    for reviewer, vote in zip(reviewers[:2], [decision, "pass"]):
        result = submit(client, auth_headers, track, reviewer, vote)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "peer_review"
    result = submit(client, auth_headers, track, reviewers[2])
    assert result.status_code == 200, result.text
    assert result.json()["status"] == target
    assert submit(client, auth_headers, track, reviewers[2]).status_code in (400, 403, 409)


def test_roster_edit_retains_results_and_rejoining_creates_new_task(client, auth_headers, review_case, db_session):
    producer, _, reviewers, _, track, assignments = review_case
    assert manage(client, auth_headers, producer, track, [u.id for u in reviewers]).status_code == 200
    assert submit(client, auth_headers, track, reviewers[0], "needs_revision").status_code == 200
    removed = manage(client, auth_headers, producer, track, [u.id for u in reviewers[1:]])
    assert removed.status_code == 200
    db_session.refresh(assignments[0])
    assert assignments[0].status == "cancelled"
    assert assignments[0].decision == "needs_revision"
    assert assignments[0].completed_at is not None
    assert manage(client, auth_headers, producer, track, [u.id for u in reviewers]).status_code == 200
    active = db_session.scalars(select(StageAssignment).where(
        StageAssignment.track_id == track.id, StageAssignment.user_id == reviewers[0].id,
        StageAssignment.status == "pending")).one()
    assert active.id != assignments[0].id and active.decision is None


@pytest.mark.parametrize("remove_revision,target", [(False, "peer_revision"), (True, "producer_gate")])
def test_removals_can_immediately_finish_and_exclude_old_votes(client, auth_headers, review_case, remove_revision, target):
    producer, _, reviewers, _, track, _ = review_case
    assert manage(client, auth_headers, producer, track, [u.id for u in reviewers]).status_code == 200
    assert submit(client, auth_headers, track, reviewers[0], "needs_revision").status_code == 200
    assert submit(client, auth_headers, track, reviewers[1]).status_code == 200
    ids = [reviewers[1].id] if remove_revision else [u.id for u in reviewers[:2]]
    result = manage(client, auth_headers, producer, track, ids)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == target


def test_old_snapshot_rejected_after_submission(client, auth_headers, review_case):
    producer, _, reviewers, _, track, _ = review_case
    assert manage(client, auth_headers, producer, track, [u.id for u in reviewers]).status_code == 200
    old = snapshot(client, auth_headers, producer, track)
    assert submit(client, auth_headers, track, reviewers[0]).status_code == 200
    result = manage(client, auth_headers, producer, track, [reviewers[0].id], old)
    assert result.status_code == 409
    assert snapshot(client, auth_headers, producer, track)["active_assignment_count"] == 3


def test_validation_permissions_and_legacy_bypass(client, auth_headers, review_case, factory, db_session):
    producer, author, reviewers, album, track, _ = review_case
    state = snapshot(client, auth_headers, producer, track)
    assert manage(client, auth_headers, author, track, [reviewers[0].id], state).status_code == 403
    assert manage(client, auth_headers, producer, track, [], state).status_code == 422
    assert manage(client, auth_headers, producer, track, [author.id], state).status_code == 400
    outsider = factory.user()
    assert manage(client, auth_headers, producer, track, [outsider.id], state).status_code == 400
    assert manage(client, auth_headers, producer, track, [u.id for u in reviewers], state).status_code == 200
    for endpoint in ("assign-reviewer", "reassign-reviewer"):
        result = client.post(f"/api/tracks/{track.id}/{endpoint}", headers=auth_headers(producer),
                             json={"user_ids": [reviewers[0].id]})
        assert result.status_code == 409, result.text
    album.archived_at = datetime.now(timezone.utc)
    db_session.commit()
    assert manage(client, auth_headers, producer, track, [reviewers[0].id]).status_code == 409


def test_flexible_reentry_preserves_history_and_uses_saved_roster(client, auth_headers, review_case, db_session):
    producer, _, reviewers, album, track, assignments = review_case
    ids = [u.id for u in reviewers[:2]]
    assert manage(client, auth_headers, producer, track, ids).status_code == 200
    assert submit(client, auth_headers, track, reviewers[0], "needs_revision").status_code == 200
    assert submit(client, auth_headers, track, reviewers[1]).json()["status"] == "peer_revision"
    db_session.refresh(track)
    track.status = "peer_review"
    prepare_review_assignments_for_stage_entry(db_session, album, track, "peer_review", BackgroundTasks(), producer)
    db_session.commit()
    for original in assignments:
        db_session.refresh(original)
        assert original.status == "cancelled" and original.completed_at is not None
    active = db_session.scalars(select(StageAssignment).where(StageAssignment.track_id == track.id,
                                                             StageAssignment.status == "pending")).all()
    assert {a.user_id for a in active} == set(ids)
    assert all(a.decision is None for a in active)
    assert snapshot(client, auth_headers, producer, track)["flexible"]


def test_existing_review_keeps_group_finalization(client, auth_headers, review_case, db_session):
    producer, _, reviewers, album, track, _ = review_case
    config = json.loads(album.workflow_config)
    next(s for s in config["steps"] if s["id"] == "peer_review")["required_reviewer_count"] = 2
    album.workflow_config = json.dumps(config)
    db_session.commit()
    assert not snapshot(client, auth_headers, producer, track)["flexible"]
    assert submit(client, auth_headers, track, reviewers[0]).json()["status"] == "peer_review"
    result = submit(client, auth_headers, track, reviewers[1])
    assert result.json()["status"] == "peer_review"
    assert result.json()["review_state"]["requires_group_finalization"]


def test_flexible_review_disables_early_revision_and_keeps_anonymous_history(client, auth_headers, review_case, db_session):
    producer, author, reviewers, album, track, _ = review_case
    config = json.loads(album.workflow_config)
    next(s for s in config["steps"] if s["id"] == "peer_review")["revision_decision_policy"] = "first_revision_request"
    album.workflow_config = json.dumps(config)
    db_session.commit()
    assert manage(client, auth_headers, producer, track, [u.id for u in reviewers[:2]]).status_code == 200
    response = submit(client, auth_headers, track, reviewers[0], "needs_revision")
    assert response.status_code == 200
    assert response.json()["status"] == "peer_review"
    assert not any(t["decision"] == "request_revision_now" for t in (response.json()["workflow_transitions"] or []))
    assert submit(client, auth_headers, track, reviewers[0], "request_revision_now").status_code in (400, 403)
    detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(author)).json()
    event = next(e for e in detail["events"] if e["event_type"] == "review_roster_updated")
    assert "user_ids" not in event["payload"]


def test_workflow_edit_invalidates_roster_snapshot(client, auth_headers, review_case, db_session):
    producer, _, reviewers, album, track, _ = review_case
    old = snapshot(client, auth_headers, producer, track)
    config = json.loads(album.workflow_config)
    next(s for s in config["steps"] if s["id"] == "peer_review")["label"] = "Updated review"
    album.workflow_config = json.dumps(config)
    db_session.commit()
    assert manage(client, auth_headers, producer, track, [reviewers[0].id], old).status_code == 409


def test_custom_ambiguous_outcomes_do_not_offer_flexible_mode(client, auth_headers, review_case, db_session):
    producer, _, reviewers, album, track, _ = review_case
    config = json.loads(album.workflow_config)
    next(s for s in config["steps"] if s["id"] == "peer_review")["transitions"]["reject"] = "__rejected"
    album.workflow_config = json.dumps(config)
    db_session.commit()
    state = snapshot(client, auth_headers, producer, track)
    assert not state["flexible_available"]
    assert manage(client, auth_headers, producer, track, [reviewers[0].id], state).status_code == 409


def test_roster_save_racing_last_submission_never_double_advances(client, auth_headers, review_case, db_session):
    producer, _, reviewers, _, track, _ = review_case
    assert manage(client, auth_headers, producer, track, [u.id for u in reviewers[:2]]).status_code == 200
    assert submit(client, auth_headers, track, reviewers[0]).status_code == 200
    state = snapshot(client, auth_headers, producer, track)
    barrier = Barrier(2)

    def change_roster():
        barrier.wait()
        return manage(client, auth_headers, producer, track, [u.id for u in reviewers], state)

    def finish_review():
        barrier.wait()
        return submit(client, auth_headers, track, reviewers[1])

    with ThreadPoolExecutor(max_workers=2) as executor:
        change = executor.submit(change_roster)
        finish = executor.submit(finish_review)
        results = [change.result(), finish.result()]
    assert all(r.status_code in (200, 409) for r in results), [r.text for r in results]
    assert any(r.status_code == 200 for r in results)
    db_session.refresh(track)
    transitions = db_session.scalars(select(WorkflowEvent).where(
        WorkflowEvent.track_id == track.id, WorkflowEvent.event_type == "workflow_transition_pass")).all()
    if track.status == "producer_gate":
        assert len(transitions) == 1 and results[0].status_code == 409
    else:
        assert track.status == "peer_review" and not transitions
        assert results[0].status_code == 200


def test_admin_album_scopes_use_real_relationships(client, auth_headers, factory, db_session):
    admin = factory.user(is_admin=True)
    other = factory.user(role="producer")
    mine = factory.album(producer=admin, mastering_engineer=other)
    member = factory.album(producer=other, mastering_engineer=other, members=[admin])
    unrelated = factory.album(producer=other, mastering_engineer=other)
    shared = factory.album(producer=other, mastering_engineer=other)
    circle = Circle(name="Shared", created_by=other.id)
    db_session.add(circle)
    db_session.flush()
    db_session.add(CircleMember(circle_id=circle.id, user_id=admin.id, role="co_producer"))
    shared.circle_id = circle.id
    db_session.commit()

    def albums(**params):
        result = client.get("/api/albums", params=params, headers=auth_headers(admin))
        assert result.status_code == 200, result.text
        return {a["id"] for a in result.json()}

    assert albums() == {mine.id, member.id, shared.id, unrelated.id}
    assert albums(scope="managed") == {mine.id, shared.id}
    assert albums(scope="participating") == {mine.id, member.id, shared.id}
    assert albums(scope="managed", search=mine.title) == {mine.id}
    mine.archived_at = datetime.now(timezone.utc)
    db_session.commit()
    assert albums(scope="managed", archived_only=True) == {mine.id}
    assert albums(scope="managed") == {shared.id}


def test_participating_scope_includes_authors_mastering_and_current_reviewers(client, auth_headers, factory, db_session):
    admin = factory.user(is_admin=True)
    owner = factory.user(role="producer")
    composed = factory.album(producer=owner, mastering_engineer=owner)
    reviewed = factory.album(producer=owner, mastering_engineer=owner)
    old_review = factory.album(producer=owner, mastering_engineer=owner)
    mastered = factory.album(producer=owner, mastering_engineer=admin)
    factory.track(album=composed, submitter=owner, composers=[admin])
    current = factory.track(album=reviewed, submitter=owner, status="peer_review")
    former = factory.track(album=old_review, submitter=owner, status="producer_gate")
    db_session.add_all([
        StageAssignment(track_id=current.id, stage_id="peer_review", user_id=admin.id, status="pending"),
        StageAssignment(track_id=former.id, stage_id="peer_review", user_id=admin.id, status="completed"),
    ])
    db_session.commit()
    result = client.get("/api/albums?scope=participating", headers=auth_headers(admin))
    assert result.status_code == 200, result.text
    assert {a["id"] for a in result.json()} == {composed.id, reviewed.id, mastered.id}


def test_flexible_roster_notifies_only_members_added_or_removed(client, auth_headers, review_case, db_session):
    from app.models.notification import Notification

    producer, _, reviewers, _, track, _ = review_case
    assert manage(client, auth_headers, producer, track, [u.id for u in reviewers]).status_code == 200
    assigned = db_session.scalars(select(Notification).where(Notification.type == "reviewer_assigned")).all()
    assert [n.user_id for n in assigned] == [reviewers[2].id]
    assert manage(client, auth_headers, producer, track, [u.id for u in reviewers[1:]]).status_code == 200
    removed = db_session.scalars(select(Notification).where(Notification.type == "reviewer_reassigned")).all()
    assert [n.user_id for n in removed] == [reviewers[0].id]
    assert db_session.scalar(select(Track.status).where(Track.id == track.id)) == "peer_review"
