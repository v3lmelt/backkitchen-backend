import logging
from pathlib import Path

from app.config import settings
from app.models.album import Album
from app.models.track import Track

logger = logging.getLogger(__name__)


def collect_album_files(album: Album) -> tuple[list[Path], list[str]]:
    """Collect all file references for an album (cover + all tracks' files)."""
    upload_base = settings.get_upload_path()
    local_paths: list[Path] = []
    r2_keys: list[str] = []

    if album.cover_image:
        local_paths.append(upload_base / album.cover_image)

    for track in album.tracks:
        track_local, track_r2 = collect_track_files(track)
        local_paths.extend(track_local)
        r2_keys.extend(track_r2)

    return local_paths, r2_keys


def collect_track_files(track: Track) -> tuple[list[Path], list[str]]:
    """Collect all file references associated with a track and its children.

    Returns ``(local_paths, r2_keys)`` so callers can clean up both backends.
    """
    upload_base = settings.get_upload_path()
    local_paths: list[Path] = []
    r2_keys: list[str] = []

    def _add(file_path: str | None, backend: str) -> None:
        if not file_path:
            return
        if backend == "r2":
            r2_keys.append(file_path)
        else:
            local_paths.append(Path(file_path))

    # Current track audio
    _add(track.file_path, track.storage_backend)

    # All source version audio files
    for sv in track.source_versions:
        _add(sv.file_path, sv.storage_backend)

    # All master delivery audio files
    for md in track.master_deliveries:
        _add(md.file_path, md.storage_backend)

    # Issue comment images (always local) and audios (local or r2)
    for issue in track.issues:
        for comment in issue.comments:
            for img in comment.images:
                if img.file_path:
                    local_paths.append(upload_base / img.file_path)
            for audio in comment.audios:
                if not audio.file_path:
                    continue
                if audio.storage_backend == "r2":
                    r2_keys.append(audio.file_path)
                else:
                    local_paths.append(upload_base / audio.file_path)

    # Discussion images (always local)
    for disc in track.discussions:
        for img in disc.images:
            if img.file_path:
                local_paths.append(upload_base / img.file_path)

    return local_paths, r2_keys


def cleanup_files(local_paths: list[Path], r2_keys: list[str]) -> None:
    """Delete files from local disk and R2."""
    for p in local_paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    if r2_keys:
        try:
            from app.services.r2 import delete_objects
            delete_objects(r2_keys)
        except Exception:
            logger.warning("Failed to delete R2 objects", exc_info=True)
