from app.models.notification import Notification


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
