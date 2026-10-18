from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import boto3
import botocore.auth
import pytest
from botocore.config import Config
from botocore.httpsession import URLLib3Session

from app.models.comment import Comment
from app.models.comment_audio import CommentAudio
from app.models.discussion import TrackDiscussion, TrackDiscussionAudio
from app.models.issue import IssuePhase, IssueStatus
from app.models.issue_audio import IssueAudio
from app.security import create_access_token
from app.services import r2


@pytest.fixture
def signed_r2(monkeypatch):
    def reject_network(*_args, **_kwargs):
        pytest.fail("R2 capability tests must not make network requests")

    monkeypatch.setattr(URLLib3Session, "send", reject_network)
    monkeypatch.setattr(
        botocore.auth,
        "get_current_datetime",
        lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    client = boto3.client(
        "s3",
        endpoint_url="https://r2.example.invalid",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        region_name="auto",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    signing_requests = []

    def record_signing(request, **_kwargs):
        signing_requests.append(request)

    client.meta.events.register("before-sign.s3", record_signing)
    monkeypatch.setattr(r2, "get_r2_client", lambda: client)
    monkeypatch.setattr(r2.settings, "R2_BUCKET_NAME", "test-audio")
    monkeypatch.setattr(r2.settings, "R2_PRESIGNED_UPLOAD_EXPIRY", 900)
    try:
        yield signing_requests
    finally:
        client.close()


def _assert_signed_download(url, object_key):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "r2.example.invalid"
    assert unquote(parsed.path) == f"/test-audio/{object_key}"
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["300"]
    assert query["X-Amz-Date"] == ["20260102T030405Z"]
    assert query["response-cache-control"] == ["private, no-store"]
    assert len(query["X-Amz-Signature"][0]) == 64


def test_download_url_is_short_lived_signed_get_without_network(signed_r2):
    object_key = "tracks/42/source/reference + 中文.wav"
    url = r2.generate_download_url(object_key)

    _assert_signed_download(url, object_key)
    assert len(signed_r2) == 1
    assert signed_r2[0].method == "GET"


def test_upload_url_signs_private_no_store_cache_control(signed_r2):
    object_key = "tracks/42/source/reference.wav"
    url = r2.generate_upload_url(object_key, "audio/wav")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)

    assert unquote(parsed.path) == f"/test-audio/{object_key}"
    assert query["X-Amz-Expires"] == ["900"]
    assert query["X-Amz-SignedHeaders"] == ["cache-control;content-type;host"]
    assert len(query["X-Amz-Signature"][0]) == 64
    assert len(signed_r2) == 1
    assert signed_r2[0].method == "PUT"
    assert signed_r2[0].headers["Cache-Control"] == "private, no-store"
    assert signed_r2[0].headers["Content-Type"] == "audio/wav"


@pytest.fixture(params=["issue", "comment", "discussion"])
def audio_asset(request, db_session, factory):
    producer = factory.user(role="producer")
    mastering = factory.user(role="mastering_engineer")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    outsider = factory.user(username="outsider")
    album = factory.album(
        producer=producer,
        mastering_engineer=mastering,
        members=[submitter, reviewer],
    )
    track = factory.track(
        album=album,
        submitter=submitter,
        peer_reviewer=reviewer,
        status="peer_review",
    )
    kind = request.param
    object_key = f"tracks/{track.id}/{kind}/reference + 中文.wav"
    values = {
        "file_path": object_key,
        "storage_backend": "r2",
        "original_filename": "reference.wav",
    }
    issue = None
    comment = None
    discussion = None
    if kind in {"issue", "comment"}:
        issue = factory.issue(
            track=track,
            author=reviewer,
            phase=IssuePhase.PEER,
            source_version_id=track.source_versions[-1].id,
        )
    if kind == "issue":
        audio = IssueAudio(issue_id=issue.id, **values)
    elif kind == "comment":
        comment = Comment(issue_id=issue.id, author_id=reviewer.id, content="Audio note")
        db_session.add(comment)
        db_session.flush()
        audio = CommentAudio(comment_id=comment.id, **values)
    else:
        discussion = TrackDiscussion(
            track_id=track.id,
            author_id=reviewer.id,
            content="Audio discussion",
            phase="general",
        )
        db_session.add(discussion)
        db_session.flush()
        audio = TrackDiscussionAudio(discussion_id=discussion.id, **values)
    db_session.add(audio)
    db_session.commit()
    return SimpleNamespace(
        kind=kind,
        audio=audio,
        url=f"/api/{kind}-audios/{audio.id}/file",
        producer=producer,
        submitter=submitter,
        reviewer=reviewer,
        outsider=outsider,
        issue=issue,
        comment=comment,
        discussion=discussion,
    )


@pytest.mark.parametrize("resolve", [None, "json"])
@pytest.mark.parametrize("authentication", ["bearer", "token"])
def test_authorized_attachment_api_returns_uncacheable_signed_url(
    client, auth_headers, audio_asset, signed_r2, resolve, authentication
):
    params = {"resolve": resolve} if resolve else {}
    headers = {}
    if authentication == "bearer":
        headers = auth_headers(audio_asset.producer)
    else:
        params["token"] = create_access_token(audio_asset.producer)
    response = client.get(
        audio_asset.url, params=params, headers=headers, follow_redirects=False
    )

    assert response.headers["cache-control"] == "private, no-store"
    if resolve == "json":
        assert response.status_code == 200
        url = response.json()["url"]
    else:
        assert response.status_code == 302
        url = response.headers["location"]
    _assert_signed_download(url, audio_asset.audio.file_path)
    assert len(signed_r2) == 1
    assert signed_r2[0].method == "GET"


@pytest.mark.parametrize("resolve", [None, "json"])
@pytest.mark.parametrize("authentication", ["none", "bearer", "token"])
def test_unauthorized_attachment_api_rejects_before_signing(
    client, auth_headers, audio_asset, signed_r2, resolve, authentication
):
    params = {"resolve": resolve} if resolve else {}
    headers = {}
    if authentication == "bearer":
        headers = auth_headers(audio_asset.outsider)
    elif authentication == "token":
        params["token"] = create_access_token(audio_asset.outsider)
    response = client.get(
        audio_asset.url, params=params, headers=headers, follow_redirects=False
    )

    assert response.status_code == (401 if authentication == "none" else 403)
    assert signed_r2 == []


@pytest.mark.parametrize("resolve", [None, "json"])
def test_hidden_attachment_rejects_track_composer_before_signing(
    client, db_session, auth_headers, audio_asset, signed_r2, resolve
):
    if audio_asset.kind == "issue":
        audio_asset.issue.status = IssueStatus.PENDING_DISCUSSION
    elif audio_asset.kind == "comment":
        audio_asset.comment.visibility = "internal"
    else:
        audio_asset.discussion.visibility = "internal"
    db_session.commit()
    response = client.get(
        audio_asset.url,
        params={"resolve": resolve} if resolve else {},
        headers=auth_headers(audio_asset.submitter),
        follow_redirects=False,
    )

    assert response.status_code == (403 if audio_asset.kind == "discussion" else 404)
    assert signed_r2 == []


@pytest.mark.parametrize("audio_asset", ["discussion"], indirect=True)
@pytest.mark.parametrize("resolve", [None, "json"])
def test_mastering_discussion_rejects_peer_reviewer_before_signing(
    client, db_session, auth_headers, audio_asset, signed_r2, resolve
):
    audio_asset.discussion.phase = "mastering"
    db_session.commit()
    response = client.get(
        audio_asset.url,
        params={"resolve": resolve} if resolve else {},
        headers=auth_headers(audio_asset.reviewer),
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert signed_r2 == []
