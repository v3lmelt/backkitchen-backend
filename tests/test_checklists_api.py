from app.models.track import TrackStatus


def test_submit_checklist_replaces_existing_items_for_current_source_version(client, db_session, factory, auth_headers):
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
    source_version = track.source_versions[-1]
    factory.checklist(track=track, reviewer=reviewer, source_version_id=source_version.id, label="Balance", passed=False)

    response = client.post(
        f"/api/tracks/{track.id}/checklist",
        headers=auth_headers(reviewer),
        json={"items": [{"label": "Balance", "passed": True, "note": "fixed"}]},
    )

    assert response.status_code == 201
    items = db_session.get(type(track), track.id).checklist_items
    assert len(items) == 1
    assert items[0].passed is True
    assert items[0].note == "fixed"


def test_get_checklist_only_returns_current_source_version_items(client, factory, auth_headers):
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
        version=2,
    )
    old_version = factory.source_version(track=track, uploaded_by=submitter, version_number=1)
    current_version = track.source_versions[-1]
    factory.checklist(track=track, reviewer=reviewer, source_version_id=old_version.id, label="Arrangement")
    factory.checklist(track=track, reviewer=reviewer, source_version_id=current_version.id, label="Balance")

    response = client.get(f"/api/tracks/{track.id}/checklist", headers=auth_headers(reviewer))

    assert response.status_code == 200
    assert [item["label"] for item in response.json()] == ["Balance"]


def test_submit_checklist_requires_assigned_reviewer(client, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    outsider = factory.user(username="outsider")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer, outsider])
    track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.PEER_REVIEW,
        peer_reviewer=reviewer,
    )

    response = client.post(
        f"/api/tracks/{track.id}/checklist",
        headers=auth_headers(outsider),
        json={"items": [{"label": "Balance", "passed": True}]},
    )

    assert response.status_code == 403
