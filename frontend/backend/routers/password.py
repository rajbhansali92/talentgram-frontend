"""Password flows: change-password (authenticated) + forgot / reset-password.

Contract (locked by product owner 2026-04):
  - `POST /api/auth/change-password` — requires valid JWT; verifies the
    current password before accepting the new one; bumps `token_version`
    which kills every other existing session for that user.
  - `POST /api/public/forgot-password` — rate-limited; ALWAYS returns the
    same generic message so attackers can't enumerate valid emails. No
    reset link is generated here — administrators must issue reset links
    via /admin/users. Rate-limit: 5 requests per IP per 15 min.
  - `POST /api/public/reset-password/validate` + `POST /api/public/reset-password`
    consume the admin-generated reset token (stored as SHA-256 hash,
    1-hour TTL, single-use) and finalise the new password.

JWT invalidation: every new JWT carries a `tv` claim equal to the user's
stored `token_version`. The `current_user` dependency in core.py rejects
tokens whose `tv` is older than the stored value, so bumping the counter
instantly logs the user out everywhere.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request

from core import (
    ChangePasswordIn,
    ForgotPasswordIn,
    ResetPasswordCompleteIn,
    ResetTokenValidateIn,
    _now,
    current_user,
    db,
    enforce_password_policy,
    hash_password,
    hash_reset_token,
    verify_password,
)

router = APIRouter(prefix="/api", tags=["password"])
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# In-memory rate limiter (single-instance FastAPI — per pod).
# Simple sliding window — 5 hits / 15 min per bucket key.
# --------------------------------------------------------------------------
_RATE_WINDOW_SECONDS = 15 * 60
_RATE_MAX_HITS = 5
_rate_log: Dict[str, list] = {}


def _check_rate_limit(bucket: str) -> Tuple[bool, int]:
    now = time.time()
    cutoff = now - _RATE_WINDOW_SECONDS
    entries = [t for t in _rate_log.get(bucket, []) if t > cutoff]
    if len(entries) >= _RATE_MAX_HITS:
        _rate_log[bucket] = entries
        return False, int(entries[0] + _RATE_WINDOW_SECONDS - now)
    entries.append(now)
    _rate_log[bucket] = entries
    # Opportunistic cleanup to prevent unbounded growth.
    if len(_rate_log) > 10000:
        for key in list(_rate_log.keys()):
            if not [t for t in _rate_log[key] if t > cutoff]:
                _rate_log.pop(key, None)
    return True, 0


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------
# Change password (authenticated)
# --------------------------------------------------------------------------
@router.post("/auth/change-password")
async def change_password(
    payload: ChangePasswordIn,
    user: dict = Depends(current_user),
):
    """Authenticated: verify current password, set a new one, bump token_version.

    Any other JWT previously issued for this user becomes invalid on the next
    request. The token used for this call remains valid for the response itself
    (the caller will typically re-login immediately after).
    """
    stored = await db.users.find_one({"id": user["id"]}, {"password_hash": 1, "token_version": 1})
    if not stored or not stored.get("password_hash"):
        raise HTTPException(400, "Password cannot be changed for this account")
    if not verify_password(payload.current_password or "", stored["password_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    new_pw = (payload.new_password or "").strip()
    if new_pw == (payload.current_password or ""):
        raise HTTPException(400, "New password must differ from current password")
    enforce_password_policy(new_pw)

    new_tv = int(stored.get("token_version") or 0) + 1
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {
            "password_hash": hash_password(new_pw),
            "token_version": new_tv,
            "password_changed_at": _now(),
        }},
    )
    logger.info("Password changed for user=%s (token_version=%d)", user.get("email"), new_tv)
    return {"ok": True}


# --------------------------------------------------------------------------
# Forgot password (public) — generic response only.
# --------------------------------------------------------------------------
_GENERIC_FORGOT_MESSAGE = (
    "If that account exists, contact your administrator to reset your password."
)


@router.post("/public/forgot-password")
async def forgot_password(payload: ForgotPasswordIn, request: Request):
    """Public endpoint — ALWAYS returns the same generic message.

    Does NOT generate a reset token. Admin-triggered flow via
    `/api/users/{uid}/reset-password` is the only source of reset links.
    Rate-limited to 5/15min per IP to slow down enumeration attempts.
    """
    ip = _client_ip(request)
    bucket = f"forgot:{ip}"
    ok, retry_after = _check_rate_limit(bucket)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a few minutes and try again.",
            headers={"Retry-After": str(retry_after)},
        )
    # Deliberately no DB write, no leak, no reset token. Log for auditing.
    logger.info("FORGOT password request ip=%s email=%s", ip, payload.email.lower())
    return {"ok": True, "message": _GENERIC_FORGOT_MESSAGE}


# --------------------------------------------------------------------------
# Reset-password — consume admin-generated token.
# --------------------------------------------------------------------------
async def _lookup_reset_token(raw: str) -> dict:
    """Look up a reset token by its SHA-256 hash. Returns the doc or raises 400."""
    token_hash = hash_reset_token(raw or "")
    doc = await db.password_reset_tokens.find_one({"token_hash": token_hash}, {"_id": 0})
    if not doc:
        raise HTTPException(400, "Reset link is invalid or has already been used")
    if doc.get("used_at"):
        raise HTTPException(400, "Reset link has already been used")
    expires_at = doc.get("expires_at")
    # Mongo returns a naive datetime for documents stored with tz-aware datetimes.
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except Exception:
            expires_at = None
    if not expires_at:
        raise HTTPException(400, "Reset link is invalid")
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise HTTPException(400, "Reset link has expired — ask an admin for a new one")
    return doc


@router.post("/public/reset-password/validate")
async def validate_reset_token(payload: ResetTokenValidateIn):
    """Validate a reset token without consuming it — used by the reset page
    to show the associated email + enable the form."""
    doc = await _lookup_reset_token(payload.token)
    return {"ok": True, "email": doc.get("email")}


@router.post("/public/reset-password")
async def complete_reset_password(payload: ResetPasswordCompleteIn):
    doc = await _lookup_reset_token(payload.token)
    new_pw = (payload.new_password or "").strip()
    enforce_password_policy(new_pw)

    user = await db.users.find_one({"id": doc["user_id"]}, {"token_version": 1})
    if not user:
        raise HTTPException(400, "Reset link is invalid")
    new_tv = int(user.get("token_version") or 0) + 1

    await db.users.update_one(
        {"id": doc["user_id"]},
        {"$set": {
            "password_hash": hash_password(new_pw),
            "token_version": new_tv,
            "password_changed_at": _now(),
            # If the account was invited-but-never-activated, completing a
            # reset is effectively activation — push it to active.
            "status": "active",
        },
        "$unset": {"invite_token": "", "invite_expires_at": ""}},
    )
    # Single-use: mark the token consumed so it can never be replayed.
    await db.password_reset_tokens.update_one(
        {"token_hash": doc["token_hash"]},
        {"$set": {"used_at": _now()}},
    )
    logger.info("Password reset completed for email=%s (token_version=%d)", doc.get("email"), new_tv)
    return {"ok": True, "email": doc.get("email")}
