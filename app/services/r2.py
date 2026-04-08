"""Cloudflare R2 (S3-compatible) storage helpers."""

import logging
import tempfile
import time
import uuid
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_r2_client():
    """Return a cached boto3 S3 client configured for Cloudflare R2."""
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        config=BotoConfig(signature_version="s3v4"),
        region_name="auto",
    )


def generate_upload_url(object_key: str, content_type: str) -> str:
    """Generate a presigned PUT URL for direct client upload to R2."""
    client = get_r2_client()
    return client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.R2_BUCKET_NAME,
            "Key": object_key,
            "ContentType": content_type,
        },
        ExpiresIn=settings.R2_PRESIGNED_UPLOAD_EXPIRY,
    )


_download_url_cache: dict[str, tuple[str, float]] = {}
_DOWNLOAD_URL_CACHE_MARGIN = 300  # re-generate 5 min before expiry


def generate_download_url(object_key: str, expires_in: int | None = None) -> str:
    """Generate a presigned GET URL for downloading/streaming from R2.

    Presigned URLs are cached per object key so that the same URL is returned
    within the validity window.  This allows browser HTTP caching to work
    because the URL (and therefore the cache key) remains stable.
    """
    ttl = expires_in or settings.R2_PRESIGNED_DOWNLOAD_EXPIRY
    now = time.monotonic()

    cached = _download_url_cache.get(object_key)
    if cached is not None:
        url, deadline = cached
        if now < deadline:
            return url

    client = get_r2_client()
    url = client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.R2_BUCKET_NAME,
            "Key": object_key,
            "ResponseCacheControl": f"private, max-age={ttl}, immutable",
        },
        ExpiresIn=ttl,
    )
    _download_url_cache[object_key] = (url, now + ttl - _DOWNLOAD_URL_CACHE_MARGIN)
    return url


def object_exists(object_key: str) -> bool:
    """Check whether an object exists in R2 via HEAD request."""
    client = get_r2_client()
    try:
        client.head_object(Bucket=settings.R2_BUCKET_NAME, Key=object_key)
        return True
    except ClientError:
        return False


def get_object_size(object_key: str) -> int | None:
    """Return the size in bytes of an R2 object, or None if not found."""
    client = get_r2_client()
    try:
        resp = client.head_object(Bucket=settings.R2_BUCKET_NAME, Key=object_key)
        return resp["ContentLength"]
    except ClientError:
        return None


def download_to_temp(object_key: str) -> Path:
    """Download an R2 object to a temporary file and return its path.

    The caller is responsible for deleting the temp file when done.
    """
    client = get_r2_client()
    suffix = Path(object_key).suffix
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        client.download_fileobj(settings.R2_BUCKET_NAME, object_key, tmp)
        tmp.close()
        return Path(tmp.name)
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise


def delete_object(object_key: str) -> None:
    """Delete a single object from R2."""
    client = get_r2_client()
    try:
        client.delete_object(Bucket=settings.R2_BUCKET_NAME, Key=object_key)
    except ClientError:
        logger.warning("Failed to delete R2 object: %s", object_key, exc_info=True)


def delete_objects(keys: list[str]) -> None:
    """Delete multiple objects from R2 in a single batch request."""
    if not keys:
        return
    client = get_r2_client()
    # S3 DeleteObjects supports up to 1000 keys per request
    for i in range(0, len(keys), 1000):
        batch = keys[i : i + 1000]
        try:
            client.delete_objects(
                Bucket=settings.R2_BUCKET_NAME,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
        except ClientError:
            logger.warning("Failed to delete R2 objects batch: %s", batch, exc_info=True)


def make_object_key(prefix: str, record_id: int, filename: str) -> str:
    """Build an R2 object key like ``tracks/42/source/abc123.flac``."""
    ext = Path(filename).suffix.lower()
    return f"{prefix}/{record_id}/{uuid.uuid4().hex}{ext}"
