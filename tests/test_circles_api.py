from datetime import datetime, timedelta, timezone

from app.models.circle import CircleInviteCode, CircleMember


def test_create_circle_requires_producer(client, factory, auth_headers):
    member = factory.user()

    response = client.post(
        "/api/circles",
        headers=auth_headers(member),
        json={"name": "Circle One", "description": "desc", "website": "https://example.com"},
    )

    assert response.status_code == 403


def test_create_circle_adds_creator_as_owner(client, factory, auth_headers):
    producer = factory.user(role="producer")

    response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Circle One", "description": "desc", "website": "https://example.com"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Circle One"
    assert body["default_checklist_enabled"] is False
    assert len(body["members"]) == 1
    assert body["members"][0]["user_id"] == producer.id
    assert body["members"][0]["role"] == "owner"


def test_update_circle_can_toggle_default_checklist_enabled(client, factory, auth_headers):
    producer = factory.user(role="producer")
    create_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Before", "description": "desc"},
    )
    circle_id = create_response.json()["id"]

    response = client.patch(
        f"/api/circles/{circle_id}",
        headers=auth_headers(producer),
        json={"default_checklist_enabled": False},
    )

    assert response.status_code == 200
    assert response.json()["default_checklist_enabled"] is False


def test_get_circle_blocks_outsider(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    member = factory.user(username="member")
    outsider = factory.user(username="outsider")
    client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Circle One", "description": "desc"},
    )
    circle_id = client.get("/api/circles", headers=auth_headers(producer)).json()[0]["id"]
    db_session.add(CircleMember(circle_id=circle_id, user_id=member.id, role="member"))
    db_session.commit()

    member_response = client.get(f"/api/circles/{circle_id}", headers=auth_headers(member))
    outsider_response = client.get(f"/api/circles/{circle_id}", headers=auth_headers(outsider))

    assert member_response.status_code == 200
    assert outsider_response.status_code == 403


def test_update_circle_requires_creator(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    other = factory.user(role="producer", username="other")
    create_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Before", "description": "desc"},
    )
    circle_id = create_response.json()["id"]

    forbidden = client.patch(
        f"/api/circles/{circle_id}",
        headers=auth_headers(other),
        json={"name": "After"},
    )
    allowed = client.patch(
        f"/api/circles/{circle_id}",
        headers=auth_headers(producer),
        json={"name": "After"},
    )

    assert forbidden.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["name"] == "After"


def test_invite_code_lifecycle(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    create_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Circle One", "description": "desc"},
    )
    circle_id = create_response.json()["id"]

    created = client.post(
        f"/api/circles/{circle_id}/invite-codes",
        headers=auth_headers(producer),
        json={"role": "member", "expires_in_days": 7},
    )
    listed = client.get(f"/api/circles/{circle_id}/invite-codes", headers=auth_headers(producer))

    assert created.status_code == 201
    code_id = created.json()["id"]
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [code_id]

    revoked = client.delete(
        f"/api/circles/{circle_id}/invite-codes/{code_id}",
        headers=auth_headers(producer),
    )
    db_session.expire_all()
    invite = db_session.get(CircleInviteCode, code_id)

    assert revoked.status_code == 204
    assert invite is not None
    assert invite.is_active is False


def test_join_circle_accepts_active_code(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    joiner = factory.user(username="joiner")
    create_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Circle One", "description": "desc"},
    )
    circle_id = create_response.json()["id"]
    invite = CircleInviteCode(
        circle_id=circle_id,
        code="JOIN123456",
        role="mastering_engineer",
        created_by=producer.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        is_active=True,
    )
    db_session.add(invite)
    db_session.commit()

    response = client.post(
        "/api/circles/join",
        headers=auth_headers(joiner),
        json={"code": "JOIN123456"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == circle_id
    membership = db_session.query(CircleMember).filter_by(circle_id=circle_id, user_id=joiner.id).one()
    assert membership.role == "mastering_engineer"


def test_join_circle_rejects_expired_or_duplicate_membership(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    joiner = factory.user(username="joiner")
    create_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Circle One", "description": "desc"},
    )
    circle_id = create_response.json()["id"]
    expired = CircleInviteCode(
        circle_id=circle_id,
        code="EXPIRED123",
        role="member",
        created_by=producer.id,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        is_active=True,
    )
    active = CircleInviteCode(
        circle_id=circle_id,
        code="ACTIVE12345",
        role="member",
        created_by=producer.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        is_active=True,
    )
    db_session.add_all([expired, active])
    db_session.commit()

    expired_response = client.post(
        "/api/circles/join",
        headers=auth_headers(joiner),
        json={"code": "EXPIRED123"},
    )
    first_join = client.post(
        "/api/circles/join",
        headers=auth_headers(joiner),
        json={"code": "ACTIVE12345"},
    )
    duplicate = client.post(
        "/api/circles/join",
        headers=auth_headers(joiner),
        json={"code": "ACTIVE12345"},
    )

    assert expired_response.status_code == 400
    assert "expired" in expired_response.json()["detail"].lower()
    assert first_join.status_code == 200
    assert duplicate.status_code == 400
    assert "already a member" in duplicate.json()["detail"].lower()


def test_upload_circle_logo_validates_file_type(client, factory, auth_headers):
    producer = factory.user(role="producer")
    create_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Circle One", "description": "desc"},
    )
    circle_id = create_response.json()["id"]

    bad_type = client.post(
        f"/api/circles/{circle_id}/logo",
        headers=auth_headers(producer),
        files={"file": ("logo.txt", b"not-image", "text/plain")},
    )
    bad_ext = client.post(
        f"/api/circles/{circle_id}/logo",
        headers=auth_headers(producer),
        files={"file": ("logo.bmp", b"fake-image", "image/bmp")},
    )
    ok = client.post(
        f"/api/circles/{circle_id}/logo",
        headers=auth_headers(producer),
        files={"file": ("logo.png", b"png-bytes", "image/png")},
    )

    assert bad_type.status_code == 400
    assert bad_ext.status_code == 400
    assert ok.status_code == 200
    assert ok.json()["logo_url"].startswith("logos/")
