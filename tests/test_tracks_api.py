from pathlib import Path

from sqlalchemy import select

from app.models.issue import IssuePhase, IssueStatus
from app.models.master_delivery import MasterDelivery
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
    assert body["status"] == TrackStatus.SUBMITTED.value
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
    assert body["status"] == TrackStatus.SUBMITTED.value
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


def test_finish_peer_review_requires_checklist_and_advances_state(client, db_session, factory, auth_headers):
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

    failure = client.post(
        f"/api/tracks/{track.id}/peer-review/finish",
        headers=auth_headers(reviewer),
        json={"decision": "pass"},
    )
    assert failure.status_code == 409

    source_version = db_session.scalars(
        select(TrackSourceVersion).where(TrackSourceVersion.track_id == track.id)
    ).first()
    factory.checklist(track=track, reviewer=reviewer, source_version_id=source_version.id)

    success = client.post(
        f"/api/tracks/{track.id}/peer-review/finish",
        headers=auth_headers(reviewer),
        json={"decision": "pass"},
    )
    assert success.status_code == 200
    assert success.json()["status"] == TrackStatus.PRODUCER_MASTERING_GATE.value


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
    assert response.json()["status"] == TrackStatus.FINAL_REVIEW.value
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


def test_return_to_mastering_requires_open_final_review_issue(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.FINAL_REVIEW)
    delivery = factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)

    failure = client.post(
        f"/api/tracks/{track.id}/final-review/return",
        headers=auth_headers(producer),
    )
    assert failure.status_code == 409

    factory.issue(
        track=track,
        author=producer,
        phase=IssuePhase.FINAL_REVIEW,
        status=IssueStatus.OPEN,
        master_delivery_id=delivery.id,
    )

    success = client.post(
        f"/api/tracks/{track.id}/final-review/return",
        headers=auth_headers(producer),
    )
    assert success.status_code == 200
    assert success.json()["status"] == TrackStatus.MASTERING.value


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
