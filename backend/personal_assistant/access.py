"""Personal Assistant — who may use it. Enforced on the BACKEND for every route; hiding the icon is only cosmetic.

Default: users with the `admin` role. Further users are enabled WITHOUT code or schema changes by listing their
e-mail (or user id) in the `ASSISTANT_ALLOWED_USERS` environment variable (comma separated) — each keeps a fully
private conversation and task list regardless. To make the list EXCLUSIVE (e.g. only the owner's account, admins
included), also set `ASSISTANT_RESTRICT_TO_ALLOWLIST=true`. `PERSONAL_ASSISTANT_DISABLED=true` switches the whole
feature off.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import Depends, HTTPException

from core import current_team_or_admin


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def disabled() -> bool:
    return _truthy(os.environ.get("PERSONAL_ASSISTANT_DISABLED", ""))


def allowed_users() -> set:
    raw = os.environ.get("ASSISTANT_ALLOWED_USERS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def is_allowed(user: Dict[str, Any]) -> bool:
    if disabled() or not user:
        return False
    listed = (str(user.get("email") or "").lower() in allowed_users()) or (str(user.get("id") or "").lower() in allowed_users())
    if _truthy(os.environ.get("ASSISTANT_RESTRICT_TO_ALLOWLIST", "")):
        return listed
    return user.get("role") == "admin" or listed


async def assistant_user(user: Dict[str, Any] = Depends(current_team_or_admin)) -> Dict[str, Any]:
    if not is_allowed(user):
        raise HTTPException(status_code=403, detail="The assistant isn't enabled for this account.")
    return user
