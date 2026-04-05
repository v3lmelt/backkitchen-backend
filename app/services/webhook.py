import ipaddress
import logging
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

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


async def post_webhook(url: str, payload: dict) -> bool:
    try:
        _validate_webhook_url(url)
    except ValueError as exc:
        logger.warning("Webhook URL rejected: %s — %s", url, exc)
        return False
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
