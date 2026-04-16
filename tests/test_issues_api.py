import json
import sys
from io import BytesIO
from types import SimpleNamespace

from sqlalchemy import select

from app.models.comment import Comment
from app.models.issue import Issue, IssuePhase, IssueStatus
from app.models.issue_audio import IssueAudio
from app.models.stage_assignment import StageAssignment
from app.models.track import TrackStatus
from app.models.track_source_version import TrackSourceVersion
from app.security import create_access_token


def test_create_peer_issue_binds_to_current_source_version(client, db_session, factory, auth_headers):
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
    latest_version = db_session.scalars(
        select(TrackSourceVersion).where(TrackSourceVersion.track_id == track.id)
    ).first()

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Clicks at intro",
            "description": "Please clean this up.",
            "phase": "peer",
            "severity": "major",
            "markers": [{"marker_type": "point", "time_start": 1.2}],
        },
    )

    assert response.status_code == 201
    assert response.json()["phase"] == IssuePhase.PEER.value
    assert response.json()["source_version_id"] == latest_version.id
    assert len(response.json()["markers"]) == 1
    assert response.json()["markers"][0]["marker_type"] == "point"
    assert response.json()["markers"][0]["time_start"] == 1.2


def test_create_final_review_issue_binds_to_current_delivery(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.FINAL_REVIEW)
    delivery = factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)

    # The default workflow's ``final_review`` step is assigned to the
    # producer, so only the producer (not the submitter) may raise issues.
    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(producer),
        json={
            "title": "Master too bright",
            "description": "The top end feels sharp.",
            "phase": "final_review",
            "severity": "major",
            "markers": [{"marker_type": "point", "time_start": 9.5}],
        },
    )

    assert response.status_code == 201
    assert response.json()["master_delivery_id"] == delivery.id


def test_create_general_issue_no_markers(client, factory, auth_headers):
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

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Overall mix too bright",
            "description": "The whole track feels harsh.",
            "phase": "peer",
            "severity": "major",
            "markers": [],
        },
    )

    assert response.status_code == 201
    assert response.json()["markers"] == []


def test_create_multi_marker_issue(client, factory, auth_headers):
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

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Clicks in multiple places",
            "description": "Clicks at intro and bridge.",
            "phase": "peer",
            "severity": "major",
            "markers": [
                {"marker_type": "point", "time_start": 1.2},
                {"marker_type": "range", "time_start": 30.0, "time_end": 45.0},
            ],
        },
    )

    assert response.status_code == 201
    assert len(response.json()["markers"]) == 2
    assert response.json()["markers"][0]["marker_type"] == "point"
    assert response.json()["markers"][1]["marker_type"] == "range"
    assert response.json()["markers"][1]["time_end"] == 45.0


def test_create_range_marker_requires_time_end(client, factory, auth_headers):
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

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Long problem section",
            "description": "This needs fixing.",
            "phase": "peer",
            "severity": "minor",
            "markers": [{"marker_type": "range", "time_start": 4.0}],
        },
    )

    assert response.status_code == 422


def test_update_issue_enforces_phase_permissions(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
        peer_reviewer=reviewer,
    )
    source_version = track.source_versions[-1]
    issue = factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        source_version_id=source_version.id,
    )

    failure = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(outsider),
        json={"status": "resolved"},
    )
    success = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(submitter),
        json={"status": "resolved"},
    )

    assert failure.status_code == 403
    assert success.status_code == 200
    assert success.json()["status"] == IssueStatus.RESOLVED.value


def test_final_review_issue_status_is_managed_by_mastering_engineer_only(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.FINAL_REVIEW)
    delivery = factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)
    issue = factory.issue(
        track=track,
        author=producer,
        phase=IssuePhase.FINAL_REVIEW,
        master_delivery_id=delivery.id,
    )

    metadata_update = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(producer),
        json={"title": "Updated final review note"},
    )
    forbidden_status_update = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(producer),
        json={"status": "resolved"},
    )
    allowed_status_update = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(mastering),
        json={"status": "resolved"},
    )

    assert metadata_update.status_code == 200
    assert metadata_update.json()["title"] == "Updated final review note"
    assert forbidden_status_update.status_code == 403
    assert allowed_status_update.status_code == 200
    assert allowed_status_update.json()["status"] == IssueStatus.RESOLVED.value


def test_add_comment_rejects_invalid_image_type(client, factory, auth_headers):
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
    issue = factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER, source_version_id=track.source_versions[-1].id)

    response = client.post(
        f"/api/issues/{issue.id}/comments",
        headers=auth_headers(submitter),
        data={"content": "Here is an image"},
        files={"images": ("note.txt", BytesIO(b"hello"), "text/plain")},
    )

    assert response.status_code == 422


def test_add_comment_persists_images(client, db_session, factory, auth_headers):
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
    issue = factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER, source_version_id=track.source_versions[-1].id)

    response = client.post(
        f"/api/issues/{issue.id}/comments",
        headers=auth_headers(submitter),
        data={"content": "Please see attached"},
        files=[
            ("images", ("shot.png", BytesIO(b"pngdata"), "image/png")),
            ("images", ("shot2.webp", BytesIO(b"webpdata"), "image/webp")),
        ],
    )

    assert response.status_code == 201
    body = response.json()
    assert len(body["images"]) == 2
    assert all(image["image_url"].startswith("/uploads/comment_images/") for image in body["images"])

    comments = db_session.scalars(select(Comment).where(Comment.issue_id == issue.id)).all()
    assert len(comments) == 1
    assert len(comments[0].images) == 2


def test_create_issue_returns_protected_audio_urls_for_local_uploads(client, factory, auth_headers):
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

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        data={
            "title": "Clicks in intro",
            "description": "See attached example",
            "phase": "peer",
            "severity": "major",
            "markers_json": "[]",
        },
        files=[("audios", ("issue-note.wav", BytesIO(b"RIFFissue"), "audio/wav"))],
    )

    assert response.status_code == 201
    audios = response.json()["audios"]
    assert len(audios) == 1
    assert audios[0]["audio_url"] == f"/api/issue-audios/{audios[0]['id']}/file"

    download = client.get(
        audios[0]["audio_url"],
        params={"token": create_access_token(reviewer)},
    )

    assert download.status_code == 200
    assert download.content == b"RIFFissue"
    assert download.headers["content-type"].startswith("audio/wav")


def test_add_comment_returns_protected_audio_urls_for_local_uploads(client, factory, auth_headers):
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
    issue = factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER, source_version_id=track.source_versions[-1].id)

    response = client.post(
        f"/api/issues/{issue.id}/comments",
        headers=auth_headers(submitter),
        data={"content": "Attached audio note"},
        files=[("audios", ("comment-note.wav", BytesIO(b"RIFFcomment"), "audio/wav"))],
    )

    assert response.status_code == 201
    audios = response.json()["audios"]
    assert len(audios) == 1
    assert audios[0]["audio_url"] == f"/api/comment-audios/{audios[0]['id']}/file"

    download = client.get(
        audios[0]["audio_url"],
        params={"token": create_access_token(submitter)},
    )

    assert download.status_code == 200
    assert download.content == b"RIFFcomment"
    assert download.headers["content-type"].startswith("audio/wav")


def test_issue_audio_route_redirects_r2_attachments(client, db_session, factory, auth_headers, monkeypatch):
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
    issue = factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER, source_version_id=track.source_versions[-1].id)
    issue_audio = IssueAudio(
        issue_id=issue.id,
        file_path="issues/1/example.wav",
        storage_backend="r2",
        original_filename="example.wav",
        duration=1.23,
    )
    db_session.add(issue_audio)
    db_session.commit()
    db_session.refresh(issue_audio)

    monkeypatch.setitem(
        sys.modules,
        "app.services.r2",
        SimpleNamespace(public_url=lambda key: f"https://cdn.example.com/{key}"),
    )

    response = client.get(
        f"/api/issue-audios/{issue_audio.id}/file",
        params={"token": create_access_token(reviewer)},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "https://cdn.example.com/issues/1/example.wav"


def test_list_issues_for_track(client, factory, auth_headers):
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
    sv = track.source_versions[-1]
    factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER, source_version_id=sv.id)
    factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER, source_version_id=sv.id)

    response = client.get(f"/api/tracks/{track.id}/issues", headers=auth_headers(reviewer))
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_get_issue_detail(client, factory, auth_headers):
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
    sv = track.source_versions[-1]
    issue = factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER, source_version_id=sv.id)

    response = client.get(f"/api/issues/{issue.id}", headers=auth_headers(reviewer))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == issue.id
    assert "comments" in body
    assert "markers" in body


def test_get_issue_not_found(client, factory, auth_headers):
    user = factory.user()
    response = client.get("/api/issues/99999", headers=auth_headers(user))
    assert response.status_code == 404


def test_batch_update_issues(client, db_session, factory, auth_headers):
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
    sv = track.source_versions[-1]
    issue1 = factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER, source_version_id=sv.id)
    issue2 = factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER, source_version_id=sv.id)

    response = client.patch(
        f"/api/tracks/{track.id}/issues/batch",
        headers=auth_headers(submitter),
        json={
            "issue_ids": [issue1.id, issue2.id],
            "status": "resolved",
            "status_note": "All fixed",
        },
    )
    assert response.status_code == 200
    assert len(response.json()) == 2
    assert all(i["status"] == IssueStatus.RESOLVED.value for i in response.json())


def test_batch_update_issues_forbidden_for_outsider(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
        peer_reviewer=reviewer,
    )
    sv = track.source_versions[-1]
    issue = factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER, source_version_id=sv.id)

    response = client.patch(
        f"/api/tracks/{track.id}/issues/batch",
        headers=auth_headers(outsider),
        json={"issue_ids": [issue.id], "status": "resolved"},
    )
    assert response.status_code == 403


def test_create_marker_time_end_must_exceed_time_start(client, factory, auth_headers):
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

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Bad range",
            "description": "End before start.",
            "phase": "peer",
            "severity": "minor",
            "markers": [{"marker_type": "range", "time_start": 10.0, "time_end": 5.0}],
        },
    )
    assert response.status_code == 422


def test_add_comment_text_only(client, factory, auth_headers):
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
    issue = factory.issue(
        track=track, author=reviewer, phase=IssuePhase.PEER,
        source_version_id=track.source_versions[-1].id,
    )

    response = client.post(
        f"/api/issues/{issue.id}/comments",
        headers=auth_headers(submitter),
        data={"content": "Just a text comment"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["content"] == "Just a text comment"
    assert body["images"] == []


def test_create_issue_custom_review_step_allows_stage_assignment_reviewer(client, db_session, factory, auth_headers):
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
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Needs tweak",
            "description": "Custom review note",
            "phase": "custom_review",
            "severity": "major",
            "markers": [],
        },
    )

    assert response.status_code == 201
    assert response.json()["phase"] == "custom_review"


def test_create_issue_custom_review_step_rejects_unassigned_member(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    outsider = factory.user(username="outsider")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer, outsider],
    )
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
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(outsider),
        json={
            "title": "Unauthorized note",
            "description": "Should fail",
            "phase": "custom_review",
            "severity": "major",
            "markers": [],
        },
    )

    assert response.status_code == 403


def test_multi_reviewer_issue_defaults_to_pending_discussion_and_hidden_from_submitter(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer_a, reviewer_b])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer_a)

    db_session.add_all([
        StageAssignment(track_id=track.id, stage_id="peer_review", user_id=reviewer_a.id, status="pending"),
        StageAssignment(track_id=track.id, stage_id="peer_review", user_id=reviewer_b.id, status="pending"),
    ])
    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "peer_review",
                    "label": "Peer Review",
                    "type": "review",
                    "ui_variant": "peer_review",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "transitions": {"pass": "producer_gate", "needs_revision": "peer_revision"},
                    "assignment_mode": "manual",
                    "required_reviewer_count": 2,
                    "revision_step": "peer_revision",
                },
                {
                    "id": "peer_revision",
                    "label": "Peer Revision",
                    "type": "revision",
                    "assignee_role": "submitter",
                    "order": 1,
                    "return_to": "peer_review",
                    "transitions": {},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 2,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    db_session.commit()

    create_response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer_a),
        json={
            "title": "Needs discussion",
            "description": "Reviewer notes",
            "phase": "peer",
            "severity": "major",
            "markers": [],
        },
    )

    assert create_response.status_code == 201
    issue_id = create_response.json()["id"]
    assert create_response.json()["status"] == IssueStatus.PENDING_DISCUSSION.value

    list_submitter = client.get(f"/api/tracks/{track.id}/issues", headers=auth_headers(submitter))
    assert list_submitter.status_code == 200
    assert all(item["id"] != issue_id for item in list_submitter.json())

    detail_submitter = client.get(f"/api/issues/{issue_id}", headers=auth_headers(submitter))
    assert detail_submitter.status_code == 404

    track_detail_submitter = client.get(f"/api/tracks/{track.id}", headers=auth_headers(submitter))
    assert track_detail_submitter.status_code == 200
    assert track_detail_submitter.json()["track"]["open_issue_count"] == 0


def test_pending_discussion_visible_after_reviewer_moves_to_open(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)
    issue = factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.PENDING_DISCUSSION,
        source_version_id=track.source_versions[-1].id,
    )

    hidden_detail = client.get(f"/api/issues/{issue.id}", headers=auth_headers(submitter))
    assert hidden_detail.status_code == 404

    update_response = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(reviewer),
        json={"status": "open"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["status"] == IssueStatus.OPEN.value

    visible_detail = client.get(f"/api/issues/{issue.id}", headers=auth_headers(submitter))
    assert visible_detail.status_code == 200


def test_submitter_cannot_change_pending_discussion_status(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)
    issue = factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.PENDING_DISCUSSION,
        source_version_id=track.source_versions[-1].id,
    )

    response = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(submitter),
        json={"status": "resolved"},
    )

    assert response.status_code == 404


def test_reviewer_can_mark_pending_discussion_internal_resolved_and_keep_hidden(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)
    issue = factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.PENDING_DISCUSSION,
        source_version_id=track.source_versions[-1].id,
    )

    to_internal_resolved = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(reviewer),
        json={"status": "internal_resolved"},
    )
    assert to_internal_resolved.status_code == 200
    assert to_internal_resolved.json()["status"] == "internal_resolved"

    list_for_submitter = client.get(f"/api/tracks/{track.id}/issues", headers=auth_headers(submitter))
    assert list_for_submitter.status_code == 200
    assert all(item["id"] != issue.id for item in list_for_submitter.json())

    detail_for_submitter = client.get(f"/api/issues/{issue.id}", headers=auth_headers(submitter))
    assert detail_for_submitter.status_code == 404


def test_internal_resolved_becomes_visible_when_published_open(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)
    issue = factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.INTERNAL_RESOLVED,
        source_version_id=track.source_versions[-1].id,
    )

    hidden_detail = client.get(f"/api/issues/{issue.id}", headers=auth_headers(submitter))
    assert hidden_detail.status_code == 404

    publish = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(reviewer),
        json={"status": "open"},
    )
    assert publish.status_code == 200
    assert publish.json()["status"] == IssueStatus.OPEN.value

    visible_detail = client.get(f"/api/issues/{issue.id}", headers=auth_headers(submitter))
    assert visible_detail.status_code == 200


def test_internal_resolved_not_counted_as_open_issue_for_submitter(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)

    factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.INTERNAL_RESOLVED,
        source_version_id=track.source_versions[-1].id,
    )

    detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(submitter))
    assert detail.status_code == 200
    assert detail.json()["track"]["open_issue_count"] == 0


def test_single_reviewer_issue_stays_open_by_default(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)

    db_session.add(StageAssignment(track_id=track.id, stage_id="peer_review", user_id=reviewer.id, status="pending"))
    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "peer_review",
                    "label": "Peer Review",
                    "type": "review",
                    "ui_variant": "peer_review",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "transitions": {"pass": "producer_gate", "needs_revision": "peer_revision"},
                    "assignment_mode": "manual",
                    "required_reviewer_count": 1,
                    "revision_step": "peer_revision",
                },
                {
                    "id": "peer_revision",
                    "label": "Peer Revision",
                    "type": "revision",
                    "assignee_role": "submitter",
                    "order": 1,
                    "return_to": "peer_review",
                    "transitions": {},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 2,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    db_session.commit()

    create_response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Directly visible",
            "description": "Single-review issue",
            "phase": "peer",
            "severity": "major",
            "markers": [],
        },
    )

    assert create_response.status_code == 201
    assert create_response.json()["status"] == IssueStatus.OPEN.value


def test_submitter_cannot_see_internal_comments_after_issue_is_published(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)

    issue = factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.PENDING_DISCUSSION,
        source_version_id=track.source_versions[-1].id,
    )

    first_comment = client.post(
        f"/api/issues/{issue.id}/comments",
        headers=auth_headers(reviewer),
        data={"content": "Internal note"},
    )
    assert first_comment.status_code == 201
    assert first_comment.json()["visibility"] == "internal"

    publish = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(reviewer),
        json={"status": "open"},
    )
    assert publish.status_code == 200

    second_comment = client.post(
        f"/api/issues/{issue.id}/comments",
        headers=auth_headers(reviewer),
        data={"content": "Public note"},
    )
    assert second_comment.status_code == 201
    assert second_comment.json()["visibility"] == "public"

    detail_for_submitter = client.get(f"/api/issues/{issue.id}", headers=auth_headers(submitter))
    assert detail_for_submitter.status_code == 200
    comment_payload = detail_for_submitter.json()["comments"]
    assert len(comment_payload) == 1
    assert comment_payload[0]["content"] == "Public note"
    assert comment_payload[0]["visibility"] == "public"
