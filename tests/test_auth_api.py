from datetime import datetime, timedelta, timezone

from app.models.email_verification import EmailVerificationToken
from app.models.password_reset import PasswordResetToken
from app.models.user import User
from app.routers import auth as auth_router
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
    assert body["email"] == "new@example.com"
    assert "message" in body


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
    user = factory.user(email="login@example.com", email_verified=True)
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


def test_update_me_email_requires_reverification(client, factory, auth_headers):
    user = factory.user(email="old@example.com", email_verified=True)
    user.password = hash_password("testpass123")
    factory.session.commit()

    response = client.patch(
        "/api/auth/me",
        headers=auth_headers(user),
        json={"email": "new@example.com"},
    )

    assert response.status_code == 200
    assert response.json()["email"] == "new@example.com"
    assert response.json()["email_verified"] is False

    login_response = client.post(
        "/api/auth/login",
        json={"email": "new@example.com", "password": "testpass123"},
    )
    assert login_response.status_code == 403


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
    user = factory.user(email_verified=True)
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


def test_login_rejects_unverified_email(client, factory):
    user = factory.user(email="pending@example.com", email_verified=False)
    user.password = hash_password("testpass123")
    factory.session.commit()

    response = client.post(
        "/api/auth/login",
        json={"email": "pending@example.com", "password": "testpass123"},
    )
    assert response.status_code == 403


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


def test_verify_email_marks_token_used_and_returns_auth_response(client, db_session):
    payload = {
        "username": "pendinguser",
        "display_name": "Pending User",
        "email": "pending@example.com",
        "password": "securepass123",
    }
    register_response = client.post("/api/auth/register", json=payload)
    assert register_response.status_code == 201

    token = db_session.query(EmailVerificationToken).filter_by(email=payload["email"]).one()

    response = client.post(f"/api/auth/verify-email?token={token.token}")

    db_session.expire_all()
    refreshed_user = db_session.query(User).filter_by(email=payload["email"]).one()
    refreshed_token = db_session.get(EmailVerificationToken, token.id)

    assert response.status_code == 200
    assert response.json()["user"]["email"] == payload["email"]
    assert refreshed_user.email_verified is True
    assert refreshed_token is not None
    assert refreshed_token.used is True


def test_resend_verification_invalidates_old_token_and_sends_new_one(client, db_session, factory, monkeypatch):
    user = factory.user(email="pending@example.com", email_verified=False)
    old_token = EmailVerificationToken(
        token="old-verification-token",
        email=user.email,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        used=False,
    )
    db_session.add(old_token)
    db_session.commit()

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        auth_router,
        "send_verification_email",
        lambda email, token: sent.append((email, token)),
    )

    response = client.post(f"/api/auth/resend-verification?email={user.email}")

    db_session.expire_all()
    tokens = db_session.query(EmailVerificationToken).filter_by(email=user.email).order_by(EmailVerificationToken.id.asc()).all()
    replacement = next(token for token in tokens if token.id != old_token.id)

    assert response.status_code == 204
    assert len(tokens) == 2
    assert db_session.get(EmailVerificationToken, old_token.id).used is True
    assert replacement.used is False
    assert sent == [(user.email, replacement.token)]


def test_forgot_password_creates_token_only_for_verified_user(client, db_session, factory, monkeypatch):
    verified = factory.user(email="verified@example.com", email_verified=True)
    unverified = factory.user(email="unverified@example.com", email_verified=False)

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        auth_router,
        "send_password_reset_email",
        lambda email, token: sent.append((email, token)),
    )

    verified_response = client.post("/api/auth/forgot-password", json={"email": verified.email})
    unverified_response = client.post("/api/auth/forgot-password", json={"email": unverified.email})
    missing_response = client.post("/api/auth/forgot-password", json={"email": "missing@example.com"})

    verified_token = db_session.query(PasswordResetToken).filter_by(email=verified.email).one()
    unverified_tokens = db_session.query(PasswordResetToken).filter_by(email=unverified.email).all()

    assert verified_response.status_code == 204
    assert unverified_response.status_code == 204
    assert missing_response.status_code == 204
    assert sent == [(verified.email, verified_token.token)]
    assert unverified_tokens == []


def test_reset_password_updates_hash_and_marks_token_used(client, db_session, factory):
    user = factory.user(email="reset@example.com", email_verified=True)
    user.password = hash_password("oldpass1234")
    db_session.commit()

    reset_token = PasswordResetToken(
        token="reset-token",
        email=user.email,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
        used=False,
    )
    db_session.add(reset_token)
    db_session.commit()

    response = client.post(
        "/api/auth/reset-password",
        json={"token": reset_token.token, "new_password": "newpass1234"},
    )

    db_session.expire_all()
    refreshed_token = db_session.get(PasswordResetToken, reset_token.id)

    assert response.status_code == 200
    assert response.json()["user"]["id"] == user.id
    assert refreshed_token is not None
    assert refreshed_token.used is True
    assert client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "newpass1234"},
    ).status_code == 200


def test_reset_password_rejects_expired_token(client, db_session, factory):
    user = factory.user(email="expired@example.com", email_verified=True)
    expired_token = PasswordResetToken(
        token="expired-token",
        email=user.email,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        used=False,
    )
    db_session.add(expired_token)
    db_session.commit()

    response = client.post(
        "/api/auth/reset-password",
        json={"token": expired_token.token, "new_password": "newpass1234"},
    )

    assert response.status_code == 400
    assert "expired" in response.json()["detail"].lower()


def test_upload_avatar_replaces_old_file(client, db_session, factory, auth_headers, upload_dir):
    user = factory.user()
    avatars_dir = upload_dir / "avatars"
    avatars_dir.mkdir(parents=True, exist_ok=True)
    old_file = avatars_dir / "old.png"
    old_file.write_bytes(b"old-image")
    user.avatar_image = "avatars/old.png"
    db_session.commit()

    response = client.post(
        "/api/auth/me/avatar",
        headers=auth_headers(user),
        files={"file": ("avatar.png", b"new-image", "image/png")},
    )

    db_session.expire_all()
    refreshed_user = db_session.get(User, user.id)

    assert response.status_code == 200
    assert refreshed_user is not None
    assert refreshed_user.avatar_image is not None
    assert refreshed_user.avatar_image.startswith("avatars/")
    assert refreshed_user.avatar_image != "avatars/old.png"
    assert old_file.exists() is False
    assert (upload_dir / refreshed_user.avatar_image).exists()


def test_upload_avatar_rejects_invalid_type_and_extension(client, factory, auth_headers):
    user = factory.user()

    bad_type = client.post(
        "/api/auth/me/avatar",
        headers=auth_headers(user),
        files={"file": ("avatar.txt", b"not-image", "text/plain")},
    )
    bad_extension = client.post(
        "/api/auth/me/avatar",
        headers=auth_headers(user),
        files={"file": ("avatar.bmp", b"fake-image", "image/bmp")},
    )

    assert bad_type.status_code == 400
    assert bad_extension.status_code == 400


def test_delete_avatar_removes_existing_file(client, db_session, factory, auth_headers, upload_dir):
    user = factory.user()
    avatars_dir = upload_dir / "avatars"
    avatars_dir.mkdir(parents=True, exist_ok=True)
    avatar_file = avatars_dir / "existing.png"
    avatar_file.write_bytes(b"image")
    user.avatar_image = "avatars/existing.png"
    db_session.commit()

    response = client.delete("/api/auth/me/avatar", headers=auth_headers(user))

    db_session.expire_all()
    refreshed_user = db_session.get(User, user.id)

    assert response.status_code == 200
    assert refreshed_user is not None
    assert refreshed_user.avatar_image is None
    assert avatar_file.exists() is False


def test_delete_account_soft_deletes_user_and_invalidates_reset_tokens(client, db_session, factory, auth_headers):
    user = factory.user(email="close@example.com", username="close-me")
    user.password = hash_password("closepass123")
    db_session.commit()

    reset_token = PasswordResetToken(
        token="close-reset-token",
        email=user.email,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
        used=False,
    )
    db_session.add(reset_token)
    db_session.commit()

    response = client.post(
        "/api/auth/me/delete-account",
        headers=auth_headers(user),
        json={"password": "closepass123"},
    )

    db_session.expire_all()
    refreshed_user = db_session.get(User, user.id)
    refreshed_token = db_session.get(PasswordResetToken, reset_token.id)

    assert response.status_code == 204
    assert refreshed_user is not None
    assert refreshed_user.deleted_at is not None
    assert refreshed_user.display_name == "[deleted user]"
    assert ".deleted-" in refreshed_user.username
    assert ".deleted-" in refreshed_user.email
    assert refreshed_token is not None
    assert refreshed_token.used is True


def test_delete_account_rejects_when_user_owns_a_circle(client, db_session, factory, auth_headers):
    owner = factory.user(role="producer", email="owner@example.com")
    owner.password = hash_password("circlepass123")
    db_session.commit()
    create_circle = client.post(
        "/api/circles",
        headers=auth_headers(owner),
        json={"name": "Owned Circle", "description": "desc"},
    )
    assert create_circle.status_code == 201

    response = client.post(
        "/api/auth/me/delete-account",
        headers=auth_headers(owner),
        json={"password": "circlepass123"},
    )

    assert response.status_code == 409
    assert "circles" in response.json()["detail"].lower()


def test_delete_account_rejects_when_user_produces_active_album(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer", email="producer@example.com")
    producer.password = hash_password("albumpass123")
    mastering = factory.user(username="mastering")
    db_session.commit()
    factory.album(producer=producer, mastering_engineer=mastering, members=[mastering], title="Active Album")

    response = client.post(
        "/api/auth/me/delete-account",
        headers=auth_headers(producer),
        json={"password": "albumpass123"},
    )

    assert response.status_code == 409
    assert "active albums" in response.json()["detail"].lower()


def test_delete_account_rejects_active_track_submitter(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(email="submitter@example.com", email_verified=True)
    submitter.password = hash_password("deletepass123")
    factory.session.commit()

    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    factory.track(album=album, submitter=submitter, status="peer_revision")

    response = client.post(
        "/api/auth/me/delete-account",
        headers=auth_headers(submitter),
        json={"password": "deletepass123"},
    )

    assert response.status_code == 409
    assert "active tracks" in response.json()["detail"]


def test_delete_account_rejects_active_mastering_owner(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(email="mastering@example.com", email_verified=True)
    mastering.password = hash_password("deletepass123")
    submitter = factory.user()
    factory.session.commit()

    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    factory.track(album=album, submitter=submitter, status="mastering")

    response = client.post(
        "/api/auth/me/delete-account",
        headers=auth_headers(mastering),
        json={"password": "deletepass123"},
    )

    assert response.status_code == 409
    assert "mastering tracks" in response.json()["detail"]
