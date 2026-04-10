def test_list_users_unauthenticated(client):
    response = client.get("/api/admin/users")
    assert response.status_code == 401


def test_list_users_requires_admin(client, factory, auth_headers):
    member = factory.user(role="member")
    response = client.get("/api/admin/users", headers=auth_headers(member))
    assert response.status_code == 403


def test_list_users_as_admin(client, factory, auth_headers):
    admin_user = factory.user(username="admin", is_admin=True)
    u1 = factory.user(username="alice")
    u2 = factory.user(username="bob")
    response = client.get("/api/admin/users", headers=auth_headers(admin_user))
    assert response.status_code == 200
    data = response.json()
    ids = {u["id"] for u in data}
    assert {admin_user.id, u1.id, u2.id} <= ids
    # Response must include is_admin field
    for u in data:
        assert "is_admin" in u


def test_update_role_as_admin(client, factory, auth_headers):
    admin_user = factory.user(username="admin", is_admin=True)
    target = factory.user(username="target", role="member")
    response = client.patch(
        f"/api/admin/users/{target.id}",
        headers=auth_headers(admin_user),
        json={"role": "producer"},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "producer"


def test_update_is_admin_as_admin(client, factory, auth_headers):
    admin_user = factory.user(username="admin", is_admin=True)
    target = factory.user(username="target")
    response = client.patch(
        f"/api/admin/users/{target.id}",
        headers=auth_headers(admin_user),
        json={"is_admin": True},
    )
    assert response.status_code == 200
    assert response.json()["is_admin"] is True


def test_update_email_verified_as_admin(client, factory, auth_headers):
    admin_user = factory.user(username="admin", is_admin=True)
    target = factory.user(username="target")
    response = client.patch(
        f"/api/admin/users/{target.id}",
        headers=auth_headers(admin_user),
        json={"email_verified": True},
    )
    assert response.status_code == 200
    assert response.json()["email_verified"] is True


def test_update_user_not_found(client, factory, auth_headers):
    admin_user = factory.user(username="admin", is_admin=True)
    response = client.patch(
        "/api/admin/users/99999",
        headers=auth_headers(admin_user),
        json={"role": "producer"},
    )
    assert response.status_code == 404


def test_delete_user_as_admin(client, factory, auth_headers):
    admin_user = factory.user(username="admin", is_admin=True, role="producer")
    target = factory.user(username="todelete")
    response = client.delete(
        f"/api/admin/users/{target.id}",
        headers=auth_headers(admin_user),
    )
    assert response.status_code == 204
    # Confirm gone (GET /api/users/{id} requires producer or self)
    get_resp = client.get(f"/api/users/{target.id}", headers=auth_headers(admin_user))
    assert get_resp.status_code == 404


def test_delete_self_forbidden(client, factory, auth_headers):
    admin_user = factory.user(username="admin", is_admin=True)
    response = client.delete(
        f"/api/admin/users/{admin_user.id}",
        headers=auth_headers(admin_user),
    )
    assert response.status_code == 400


def test_non_admin_cannot_patch(client, factory, auth_headers):
    member = factory.user(role="member", username="member1")
    target = factory.user(username="target")
    response = client.patch(
        f"/api/admin/users/{target.id}",
        headers=auth_headers(member),
        json={"role": "producer"},
    )
    assert response.status_code == 403


def test_non_admin_cannot_delete(client, factory, auth_headers):
    member = factory.user(role="member", username="member1")
    target = factory.user(username="target")
    response = client.delete(
        f"/api/admin/users/{target.id}",
        headers=auth_headers(member),
    )
    assert response.status_code == 403
