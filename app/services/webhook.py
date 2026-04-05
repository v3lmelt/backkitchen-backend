import json
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)


async def post_webhook(url: str, payload: dict) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code >= 400:
                logger.warning("Webhook POST to %s returned %s", url, resp.status_code)
                return False
            return True
    except Exception as exc:
        logger.warning("Webhook POST to %s failed: %s", url, exc)
        return False


def build_webhook_payload(
    event: str,
    title: str,
    body: str,
    *,
    track_id: int | None = None,
    album_id: int | None = None,
    issue_id: int | None = None,
) -> dict:
    return {
        "event": event,
        "title": title,
        "body": body,
        "track_id": track_id,
        "album_id": album_id,
        "issue_id": issue_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
