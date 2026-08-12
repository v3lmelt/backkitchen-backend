"""Tests for the Phase 4a backend contract fields/endpoints.

Covers:
* canonical anonymous-display token (``app.anon`` / ``UserRead.anon_token``)
* viewer-context flags on the track payload
* server-computed ``review_state`` summary
* per-step ``is_mastering_related`` metadata
* ``GET /api/workflow/default-config``
* server-side issue phase inference on creation
"""

from app.anon import fnv1a_anon_token
from app.models.issue import IssuePhase
from app.models.stage_assignment import StageAssignment
from app.schemas.schemas import UserRead
from app.workflow_defaults import DEFAULT_WORKFLOW_CONFIG


def _make_track_with_viewer(factory, *, status="peer_review"):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user()
    reviewer = factory.user(username="reviewer")
    viewer = factory.user(username="viewer")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer, viewer],
    )
    track = factory.track(
        album=album,
        submitter=submitter,
        status=status,
        peer_reviewer=reviewer,
    )
    return producer, mastering, submitter, reviewer, viewer, album, track


# ---------------------------------------------------------------------------
# Anonymous token
# ---------------------------------------------------------------------------


def test_fnv1a_anon_token_matches_frontend_vectors():
    # Reference values produced by the frontend's hashId (src/utils/hash.ts).
    assert fnv1a_anon_token(1) == "DA2AD3"
    assert fnv1a_anon_token(42) == "F45E2B"
    assert fnv1a_anon_token(0) == "D92AD1"
    assert fnv1a_anon_token(999999) == "4445E1"


def test_fnv1a_anon_token_rotates_away_from_legacy_bare_id_hash():
    def legacy(user_id: int) -> str:
        h = 2166136261
        for char in str(user_id):
            h ^= ord(char)
            h = (h * 16777619) & 0xFFFFFFFF
        return f"{h:08X}"[:6]

    assert fnv1a_anon_token(1) != legacy(1)


def test_user_read_includes_anon_token(factory):
    user = factory.user()
    payload = UserRead.model_validate(user).model_dump(mode="json")
    assert payload["anon_token"] == fnv1a_anon_token(user.id)


def test_masked_peer_identity_uses_canonical_token(client, db_session, factory, auth_headers):
    _, _, _, reviewer, viewer, _, track = _make_track_with_viewer(factory)
    # Plain members may only view public tracks; keep the peer-anonymous phase.
    track.is_public = True
    db_session.commit()
    factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER)

    response = client.get(f"/api/tracks/{track.id}", headers=auth_headers(viewer))

    assert response.status_code == 200
    payload = response.json()["track"]
    # Track-level identities are fully redacted for this viewer...
    assert payload["peer_reviewer"] is None
    assert payload["submitter"] is None
    # ...while issue authors are masked with the canonical v2 token.
    reviewer_token = fnv1a_anon_token(reviewer.id)
    author = response.json()["issues"][0]["author"]
    assert author["display_name"] == f"#{reviewer_token}"
    assert author["username"] == f"anon_{reviewer_token.lower()}"
    assert author["anon_token"] == reviewer_token


def test_anonymous_view_nulls_raw_identity_ids(client, db_session, factory, auth_headers):
    _, _, _, reviewer, viewer, _, track = _make_track_with_viewer(factory)
    track.is_public = True
    db_session.commit()
    factory.issue(track=track, author=reviewer, phase=IssuePhase.PEER)

    payload = client.get(f"/api/tracks/{track.id}", headers=auth_headers(viewer)).json()["track"]

    # Raw numeric IDs are as sensitive as the masked identity objects: an
    # anonymous viewer must not be able to enumerate users by pairing IDs with
    # the deterministic FNV anon tokens.
    assert payload["submitter_id"] is None
    assert payload["composer_ids"] is None
    assert payload["peer_reviewer_id"] is None
    assert payload["proxy_uploader_id"] is None


def test_privileged_view_keeps_raw_identity_ids(client, db_session, factory, auth_headers):
    producer, _, submitter, reviewer, _, _, track = _make_track_with_viewer(factory)

    payload = client.get(f"/api/tracks/{track.id}", headers=auth_headers(producer)).json()["track"]

    assert payload["submitter_id"] == submitter.id
    assert payload["composer_ids"] == [submitter.id]
    assert payload["peer_reviewer_id"] == reviewer.id


# ---------------------------------------------------------------------------
# Viewer-context flags
# ---------------------------------------------------------------------------


def test_track_detail_viewer_flags_for_composer(client, db_session, factory, auth_headers):
    _, _, submitter, _, _, _, track = _make_track_with_viewer(factory, status="intake")

    payload = client.get(f"/api/tracks/{track.id}", headers=auth_headers(submitter)).json()["track"]

    assert payload["viewer_is_composer_actor"] is True
    assert payload["viewer_is_mastering_participant"] is True
    # intake step is assigned to the producer, not the composer
    assert payload["viewer_is_step_assignee"] is False


def test_track_detail_viewer_flags_for_producer(client, db_session, factory, auth_headers):
    producer, _, _, _, _, _, track = _make_track_with_viewer(factory, status="intake")

    payload = client.get(f"/api/tracks/{track.id}", headers=auth_headers(producer)).json()["track"]

    assert payload["viewer_is_album_manager"] is True
    assert payload["viewer_is_composer_actor"] is False
    assert payload["viewer_is_mastering_participant"] is True
    assert payload["viewer_is_step_assignee"] is True


def test_track_detail_viewer_flags_for_mastering_engineer(client, db_session, factory, auth_headers):
    _, mastering, _, _, _, _, track = _make_track_with_viewer(factory, status="intake")

    payload = client.get(f"/api/tracks/{track.id}", headers=auth_headers(mastering)).json()["track"]

    assert payload["viewer_is_composer_actor"] is False
    assert payload["viewer_is_mastering_participant"] is True
    assert payload["viewer_is_step_assignee"] is False


def test_track_detail_viewer_flags_for_peer_reviewer_assignment(client, db_session, factory, auth_headers):
    _, _, _, reviewer, _, _, track = _make_track_with_viewer(factory)
    db_session.add(
        StageAssignment(
            track_id=track.id,
            stage_id="peer_review",
            user_id=reviewer.id,
            status="pending",
        )
    )
    db_session.commit()

    payload = client.get(f"/api/tracks/{track.id}", headers=auth_headers(reviewer)).json()["track"]

    assert payload["viewer_is_composer_actor"] is False
    assert payload["viewer_is_step_assignee"] is True


# ---------------------------------------------------------------------------
# review_state
# ---------------------------------------------------------------------------


def test_review_state_reflects_assignment_progress(client, db_session, factory, auth_headers):
    producer, _, _, reviewer, _, _, track = _make_track_with_viewer(factory)
    assignment = StageAssignment(
        track_id=track.id,
        stage_id="peer_review",
        user_id=reviewer.id,
        status="pending",
    )
    db_session.add(assignment)
    db_session.commit()

    payload = client.get(f"/api/tracks/{track.id}", headers=auth_headers(producer)).json()["track"]

    review_state = payload["review_state"]
    assert review_state["step_id"] == "peer_review"
    assert review_state["assignment_mode"] == "auto"
    assert review_state["required_review_count"] == 1
    assert review_state["active_assignment_count"] == 1
    assert review_state["completed_review_count"] == 0
    assert review_state["quorum_reached"] is False
    assert review_state["requires_group_finalization"] is False

    assignment.status = "completed"
    db_session.commit()

    review_state = client.get(
        f"/api/tracks/{track.id}", headers=auth_headers(producer)
    ).json()["track"]["review_state"]
    assert review_state["completed_review_count"] == 1
    assert review_state["quorum_reached"] is True


def test_review_state_is_none_outside_review_steps(client, db_session, factory, auth_headers):
    producer, _, _, _, _, _, track = _make_track_with_viewer(factory, status="intake")

    payload = client.get(f"/api/tracks/{track.id}", headers=auth_headers(producer)).json()["track"]

    assert payload["review_state"] is None


# ---------------------------------------------------------------------------
# Step metadata
# ---------------------------------------------------------------------------


def test_track_detail_serves_step_mastering_metadata(client, db_session, factory, auth_headers):
    producer, _, _, _, _, _, track = _make_track_with_viewer(factory, status="intake")

    detail = client.get(f"/api/tracks/{track.id}", headers=auth_headers(producer)).json()

    assert detail["track"]["workflow_step"]["is_mastering_related"] is False
    steps = {step["id"]: step for step in detail["workflow_config"]["steps"]}
    assert steps["mastering"]["is_mastering_related"] is True
    assert steps["mastering_revision"]["is_mastering_related"] is True
    assert steps["peer_review"]["is_mastering_related"] is False
    assert steps["final_review"]["is_mastering_related"] is False


def test_album_workflow_endpoint_serves_step_mastering_metadata(client, db_session, factory, auth_headers):
    producer, _, _, _, _, album, _ = _make_track_with_viewer(factory)

    response = client.get(f"/api/albums/{album.id}/workflow", headers=auth_headers(producer))

    assert response.status_code == 200
    steps = {step["id"]: step for step in response.json()["steps"]}
    assert steps["mastering"]["is_mastering_related"] is True
    assert steps["intake"]["is_mastering_related"] is False


# ---------------------------------------------------------------------------
# Default workflow config endpoint
# ---------------------------------------------------------------------------


def test_default_workflow_config_endpoint(client, factory, auth_headers):
    user = factory.user()

    response = client.get("/api/workflow/default-config", headers=auth_headers(user))

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == DEFAULT_WORKFLOW_CONFIG["version"]
    assert [step["id"] for step in payload["steps"]] == [
        step["id"] for step in DEFAULT_WORKFLOW_CONFIG["steps"]
    ]
    steps = {step["id"]: step for step in payload["steps"]}
    assert steps["mastering"]["is_mastering_related"] is True
    assert steps["peer_review"]["is_mastering_related"] is False


def test_default_workflow_config_endpoint_requires_auth(client):
    assert client.get("/api/workflow/default-config").status_code == 401


# ---------------------------------------------------------------------------
# Issue phase inference
# ---------------------------------------------------------------------------


def test_create_issue_infers_phase_when_omitted(client, factory, auth_headers):
    _, _, _, reviewer, _, _, track = _make_track_with_viewer(factory)

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Clicks at intro",
            "description": "Please clean this up.",
            "severity": "major",
            "markers": [{"marker_type": "point", "time_start": 1.2}],
        },
    )

    assert response.status_code == 201
    assert response.json()["phase"] == "peer"


def test_create_issue_explicit_phase_still_wins(client, factory, auth_headers):
    _, _, _, reviewer, _, _, track = _make_track_with_viewer(factory)

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Clicks at intro",
            "description": "Please clean this up.",
            "phase": "peer_review",
            "severity": "major",
            "markers": [],
        },
    )

    assert response.status_code == 201
    # Step ids are canonicalized to the canonical phase.
    assert response.json()["phase"] == "peer"


def test_create_issue_explicit_mismatched_phase_still_rejected(client, factory, auth_headers):
    _, _, _, reviewer, _, _, track = _make_track_with_viewer(factory)

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        json={
            "title": "Clicks at intro",
            "description": "Please clean this up.",
            "phase": "mastering",
            "severity": "major",
            "markers": [],
        },
    )

    assert response.status_code == 400


def test_create_issue_infers_phase_for_form_uploads(client, factory, auth_headers):
    _, _, _, reviewer, _, _, track = _make_track_with_viewer(factory)

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(reviewer),
        data={
            "title": "Clicks at intro",
            "description": "Please clean this up.",
            "severity": "major",
        },
    )

    assert response.status_code == 201
    assert response.json()["phase"] == "peer"


def test_create_issue_without_phase_on_terminal_track_conflicts(client, db_session, factory, auth_headers):
    producer, _, submitter, _, _, _, track = _make_track_with_viewer(factory)
    track.status = "completed"
    db_session.commit()

    response = client.post(
        f"/api/tracks/{track.id}/issues",
        headers=auth_headers(producer),
        json={
            "title": "Late note",
            "description": "Track already completed.",
            "severity": "minor",
            "markers": [],
        },
    )

    assert response.status_code == 409
