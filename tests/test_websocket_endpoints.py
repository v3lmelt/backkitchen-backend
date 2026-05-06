import logging
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.main as main
from app.models.stage_assignment import StageAssignment
from app.security import create_access_token


@pytest.fixture
def websocket_client(session_factory, monkeypatch):
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    main.track_manager.active_connections.clear()
    main.track_manager._total_count = 0
    main.notification_manager.active_connections.clear()
    main.notification_manager._total_count = 0

    app = FastAPI()
    app.websocket("/ws/tracks/{track_id}")(main.websocket_track)
    app.websocket("/ws/notifications")(main.websocket_notifications)
    with TestClient(app) as client:
        yield client

    main.track_manager.active_connections.clear()
    main.track_manager._total_count = 0
    main.notification_manager.active_connections.clear()
    main.notification_manager._total_count = 0


def _assert_ws_rejected(client: TestClient, url: str, code: int) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(url):
            pass
    assert exc_info.value.code == code


@pytest.mark.parametrize("assignment_status", ["pending", "completed"])
def test_track_websocket_allows_active_stage_assignment_without_legacy_reviewer(
    websocket_client,
    db_session,
    factory,
    assignment_status,
):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="assigned-reviewer")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer],
    )
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=None)
    db_session.add(
        StageAssignment(
            track_id=track.id,
            stage_id="peer_review",
            user_id=reviewer.id,
            status=assignment_status,
        )
    )
    db_session.commit()

    token = create_access_token(reviewer)
    with websocket_client.websocket_connect(f"/ws/tracks/{track.id}?token={token}") as ws:
        ws.send_text('{"type":"probe"}')
        message = ws.receive_json()

    assert message["type"] == "probe"
    assert message["track_id"] == track.id


def test_track_websocket_rejects_regular_member_without_track_access(
    websocket_client,
    factory,
):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    member = factory.user(username="regular-member")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, member],
    )
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=None)

    _assert_ws_rejected(
        websocket_client,
        f"/ws/tracks/{track.id}?token={create_access_token(member)}",
        4003,
    )


@pytest.mark.parametrize("participant", ["submitter", "producer", "mastering"])
def test_track_websocket_allows_existing_track_participants(
    websocket_client,
    factory,
    participant,
):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=None)
    user = {"submitter": submitter, "producer": producer, "mastering": mastering}[participant]

    with websocket_client.websocket_connect(f"/ws/tracks/{track.id}?token={create_access_token(user)}") as ws:
        ws.send_text('{"type":"probe"}')
        message = ws.receive_json()

    assert message["track_id"] == track.id


@pytest.mark.parametrize("state", ["revoked", "suspended", "deleted"])
@pytest.mark.parametrize("channel", ["track", "notification"])
def test_websockets_reject_unusable_token_user(
    websocket_client,
    db_session,
    factory,
    state,
    channel,
):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username=f"{state}-{channel}-reviewer")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer],
    )
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=None)
    db_session.add(
        StageAssignment(
            track_id=track.id,
            stage_id="peer_review",
            user_id=reviewer.id,
            status="pending",
        )
    )
    db_session.commit()

    token = create_access_token(reviewer)
    if state == "revoked":
        reviewer.session_version += 1
    elif state == "suspended":
        reviewer.suspended_at = datetime.now(timezone.utc)
    else:
        reviewer.deleted_at = datetime.now(timezone.utc)
    db_session.commit()

    url = (
        f"/ws/tracks/{track.id}?token={token}"
        if channel == "track"
        else f"/ws/notifications?token={token}"
    )
    _assert_ws_rejected(websocket_client, url, 4001)


def test_track_websocket_logs_unexpected_handshake_errors(
    websocket_client,
    factory,
    monkeypatch,
    caplog,
):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=None)

    def fail_visibility(*_args, **_kwargs):
        raise RuntimeError("visibility exploded")

    monkeypatch.setattr(main, "ensure_track_visibility", fail_visibility)

    with caplog.at_level(logging.ERROR, logger="app.websocket"):
        _assert_ws_rejected(
            websocket_client,
            f"/ws/tracks/{track.id}?token={create_access_token(submitter)}",
            1011,
        )

    assert "track_ws_rejected" in caplog.text
    assert "reason=unexpected_error" in caplog.text
    assert "visibility exploded" in caplog.text


def test_notification_websocket_logs_unexpected_handshake_errors(
    websocket_client,
    factory,
    monkeypatch,
    caplog,
):
    user = factory.user(username="notification-user")
    token = create_access_token(user)

    def fail_session():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main, "SessionLocal", fail_session)

    with caplog.at_level(logging.ERROR, logger="app.websocket"):
        _assert_ws_rejected(websocket_client, f"/ws/notifications?token={token}", 1011)

    assert "notification_ws_rejected" in caplog.text
    assert "reason=unexpected_error" in caplog.text
    assert "database unavailable" in caplog.text
