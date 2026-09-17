"""Tamper-evident signing for the round-tripped conversation context.

Phase 2 keeps conversation state client-side (the opaque ``context`` blob
the ``/command`` response returns and the next request echoes back). Phase 3
executes real mutations off that blob, so it MUST be impossible for a
client to hand-craft or edit a plan and have the server act on it.

We do not add a persistence layer for this — instead every ``context`` the
server emits is HMAC-signed with a key derived from ``core.JWT_SECRET``
(the same server secret that already protects every session token). On
``/command/confirm`` the signature is recomputed over the exact
``pending`` payload and rejected on any mismatch. Even a valid signature
is not trusted on its own: the executor still re-validates entity IDs,
canonical values and live pipeline state against the database.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from core import JWT_SECRET

_KEY = hashlib.sha256(b"simple-assistant/context/v1\x00" + JWT_SECRET.encode()).digest()


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def sign(payload: Any) -> str:
    return hmac.new(_KEY, _canonical(payload), hashlib.sha256).hexdigest()


def verify(payload: Any, signature: str) -> bool:
    if not signature or not isinstance(signature, str):
        return False
    try:
        return hmac.compare_digest(sign(payload), signature)
    except Exception:
        return False
