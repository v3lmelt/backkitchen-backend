def test_list_users_requires_auth(client):
    response = client.get("/api/users")
    assert response.status_code == 401


def test_list_users(client, factory, auth_headers):
    u1 = factory.user(username="alice")
    u2 = factory.user(username="bob")
    response = client.get("/api/users", headers=auth_headers(u1))
    assert response.status_code == 200
    ids = {u["id"] for u in response.json()}
    assert {u1.id, u2.id} <= ids


def test_get_user_by_id(client, factory, auth_headers):
    user = factory.user(username="target")
    caller = factory.user(username="caller")
    response = client.get(f"/api/users/{user.id}", headers=auth_headers(caller))
    assert response.status_code == 200
    assert response.json()["username"] == "target"


def test_get_user_not_found(client, factory, auth_headers):
    caller = factory.user()
    response = client.get("/api/users/99999", headers=auth_headers(caller))
    assert response.status_code == 404


def test_create_user_as_producer(client, factory, auth_headers):
    producer = factory.user(role="producer", username="prod")
    response = client.post(
        "/api/users",
        headers=auth_headers(producer),
        json={
            "username": "created_user",
            "display_name": "Created User",
            "role": "member",
            "password": "password123",
        },
    )
    assert response.status_code == 201
    assert response.json()["username"] == "created_user"


def test_create_user_forbidden_for_member(client, factory, auth_headers):
    member = factory.user(role="member")
    response = client.post(
        "/api/users",
        headers=auth_headers(member),
        json={
            "username": "should_fail",
            "display_name": "Fail",
            "role": "member",
        },
    )
    assert response.status_code == 403


def test_create_user_duplicate_username(client, factory, auth_headers):
    producer = factory.user(role="producer", username="prod")
    factory.user(username="existing")
    response = client.post(
        "/api/users",
        headers=auth_headers(producer),
        json={
            "username": "existing",
            "display_name": "Dup",
            "role": "member",
        },
    )
    assert response.status_code == 409


def test_create_user_duplicate_email(client, factory, auth_headers):
    producer = factory.user(role="producer", username="prod")
    factory.user(username="emailowner", email="dup@example.com")
    response = client.post(
        "/api/users",
        headers=auth_headers(producer),
        json={
            "username": "newguy",
            "display_name": "New",
            "role": "member",
            "email": "dup@example.com",
        },
    )
    assert response.status_code == 409
