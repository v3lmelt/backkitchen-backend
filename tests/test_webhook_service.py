import asyncio
import json
from types import SimpleNamespace

from fastapi import BackgroundTasks

from app import notifications
from app.models.notification import Notification
from app.models.webhook_delivery import WebhookDelivery
from app.services import webhook
from app.services.webhook_adapters import adapt_payload, _feishu_sign


class DummyResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class DummyAsyncClient:
    def __init__(self, response: DummyResponse):
        self.response = response
        self.posts: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url: str, json: dict, headers: dict | None = None, params: dict | None = None):
        self.posts.append({
            "url": url,
            "json": json,
            "headers": headers,
            "params": params,
        })
        return self.response


def test_adapt_payload_supports_generic_and_feishu(monkeypatch):
    payload = {
        "event": "track_reopened",
        "title": "Track reopened",
        "body": "Please review again",
        "context": {
            "album_title": "Album One",
            "track_title": "Track One",
            "from_step": "Mastering",
            "to_step": "Final Review",
            "track_url": "https://frontend.example.com/tracks/1",
        },
    }

    assert adapt_payload(payload) == payload

    monkeypatch.setattr("app.services.webhook_adapters.time.time", lambda: 12345)
    result = adapt_payload(
        payload,
        webhook_type="feishu",
        secret="shared-secret",
        resolved_mentions=[{"name": "Ann", "open_id": "ou_123"}],
    )

    assert result["msg_type"] == "interactive"
    assert result["timestamp"] == "12345"
    assert result["sign"] == _feishu_sign("12345", "shared-secret")
    serialized = json.dumps(result, ensure_ascii=False)
    assert "<at id=ou_123></at>" in serialized
    assert "https://frontend.example.com/tracks/1" in serialized
    assert "Track reopened" in serialized


def test_validate_webhook_url_rejects_private_addresses(monkeypatch):
    monkeypatch.setattr(
        webhook.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (None, None, None, None, ("127.0.0.1", 0)),
        ],
    )

    try:
        webhook._validate_webhook_url("https://internal.example.com/hook")
    except ValueError as exc:
        assert "blocked address" in str(exc)
    else:
        raise AssertionError("Expected webhook URL validation to reject loopback hosts")


def test_post_webhook_persists_success_and_prunes_old_records(db_session, factory, monkeypatch):
    producer = factory.user(role="producer")
    mastering = factory.user(username="mastering")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[producer, mastering])

    for index in range(webhook.MAX_DELIVERY_RECORDS):
        db_session.add(WebhookDelivery(
            album_id=album.id,
            event_type="old_event",
            success=True,
            status_code=200,
            target_url=f"https://old.example.com/{index}",
            error_detail=None,
        ))
    db_session.commit()

    client = DummyAsyncClient(DummyResponse(200))
    monkeypatch.setattr(webhook, "_validate_webhook_url", lambda _url: None)
    monkeypatch.setattr(webhook.httpx, "AsyncClient", lambda timeout=10: client)

    success = asyncio.run(
        webhook.post_webhook(
            "https://hooks.example.com/endpoint",
            {"event": "track_submitted"},
            db=db_session,
            album_id=album.id,
            event_type="track_submitted",
        )
    )

    deliveries = db_session.query(WebhookDelivery).filter_by(album_id=album.id).order_by(WebhookDelivery.id.asc()).all()

    assert success is True
    assert len(deliveries) == webhook.MAX_DELIVERY_RECORDS
    assert all(item.target_url != "https://old.example.com/0" for item in deliveries)
    assert deliveries[-1].target_url == "https://hooks.example.com/endpoint"
    assert deliveries[-1].success is True
    assert client.posts[0]["headers"] == {"Content-Type": "application/json"}


def test_post_webhook_rejected_url_persists_failure(db_session, factory, monkeypatch):
    producer = factory.user(role="producer")
    mastering = factory.user(username="mastering")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[producer, mastering])

    def raise_invalid(_url: str) -> None:
        raise ValueError("blocked")

    monkeypatch.setattr(webhook, "_validate_webhook_url", raise_invalid)

    success = asyncio.run(
        webhook.post_webhook(
            "https://blocked.example.com",
            {"event": "test"},
            db=db_session,
            album_id=album.id,
            event_type="test",
        )
    )

    delivery = db_session.query(WebhookDelivery).filter_by(album_id=album.id).order_by(WebhookDelivery.id.desc()).first()

    assert success is False
    assert delivery is not None
    assert delivery.success is False
    assert delivery.error_detail == "blocked"


def test_notify_dedupes_notifications_and_schedules_broadcast_and_webhook(db_session, factory, monkeypatch):
    first = factory.user(username="first")
    second = factory.user(username="second")

    broadcast_calls: list[list[int | None]] = []
    webhook_calls: list[dict] = []

    monkeypatch.setattr(
        notifications,
        "broadcast_notifications_updated",
        lambda _background_tasks, user_ids: broadcast_calls.append(user_ids),
    )

    def record_webhook(*args, **kwargs):
        webhook_calls.append({
            "album_id": args[2],
            "notified_user_ids": args[8],
            "webhook_context": kwargs.get("webhook_context"),
        })

    monkeypatch.setattr(notifications, "_try_dispatch_webhook", record_webhook)

    notifications.notify(
        db_session,
        [first.id, None, first.id, second.id],
        "track_status_changed",
        "Title",
        "Body",
        related_track_id=123,
        background_tasks=BackgroundTasks(),
        album_id=77,
        webhook_context={"actor_id": first.id},
    )
    db_session.flush()

    rows = db_session.query(Notification).filter_by(related_track_id=123).all()

    assert {row.user_id for row in rows} == {first.id, second.id}
    assert len(broadcast_calls) == 1
    assert set(broadcast_calls[0]) == {first.id, second.id}
    assert len(webhook_calls) == 1
    assert webhook_calls[0]["album_id"] == 77
    assert set(webhook_calls[0]["notified_user_ids"]) == {first.id, second.id}
    assert webhook_calls[0]["webhook_context"] == {"actor_id": first.id}


def test_try_dispatch_webhook_builds_background_task_with_context_and_mentions(db_session, factory, monkeypatch):
    producer = factory.user(role="producer", username="producer")
    producer.feishu_contact = "producer@example.com"
    mastering = factory.user(username="mastering")
    db_session.commit()

    album = factory.album(producer=producer, mastering_engineer=mastering, members=[producer, mastering], title="Album One")
    track = factory.track(album=album, submitter=producer, status="mastering")
    album.webhook_config = json.dumps({
        "enabled": True,
        "url": "https://hooks.example.com/feishu",
        "events": ["track_status_changed"],
        "type": "feishu",
        "app_id": "app-id",
        "app_secret": "app-secret",
        "filter_user_ids": [producer.id],
    })
    db_session.commit()

    background_tasks = BackgroundTasks()
    notifications._try_dispatch_webhook(
        db_session,
        background_tasks,
        album.id,
        "track_status_changed",
        "Track entered mastering",
        "Please review",
        track.id,
        None,
        [producer.id],
        webhook_context={"actor_name": "Producer"},
    )

    assert len(background_tasks.tasks) == 1
    task = background_tasks.tasks[0]

    assert task.func is notifications._deliver_webhook_background
    assert task.args[0] == "https://hooks.example.com/feishu"
    assert task.args[2] == album.id
    assert task.args[3] == "track_status_changed"
    assert task.kwargs["webhook_type"] == "feishu"
    assert task.kwargs["feishu_app_id"] == "app-id"
    assert task.kwargs["feishu_app_secret"] == "app-secret"
    assert task.kwargs["mention_users"] == [{
        "name": producer.display_name,
        "feishu_contact": "producer@example.com",
    }]
    payload = task.args[1]
    assert payload["context"]["album_title"] == album.title
    assert payload["context"]["track_title"] == track.title
    assert payload["context"]["action_required_by"] == producer.display_name
