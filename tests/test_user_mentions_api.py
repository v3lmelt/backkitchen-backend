from app.models.issue import IssuePhase, IssueStatus
from app.models.notification import Notification
from app.models.stage_assignment import StageAssignment


def _notifications_for(db_session, user_id: int, type_: str = "user_mentioned") -> list[Notification]:
    return (
        db_session.query(Notification)
        .filter(Notification.user_id == user_id, Notification.type == type_)
        .order_by(Notification.id)
        .all()
    )


def test_public_issue_comment_mentions_allowed_user_once(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)
    issue = factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        source_version_id=track.source_versions[-1].id,
    )

    response = client.post(
        f"/api/issues/{issue.id}/comments",
        headers=auth_headers(submitter),
        data={"content": f"Please check @user:{producer.id} and again @user:{producer.id}."},
    )

    assert response.status_code == 201
    mention_notifications = _notifications_for(db_session, producer.id)
    assert len(mention_notifications) == 1
    assert mention_notifications[0].related_track_id == track.id
    assert mention_notifications[0].related_issue_id == issue.id


def test_internal_issue_comment_mentions_do_not_notify_submitter(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)
    issue = factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.PENDING_DISCUSSION,
        source_version_id=track.source_versions[-1].id,
    )

    response = client.post(
        f"/api/issues/{issue.id}/comments",
        headers=auth_headers(reviewer),
        data={"content": f"Internal note for @user:{submitter.id} and @user:{producer.id}."},
    )

    assert response.status_code == 201
    assert _notifications_for(db_session, submitter.id) == []
    assert len(_notifications_for(db_session, producer.id)) == 1


def test_general_discussion_mentions_visible_stage_assignment_user(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    assigned = factory.user(username="assigned")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="peer_review")
    db_session.add(StageAssignment(track_id=track.id, stage_id="peer_review", user_id=assigned.id, status="pending"))
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/discussions",
        headers=auth_headers(submitter),
        data={"content": f"Looping in @user:{assigned.id}."},
    )

    assert response.status_code == 201
    assert len(_notifications_for(db_session, assigned.id)) == 1
    normal_notifications = _notifications_for(db_session, assigned.id, "new_discussion")
    assert len(normal_notifications) == 1


def test_mastering_discussion_mentions_only_mastering_participants(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="mastering", peer_reviewer=reviewer)

    response = client.post(
        f"/api/tracks/{track.id}/discussions",
        headers=auth_headers(mastering),
        data={"phase": "mastering", "content": f"FYI @user:{producer.id} @user:{reviewer.id}."},
    )

    assert response.status_code == 201
    assert len(_notifications_for(db_session, producer.id)) == 1
    assert _notifications_for(db_session, reviewer.id) == []


def test_editing_comment_does_not_send_new_mention_notification(client, db_session, factory, auth_headers):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)
    issue = factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        source_version_id=track.source_versions[-1].id,
    )

    create_response = client.post(
        f"/api/issues/{issue.id}/comments",
        headers=auth_headers(submitter),
        data={"content": "No mention yet."},
    )
    assert create_response.status_code == 201
    comment_id = create_response.json()["id"]

    update_response = client.patch(
        f"/api/comments/{comment_id}",
        headers=auth_headers(submitter),
        json={"content": f"Adding @user:{producer.id} later."},
    )

    assert update_response.status_code == 200
    assert _notifications_for(db_session, producer.id) == []
