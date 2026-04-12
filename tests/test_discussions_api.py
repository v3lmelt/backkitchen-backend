from app.models.notification import Notification
from app.models.issue import IssuePhase, IssueStatus


def test_list_discussions_requires_track_visibility(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[])
    track = factory.track(album=album, submitter=submitter)

    allowed = client.get(f"/api/tracks/{track.id}/discussions", headers=auth_headers(submitter))
    denied = client.get(f"/api/tracks/{track.id}/discussions", headers=auth_headers(outsider))

    assert allowed.status_code == 200
    assert allowed.json() == []
    assert denied.status_code == 403


def test_create_discussion_persists_message_and_notifies_participants(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, peer_reviewer=reviewer)

    response = client.post(
        f"/api/tracks/{track.id}/discussions",
        headers=auth_headers(submitter),
        data={"content": "Please check the chorus transition."},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["content"] == "Please check the chorus transition."
    assert body["author_id"] == submitter.id
    assert body["images"] == []

    notifications = db_session.query(Notification).filter(Notification.related_track_id == track.id).all()
    notified_user_ids = {item.user_id for item in notifications}
    assert notified_user_ids == {producer.id, mastering.id, reviewer.id}


def test_create_discussion_accepts_images(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[])
    track = factory.track(album=album, submitter=submitter)

    response = client.post(
        f"/api/tracks/{track.id}/discussions",
        headers=auth_headers(submitter),
        data={"content": "Added screenshots"},
        files=[("images", ("shot.png", b"png-bytes", "image/png"))],
    )

    assert response.status_code == 201
    images = response.json()["images"]
    assert len(images) == 1
    assert images[0]["image_url"].startswith("/uploads/discussion_images/")


def test_create_discussion_rejects_non_image_upload(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[])
    track = factory.track(album=album, submitter=submitter)

    response = client.post(
        f"/api/tracks/{track.id}/discussions",
        headers=auth_headers(submitter),
        data={"content": "Bad upload"},
        files=[("images", ("notes.txt", b"plain-text", "text/plain"))],
    )

    assert response.status_code == 422
    assert "image" in response.json()["detail"].lower()


def test_discussion_track_not_found(client, factory, auth_headers):
    user = factory.user()

    list_response = client.get("/api/tracks/99999/discussions", headers=auth_headers(user))
    create_response = client.post(
        "/api/tracks/99999/discussions",
        headers=auth_headers(user),
        data={"content": "Hello"},
    )

    assert list_response.status_code == 404
    assert create_response.status_code == 404


def test_submitter_cannot_view_internal_discussions_while_issue_pending_discussion(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, peer_reviewer=reviewer, status="peer_review")
    factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.PENDING_DISCUSSION,
        source_version_id=track.source_versions[-1].id,
    )

    create_response = client.post(
        f"/api/tracks/{track.id}/discussions",
        headers=auth_headers(reviewer),
        data={"content": "Internal discussion"},
    )
    assert create_response.status_code == 201
    assert create_response.json()["visibility"] == "internal"

    list_submitter = client.get(f"/api/tracks/{track.id}/discussions", headers=auth_headers(submitter))
    assert list_submitter.status_code == 200
    assert list_submitter.json() == []


def test_submitter_still_cannot_view_internal_discussions_after_issue_is_published(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, peer_reviewer=reviewer, status="peer_review")
    issue = factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.PENDING_DISCUSSION,
        source_version_id=track.source_versions[-1].id,
    )

    internal_discussion = client.post(
        f"/api/tracks/{track.id}/discussions",
        headers=auth_headers(reviewer),
        data={"content": "Internal discussion"},
    )
    assert internal_discussion.status_code == 201
    discussion_id = internal_discussion.json()["id"]

    publish = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(reviewer),
        json={"status": "open"},
    )
    assert publish.status_code == 200

    list_submitter = client.get(f"/api/tracks/{track.id}/discussions", headers=auth_headers(submitter))
    assert list_submitter.status_code == 200
    assert all(item["id"] != discussion_id for item in list_submitter.json())


def test_internal_resolved_issue_keeps_new_discussion_internal(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, peer_reviewer=reviewer, status="peer_review")
    factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.INTERNAL_RESOLVED,
        source_version_id=track.source_versions[-1].id,
    )

    create_response = client.post(
        f"/api/tracks/{track.id}/discussions",
        headers=auth_headers(reviewer),
        data={"content": "Still internal thread"},
    )
    assert create_response.status_code == 201
    assert create_response.json()["visibility"] == "internal"

    list_submitter = client.get(f"/api/tracks/{track.id}/discussions", headers=auth_headers(submitter))
    assert list_submitter.status_code == 200
    assert list_submitter.json() == []
