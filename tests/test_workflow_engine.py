import copy
import json
from datetime import datetime, timezone
from unittest.mock import ANY

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select

from app.models.issue import IssuePhase, IssueStatus
from app.models.master_delivery import MasterDelivery
from app.models.stage_assignment import StageAssignment
from app.workflow_defaults import DEFAULT_WORKFLOW_CONFIG
from app.workflow_engine import (
    _discard_internal_review_issues,
    _notify_assigned_reviewers,
    ASSIGNMENT_CANCEL_REASON_REASSIGNED,
    StepDef,
    assign_peer_reviewer_for_step,
    assign_reviewers,
    compute_reopen_resets,
    execute_delivery_confirm,
    execute_delivery_upload,
    execute_reopen,
    execute_transition,
    migrate_tracks_on_workflow_change,
    parse_workflow_config,
)


def test_parse_workflow_config_upgrades_v1_gate_steps(factory):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    album = factory.album(producer=producer, mastering_engineer=mastering)
    album.workflow_config = json.dumps(
        {
            "version": 1,
            "steps": [
                {
                    "id": "submitted",
                    "label": "Submitted",
                    "type": "gate",
                    "assignee_role": "producer",
                    "order": 0,
                    "transitions": {"accept": "peer_review"},
                },
                {
                    "id": "peer_review",
                    "label": "Peer Review",
                    "type": "review",
                    "assignee_role": "peer_reviewer",
                    "order": 1,
                    "transitions": {"pass": "__completed"},
                },
            ],
        }
    )

    parsed = parse_workflow_config(album)

    assert parsed["version"] == 2
    assert [step["type"] for step in parsed["steps"]] == ["approval", "review"]


def test_assign_reviewers_auto_falls_back_to_manual_when_pool_is_insufficient(
    monkeypatch,
    db_session,
    factory,
):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=None)
    step = StepDef(
        id="peer_review",
        label="Peer Review",
        type="review",
        ui_variant="peer_review",
        assignee_role="peer_reviewer",
        order=1,
        transitions={"pass": "__completed"},
        assignment_mode="auto",
        reviewer_pool=[submitter.id, reviewer.id],
        required_reviewer_count=2,
    )
    notifications: list[dict] = []

    def capture_notify(_db, recipients, event_type, title, body, *_args, **kwargs):
        notifications.append({
            "recipients": list(recipients),
            "event_type": event_type,
            "title": title,
            "body": body,
            "kwargs": kwargs,
        })

    monkeypatch.setattr("app.workflow_engine.notify", capture_notify)

    assigned = assign_reviewers(db_session, album, track, step, BackgroundTasks(), actor=reviewer)

    assert assigned == []
    assert notifications == [{
        "recipients": [producer.id],
        "event_type": "reviewer_assignment_needed",
        "title": "需要手动指派评审人",
        "body": f"「{track.title}」已进入「同行评审」阶段，需要制作人手动指派评审人。",
        "kwargs": {
            "related_track_id": track.id,
            "background_tasks": ANY,
            "album_id": track.album_id,
            "webhook_context": {"actor_id": reviewer.id, "actor_name": reviewer.display_name},
        },
    }]
    assert db_session.scalars(select(StageAssignment).where(StageAssignment.track_id == track.id)).all() == []


def test_assign_peer_reviewer_for_step_honors_assignee_override(
    monkeypatch,
    db_session,
    factory,
):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    old_reviewer = factory.user(username="old_reviewer")
    new_reviewer = factory.user(username="new_reviewer")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, old_reviewer, new_reviewer],
    )
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=old_reviewer)
    existing = StageAssignment(
        track_id=track.id,
        stage_id="peer_review",
        user_id=old_reviewer.id,
        status="pending",
    )
    db_session.add(existing)
    db_session.commit()

    step = StepDef(
        id="peer_review",
        label="Peer Review",
        type="review",
        ui_variant="peer_review",
        assignee_role="peer_reviewer",
        order=1,
        transitions={"pass": "__completed"},
        assignment_mode="manual",
        required_reviewer_count=1,
        assignee_user_id=new_reviewer.id,
    )
    notifications: list[dict] = []

    def capture_notify(_db, recipients, event_type, title, body, *_args, **kwargs):
        notifications.append({
            "recipients": list(recipients),
            "event_type": event_type,
            "title": title,
            "body": body,
            "kwargs": kwargs,
        })

    monkeypatch.setattr("app.workflow_engine.notify", capture_notify)

    assign_peer_reviewer_for_step(db_session, album, track, step, BackgroundTasks(), actor=producer)
    db_session.commit()
    db_session.refresh(track)

    assignments = db_session.scalars(
        select(StageAssignment)
        .where(
            StageAssignment.track_id == track.id,
            StageAssignment.stage_id == "peer_review",
        )
        .order_by(StageAssignment.user_id.asc())
    ).all()

    assert track.peer_reviewer_id == new_reviewer.id
    assert [(item.user_id, item.status, item.cancellation_reason) for item in assignments] == [
        (old_reviewer.id, "cancelled", ASSIGNMENT_CANCEL_REASON_REASSIGNED),
        (new_reviewer.id, "pending", None),
    ]
    assert notifications == [{
        "recipients": [new_reviewer.id],
        "event_type": "reviewer_assigned",
        "title": "你被指派为评审人",
        "body": f"你已被指派评审「{track.title}」（同行评审）。",
        "kwargs": {
            "related_track_id": track.id,
            "background_tasks": ANY,
            "album_id": track.album_id,
            "webhook_context": {"actor_id": producer.id, "actor_name": producer.display_name},
        },
    }]


def test_notify_assigned_reviewers_reopened_includes_actor_context(monkeypatch, factory, db_session):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="peer_review", peer_reviewer=reviewer)
    step = StepDef(
        id="peer_review",
        label="Peer Review",
        type="review",
        ui_variant="peer_review",
        assignee_role="peer_reviewer",
        order=1,
        transitions={"pass": "__completed"},
    )
    notifications: list[dict] = []

    def capture_notify(_db, recipients, event_type, title, body, *_args, **kwargs):
        notifications.append({
            "recipients": list(recipients),
            "event_type": event_type,
            "title": title,
            "body": body,
            "kwargs": kwargs,
        })

    monkeypatch.setattr("app.workflow_engine.notify", capture_notify)

    _notify_assigned_reviewers(
        db_session,
        track,
        step,
        [reviewer.id],
        BackgroundTasks(),
        reopened=True,
        actor=producer,
    )

    assert notifications == [{
        "recipients": [reviewer.id],
        "event_type": "reviewer_assigned",
        "title": "评审已重新开启",
        "body": f"「{track.title}」已重新进入「同行评审」阶段，请继续评审。",
        "kwargs": {
            "related_track_id": track.id,
            "background_tasks": ANY,
            "album_id": track.album_id,
            "webhook_context": {"actor_id": producer.id, "actor_name": producer.display_name},
        },
    }]


def test_discard_internal_review_issues_includes_actor_context(monkeypatch, db_session, factory):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])
    track = factory.track(album=album, submitter=submitter, status="custom_review", peer_reviewer=reviewer)
    issue = factory.issue(
        track=track,
        author=reviewer,
        phase=IssuePhase.PEER,
        status=IssueStatus.PENDING_DISCUSSION,
        source_version_id=track.source_versions[-1].id,
    )
    notifications: list[dict] = []

    def capture_notify(_db, recipients, event_type, title, body, *_args, **kwargs):
        notifications.append({
            "recipients": list(recipients),
            "event_type": event_type,
            "title": title,
            "body": body,
            "kwargs": kwargs,
        })

    monkeypatch.setattr("app.workflow_engine.notify", capture_notify)

    _discard_internal_review_issues(db_session, track, BackgroundTasks(), actor=submitter)

    assert issue.status == IssueStatus.INTERNAL_RESOLVED
    assert notifications == [{
        "recipients": [reviewer.id],
        "event_type": "issue_status_changed",
        "title": "内部讨论问题已自动结案",
        "body": f"由于「{track.title}」已进入修订阶段，1 个待讨论问题已自动内部结案。",
        "kwargs": {
            "related_track_id": track.id,
            "background_tasks": ANY,
            "album_id": track.album_id,
            "webhook_context": {"actor_id": submitter.id, "actor_name": submitter.display_name},
        },
    }]


def test_execute_transition_review_quorum_notification_includes_actor_context(monkeypatch, db_session, factory):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer_a = factory.user(username="reviewer_a")
    reviewer_b = factory.user(username="reviewer_b")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer_a, reviewer_b],
        workflow_config={
            "version": 2,
            "steps": [
                {
                    "id": "custom_review",
                    "label": "Custom Review",
                    "type": "review",
                    "ui_variant": "generic",
                    "assignee_role": "peer_reviewer",
                    "order": 0,
                    "required_reviewer_count": 2,
                    "transitions": {"pass": "producer_gate"},
                },
                {
                    "id": "producer_gate",
                    "label": "Producer Gate",
                    "type": "approval",
                    "assignee_role": "producer",
                    "order": 1,
                    "transitions": {"approve": "__completed"},
                },
            ],
        },
    )
    track = factory.track(album=album, submitter=submitter, status="custom_review", peer_reviewer=None)
    db_session.add_all([
        StageAssignment(track_id=track.id, stage_id="custom_review", user_id=reviewer_a.id, status="completed"),
        StageAssignment(track_id=track.id, stage_id="custom_review", user_id=reviewer_b.id, status="pending"),
    ])
    db_session.commit()

    notifications: list[dict] = []

    def capture_notify(_db, recipients, event_type, title, body, *_args, **kwargs):
        notifications.append({
            "recipients": list(recipients),
            "event_type": event_type,
            "title": title,
            "body": body,
            "kwargs": kwargs,
        })

    monkeypatch.setattr("app.workflow_engine.notify", capture_notify)

    execute_transition(db_session, album, track, reviewer_b, "pass", BackgroundTasks())

    assert notifications == [{
        "recipients": [reviewer_a.id],
        "event_type": "workflow_review_ready_for_final_decision",
        "title": "同行评审已达成人数",
        "body": f"「{track.title}」的同行评审已满足人数要求，请评审组内汇总意见并提交最终结论。",
        "kwargs": {
            "related_track_id": track.id,
            "background_tasks": ANY,
            "album_id": track.album_id,
            "webhook_context": {"actor_id": reviewer_b.id, "actor_name": reviewer_b.display_name},
        },
    }]


def test_execute_delivery_upload_respects_confirmation_requirement(factory):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")

    assert execute_delivery_upload(album, track) is None
    assert execute_delivery_confirm(album, track) == "final_review"


def test_execute_delivery_upload_raises_when_deliver_transition_is_missing(factory):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter],
        workflow_config={
            "version": 2,
            "steps": [
                {
                    "id": "custom_delivery",
                    "label": "Custom Delivery",
                    "type": "delivery",
                    "assignee_role": "mastering_engineer",
                    "order": 0,
                    "transitions": {},
                }
            ],
        },
    )
    track = factory.track(album=album, submitter=submitter, status="custom_delivery")

    with pytest.raises(HTTPException) as excinfo:
        execute_delivery_upload(album, track)

    assert excinfo.value.status_code == 500
    assert "has no 'deliver' transition" in excinfo.value.detail


def test_migrate_tracks_on_workflow_change_reassigns_review_override(db_session, factory):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer],
    )
    old_config = {
        "version": 2,
        "steps": [
            {
                "id": "intake",
                "label": "Intake",
                "type": "approval",
                "assignee_role": "producer",
                "order": 0,
                "transitions": {"accept": "obsolete_review"},
            },
            {
                "id": "obsolete_review",
                "label": "Obsolete Review",
                "type": "review",
                "assignee_role": "peer_reviewer",
                "order": 1,
                "transitions": {"pass": "obsolete_revision"},
            },
            {
                "id": "obsolete_revision",
                "label": "Obsolete Revision",
                "type": "revision",
                "assignee_role": "submitter",
                "order": 2,
                "transitions": {},
            },
        ],
    }
    new_config = {
        "version": 2,
        "steps": [
            {
                "id": "intake",
                "label": "Intake",
                "type": "approval",
                "assignee_role": "producer",
                "order": 0,
                "transitions": {"accept": "producer_gate"},
            },
            {
                "id": "producer_gate",
                "label": "Producer Gate",
                "type": "approval",
                "assignee_role": "producer",
                "order": 1,
                "transitions": {"approve": "__completed"},
            },
            {
                "id": "custom_review",
                "label": "Custom Review",
                "type": "review",
                "assignee_role": "peer_reviewer",
                "order": 2,
                "transitions": {"pass": "__completed"},
                "assignee_user_id": reviewer.id,
            },
        ],
    }

    migrated_track = factory.track(album=album, submitter=submitter, status="obsolete_revision", peer_reviewer=None)
    assigned_track = factory.track(album=album, submitter=submitter, status="custom_review", peer_reviewer=None)

    migrations = migrate_tracks_on_workflow_change(
        db_session,
        album,
        old_config,
        new_config,
        background_tasks=None,
    )
    db_session.commit()
    db_session.refresh(migrated_track)
    db_session.refresh(assigned_track)

    assignment = db_session.scalar(
        select(StageAssignment).where(
            StageAssignment.track_id == assigned_track.id,
            StageAssignment.stage_id == "custom_review",
            StageAssignment.status == "pending",
        )
    )

    assert migrations == [
        {
            "track_id": migrated_track.id,
            "track_title": migrated_track.title,
            "from_step": "obsolete_revision",
            "to_step": "producer_gate",
        }
    ]
    assert migrated_track.status == "producer_gate"
    assert assigned_track.peer_reviewer_id == reviewer.id
    assert assignment is not None
    assert assignment.user_id == reviewer.id


def test_execute_reopen_to_final_review_keeps_cycle_and_only_clears_approvals(
    db_session,
    factory,
):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer],
        workflow_config=copy.deepcopy(DEFAULT_WORKFLOW_CONFIG),
    )
    track = factory.track(
        album=album,
        submitter=submitter,
        status="completed",
        peer_reviewer=reviewer,
        version=3,
        workflow_cycle=2,
    )
    source_version = track.source_versions[-1]
    checklist = factory.checklist(
        track=track,
        reviewer=reviewer,
        source_version_id=source_version.id,
        label="Balance",
        passed=True,
    )
    delivery = factory.master_delivery(
        track=track,
        uploaded_by=mastering,
        delivery_number=2,
        workflow_cycle=2,
    )
    delivery.producer_approved_at = datetime.now(timezone.utc)
    delivery.submitter_approved_at = datetime.now(timezone.utc)
    db_session.commit()

    preview = compute_reopen_resets(db_session, album, track, "final_review")
    resets = execute_reopen(db_session, album, track, producer, "final_review")
    db_session.commit()
    db_session.refresh(track)
    db_session.refresh(delivery)

    assert preview == ["master_delivery_approvals"]
    assert resets == ["master_delivery_approvals"]
    assert track.status == "final_review"
    assert track.workflow_cycle == 2
    assert delivery.producer_approved_at is None
    assert delivery.submitter_approved_at is None
    assert db_session.get(MasterDelivery, delivery.id) is not None
    assert db_session.get(type(checklist), checklist.id) is not None
