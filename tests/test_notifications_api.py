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


def test_list_notifications_supports_limit_and_offset(client, factory, auth_headers):
    user = factory.user()
    titles = [f"Notif {idx}" for idx in range(4)]
    for title in titles:
        factory.notification(user=user, title=title)

    first_page = client.get(
        "/api/notifications",
        headers=auth_headers(user),
        params={"limit": 2, "offset": 0},
    )
    second_page = client.get(
        "/api/notifications",
        headers=auth_headers(user),
        params={"limit": 2, "offset": 2},
    )

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert len(first_page.json()) == 2
    assert len(second_page.json()) == 2
    assert first_page.json()[0]["id"] > first_page.json()[1]["id"]
    assert first_page.json()[1]["id"] > second_page.json()[0]["id"]


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
