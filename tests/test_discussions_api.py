from io import BytesIO

from app.models.notification import Notification
from app.models.issue import IssuePhase, IssueStatus
from app.security import create_access_token


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


def test_mastering_discussion_audio_uses_protected_api_url_and_supports_token_download(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")

    response = client.post(
        f"/api/tracks/{track.id}/discussions",
        headers=auth_headers(mastering),
        data={"content": "Mastering reference", "phase": "mastering"},
        files=[("audios", ("reference.wav", BytesIO(b"RIFFdiscussion"), "audio/wav"))],
    )

    assert response.status_code == 201
    audios = response.json()["audios"]
    assert len(audios) == 1
    assert audios[0]["audio_url"] == f"/api/discussion-audios/{audios[0]['id']}/file"

    download = client.get(
        audios[0]["audio_url"],
        params={"token": create_access_token(mastering)},
    )

    assert download.status_code == 200
    assert download.content == b"RIFFdiscussion"
    assert download.headers["content-type"].startswith("audio/wav")


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


def test_submitter_can_view_discussions_while_issue_pending_discussion(client, db_session, factory, auth_headers):
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
    discussion_id = create_response.json()["id"]
    assert create_response.json()["visibility"] == "public"

    list_submitter = client.get(f"/api/tracks/{track.id}/discussions", headers=auth_headers(submitter))
    assert list_submitter.status_code == 200
    assert [item["id"] for item in list_submitter.json()] == [discussion_id]


def test_submitter_can_still_view_discussions_after_issue_is_published(client, db_session, factory, auth_headers):
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
    assert internal_discussion.json()["visibility"] == "public"

    publish = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(reviewer),
        json={"status": "open"},
    )
    assert publish.status_code == 200

    list_submitter = client.get(f"/api/tracks/{track.id}/discussions", headers=auth_headers(submitter))
    assert list_submitter.status_code == 200
    assert [item["id"] for item in list_submitter.json()] == [discussion_id]


def test_internal_resolved_issue_does_not_force_new_discussion_internal(client, db_session, factory, auth_headers):
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
    discussion_id = create_response.json()["id"]
    assert create_response.json()["visibility"] == "public"

    list_submitter = client.get(f"/api/tracks/{track.id}/discussions", headers=auth_headers(submitter))
    assert list_submitter.status_code == 200
    assert [item["id"] for item in list_submitter.json()] == [discussion_id]


def _seed_general_discussions(client, headers, track_id: int, count: int) -> list[int]:
    ids: list[int] = []
    for i in range(count):
        response = client.post(
            f"/api/tracks/{track_id}/discussions",
            headers=headers,
            data={"content": f"msg {i}"},
        )
        assert response.status_code == 201
        ids.append(response.json()["id"])
    return ids


def test_list_discussions_defaults_to_full_ascending_list(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter)
    headers = auth_headers(submitter)
    ids = _seed_general_discussions(client, headers, track.id, 5)

    response = client.get(f"/api/tracks/{track.id}/discussions", headers=headers)

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ids


def test_list_discussions_limit_returns_latest_page_ascending(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter)
    headers = auth_headers(submitter)
    ids = _seed_general_discussions(client, headers, track.id, 25)

    response = client.get(f"/api/tracks/{track.id}/discussions?limit=10", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == ids[-10:]


def test_list_discussions_before_id_returns_older_slice(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter)
    headers = auth_headers(submitter)
    ids = _seed_general_discussions(client, headers, track.id, 15)

    latest = client.get(f"/api/tracks/{track.id}/discussions?limit=5", headers=headers).json()
    assert [item["id"] for item in latest] == ids[-5:]

    cursor = latest[0]["id"]
    older = client.get(
        f"/api/tracks/{track.id}/discussions?limit=5&before_id={cursor}",
        headers=headers,
    )

    assert older.status_code == 200
    assert [item["id"] for item in older.json()] == ids[-10:-5]


def test_list_discussions_rejects_invalid_limit(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter)
    headers = auth_headers(submitter)

    assert client.get(f"/api/tracks/{track.id}/discussions?limit=0", headers=headers).status_code == 422
    assert client.get(f"/api/tracks/{track.id}/discussions?limit=51", headers=headers).status_code == 422


def test_track_detail_caps_discussions_to_seed_size(client, factory, auth_headers):
    from app.workflow import TRACK_DETAIL_DISCUSSION_SEED_SIZE

    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter)
    headers = auth_headers(submitter)
    total = TRACK_DETAIL_DISCUSSION_SEED_SIZE + 5
    ids = _seed_general_discussions(client, headers, track.id, total)

    response = client.get(f"/api/tracks/{track.id}", headers=headers)

    assert response.status_code == 200
    detail_ids = [d["id"] for d in response.json()["discussions"]]
    assert detail_ids == ids[-TRACK_DETAIL_DISCUSSION_SEED_SIZE:]
