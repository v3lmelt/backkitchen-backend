from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.models.issue import IssueStatus
from app.models.track import TrackStatus
from app.workflow import (
    current_master_delivery,
    current_source_version,
    ensure_track_visibility,
    log_track_event,
)


def test_current_source_version_returns_latest(factory):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, version=2)
    older = factory.source_version(track=track, uploaded_by=submitter, version_number=1)
    latest = current_source_version(track)
    assert latest is not None
    assert latest.id != older.id
    assert latest.version_number == 2


def test_current_master_delivery_prefers_current_cycle(factory):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status=TrackStatus.FINAL_REVIEW, workflow_cycle=2)
    factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=4, workflow_cycle=1)
    current = factory.master_delivery(track=track, uploaded_by=mastering, delivery_number=1, workflow_cycle=2)
    assert current_master_delivery(track).id == current.id


def test_ensure_track_visibility_allows_submitter_and_blocks_outsider(factory):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[])
    track = factory.track(album=album, submitter=submitter)

    visible_album = ensure_track_visibility(track, submitter, factory.session)
    assert visible_album.id == album.id

    with pytest.raises(HTTPException) as exc:
        ensure_track_visibility(track, outsider, factory.session)
    assert exc.value.status_code == 403


def test_log_track_event_serializes_enums_and_datetimes(factory):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter)

    event = log_track_event(
        factory.session,
        track,
        submitter,
        "issue_updated",
        from_status="peer_review",
        to_status="peer_revision",
        payload={"status": IssueStatus.RESOLVED, "at": datetime(2024, 1, 1, tzinfo=timezone.utc)},
    )

    assert event.payload is not None
    assert "resolved" in event.payload
    assert "2024-01-01T00:00:00+00:00" in event.payload
