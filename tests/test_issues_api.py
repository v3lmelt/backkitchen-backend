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
