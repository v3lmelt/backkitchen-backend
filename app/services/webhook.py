import ipaddress
import logging
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.services.webhook_adapters import adapt_payload

logger = logging.getLogger(__name__)

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

# Maximum number of delivery records to keep per album
MAX_DELIVERY_RECORDS = 50


def _validate_webhook_url(url: str) -> None:
    """Reject webhook URLs targeting private/internal networks (SSRF prevention)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported webhook URL scheme: {parsed.scheme}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Webhook URL has no hostname.")
    try:
        resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve webhook hostname: {hostname}") from exc
    for _family, _type, _proto, _canonname, sockaddr in resolved:
        addr = ipaddress.ip_address(sockaddr[0])
        for net in _BLOCKED_NETWORKS:
            if addr in net:
                raise ValueError(f"Webhook URL resolves to a blocked address: {addr}")


async def post_webhook(
    url: str,
    payload: dict,
    *,
    db: Session | None = None,
    album_id: int | None = None,
    event_type: str = "unknown",
    webhook_type: str = "generic",
    webhook_secret: str = "",
    feishu_app_id: str = "",
    feishu_app_secret: str = "",
    mention_users: list[dict] | None = None,
) -> bool:
    """Send webhook and optionally persist a delivery record.

    Pass ``db`` and ``album_id`` to record the delivery. Omitting them keeps
    the old behaviour (no persistence).

    ``webhook_type`` selects the payload adapter (e.g. "generic", "feishu").
    ``webhook_secret`` is forwarded to the adapter for signing if needed.
    ``mention_users`` is a list of ``{"name": ..., "feishu_contact": ...}``
    dicts; contacts are resolved to open_ids for the Feishu adapter.
    """
    status_code: int | None = None
    error_detail: str | None = None
    success = False

    try:
        _validate_webhook_url(url)
    except ValueError as exc:
        logger.warning("Webhook URL rejected: %s — %s", url, exc)
        error_detail = str(exc)
        _persist_delivery(db, album_id, event_type, url, success, status_code, error_detail)
        return False

    # Resolve Feishu @mentions
    resolved_mentions: list[dict] = []
    if webhook_type == "feishu" and mention_users and feishu_app_id and feishu_app_secret:
        from app.services.feishu import resolve_open_ids

        contacts = [u["feishu_contact"] for u in mention_users if u.get("feishu_contact")]
        if contacts:
            mapping = await resolve_open_ids(feishu_app_id, feishu_app_secret, contacts)
            resolved_mentions = [
                {"name": u["name"], "open_id": mapping[u["feishu_contact"]]}
                for u in mention_users
                if u.get("feishu_contact") and u["feishu_contact"] in mapping
            ]

    adapted = adapt_payload(
        payload, webhook_type=webhook_type, secret=webhook_secret,
        resolved_mentions=resolved_mentions,
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json=adapted,
                headers={"Content-Type": "application/json"},
            )
            status_code = resp.status_code
            if resp.status_code >= 400:
                logger.warning("Webhook POST to %s returned %s", url, resp.status_code)
                error_detail = f"HTTP {resp.status_code}"
            else:
                success = True
    except Exception as exc:
        logger.warning("Webhook POST to %s failed: %s", url, exc)
        error_detail = str(exc)[:500]

    _persist_delivery(db, album_id, event_type, url, success, status_code, error_detail)
    return success


def _persist_delivery(
    db: Session | None,
    album_id: int | None,
    event_type: str,
    target_url: str,
    success: bool,
    status_code: int | None,
    error_detail: str | None,
) -> None:
    if db is None or album_id is None:
        return
    try:
        from app.models.webhook_delivery import WebhookDelivery  # avoid circular import

        record = WebhookDelivery(
            album_id=album_id,
            event_type=event_type,
            success=success,
            status_code=status_code,
            target_url=target_url,
            error_detail=error_detail,
        )
        db.add(record)
        db.flush()

        # Prune oldest records beyond the cap
        old_ids = (
            db.query(WebhookDelivery.id)
            .filter(WebhookDelivery.album_id == album_id)
            .order_by(WebhookDelivery.id.desc())
            .offset(MAX_DELIVERY_RECORDS)
            .all()
        )
        if old_ids:
            ids = [r[0] for r in old_ids]
            db.query(WebhookDelivery).filter(WebhookDelivery.id.in_(ids)).delete(
                synchronize_session=False
            )
        db.commit()
    except Exception as exc:
        logger.warning("Failed to persist webhook delivery record: %s", exc)
        db.rollback()


def build_webhook_payload(
    event: str,
    title: str,
    body: str,
    *,
    track_id: int | None = None,
    album_id: int | None = None,
    issue_id: int | None = None,
    context: dict | None = None,
) -> dict:
    payload: dict = {
        "event": event,
        "title": title,
        "body": body,
        "track_id": track_id,
        "album_id": album_id,
        "issue_id": issue_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if context:
        payload["context"] = context
    return payload
