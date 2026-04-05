"""Streaming upload helpers that avoid loading entire files into memory."""

from pathlib import Path

from fastapi import HTTPException, UploadFile, status


async def stream_upload(file: UploadFile, dest: Path, max_size: int) -> None:
    """Stream an uploaded file to disk in 8 KB chunks, enforcing *max_size*.

    On size violation the partial file is cleaned up before raising HTTP 413.
    """
    total = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(8192):
            total += len(chunk)
            if total > max_size:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File too large. Maximum size is {max_size // (1024 * 1024)} MB.",
                )
            f.write(chunk)


def stream_upload_sync(file: UploadFile, dest: Path, max_size: int) -> None:
    """Synchronous variant of :func:`stream_upload` for non-async endpoints."""
    total = 0
    with open(dest, "wb") as f:
        while chunk := file.file.read(8192):
            total += len(chunk)
            if total > max_size:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File too large. Maximum size is {max_size // (1024 * 1024)} MB.",
                )
            f.write(chunk)
