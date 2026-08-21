"""Canonical anonymous-display token for user ids.

The anonymized peer-review UI shows ``#<token>`` instead of real display
names.  Historically the backend hashed the bare ``str(user_id)`` while the
frontend hashed ``'backkitchen-anon-v2:<id>'`` (see
``frontend/src/utils/hash.ts``), producing different anonymous names for the
same user.  The v2 namespace is now the canonical algorithm on both sides:
anonymous names are never persisted, and the v2-derived names are what users
already see in the frontend, so adopting v2 keeps displayed names stable.

This module is a leaf: it must not import other ``app`` modules so both the
schemas layer and the permission layer can use it without import cycles.
"""

ANON_ID_NAMESPACE = "backkitchen-anon-v2"

_FNV_OFFSET_BASIS = 2166136261
_FNV_PRIME = 16777619


def fnv1a_anon_token(user_id: int) -> str:
    """Return the 6-char uppercase hex anonymous token for a user id.

    FNV-1a 32-bit over ``"<namespace>:<user_id>"``, rendered as 8 hex digits
    and truncated to the first 6 — byte-for-byte identical to the frontend's
    ``hashId`` in ``src/utils/hash.ts``.
    """
    h = _FNV_OFFSET_BASIS
    for char in f"{ANON_ID_NAMESPACE}:{user_id}":
        h ^= ord(char)
        h = (h * _FNV_PRIME) & 0xFFFFFFFF
    return f"{h:08X}"[:6]
