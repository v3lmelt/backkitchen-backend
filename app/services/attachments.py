"""Shared helpers for issue/comment/discussion attachments.

Consolidates the image/audio upload constants and the copy-pasted
attachment save/verify logic previously duplicated across the
``issues``, ``discussions``, ``tracks``, ``albums``, ``auth`` and
``circles`` routers.
"""

import uuid
from pathlib import Path
from typing import Callable, Iterable

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import (
    AUDIO_EXT_MAP,
    MAX_AUDIO_UPLOAD_SIZE,
    settings,
)
from app.services.audio import extract_audio_metadata
from app.services.upload import stream_upload

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
IMAGE_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

AUDIO_MIME_MAP = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
}


def verify_r2_audio_keys(object_keys: list[str], *, expected_prefix: str | None = None) -> None:
    """Validate that R2 object keys exist (and match ``expected_prefix`` when given)."""
    from app.services.r2 import object_exists

    normalized_prefix = None
    if expected_prefix is not None:
        normalized_prefix = expected_prefix.strip("/") + "/"

    for key in object_keys:
        normalized_key = key.strip("/")
        if normalized_prefix and not normalized_key.startswith(normalized_prefix):
            raise HTTPException(status_code=400, detail=f"Upload key does not match the expected target: {key}")
        if not object_exists(normalized_key):
            raise HTTPException(status_code=400, detail=f"Upload not found in R2: {key}")


def parse_r2_audio_key_list(
    audio_object_keys: str | None,
    audio_original_filenames: str | None,
) -> tuple[list[str], list[str]]:
    """Split newline-separated R2 key/name form fields into parallel lists.

    Names are padded with the key basename when fewer names than keys
    were provided.
    """
    keys: list[str] = []
    names: list[str] = []
    if audio_object_keys:
        keys = [k.strip() for k in audio_object_keys.split("\n") if k.strip()]
        names = [n.strip() for n in (audio_original_filenames or "").split("\n")]
        while len(names) < len(keys):
            names.append(Path(keys[len(names)]).name)
    return keys, names


async def save_uploaded_audios(
    db: Session,
    audios: Iterable[UploadFile],
    *,
    subdir: str,
    row_factory: Callable[..., object],
) -> None:
    """Persist direct-upload audio files under ``subdir`` and add one ORM row each.

    ``row_factory`` is called with ``file_path``, ``original_filename`` and
    ``duration`` keyword arguments and must return the ORM instance to add
    (e.g. ``IssueAudio``/``CommentAudio``/``TrackDiscussionAudio`` with its
    parent FK already bound).
    """
    upload_dir = settings.get_upload_path() / subdir
    upload_dir.mkdir(parents=True, exist_ok=True)
    for audio_file in audios:
        ext = AUDIO_EXT_MAP.get(audio_file.content_type or "", ".mp3")
        filename = f"{uuid.uuid4().hex}{ext}"
        dest = upload_dir / filename
        await stream_upload(audio_file, dest, MAX_AUDIO_UPLOAD_SIZE)
        duration = extract_audio_metadata(dest).duration
        db.add(row_factory(
            file_path=f"{subdir}/{filename}",
            original_filename=audio_file.filename or filename,
            duration=duration,
        ))
