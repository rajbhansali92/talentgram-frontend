"""User management (admin-only) + public signup via invite token."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from core import (
    USER_ROLES,
    SignupCompleteIn,
    SignupValidateIn,
    UserInviteIn,
    UserRolePatchIn,
    _now,
    _public_user,
    current_user,
    db,
    enforce_password_policy,
    generate_invite_token,
    generate_reset_token,
    hash_password,
    hash_reset_token,
    require_role,
)

router = APIRouter(prefix="/api", tags=["users"])

# Invites expire after 7 days. Tokens are single-use (consumed on signup).
INVITE_TTL_DAYS = 7
# Admin-generated password reset links expire after 1 hour. Single-use.
RESET_TOKEN_TTL_SECONDS = 3600


def _invite_expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS)).isoformat()


def _invite_is_expired(expires_at: str) -> bool:
    try:
        return datetime.fromisoformat(expires_at) < datetime.now(timezone.utc)
    except Exception:
        return True


# --------------------------------------------------------------------------
# Admin-only user CRUD
# --------------------------------------------------------------------------
@router.get("/users")
async def list_users(_: dict = Depends(require_role("admin"))):
    items = await db.users.find(
        {}, {"_id": 0, "password_hash": 0, "invite_token": 0}
    ).sort("created_at", -1).to_list(5000)
    # Stats summary for header cards
    total = len(items)
    admin_count = sum(1 for u in items if u.get("role") == "admin")
    team_count = sum(1 for u in items if u.get("role") == "team")
    return {
        "items": items,
        "stats": {
            "total": total,
            "admin": admin_count,
            "team": team_count,
            "disabled": sum(1 for u in items if u.get("status") == "disabled"),
            "invited": sum(1 for u in items if u.get("status") == "invited"),
        },
    }


@router.post("/users/invite")
async def invite_user(
    payload: UserInviteIn,
    admin: dict = Depends(require_role("admin")),
):
    if payload.role not in USER_ROLES:
        raise HTTPException(400, f"Invalid role (must be one of {USER_ROLES})")
    email = payload.email.lower().strip()
    existing = await db.users.find_one({"email": email}, {"_id": 0})
    if existing and existing.get("status") != "invited":
        raise HTTPException(409, "User already exists")

    token = generate_invite_token()
    now = _now()
    doc: Dict[str, Any] = {
        "id": existing.get("id") if existing else str(uuid.uuid4()),
        "name": payload.name.strip(),
        "email": email,
        "role": payload.role,
        "status": "invited",
        "password_hash": None,
        "invite_token": token,
        "invite_expires_at": _invite_expires_at(),
        "invited_by": admin.get("id"),
        "created_at": existing.get("created_at") if existing else now,
        "last_login": None,
    }
    await db.users.update_one({"email": email}, {"$set": doc}, upsert=True)
    return {
        "user": _public_user(doc),
        "invite_token": token,
        "invite_path": f"/signup?token={token}",
        "expires_at": doc["invite_expires_at"],
    }


@router.post("/users/{uid}/role")
async def update_role(
    uid: str,
    payload: UserRolePatchIn,
    admin: dict = Depends(require_role("admin")),
):
    if payload.role not in USER_ROLES:
        raise HTTPException(400, f"Invalid role (must be one of {USER_ROLES})")
    target = await db.users.find_one({"id": uid}, {"_id": 0})
    if not target:
        raise HTTPException(404, "User not found")
    # Guard: prevent the last admin from demoting themselves into team.
    if target.get("role") == "admin" and payload.role != "admin":
        admin_count = await db.users.count_documents({"role": "admin", "status": "active"})
        if admin_count <= 1:
            raise HTTPException(400, "Cannot demote the last active admin")
    await db.users.update_one({"id": uid}, {"$set": {"role": payload.role}})
    updated = await db.users.find_one({"id": uid}, {"_id": 0, "password_hash": 0, "invite_token": 0})
    return _public_user(updated)


@router.post("/users/{uid}/disable")
async def disable_user(uid: str, admin: dict = Depends(require_role("admin"))):
    target = await db.users.find_one({"id": uid}, {"_id": 0})
    if not target:
        raise HTTPException(404, "User not found")
    if target.get("id") == admin.get("id"):
        raise HTTPException(400, "Cannot disable your own account")
    if target.get("role") == "admin" and target.get("status") == "active":
        admin_count = await db.users.count_documents({"role": "admin", "status": "active"})
        if admin_count <= 1:
            raise HTTPException(400, "Cannot disable the last active admin")
    await db.users.update_one({"id": uid}, {"$set": {"status": "disabled"}})
    updated = await db.users.find_one({"id": uid}, {"_id": 0, "password_hash": 0, "invite_token": 0})
    return _public_user(updated)


@router.post("/users/{uid}/enable")
async def enable_user(uid: str, _: dict = Depends(require_role("admin"))):
    target = await db.users.find_one({"id": uid}, {"_id": 0})
    if not target:
        raise HTTPException(404, "User not found")
    if not target.get("password_hash"):
        raise HTTPException(400, "User has not completed signup — reissue an invite instead")
    await db.users.update_one({"id": uid}, {"$set": {"status": "active"}})
    updated = await db.users.find_one({"id": uid}, {"_id": 0, "password_hash": 0, "invite_token": 0})
    return _public_user(updated)


@router.delete("/users/{uid}")
async def delete_user(uid: str, admin: dict = Depends(require_role("admin"))):
    target = await db.users.find_one({"id": uid}, {"_id": 0})
    if not target:
        raise HTTPException(404, "User not found")
    if target.get("id") == admin.get("id"):
        raise HTTPException(400, "Cannot delete your own account")
    if target.get("role") == "admin" and target.get("status") == "active":
        admin_count = await db.users.count_documents({"role": "admin", "status": "active"})
        if admin_count <= 1:
            raise HTTPException(400, "Cannot delete the last active admin")
    await db.users.delete_one({"id": uid})
    return {"ok": True}


@router.post("/users/{uid}/reset-password")
async def admin_generate_reset_link(
    uid: str, admin: dict = Depends(require_role("admin"))
):
    """Admin-only: generate a single-use password reset link for a user.

    The raw token is returned to the admin ONCE (shown in a modal) and
    never persisted — only its SHA-256 hash is stored. Old JWTs remain
    valid until the reset is completed; completion bumps `token_version`
    which kills every existing session for that user.
    """
    target = await db.users.find_one({"id": uid}, {"_id": 0})
    if not target:
        raise HTTPException(404, "User not found")
    raw = generate_reset_token()
    token_hash = hash_reset_token(raw)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=RESET_TOKEN_TTL_SECONDS)

    # Invalidate any prior unused tokens for this user — only one active at a time.
    await db.password_reset_tokens.delete_many({"user_id": uid, "used_at": None})
    await db.password_reset_tokens.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": uid,
        "email": target["email"],
        "token_hash": token_hash,
        "expires_at": expires_at,
        "created_at": _now(),
        "created_by": admin["id"],
        "used_at": None,
    })
    return {
        "reset_token": raw,
        "reset_path": f"/reset-password?token={raw}",
        "expires_at": expires_at.isoformat(),
        "email": target["email"],
    }


# --------------------------------------------------------------------------
# Public signup (invite-token based)
# --------------------------------------------------------------------------
@router.post("/public/signup/validate")
async def signup_validate(payload: SignupValidateIn):
    user = await db.users.find_one({"invite_token": payload.token}, {"_id": 0})
    if not user or user.get("status") != "invited":
        raise HTTPException(404, "Invite not found or already used")
    exp = user.get("invite_expires_at")
    if not exp or _invite_is_expired(exp):
        raise HTTPException(410, "Invite expired — ask an admin to resend")
    return {
        "email": user["email"],
        "name": user.get("name"),
        "role": user.get("role"),
    }


@router.post("/public/signup/complete")
async def signup_complete(payload: SignupCompleteIn):
    pwd = (payload.password or "").strip()
    enforce_password_policy(pwd)

    user = await db.users.find_one({"invite_token": payload.token}, {"_id": 0})
    if not user or user.get("status") != "invited":
        raise HTTPException(404, "Invite not found or already used")
    exp = user.get("invite_expires_at")
    if not exp or _invite_is_expired(exp):
        raise HTTPException(410, "Invite expired — ask an admin to resend")

    await db.users.update_one(
        {"id": user["id"]},
        {
            "$set": {
                "password_hash": hash_password(pwd),
                "status": "active",
                "token_version": int(user.get("token_version") or 0) + 1,
            },
            "$unset": {"invite_token": "", "invite_expires_at": ""},
        },
    )
    return {"ok": True, "email": user["email"]}
