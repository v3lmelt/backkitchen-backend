import json
from pathlib import Path

from sqlalchemy import select

from app.models.master_delivery import MasterDelivery
from app.models.stage_assignment import StageAssignment
from app.models.track import RejectionMode, Track, TrackStatus
from app.models.track_source_version import TrackSourceVersion
from app.models.workflow_event import WorkflowEvent


def test_create_track_creates_source_version_and_event(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])

    response = client.post(
        "/api/tracks",
        headers=auth_headers(submitter),
        data={"title": "New Song", "artist": "Nova", "album_id": str(album.id), "bpm": "172"},
        files={"file": ("demo.wav", b"RIFFdata", "audio/wav")},
    )

    assert response.status_code == 201
    body = response.json()
    # Default workflow's first step is ``intake`` (replaces the legacy
    # ``submitted`` status).
    assert body["status"] == "intake"
    assert body["version"] == 1
    assert body["workflow_cycle"] == 1
    track_id = body["id"]

    versions = db_session.scalars(
        select(TrackSourceVersion).where(TrackSourceVersion.track_id == track_id)
    ).all()
    assert len(versions) == 1
    assert versions[0].version_number == 1

    events = db_session.scalars(
        select(WorkflowEvent).where(WorkflowEvent.track_id == track_id)
    ).all()
    assert [event.event_type for event in events] == ["track_submitted"]


def test_list_tracks_respects_submitter_and_reviewer_visibility(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[reviewer])
    submitter_track = factory.track(album=album, submitter=submitter)
    reviewer_track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.PEER_REVIEW,
        peer_reviewer=reviewer,
    )

    submitter_response = client.get("/api/tracks", headers=auth_headers(submitter))
    reviewer_response = client.get(
        "/api/tracks",
        headers=auth_headers(reviewer),
        params={"status": TrackStatus.PEER_REVIEW.value, "album_id": album.id},
    )
    outsider_response = client.get("/api/tracks", headers=auth_headers(outsider))

    assert submitter_response.status_code == 200
    assert {item["id"] for item in submitter_response.json()} == {submitter_track.id, reviewer_track.id}
    assert reviewer_response.status_code == 200
    assert [item["id"] for item in reviewer_response.json()] == [reviewer_track.id]
    assert outsider_response.status_code == 200
    assert outsider_response.json() == []


def test_upload_source_version_resubmittable_resets_cycle_and_assignment(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.REJECTED,
        rejection_mode=RejectionMode.RESUBMITTABLE,
        peer_reviewer=reviewer,
    )

    response = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(submitter),
        files={"file": ("revision.wav", b"RIFFrevision", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    # Resubmit sends the track back to the first step of the default workflow.
    assert body["status"] == "intake"
    assert body["version"] == 2
    assert body["workflow_cycle"] == 2
    assert body["peer_reviewer_id"] is None
    assert body["rejection_mode"] is None

    db_session.refresh(track)
    versions = db_session.scalars(
        select(TrackSourceVersion).where(TrackSourceVersion.track_id == track.id)
    ).all()
    assert len(versions) == 2
    assert max(version.workflow_cycle for version in versions) == 2


def test_upload_master_delivery_increments_delivery_number(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.MASTERING)
    factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)

    response = client.post(
        f"/api/tracks/{track.id}/master-deliveries",
        headers=auth_headers(mastering),
        files={"file": ("master.mp3", b"ID3master", "audio/mpeg")},
    )

    assert response.status_code == 200
    # The default workflow's ``mastering`` step has ``require_confirmation``
    # set, so the track stays put until the mastering engineer confirms.
    assert response.json()["status"] == TrackStatus.MASTERING.value
    deliveries = db_session.scalars(
        select(MasterDelivery).where(MasterDelivery.track_id == track.id)
    ).all()
    assert sorted(delivery.delivery_number for delivery in deliveries) == [1, 2]


def test_final_review_requires_two_approvals_to_complete(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.FINAL_REVIEW)
    delivery = factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)

    producer_response = client.post(
        f"/api/tracks/{track.id}/final-review/approve",
        headers=auth_headers(producer),
    )
    assert producer_response.status_code == 200
    assert producer_response.json()["status"] == TrackStatus.FINAL_REVIEW.value

    submitter_response = client.post(
        f"/api/tracks/{track.id}/final-review/approve",
        headers=auth_headers(submitter),
    )
    assert submitter_response.status_code == 200
    assert submitter_response.json()["status"] == TrackStatus.COMPLETED.value

    db_session.refresh(delivery)
    assert delivery.producer_approved_at is not None
    assert delivery.submitter_approved_at is not None


def test_submitter_can_request_reopen_to_mastering_after_completion(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.COMPLETED)

    response = client.post(
        f"/api/tracks/{track.id}/reopen-request",
        headers=auth_headers(submitter),
        json={"target_stage_id": "mastering", "reason": "Need another mastering pass."},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["track_id"] == track.id
    assert body["target_stage_id"] == "mastering"
    assert body["status"] == "pending"


def test_producer_can_direct_reopen_completed_track_to_mastering(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.COMPLETED)

    response = client.post(
        f"/api/tracks/{track.id}/reopen",
        headers=auth_headers(producer),
        json={"target_stage_id": "mastering"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == TrackStatus.MASTERING.value
    assert body["workflow_cycle"] == track.workflow_cycle + 1


def test_get_track_detail(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.PEER_REVIEW,
        peer_reviewer=reviewer,
    )

    response = client.get(f"/api/tracks/{track.id}", headers=auth_headers(submitter))
    assert response.status_code == 200
    body = response.json()
    assert body["track"]["id"] == track.id
    assert "issues" in body
    assert "checklist_items" in body
    assert "events" in body
    assert "source_versions" in body


def test_get_track_not_found(client, factory, auth_headers):
    user = factory.user()
    response = client.get("/api/tracks/99999", headers=auth_headers(user))
    assert response.status_code == 404


def test_upload_source_version_from_peer_revision(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.PEER_REVISION,
        peer_reviewer=reviewer,
    )

    response = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(submitter),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == TrackStatus.PEER_REVIEW.value
    assert body["version"] == 2


def test_upload_source_version_from_mastering_revision(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.MASTERING_REVISION,
    )

    response = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(submitter),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == TrackStatus.MASTERING.value
    assert body["version"] == 2


def test_upload_source_version_wrong_status_fails(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.PEER_REVIEW)

    response = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(submitter),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )
    assert response.status_code == 409


def test_upload_source_version_forbidden_for_non_submitter(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.PEER_REVISION)

    response = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(producer),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )
    assert response.status_code == 403


def test_delete_track_forbidden_for_outsider(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, outsider])
    track = factory.track(album=album, submitter=submitter)

    response = client.delete(f"/api/tracks/{track.id}", headers=auth_headers(outsider))
    assert response.status_code == 403


def test_delete_track_removes_audio_file(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter)
    track_id = track.id
    audio_path = Path(track.file_path)

    response = client.delete(f"/api/tracks/{track_id}", headers=auth_headers(submitter))

    assert response.status_code == 204
    db_session.expire_all()
    assert db_session.get(Track, track_id) is None
    assert not audio_path.exists()


def test_assign_reviewer_rejects_non_album_member(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    valid_reviewer = factory.user(username="valid_reviewer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, valid_reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.PEER_REVIEW,
        peer_reviewer=valid_reviewer,
    )

    response = client.post(
        f"/api/tracks/{track.id}/assign-reviewer",
        headers=auth_headers(producer),
        json={"user_ids": [outsider.id]},
    )

    assert response.status_code == 400
    assert "not members" in response.text


def test_reassign_reviewer_rejects_non_album_member(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    current_reviewer = factory.user(username="reviewer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, current_reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.PEER_REVIEW,
        peer_reviewer=current_reviewer,
    )

    response = client.post(
        f"/api/tracks/{track.id}/reassign-reviewer",
        headers=auth_headers(producer),
        json={"user_id": outsider.id},
    )

    assert response.status_code == 400
    assert "not a member" in response.text


def test_upload_source_version_custom_revision_requires_assigned_user(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, outsider])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.SUBMITTED, peer_reviewer=None)

    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "custom_revision",
                    "label": "Custom Revision",
                    "type": "revision",
                    "ui_variant": "generic",
                    "assignee_role": "submitter",
                    "order": 0,
                    "return_to": "final_gate",
                    "transitions": {},
                },
                {
                    "id": "final_gate",
                    "label": "Final Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 1,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    track.status = "custom_revision"
    db_session.add(
        StageAssignment(
            track_id=track.id,
            stage_id="custom_revision",
            user_id=outsider.id,
            status="pending",
        )
    )
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(outsider),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )

    assert response.status_code == 403
    assert "assigned user" in response.text


def test_upload_master_delivery_custom_delivery_requires_assigned_user(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, outsider])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.SUBMITTED, peer_reviewer=None)

    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "custom_delivery",
                    "label": "Custom Delivery",
                    "type": "delivery",
                    "ui_variant": "generic",
                    "assignee_role": "mastering_engineer",
                    "order": 0,
                    "transitions": {"deliver": "final_gate"},
                    "require_confirmation": False,
                },
                {
                    "id": "final_gate",
                    "label": "Final Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 1,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    track.status = "custom_delivery"
    db_session.add(
        StageAssignment(
            track_id=track.id,
            stage_id="custom_delivery",
            user_id=outsider.id,
            status="pending",
        )
    )
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/master-deliveries",
        headers=auth_headers(outsider),
        files={"file": ("master.mp3", b"ID3master", "audio/mpeg")},
    )

    assert response.status_code == 403
    assert "assigned user" in response.text


def test_create_issue_custom_step_rejects_mismatched_phase(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.SUBMITTED, peer_reviewer=None)

    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "custom_review",
                    "label": "Custom Review",
                    "type": "review",
                    "ui_variant": "generic",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "transitions": {"pass": "final_gate"},
                    "assignment_mode": "manual",
                    "required_reviewer_count": 1,
                },
                {
                    "id": "final_gate",
                    "label": "Final Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 1,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    track.status = "custom_review"
    db_session.add(
        StageAssignment(
            track_id=track.id,
            stage_id="custom_review",
            user_id=reviewer.id,
            status="pending",
        )
    )
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Wrong phase",
            "description": "Phase should be rejected",
            "phase": "peer",
            "severity": "major",
            "markers": [],
        },
    )

    assert response.status_code == 400
    assert "must match current workflow step" in response.text
