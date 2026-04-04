from app.models.issue import IssuePhase, IssueStatus
from app.models.track import TrackStatus


def test_create_album(client, factory, auth_headers):
    user = factory.user()
    response = client.post(
        "/api/albums",
        headers=auth_headers(user),
        json={"title": "My Album", "description": "desc"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "My Album"
    assert body["producer_id"] == user.id
    assert any(m["user_id"] == user.id for m in body["members"])


def test_list_albums_visibility(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    member = factory.user()
    outsider = factory.user(username="outsider")
    factory.album(producer=producer, mastering_engineer=mastering, members=[member])

    assert len(client.get("/api/albums", headers=auth_headers(producer)).json()) == 1
    assert len(client.get("/api/albums", headers=auth_headers(member)).json()) == 1
    assert len(client.get("/api/albums", headers=auth_headers(mastering)).json()) == 1
    assert len(client.get("/api/albums", headers=auth_headers(outsider)).json()) == 0


def test_get_album(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    response = client.get(f"/api/albums/{album.id}", headers=auth_headers(producer))
    assert response.status_code == 200
    assert response.json()["id"] == album.id


def test_get_album_forbidden_for_outsider(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    response = client.get(f"/api/albums/{album.id}", headers=auth_headers(outsider))
    assert response.status_code == 403


def test_get_album_not_found(client, factory, auth_headers):
    user = factory.user()
    response = client.get("/api/albums/99999", headers=auth_headers(user))
    assert response.status_code == 404


def test_update_album_team(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    new_member = factory.user(username="new_member")
    new_mastering = factory.user(role="mastering_engineer", username="new_me")
    album = factory.album(producer=producer, mastering_engineer=mastering)

    response = client.patch(
        f"/api/albums/{album.id}/team",
        headers=auth_headers(producer),
        json={
            "mastering_engineer_id": new_mastering.id,
            "member_ids": [producer.id, new_member.id],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mastering_engineer_id"] == new_mastering.id
    member_user_ids = {m["user_id"] for m in body["members"]}
    assert new_member.id in member_user_ids
    assert producer.id in member_user_ids


def test_update_album_team_forbidden_for_non_producer(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    member = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[member])

    response = client.patch(
        f"/api/albums/{album.id}/team",
        headers=auth_headers(member),
        json={"member_ids": [member.id]},
    )
    assert response.status_code == 403


def test_album_stats(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])

    track1 = factory.track(album=album, submitter=submitter, status=TrackStatus.PEER_REVIEW, peer_reviewer=reviewer)
    factory.track(album=album, submitter=submitter, status=TrackStatus.COMPLETED)
    sv = track1.source_versions[-1]
    factory.issue(
        track=track1,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.OPEN,
        source_version_id=sv.id,
    )

    response = client.get(f"/api/albums/{album.id}/stats", headers=auth_headers(producer))
    assert response.status_code == 200
    body = response.json()
    assert body["total_tracks"] == 2
    assert body["open_issues"] == 1
    assert "peer_review" in body["by_status"]
    assert "completed" in body["by_status"]


def test_list_album_tracks(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    factory.track(album=album, submitter=submitter)
    factory.track(album=album, submitter=submitter)

    response = client.get(f"/api/albums/{album.id}/tracks", headers=auth_headers(producer))
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_list_album_tracks_forbidden_for_outsider(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering)

    response = client.get(f"/api/albums/{album.id}/tracks", headers=auth_headers(outsider))
    assert response.status_code == 403
