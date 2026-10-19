import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app.models.discussion import TrackDiscussion
from app.models.issue import IssuePhase, IssueStatus
from app.models.track import TrackStatus
from app.models.track_source_version import TrackSourceVersion
from app.models.workflow_event import WorkflowEvent
from app.routers import albums as albums_router, tracks as tracks_router
from app.security import create_access_token
from app.services.track_queries import current_master_delivery, latest_current_cycle_master_delivery


@pytest.fixture
def master_access(factory):
    producer = factory.user(role="producer")
    engineer = factory.user(role="mastering_engineer")
    composer = factory.user(username="composer")
    member = factory.user(username="ordinary-member")
    outsider = factory.user(username="outsider")
    album = factory.album(
        producer=producer,
        mastering_engineer=engineer,
        members=[composer, member],
    )
    return SimpleNamespace(
        producer=producer, engineer=engineer, composer=composer,
        member=member, outsider=outsider, album=album,
    )


@pytest.mark.parametrize("status", ["intake", "mastering", "mastering_revision", "final_review"])
def test_public_active_track_does_not_publish_master_audio(
    client, db_session, factory, auth_headers, master_access, status,
):
    actors = master_access
    track = factory.track(album=actors.album, submitter=actors.composer, status=status)
    delivery = factory.master_delivery(track=track, uploaded_by=actors.engineer)
    track.is_public = True
    track.mastering_notes = "Private mastering instructions"
    track.requested_revision_type = "mastering"
    spec = {
        "enabled": True, "allowed_sample_rates_hz": [48000],
        "allowed_sample_formats": ["pcm_s24"], "allowed_containers": ["wav"],
    }
    track.audio_spec_overrides = json.dumps({"source": spec, "master": spec})
    db_session.add(TrackDiscussion(
        track_id=track.id, author_id=actors.engineer.id,
        phase="mastering", content="Private mastering conversation",
    ))
    db_session.commit()
    issue = factory.issue(
        track=track, author=actors.producer, phase=IssuePhase.FINAL_REVIEW,
        master_delivery_id=delivery.id,
    )

    response = client.get(f"/api/tracks/{track.id}", headers=auth_headers(actors.member))
    assert response.status_code == 200, response.text
    detail = response.json()
    payload = detail["track"]
    assert payload["viewer_is_mastering_participant"] is False
    assert payload["viewer_can_access_master_audio"] is False
    assert payload["current_master_delivery"] is None
    assert detail["master_deliveries"] == []
    assert payload["mastering_notes"] is None
    assert payload["requested_revision_type"] is None
    assert payload["audio_spec_overrides"]["master"] is None
    assert payload["effective_audio_specs"]["master"] is None
    assert payload["master_spec_check"] == {"status": "unknown", "differences": []}
    assert payload["audio_spec_overrides"]["source"] == spec
    assert payload["effective_audio_specs"]["source"] == spec
    assert detail["discussions"] == []
    assert detail["issues"] == []
    issue_detail = client.get(f"/api/issues/{issue.id}", headers=auth_headers(actors.member))
    assert issue_detail.status_code == 404, issue_detail.text
    participant_detail = client.get(f"/api/issues/{issue.id}", headers=auth_headers(actors.engineer))
    assert participant_detail.status_code == 200, participant_detail.text
    assert participant_detail.json()["master_delivery_number"] == delivery.delivery_number
    assert participant_detail.json()["master_delivery_workflow_cycle"] == delivery.workflow_cycle
    assert "master_delivery" not in participant_detail.json()

    token = create_access_token(actors.member)
    for path in [
        f"/api/tracks/{track.id}/master-audio",
        f"/api/tracks/{track.id}/master-deliveries/{delivery.id}/audio",
    ]:
        denied = client.get(path, headers=auth_headers(actors.member))
        assert denied.status_code == 403, denied.text
        denied_token = client.get(path, params={"token": token, "resolve": "json"})
        assert denied_token.status_code == 403, denied_token.text

    source = client.get(f"/api/tracks/{track.id}/audio", headers=auth_headers(actors.member))
    assert source.status_code == 200, source.text
    source_snapshot = client.get(
        f"/api/tracks/{track.id}/source-versions/{track.source_versions[0].id}/audio",
        headers=auth_headers(actors.member),
    )
    assert source_snapshot.status_code == 200, source_snapshot.text


def test_historical_issue_provenance_stays_scalar_and_uses_the_original_delivery_cycle(
    client, db_session, factory, auth_headers, master_access,
):
    actors = master_access
    track = factory.track(
        album=actors.album, submitter=actors.composer, status=TrackStatus.COMPLETED, workflow_cycle=2,
    )
    old = factory.master_delivery(
        track=track, uploaded_by=actors.engineer, workflow_cycle=1, delivery_number=7,
        delivery_message="Do not expose the historical draft notes",
    )
    final = factory.master_delivery(track=track, uploaded_by=actors.engineer, workflow_cycle=2, delivery_number=1)
    issue = factory.issue(track=track, author=actors.producer, phase=IssuePhase.FINAL_REVIEW, master_delivery_id=old.id)
    issue.workflow_cycle = 1
    db_session.commit()
    detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(actors.engineer)).json()
    assert {delivery["id"] for delivery in detail["master_deliveries"]} == {old.id, final.id}
    origins = [detail["issues"][0]]
    response = client.get(f"/api/tracks/{track.id}/issues", headers=auth_headers(actors.engineer))
    assert response.status_code == 200, response.text
    origins.extend(response.json())
    response = client.get(f"/api/issues/{issue.id}", headers=auth_headers(actors.engineer))
    assert response.status_code == 200, response.text
    origins.append(response.json())
    for origin in origins:
        assert origin["master_delivery_id"] == old.id
        assert origin["master_delivery_number"] == 7
        assert origin["master_delivery_workflow_cycle"] == 1
        assert "master_delivery" not in origin
        assert "file_path" not in origin
        assert "delivery_message" not in origin
    denied = client.get(f"/api/tracks/{track.id}/master-deliveries/{old.id}/audio", headers=auth_headers(actors.member))
    assert denied.status_code == 403, denied.text


@pytest.mark.parametrize("count", [1, 8])
def test_issue_list_loads_master_provenance_in_one_scalar_query(
    client, db_session, factory, auth_headers, master_access, count,
):
    actors = master_access
    track = factory.track(album=actors.album, submitter=actors.composer, status="final_review")
    for number in range(1, count + 1):
        delivery = factory.master_delivery(track=track, uploaded_by=actors.engineer, delivery_number=number)
        factory.issue(track=track, author=actors.producer, phase=IssuePhase.FINAL_REVIEW, master_delivery_id=delivery.id)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        sql = statement.lower()
        if sql.lstrip().startswith("select") and "from master_deliveries" in sql:
            statements.append(sql)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", capture)
    try:
        response = client.get(f"/api/tracks/{track.id}/issues", headers=auth_headers(actors.engineer))
    finally:
        event.remove(bind, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert len(response.json()) == count
    assert {row["master_delivery_number"] for row in response.json()} == set(range(1, count + 1))
    assert len(statements) == 1
    assert "master_deliveries.file_path" not in statements[0]
    assert "master_deliveries.delivery_message" not in statements[0]


def test_completed_member_gets_only_latest_current_cycle_file_without_confirmation(
    client, db_session, factory, auth_headers, master_access, upload_dir,
):
    actors = master_access
    track = factory.track(
        album=actors.album, submitter=actors.composer,
        status=TrackStatus.COMPLETED, workflow_cycle=2,
    )
    deliveries = []
    for cycle, number, name, content in [
        (1, 9, "old-cycle.mp3", b"ID3old-cycle"),
        (2, 1, "earlier-draft.mp3", b"ID3earlier-draft"),
        (2, 2, "final.mp3", b"ID3published-final"),
    ]:
        path = upload_dir / name
        path.write_bytes(content)
        deliveries.append(factory.master_delivery(
            track=track, uploaded_by=actors.engineer,
            workflow_cycle=cycle, delivery_number=number, file_path=str(path),
            delivery_message=f"Private notes for {name}",
        ))
    old, draft, final = deliveries
    assert final.confirmed_at is None
    track.mastering_notes = "Private production notes"
    db_session.add(TrackDiscussion(
        track_id=track.id, author_id=actors.engineer.id,
        phase="mastering", content="Private mastering conversation",
    ))
    db_session.commit()

    response = client.get(f"/api/tracks/{track.id}", headers=auth_headers(actors.member))
    assert response.status_code == 200, response.text
    detail = response.json()
    payload = detail["track"]
    assert payload["viewer_is_mastering_participant"] is False
    assert payload["viewer_can_access_master_audio"] is True
    assert payload["current_master_delivery"]["id"] == final.id
    assert payload["current_master_delivery"]["confirmed_at"] is None
    assert payload["current_master_delivery"]["delivery_message"] is None
    assert [row["id"] for row in detail["master_deliveries"]] == [final.id]
    assert detail["master_deliveries"][0]["delivery_message"] is None
    assert payload["mastering_notes"] is None
    assert detail["discussions"] == []

    current = client.get(f"/api/tracks/{track.id}/master-audio", headers=auth_headers(actors.member))
    assert current.status_code == 200, current.text
    assert current.content == b"ID3published-final"
    final_download = client.get(
        f"/api/tracks/{track.id}/master-deliveries/{final.id}/audio",
        params={"token": create_access_token(actors.member)},
    )
    assert final_download.status_code == 200, final_download.text
    assert final_download.content == b"ID3published-final"

    for delivery in [old, draft]:
        response = client.get(
            f"/api/tracks/{track.id}/master-deliveries/{delivery.id}/audio",
            headers=auth_headers(actors.member),
        )
        assert response.status_code == 403, response.text

    for participant in [actors.producer, actors.composer, actors.engineer]:
        detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(participant)).json()
        assert detail["track"]["viewer_is_mastering_participant"] is True
        assert detail["track"]["viewer_can_access_master_audio"] is True
        assert {row["id"] for row in detail["master_deliveries"]} == {old.id, draft.id, final.id}
        assert detail["track"]["mastering_notes"] == "Private production notes"
        assert len(detail["discussions"]) == 1
        for delivery in [old, draft]:
            playable = client.get(
                f"/api/tracks/{track.id}/master-deliveries/{delivery.id}/audio",
                headers=auth_headers(participant),
            )
            assert playable.status_code == 200, playable.text


@pytest.mark.parametrize("participant", ["producer", "composer", "engineer"])
def test_reopened_participant_keeps_historical_playback_fallback(
    client, db_session, factory, auth_headers, master_access, participant,
):
    actors = master_access
    track = factory.track(album=actors.album, submitter=actors.composer, status="final_review")
    old = factory.master_delivery(track=track, uploaded_by=actors.engineer, delivery_number=2)
    track.workflow_cycle = 2
    track.is_public = True
    track.mastering_notes = "Retained mastering workspace"
    db_session.commit()

    user = getattr(actors, participant)
    response = client.get(f"/api/tracks/{track.id}", headers=auth_headers(user))
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["track"]["viewer_is_mastering_participant"] is True
    assert detail["track"]["viewer_can_access_master_audio"] is True
    assert detail["track"]["current_master_delivery"]["id"] == old.id
    assert detail["track"]["mastering_notes"] == "Retained mastering workspace"
    assert [row["id"] for row in detail["master_deliveries"]] == [old.id]
    assert "approve_final" not in detail["track"]["allowed_actions"]
    for path in [
        f"/api/tracks/{track.id}/master-audio",
        f"/api/tracks/{track.id}/master-deliveries/{old.id}/audio",
    ]:
        response = client.get(path, headers=auth_headers(user))
        assert response.status_code == 200, response.text
        denied = client.get(path, headers=auth_headers(actors.member))
        assert denied.status_code == 403, denied.text


def test_completed_track_without_current_cycle_delivery_does_not_publish_history(
    client, db_session, factory, auth_headers, master_access,
):
    actors = master_access
    track = factory.track(album=actors.album, submitter=actors.composer, status=TrackStatus.COMPLETED)
    old = factory.master_delivery(track=track, uploaded_by=actors.engineer)
    track.workflow_cycle = 2
    db_session.commit()
    assert current_master_delivery(track).id == old.id
    assert latest_current_cycle_master_delivery(track) is None

    response = client.get(f"/api/tracks/{track.id}", headers=auth_headers(actors.member))
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["track"]["viewer_can_access_master_audio"] is False
    assert detail["track"]["current_master_delivery"] is None
    assert detail["master_deliveries"] == []
    for path in [
        f"/api/tracks/{track.id}/master-audio",
        f"/api/tracks/{track.id}/master-deliveries/{old.id}/audio",
    ]:
        response = client.get(path, headers=auth_headers(actors.member))
        assert response.status_code == 403, response.text


@pytest.mark.parametrize("delivery_kind", ["text", "file"])
def test_completed_latest_delivery_without_file_does_not_publish_earlier_draft(
    client, db_session, factory, auth_headers, master_access, delivery_kind,
):
    actors = master_access
    track = factory.track(album=actors.album, submitter=actors.composer, status=TrackStatus.COMPLETED)
    draft = factory.master_delivery(track=track, uploaded_by=actors.engineer)
    latest = factory.master_delivery(
        track=track, uploaded_by=actors.engineer, delivery_number=2,
        delivery_kind=delivery_kind, delivery_message="Legacy delivery notes",
    )
    latest.file_path = None
    db_session.commit()

    detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(actors.member)).json()
    assert detail["track"]["viewer_can_access_master_audio"] is False
    assert detail["track"]["current_master_delivery"] is None
    assert detail["master_deliveries"] == []
    for path in [
        f"/api/tracks/{track.id}/master-audio",
        f"/api/tracks/{track.id}/master-deliveries/{draft.id}/audio",
        f"/api/tracks/{track.id}/master-deliveries/{latest.id}/audio",
    ]:
        denied = client.get(path, headers=auth_headers(actors.member))
        assert denied.status_code == 403, denied.text
    participant = client.get(
        f"/api/tracks/{track.id}/master-audio", headers=auth_headers(actors.producer),
    )
    assert participant.status_code == 404, participant.text


def test_master_delivery_identity_and_track_visibility_are_checked_before_file_access(
    client, factory, auth_headers, master_access,
):
    actors = master_access
    track = factory.track(album=actors.album, submitter=actors.composer, status=TrackStatus.COMPLETED)
    final = factory.master_delivery(track=track, uploaded_by=actors.engineer)
    other = factory.track(album=actors.album, submitter=actors.composer, status=TrackStatus.COMPLETED)
    other_delivery = factory.master_delivery(track=other, uploaded_by=actors.engineer)

    for user in [actors.member, actors.producer]:
        wrong_track = client.get(
            f"/api/tracks/{track.id}/master-deliveries/{other_delivery.id}/audio",
            headers=auth_headers(user),
        )
        assert wrong_track.status_code == 404, wrong_track.text
        missing = client.get(
            f"/api/tracks/{track.id}/master-deliveries/999999/audio",
            headers=auth_headers(user),
        )
        assert missing.status_code == 404, missing.text
    for path in [
        f"/api/tracks/{track.id}/master-audio",
        f"/api/tracks/{track.id}/master-deliveries/{final.id}/audio",
    ]:
        outsider = client.get(path, headers=auth_headers(actors.outsider))
        assert outsider.status_code == 403, outsider.text
        anonymous = client.get(path)
        assert anonymous.status_code == 401, anonymous.text


def test_master_capability_and_delivery_redaction_are_consistent_in_track_lists(
    client, db_session, factory, auth_headers, master_access,
):
    actors = master_access
    # The album collection is assignment-scoped until the whole album completes.
    # A peer reviewer is visible in both lists without becoming a mastering participant.
    active = factory.track(
        album=actors.album, submitter=actors.composer, status="mastering",
        peer_reviewer=actors.member,
    )
    active.is_public = True
    active.mastering_notes = "Active private notes"
    factory.master_delivery(track=active, uploaded_by=actors.engineer)
    completed = factory.track(
        album=actors.album, submitter=actors.composer,
        status=TrackStatus.COMPLETED, workflow_cycle=2,
        peer_reviewer=actors.member,
    )
    factory.master_delivery(track=completed, uploaded_by=actors.engineer, workflow_cycle=1)
    final = factory.master_delivery(track=completed, uploaded_by=actors.engineer, workflow_cycle=2)
    db_session.commit()

    for path in [f"/api/tracks?album_id={actors.album.id}", f"/api/albums/{actors.album.id}/tracks"]:
        response = client.get(path, headers=auth_headers(actors.member))
        assert response.status_code == 200, response.text
        rows = {row["id"]: row for row in response.json()}
        assert rows[active.id]["viewer_can_access_master_audio"] is False
        assert rows[active.id]["current_master_delivery"] is None
        assert rows[active.id]["mastering_notes"] is None
        assert rows[completed.id]["viewer_can_access_master_audio"] is True
        assert rows[completed.id]["viewer_is_mastering_participant"] is False
        assert rows[completed.id]["current_master_delivery"]["id"] == final.id


def test_final_approval_rejects_prior_cycle_fallback_without_mutating_delivery_or_track(
    client, db_session, factory, auth_headers, master_access,
):
    actors = master_access
    track = factory.track(album=actors.album, submitter=actors.composer, status="final_review")
    old = factory.master_delivery(track=track, uploaded_by=actors.engineer)
    old.producer_approved_at = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    old.confirmed_at = datetime(2024, 1, 1, 3, 4, 5, tzinfo=timezone.utc)
    track.workflow_cycle = 2
    db_session.commit()
    db_session.refresh(old)
    db_session.refresh(track)
    before = (old.producer_approved_at, old.submitter_approved_at, old.confirmed_at, track.updated_at)
    assert current_master_delivery(track).id == old.id

    for user in [actors.composer, actors.producer]:
        detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(user))
        assert detail.status_code == 200, detail.text
        assert detail.json()["track"]["current_master_delivery"]["id"] == old.id
        assert "approve_final" not in detail.json()["track"]["allowed_actions"]
        response = client.post(
            f"/api/tracks/{track.id}/final-review/approve", headers=auth_headers(user),
        )
        assert response.status_code == 409, response.text
        assert "current workflow cycle" in response.json()["detail"]
        db_session.refresh(old)
        db_session.refresh(track)
        assert track.status == "final_review"
        assert track.workflow_cycle == 2
        assert (old.producer_approved_at, old.submitter_approved_at, old.confirmed_at, track.updated_at) == before
        assert db_session.scalars(
            select(WorkflowEvent).where(WorkflowEvent.track_id == track.id),
        ).all() == []


def test_final_approval_uses_latest_current_cycle_delivery_and_completes_normally(
    client, db_session, factory, auth_headers, master_access,
):
    actors = master_access
    track = factory.track(
        album=actors.album, submitter=actors.composer, status="final_review", workflow_cycle=2,
    )
    old = factory.master_delivery(track=track, uploaded_by=actors.engineer, workflow_cycle=1, delivery_number=9)
    draft = factory.master_delivery(track=track, uploaded_by=actors.engineer, workflow_cycle=2, delivery_number=1)
    approved_at = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    for delivery in [old, draft]:
        delivery.producer_approved_at = approved_at
        delivery.submitter_approved_at = approved_at
    final = factory.master_delivery(track=track, uploaded_by=actors.engineer, workflow_cycle=2, delivery_number=2)
    db_session.commit()
    db_session.refresh(old)
    db_session.refresh(draft)
    history_before = [(delivery.producer_approved_at, delivery.submitter_approved_at) for delivery in [old, draft]]

    for user, expected_status in [(actors.producer, "final_review"), (actors.composer, "completed")]:
        detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(user))
        assert detail.status_code == 200, detail.text
        assert detail.json()["track"]["current_master_delivery"]["id"] == final.id
        assert "approve_final" in detail.json()["track"]["allowed_actions"]
        response = client.post(
            f"/api/tracks/{track.id}/final-review/approve", headers=auth_headers(user),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == expected_status
        assert response.json()["current_master_delivery"]["id"] == final.id
    db_session.refresh(final)
    db_session.refresh(old)
    db_session.refresh(draft)
    assert final.producer_approved_at is not None
    assert final.submitter_approved_at is not None
    assert [(delivery.producer_approved_at, delivery.submitter_approved_at) for delivery in [old, draft]] == history_before
    published = client.get(f"/api/tracks/{track.id}/master-audio", headers=auth_headers(actors.member))
    assert published.status_code == 200, published.text


def test_same_title_source_uploads_keep_distinct_files_and_references(
    client, db_session, auth_headers, master_access, monkeypatch,
):
    actors = master_access
    values = iter(["a" * 32, "b" * 32])
    monkeypatch.setattr(tracks_router, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(values))))
    tracks = []
    for content in [b"ID3first-source", b"ID3second-source"]:
        response = client.post(
            "/api/tracks", headers=auth_headers(actors.composer),
            data={
                "title": "Shared Source Title", "artist": "Composer", "album_id": actors.album.id,
                "composer_ids": [str(actors.composer.id)],
            },
            files={"file": ("source.mp3", content, "audio/mpeg")},
        )
        assert response.status_code == 201, response.text
        tracks.append(response.json())
    paths = [Path(track["file_path"]) for track in tracks]
    assert len(set(paths)) == 2
    assert [path.read_bytes() for path in paths] == [b"ID3first-source", b"ID3second-source"]
    versions = db_session.scalars(
        select(TrackSourceVersion).where(TrackSourceVersion.track_id.in_([track["id"] for track in tracks])),
    ).all()
    assert {version.file_path for version in versions} == {str(path) for path in paths}
    for track, content in zip(tracks, [b"ID3first-source", b"ID3second-source"]):
        served = client.get(f"/api/tracks/{track['id']}/audio", headers=auth_headers(actors.composer))
        assert served.status_code == 200, served.text
        assert served.content == content


@pytest.mark.parametrize("upload_kind", ["source", "master"])
@pytest.mark.parametrize("extension, mime_type", [(".mp3", "audio/mpeg"), (".flac", "audio/flac")])
def test_long_unicode_title_upload_preserves_audio_bytes_with_bounded_filename(
    client, db_session, factory, auth_headers, master_access, upload_kind, extension, mime_type,
):
    actors = master_access
    title = "音乐标题" * 25
    content = b"long-unicode-title-audio"
    if upload_kind == "source":
        response = client.post(
            "/api/tracks", headers=auth_headers(actors.composer),
            data={
                "title": title, "artist": "Composer", "album_id": actors.album.id,
                "composer_ids": [str(actors.composer.id)],
            },
            files={"file": (f"source{extension}", content, mime_type)},
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        file_path = payload["file_path"]
        audio_path = f"/api/tracks/{payload['id']}/audio"
    else:
        track = factory.track(album=actors.album, submitter=actors.composer, status="mastering")
        track.title = title
        db_session.commit()
        response = client.post(
            f"/api/tracks/{track.id}/master-deliveries", headers=auth_headers(actors.engineer),
            files={"file": (f"master{extension}", content, mime_type)},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        file_path = payload["current_master_delivery"]["file_path"]
        audio_path = f"/api/tracks/{track.id}/master-audio"

    assert payload["title"] == title
    path = Path(file_path)
    assert len(path.name.encode("utf-8")) <= 255
    assert path.suffix == extension
    assert "音乐" in path.stem
    assert path.read_bytes() == content
    served = client.get(audio_path, headers=auth_headers(actors.composer))
    assert served.status_code == 200, served.text
    assert served.content == content


def test_same_title_master_uploads_keep_legacy_and_cross_cycle_files_distinct(
    client, db_session, factory, auth_headers, master_access, monkeypatch, upload_dir,
):
    actors = master_access
    first = factory.track(album=actors.album, submitter=actors.composer, status="mastering")
    second = factory.track(album=actors.album, submitter=actors.composer, status="mastering")
    first.title = second.title = "Shared Master Title"
    legacy_path = upload_dir / "Shared_Master_Title_master_v1.mp3"
    legacy_path.write_bytes(b"ID3legacy-master")
    legacy = factory.master_delivery(track=first, uploaded_by=actors.engineer, file_path=str(legacy_path))
    values = iter(["a" * 32, "b" * 32, "c" * 32])
    monkeypatch.setattr(tracks_router, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(values))))
    new_deliveries = []
    for track, cycle, content in [
        (first, 2, b"ID3cycle-two"), (second, 1, b"ID3different-track"), (first, 3, b"ID3cycle-three"),
    ]:
        track.workflow_cycle = cycle
        db_session.commit()
        response = client.post(
            f"/api/tracks/{track.id}/master-deliveries", headers=auth_headers(actors.engineer),
            files={"file": ("master.mp3", content, "audio/mpeg")},
        )
        assert response.status_code == 200, response.text
        delivery = response.json()["current_master_delivery"]
        assert delivery["workflow_cycle"] == cycle
        assert delivery["delivery_number"] == 1
        new_deliveries.append((track.id, delivery, content))
        db_session.refresh(track)
    paths = [Path(delivery["file_path"]) for _, delivery, _ in new_deliveries]
    assert len(set(paths + [legacy_path])) == 4
    assert [path.read_bytes() for path in paths] == [content for _, _, content in new_deliveries]
    assert legacy_path.read_bytes() == b"ID3legacy-master"
    for track_id, delivery, content in new_deliveries:
        response = client.get(
            f"/api/tracks/{track_id}/master-deliveries/{delivery['id']}/audio",
            headers=auth_headers(actors.composer),
        )
        assert response.status_code == 200, response.text
        assert response.content == content
    old = client.get(
        f"/api/tracks/{first.id}/master-deliveries/{legacy.id}/audio", headers=auth_headers(actors.composer),
    )
    assert old.status_code == 200, old.text
    assert old.content == b"ID3legacy-master"
    db_session.refresh(legacy)
    assert legacy.file_path == str(legacy_path)


@pytest.mark.parametrize("resolve_json", [False, True])
def test_r2_master_download_signs_only_authorized_delivery_and_is_not_cacheable(
    client, db_session, factory, auth_headers, master_access, monkeypatch, resolve_json,
):
    actors = master_access
    track = factory.track(album=actors.album, submitter=actors.composer, status=TrackStatus.COMPLETED)
    old = factory.master_delivery(track=track, uploaded_by=actors.engineer)
    final = factory.master_delivery(track=track, uploaded_by=actors.engineer, delivery_number=2)
    old.file_path = f"tracks/{track.id}/master/1/old.mp3"
    final.file_path = f"tracks/{track.id}/master/2/final.mp3"
    old.storage_backend = final.storage_backend = "r2"
    db_session.commit()
    calls = []

    def sign_download(key):
        calls.append(key)
        return f"https://private.example/{key}?signature=test"

    monkeypatch.setitem(sys.modules, "app.services.r2", SimpleNamespace(generate_download_url=sign_download))
    params = {"resolve": "json"} if resolve_json else {}
    for path in [
        f"/api/tracks/{track.id}/master-audio",
        f"/api/tracks/{track.id}/master-deliveries/{final.id}/audio",
    ]:
        response = client.get(path, params=params, headers=auth_headers(actors.member), follow_redirects=False)
        assert response.status_code == (200 if resolve_json else 302), response.text
        assert response.headers["cache-control"] == "private, no-store"
        url = response.json()["url"] if resolve_json else response.headers["location"]
        assert url == f"https://private.example/{final.file_path}?signature=test"
    assert calls == [final.file_path, final.file_path]
    denied = client.get(
        f"/api/tracks/{track.id}/master-deliveries/{old.id}/audio", params=params,
        headers=auth_headers(actors.member), follow_redirects=False,
    )
    assert denied.status_code == 403, denied.text
    outsider = client.get(
        f"/api/tracks/{track.id}/master-audio", params=params,
        headers=auth_headers(actors.outsider), follow_redirects=False,
    )
    assert outsider.status_code == 403, outsider.text
    track.status = "final_review"
    track.is_public = True
    db_session.commit()
    active = client.get(
        f"/api/tracks/{track.id}/master-audio", params=params,
        headers=auth_headers(actors.member), follow_redirects=False,
    )
    assert active.status_code == 403, active.text
    assert calls == [final.file_path, final.file_path]
    participant = client.get(
        f"/api/tracks/{track.id}/master-deliveries/{old.id}/audio", params=params,
        headers=auth_headers(actors.composer), follow_redirects=False,
    )
    assert participant.status_code == (200 if resolve_json else 302), participant.text
    assert calls[-1] == old.file_path


@pytest.mark.parametrize("resolve_json", [False, True])
def test_r2_source_download_remains_visible_and_uses_private_signed_response(
    client, db_session, factory, auth_headers, master_access, monkeypatch, resolve_json,
):
    actors = master_access
    track = factory.track(album=actors.album, submitter=actors.composer, status="mastering")
    source = track.source_versions[0]
    track.is_public = True
    track.file_path = source.file_path = f"tracks/{track.id}/source/1/source.mp3"
    track.storage_backend = source.storage_backend = "r2"
    db_session.commit()
    url = f"https://private.example/{source.file_path}?signature=test"
    monkeypatch.setitem(sys.modules, "app.services.r2", SimpleNamespace(generate_download_url=lambda _key: url))
    for path in [f"/api/tracks/{track.id}/audio", f"/api/tracks/{track.id}/source-versions/{source.id}/audio"]:
        response = client.get(
            path, params={"resolve": "json"} if resolve_json else {},
            headers=auth_headers(actors.member), follow_redirects=False,
        )
        assert response.status_code == (200 if resolve_json else 302), response.text
        assert response.headers["cache-control"] == "private, no-store"
        assert (response.json()["url"] if resolve_json else response.headers["location"]) == url


def test_album_export_only_packages_current_cycle_final_files_and_remains_manager_only(
    client, db_session, factory, auth_headers, master_access, monkeypatch, upload_dir,
):
    actors = master_access
    current = factory.track(
        album=actors.album, submitter=actors.composer, status=TrackStatus.COMPLETED, workflow_cycle=2,
    )
    old_path = upload_dir / "export-old.mp3"
    old_path.write_bytes(b"ID3old-export")
    factory.master_delivery(track=current, uploaded_by=actors.engineer, workflow_cycle=1, delivery_number=9, file_path=str(old_path))
    draft_path = upload_dir / "export-draft.mp3"
    draft_path.write_bytes(b"ID3draft-export")
    factory.master_delivery(track=current, uploaded_by=actors.engineer, workflow_cycle=2, delivery_number=1, file_path=str(draft_path))
    final_path = upload_dir / "export-final.mp3"
    final_path.write_bytes(b"ID3final-export")
    factory.master_delivery(track=current, uploaded_by=actors.engineer, workflow_cycle=2, delivery_number=2, file_path=str(final_path))
    stale = factory.track(album=actors.album, submitter=actors.composer, status=TrackStatus.COMPLETED)
    factory.master_delivery(track=stale, uploaded_by=actors.engineer)
    stale.workflow_cycle = 2
    text_track = factory.track(album=actors.album, submitter=actors.composer, status=TrackStatus.COMPLETED)
    factory.master_delivery(track=text_track, uploaded_by=actors.engineer, delivery_kind="text", delivery_message="Private external note")
    active = factory.track(album=actors.album, submitter=actors.composer, status="mastering")
    active.is_public = True
    factory.master_delivery(track=active, uploaded_by=actors.engineer)
    db_session.commit()
    monkeypatch.setattr("app.services.audio.embed_audio_metadata", lambda _path, **_metadata: None)

    denied = client.get(f"/api/albums/{actors.album.id}/export/stream", headers=auth_headers(actors.member))
    assert denied.status_code == 403, denied.text
    response = client.get(f"/api/albums/{actors.album.id}/export/stream", headers=auth_headers(actors.producer))
    assert response.status_code == 200, response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[0] == {"type": "start", "total": 1}
    completed = next(event for event in events if event["type"] == "complete")
    assert completed["total"] == completed["processed"] == 1
    download_id = completed["download_id"]
    try:
        denied_download = client.get(
            f"/api/albums/{actors.album.id}/export/download/{download_id}", headers=auth_headers(actors.member),
        )
        assert denied_download.status_code == 403, denied_download.text
        archive = client.get(
            f"/api/albums/{actors.album.id}/export/download/{download_id}", headers=auth_headers(actors.producer),
        )
        assert archive.status_code == 200, archive.text
        with zipfile.ZipFile(io.BytesIO(archive.content)) as exported:
            manifest = json.loads(exported.read("manifest.json"))
            assert len(manifest["tracks"]) == 1
            assert manifest["tracks"][0]["title"] == current.title
            assert exported.read(manifest["tracks"][0]["file"]) == b"ID3final-export"
    finally:
        with albums_router._export_temp_store_lock:
            artifact = albums_router._export_temp_store.pop(download_id, None)
        if artifact is not None:
            Path(artifact.file_path).unlink(missing_ok=True)
    assert old_path.read_bytes() == b"ID3old-export"
    assert draft_path.read_bytes() == b"ID3draft-export"
    assert final_path.read_bytes() == b"ID3final-export"


def test_issue_linked_timeline_events_follow_issue_visibility_without_hiding_progression(
    client, db_session, factory, auth_headers, master_access,
):
    actors = master_access
    track = factory.track(album=actors.album, submitter=actors.composer, status="final_review")
    track.is_public = True
    peer = factory.issue(track=track, author=actors.producer, phase=IssuePhase.PEER)
    private = factory.issue(track=track, author=actors.engineer, phase=IssuePhase.MASTERING)
    hidden_peer = factory.issue(track=track, author=actors.producer, phase=IssuePhase.PEER)
    hidden_peer.status = IssueStatus.PENDING_DISCUSSION
    rows = [
        ("issue_created", {"issue_id": peer.id, "phase": "peer", "title": "Visible peer event"}),
        ("issue_created", {"issue_id": private.id, "phase": "peer", "title": "Private row wins over stale phase"}),
        ("issue_updated", {"issue_id": str(private.id), "phase": "peer", "title": "Private row with string ID"}),
        ("issue_created", {"issue_id": 987654, "phase": "mastering", "title": "Deleted private mastering event"}),
        ("issue_updated", {"issue_id": 987655, "title": "Unclassified deleted issue edit"}),
        ("issue_created", {"phase": "final_review", "title": "Unknown private final event"}),
        ("issue_created", {"issue_id": hidden_peer.id, "phase": "peer", "title": "Composer-hidden peer event"}),
        ("workflow_transition", {"phase": "mastering", "title": "Public progression"}),
    ]
    for kind, payload in rows:
        db_session.add(WorkflowEvent(
            track_id=track.id, album_id=actors.album.id, actor_user_id=actors.producer.id,
            event_type=kind, payload=json.dumps(payload),
        ))
    db_session.commit()
    member = client.get(f"/api/tracks/{track.id}", headers=auth_headers(actors.member))
    assert member.status_code == 200, member.text
    member_titles = {event["payload"]["title"] for event in member.json()["events"] if event["payload"] and "title" in event["payload"]}
    assert member_titles == {"Visible peer event", "Composer-hidden peer event", "Public progression"}
    composer = client.get(f"/api/tracks/{track.id}", headers=auth_headers(actors.composer))
    assert composer.status_code == 200, composer.text
    composer_titles = {event["payload"]["title"] for event in composer.json()["events"] if event["payload"] and "title" in event["payload"]}
    assert "Composer-hidden peer event" not in composer_titles
    assert "Deleted private mastering event" in composer_titles
    assert "Unknown private final event" in composer_titles
    assert "Public progression" in composer_titles
    engineer = client.get(f"/api/tracks/{track.id}", headers=auth_headers(actors.engineer))
    assert engineer.status_code == 200, engineer.text
    engineer_titles = {event["payload"]["title"] for event in engineer.json()["events"] if event["payload"] and "title" in event["payload"]}
    assert engineer_titles == {payload["title"] for _, payload in rows}
