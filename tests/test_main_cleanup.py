import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app import main as main_module
from app.models.album import Album
from app.models.track import RejectionMode, Track, TrackStatus
from app.models.track_source_version import TrackSourceVersion
from app.services import cleanup as cleanup_service


def test_delete_file_handles_local_relative_and_r2(upload_dir, monkeypatch):
    relative = upload_dir / "relative.wav"
    relative.write_bytes(b"audio")

    deleted_keys: list[str] = []
    monkeypatch.setattr(main_module.settings, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setitem(sys.modules, "app.services.r2", SimpleNamespace(delete_object=lambda key: deleted_keys.append(key)))

    main_module._delete_file("relative.wav", "local")
    main_module._delete_file("r2/key.wav", "r2")

    assert relative.exists() is False
    assert deleted_keys == ["r2/key.wav"]


def test_run_expired_source_cleanup_clears_expired_versions_and_rejected_track_file(
    db_session,
    session_factory,
    factory,
    upload_dir,
    monkeypatch,
):
    producer = factory.user(role="producer")
    mastering = factory.user(username="mastering")
    submitter = factory.user(username="submitter")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])

    track_file = upload_dir / "track-current.wav"
    version_file = upload_dir / "track-version.wav"
    track_file.write_bytes(b"track")
    version_file.write_bytes(b"version")

    track = factory.track(
        album=album,
        submitter=submitter,
        status=TrackStatus.REJECTED.value,
        rejection_mode=RejectionMode.FINAL,
        file_path=str(track_file),
    )
    source_version = track.source_versions[-1]
    source_version.file_path = str(version_file)
    source_version.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.commit()

    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(main_module, "SessionLocal", session_factory)
    monkeypatch.setattr(main_module, "_delete_file", lambda path, backend: deleted.append((path, backend)))

    cleaned = main_module._run_expired_source_cleanup()

    db_session.expire_all()
    refreshed_track = db_session.get(Track, track.id)
    refreshed_version = db_session.get(TrackSourceVersion, source_version.id)

    assert cleaned == 2
    assert refreshed_track is not None
    assert refreshed_track.file_path is None
    assert refreshed_version is not None
    assert refreshed_version.file_path is None
    assert deleted == [
        (str(version_file), "local"),
        (str(track_file), "local"),
    ]


def test_run_archived_track_cleanup_deletes_expired_tracks_and_cleans_files(
    db_session,
    session_factory,
    factory,
    monkeypatch,
):
    producer = factory.user(role="producer")
    mastering = factory.user(username="mastering")
    submitter = factory.user(username="submitter")
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])

    expired_track = factory.track(album=album, submitter=submitter)
    expired_track.archived_at = datetime.now(timezone.utc) - timedelta(days=30)
    expired_track_id = expired_track.id
    recent_track = factory.track(album=album, submitter=submitter)
    recent_track.archived_at = datetime.now(timezone.utc) - timedelta(days=1)
    recent_track_id = recent_track.id
    db_session.commit()

    cleanup_calls: list[tuple[list[Path], list[str]]] = []
    monkeypatch.setattr(main_module, "SessionLocal", session_factory)
    monkeypatch.setattr(cleanup_service, "collect_track_files", lambda track: ([Path(f"track-{track.id}.wav")], [f"r2/{track.id}"]))
    monkeypatch.setattr(cleanup_service, "cleanup_files", lambda local_paths, r2_keys: cleanup_calls.append((local_paths, r2_keys)))

    deleted = main_module._run_archived_track_cleanup()

    db_session.expire_all()

    assert deleted == 1
    assert db_session.get(Track, expired_track_id) is None
    assert db_session.get(Track, recent_track_id) is not None
    assert cleanup_calls == [([Path(f"track-{expired_track_id}.wav")], [f"r2/{expired_track_id}"])]


def test_run_archived_album_cleanup_deletes_expired_albums_and_cleans_files(
    db_session,
    session_factory,
    factory,
    monkeypatch,
):
    producer = factory.user(role="producer")
    mastering = factory.user(username="mastering")
    expired_album = factory.album(producer=producer, mastering_engineer=mastering, members=[producer], title="Expired")
    expired_album.archived_at = datetime.now(timezone.utc) - timedelta(days=30)
    expired_album_id = expired_album.id
    recent_album = factory.album(producer=producer, mastering_engineer=mastering, members=[producer], title="Recent")
    recent_album.archived_at = datetime.now(timezone.utc) - timedelta(days=1)
    recent_album_id = recent_album.id
    db_session.commit()

    cleanup_calls: list[tuple[list[Path], list[str]]] = []
    monkeypatch.setattr(main_module, "SessionLocal", session_factory)
    monkeypatch.setattr(cleanup_service, "collect_album_files", lambda album: ([Path(f"album-{album.id}.zip")], [f"r2/album-{album.id}"]))
    monkeypatch.setattr(cleanup_service, "cleanup_files", lambda local_paths, r2_keys: cleanup_calls.append((local_paths, r2_keys)))

    deleted = main_module._run_archived_album_cleanup()

    db_session.expire_all()

    assert deleted == 1
    assert db_session.get(Album, expired_album_id) is None
    assert db_session.get(Album, recent_album_id) is not None
    assert cleanup_calls == [([Path(f"album-{expired_album_id}.zip")], [f"r2/album-{expired_album_id}"])]
