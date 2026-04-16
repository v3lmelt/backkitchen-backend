from app.models.circle import Circle, CircleMember
from app.models.reopen_request import ReopenRequest
from app.models.track import TrackStatus


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


def test_suspend_user_revokes_existing_session(client, factory, auth_headers):
    admin_user = factory.user(username="admin", admin_role="operator", is_admin=True)
    target = factory.user(username="target")

    response = client.post(
        f"/api/admin/users/{target.id}/suspend",
        headers=auth_headers(admin_user),
        json={"reason": "Policy violation"},
    )
    assert response.status_code == 200
    assert response.json()["suspended_at"] is not None

    me_response = client.get("/api/auth/me", headers=auth_headers(target))
    assert me_response.status_code == 401


def test_transfer_ownership_allows_soft_delete_after_active_assets(client, db_session, factory, auth_headers):
    admin_user = factory.user(username="admin", admin_role="superadmin", is_admin=True)
    source = factory.user(username="source", role="producer")
    target = factory.user(username="target", role="producer")
    mastering = factory.user(username="master", role="producer")

    circle = Circle(name="Ops Circle", description=None, website=None, created_by=source.id)
    db_session.add(circle)
    db_session.flush()
    db_session.add(CircleMember(circle_id=circle.id, user_id=source.id, role="owner"))
    db_session.commit()

    album = factory.album(producer=source, mastering_engineer=mastering, members=[source], title="Owned Album")
    factory.track(album=album, submitter=source, status="intake")

    delete_before = client.delete(
        f"/api/admin/users/{source.id}",
        headers=auth_headers(admin_user),
    )
    assert delete_before.status_code == 409

    transfer = client.post(
        f"/api/admin/users/{source.id}/transfer-ownership",
        headers=auth_headers(admin_user),
        json={"target_user_id": target.id, "reason": "Owner left"},
    )
    assert transfer.status_code == 200
    assert transfer.json()["albums"] == 1
    assert transfer.json()["circles"] == 1

    delete_after = client.delete(
        f"/api/admin/users/{source.id}",
        headers=auth_headers(admin_user),
    )
    assert delete_after.status_code == 204


def test_admin_audit_log_returns_user_governance_actions(client, factory, auth_headers):
    admin_user = factory.user(username="admin", admin_role="operator", is_admin=True)
    target = factory.user(username="target")

    client.post(
        f"/api/admin/users/{target.id}/suspend",
        headers=auth_headers(admin_user),
        json={"reason": "Security incident"},
    )

    response = client.get("/api/admin/audit-log", headers=auth_headers(admin_user))
    assert response.status_code == 200
    assert response.json()[0]["action"] == "user_suspended"
    assert response.json()[0]["target_user"]["id"] == target.id


def test_admin_album_tracks_include_archived_and_rejected(client, db_session, factory, auth_headers):
    admin_user = factory.user(username="admin", admin_role="viewer", is_admin=True)
    producer = factory.user(username="producer", role="producer")
    mastering = factory.user(username="master", role="producer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[producer], title="Mixed Album")

    active_track = factory.track(album=album, submitter=producer, status="intake")
    rejected_track = factory.track(album=album, submitter=producer, status=TrackStatus.REJECTED.value)
    archived_track = factory.track(album=album, submitter=producer, status=TrackStatus.COMPLETED.value)
    archived_track.archived_at = active_track.created_at
    db_session.commit()

    response = client.get(
        f"/api/admin/albums/{album.id}/tracks",
        headers=auth_headers(admin_user),
    )
    assert response.status_code == 200
    returned_ids = {item["id"] for item in response.json()}
    assert {active_track.id, rejected_track.id, archived_track.id} <= returned_ids


def test_dashboard_exposes_reopen_and_audit_metrics(client, db_session, factory, auth_headers):
    admin_user = factory.user(username="admin", admin_role="operator", is_admin=True)
    producer = factory.user(username="producer", role="producer")
    mastering = factory.user(username="master", role="producer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[producer], title="Metrics Album")
    completed_track = factory.track(album=album, submitter=producer, status=TrackStatus.COMPLETED.value)

    db_session.add(
        ReopenRequest(
            track_id=completed_track.id,
            requested_by_id=producer.id,
            target_stage_id="intake",
            reason="Need revisions",
            status="pending",
        )
    )
    db_session.commit()

    client.post(
        f"/api/admin/users/{producer.id}/suspend",
        headers=auth_headers(admin_user),
        json={"reason": "Temporary hold"},
    )

    response = client.get("/api/admin/dashboard", headers=auth_headers(admin_user))
    assert response.status_code == 200
    body = response.json()
    assert body["pending_reopen_requests"] == 1
    assert body["suspended_users"] == 1
    assert body["recent_audits"][0]["action"] == "user_suspended"
