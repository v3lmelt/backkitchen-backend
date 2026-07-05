import copy
from datetime import datetime, timezone
from io import BytesIO

from sqlalchemy import select

from app.models.comment import Comment
from app.models.issue import IssuePhase, IssueStatus
from app.models.master_delivery import MasterDelivery
from app.models.notification import Notification
from app.models.stage_assignment import StageAssignment
from app.models.track import TrackStatus
from app.models.track_composer import TrackComposer, TrackExternalComposer
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


def test_track_composer_patch_accepts_active_non_member_and_replaces_platform_list(client, db_session, factory, auth_headers):
    producer, _mastering, _submitter, secondary, _reviewer, _album, track = _collab_track(factory)
    replacement = factory.user(username="replacement")
    db_session.add(TrackComposer(track_id=track.id, user_id=replacement.id))
    db_session.commit()

    response = client.patch(
        f"/api/tracks/{track.id}/composers",
        headers=auth_headers(producer),
        json={"composer_ids": [secondary.id, replacement.id]},
    )
    assert response.status_code == 200
    assert set(response.json()["composer_ids"]) == {secondary.id, replacement.id}


def test_track_composer_patch_rejects_missing_deleted_or_suspended_users(client, db_session, factory, auth_headers):
    producer, _mastering, _submitter, secondary, _reviewer, _album, track = _collab_track(factory)
    deleted = factory.user(username="deleted-composer")
    suspended = factory.user(username="suspended-composer")
    deleted.deleted_at = datetime.now(timezone.utc)
    suspended.suspended_at = datetime.now(timezone.utc)
    db_session.commit()

    response = client.patch(
        f"/api/tracks/{track.id}/composers",
        headers=auth_headers(producer),
        json={"composer_ids": [secondary.id, deleted.id]},
    )
    assert response.status_code == 422
    assert "not active platform users" in response.text

    response = client.patch(
        f"/api/tracks/{track.id}/composers",
        headers=auth_headers(producer),
        json={"composer_ids": [secondary.id, suspended.id]},
    )
    assert response.status_code == 422
    assert "not active platform users" in response.text

    response = client.patch(
        f"/api/tracks/{track.id}/composers",
        headers=auth_headers(producer),
        json={"composer_ids": [secondary.id, 999999]},
    )
    assert response.status_code == 422
    assert "not active platform users" in response.text


def test_create_track_accepts_multiple_external_composers_with_platform_composer(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer", username="producer-external-create")
    mastering = factory.user(role="mastering_engineer", username="mastering-external-create")
    submitter = factory.user(username="submitter-external-create")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])

    response = client.post(
        "/api/tracks",
        headers=auth_headers(submitter),
        data={
            "title": "Mixed Song",
            "artist": "Mixed Unit",
            "album_id": str(album.id),
            "composer_ids": [str(submitter.id)],
            "external_composer_names": ["Guest A", "Guest B"],
        },
        files={"file": ("mixed.wav", BytesIO(b"RIFFmixed"), "audio/wav")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["composer_ids"] == [submitter.id]
    assert body["external_composer_names"] == ["Guest A", "Guest B"]
    assert [item["name"] for item in body["external_composers"]] == ["Guest A", "Guest B"]
    assert body["is_proxy_submission"] is False

    stored_names = db_session.scalars(
        select(TrackExternalComposer.name)
        .where(TrackExternalComposer.track_id == body["id"])
        .order_by(TrackExternalComposer.sort_order)
    ).all()
    assert stored_names == ["Guest A", "Guest B"]


def test_external_only_track_uses_producer_as_composer_actor(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer", username="external-only-producer")
    mastering = factory.user(role="mastering_engineer", username="external-only-mastering")
    reviewer = factory.user(username="external-only-reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[reviewer], quick_followup_enabled=True)
    track = factory.track(
        album=album,
        submitter=producer,
        status="peer_revision",
        peer_reviewer=reviewer,
        external_composers=["Offline A", "Offline B"],
        include_submitter_composer=False,
    )
    track.proxy_uploader_id = producer.id
    db_session.commit()

    detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(producer))
    assert detail.status_code == 200
    detail_track = detail.json()["track"]
    assert detail_track["composer_ids"] == []
    assert detail_track["external_composer_names"] == ["Offline A", "Offline B"]
    assert "upload_revision" in detail_track["allowed_actions"]

    revision = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(producer),
        files={"file": ("revision.wav", BytesIO(b"RIFFexternal"), "audio/wav")},
    )
    assert revision.status_code == 200
    assert revision.json()["version"] == 2


def test_platform_composer_can_handle_mixed_external_track_actions(client, factory, auth_headers):
    _producer, _mastering, _submitter, secondary, _reviewer, _album, track = _collab_track(
        factory,
        status="peer_revision",
    )
    factory.session.add(TrackExternalComposer(track_id=track.id, name="Offline Guest", sort_order=0))
    track.external_submitter_name = "Offline Guest"
    factory.session.commit()

    revision = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(secondary),
        files={"file": ("revision.wav", BytesIO(b"RIFFmixedrev"), "audio/wav")},
    )

    assert revision.status_code == 200
    body = revision.json()
    assert set(body["composer_ids"]) == {track.submitter_id, secondary.id}
    assert body["external_composer_names"] == ["Offline Guest"]


def test_patch_track_composers_updates_external_names_and_rejects_external_only_non_producer(client, db_session, factory, auth_headers):
    producer, _mastering, submitter, secondary, _reviewer, _album, track = _collab_track(factory)

    mixed = client.patch(
        f"/api/tracks/{track.id}/composers",
        headers=auth_headers(submitter),
        json={
            "composer_ids": [submitter.id, secondary.id],
            "external_composer_names": ["Guest A", "Guest B"],
        },
    )
    assert mixed.status_code == 200
    assert set(mixed.json()["composer_ids"]) == {submitter.id, secondary.id}
    assert mixed.json()["external_composer_names"] == ["Guest A", "Guest B"]

    external_only_by_submitter = client.patch(
        f"/api/tracks/{track.id}/composers",
        headers=auth_headers(submitter),
        json={"composer_ids": [], "external_composer_names": ["Guest Only"]},
    )
    assert external_only_by_submitter.status_code == 403

    external_only_by_producer = client.patch(
        f"/api/tracks/{track.id}/composers",
        headers=auth_headers(producer),
        json={"composer_ids": [], "external_composer_names": ["Guest Only"]},
    )
    assert external_only_by_producer.status_code == 200
    body = external_only_by_producer.json()
    assert body["composer_ids"] == []
    assert body["external_composer_names"] == ["Guest Only"]
    assert body["proxy_uploader_id"] == producer.id
