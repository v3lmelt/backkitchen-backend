import base64
import json
from types import SimpleNamespace

import pytest

from app.models.comment import Comment
from app.models.comment_image import CommentImage
from app.models.issue import IssuePhase
from app.models.issue_image import IssueImage
from app.models.workflow_event import WorkflowEvent


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP438LwHwAGjAKD73hbzwAAAABJRU5ErkJggg==")


@pytest.fixture
def production_records(factory, db_session, upload_dir):
    actors = {
        "producer": factory.user(role="producer"),
        "engineer": factory.user(),
        "composer": factory.user(),
        "co_composer": factory.user(),
        "member": factory.user(),
        "unrelated_producer": factory.user(role="producer"),
    }
    album = factory.album(
        producer=actors["producer"], mastering_engineer=actors["engineer"],
        members=[user for role, user in actors.items() if role != "producer"],
    )
    track = factory.track(
        album=album, submitter=actors["composer"], composers=[actors["co_composer"]], status="mastering",
    )
    track.is_public = True
    db_session.commit()
    delivery = factory.master_delivery(track=track, uploaded_by=actors["engineer"])
    master = factory.issue(
        track=track, author=actors["engineer"], phase=IssuePhase.MASTERING,
        source_version_id=track.source_versions[0].id,
    )
    final = factory.issue(
        track=track, author=actors["producer"], phase=IssuePhase.FINAL_REVIEW,
        master_delivery_id=delivery.id,
    )
    peer = factory.issue(
        track=track, author=actors["producer"], phase=IssuePhase.PEER,
        source_version_id=track.source_versions[0].id,
    )
    old_comment = Comment(issue_id=master.id, author_id=actors["member"].id, content="Earlier member comment")
    peer_internal = Comment(issue_id=peer.id, author_id=actors["producer"].id, content="Internal peer note", visibility="internal")
    db_session.add_all([old_comment, peer_internal])
    db_session.flush()
    for folder in ("issue_images", "comment_images"):
        (upload_dir / folder).mkdir()
    issue_image = IssueImage(issue_id=master.id, file_path="issue_images/production.png")
    comment_image = CommentImage(comment_id=old_comment.id, file_path="comment_images/production.png")
    internal_image = CommentImage(comment_id=peer_internal.id, file_path="comment_images/internal.png")
    for image in (issue_image, comment_image, internal_image):
        (upload_dir / image.file_path).write_bytes(PNG)
    (upload_dir / "cover.png").write_bytes(PNG)
    db_session.add_all([issue_image, comment_image, internal_image])
    issue_events = [
        WorkflowEvent(
            track_id=track.id, album_id=album.id, actor_user_id=actors["engineer"].id,
            event_type="issue_created", payload=json.dumps({"issue_id": issue.id, "phase": issue.phase, "title": issue.title}),
        )
        for issue in (master, final, peer)
    ]
    progress = WorkflowEvent(track_id=track.id, album_id=album.id, event_type="track_status_changed", from_status="mastering", to_status="final_review")
    db_session.add_all([*issue_events, progress])
    db_session.commit()
    return SimpleNamespace(
        actors=actors, album=album, track=track, delivery=delivery, master=master, final=final, peer=peer,
        old_comment=old_comment, peer_internal=peer_internal, issue_image=issue_image,
        comment_image=comment_image, internal_image=internal_image, issue_events=issue_events, progress=progress,
    )


@pytest.mark.parametrize("status", ["mastering", "completed"])
def test_record_visibility_is_independent_of_completed_audio_access(
    client, db_session, auth_headers, production_records, status,
):
    records = production_records
    records.track.status = status
    db_session.commit()
    participants = {"producer", "engineer", "composer", "co_composer"}
    for role, user in records.actors.items():
        headers = auth_headers(user)
        expected_ids = {records.peer.id}
        if role in participants:
            expected_ids.update({records.master.id, records.final.id})
        response = client.get(f"/api/tracks/{records.track.id}/issues", headers=headers)
        assert response.status_code == 200, (role, response.text)
        assert {issue["id"] for issue in response.json()} == expected_ids, role
        detail = client.get(f"/api/tracks/{records.track.id}", headers=headers)
        assert detail.status_code == 200, (role, detail.text)
        assert {issue["id"] for issue in detail.json()["issues"]} == expected_ids, role
        assert detail.json()["track"]["open_issue_count"] == len(expected_ids), role
        event_ids = {records.issue_events[2].id, records.progress.id}
        if role in participants:
            event_ids.update(event.id for event in records.issue_events[:2])
        assert {event["id"] for event in detail.json()["events"]} == event_ids, role
        for issue in (records.master, records.final):
            issue_response = client.get(f"/api/issues/{issue.id}", headers=headers)
            assert issue_response.status_code == (200 if role in participants else 404), (role, issue_response.text)
        audio = client.get(f"/api/tracks/{records.track.id}/master-audio", headers=headers)
        assert audio.status_code == (200 if status == "completed" or role in participants else 403), (role, audio.text)


def test_former_comment_author_cannot_read_or_mutate_private_records(client, auth_headers, production_records):
    records = production_records
    member_headers = auth_headers(records.actors["member"])
    path = f"/api/comments/{records.old_comment.id}"
    assert client.get(path + "/history", headers=member_headers).status_code == 404
    assert client.patch(path, json={"content": "changed"}, headers=member_headers).status_code == 404
    assert client.delete(path, headers=member_headers).status_code == 404
    assert client.post(f"/api/issues/{records.master.id}/comments", data={"content": "new private reply"}, headers=member_headers).status_code == 404
    participant = client.get(f"/api/issues/{records.master.id}", headers=auth_headers(records.actors["engineer"]))
    assert participant.status_code == 200
    assert {comment["id"] for comment in participant.json()["comments"]} == {records.old_comment.id}
    public_reply = client.post(f"/api/issues/{records.peer.id}/comments", data={"content": "Public peer reply"}, headers=member_headers)
    assert public_reply.status_code == 201, public_reply.text
    assert public_reply.json()["content"] == "Public peer reply"


def test_attachment_files_follow_record_permissions_and_public_covers_remain_available(
    client, auth_headers, production_records,
):
    records = production_records
    engineer_headers = auth_headers(records.actors["engineer"])
    member_headers = auth_headers(records.actors["member"])
    detail = client.get(f"/api/issues/{records.master.id}", headers=engineer_headers).json()
    urls = [detail["images"][0]["image_url"], detail["comments"][0]["images"][0]["image_url"]]
    for url in urls:
        allowed = client.get(url, headers=engineer_headers)
        assert allowed.status_code == 200, allowed.text
        assert allowed.content == PNG
        assert client.get(url).status_code == 401
        assert client.get(url, headers=member_headers).status_code == 404
    for image in (records.issue_image, records.comment_image, records.internal_image):
        assert client.get(f"/uploads/{image.file_path}", headers=engineer_headers).status_code == 404
    internal_url = f"/api/comment-images/{records.internal_image.id}/file"
    assert client.get(internal_url, headers=member_headers).status_code == 200
    assert client.get(internal_url, headers=auth_headers(records.actors["composer"])).status_code == 404
    assert client.get("/uploads/cover.png").content == PNG


def test_private_notifications_are_filtered_before_pagination_and_mark_read(
    client, factory, db_session, auth_headers, production_records,
):
    records = production_records
    user = records.actors["member"]
    generic = factory.notification(user=user, type="track_status_changed")
    public = factory.notification(user=user, related_track_id=records.track.id, related_issue_id=records.peer.id)
    private = factory.notification(user=user, related_track_id=records.track.id, related_issue_id=records.master.id)
    headers = auth_headers(user)
    first = client.get("/api/notifications", params={"limit": 1, "offset": 0}, headers=headers)
    second = client.get("/api/notifications", params={"limit": 1, "offset": 1}, headers=headers)
    assert {row["id"] for row in first.json()} == {public.id}
    assert {row["id"] for row in second.json()} == {generic.id}
    assert client.patch(f"/api/notifications/{private.id}/read", headers=headers).status_code == 404
    marked = client.patch("/api/notifications/read-all", headers=headers)
    assert marked.status_code == 200
    assert marked.json()["updated"] == 2
    db_session.refresh(private)
    assert private.is_read is False
    assert {row["id"] for row in client.get("/api/notifications", headers=headers).json()} == {public.id, generic.id}


def test_private_replies_skip_former_participants_and_ordinary_mentions(
    client, db_session, auth_headers, production_records,
):
    from sqlalchemy import select
    from app.models.notification import Notification

    records = production_records
    member = records.actors["member"]
    mentioned_member = records.actors["unrelated_producer"]
    reply = client.post(
        f"/api/issues/{records.master.id}/comments",
        data={"content": f"@user:{member.id} @user:{mentioned_member.id} please review"},
        headers=auth_headers(records.actors["engineer"]),
    )
    assert reply.status_code == 201, reply.text
    member_notifications = list(db_session.scalars(select(Notification).where(Notification.user_id == member.id)))
    assert member_notifications == []
    mention_notifications = list(db_session.scalars(select(Notification).where(Notification.user_id == mentioned_member.id)))
    assert mention_notifications == []
    composer_notifications = list(db_session.scalars(select(Notification).where(Notification.user_id == records.actors["composer"].id)))
    assert {notification.type for notification in composer_notifications} == {"new_comment"}
    peer_reply = client.post(
        f"/api/issues/{records.peer.id}/comments",
        data={"content": f"@user:{mentioned_member.id} peer feedback"},
        headers=auth_headers(records.actors["engineer"]),
    )
    assert peer_reply.status_code == 201, peer_reply.text
    peer_mentions = list(db_session.scalars(select(Notification).where(Notification.user_id == mentioned_member.id)))
    assert {notification.type for notification in peer_mentions} == {"user_mentioned"}


def test_reassigning_engineer_revokes_records_and_old_image_links(
    client, factory, db_session, auth_headers, production_records,
):
    records = production_records
    old_engineer = records.actors["engineer"]
    factory.notification(user=old_engineer, related_track_id=records.track.id, related_issue_id=records.master.id)
    old_headers = auth_headers(old_engineer)
    assert client.get(f"/api/issues/{records.master.id}", headers=old_headers).status_code == 200
    records.album.mastering_engineer_id = records.actors["unrelated_producer"].id
    records.track.status = "completed"
    db_session.commit()
    assert client.get(f"/api/issues/{records.master.id}", headers=old_headers).status_code == 404
    assert client.get(f"/api/comment-images/{records.comment_image.id}/file", headers=old_headers).status_code == 404
    assert client.get("/api/notifications", headers=old_headers).json() == []
    assert client.get(f"/api/tracks/{records.track.id}/master-audio", headers=old_headers).status_code == 200


def test_custom_mastering_phase_and_assignee_do_not_expand_record_audience(
    client, db_session, auth_headers, production_records,
):
    records = production_records
    records.album.workflow_config = json.dumps({
        "version": 2,
        "steps": [{
            "id": "delivery_check", "label": "Custom mastering review", "type": "review",
            "ui_variant": "mastering", "assignee_role": "peer_reviewer", "order": 0,
            "transitions": {"approve": "__completed"},
        }],
    })
    records.track.status = "delivery_check"
    records.track.peer_reviewer_id = records.actors["member"].id
    records.master.phase = "delivery_check"
    db_session.commit()
    headers = auth_headers(records.actors["member"])
    assert client.get(f"/api/issues/{records.master.id}", headers=headers).status_code == 404
    created = client.post(
        f"/api/tracks/{records.track.id}/issues",
        json={"title": "Unauthorized custom-stage issue", "description": "description", "phase": "delivery_check", "severity": "minor", "markers": []},
        headers=headers,
    )
    assert created.status_code == 403, created.text
    assert client.get(f"/api/issues/{records.master.id}", headers=auth_headers(records.actors["engineer"])).status_code == 200


def test_album_activity_filters_private_records_before_visible_pagination(
    client, db_session, auth_headers, production_records,
):
    from datetime import datetime, timedelta, timezone

    records = production_records
    base = datetime(2030, 1, 1, tzinfo=timezone.utc)
    db_session.add_all([
        WorkflowEvent(
            track_id=records.track.id, album_id=records.album.id,
            actor_user_id=records.actors["engineer"].id, event_type="issue_updated",
            payload=json.dumps({"issue_id": records.master.id, "title": "private edit"}),
            created_at=base + timedelta(seconds=index),
        )
        for index in range(110)
    ])
    visible = WorkflowEvent(
        track_id=records.track.id, album_id=records.album.id,
        actor_user_id=records.actors["producer"].id, event_type="issue_created",
        payload=json.dumps({"issue_id": records.peer.id, "phase": "peer"}),
        created_at=base - timedelta(seconds=1),
    )
    db_session.add(visible)
    db_session.commit()
    headers = auth_headers(records.actors["member"])
    path = f"/api/albums/{records.album.id}/activity"
    first = client.get(path, params={"limit": 1}, headers=headers)
    second = client.get(path, params={"limit": 1, "offset": 1}, headers=headers)
    assert first.status_code == 200, first.text
    assert {event["id"] for event in first.json()} == {visible.id}
    assert {event["id"] for event in second.json()} == {records.progress.id}
    all_visible = client.get(path, params={"limit": 100}, headers=headers).json()
    assert {event["id"] for event in all_visible} == {visible.id, records.issue_events[2].id, records.progress.id}
    created_page = client.get(path, params={"event_type": "issue_created", "limit": 1, "offset": 1}, headers=headers)
    assert {event["id"] for event in created_page.json()} == {records.issue_events[2].id}
    actor_page = client.get(path, params={"limit": 1}, headers=auth_headers(records.actors["engineer"]))
    assert actor_page.status_code == 200
    assert actor_page.json()[0]["payload"]["issue_id"] == records.master.id


def test_album_summaries_apply_independent_visible_event_caps(
    client, factory, db_session, auth_headers, production_records,
):
    from datetime import datetime, timedelta, timezone

    records = production_records
    base = datetime(2030, 1, 1, tzinfo=timezone.utc)
    db_session.add_all([
        WorkflowEvent(
            track_id=records.track.id, album_id=records.album.id, event_type="issue_updated",
            payload=json.dumps({"issue_id": records.master.id, "title": "private update"}),
            created_at=base + timedelta(seconds=index),
        )
        for index in range(110)
    ])
    second_album = factory.album(
        producer=records.actors["producer"], mastering_engineer=records.actors["engineer"],
        members=[records.actors["member"], records.actors["composer"]],
    )
    second_track = factory.track(album=second_album, submitter=records.actors["composer"], status="completed")
    peer = factory.issue(track=second_track, author=records.actors["producer"], phase=IssuePhase.PEER)
    second_events = [
        WorkflowEvent(
            track_id=second_track.id, album_id=second_album.id, event_type="issue_created",
            payload=json.dumps({"issue_id": peer.id, "phase": "peer"}),
            created_at=datetime(2031, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index),
        )
        for index in range(6)
    ]
    db_session.add_all(second_events)
    db_session.commit()
    response = client.get("/api/albums", headers=auth_headers(records.actors["member"]))
    assert response.status_code == 200, response.text
    albums = {album["id"]: album for album in response.json()}
    visible_first = {records.issue_events[2].id, records.progress.id}
    assert {event["id"] for event in albums[records.album.id]["recent_events"]} == visible_first
    assert {event["id"] for event in albums[second_album.id]["recent_events"]} == {event.id for event in second_events}
    from app.routers.albums import _build_album_stats_summary_map

    capped = _build_album_stats_summary_map(
        [records.album, second_album], db_session, records.actors["member"], recent_event_limit=3,
    )
    assert {event.id for event in capped[records.album.id].recent_events} == visible_first
    assert {event.id for event in capped[second_album.id].recent_events} == {event.id for event in second_events[-3:]}
    stats = client.get(f"/api/albums/{records.album.id}/stats", headers=auth_headers(records.actors["member"]))
    assert stats.status_code == 200, stats.text
    assert {event["id"] for event in stats.json()["recent_events"]} == visible_first
