import sys
from pathlib import Path
from types import SimpleNamespace

from app.models.comment import Comment
from app.models.comment_audio import CommentAudio
from app.models.comment_image import CommentImage
from app.models.discussion import TrackDiscussion, TrackDiscussionAudio, TrackDiscussionImage
from app.models.issue_audio import IssueAudio
from app.services import cleanup


def test_collect_track_files_includes_local_and_r2_assets(db_session, factory, upload_dir):
    producer = factory.user(role="producer")
    mastering = factory.user(username="mastering")
    submitter = factory.user(username="submitter")
    reviewer = factory.user(username="reviewer")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter, reviewer])

    current_audio = upload_dir / "current.wav"
    current_audio.write_bytes(b"current")
    master_audio = upload_dir / "master.mp3"
    master_audio.write_bytes(b"master")

    track = factory.track(
        album=album,
        submitter=submitter,
        peer_reviewer=reviewer,
        file_path=str(current_audio),
    )
    track.source_versions[0].file_path = "tracks/1/source/r2.wav"
    track.source_versions[0].storage_backend = "r2"
    delivery = factory.master_delivery(track=track, uploaded_by=mastering, file_path=str(master_audio))
    issue = factory.issue(track=track, author=reviewer, phase="peer", source_version_id=track.source_versions[-1].id)

    db_session.add(IssueAudio(
        issue_id=issue.id,
        file_path="issue_audios/issue.mp3",
        storage_backend="local",
        original_filename="issue.mp3",
        duration=1.0,
    ))
    comment = Comment(
        issue_id=issue.id,
        author_id=reviewer.id,
        content="Comment",
        visibility="public",
    )
    db_session.add(comment)
    db_session.flush()
    db_session.add(CommentImage(comment_id=comment.id, file_path="comment_images/comment.png"))
    db_session.add(CommentAudio(
        comment_id=comment.id,
        file_path="comment_audios/comment.mp3",
        storage_backend="r2",
        original_filename="comment.mp3",
        duration=2.0,
    ))
    discussion = TrackDiscussion(
        track_id=track.id,
        author_id=submitter.id,
        content="Discussion",
        phase="general",
        visibility="public",
    )
    db_session.add(discussion)
    db_session.flush()
    db_session.add(TrackDiscussionImage(discussion_id=discussion.id, file_path="discussion_images/discussion.png"))
    db_session.add(
        TrackDiscussionAudio(
            discussion_id=discussion.id,
            file_path="discussion_audios/local-discussion.mp3",
            storage_backend="local",
            original_filename="local-discussion.mp3",
            duration=1.5,
        )
    )
    db_session.add(
        TrackDiscussionAudio(
            discussion_id=discussion.id,
            file_path="discussions/1/r2-discussion.mp3",
            storage_backend="r2",
            original_filename="r2-discussion.mp3",
            duration=2.5,
        )
    )
    db_session.commit()
    db_session.refresh(track)
    db_session.refresh(delivery)

    local_paths, r2_keys = cleanup.collect_track_files(track)

    assert Path(str(current_audio)) in local_paths
    assert Path(str(master_audio)) in local_paths
    assert upload_dir / "issue_audios/issue.mp3" in local_paths
    assert upload_dir / "comment_images/comment.png" in local_paths
    assert upload_dir / "discussion_images/discussion.png" in local_paths
    assert upload_dir / "discussion_audios/local-discussion.mp3" in local_paths
    assert "tracks/1/source/r2.wav" in r2_keys
    assert "comment_audios/comment.mp3" in r2_keys
    assert "discussions/1/r2-discussion.mp3" in r2_keys


def test_cleanup_files_removes_local_files_and_r2_keys(tmp_path, monkeypatch):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    deleted_batches: list[list[str]] = []
    monkeypatch.setitem(
        sys.modules,
        "app.services.r2",
        SimpleNamespace(delete_objects=lambda keys: deleted_batches.append(keys)),
    )

    cleanup.cleanup_files([first, second], ["r2/audio-1", "r2/audio-2"])

    assert first.exists() is False
    assert second.exists() is False
    assert deleted_batches == [["r2/audio-1", "r2/audio-2"]]
