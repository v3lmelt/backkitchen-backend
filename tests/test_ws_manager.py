import asyncio
import json

from app.ws_manager import ConnectionManager, UserConnectionManager


class DummyWebSocket:
    def __init__(self, *, fail_send: bool = False):
        self.accepted = False
        self.closed: list[tuple[int | None, str | None]] = []
        self.sent: list[str] = []
        self.fail_send = fail_send

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closed.append((code, reason))

    async def send_text(self, payload: str) -> None:
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append(payload)


def test_connection_manager_enforces_total_and_track_limits():
    manager = ConnectionManager()
    manager.MAX_TOTAL_CONNECTIONS = 1
    first = DummyWebSocket()
    second = DummyWebSocket()

    assert asyncio.run(manager.connect(1, first)) is True
    assert first.accepted is True
    assert asyncio.run(manager.connect(2, second)) is False
    assert second.accepted is True
    assert second.closed == [(1013, "Server connection limit reached")]

    per_track = ConnectionManager()
    per_track.MAX_CONNECTIONS_PER_TRACK = 1
    track_first = DummyWebSocket()
    track_second = DummyWebSocket()

    assert asyncio.run(per_track.connect(7, track_first)) is True
    assert asyncio.run(per_track.connect(7, track_second)) is False
    assert track_second.closed == [(1013, "Track connection limit reached")]


def test_connection_manager_broadcast_prunes_dead_connections():
    manager = ConnectionManager()
    alive = DummyWebSocket()
    dead = DummyWebSocket(fail_send=True)

    assert asyncio.run(manager.connect(9, alive)) is True
    assert asyncio.run(manager.connect(9, dead)) is True

    message = {"type": "track_updated", "track_id": 9}
    asyncio.run(manager.broadcast(9, message))

    assert alive.sent == [json.dumps(message)]
    assert manager.active_connections[9] == [alive]
    assert manager._total_count == 1

    manager.disconnect(9, alive)
    assert 9 not in manager.active_connections
    assert manager._total_count == 0


def test_user_connection_manager_broadcast_many_dedupes_user_ids():
    manager = UserConnectionManager()
    user_one = DummyWebSocket()
    user_two = DummyWebSocket()

    assert asyncio.run(manager.connect(1, user_one)) is True
    assert asyncio.run(manager.connect(2, user_two)) is True

    payload = {"type": "notifications_updated"}
    asyncio.run(manager.broadcast_many([1, 1, 2], payload))

    expected = json.dumps(payload)
    assert user_one.sent == [expected]
    assert user_two.sent == [expected]
