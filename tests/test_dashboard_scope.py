from datetime import datetime, timezone

import pytest

from app.models.circle import Circle, CircleMember
from app.models.stage_assignment import StageAssignment


def list_ids(client, auth_headers, user, resource, **params):
    response = client.get(f"/api/{resource}", headers=auth_headers(user), params=params)
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()]


@pytest.mark.parametrize("admin", [False, True])
def test_dashboard_scopes_share_real_album_relationships(client, auth_headers, factory, db_session, admin):
    viewer = factory.user(role="producer", is_admin=admin)
    other = factory.user(role="producer")
    albums = {
        "unrelated": factory.album(producer=other, mastering_engineer=other),
        "managed": factory.album(producer=viewer, mastering_engineer=other),
        "owner": factory.album(producer=other, mastering_engineer=other),
        "co_producer": factory.album(producer=other, mastering_engineer=other),
        "member": factory.album(producer=other, mastering_engineer=other, members=[viewer]),
        "mastering": factory.album(producer=other, mastering_engineer=viewer),
    }
    for name in ("owner", "co_producer"):
        circle = Circle(name=name, created_by=viewer.id if name == "owner" else other.id)
        db_session.add(circle)
        db_session.flush()
        albums[name].circle_id = circle.id
        if name == "co_producer":
            db_session.add(CircleMember(circle_id=circle.id, user_id=viewer.id, role=name))
    db_session.commit()
    tracks = {name: factory.track(album=album, submitter=other, status="completed") for name, album in albums.items()}
    for scope, expected in (("managed", {"managed", "owner", "co_producer"}),
                            ("participating", set(albums) - {"unrelated"})):
        assert set(list_ids(client, auth_headers, viewer, "albums", scope=scope)) == {albums[name].id for name in expected}
        assert set(list_ids(client, auth_headers, viewer, "tracks", album_scope=scope)) == {tracks[name].id for name in expected}
    default = list_ids(client, auth_headers, viewer, "tracks")
    assert default == list_ids(client, auth_headers, viewer, "tracks", album_scope="all")
    assert (tracks["unrelated"].id in default) is admin


def test_scope_applies_before_pagination_and_independently_of_search(client, auth_headers, factory, db_session):
    viewer = factory.user(is_admin=True)
    other = factory.user(role="producer")
    unrelated = factory.album(producer=other, mastering_engineer=other)
    factory.track(album=unrelated, submitter=other)
    mine = factory.album(producer=viewer, mastering_engineer=other, title="No title match")
    matching = [factory.track(album=mine, submitter=other, status="completed") for _ in range(3)]
    for track in matching:
        track.title = "Needle"
    rejected = factory.track(album=mine, submitter=other, status="rejected")
    archived = factory.track(album=mine, submitter=other, status="completed")
    archived.archived_at = datetime.now(timezone.utc)
    db_session.commit()
    params = dict(album_scope="managed", search="Needle", limit=1)
    assert list_ids(client, auth_headers, viewer, "tracks", **params, offset=0) == [matching[0].id]
    assert list_ids(client, auth_headers, viewer, "tracks", **params, offset=1) == [matching[1].id]
    assert list_ids(client, auth_headers, viewer, "tracks", **params, offset=2) == [matching[2].id]
    assert list_ids(client, auth_headers, viewer, "tracks", **params, offset=3) == []
    assert list_ids(client, auth_headers, viewer, "albums", scope="managed", search="Needle") == []
    assert list_ids(client, auth_headers, viewer, "tracks", album_scope="managed", status="rejected") == [rejected.id]
    assert list_ids(client, auth_headers, viewer, "tracks", album_scope="managed", album_id=unrelated.id) == []
    assert list_ids(client, auth_headers, viewer, "tracks", album_scope="managed", status="completed") == [track.id for track in matching]


@pytest.mark.parametrize("assignment_status", ["pending", "completed"])
def test_participation_includes_authors_and_only_current_active_reviews(client, auth_headers, factory, db_session, assignment_status):
    viewer = factory.user(is_admin=True)
    other = factory.user(role="producer")
    tracks = {}
    for name in ("submitter", "composer", "review", "old_review", "removed", "archived_review"):
        album = factory.album(producer=other, mastering_engineer=other)
        track = factory.track(
            album=album, submitter=viewer if name == "submitter" else other,
            composers=[viewer] if name == "composer" else [], status="peer_review",
        )
        tracks[name] = track
        if "review" in name or name == "removed":
            db_session.add(StageAssignment(
                track_id=track.id, user_id=viewer.id,
                stage_id="intake" if name == "old_review" else "peer_review",
                status="cancelled" if name == "removed" else assignment_status,
            ))
        if name == "archived_review":
            track.archived_at = datetime.now(timezone.utc)
    db_session.commit()
    expected = {"submitter", "composer", "review"}
    assert set(list_ids(client, auth_headers, viewer, "tracks", album_scope="participating")) == {tracks[name].id for name in expected}
    assert set(list_ids(client, auth_headers, viewer, "albums", scope="participating")) == {tracks[name].album_id for name in expected}
    assert list_ids(client, auth_headers, viewer, "tracks", album_scope="managed") == []


def test_participating_scope_does_not_grant_track_or_album_access(client, auth_headers, factory, db_session):
    viewer = factory.user()
    other = factory.user(role="producer")
    member_album = factory.album(producer=other, mastering_engineer=other, members=[viewer])
    private = factory.track(album=member_album, submitter=other, status="peer_review")
    public = factory.track(album=member_album, submitter=other, status="peer_review")
    public.is_public = True
    outside_album = factory.album(producer=other, mastering_engineer=other)
    own = factory.track(album=outside_album, submitter=viewer, status="peer_review")
    factory.track(album=outside_album, submitter=other, status="peer_review")
    db_session.commit()
    ids = list_ids(client, auth_headers, viewer, "tracks", album_scope="participating")
    assert set(ids) == {public.id, own.id}
    assert private.id not in ids
    assert set(list_ids(client, auth_headers, viewer, "albums", scope="participating")) == {member_album.id}
    assert set(ids) <= set(list_ids(client, auth_headers, viewer, "tracks"))


def test_invalid_dashboard_scope_is_rejected(client, auth_headers, factory):
    viewer = factory.user()
    response = client.get("/api/tracks?album_scope=invalid", headers=auth_headers(viewer))
    assert response.status_code == 422
