"""Webhook payload adapters for different target platforms.

Each adapter converts the generic internal payload into the format
expected by the target service.
"""

import base64
import hashlib
import hmac
import time

# Event type → Feishu card header color
_FEISHU_COLOR_MAP: dict[str, str] = {
    "track_submitted": "green",
    "track_status_changed": "orange",
    "new_issue": "red",
    "issue_status_changed": "yellow",
    "new_comment": "blue",
    "new_discussion": "blue",
    "reviewer_assigned": "turquoise",
    "reviewer_reassigned": "turquoise",
    "track_archived": "grey",
    "track_restored": "green",
    "track_reopened": "violet",
    "reopen_request": "violet",
    "reopen_approved": "green",
    "reopen_rejected": "red",
    "test": "orange",
}


def adapt_payload(
    payload: dict,
    *,
    webhook_type: str = "generic",
    secret: str = "",
    resolved_mentions: list[dict] | None = None,
) -> dict:
    """Return a platform-specific payload dict ready to POST as JSON."""
    adapter = _ADAPTERS.get(webhook_type, _generic)
    return adapter(payload, secret=secret, resolved_mentions=resolved_mentions or [])


# -- adapters ----------------------------------------------------------------

def _generic(payload: dict, **_kwargs: object) -> dict:
    return payload


def _feishu(
    payload: dict,
    *,
    secret: str = "",
    resolved_mentions: list[dict] | None = None,
    **_kwargs: object,
) -> dict:
    title = payload.get("title", "")
    body = payload.get("body", "")
    event = payload.get("event", "")
    ctx = payload.get("context") or {}

    color = _FEISHU_COLOR_MAP.get(event, "orange")

    # -- Build card elements --
    elements: list[dict] = []

    # Row 1: album + track (short fields side by side)
    row1_fields = []
    if ctx.get("album_title"):
        row1_fields.append(_md_field(f"**专辑**\n{ctx['album_title']}", short=True))
    if ctx.get("track_title"):
        row1_fields.append(_md_field(f"**曲目**\n{ctx['track_title']}", short=True))
    if row1_fields:
        elements.append({"tag": "column_set", "flex_mode": "bisect", "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [row1_fields[0]["text"]]}
            if len(row1_fields) > 0 else None,
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [row1_fields[1]["text"]]}
            if len(row1_fields) > 1 else None,
        ]})
        # Fallback to simpler div if column_set acts up
        elements[-1] = {"tag": "div", "fields": row1_fields}

    # Row 2: actor + action_required_by
    row2_fields = []
    if ctx.get("actor_name"):
        row2_fields.append(_md_field(f"**操作人**\n{ctx['actor_name']}", short=True))
    if ctx.get("action_required_by"):
        row2_fields.append(_md_field(f"**待办人**\n{ctx['action_required_by']}", short=True))
    if row2_fields:
        elements.append({"tag": "div", "fields": row2_fields})

    # Row 3: step transition
    if ctx.get("from_step") and ctx.get("to_step"):
        elements.append({"tag": "div", "text": {
            "tag": "lark_md",
            "content": f"**变更**\n{ctx['from_step']} → {ctx['to_step']}",
        }})

    # Divider
    if elements:
        elements.append({"tag": "hr"})

    # Body text
    if body:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": body}})

    # @mentions
    if resolved_mentions:
        names = " ".join(f"<at id={m['open_id']}></at>" for m in resolved_mentions if m.get("open_id"))
        if names:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": names}})

    # Action button
    if ctx.get("track_url"):
        elements.append({"tag": "action", "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看详情"},
            "type": "primary",
            "url": ctx["track_url"],
        }]})

    result: dict = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": elements,
        },
    }

    if secret:
        timestamp = str(int(time.time()))
        result["timestamp"] = timestamp
        result["sign"] = _feishu_sign(timestamp, secret)

    return result


def _md_field(content: str, *, short: bool = False) -> dict:
    return {
        "is_short": short,
        "text": {"tag": "lark_md", "content": content},
    }


def _feishu_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hm = hmac.new(
        string_to_sign.encode("utf-8"),
        b"",
        digestmod=hashlib.sha256,
    )
    return base64.b64encode(hm.digest()).decode("utf-8")


# -- registry ----------------------------------------------------------------

_ADAPTERS: dict[str, object] = {
    "generic": _generic,
    "feishu": _feishu,
}

SUPPORTED_TYPES = list(_ADAPTERS.keys())
