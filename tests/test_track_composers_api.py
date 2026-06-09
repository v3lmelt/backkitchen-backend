import copy
from io import BytesIO

from sqlalchemy import select

from app.models.comment import Comment
from app.models.issue import IssuePhase, IssueStatus
from app.models.master_delivery import MasterDelivery
from app.models.notification import Notification
from app.models.stage_assignment import StageAssignment
from app.models.track import TrackStatus
from app.models.track_composer import TrackComposer
from app.workflow_defaults import DEFAULT_WORKFLOW_CONFIG
from app.workflow_engine import assign_reviewers, get_current_step, parse_workflow_config


def _collab_track(factory, *, status: str = "peer_review", quick_followup_enabled: bool = False):
    producer = factory.user(role="producer", username="producer")
    mastering = factory.user(role="mastering_engineer", username="mastering")
    submitter = factory.user(username="primary")
    secondary = factory.user(username="secondary")
    reviewer = factory.user(username="reviewer")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, secondary, reviewer],
        quick_followup_enabled=quick_followup_enabled,
    )
    track = factory.track(
        album=album,
        submitter=submitter,
        composers=[secondary],
        status=status,
        peer_reviewer=reviewer,
    )
    return producer, mastering, submitter, secondary, reviewer, album, track


def test_secondary_composer_can_see_private_track(client, factory, auth_headers):
    _producer, _mastering, submitter, secondary, _reviewer, album, track = _collab_track(factory)

    detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(secondary))
    assert detail.status_code == 200
    detail_track = detail.json()["track"]
    assert set(detail_track["composer_ids"]) == {submitter.id, secondary.id}
    assert {user["id"] for user in detail_track["composers"]} == {submitter.id, secondary.id}

    track_list = client.get(f"/api/tracks?album_id={album.id}", headers=auth_headers(secondary))
    assert track_list.status_code == 200
    assert track.id in {item["id"] for item in track_list.json()}

    album_tracks = client.get(f"/api/albums/{album.id}/tracks", headers=auth_headers(secondary))
    assert album_tracks.status_code == 200
    assert track.id in {item["id"] for item in album_tracks.json()}


def test_secondary_composer_issue_visibility_and_comments(client, db_session, factory, auth_headers):
    _producer, _mastering, _submitter, secondary, reviewer, _album, track = _collab_track(factory)
    public_issue = factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER.value, status=IssueStatus.OPEN)
    hidden_issue = factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER.value,
        status=IssueStatus.PENDING_DISCUSSION,
    )
    db_session.add_all(
        [
            Comment(issue_id=public_issue.id, author_id=reviewer.id, content="public", visibility="public"),
            Comment(issue_id=public_issue.id, author_id=reviewer.id, content="internal", visibility="internal"),
        ]
    )
    db_session.commit()

    listing = client.get(f"/api/tracks/{track.id}/issues", headers=auth_headers(secondary))
    assert listing.status_code == 200
    assert {item["id"] for item in listing.json()} == {public_issue.id}

    hidden_detail = client.get(f"/api/issues/{hidden_issue.id}", headers=auth_headers(secondary))
    assert hidden_detail.status_code == 404

    public_detail = client.get(f"/api/issues/{public_issue.id}", headers=auth_headers(secondary))
    assert public_detail.status_code == 200
    comments = public_detail.json()["comments"]
    assert {comment["content"] for comment in comments} == {"public"}

    created = client.post(
        f"/api/issues/{public_issue.id}/comments",
        headers=auth_headers(secondary),
        data={"content": "secondary reply"},
    )
    assert created.status_code == 201
    assert created.json()["content"] == "secondary reply"


def test_secondary_composer_revision_followup_and_final_approval(client, db_session, factory, auth_headers):
    producer, mastering, submitter, secondary, _reviewer, _album, revision_track = _collab_track(
        factory,
        status="peer_revision",
    )

    revision = client.post(
        f"/api/tracks/{revision_track.id}/source-versions",
        headers=auth_headers(secondary),
        files={"file": ("revision.wav", BytesIO(b"RIFFrevision"), "audio/wav")},
    )
    assert revision.status_code == 200
    assert revision.json()["version"] == 2

    followup_album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, secondary],
        quick_followup_enabled=True,
    )
    followup_track = factory.track(
        album=followup_album,
        submitter=submitter,
        composers=[secondary],
        status="producer_gate",
    )
    requested = client.post(
        f"/api/tracks/{followup_track.id}/source-followups",
        headers=auth_headers(secondary),
        data={"reason": "updated source"},
        files={"file": ("followup.wav", BytesIO(b"RIFFfollowup"), "audio/wav")},
    )
    assert requested.status_code == 200
    body = requested.json()
    assert body["status"] == TrackStatus.SOURCE_FOLLOWUP_PENDING.value
    request_id = body["pending_source_followup_request"]["id"]

    cancelled = client.post(
        f"/api/tracks/source-followups/{request_id}/cancel",
        headers=auth_headers(submitter),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "producer_gate"

    final_track = factory.track(
        album=followup_album,
        submitter=submitter,
        composers=[secondary],
        status=TrackStatus.FINAL_REVIEW.value,
    )
    delivery = factory.master_delivery(track=final_track, uploaded_by=mastering)
    approved = client.post(f"/api/tracks/{final_track.id}/final-review/approve", headers=auth_headers(secondary))
    assert approved.status_code == 200
    db_session.refresh(delivery)
    assert delivery.submitter_approved_at is not None
    assert delivery.producer_approved_at is None


def test_public_issue_notifications_target_all_composers_except_actor(client, db_session, factory, auth_headers):
    _producer, _mastering, submitter, secondary, reviewer, _album, track = _collab_track(factory)

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Timing click",
            "description": "Click at the downbeat.",
            "phase": IssuePhase.PEER.value,
            "severity": "major",
            "markers": [],
            "visibility": "public",
        },
    )
    assert response.status_code == 201
    db_session.expire_all()
    notified_user_ids = set(
        db_session.scalars(
            select(Notification.user_id).where(Notification.type == "new_issue")
        ).all()
    )
    assert notified_user_ids == {submitter.id, secondary.id}


def test_all_composers_are_excluded_from_reviewer_assignment(client, db_session, factory, auth_headers):
    producer, mastering, _submitter, secondary, reviewer, _album, track = _collab_track(factory)

    manual = client.post(
        f"/api/tracks/{track.id}/assign-reviewer",
        headers=auth_headers(producer),
        json={"user_ids": [secondary.id]},
    )
    assert manual.status_code == 400

    fixed_config = copy.deepcopy(DEFAULT_WORKFLOW_CONFIG)
    fixed_step = next(step for step in fixed_config["steps"] if step["id"] == "peer_review")
    fixed_step["assignment_mode"] = "fixed"
    fixed_step["reviewer_pool"] = [secondary.id, reviewer.id]
    fixed_album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[secondary, reviewer],
        workflow_config=fixed_config,
    )
    fixed_track = factory.track(album=fixed_album, submitter=factory.user(username="fixed-primary"), composers=[secondary], status="peer_review")
    fixed_assigned = assign_reviewers(
        db_session,
        fixed_album,
        fixed_track,
        get_current_step(parse_workflow_config(fixed_album), fixed_track),
    )
    assert fixed_assigned == [reviewer.id]

    auto_config = copy.deepcopy(DEFAULT_WORKFLOW_CONFIG)
    auto_step = next(step for step in auto_config["steps"] if step["id"] == "peer_review")
    auto_step["assignment_mode"] = "auto"
    auto_step["reviewer_pool"] = [secondary.id, reviewer.id]
    auto_album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[secondary, reviewer],
        workflow_config=auto_config,
    )
    auto_track = factory.track(album=auto_album, submitter=factory.user(username="auto-primary"), composers=[secondary], status="peer_review")
    auto_assigned = assign_reviewers(
        db_session,
        auto_album,
        auto_track,
        get_current_step(parse_workflow_config(auto_album), auto_track),
    )
    assert auto_assigned == [reviewer.id]
    assert not db_session.scalars(
        select(StageAssignment).where(
            StageAssignment.track_id.in_([fixed_track.id, auto_track.id]),
            StageAssignment.user_id == secondary.id,
        )
    ).all()


def test_track_composer_patch_preserves_primary_submitter(client, db_session, factory, auth_headers):
    producer, _mastering, submitter, secondary, _reviewer, _album, track = _collab_track(factory)
    replacement = factory.user(username="replacement")
    db_session.add(TrackComposer(track_id=track.id, user_id=replacement.id))
    db_session.commit()

    response = client.patch(
        f"/api/tracks/{track.id}/composers",
        headers=auth_headers(producer),
        json={"composer_ids": [secondary.id, replacement.id]},
    )
    assert response.status_code == 422

    album = track.album
    from app.models.album_member import AlbumMember

    db_session.add(AlbumMember(album_id=album.id, user_id=replacement.id))
    db_session.commit()
    response = client.patch(
        f"/api/tracks/{track.id}/composers",
        headers=auth_headers(producer),
        json={"composer_ids": [secondary.id, replacement.id]},
    )
    assert response.status_code == 200
    assert set(response.json()["composer_ids"]) == {submitter.id, secondary.id, replacement.id}
