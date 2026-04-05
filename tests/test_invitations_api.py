def test_create_invitation(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    invitee = factory.user(username="invitee")
    album = factory.album(producer=producer, mastering_engineer=mastering)

    response = client.post(
        f"/api/albums/{album.id}/invitations",
        headers=auth_headers(producer),
        json={"user_id": invitee.id},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["user_id"] == invitee.id
    assert body["status"] == "pending"
    assert body["album"]["id"] == album.id


def test_create_invitation_forbidden_for_non_producer(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    member = factory.user()
    invitee = factory.user(username="invitee")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[member])

    response = client.post(
        f"/api/albums/{album.id}/invitations",
        headers=auth_headers(member),
        json={"user_id": invitee.id},
    )
    assert response.status_code == 403


def test_create_invitation_rejects_existing_member(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    member = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[member])

    response = client.post(
        f"/api/albums/{album.id}/invitations",
        headers=auth_headers(producer),
        json={"user_id": member.id},
    )
    assert response.status_code == 409
    assert "already a member" in response.json()["detail"]


def test_create_invitation_rejects_duplicate_pending(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    invitee = factory.user(username="invitee")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    factory.invitation(album=album, user=invitee, invited_by=producer)

    response = client.post(
        f"/api/albums/{album.id}/invitations",
        headers=auth_headers(producer),
        json={"user_id": invitee.id},
    )
    assert response.status_code == 409
    assert "pending" in response.json()["detail"]


def test_list_album_invitations(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    invitee = factory.user(username="invitee")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    factory.invitation(album=album, user=invitee, invited_by=producer)

    response = client.get(
        f"/api/albums/{album.id}/invitations",
        headers=auth_headers(producer),
    )
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_list_album_invitations_forbidden_for_non_producer(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    member = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[member])

    response = client.get(
        f"/api/albums/{album.id}/invitations",
        headers=auth_headers(member),
    )
    assert response.status_code == 403


def test_list_my_invitations(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    invitee = factory.user(username="invitee")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    factory.invitation(album=album, user=invitee, invited_by=producer)

    response = client.get("/api/invitations", headers=auth_headers(invitee))
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["album_id"] == album.id


def test_accept_invitation(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    invitee = factory.user(username="invitee")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    inv = factory.invitation(album=album, user=invitee, invited_by=producer)

    response = client.post(
        f"/api/invitations/{inv.id}/accept",
        headers=auth_headers(invitee),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_accept_invitation_forbidden_for_wrong_user(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    invitee = factory.user(username="invitee")
    other = factory.user(username="other")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    inv = factory.invitation(album=album, user=invitee, invited_by=producer)

    response = client.post(
        f"/api/invitations/{inv.id}/accept",
        headers=auth_headers(other),
    )
    assert response.status_code == 403


def test_accept_already_accepted_invitation(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    invitee = factory.user(username="invitee")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    inv = factory.invitation(album=album, user=invitee, invited_by=producer, invitation_status="accepted")

    response = client.post(
        f"/api/invitations/{inv.id}/accept",
        headers=auth_headers(invitee),
    )
    assert response.status_code == 409


def test_decline_invitation(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    invitee = factory.user(username="invitee")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    inv = factory.invitation(album=album, user=invitee, invited_by=producer)

    response = client.post(
        f"/api/invitations/{inv.id}/decline",
        headers=auth_headers(invitee),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "declined"


def test_cancel_invitation(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    invitee = factory.user(username="invitee")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    inv = factory.invitation(album=album, user=invitee, invited_by=producer)

    response = client.delete(
        f"/api/invitations/{inv.id}",
        headers=auth_headers(producer),
    )
    assert response.status_code == 204


def test_cancel_invitation_forbidden_for_non_producer(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    invitee = factory.user(username="invitee")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    inv = factory.invitation(album=album, user=invitee, invited_by=producer)

    response = client.delete(
        f"/api/invitations/{inv.id}",
        headers=auth_headers(invitee),
    )
    assert response.status_code == 403
