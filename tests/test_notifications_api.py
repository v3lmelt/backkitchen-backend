def test_list_notifications(client, factory, auth_headers):
    user = factory.user()
    factory.notification(user=user, title="Notif 1")
    factory.notification(user=user, title="Notif 2")

    response = client.get("/api/notifications", headers=auth_headers(user))
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_list_notifications_only_shows_own(client, factory, auth_headers):
    user1 = factory.user(username="u1")
    user2 = factory.user(username="u2")
    factory.notification(user=user1, title="For u1")
    factory.notification(user=user2, title="For u2")

    response = client.get("/api/notifications", headers=auth_headers(user1))
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["title"] == "For u1"


def test_mark_all_read(client, factory, auth_headers):
    user = factory.user()
    factory.notification(user=user, is_read=False)
    factory.notification(user=user, is_read=False)
    factory.notification(user=user, is_read=True)

    response = client.patch("/api/notifications/read-all", headers=auth_headers(user))
    assert response.status_code == 200
    assert response.json()["updated"] == 2

    all_notifs = client.get("/api/notifications", headers=auth_headers(user)).json()
    assert all(n["is_read"] for n in all_notifs)


def test_mark_single_read(client, factory, auth_headers):
    user = factory.user()
    notif = factory.notification(user=user, is_read=False)

    response = client.patch(
        f"/api/notifications/{notif.id}/read",
        headers=auth_headers(user),
    )
    assert response.status_code == 200
    assert response.json()["is_read"] is True


def test_mark_read_not_found_for_other_user(client, factory, auth_headers):
    user1 = factory.user(username="u1")
    user2 = factory.user(username="u2")
    notif = factory.notification(user=user1)

    response = client.patch(
        f"/api/notifications/{notif.id}/read",
        headers=auth_headers(user2),
    )
    assert response.status_code == 404


def test_mark_read_nonexistent(client, factory, auth_headers):
    user = factory.user()
    response = client.patch(
        "/api/notifications/99999/read",
        headers=auth_headers(user),
    )
    assert response.status_code == 404
