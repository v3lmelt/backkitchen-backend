from datetime import datetime, timedelta, timezone

from app.models.album_member import AlbumMember
from app.models.circle import Circle, CircleInviteCode, CircleMember
from app.models.invitation import Invitation
from app.models.stage_assignment import StageAssignment


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


def test_list_circles_marks_album_creation_access_per_circle(client, db_session, factory, auth_headers):
    viewer = factory.user(username="circle-manager")
    creator = factory.user(role="producer", username="circle-owner")
    producer_viewer = factory.user(role="producer", username="album-producer")

    owned_circle = Circle(name="Owned Circle", description=None, website=None, created_by=creator.id)
    managed_circle = Circle(name="Managed Circle", description=None, website=None, created_by=creator.id)
    member_circle = Circle(name="Member Circle", description=None, website=None, created_by=creator.id)
    producer_member_circle = Circle(name="Producer Member Circle", description=None, website=None, created_by=creator.id)
    db_session.add_all([owned_circle, managed_circle, member_circle, producer_member_circle])
    db_session.flush()
    db_session.add_all([
        CircleMember(circle_id=owned_circle.id, user_id=viewer.id, role="owner"),
        CircleMember(circle_id=managed_circle.id, user_id=viewer.id, role="co_producer"),
        CircleMember(circle_id=member_circle.id, user_id=viewer.id, role="member"),
        CircleMember(circle_id=producer_member_circle.id, user_id=producer_viewer.id, role="member"),
    ])
    db_session.commit()

    response = client.get("/api/circles", headers=auth_headers(viewer))

    assert response.status_code == 200
    access_by_name = {
        circle["name"]: circle["viewer_can_create_album"]
        for circle in response.json()
    }
    assert access_by_name == {
        "Owned Circle": True,
        "Managed Circle": True,
        "Member Circle": False,
    }

    producer_response = client.get("/api/circles", headers=auth_headers(producer_viewer))
    producer_access_by_name = {
        circle["name"]: circle["viewer_can_create_album"]
        for circle in producer_response.json()
    }
    assert producer_response.status_code == 200
    assert producer_access_by_name == {"Producer Member Circle": True}

def test_owner_can_promote_member_to_co_producer(client, db_session, factory, auth_headers):
    owner = factory.user(role="producer")
    member = factory.user(username="co")
    create_response = client.post(
        "/api/circles",
        headers=auth_headers(owner),
        json={"name": "Circle One", "description": "desc"},
    )
    circle_id = create_response.json()["id"]
    db_session.add(CircleMember(circle_id=circle_id, user_id=member.id, role="member"))
    db_session.commit()

    response = client.patch(
        f"/api/circles/{circle_id}/members/{member.id}",
        headers=auth_headers(owner),
        json={"role": "co_producer"},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "co_producer"


def test_co_producer_can_manage_circle_but_not_ownership_boundaries(client, db_session, factory, auth_headers):
    owner = factory.user(role="producer")
    co_producer = factory.user(username="co")
    member = factory.user(username="member")
    create_response = client.post(
        "/api/circles",
        headers=auth_headers(owner),
        json={"name": "Circle One", "description": "desc"},
    )
    circle_id = create_response.json()["id"]
    db_session.add_all([
        CircleMember(circle_id=circle_id, user_id=co_producer.id, role="co_producer"),
        CircleMember(circle_id=circle_id, user_id=member.id, role="member"),
    ])
    db_session.commit()

    update_response = client.patch(
        f"/api/circles/{circle_id}",
        headers=auth_headers(co_producer),
        json={"name": "Managed Circle"},
    )
    invite_response = client.post(
        f"/api/circles/{circle_id}/invite-codes",
        headers=auth_headers(co_producer),
        json={"role": "member", "expires_in_days": 7},
    )
    promote_response = client.patch(
        f"/api/circles/{circle_id}/members/{member.id}",
        headers=auth_headers(co_producer),
        json={"role": "co_producer"},
    )
    remove_member_response = client.delete(
        f"/api/circles/{circle_id}/members/{member.id}",
        headers=auth_headers(co_producer),
    )
    delete_response = client.delete(
        f"/api/circles/{circle_id}",
        headers=auth_headers(co_producer),
    )

    assert update_response.status_code == 200
    assert update_response.json()["name"] == "Managed Circle"
    assert invite_response.status_code == 201
    assert remove_member_response.status_code == 204
    assert promote_response.status_code == 403
    assert delete_response.status_code == 403


def test_remove_member_revokes_circle_derived_album_access(client, db_session, factory, auth_headers):
    owner = factory.user(role="producer", username="owner")
    member = factory.user(username="departing")
    circle = Circle(name="Circle", description=None, website=None, created_by=owner.id)
    db_session.add(circle)
    db_session.flush()
    membership = CircleMember(circle_id=circle.id, user_id=member.id, role="member")
    db_session.add(membership)
    db_session.commit()

    album = factory.album(producer=owner, mastering_engineer=member, members=[member])
    album.circle_id = circle.id
    track = factory.track(album=album, submitter=owner, peer_reviewer=member)
    assignments = [
        StageAssignment(track_id=track.id, stage_id="peer_review", user_id=member.id, status="pending"),
        StageAssignment(track_id=track.id, stage_id="producer_gate", user_id=member.id, status="completed"),
        StageAssignment(
            track_id=track.id,
            stage_id="mastering",
            user_id=member.id,
            status="cancelled",
            cancellation_reason="revision_requested",
        ),
        StageAssignment(
            track_id=track.id,
            stage_id="final_review",
            user_id=member.id,
            status="cancelled",
            cancellation_reason="reassigned",
        ),
    ]
    db_session.add_all(assignments)
    invitation = Invitation(
        album_id=album.id,
        user_id=member.id,
        invited_by_user_id=owner.id,
        status="pending",
    )
    db_session.add(invitation)
    db_session.commit()

    membership_id = membership.id
    album_member_id = db_session.query(AlbumMember).filter_by(
        album_id=album.id,
        user_id=member.id,
    ).one().id
    invitation_id = invitation.id
    assignment_ids = [assignment.id for assignment in assignments]

    response = client.delete(
        f"/api/circles/{circle.id}/members/{member.id}",
        headers=auth_headers(owner),
    )

    assert response.status_code == 204
    db_session.expire_all()
    assert db_session.get(CircleMember, membership_id) is None
    assert db_session.get(AlbumMember, album_member_id) is None
    assert db_session.get(Invitation, invitation_id) is None
    assert db_session.get(type(album), album.id).mastering_engineer_id is None
    assert db_session.get(type(track), track.id).peer_reviewer_id is None
    refreshed_assignments = [db_session.get(StageAssignment, assignment_id) for assignment_id in assignment_ids]
    assert [assignment.status for assignment in refreshed_assignments[:3]] == ["cancelled"] * 3
    assert [assignment.cancellation_reason for assignment in refreshed_assignments[:3]] == [
        "circle_membership_revoked",
    ] * 3
    assert refreshed_assignments[3].cancellation_reason == "reassigned"


def test_leave_circle_revokes_circle_derived_album_membership(client, db_session, factory, auth_headers):
    owner = factory.user(role="producer", username="owner")
    co_producer = factory.user(username="departing-co-producer")
    circle = Circle(name="Circle", description=None, website=None, created_by=owner.id)
    db_session.add(circle)
    db_session.flush()
    db_session.add(CircleMember(circle_id=circle.id, user_id=co_producer.id, role="co_producer"))
    db_session.commit()

    album = factory.album(producer=owner, mastering_engineer=owner, members=[co_producer])
    album.circle_id = circle.id
    db_session.commit()
    album_member_id = db_session.query(AlbumMember).filter_by(
        album_id=album.id,
        user_id=co_producer.id,
    ).one().id

    response = client.post(
        f"/api/circles/{circle.id}/leave",
        headers=auth_headers(co_producer),
    )

    assert response.status_code == 204
    db_session.expire_all()
    assert db_session.get(AlbumMember, album_member_id) is None
    assert db_session.query(CircleMember).filter_by(
        circle_id=circle.id,
        user_id=co_producer.id,
    ).one_or_none() is None


def test_role_downgrade_keeps_explicit_album_membership(client, db_session, factory, auth_headers):
    owner = factory.user(role="producer", username="owner")
    co_producer = factory.user(username="co-producer")
    circle = Circle(name="Circle", description=None, website=None, created_by=owner.id)
    db_session.add(circle)
    db_session.flush()
    db_session.add(CircleMember(circle_id=circle.id, user_id=co_producer.id, role="co_producer"))
    db_session.commit()

    album = factory.album(producer=owner, mastering_engineer=owner, members=[co_producer])
    album.circle_id = circle.id
    db_session.commit()

    response = client.patch(
        f"/api/circles/{circle.id}/members/{co_producer.id}",
        headers=auth_headers(owner),
        json={"role": "member"},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "member"
    assert db_session.query(AlbumMember).filter_by(
        album_id=album.id,
        user_id=co_producer.id,
    ).one_or_none() is not None


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


def test_invite_code_expiry_accepts_one_year_limit(client, factory, auth_headers):
    producer = factory.user(role="producer")
    create_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Circle One", "description": "desc"},
    )
    circle_id = create_response.json()["id"]

    accepted = client.post(
        f"/api/circles/{circle_id}/invite-codes",
        headers=auth_headers(producer),
        json={"role": "member", "expires_in_days": 365},
    )
    rejected = client.post(
        f"/api/circles/{circle_id}/invite-codes",
        headers=auth_headers(producer),
        json={"role": "member", "expires_in_days": 366},
    )

    assert accepted.status_code == 201
    assert rejected.status_code == 422


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
