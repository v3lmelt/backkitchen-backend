from app.security import hash_password


def test_register_returns_token_and_user(client):
    response = client.post(
        "/api/auth/register",
        json={
            "username": "newuser",
            "display_name": "New User",
            "email": "new@example.com",
            "password": "securepass123",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["access_token"]
    assert body["user"]["username"] == "newuser"
    assert body["user"]["email"] == "new@example.com"
    assert body["user"]["role"] == "member"


def test_register_rejects_duplicate_email(client):
    payload = {
        "username": "first",
        "display_name": "First",
        "email": "dup@example.com",
        "password": "securepass123",
    }
    assert client.post("/api/auth/register", json=payload).status_code == 201

    payload["username"] = "second"
    response = client.post("/api/auth/register", json=payload)
    assert response.status_code == 409
    assert "Email" in response.json()["detail"]


def test_register_rejects_duplicate_username(client):
    payload = {
        "username": "sameuser",
        "display_name": "Same",
        "email": "one@example.com",
        "password": "securepass123",
    }
    assert client.post("/api/auth/register", json=payload).status_code == 201

    payload["email"] = "two@example.com"
    response = client.post("/api/auth/register", json=payload)
    assert response.status_code == 409
    assert "Username" in response.json()["detail"]


def test_login_success(client, factory):
    user = factory.user(email="login@example.com")
    user.password = hash_password("testpass123")
    factory.session.commit()

    response = client.post(
        "/api/auth/login",
        json={"email": "login@example.com", "password": "testpass123"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["user"]["id"] == user.id


def test_login_wrong_password(client, factory):
    user = factory.user(email="wrong@example.com")
    user.password = hash_password("rightpass123")
    factory.session.commit()

    response = client.post(
        "/api/auth/login",
        json={"email": "wrong@example.com", "password": "wrongpass"},
    )
    assert response.status_code == 401


def test_login_nonexistent_email(client):
    response = client.post(
        "/api/auth/login",
        json={"email": "noone@example.com", "password": "anything"},
    )
    assert response.status_code == 401


def test_get_me(client, factory, auth_headers):
    user = factory.user(username="meuser")
    response = client.get("/api/auth/me", headers=auth_headers(user))
    assert response.status_code == 200
    assert response.json()["username"] == "meuser"


def test_get_me_requires_auth(client):
    response = client.get("/api/auth/me")
    assert response.status_code == 401


def test_update_me_display_name(client, factory, auth_headers):
    user = factory.user()
    response = client.patch(
        "/api/auth/me",
        headers=auth_headers(user),
        json={"display_name": "Updated Name"},
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "Updated Name"


def test_update_me_email(client, factory, auth_headers):
    user = factory.user(email="old@example.com")
    response = client.patch(
        "/api/auth/me",
        headers=auth_headers(user),
        json={"email": "new@example.com"},
    )
    assert response.status_code == 200
    assert response.json()["email"] == "new@example.com"


def test_update_me_email_conflict(client, factory, auth_headers):
    factory.user(email="taken@example.com", username="other")
    user = factory.user(email="mine@example.com")
    response = client.patch(
        "/api/auth/me",
        headers=auth_headers(user),
        json={"email": "taken@example.com"},
    )
    assert response.status_code == 409


def test_change_password(client, factory, auth_headers):
    user = factory.user()
    user.password = hash_password("oldpass1234")
    factory.session.commit()

    response = client.post(
        "/api/auth/me/change-password",
        headers=auth_headers(user),
        json={"current_password": "oldpass1234", "new_password": "newpass1234"},
    )
    assert response.status_code == 204

    login_response = client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "newpass1234"},
    )
    assert login_response.status_code == 200


def test_change_password_wrong_current(client, factory, auth_headers):
    user = factory.user()
    user.password = hash_password("correct123")
    factory.session.commit()

    response = client.post(
        "/api/auth/me/change-password",
        headers=auth_headers(user),
        json={"current_password": "wrongpass1", "new_password": "newpass1234"},
    )
    assert response.status_code == 400
