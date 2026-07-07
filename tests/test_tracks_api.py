import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from app.config import settings
from app.models.comment import Comment
from app.models.circle import CircleMember
from app.models.discussion import TrackDiscussion, TrackDiscussionAudio
from app.models.issue import IssuePhase, IssueStatus
from app.models.issue_image import IssueImage
from app.models.master_delivery import MasterDelivery
from app.models.source_followup_request import SourceFollowupRequest
from app.models.stage_assignment import StageAssignment
from app.models.track import RejectionMode, Track, TrackStatus
from app.models.track_composer import TrackExternalComposer
from app.models.track_playback_preference import TrackPlaybackPreference
from app.models.track_source_version import TrackSourceVersion
from app.models.workflow_event import WorkflowEvent
from app.security import create_access_token
from app.workflow_defaults import DEFAULT_WORKFLOW_CONFIG


def _workflow_config_with_fixed_peer_reviewer(reviewer_id: int) -> dict:
    config = json.loads(json.dumps(DEFAULT_WORKFLOW_CONFIG))
    for step in config["steps"]:
        if step["id"] == "peer_review":
            step["assignment_mode"] = "fixed"
            step["reviewer_pool"] = [reviewer_id]
            step["required_reviewer_count"] = 1
            break
    return config


def _transition(client, auth_headers, track_id: int, actor, decision: str, *, revision_type: str | None = None) -> dict:
    payload = {"decision": decision}
    if revision_type is not None:
        payload["revision_type"] = revision_type
    response = client.post(
        f"/api/tracks/{track_id}/workflow/transition",
        headers=auth_headers(actor),
        json=payload,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _start_uploaded_track_through_mastering(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer],
        workflow_config=_workflow_config_with_fixed_peer_reviewer(reviewer.id),
    )

    created = client.post(
        "/api/tracks",
        headers=auth_headers(submitter),
        data={
            "title": "Full Flow Song",
            "artist": "Flow Unit",
            "album_id": str(album.id),
            "composer_ids": [str(submitter.id)],
        },
        files={"file": ("demo.wav", b"RIFFdata", "audio/wav")},
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    assert created_body["status"] == "intake"
    track_id = created_body["id"]

    intake = _transition(client, auth_headers, track_id, producer, "accept")
    assert intake["status"] == "peer_review"
    assert intake["peer_reviewer_id"] == reviewer.id

    checklist = client.post(
        f"/api/tracks/{track_id}/checklist",
        headers=auth_headers(reviewer),
        json={"items": [{"label": "Balance", "passed": True}]},
    )
    assert checklist.status_code == 201, checklist.text

    peer_review = _transition(client, auth_headers, track_id, reviewer, "pass")
    assert peer_review["status"] == "producer_gate"

    producer_gate = _transition(client, auth_headers, track_id, producer, "approve")
    assert producer_gate["status"] == "mastering"

    return producer, mastering, submitter, track_id


def test_co_producer_can_execute_producer_workflow_transition(client, db_session, factory, auth_headers):
    owner = factory.user(role="producer")
    co_producer = factory.user(username="co")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    create_response = client.post(
        "/api/circles",
        headers=auth_headers(owner),
        json={"name": "Circle One", "description": "desc"},
    )
    circle_id = create_response.json()["id"]
    db_session.add_all([
        CircleMember(circle_id=circle_id, user_id=co_producer.id, role="co_producer"),
        CircleMember(circle_id=circle_id, user_id=mastering.id, role="mastering_engineer"),
        CircleMember(circle_id=circle_id, user_id=submitter.id, role="member"),
    ])
    album = factory.album(producer=owner, mastering_engineer=mastering, members=[submitter])
    album.circle_id = circle_id
    track = factory.track(album=album, submitter=submitter, status="intake")
    db_session.commit()

    detail_response = client.get(f"/api/tracks/{track.id}", headers=auth_headers(co_producer))
    assert detail_response.status_code == 200
    detail_body = detail_response.json()["track"]
    assert detail_body["viewer_is_album_manager"] is True
    assert "accept_producer_direct" in detail_body["allowed_actions"]

    response = _transition(client, auth_headers, track.id, co_producer, "accept_producer_direct")

    assert response["status"] == "producer_gate"


def _upload_confirm_and_approve_master(client, auth_headers, track_id: int, producer, mastering, submitter) -> dict:
    delivery = client.post(
        f"/api/tracks/{track_id}/master-deliveries",
        headers=auth_headers(mastering),
        files={"file": ("master.mp3", b"ID3master", "audio/mpeg")},
    )
    assert delivery.status_code == 200, delivery.text
    delivery_body = delivery.json()
    assert delivery_body["status"] == "mastering"
    delivery_id = delivery_body["current_master_delivery"]["id"]

    confirmed = client.post(
        f"/api/tracks/{track_id}/master-deliveries/{delivery_id}/confirm",
        headers=auth_headers(mastering),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "final_review"

    producer_approval = client.post(
        f"/api/tracks/{track_id}/final-review/approve",
        headers=auth_headers(producer),
    )
    assert producer_approval.status_code == 200, producer_approval.text
    assert producer_approval.json()["status"] == "final_review"

    submitter_approval = client.post(
        f"/api/tracks/{track_id}/final-review/approve",
        headers=auth_headers(submitter),
    )
    assert submitter_approval.status_code == 200, submitter_approval.text
    assert submitter_approval.json()["status"] == "completed"
    return submitter_approval.json()


def test_create_track_creates_source_version_and_event(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])

    response = client.post(
        "/api/tracks",
        headers=auth_headers(submitter),
        data={
            "title": "New Song",
            "artist": "Nova",
            "album_id": str(album.id),
            "bpm": "172",
            "composer_ids": [str(submitter.id)],
        },
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


def test_create_track_requires_explicit_composer_binding(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])

    response = client.post(
        "/api/tracks",
        headers=auth_headers(submitter),
        data={"title": "New Song", "artist": "Nova", "album_id": str(album.id)},
        files={"file": ("demo.wav", b"RIFFdata", "audio/wav")},
    )

    assert response.status_code == 422
    assert "At least one platform composer or external composer is required" in response.text


def test_create_proxy_track_as_producer_records_external_submitter(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    album = factory.album(producer=producer, mastering_engineer=mastering)

    response = client.post(
        "/api/tracks",
        headers=auth_headers(producer),
        data={
            "title": "Proxy Song",
            "artist": "Offline Composer",
            "album_id": str(album.id),
            "proxy_submission": "true",
            "external_submitter_name": "Offline Composer",
        },
        files={"file": ("demo.wav", b"RIFFdata", "audio/wav")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["submitter_id"] == producer.id
    assert body["proxy_uploader_id"] == producer.id
    assert body["external_submitter_name"] == "Offline Composer"
    assert body["external_composer_names"] == ["Offline Composer"]
    assert [item["name"] for item in body["external_composers"]] == ["Offline Composer"]
    assert body["composer_ids"] == []
    assert body["is_proxy_submission"] is True
    assert body["submitter"]["id"] == producer.id
    assert body["proxy_uploader"]["id"] == producer.id

    track = db_session.get(Track, body["id"])
    assert track.submitter_id == producer.id
    assert track.proxy_uploader_id == producer.id
    assert track.external_submitter_name == "Offline Composer"
    external_names = db_session.scalars(
        select(TrackExternalComposer.name).where(TrackExternalComposer.track_id == track.id)
    ).all()
    assert external_names == ["Offline Composer"]
    assert track.source_versions[0].uploaded_by_id == producer.id


def test_proxy_track_forbidden_for_non_producer(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    member = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[member])

    response = client.post(
        "/api/tracks",
        headers=auth_headers(member),
        data={
            "title": "Proxy Song",
            "artist": "Offline Composer",
            "album_id": str(album.id),
            "proxy_submission": "true",
            "external_submitter_name": "Offline Composer",
        },
        files={"file": ("demo.wav", b"RIFFdata", "audio/wav")},
    )

    assert response.status_code == 403


def test_list_tracks_respects_submitter_and_reviewer_visibility(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[reviewer])
    submitter_track = factory.track(album=album, submitter=submitter)
    submitter_track.original_title = "Bad Apple!!"
    submitter_track.original_artist = "Touhou"
    db_session.commit()
    reviewer_track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
        peer_reviewer=reviewer,
    )

    submitter_response = client.get("/api/tracks", headers=auth_headers(submitter))
    reviewer_response = client.get(
        "/api/tracks",
        headers=auth_headers(reviewer),
        params={"status": "peer_review", "album_id": album.id},
    )
    outsider_response = client.get("/api/tracks", headers=auth_headers(outsider))

    assert submitter_response.status_code == 200
    assert {item["id"] for item in submitter_response.json()} == {submitter_track.id, reviewer_track.id}
    submitter_item = next(item for item in submitter_response.json() if item["id"] == submitter_track.id)
    assert submitter_item["original_title"] == "Bad Apple!!"
    assert submitter_item["original_artist"] == "Touhou"
    assert reviewer_response.status_code == 200
    assert [item["id"] for item in reviewer_response.json()] == [reviewer_track.id]
    assert outsider_response.status_code == 200
    assert outsider_response.json() == []


def test_create_track_accepts_active_non_member_composer_uid(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    uploader = factory.user(username="uploader")
    composer = factory.user(username="manual-uid-composer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[uploader])

    response = client.post(
        "/api/tracks",
        headers=auth_headers(uploader),
        data={
            "title": "Manual UID Song",
            "artist": "Manual Alias",
            "album_id": str(album.id),
            "composer_ids": [str(composer.id)],
        },
        files={"file": ("manual.wav", b"RIFFdata", "audio/wav")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["composer_ids"] == [composer.id]
    track_id = body["id"]

    track_list = client.get("/api/tracks", headers=auth_headers(composer))
    assert track_list.status_code == 200
    assert track_id in {item["id"] for item in track_list.json()}

    albums = client.get("/api/albums", headers=auth_headers(composer))
    assert albums.status_code == 200
    assert album.id not in {item["id"] for item in albums.json()}


def test_create_track_for_circle_album_restricts_platform_composers_to_circle(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    circle_composer = factory.user(username="circle-composer")
    outsider = factory.user(username="outsider-composer")
    circle_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Composer Circle", "description": "desc"},
    )
    circle_id = circle_response.json()["id"]
    db_session.add_all(
        [
            CircleMember(circle_id=circle_id, user_id=submitter.id, role="member"),
            CircleMember(circle_id=circle_id, user_id=circle_composer.id, role="member"),
        ]
    )
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    album.circle_id = circle_id
    db_session.commit()

    rejected = client.post(
        "/api/tracks",
        headers=auth_headers(submitter),
        data={
            "title": "Outside Composer",
            "artist": "Nova",
            "album_id": str(album.id),
            "composer_ids": [str(outsider.id)],
        },
        files={"file": ("outside.wav", b"RIFFdata", "audio/wav")},
    )
    accepted = client.post(
        "/api/tracks",
        headers=auth_headers(submitter),
        data={
            "title": "Circle Composer",
            "artist": "Nova",
            "album_id": str(album.id),
            "composer_ids": [str(circle_composer.id)],
        },
        files={"file": ("circle.wav", b"RIFFdata", "audio/wav")},
    )

    assert rejected.status_code == 422
    assert "not members of this circle" in rejected.text
    assert accepted.status_code == 201
    assert accepted.json()["composer_ids"] == [circle_composer.id]


def test_r2_track_upload_for_circle_album_rejects_non_circle_composer(client, db_session, factory, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "R2_ENABLED", True)
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    outsider = factory.user(username="outsider-composer")
    circle_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "R2 Composer Circle", "description": "desc"},
    )
    circle_id = circle_response.json()["id"]
    db_session.add(CircleMember(circle_id=circle_id, user_id=submitter.id, role="member"))
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    album.circle_id = circle_id
    db_session.commit()

    request_response = client.post(
        "/api/tracks/request-upload",
        headers=auth_headers(submitter),
        json={
            "filename": "outside.wav",
            "content_type": "audio/wav",
            "file_size": 16,
            "album_id": album.id,
            "title": "Outside Composer",
            "artist": "Nova",
            "composer_ids": [outsider.id],
        },
    )
    confirm_response = client.post(
        "/api/tracks/confirm-upload",
        headers=auth_headers(submitter),
        json={
            "upload_id": "upload-1",
            "object_key": f"tracks/new/source/{submitter.id}/outside.wav",
            "album_id": album.id,
            "title": "Outside Composer",
            "artist": "Nova",
            "composer_ids": [outsider.id],
        },
    )

    assert request_response.status_code == 422
    assert "not members of this circle" in request_response.text
    assert confirm_response.status_code == 422
    assert "not members of this circle" in confirm_response.text


def test_proxy_submitter_identity_is_hidden_from_peer_reviewer(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[reviewer])
    track = factory.track(
        album=album,
        submitter=producer,
        status="peer_review",
        peer_reviewer=reviewer,
    )
    track.external_submitter_name = "Offline Composer"
    track.proxy_uploader_id = producer.id
    db_session.commit()

    response = client.get(f"/api/tracks/{track.id}", headers=auth_headers(reviewer))

    assert response.status_code == 200
    body = response.json()["track"]
    assert body["artist"] is None
    assert body["submitter"] is None
    assert body["proxy_uploader"] is None
    assert body["external_submitter_name"] is None
    assert body["is_proxy_submission"] is False


def test_list_tracks_hides_rejected_by_default_but_allows_explicit_rejected_filter(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    active_track = factory.track(album=album, submitter=submitter, status="peer_review")
    rejected_track = factory.track(album=album, submitter=submitter, status=TrackStatus.REJECTED)
    db_session.commit()

    default_response = client.get("/api/tracks", headers=auth_headers(submitter))
    rejected_response = client.get(
        "/api/tracks",
        headers=auth_headers(submitter),
        params={"status": TrackStatus.REJECTED.value},
    )

    assert default_response.status_code == 200
    assert {item["id"] for item in default_response.json()} == {active_track.id}
    assert rejected_response.status_code == 200
    assert [item["id"] for item in rejected_response.json()] == [rejected_track.id]


def test_list_tracks_includes_stage_assigned_reviewer_and_allowed_actions(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer_a, reviewer_b])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
        peer_reviewer=reviewer_a,
    )
    db_session.add(
        StageAssignment(
            track_id=track.id,
            stage_id="peer_review",
            user_id=reviewer_b.id,
            status="pending",
        )
    )
    db_session.commit()

    response = client.get(
        "/api/tracks",
        headers=auth_headers(reviewer_b),
        params={"status": "peer_review", "album_id": album.id},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == [track.id]
    assert {"pass", "needs_revision"}.issubset(set(body[0]["allowed_actions"]))


def test_list_tracks_includes_completed_stage_assignment_reviewer(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer_a, reviewer_b])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
        peer_reviewer=reviewer_a,
    )

    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "peer_review",
                    "label": "Peer Review",
                    "type": "review",
                    "ui_variant": "peer_review",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "transitions": {
                        "pass": "producer_gate",
                        "needs_revision": "peer_revision",
                    },
                    "assignment_mode": "manual",
                    "required_reviewer_count": 2,
                    "revision_step": "peer_revision",
                },
                {
                    "id": "peer_revision",
                    "label": "Peer Revision",
                    "type": "revision",
                    "assignee_role": "submitter",
                    "order": 1,
                    "return_to": "peer_review",
                    "transitions": {},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 2,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            StageAssignment(
                track_id=track.id,
                stage_id="peer_review",
                user_id=reviewer_a.id,
                status="pending",
                assigned_at=now,
            ),
            StageAssignment(
                track_id=track.id,
                stage_id="peer_review",
                user_id=reviewer_b.id,
                status="completed",
                assigned_at=now,
                completed_at=now,
                decision="pass",
            ),
        ]
    )
    db_session.commit()

    response = client.get(
        "/api/tracks",
        headers=auth_headers(reviewer_b),
        params={"status": "peer_review", "album_id": album.id},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == [track.id]
    assert body[0]["allowed_actions"] == []


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
    track = factory.track(album=album, submitter=submitter, status="mastering")
    factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)

    response = client.post(
        f"/api/tracks/{track.id}/master-deliveries",
        headers=auth_headers(mastering),
        files={"file": ("master.mp3", b"ID3master", "audio/mpeg")},
    )

    assert response.status_code == 200
    # The default workflow's ``mastering`` step has ``require_confirmation``
    # set, so the track stays put until the mastering engineer confirms.
    assert response.json()["status"] == "mastering"
    deliveries = db_session.scalars(
        select(MasterDelivery).where(MasterDelivery.track_id == track.id)
    ).all()
    assert sorted(delivery.delivery_number for delivery in deliveries) == [1, 2]



def test_upload_master_delivery_rejects_text_only_message(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")
    factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)

    response = client.post(
        f"/api/tracks/{track.id}/master-deliveries",
        headers=auth_headers(mastering),
        data={"delivery_message": "https://cloud.example/stems\n提取码: bk24"},
    )

    assert response.status_code == 422
    assert "requires an audio file" in response.text
    deliveries = db_session.scalars(
        select(MasterDelivery).where(MasterDelivery.track_id == track.id)
    ).all()
    assert sorted(delivery.delivery_number for delivery in deliveries) == [1]

def test_upload_master_delivery_rejects_empty_submission(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")

    response = client.post(
        f"/api/tracks/{track.id}/master-deliveries",
        headers=auth_headers(mastering),
        data={"delivery_message": " \n\t "},
    )

    assert response.status_code == 422
    assert "requires an audio file" in response.text


def test_upload_master_delivery_requires_assigned_user(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")

    response = client.post(
        f"/api/tracks/{track.id}/master-deliveries",
        headers=auth_headers(submitter),
        data={"delivery_message": "https://cloud.example/not-allowed"},
    )

    assert response.status_code == 403
    assert "assigned user" in response.text


def test_master_audio_rejects_text_only_delivery(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="final_review")
    factory.master_delivery(
        track=track,
        uploaded_by=mastering,
        delivery_kind="text",
        delivery_message="https://cloud.example/stems",
    )

    response = client.get(
        f"/api/tracks/{track.id}/master-audio",
        headers=auth_headers(producer),
    )

    assert response.status_code == 404
    assert "does not include an audio file" in response.text

def test_final_review_reject_to_mastering_keeps_cycle_and_clears_approvals(
    client,
    db_session,
    factory,
    auth_headers,
):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="final_review", workflow_cycle=3)
    delivery = factory.master_delivery(
        track=track,
        uploaded_by=mastering,
        workflow_cycle=3,
        delivery_number=1,
    )
    delivery.producer_approved_at = datetime.now(timezone.utc)
    delivery.submitter_approved_at = datetime.now(timezone.utc)
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(producer),
        json={"decision": "reject_to_mastering"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "mastering"
    assert body["workflow_cycle"] == 3
    db_session.refresh(delivery)
    assert delivery.producer_approved_at is None
    assert delivery.submitter_approved_at is None

    upload_response = client.post(
        f"/api/tracks/{track.id}/master-deliveries",
        headers=auth_headers(mastering),
        files={"file": ("master-v2.mp3", b"ID3master-v2", "audio/mpeg")},
    )

    assert upload_response.status_code == 200
    deliveries = db_session.scalars(
        select(MasterDelivery).where(MasterDelivery.track_id == track.id)
    ).all()
    assert sorted((item.workflow_cycle, item.delivery_number) for item in deliveries) == [
        (3, 1),
        (3, 2),
    ]


def test_track_playback_preference_defaults_to_zero_gain(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")

    response = client.get(
        f"/api/tracks/{track.id}/playback-preferences/source",
        headers=auth_headers(mastering),
    )

    assert response.status_code == 200
    assert response.json() == {
        "track_id": track.id,
        "user_id": mastering.id,
        "scope": "source",
        "gain_db": 0.0,
        "updated_at": None,
    }


def test_track_playback_preference_upserts_per_user(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")

    create_response = client.put(
        f"/api/tracks/{track.id}/playback-preferences/source",
        headers=auth_headers(mastering),
        json={"gain_db": 3.5},
    )
    update_response = client.put(
        f"/api/tracks/{track.id}/playback-preferences/source",
        headers=auth_headers(mastering),
        json={"gain_db": -1.5},
    )

    assert create_response.status_code == 200
    assert create_response.json()["gain_db"] == 3.5
    assert update_response.status_code == 200
    assert update_response.json()["gain_db"] == -1.5

    preferences = db_session.scalars(
        select(TrackPlaybackPreference).where(TrackPlaybackPreference.track_id == track.id)
    ).all()
    assert len(preferences) == 1
    assert preferences[0].user_id == mastering.id
    assert preferences[0].scope == "source"
    assert preferences[0].gain_db == -1.5


def test_track_playback_preference_isolated_between_users(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")

    write_response = client.put(
        f"/api/tracks/{track.id}/playback-preferences/source",
        headers=auth_headers(mastering),
        json={"gain_db": 6.0},
    )
    read_other_response = client.get(
        f"/api/tracks/{track.id}/playback-preferences/source",
        headers=auth_headers(producer),
    )

    assert write_response.status_code == 200
    assert read_other_response.status_code == 200
    assert read_other_response.json()["user_id"] == producer.id
    assert read_other_response.json()["gain_db"] == 0.0


def test_track_playback_preference_requires_track_visibility(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")

    response = client.get(
        f"/api/tracks/{track.id}/playback-preferences/source",
        headers=auth_headers(outsider),
    )

    assert response.status_code == 403


def test_track_playback_preference_accepts_master_scope(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="final_review")

    response = client.put(
        f"/api/tracks/{track.id}/playback-preferences/master",
        headers=auth_headers(mastering),
        json={"gain_db": 2.0},
    )

    assert response.status_code == 200
    assert response.json()["scope"] == "master"
    assert response.json()["gain_db"] == 2.0


def test_delivery_step_hides_manual_deliver_transition_until_upload_flow_advances(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")
    factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)

    response = client.get(f"/api/tracks/{track.id}", headers=auth_headers(mastering))

    assert response.status_code == 200
    body = response.json()["track"]
    assert "deliver" not in body["allowed_actions"]
    assert "confirm_delivery" in body["allowed_actions"]
    assert {transition["decision"] for transition in body["workflow_transitions"]} == {"request_revision"}


def test_delivery_step_rejects_manual_deliver_transition(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")

    response = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(mastering),
        json={"decision": "deliver"},
    )

    assert response.status_code == 409
    assert "delivery upload flow" in response.text


def test_legacy_mastering_roll_back_to_producer_is_hidden_and_rejected(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "mastering",
                    "label": "Mastering",
                    "type": "delivery",
                    "ui_variant": "mastering",
                    "assignee_role": "mastering_engineer",
                    "order": 0,
                    "transitions": {
                        "deliver": "final_review",
                        "request_revision": "mastering_revision",
                        "reject_to_producer_gate": "producer_gate",
                    },
                    "revision_step": "mastering_revision",
                    "require_confirmation": True,
                },
                {
                    "id": "mastering_revision",
                    "label": "Mastering Revision",
                    "type": "revision",
                    "assignee_role": "submitter",
                    "order": 1,
                    "return_to": "mastering",
                    "transitions": {},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Review",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 2,
                    "transitions": {"approve": "__completed"},
                },
                {
                    "id": "final_review",
                    "label": "Final Review",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 3,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    db_session.add(album)
    db_session.commit()

    track = factory.track(album=album, submitter=submitter, status="mastering")

    detail_response = client.get(f"/api/tracks/{track.id}", headers=auth_headers(mastering))

    assert detail_response.status_code == 200
    transitions = {transition["decision"] for transition in detail_response.json()["track"]["workflow_transitions"]}
    assert "reject_to_producer_gate" not in transitions

    transition_response = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(mastering),
        json={"decision": "reject_to_producer_gate"},
    )

    assert transition_response.status_code == 409
    assert "delivery upload flow" in transition_response.text


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


def test_proxy_track_producer_final_approval_counts_for_submitter(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    track = factory.track(
        album=album,
        submitter=producer,
        status=TrackStatus.FINAL_REVIEW,
        external_composers=["Offline Composer"],
        include_submitter_composer=False,
    )
    track.proxy_uploader_id = producer.id
    delivery = factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/final-review/approve",
        headers=auth_headers(producer),
    )

    assert response.status_code == 200
    assert response.json()["status"] == TrackStatus.COMPLETED.value
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
        json={"target_stage_id": "mastering", "mastering_notes": "Please keep the dynamics."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "mastering"
    assert body["workflow_cycle"] == track.workflow_cycle + 1
    assert body["mastering_notes"] == "Please keep the dynamics."


def test_submitter_can_stage_source_followup_when_album_allows_it(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter],
        quick_followup_enabled=True,
    )
    track = factory.track(album=album, submitter=submitter, status="peer_review")

    response = client.post(
        f"/api/tracks/{track.id}/source-followups",
        headers=auth_headers(submitter),
        data={"reason": "Need to replace a render with clipped intro."},
        files={"file": ("followup.wav", b"RIFFfollowup", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == TrackStatus.SOURCE_FOLLOWUP_PENDING.value
    assert body["pending_source_followup_request"]["reason"] == "Need to replace a render with clipped intro."
    assert "cancel_source_followup" in body["allowed_actions"]

    db_session.refresh(track)
    assert track.status == TrackStatus.SOURCE_FOLLOWUP_PENDING.value
    req = db_session.scalar(select(SourceFollowupRequest).where(SourceFollowupRequest.track_id == track.id))
    assert req is not None
    assert req.previous_status == "peer_review"
    assert req.status == "pending"
    assert Path(req.staged_file_path).exists()
    versions = db_session.scalars(
        select(TrackSourceVersion).where(TrackSourceVersion.track_id == track.id)
    ).all()
    assert len(versions) == 1


def test_source_followup_approval_applies_new_source_and_invalidates_old_master(
    client,
    db_session,
    factory,
    auth_headers,
):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter],
        quick_followup_enabled=True,
    )
    track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.COMPLETED,
        workflow_cycle=3,
    )
    factory.master_delivery(track=track, uploaded_by=mastering, workflow_cycle=3)
    original_cycle = track.workflow_cycle

    request_response = client.post(
        f"/api/tracks/{track.id}/source-followups",
        headers=auth_headers(submitter),
        data={"reason": "Need a cleaner source before remastering."},
        files={"file": ("clean.wav", b"RIFFclean", "audio/wav")},
    )
    assert request_response.status_code == 200
    request_id = request_response.json()["pending_source_followup_request"]["id"]
    req = db_session.get(SourceFollowupRequest, request_id)
    assert req is not None
    staged_file_path = req.staged_file_path

    decision_response = client.post(
        f"/api/tracks/source-followups/{request_id}/decide",
        headers=auth_headers(producer),
        json={"decision": "approve", "target_stage_id": "mastering"},
    )

    assert decision_response.status_code == 200
    body = decision_response.json()
    assert body["status"] == "mastering"
    assert body["version"] == 2
    assert body["workflow_cycle"] == original_cycle + 1
    assert body["current_master_delivery"] is None
    assert body["pending_source_followup_request"] is None

    db_session.refresh(track)
    db_session.refresh(req)
    assert track.file_path == staged_file_path
    assert req.status == "applied"
    assert req.target_stage_id == "mastering"
    versions = db_session.scalars(
        select(TrackSourceVersion).where(TrackSourceVersion.track_id == track.id)
    ).all()
    latest = max(versions, key=lambda item: item.version_number)
    assert len(versions) == 2
    assert latest.file_path == staged_file_path
    assert latest.workflow_cycle == original_cycle + 1
    assert latest.revision_notes == "Need a cleaner source before remastering."
    assert req.applied_source_version_id == latest.id


def test_source_followup_rejection_restores_previous_status_and_deletes_draft(
    client,
    db_session,
    factory,
    auth_headers,
):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter],
        quick_followup_enabled=True,
    )
    track = factory.track(album=album, submitter=submitter, status="peer_review")

    request_response = client.post(
        f"/api/tracks/{track.id}/source-followups",
        headers=auth_headers(submitter),
        data={"reason": "Wrong export bounced."},
        files={"file": ("wrong.wav", b"RIFFwrong", "audio/wav")},
    )
    assert request_response.status_code == 200
    request_id = request_response.json()["pending_source_followup_request"]["id"]
    req = db_session.get(SourceFollowupRequest, request_id)
    assert req is not None
    staged_file_path = req.staged_file_path
    assert Path(staged_file_path).exists()

    decision_response = client.post(
        f"/api/tracks/source-followups/{request_id}/decide",
        headers=auth_headers(producer),
        json={"decision": "reject"},
    )

    assert decision_response.status_code == 200
    assert decision_response.json()["status"] == "peer_review"
    assert decision_response.json()["pending_source_followup_request"] is None
    assert not Path(staged_file_path).exists()

    db_session.refresh(req)
    assert req.status == "rejected"
    versions = db_session.scalars(
        select(TrackSourceVersion).where(TrackSourceVersion.track_id == track.id)
    ).all()
    assert len(versions) == 1


def test_source_followup_requires_album_switch_and_non_revision_stage(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    disabled_album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    disabled_track = factory.track(album=disabled_album, submitter=submitter, status="peer_review")

    disabled_response = client.post(
        f"/api/tracks/{disabled_track.id}/source-followups",
        headers=auth_headers(submitter),
        data={"reason": "Try outside enabled album."},
        files={"file": ("followup.wav", b"RIFFfollowup", "audio/wav")},
    )
    assert disabled_response.status_code == 403

    enabled_album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter],
        quick_followup_enabled=True,
    )
    revision_track = factory.track(album=enabled_album, submitter=submitter, status="peer_revision")

    revision_response = client.post(
        f"/api/tracks/{revision_track.id}/source-followups",
        headers=auth_headers(submitter),
        data={"reason": "Revision should use the normal upload flow."},
        files={"file": ("revision.wav", b"RIFFrevision", "audio/wav")},
    )
    assert revision_response.status_code == 409


def test_mastering_engineer_can_only_approve_mastering_related_source_followup_target(
    client,
    db_session,
    factory,
    auth_headers,
):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter],
        quick_followup_enabled=True,
    )
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.COMPLETED)
    request_response = client.post(
        f"/api/tracks/{track.id}/source-followups",
        headers=auth_headers(submitter),
        data={"reason": "Mastering engineer should choose a mastering return target."},
        files={"file": ("followup.wav", b"RIFFfollowup", "audio/wav")},
    )
    assert request_response.status_code == 200
    request_id = request_response.json()["pending_source_followup_request"]["id"]

    peer_target_response = client.post(
        f"/api/tracks/source-followups/{request_id}/decide",
        headers=auth_headers(mastering),
        json={"decision": "approve", "target_stage_id": "peer_review"},
    )

    assert peer_target_response.status_code == 403
    req = db_session.get(SourceFollowupRequest, request_id)
    assert req is not None
    assert req.status == "pending"


def test_assign_reviewer_allows_album_producer_and_mastering_engineer(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="peer_review")
    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "peer_review",
                    "label": "Peer Review",
                    "type": "review",
                    "ui_variant": "peer_review",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "transitions": {"pass": "producer_gate"},
                    "assignment_mode": "manual",
                    "required_reviewer_count": 2,
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "ui_variant": "producer_gate",
                    "assignee_role": "producer",
                    "order": 1,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/assign-reviewer",
        headers=auth_headers(producer),
        json={"user_ids": [producer.id, mastering.id]},
    )

    assert response.status_code == 200
    assert {item["user_id"] for item in response.json()} == {producer.id, mastering.id}


def test_get_track_detail(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
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


def test_peer_review_transition_skips_checklist_gate_when_album_checklist_disabled(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer],
        checklist_enabled=False,
    )
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
        peer_reviewer=reviewer,
    )
    factory.session.add(
        StageAssignment(
            track_id=track.id,
            stage_id="peer_review",
            user_id=reviewer.id,
            status="pending",
        )
    )
    factory.session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(reviewer),
        json={"decision": "pass"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "producer_gate"


def test_upload_source_version_from_peer_revision(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_revision",
        peer_reviewer=reviewer,
    )

    response = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(submitter),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "peer_review"
    assert body["version"] == 2


def test_proxy_track_producer_can_upload_submitter_revision(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[reviewer])
    track = factory.track(
        album=album,
        submitter=producer,
        status="peer_revision",
        peer_reviewer=reviewer,
        external_composers=["Offline Composer"],
        include_submitter_composer=False,
    )
    track.proxy_uploader_id = producer.id
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(producer),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "peer_review"
    assert body["version"] == 2
    assert body["external_submitter_name"] == "Offline Composer"
    assert body["external_composer_names"] == ["Offline Composer"]
    assert body["composer_ids"] == []
    assert body["is_proxy_submission"] is True


def test_upload_source_version_resolves_selected_issues_transactionally(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_revision", peer_reviewer=reviewer)
    issue = factory.issue(
        track=track,
        author=reviewer,
        phase="peer",
        status=IssueStatus.OPEN,
        source_version_id=track.source_versions[-1].id,
    )

    response = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(submitter),
        files=[
            ("file", ("revision.wav", b"RIFFrev", "audio/wav")),
            ("resolved_issue_ids", (None, str(issue.id))),
            ("resolution_note", (None, "Fixed in revision v2.")),
        ],
    )

    assert response.status_code == 200
    db_session.refresh(issue)
    assert issue.status == IssueStatus.RESOLVED
    status_note = db_session.scalars(
        select(Comment).where(Comment.issue_id == issue.id, Comment.is_status_note.is_(True))
    ).first()
    assert status_note is not None
    assert status_note.old_status == IssueStatus.OPEN.value
    assert status_note.new_status == IssueStatus.RESOLVED.value
    assert status_note.content == "Fixed in revision v2."


def test_confirm_source_version_upload_resolves_selected_issues(client, db_session, factory, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "R2_ENABLED", True)
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_revision", peer_reviewer=reviewer)
    issue = factory.issue(
        track=track,
        author=reviewer,
        phase="peer",
        status=IssueStatus.OPEN,
        source_version_id=track.source_versions[-1].id,
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.r2",
        SimpleNamespace(
            object_exists=lambda key: True,
            download_to_temp=lambda key: Path(factory._audio_file(stem="r2-revision", ext=".wav")),
        ),
    )

    response = client.post(
        f"/api/tracks/{track.id}/source-versions/confirm-upload",
        headers=auth_headers(submitter),
        json={
            "upload_id": "upload-2",
            "object_key": f"tracks/{track.id}/source/{track.version + 1}/revision.wav",
            "resolved_issue_ids": [issue.id],
            "resolution_note": "Resolved in the R2 upload.",
        },
    )

    assert response.status_code == 200
    db_session.refresh(issue)
    assert issue.status == IssueStatus.RESOLVED
    status_note = db_session.scalars(
        select(Comment).where(Comment.issue_id == issue.id, Comment.is_status_note.is_(True))
    ).first()
    assert status_note is not None
    assert status_note.content == "Resolved in the R2 upload."


def test_upload_source_version_from_mastering_revision(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="mastering_revision",
    )

    response = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(submitter),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "mastering"
    assert body["version"] == 2


def test_track_full_flow_completes_after_mastering_requests_stem_files(client, db_session, factory, auth_headers):
    producer, mastering, submitter, track_id = _start_uploaded_track_through_mastering(
        client,
        factory,
        auth_headers,
    )

    revision = _transition(
        client,
        auth_headers,
        track_id,
        mastering,
        "request_revision",
        revision_type="stem_files",
    )
    assert revision["status"] == "mastering_revision"
    assert revision["requested_revision_type"] == "stem_files"

    wrong_upload = client.post(
        f"/api/tracks/{track_id}/source-versions",
        headers=auth_headers(submitter),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )
    assert wrong_upload.status_code == 409
    assert "stem files" in wrong_upload.text

    source_revision = client.post(
        f"/api/tracks/{track_id}/source-versions/external-link",
        headers=auth_headers(submitter),
        json={"revision_notes": "https://cloud.example/full-flow-stems\ncode: bk24"},
    )
    assert source_revision.status_code == 200, source_revision.text
    source_body = source_revision.json()
    assert source_body["status"] == "mastering"
    assert source_body["version"] == 2
    assert source_body["requested_revision_type"] is None
    assert source_body["current_source_version"]["source_kind"] == "external_link"
    assert source_body["current_source_version"]["file_path"] is None

    completed = _upload_confirm_and_approve_master(
        client,
        auth_headers,
        track_id,
        producer,
        mastering,
        submitter,
    )
    assert completed["status"] == "completed"

    db_session.expire_all()
    versions = db_session.scalars(
        select(TrackSourceVersion).where(TrackSourceVersion.track_id == track_id)
    ).all()
    assert len(versions) == 2
    assert max(versions, key=lambda version: version.version_number).source_kind == "external_link"


def test_track_full_flow_completes_after_mastering_requests_source_audio(client, db_session, factory, auth_headers):
    producer, mastering, submitter, track_id = _start_uploaded_track_through_mastering(
        client,
        factory,
        auth_headers,
    )

    revision = _transition(
        client,
        auth_headers,
        track_id,
        mastering,
        "request_revision",
        revision_type="source_audio",
    )
    assert revision["status"] == "mastering_revision"
    assert revision["requested_revision_type"] == "source_audio"

    wrong_link = client.post(
        f"/api/tracks/{track_id}/source-versions/external-link",
        headers=auth_headers(submitter),
        json={"revision_notes": "https://cloud.example/full-flow-stems"},
    )
    assert wrong_link.status_code == 409
    assert "source audio" in wrong_link.text

    source_revision = client.post(
        f"/api/tracks/{track_id}/source-versions",
        headers=auth_headers(submitter),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )
    assert source_revision.status_code == 200, source_revision.text
    source_body = source_revision.json()
    assert source_body["status"] == "mastering"
    assert source_body["version"] == 2
    assert source_body["requested_revision_type"] is None
    assert source_body["current_source_version"]["source_kind"] == "file"
    assert source_body["current_source_version"]["file_path"] is not None

    completed = _upload_confirm_and_approve_master(
        client,
        auth_headers,
        track_id,
        producer,
        mastering,
        submitter,
    )
    assert completed["status"] == "completed"

    db_session.expire_all()
    versions = db_session.scalars(
        select(TrackSourceVersion).where(TrackSourceVersion.track_id == track_id)
    ).all()
    assert len(versions) == 2
    assert max(versions, key=lambda version: version.version_number).source_kind == "file"


def test_upload_source_version_rejects_file_when_mastering_requested_stems(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="mastering_revision",
    )
    track.requested_revision_type = "stem_files"
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(submitter),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )

    assert response.status_code == 409
    assert "stem files" in response.text


def test_submit_external_source_link_from_mastering_revision_preserves_audio(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="mastering_revision",
    )
    track.requested_revision_type = "stem_files"
    db_session.commit()
    original_file_path = track.file_path
    original_storage_backend = track.storage_backend
    original_duration = track.duration
    issue = factory.issue(
        track=track,
        author=mastering,
        phase=IssuePhase.MASTERING,
        status=IssueStatus.OPEN,
    )

    response = client.post(
        f"/api/tracks/{track.id}/source-versions/external-link",
        headers=auth_headers(submitter),
        json={
            "revision_notes": "  https://cloud.example/stems\n提取码: bk24  ",
            "resolved_issue_ids": [issue.id],
            "resolution_note": "Stems uploaded to the linked folder.",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "mastering"
    assert body["version"] == 2
    assert body["file_path"] == original_file_path
    assert body["duration"] == original_duration
    current_source = body["current_source_version"]
    assert current_source["source_kind"] == "external_link"
    assert current_source["file_path"] is None
    assert current_source["revision_notes"] == "https://cloud.example/stems\n提取码: bk24"

    db_session.refresh(track)
    db_session.refresh(issue)
    assert track.file_path == original_file_path
    assert track.storage_backend == original_storage_backend
    assert track.duration == original_duration
    assert issue.status == IssueStatus.RESOLVED
    versions = db_session.scalars(
        select(TrackSourceVersion).where(TrackSourceVersion.track_id == track.id)
    ).all()
    assert len(versions) == 2
    external_version = max(versions, key=lambda version: version.version_number)
    assert external_version.source_kind == "external_link"
    assert external_version.file_path is None
    assert external_version.revision_notes == "https://cloud.example/stems\n提取码: bk24"


def test_submit_external_source_link_rejects_when_mastering_requested_source_audio(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering_revision")
    track.requested_revision_type = "source_audio"
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/source-versions/external-link",
        headers=auth_headers(submitter),
        json={"revision_notes": "https://cloud.example/stems"},
    )

    assert response.status_code == 409
    assert "source audio" in response.text


def test_submit_external_source_link_rejects_non_mastering_revision(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="peer_revision")

    response = client.post(
        f"/api/tracks/{track.id}/source-versions/external-link",
        headers=auth_headers(submitter),
        json={"revision_notes": "https://cloud.example/stems"},
    )

    assert response.status_code == 409
    assert "mastering revision" in response.text


def test_submit_external_source_link_requires_assigned_composer(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering_revision")

    response = client.post(
        f"/api/tracks/{track.id}/source-versions/external-link",
        headers=auth_headers(producer),
        json={"revision_notes": "https://cloud.example/stems"},
    )

    assert response.status_code == 403
    assert "assigned user" in response.text


def test_upload_source_version_wrong_status_fails(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="peer_review")

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
    track = factory.track(album=album, submitter=submitter, status="peer_revision")

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


def test_delete_track_removes_review_assignments_before_track_id_can_be_reused(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)
    album_id = album.id
    submitter_id = submitter.id
    track_id = track.id
    db_session.add(
        StageAssignment(
            track_id=track_id,
            stage_id="peer_review",
            user_id=reviewer.id,
            status="completed",
            decision="needs_revision",
        )
    )
    db_session.commit()

    response = client.delete(f"/api/tracks/{track_id}", headers=auth_headers(submitter))

    assert response.status_code == 204
    db_session.expire_all()
    assert db_session.scalars(select(StageAssignment).where(StageAssignment.track_id == track_id)).all() == []

    db_session.expunge_all()
    replacement = factory.track(
        album=db_session.get(type(album), album_id),
        submitter=db_session.get(type(submitter), submitter_id),
    )
    assert db_session.scalars(select(StageAssignment).where(StageAssignment.track_id == replacement.id)).all() == []


def test_delete_track_removes_issue_images_and_discussion_audios(client, db_session, factory, auth_headers, upload_dir):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter)

    issue = factory.issue(track=track, author=producer, phase="producer", source_version_id=track.source_versions[-1].id)
    issue_images_dir = upload_dir / "issue_images"
    issue_images_dir.mkdir(parents=True, exist_ok=True)
    issue_image_path = issue_images_dir / "cleanup-test.png"
    issue_image_path.write_bytes(b"png")
    db_session.add(IssueImage(issue_id=issue.id, file_path="issue_images/cleanup-test.png"))

    discussion = TrackDiscussion(track_id=track.id, author_id=submitter.id, phase="general", content="cleanup")
    db_session.add(discussion)
    db_session.flush()
    discussion_audios_dir = upload_dir / "discussion_audios"
    discussion_audios_dir.mkdir(parents=True, exist_ok=True)
    discussion_audio_path = discussion_audios_dir / "cleanup-test.wav"
    discussion_audio_path.write_bytes(b"RIFFcleanup")
    db_session.add(
        TrackDiscussionAudio(
            discussion_id=discussion.id,
            file_path="discussion_audios/cleanup-test.wav",
            original_filename="cleanup-test.wav",
            duration=1.0,
        )
    )
    db_session.commit()

    response = client.delete(f"/api/tracks/{track.id}", headers=auth_headers(submitter))

    assert response.status_code == 204
    assert not issue_image_path.exists()
    assert not discussion_audio_path.exists()


def test_audio_query_token_rejects_deleted_user(client, db_session, factory):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter)
    token = create_access_token(submitter)

    submitter.deleted_at = datetime.now(timezone.utc)
    db_session.commit()

    response = client.get(f"/api/tracks/{track.id}/audio", params={"token": token})

    assert response.status_code == 401


def test_confirm_track_upload_rejects_unexpected_r2_key(client, factory, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "R2_ENABLED", True)
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])

    response = client.post(
        "/api/tracks/confirm-upload",
        headers=auth_headers(submitter),
        json={
            "upload_id": "upload-1",
            "object_key": "tracks/new/source/999/not-owned.mp3",
            "album_id": album.id,
            "title": "R2 Track",
            "artist": "Nova",
            "composer_ids": [submitter.id],
        },
    )

    assert response.status_code == 400
    assert "expected target" in response.json()["detail"]


def test_r2_proxy_track_upload_records_external_submitter(client, db_session, factory, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "R2_ENABLED", True)
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    monkeypatch.setitem(
        sys.modules,
        "app.services.r2",
        SimpleNamespace(
            make_object_key=lambda prefix, user_id, filename: f"{prefix}/{user_id}/{filename}",
            generate_upload_url=lambda object_key, content_type: f"https://upload.example/{object_key}",
            object_exists=lambda key: True,
            download_to_temp=lambda key: Path(factory._audio_file(stem="r2-proxy", ext=".wav")),
        ),
    )

    request_response = client.post(
        "/api/tracks/request-upload",
        headers=auth_headers(producer),
        json={
            "filename": "proxy.wav",
            "content_type": "audio/wav",
            "file_size": 16,
            "album_id": album.id,
            "title": "R2 Proxy Track",
            "artist": "Offline Composer",
            "proxy_submission": True,
            "external_submitter_name": "Offline Composer",
        },
    )
    assert request_response.status_code == 200

    confirm_response = client.post(
        "/api/tracks/confirm-upload",
        headers=auth_headers(producer),
        json={
            "upload_id": request_response.json()["upload_id"],
            "object_key": request_response.json()["object_key"],
            "album_id": album.id,
            "title": "R2 Proxy Track",
            "artist": "Offline Composer",
            "proxy_submission": True,
            "external_submitter_name": "Offline Composer",
        },
    )

    assert confirm_response.status_code == 200
    body = confirm_response.json()
    assert body["proxy_uploader_id"] == producer.id
    assert body["external_submitter_name"] == "Offline Composer"
    assert body["external_composer_names"] == ["Offline Composer"]
    assert [item["name"] for item in body["external_composers"]] == ["Offline Composer"]
    assert body["composer_ids"] == []
    assert body["is_proxy_submission"] is True
    track = db_session.get(Track, body["id"])
    assert track.storage_backend == "r2"
    assert track.proxy_uploader_id == producer.id
    assert track.external_submitter_name == "Offline Composer"
    external_names = db_session.scalars(
        select(TrackExternalComposer.name).where(TrackExternalComposer.track_id == track.id)
    ).all()
    assert external_names == ["Offline Composer"]


def test_confirm_source_version_upload_rejects_unexpected_r2_key(client, factory, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "R2_ENABLED", True)
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="peer_revision")

    response = client.post(
        f"/api/tracks/{track.id}/source-versions/confirm-upload",
        headers=auth_headers(submitter),
        json={
            "upload_id": "upload-2",
            "object_key": f"tracks/{track.id}/source/999/not-owned.mp3",
        },
    )

    assert response.status_code == 400
    assert "expected target" in response.json()["detail"]


def test_confirm_master_delivery_upload_rejects_unexpected_r2_key(client, factory, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "R2_ENABLED", True)
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")

    response = client.post(
        f"/api/tracks/{track.id}/master-deliveries/confirm-upload",
        headers=auth_headers(mastering),
        json={
            "upload_id": "upload-3",
            "object_key": f"tracks/{track.id}/master/999/not-owned.mp3",
        },
    )

    assert response.status_code == 400
    assert "expected target" in response.json()["detail"]


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
        status="peer_review",
        peer_reviewer=valid_reviewer,
    )

    response = client.post(
        f"/api/tracks/{track.id}/assign-reviewer",
        headers=auth_headers(producer),
        json={"user_ids": [outsider.id]},
    )

    assert response.status_code == 400
    assert "not members" in response.text


def test_assign_reviewer_accepts_circle_member_not_on_album(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="circle_reviewer")

    circle_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Circle Review", "description": "desc"},
    )
    assert circle_response.status_code == 201
    circle_id = circle_response.json()["id"]
    db_session.add_all(
        [
            CircleMember(circle_id=circle_id, user_id=submitter.id, role="member"),
            CircleMember(circle_id=circle_id, user_id=reviewer.id, role="member"),
        ]
    )
    db_session.commit()

    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    album.circle_id = circle_id
    track = factory.track(album=album, submitter=submitter, status="peer_review")
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/assign-reviewer",
        headers=auth_headers(producer),
        json={"user_ids": [reviewer.id]},
    )

    assert response.status_code == 200
    assert [item["user_id"] for item in response.json()] == [reviewer.id]


def test_assign_reviewer_rejects_non_circle_member_for_circle_album(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    outsider = factory.user(username="album_only_reviewer")

    circle_response = client.post(
        "/api/circles",
        headers=auth_headers(producer),
        json={"name": "Circle Only", "description": "desc"},
    )
    assert circle_response.status_code == 201
    circle_id = circle_response.json()["id"]
    db_session.add(CircleMember(circle_id=circle_id, user_id=submitter.id, role="member"))
    db_session.commit()

    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, outsider],
    )
    album.circle_id = circle_id
    track = factory.track(album=album, submitter=submitter, status="peer_review")
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/assign-reviewer",
        headers=auth_headers(producer),
        json={"user_ids": [outsider.id]},
    )

    assert response.status_code == 400
    assert "not members of this circle" in response.text


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
        status="peer_review",
        peer_reviewer=current_reviewer,
    )

    response = client.post(
        f"/api/tracks/{track.id}/reassign-reviewer",
        headers=auth_headers(producer),
        json={"user_id": outsider.id},
    )

    assert response.status_code == 400
    assert "not members" in response.text


def test_upload_source_version_custom_revision_requires_assigned_user(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, outsider])
    track = factory.track(album=album, submitter=submitter, status="submitted", peer_reviewer=None)

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
    track = factory.track(album=album, submitter=submitter, status="submitted", peer_reviewer=None)

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


def test_confirm_delivery_rejects_steps_that_do_not_require_confirmation(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="submitted", peer_reviewer=None)

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
    delivery = factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1)
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/master-deliveries/{delivery.id}/confirm",
        headers=auth_headers(mastering),
    )

    assert response.status_code == 409
    assert "does not require confirmation" in response.text


def test_create_issue_custom_step_rejects_mismatched_phase(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="submitted", peer_reviewer=None)

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


def test_multi_reviewer_forward_waits_for_required_count(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer_a, reviewer_b])
    track = factory.track(album=album, submitter=submitter, status="submitted", peer_reviewer=None)

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
                    "transitions": {
                        "pass": "producer_gate",
                        "needs_revision": "custom_revision",
                    },
                    "assignment_mode": "manual",
                    "required_reviewer_count": 2,
                },
                {
                    "id": "custom_revision",
                    "label": "Custom Revision",
                    "type": "revision",
                    "assignee_role": "submitter",
                    "order": 1,
                    "return_to": "custom_review",
                    "transitions": {},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 2,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    track.status = "custom_review"
    db_session.add_all([
        StageAssignment(track_id=track.id, stage_id="custom_review", user_id=reviewer_a.id, status="pending"),
        StageAssignment(track_id=track.id, stage_id="custom_review", user_id=reviewer_b.id, status="pending"),
    ])
    db_session.commit()

    first = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(reviewer_a),
        json={"decision": "pass"},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "custom_review"

    track_events = db_session.scalars(
        select(WorkflowEvent)
        .where(WorkflowEvent.track_id == track.id)
        .order_by(WorkflowEvent.created_at.desc(), WorkflowEvent.id.desc())
    ).all()
    assert any(event.event_type == "workflow_review_progress" for event in track_events)

    second = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(reviewer_b),
        json={"decision": "pass"},
    )
    assert second.status_code == 200
    assert second.json()["status"] == "custom_review"

    finalize = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(reviewer_a),
        json={"decision": "pass"},
    )
    assert finalize.status_code == 200
    assert finalize.json()["status"] == "producer_gate"


def test_multi_reviewer_non_forward_decision_waits_for_peer_finalization(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer_a, reviewer_b])
    track = factory.track(album=album, submitter=submitter, status="submitted", peer_reviewer=None)

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
                    "transitions": {
                        "pass": "producer_gate",
                        "needs_revision": "custom_revision",
                    },
                    "assignment_mode": "manual",
                    "required_reviewer_count": 2,
                },
                {
                    "id": "custom_revision",
                    "label": "Custom Revision",
                    "type": "revision",
                    "assignee_role": "submitter",
                    "order": 1,
                    "return_to": "custom_review",
                    "transitions": {},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 2,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    track.status = "custom_review"
    db_session.add_all([
        StageAssignment(track_id=track.id, stage_id="custom_review", user_id=reviewer_a.id, status="pending"),
        StageAssignment(track_id=track.id, stage_id="custom_review", user_id=reviewer_b.id, status="pending"),
    ])
    db_session.commit()

    first = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(reviewer_a),
        json={"decision": "pass"},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "custom_review"

    rollback = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(reviewer_b),
        json={"decision": "needs_revision"},
    )
    assert rollback.status_code == 200
    assert rollback.json()["status"] == "custom_review"

    finalize = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(reviewer_a),
        json={"decision": "needs_revision"},
    )
    assert finalize.status_code == 200
    assert finalize.json()["status"] == "custom_revision"


def test_first_revision_request_keeps_suggestion_until_direct_action(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer_a, reviewer_b],
        checklist_enabled=False,
    )
    track = factory.track(album=album, submitter=submitter, status="submitted", peer_reviewer=None)

    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "peer_review",
                    "label": "Peer Review",
                    "type": "review",
                    "ui_variant": "peer_review",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "transitions": {"pass": "producer_gate", "needs_revision": "peer_revision"},
                    "assignment_mode": "manual",
                    "required_reviewer_count": 2,
                    "revision_step": "peer_revision",
                    "revision_decision_policy": "first_revision_request",
                },
                {
                    "id": "peer_revision",
                    "label": "Peer Revision",
                    "type": "revision",
                    "assignee_role": "submitter",
                    "order": 1,
                    "return_to": "peer_review",
                    "transitions": {},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 2,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    track.status = "peer_review"
    db_session.add_all([
        StageAssignment(track_id=track.id, stage_id="peer_review", user_id=reviewer_a.id, status="pending"),
        StageAssignment(track_id=track.id, stage_id="peer_review", user_id=reviewer_b.id, status="pending"),
    ])
    db_session.commit()

    issue = factory.issue(
        track=track,
        author=reviewer_a,
        phase=IssuePhase.PEER,
        status=IssueStatus.PENDING_DISCUSSION,
        source_version_id=track.source_versions[-1].id,
    )

    suggestion = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(reviewer_a),
        json={"decision": "needs_revision"},
    )
    assert suggestion.status_code == 200
    assert suggestion.json()["status"] == "peer_review"

    reviewer_detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(reviewer_a))
    assert reviewer_detail.status_code == 200
    decisions = {item["decision"] for item in reviewer_detail.json()["track"]["workflow_transitions"]}
    assert "request_revision_now" in decisions

    direct = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(reviewer_a),
        json={"decision": "request_revision_now"},
    )
    assert direct.status_code == 200
    assert direct.json()["status"] == "peer_revision"

    db_session.refresh(issue)
    assert issue.status == IssueStatus.PENDING_DISCUSSION

    assignments = db_session.scalars(
        select(StageAssignment)
        .where(StageAssignment.track_id == track.id, StageAssignment.stage_id == "peer_review")
        .order_by(StageAssignment.user_id.asc())
    ).all()
    by_user = {assignment.user_id: assignment for assignment in assignments}
    assert by_user[reviewer_a.id].status == "completed"
    assert by_user[reviewer_a.id].decision == "needs_revision"
    assert by_user[reviewer_b.id].status == "cancelled"
    assert by_user[reviewer_b.id].cancellation_reason == "revision_requested"

    retained_discussion = client.patch(
        f"/api/issues/{issue.id}",
        headers=auth_headers(reviewer_b),
        json={"status": "internal_resolved", "status_note": "Discussed internally."},
    )
    assert retained_discussion.status_code == 200
    assert retained_discussion.json()["status"] == "internal_resolved"

    upload = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(submitter),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )
    assert upload.status_code == 200
    assert upload.json()["status"] == "peer_review"

    db_session.expire_all()
    reopened = db_session.scalars(
        select(StageAssignment)
        .where(StageAssignment.track_id == track.id, StageAssignment.stage_id == "peer_review")
        .order_by(StageAssignment.user_id.asc())
    ).all()
    assert {assignment.user_id for assignment in reopened} == {reviewer_a.id, reviewer_b.id}
    assert all(assignment.status == "pending" for assignment in reopened)
    assert all(assignment.decision is None for assignment in reopened)


def test_completed_revision_suggestion_can_trigger_after_policy_enabled(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer_a, reviewer_b],
        checklist_enabled=False,
    )
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=None)
    now = datetime.now(timezone.utc)
    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "peer_review",
                    "label": "Peer Review",
                    "type": "review",
                    "ui_variant": "peer_review",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "transitions": {"pass": "producer_gate", "needs_revision": "peer_revision"},
                    "assignment_mode": "manual",
                    "required_reviewer_count": 2,
                    "revision_step": "peer_revision",
                    "revision_decision_policy": "first_revision_request",
                },
                {
                    "id": "peer_revision",
                    "label": "Peer Revision",
                    "type": "revision",
                    "assignee_role": "submitter",
                    "order": 1,
                    "return_to": "peer_review",
                    "transitions": {},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 2,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    db_session.add_all([
        StageAssignment(
            track_id=track.id,
            stage_id="peer_review",
            user_id=reviewer_a.id,
            status="completed",
            decision="needs_revision",
            assigned_at=now,
            completed_at=now,
        ),
        StageAssignment(track_id=track.id, stage_id="peer_review", user_id=reviewer_b.id, status="pending"),
    ])
    db_session.commit()

    detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(reviewer_a))
    assert detail.status_code == 200
    decisions = {item["decision"] for item in detail.json()["track"]["workflow_transitions"]}
    assert "request_revision_now" in decisions

    direct = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(reviewer_a),
        json={"decision": "request_revision_now"},
    )
    assert direct.status_code == 200
    assert direct.json()["status"] == "peer_revision"


def test_completed_pass_cannot_trigger_direct_revision_request(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer_a, reviewer_b],
        checklist_enabled=False,
    )
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=None)
    now = datetime.now(timezone.utc)
    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "peer_review",
                    "label": "Peer Review",
                    "type": "review",
                    "ui_variant": "peer_review",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "transitions": {"pass": "producer_gate", "needs_revision": "peer_revision"},
                    "assignment_mode": "manual",
                    "required_reviewer_count": 2,
                    "revision_step": "peer_revision",
                    "revision_decision_policy": "first_revision_request",
                },
                {
                    "id": "peer_revision",
                    "label": "Peer Revision",
                    "type": "revision",
                    "assignee_role": "submitter",
                    "order": 1,
                    "return_to": "peer_review",
                    "transitions": {},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 2,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    db_session.add_all([
        StageAssignment(
            track_id=track.id,
            stage_id="peer_review",
            user_id=reviewer_a.id,
            status="completed",
            decision="pass",
            assigned_at=now,
            completed_at=now,
        ),
        StageAssignment(track_id=track.id, stage_id="peer_review", user_id=reviewer_b.id, status="pending"),
    ])
    db_session.commit()

    direct = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(reviewer_a),
        json={"decision": "request_revision_now"},
    )
    assert direct.status_code == 403
    db_session.refresh(track)
    assert track.status == "peer_review"


def test_manual_review_stage_waits_for_explicit_assignment(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="submitted", peer_reviewer=None)

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
                    "transitions": {"pass": "producer_gate", "needs_revision": "custom_revision"},
                    "assignment_mode": "manual",
                    "required_reviewer_count": 2,
                },
                {
                    "id": "custom_revision",
                    "label": "Custom Revision",
                    "type": "revision",
                    "assignee_role": "submitter",
                    "order": 1,
                    "return_to": "custom_review",
                    "transitions": {},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 2,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    track.status = "custom_review"
    track.peer_reviewer_id = None
    db_session.commit()

    detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(producer))
    assert detail.status_code == 200
    assert detail.json()["track"]["peer_reviewer_id"] is None
    assert detail.json()["track"]["workflow_transitions"] in (None, [])

    assignments = client.get(
        f"/api/tracks/{track.id}/assignments",
        headers=auth_headers(producer),
    )
    assert assignments.status_code == 200
    assert assignments.json() == []


def test_producer_can_participate_in_peer_review_and_still_use_producer_gate(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[producer, submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="submitted", peer_reviewer=None)

    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "peer_review",
                    "label": "Peer Review",
                    "type": "review",
                    "ui_variant": "peer_review",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "transitions": {"pass": "producer_gate", "needs_revision": "peer_revision"},
                    "assignment_mode": "manual",
                    "required_reviewer_count": 2,
                    "revision_step": "peer_revision",
                },
                {
                    "id": "peer_revision",
                    "label": "Peer Revision",
                    "type": "revision",
                    "assignee_role": "submitter",
                    "order": 1,
                    "return_to": "peer_review",
                    "transitions": {},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "ui_variant": "producer_gate",
                    "assignee_role": "producer",
                    "order": 2,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    track.status = "peer_review"
    source_version = track.source_versions[-1]
    now = datetime.now(timezone.utc)
    db_session.add_all([
        StageAssignment(track_id=track.id, stage_id="peer_review", user_id=producer.id, status="pending", assigned_at=now),
        StageAssignment(track_id=track.id, stage_id="peer_review", user_id=reviewer.id, status="pending", assigned_at=now),
    ])
    db_session.commit()

    factory.checklist(track=track, reviewer=producer, source_version_id=source_version.id, label="Balance", passed=True)
    factory.checklist(track=track, reviewer=reviewer, source_version_id=source_version.id, label="Balance", passed=True)

    producer_review = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(producer),
        json={"decision": "pass"},
    )
    assert producer_review.status_code == 200
    assert producer_review.json()["status"] == "peer_review"

    reviewer_review = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(reviewer),
        json={"decision": "pass"},
    )
    assert reviewer_review.status_code == 200
    assert reviewer_review.json()["status"] == "peer_review"

    finalize = client.post(
        f"/api/tracks/{track.id}/workflow/transition",
        headers=auth_headers(producer),
        json={"decision": "pass"},
    )
    assert finalize.status_code == 200
    assert finalize.json()["status"] == "producer_gate"

    producer_gate = client.get(f"/api/tracks/{track.id}", headers=auth_headers(producer))
    assert producer_gate.status_code == 200
    assert producer_gate.json()["track"]["status"] == "producer_gate"
    assert producer_gate.json()["track"]["workflow_transitions"][0]["decision"] == "approve"


def test_revision_upload_reopens_review_assignments_with_decisions_cleared(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer_a, reviewer_b])
    track = factory.track(album=album, submitter=submitter, status="submitted", peer_reviewer=None)

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
                    "transitions": {
                        "pass": "producer_gate",
                        "needs_revision": "custom_revision",
                    },
                    "assignment_mode": "manual",
                    "required_reviewer_count": 2,
                },
                {
                    "id": "custom_revision",
                    "label": "Custom Revision",
                    "type": "revision",
                    "assignee_role": "submitter",
                    "order": 1,
                    "return_to": "custom_review",
                    "transitions": {},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 2,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    track.status = "custom_revision"
    now = datetime.now(timezone.utc)
    db_session.add_all([
        StageAssignment(
            track_id=track.id,
            stage_id="custom_review",
            user_id=reviewer_a.id,
            status="completed",
            decision="pass",
            assigned_at=now,
            completed_at=now,
        ),
        StageAssignment(
            track_id=track.id,
            stage_id="custom_review",
            user_id=reviewer_b.id,
            status="cancelled",
            decision="needs_revision",
            assigned_at=now,
            completed_at=now,
        ),
    ])
    db_session.commit()

    upload = client.post(
        f"/api/tracks/{track.id}/source-versions",
        headers=auth_headers(submitter),
        files={"file": ("revision.wav", b"RIFFrev", "audio/wav")},
    )

    assert upload.status_code == 200
    assert upload.json()["status"] == "custom_review"

    reopened = db_session.scalars(
        select(StageAssignment).where(
            StageAssignment.track_id == track.id,
            StageAssignment.stage_id == "custom_review",
        )
    ).all()
    assert len(reopened) == 2
    assert all(item.status == "pending" for item in reopened)
    assert all(item.decision is None for item in reopened)
    assert all(item.completed_at is None for item in reopened)


def test_reassign_reviewer_accepts_multiple_user_ids(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    reviewer_c = factory.user(username="reviewer_c")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer_a, reviewer_b, reviewer_c])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
        peer_reviewer=reviewer_a,
    )
    album.workflow_config = json.dumps(
        {
            "version": 2,
            "steps": [
                {
                    "id": "peer_review",
                    "label": "Peer Review",
                    "type": "review",
                    "ui_variant": "peer_review",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "transitions": {"pass": "producer_gate"},
                    "assignment_mode": "manual",
                    "required_reviewer_count": 2,
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 1,
                    "transitions": {"approve": "__completed"},
                },
            ],
        }
    )
    db_session.add(StageAssignment(track_id=track.id, stage_id="peer_review", user_id=reviewer_a.id, status="pending"))
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/reassign-reviewer",
        headers=auth_headers(producer),
        json={"user_ids": [reviewer_b.id, reviewer_c.id, reviewer_b.id]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["peer_reviewer_id"] == reviewer_b.id

    pending_assignments = db_session.scalars(
        select(StageAssignment).where(
            StageAssignment.track_id == track.id,
            StageAssignment.stage_id == "peer_review",
            StageAssignment.status == "pending",
        )
    ).all()
    assert {item.user_id for item in pending_assignments} == {reviewer_b.id, reviewer_c.id}


def test_assign_reviewer_rejects_user_ids_over_required_reviewer_count(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer_a, reviewer_b])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
    )

    response = client.post(
        f"/api/tracks/{track.id}/assign-reviewer",
        headers=auth_headers(producer),
        json={"user_ids": [reviewer_a.id, reviewer_b.id]},
    )

    assert response.status_code == 400
    assert "At most 1 reviewer" in response.text

    pending_assignments = db_session.scalars(
        select(StageAssignment).where(
            StageAssignment.track_id == track.id,
            StageAssignment.stage_id == "peer_review",
            StageAssignment.status == "pending",
        )
    ).all()
    assert pending_assignments == []


def test_reassign_reviewer_rejects_user_ids_over_required_reviewer_count(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    current_reviewer = factory.user(username="reviewer_current")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, current_reviewer, reviewer_a, reviewer_b],
    )
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
        peer_reviewer=current_reviewer,
    )
    db_session.add(StageAssignment(track_id=track.id, stage_id="peer_review", user_id=current_reviewer.id, status="pending"))
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/reassign-reviewer",
        headers=auth_headers(producer),
        json={"user_ids": [reviewer_a.id, reviewer_b.id]},
    )

    assert response.status_code == 400
    assert "At most 1 reviewer" in response.text

    db_session.refresh(track)
    assert track.peer_reviewer_id == current_reviewer.id
    pending_assignments = db_session.scalars(
        select(StageAssignment).where(
            StageAssignment.track_id == track.id,
            StageAssignment.stage_id == "peer_review",
            StageAssignment.status == "pending",
        )
    ).all()
    assert {item.user_id for item in pending_assignments} == {current_reviewer.id}


def test_assign_reviewer_dedupes_duplicate_user_ids_in_request(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(
        album=album,
        submitter=submitter,
        status="peer_review",
    )

    response = client.post(
        f"/api/tracks/{track.id}/assign-reviewer",
        headers=auth_headers(producer),
        json={"user_ids": [reviewer.id, reviewer.id]},
    )

    assert response.status_code == 200
    created = response.json()
    assert len(created) == 1
    assert created[0]["user_id"] == reviewer.id

    pending_assignments = db_session.scalars(
        select(StageAssignment).where(
            StageAssignment.track_id == track.id,
            StageAssignment.stage_id == "peer_review",
            StageAssignment.status == "pending",
        )
    ).all()
    assert len(pending_assignments) == 1
    assert pending_assignments[0].user_id == reviewer.id
