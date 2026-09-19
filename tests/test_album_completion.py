from datetime import datetime, timezone

import pytest

from app.models.issue import IssuePhase
from app.routers import albums as albums_router
from app.services.track_queries import is_album_completed


def assert_completion(client, auth_headers, album, viewer, expected):
    headers = auth_headers(viewer)
    listing = client.get('/api/albums', headers=headers)
    detail = client.get(f'/api/albums/{album.id}', headers=headers)
    stats = client.get(f'/api/albums/{album.id}/stats', headers=headers)
    assert listing.status_code == detail.status_code == stats.status_code == 200
    listed = next(item for item in listing.json() if item['id'] == album.id)
    for body in (listed, detail.json(), stats.json()):
        assert body['is_completed'] is expected
    assert detail.json()['archived_at'] is None


@pytest.mark.parametrize('tracks,expected', [
    ([], False),
    ([('completed', False)], True),
    ([('completed', False), ('completed', False)], True),
    ([('completed', False), ('peer_review', False)], False),
    ([('completed', False), ('rejected', False)], True),
    ([('completed', False), ('peer_review', True)], True),
    ([('rejected', False)], False),
    ([('completed', True)], False),
    ([('rejected', False), ('peer_review', True)], False),
])
def test_completion_counts_only_eligible_tracks(
    client, db_session, factory, auth_headers, tracks, expected,
):
    producer = factory.user(role='producer')
    member = factory.user()
    album = factory.album(producer=producer, mastering_engineer=producer, members=[member])
    album.deadline = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for status, archived in tracks:
        track = factory.track(album=album, submitter=member, status=status)
        if archived:
            track.archived_at = datetime.now(timezone.utc)
    db_session.commit()

    assert is_album_completed(db_session, album.id) is expected
    assert_completion(client, auth_headers, album, producer, expected)
    assert_completion(client, auth_headers, album, member, expected)
    response = client.get(f'/api/albums/{album.id}', headers=auth_headers(member))
    assert response.json()['deadline'].startswith('2020-01-01')


def test_completion_reopens_and_preserves_track_visibility(
    client, db_session, factory, auth_headers,
):
    producer = factory.user(role='producer')
    member = factory.user()
    other = factory.user()
    album = factory.album(producer=producer, mastering_engineer=producer, members=[member, other])
    own = factory.track(album=album, submitter=member, status='completed')
    factory.issue(track=own, author=producer, phase=IssuePhase.FINAL_REVIEW)

    assert_completion(client, auth_headers, album, member, True)
    added = factory.track(album=album, submitter=other, status='peer_review')
    assert_completion(client, auth_headers, album, member, False)
    visible = client.get(f'/api/albums/{album.id}/tracks', headers=auth_headers(member))
    assert {track['id'] for track in visible.json()} == {own.id}

    added.status = 'completed'
    db_session.commit()
    assert_completion(client, auth_headers, album, member, True)
    visible = client.get(f'/api/albums/{album.id}/tracks', headers=auth_headers(member))
    assert {track['id'] for track in visible.json()} == {own.id, added.id}

    added.status = 'mastering_revision'
    db_session.commit()
    assert_completion(client, auth_headers, album, member, False)
    archived = client.post(f'/api/tracks/{added.id}/archive', headers=auth_headers(producer))
    assert archived.status_code == 200
    assert_completion(client, auth_headers, album, member, True)
    restored = client.post(f'/api/tracks/{added.id}/restore', headers=auth_headers(producer))
    assert restored.status_code == 200
    assert_completion(client, auth_headers, album, member, False)

    # A historical open issue does not prevent completion or get removed.
    added.status = 'completed'
    db_session.commit()
    assert_completion(client, auth_headers, album, member, True)
    stats = client.get(f'/api/albums/{album.id}/stats', headers=auth_headers(producer)).json()
    assert stats['open_issues'] == 1


def test_album_reads_use_batched_completion_counts(
    client, factory, auth_headers, monkeypatch,
):
    producer = factory.user(role='producer')
    albums = [factory.album(producer=producer, mastering_engineer=producer) for _ in range(3)]
    for album in albums:
        factory.track(album=album, submitter=producer, status='completed')

    def forbid_individual_completion_query(*args, **kwargs):
        raise AssertionError('Album responses must reuse their batch statistics')

    monkeypatch.setattr(albums_router, 'is_album_completed', forbid_individual_completion_query)
    for album in albums:
        assert_completion(client, auth_headers, album, producer, True)
