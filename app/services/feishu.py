"""Feishu (Lark) API helpers — resolve email/phone to open_id."""

import logging
import time

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://open.feishu.cn/open-apis"

# Simple in-memory cache for tenant_access_token: (app_id) -> (token, expires_at)
_token_cache: dict[str, tuple[str, float]] = {}


async def _get_tenant_token(app_id: str, app_secret: str) -> str:
    cached = _token_cache.get(app_id)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu token error: {data.get('msg', data)}")
    token = data["tenant_access_token"]
    _token_cache[app_id] = (token, time.time() + data.get("expire", 7200))
    return token


async def resolve_open_ids(
    app_id: str,
    app_secret: str,
    contacts: list[str],
) -> dict[str, str]:
    """Resolve a list of emails/phones to Feishu open_ids.

    Returns ``{contact_value: open_id}`` for every contact that was found.
    Contacts containing ``@`` are treated as emails, otherwise as mobiles.
    """
    if not contacts:
        return {}

    emails = [c for c in contacts if "@" in c]
    mobiles = [c for c in contacts if "@" not in c]

    try:
        token = await _get_tenant_token(app_id, app_secret)
    except Exception:
        logger.warning("Failed to obtain Feishu tenant token", exc_info=True)
        return {}

    body: dict = {}
    if emails:
        body["emails"] = emails
    if mobiles:
        body["mobiles"] = mobiles

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_BASE}/contact/v3/users/batch_get_id",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                params={"user_id_type": "open_id"},
            )
            data = resp.json()
    except Exception:
        logger.warning("Feishu batch_get_id request failed", exc_info=True)
        return {}

    if data.get("code") != 0:
        logger.warning("Feishu batch_get_id error: %s", data.get("msg", data))
        return {}

    result: dict[str, str] = {}
    for item in data.get("data", {}).get("user_list", []):
        uid = item.get("user_id")
        if not uid:
            continue
        if item.get("email"):
            result[item["email"]] = uid
        if item.get("mobile"):
            result[item["mobile"]] = uid
    return result
