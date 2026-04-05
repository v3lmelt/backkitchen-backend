from io import BytesIO

from sqlalchemy import select

from app.models.comment import Comment
from app.models.issue import Issue, IssuePhase, IssueStatus
from app.models.track import TrackStatus
from app.models.track_source_version import TrackSourceVersion


def test_create_peer_issue_binds_to_current_source_version(client, db_session, factory, auth_headers):
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
            "issue_type": "point",
            "severity": "major",
            "time_start": 1.2,
        },
    )

    assert response.status_code == 201
    assert response.json()["phase"] == IssuePhase.PEER.value
    assert response.json()["source_version_id"] == latest_version.id


def test_create_final_review_issue_binds_to_current_delivery(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.FINAL_REVIEW)
    delivery = factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(submitter),
        json={
            "title": "Master too bright",
            "description": "The top end feels sharp.",
            "phase": "final_review",
            "issue_type": "point",
            "severity": "major",
            "time_start": 9.5,
        },
    )

    assert response.status_code == 201
    assert response.json()["master_delivery_id"] == delivery.id


def test_create_range_issue_requires_time_end(client, factory, auth_headers):
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

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Long problem section",
            "description": "This needs fixing.",
            "phase": "peer",
            "issue_type": "range",
            "severity": "minor",
            "time_start": 4.0,
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
        status=TrackStatus.PEER_REVIEW,
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
        json={"status": "will_fix"},
    )

    assert failure.status_code == 403
    assert success.status_code == 200
    assert success.json()["status"] == IssueStatus.WILL_FIX.value


def test_add_comment_rejects_invalid_image_type(client, factory, auth_headers):
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
        status=TrackStatus.PEER_REVIEW,
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


def test_list_issues_for_track(client, factory, auth_headers):
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
        status=TrackStatus.PEER_REVIEW,
        peer_reviewer=reviewer,
    )
    sv = track.source_versions[-1]
    issue = factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER, source_version_id=sv.id)

    response = client.get(f"/api/issues/{issue.id}", headers=auth_headers(reviewer))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == issue.id
    assert "comments" in body


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
        status=TrackStatus.PEER_REVIEW,
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
            "status": "will_fix",
            "status_note": "Will fix all",
        },
    )
    assert response.status_code == 200
    assert len(response.json()) == 2
    assert all(i["status"] == IssueStatus.WILL_FIX.value for i in response.json())


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
        status=TrackStatus.PEER_REVIEW,
        peer_reviewer=reviewer,
    )
    sv = track.source_versions[-1]
    issue = factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER, source_version_id=sv.id)

    response = client.patch(
        f"/api/tracks/{track.id}/issues/batch",
        headers=auth_headers(outsider),
        json={"issue_ids": [issue.id], "status": "will_fix"},
    )
    assert response.status_code == 403


def test_create_issue_time_end_must_exceed_time_start(client, factory, auth_headers):
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

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Bad range",
            "description": "End before start.",
            "phase": "peer",
            "issue_type": "range",
            "severity": "minor",
            "time_start": 10.0,
            "time_end": 5.0,
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
        status=TrackStatus.PEER_REVIEW,
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
