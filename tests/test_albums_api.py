import copy
import json
from datetime import datetime, timedelta, timezone

from app.models.circle import CircleMember
from app.routers import albums as albums_router
from app.models.issue import IssuePhase, IssueStatus
from app.models.track import TrackStatus
from app.models.workflow_event import WorkflowEvent
from app.workflow_defaults import DEFAULT_WORKFLOW_CONFIG


def test_create_album(client, factory, auth_headers):
    user = factory.user(role="producer")
    response = client.post(
        "/api/albums",
        headers=auth_headers(user),
        json={"title": "My Album", "description": "desc"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "My Album"
    assert body["producer_id"] == user.id
    assert any(m["user_id"] == user.id for m in body["members"])


def test_create_album_applies_team_deadlines_and_template_atomically(
    client, db_session, factory, auth_headers
):
    producer = factory.user(role="producer")
    mastering = factory.user(username="mastering")
    member = factory.user(username="member")

    circle_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Back Kitchen", "description": "desc"},
    )
    assert circle_response.status_code == 201
    circle_id = circle_response.json()["id"]

    db_session.add_all(
        [
            CircleMember(circle_id=circle_id, user_id=mastering.id, role="member"),
            CircleMember(circle_id=circle_id, user_id=member.id, role="member"),
        ]
    )
    db_session.commit()

    template_config = copy.deepcopy(DEFAULT_WORKFLOW_CONFIG)
    template_config["steps"][0]["label"] = "Circle Intake"
    template_response = client.post(
        f"/api/circles/{circle_id}/workflow-templates",
        headers=auth_headers(producer),
        json={
            "name": "Circle Template",
            "description": "desc",
            "workflow_config": template_config,
        },
    )
    assert template_response.status_code == 201
    template_id = template_response.json()["id"]

    response = client.post(
        "/api/albums",
        headers=auth_headers(producer),
        json={
            "title": "Atomic Album",
            "circle_id": circle_id,
            "workflow_template_id": template_id,
            "mastering_engineer_id": mastering.id,
            "member_ids": [member.id],
            "deadline": "2025-01-10T00:00:00Z",
            "phase_deadlines": {
                "peer_review": "2025-01-05T00:00:00Z",
                "mastering": "2025-01-07T00:00:00Z",
            },
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["circle_id"] == circle_id
    assert body["circle_name"] == "Back Kitchen"
    assert body["mastering_engineer_id"] == mastering.id
    assert body["deadline"] == "2025-01-10T00:00:00"
    assert body["phase_deadlines"] == {
        "peer_review": "2025-01-05T00:00:00Z",
        "mastering": "2025-01-07T00:00:00Z",
    }
    assert body["workflow_template_id"] == template_id
    assert body["workflow_config"]["steps"][0]["label"] == "Circle Intake"
    assert {member["user_id"] for member in body["members"]} == {producer.id, member.id}


def test_create_album_rejects_non_circle_team_members(client, factory, auth_headers):
    producer = factory.user(role="producer")
    outsider = factory.user(username="outsider")

    circle_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Circle One", "description": "desc"},
    )
    assert circle_response.status_code == 201

    response = client.post(
        "/api/albums",
        headers=auth_headers(producer),
        json={
            "title": "Circle Album",
            "circle_id": circle_response.json()["id"],
            "member_ids": [outsider.id],
        },
    )

    assert response.status_code == 400
    assert "not members of this album's circle" in response.text


def test_list_albums_visibility(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    member = factory.user()
    outsider = factory.user(username="outsider")
    factory.album(producer=producer, mastering_engineer=mastering, members=[member])

    assert len(client.get("/api/albums", headers=auth_headers(producer)).json()) == 1
    assert len(client.get("/api/albums", headers=auth_headers(member)).json()) == 1
    assert len(client.get("/api/albums", headers=auth_headers(mastering)).json()) == 1
    assert len(client.get("/api/albums", headers=auth_headers(outsider)).json()) == 0


def test_get_album(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    response = client.get(f"/api/albums/{album.id}", headers=auth_headers(producer))
    assert response.status_code == 200
    assert response.json()["id"] == album.id


def test_get_album_forbidden_for_outsider(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    response = client.get(f"/api/albums/{album.id}", headers=auth_headers(outsider))
    assert response.status_code == 403


def test_get_album_not_found(client, factory, auth_headers):
    user = factory.user()
    response = client.get("/api/albums/99999", headers=auth_headers(user))
    assert response.status_code == 404


def test_upload_album_cover_replaces_old_file(client, db_session, factory, auth_headers, upload_dir):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    covers_dir = upload_dir / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    old_file = covers_dir / "old.png"
    old_file.write_bytes(b"old-cover")
    album.cover_image = "covers/old.png"
    db_session.commit()

    response = client.post(
        f"/api/albums/{album.id}/cover",
        headers=auth_headers(producer),
        files={"file": ("cover.png", b"new-cover", "image/png")},
    )

    db_session.expire_all()
    refreshed_album = db_session.get(type(album), album.id)

    assert response.status_code == 200
    assert refreshed_album is not None
    assert refreshed_album.cover_image is not None
    assert refreshed_album.cover_image.startswith("covers/")
    assert refreshed_album.cover_image != "covers/old.png"
    assert old_file.exists() is False
    assert (upload_dir / refreshed_album.cover_image).exists()


def test_upload_album_cover_rejects_invalid_type_and_extension(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    album = factory.album(producer=producer, mastering_engineer=mastering)

    bad_type = client.post(
        f"/api/albums/{album.id}/cover",
        headers=auth_headers(producer),
        files={"file": ("cover.txt", b"not-image", "text/plain")},
    )
    bad_extension = client.post(
        f"/api/albums/{album.id}/cover",
        headers=auth_headers(producer),
        files={"file": ("cover.bmp", b"fake-image", "image/bmp")},
    )

    assert bad_type.status_code == 400
    assert bad_extension.status_code == 400


def test_upload_album_cover_uses_dedicated_cover_limit(client, factory, auth_headers, monkeypatch):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    album = factory.album(producer=producer, mastering_engineer=mastering)

    monkeypatch.setattr(albums_router, "MAX_ALBUM_COVER_UPLOAD_SIZE", 2 * 1024 * 1024)

    response = client.post(
        f"/api/albums/{album.id}/cover",
        headers=auth_headers(producer),
        files={"file": ("cover.png", b"x" * int(1.5 * 1024 * 1024), "image/png")},
    )

    assert response.status_code == 200
    assert response.json()["cover_image"].startswith("covers/")


def test_update_album_team(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    new_member = factory.user(username="new_member")
    new_mastering = factory.user(role="mastering_engineer", username="new_me")
    album = factory.album(producer=producer, mastering_engineer=mastering)

    response = client.patch(
        f"/api/albums/{album.id}/team",
        headers=auth_headers(producer),
        json={
            "mastering_engineer_id": new_mastering.id,
            "member_ids": [producer.id, new_member.id],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mastering_engineer_id"] == new_mastering.id
    member_user_ids = {m["user_id"] for m in body["members"]}
    assert new_member.id in member_user_ids
    assert producer.id in member_user_ids


def test_update_album_team_forbidden_for_non_producer(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    member = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[member])

    response = client.patch(
        f"/api/albums/{album.id}/team",
        headers=auth_headers(member),
        json={"member_ids": [member.id]},
    )
    assert response.status_code == 403


def test_album_stats(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])

    track1 = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)
    factory.track(album=album, submitter=submitter, status=TrackStatus.COMPLETED)
    sv = track1.source_versions[-1]
    factory.issue(
        track=track1,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.OPEN,
        source_version_id=sv.id,
    )

    response = client.get(f"/api/albums/{album.id}/stats", headers=auth_headers(producer))
    assert response.status_code == 200
    body = response.json()
    assert body["total_tracks"] == 2
    assert body["open_issues"] == 1
    assert "peer_review" in body["by_status"]
    assert "completed" in body["by_status"]


def test_list_albums_returns_summary_fields(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(username="mastering")
    member = factory.user(username="member")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[member])
    album.deadline = datetime(2025, 1, 10, tzinfo=timezone.utc)
    album.phase_deadlines = json.dumps(
        {"peer_review": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}
    )
    db_session.commit()

    track1 = factory.track(album=album, submitter=member, status="peer_review")
    track2 = factory.track(album=album, submitter=member, status=TrackStatus.COMPLETED)
    source_version = track1.source_versions[-1]
    factory.issue(
        track=track1,
        author=producer,
        phase=IssuePhase.PEER,
        status=IssueStatus.OPEN,
        source_version_id=source_version.id,
    )
    db_session.add_all(
        [
            WorkflowEvent(
                track_id=track1.id,
                album_id=album.id,
                actor_user_id=producer.id,
                event_type="track_submitted",
                to_status="peer_review",
            ),
            WorkflowEvent(
                track_id=track2.id,
                album_id=album.id,
                actor_user_id=mastering.id,
                event_type="master_delivered",
                to_status="completed",
            ),
        ]
    )
    db_session.commit()

    response = client.get("/api/albums", headers=auth_headers(producer))
    assert response.status_code == 200
    body = response.json()[0]

    assert body["track_count"] == 2
    assert body["total_tracks"] == 2
    assert body["by_status"]["peer_review"] == 1
    assert body["by_status"]["completed"] == 1
    assert body["open_issues"] == 1
    assert body["overdue_track_count"] == 1
    assert len(body["recent_events"]) == 2
    assert body["recent_events"][0]["event_type"] in {"track_submitted", "master_delivered"}


def test_list_album_tracks(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    factory.track(album=album, submitter=submitter)
    factory.track(album=album, submitter=submitter)

    response = client.get(f"/api/albums/{album.id}/tracks", headers=auth_headers(producer))
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_list_album_tracks_forbidden_for_outsider(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering)

    response = client.get(f"/api/albums/{album.id}/tracks", headers=auth_headers(outsider))
    assert response.status_code == 403


def test_create_album_sets_default_workflow_config(client, factory, auth_headers):
    producer = factory.user(role="producer")

    response = client.post(
        "/api/albums",
        headers=auth_headers(producer),
        json={"title": "Workflow Album", "description": "desc"},
    )

    assert response.status_code == 201
    body = response.json()
    normalized_response = json.loads(json.dumps(body["workflow_config"]))
    normalized_default = json.loads(json.dumps(DEFAULT_WORKFLOW_CONFIG))

    for step in normalized_response.get("steps", []):
        for key in [
            "ui_variant",
            "return_to",
            "revision_step",
            "allow_permanent_reject",
            "assignment_mode",
            "reviewer_pool",
            "required_reviewer_count",
            "assignee_user_id",
            "require_confirmation",
            "actor_roles",
        ]:
            if step.get(key) is None:
                step.pop(key, None)

    assert normalized_response == normalized_default


def test_update_workflow_rejects_forward_reject_to_target(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[producer])

    bad_config = copy.deepcopy(DEFAULT_WORKFLOW_CONFIG)
    for step in bad_config["steps"]:
        if step["id"] == "producer_gate":
            step["transitions"]["reject_to_forward"] = "mastering"
            break

    response = client.put(
        f"/api/albums/{album.id}/workflow",
        headers=auth_headers(producer),
        json=bad_config,
    )

    assert response.status_code == 422
    assert "must target an earlier step" in response.text
