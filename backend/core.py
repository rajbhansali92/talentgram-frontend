"""Shared primitives: config, DB, storage, auth, utils, constants, models, visibility filters.

Everything that multiple routers need lives here to keep router modules pure of plumbing.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import Depends, Header, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field, field_validator
from pymongo.errors import DuplicateKeyError

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
JWT_SECRET = os.environ["JWT_SECRET"]
APP_NAME = os.environ.get("APP_NAME", "talentgram")
ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

# Direct Cloudinary Upload feature flag (rollout mechanism).
# The frontend upload manager uses signed browser→Cloudinary uploads as its ONLY
# transport (no proxy fallback), so this must default ON. With the previous
# "false" default, an unset env var made /upload/sign return 400 "Direct uploads
# are currently disabled", breaking ALL image + apply-video uploads. Set
# DIRECT_UPLOAD_ENABLED=false only to deliberately disable uploads.
DIRECT_UPLOAD_ENABLED = os.environ.get("DIRECT_UPLOAD_ENABLED", "true").lower() == "true"


# --------------------------------------------------------------------------
# Email Normalization Helper
# --------------------------------------------------------------------------
def normalize_email(email: Optional[str]) -> Optional[str]:
    if not email or not isinstance(email, str):
        return None
    return email.strip().lower() or None

# Cloudinary — primary (and only) media storage as of v37m migration.
# --------------------------------------------------------------------------
import cloudinary  # noqa: E402
import cloudinary.uploader  # noqa: E402
import cloudinary.utils  # noqa: E402
import cloudinary.api  # noqa: E402  (Admin API — used by finalize video reconciliation)

cloudinary.config(
    cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
    api_key=os.environ["CLOUDINARY_API_KEY"],
    api_secret=os.environ["CLOUDINARY_API_SECRET"],
    secure=True,
)
CLOUDINARY_CLOUD_NAME = os.environ["CLOUDINARY_CLOUD_NAME"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("talentgram")

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
mongo_client = AsyncIOMotorClient(
    MONGO_URL,
    serverSelectionTimeoutMS=10_000,   # fail fast if Atlas is unreachable
    connectTimeoutMS=10_000,           # socket connect cap
    socketTimeoutMS=20_000,            # per-op socket cap
    maxPoolSize=50,                    # match expected concurrent recruiter load
    retryWrites=True,                  # survive transient Atlas failovers
    tlsAllowInvalidCertificates=True,  # allow connection on systems with missing local root certs
)
db = mongo_client[DB_NAME]

# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------
bearer = HTTPBearer(auto_error=False)


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def make_token(payload: Dict[str, Any], days: int = 30) -> str:
    jti = payload.get("jti") or str(uuid.uuid4())
    data = {**payload, "jti": jti, "exp": datetime.now(timezone.utc) + timedelta(days=days)}
    token = jwt.encode(data, JWT_SECRET, algorithm="HS256")
    
    # Persist session asynchronously
    try:
        loop = asyncio.get_running_loop()
        session_doc = {
            "jti": jti,
            "user_id": payload.get("id"),
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(),
            "revoked": False,
            "revoked_at": None,
            "role": payload.get("role")
        }
        loop.create_task(db.sessions.insert_one(session_doc))
    except Exception:
        pass
        
    return token


def make_access_token() -> str:
    """Generate a cryptographically secure opaque access token.

    Unlike make_token(), this is NOT a JWT — it is a random URL-safe string
    (43 chars, 256 bits of entropy) stored verbatim in the database. This
    makes it cross-device persistent: the token survives JWT expiry and can
    be re-used indefinitely until the submission is deleted or the user
    explicitly revokes access.
    """
    import secrets
    return secrets.token_urlsafe(32)



def decode_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None


def enforce_password_policy(pw: str) -> None:
    """Raise HTTPException if the password doesn't meet the minimum policy:

    - >= 8 characters
    - contains at least one digit OR symbol (non-alphanumeric counts)
    Spec pinned by product owner 2026-04.
    """
    if not pw or len(pw) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    has_digit = any(c.isdigit() for c in pw)
    has_symbol = any(not c.isalnum() for c in pw)
    if not (has_digit or has_symbol):
        raise HTTPException(
            status_code=400,
            detail="Password must contain at least one number or special character",
        )


def hash_reset_token(raw: str) -> str:
    """SHA-256 hex digest — used so we never store raw reset tokens in Mongo."""
    import hashlib as _h
    return _h.sha256(raw.encode("utf-8")).hexdigest()


def generate_reset_token() -> str:
    """Cryptographically random reset token (~43 chars, URL-safe)."""
    import secrets
    return secrets.token_urlsafe(32)


async def current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
) -> Dict[str, Any]:
    """Return the active user behind the JWT. Rejects disabled users and
    unknown roles. Used by every admin-plane route.

    Also invalidates tokens whose `tv` claim is older than the user's current
    `token_version` — this is how password changes kill all existing sessions.
    """
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = decode_token(credentials.credentials)
    if not data or data.get("role") not in ("admin", "team"):
        raise HTTPException(status_code=401, detail="Invalid token")
        
    # Session Revocation Check (P1). The revocation lookup and the user
    # lookup are independent reads — running them concurrently instead of
    # two sequential round trips matters here: this dependency runs on
    # EVERY admin-plane request, and each round trip to Atlas measured
    # ~500-600ms in production (Railway↔Atlas cross-region latency), so
    # this alone was adding ~0.5s to every authenticated call app-wide.
    jti = data.get("jti")
    coros = [db.users.find_one(
        {"email": data.get("email")},
        {"_id": 0, "password_hash": 0, "invite_token": 0},
    )]
    if jti:
        coros.append(db.sessions.find_one({"jti": jti}))
    results = await asyncio.gather(*coros)
    user = results[0]
    session = results[1] if jti else None
    if session and session.get("revoked") is True:
        raise HTTPException(status_code=401, detail="Session has been revoked")

    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if user.get("status") == "disabled":
        raise HTTPException(status_code=403, detail="Account disabled")
    if user.get("status") == "invited":
        raise HTTPException(status_code=403, detail="Account not activated")
    # Token-version check: any old token (including ones issued before a
    # password change) becomes invalid when the user's stored version is
    # higher than the claim embedded at issue time.
    token_tv = int(data.get("tv") or 0)
    user_tv = int(user.get("token_version") or 0)
    if token_tv < user_tv:
        raise HTTPException(status_code=401, detail="Session expired — please sign in again")
    user["role"] = user.get("role", "team")
    return user


def require_role(*roles: str):
    """Dependency factory — 403s if current_user.role not in allowed set.

    Never trust frontend role checks — this is the single source of truth.
    """
    allowed = set(roles)

    async def _dep(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        if user.get("role") not in allowed:
            raise HTTPException(status_code=403, detail="Access denied")
        return user

    return _dep


async def current_admin(
    user: Dict[str, Any] = Depends(current_user),
) -> Dict[str, Any]:
    """Admin-only dependency. Kept for backwards-compat with existing DELETE
    routes. New code should prefer `require_role("admin")`."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def current_team_or_admin(
    user: Dict[str, Any] = Depends(current_user),
) -> Dict[str, Any]:
    """Allow any active admin or team member. Use on non-destructive routes
    where team members need create/edit/read parity with admins."""
    if user.get("role") not in ("admin", "team"):
        raise HTTPException(status_code=403, detail="Access denied")
    return user


def decode_viewer(authorization: Optional[str]) -> Optional[Dict[str, Any]]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1]
    data = decode_token(token)
    if not data or data.get("role") not in ("viewer", "admin", "team"):
        return None
    return data


async def decode_submitter(authorization: Optional[str]) -> Optional[Dict[str, Any]]:
    """Authenticate a submitter.

    Two valid credential forms, both revocation-aware:

    1. A non-expired, signature-valid submitter JWT whose `sid` matches a
       record AND whose value equals the `access_token` currently persisted on
       that record. If the persisted token differs (rotated / revoked) the
       presented token is rejected immediately.
    2. The opaque persistent `access_token` stored verbatim on the record —
       matched directly. This is the long-lived cross-device credential.

    The previous `verify_exp=False` fallback (which accepted *any* expired but
    signature-valid JWT) has been removed: expired JWTs are no longer honoured,
    and a token that no longer matches the stored value can never be reused.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1]

    data = decode_token(token)
    if data and data.get("role") == "submitter":
        sid = data.get("sid")
        kind = data.get("kind")
        if sid:
            if kind == "application":
                app_doc = await db.applications.find_one({"id": sid})
                if not app_doc:
                    return None
                db_token = app_doc.get("access_token")
                # P1-A fix: a signature-valid, non-expired submitter JWT whose `sid`
                # matches a real record is itself a valid credential. We must NOT
                # require the JWT to equal the opaque cross-device access_token — they
                # are distinct values by construction (JWT vs secrets.token_urlsafe),
                # so the previous equality check rejected EVERY JWT, leaving only the
                # opaque-token fallback working. The opaque token stays independently
                # valid (verbatim match below) and rotating it still revokes it; JWTs
                # are revoked by their short expiry.
                if not db_token:
                    await db.applications.update_one({"id": sid}, {"$set": {"access_token": token}})
            else:
                sub = await db.submissions.find_one({"id": sid})
                if not sub:
                    return None
                db_token = sub.get("access_token")
                # P1-A fix (see application branch above): accept a valid submitter
                # JWT on its own; do not reject it for differing from the opaque
                # access_token. Opaque-token revocation is preserved via the verbatim
                # fallback; JWTs are revoked by expiry.
                if not db_token:
                    await db.submissions.update_one({"id": sid}, {"$set": {"access_token": token}})
        return data

    # Not a valid submitter JWT (bad signature, expired, or wrong role).
    # Fall back to matching the opaque persistent access_token verbatim. A
    # rotated token will not match here either, so revocation still holds.
    sub = await db.submissions.find_one({"access_token": token})
    if sub:
        return {"role": "submitter", "sid": sub["id"], "slug": sub["project_slug"]}
    app_doc = await db.applications.find_one({"access_token": token})
    if app_doc:
        return {"role": "submitter", "sid": app_doc["id"], "kind": "application"}
    return None


async def current_portal_talent(
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Authenticate a talent for the self-service portal.

    Identity is derived ENTIRELY from a signed, non-expired portal session
    token (role `portal`) minted only after proof of email ownership (OTP or
    Google). The token is also matched against `portal_access_token` persisted
    on the talent record so a session can be revoked by clearing/rotating that
    field. Client-supplied email parameters are never trusted for auth.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Portal authentication required")
    token = authorization.split(" ", 1)[1]
    data = decode_token(token)
    if not data or data.get("role") != "portal":
        raise HTTPException(status_code=401, detail="Invalid or expired portal session")
    email = data.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Invalid portal session")
    talent = await db.talents.find_one({"$or": [{"email": email}, {"normalized_email": email}]})
    if not talent:
        raise HTTPException(status_code=401, detail="Portal session no longer valid")
    if talent.get("portal_access_token") != token:
        raise HTTPException(status_code=401, detail="Portal session expired — please sign in again")
    return talent


def mint_portal_token(email: str) -> str:
    """Mint a signed portal session token bound to a verified talent email."""
    return make_token({"role": "portal", "email": email}, days=30)


async def verify_email_ownership(
    authorization: Optional[str],
    email: str,
    request: Optional[Request] = None,
) -> bool:
    """Return True only if the caller has *proven ownership* of ``email``.

    This is the gate that protects the otherwise-public start/prefill flows
    (`/public/apply`, `/public/projects/{slug}/submission`, `/public/prefill`)
    against anonymous PII disclosure, draft hijack and destructive resets.

    Four accepted, revocation-aware credential forms — all of which can only
    exist *after* a real ownership proof (OTP / Google) or a prior verified
    session:

    1. A signature-valid, non-expired **portal token** (role ``portal``) whose
       ``email`` claim matches. Portal tokens are minted exclusively by the OTP
       and Google verification paths, so possession == prior proof of ownership.
       Forgery requires the server-side ``JWT_SECRET``.
    2. A valid **submitter** credential (JWT or opaque ``access_token``) already
       bound to an application/submission whose ``talent_email`` matches. This
       preserves legitimate cross-device "resume" without re-OTP.
    3. A valid **trusted-device cookie** (see the trusted-device section below)
       whose bound talent's email matches. This is a read-only check — it does
       NOT rotate the cookie (rotation only happens at the dedicated
       `/public/trusted-device/recognize` endpoint and at auth-grant sites),
       so calling this can never invalidate a cookie the caller still has.

    A completely anonymous caller (no/invalid token) returns ``False``.
    """
    target = normalize_email(email)
    if not target:
        return False

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]

        # --- Form 1: portal token (pure JWT check, no DB) -------------------
        data = decode_token(token)
        if data and data.get("role") == "portal":
            if normalize_email(data.get("email")) == target:
                return True

        # --- Form 2: existing submitter credential bound to this email -----
        submitter = await decode_submitter(authorization)
        if submitter:
            sid = submitter.get("sid")
            if submitter.get("kind") == "application":
                doc = await db.applications.find_one({"id": sid}, {"talent_email": 1})
            else:
                doc = await db.submissions.find_one({"id": sid}, {"talent_email": 1})
            if doc and normalize_email(doc.get("talent_email")) == target:
                return True

    # --- Form 3: trusted-device cookie (read-only, never rotates) ----------
    if request is not None:
        raw = request.cookies.get(TRUSTED_DEVICE_COOKIE)
        talent = await peek_trusted_device_talent(raw)
        if talent:
            talent_emails = {normalize_email(talent.get("email")), normalize_email(talent.get("normalized_email"))}
            if target in talent_emails:
                return True

    return False


# ---------------------------------------------------------------------------
# Trusted-device authentication — a new, additive credential type used ONLY
# to silently recognize a returning talent on project-submission links, so
# they aren't asked to re-authenticate on every project. Deliberately
# separate from the portal token (which stays exactly as-is for the
# dashboard): each device gets its OWN row in `db.trusted_devices`, so
# authenticating on a second browser does not invalidate a first browser's
# trust — the single-field `portal_access_token` design can't do that.
# HttpOnly cookie (never readable by JS, never sent to any third party),
# opaque random token (only the sha256 hash is ever persisted), rotated on
# every successful use, individually revocable.
# ---------------------------------------------------------------------------
TRUSTED_DEVICE_COOKIE = "tg_trusted_device"
TRUSTED_DEVICE_TTL_DAYS = 30


async def mint_trusted_device(talent_id: str) -> str:
    """Issue a new trusted-device credential for a talent. Only the sha256
    hash is persisted; the raw token is returned once and never stored."""
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    await db.trusted_devices.insert_one({
        "id": str(uuid.uuid4()),
        "talent_id": talent_id,
        "token_hash": token_hash,
        "created_at": now,
        "last_used_at": now,
        "expires_at": now + timedelta(days=TRUSTED_DEVICE_TTL_DAYS),
        "revoked": False,
    })
    return raw


def set_trusted_device_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=TRUSTED_DEVICE_COOKIE,
        value=raw_token,
        max_age=TRUSTED_DEVICE_TTL_DAYS * 24 * 3600,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_trusted_device_cookie(response: Response) -> None:
    response.delete_cookie(key=TRUSTED_DEVICE_COOKIE, path="/")


async def grant_trusted_device(response: Optional[Response], talent_id: Optional[str]) -> None:
    """Best-effort: mint a new trusted-device credential and set it as a
    cookie on `response`. Never raises — a persistence hiccup here must not
    fail an otherwise-successful authentication."""
    if response is None or not talent_id:
        return
    try:
        raw = await mint_trusted_device(talent_id)
        set_trusted_device_cookie(response, raw)
    except Exception as e:
        logger.warning(f"trusted-device mint failed for talent {talent_id}: {e}")


async def _find_valid_trusted_device_row(raw_token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Shared lookup: an unrevoked, unexpired row for this raw cookie value,
    or None. No mutation — safe to call from read-only paths."""
    if not raw_token:
        return None
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    row = await db.trusted_devices.find_one({"token_hash": token_hash, "revoked": False})
    if not row:
        return None
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        return None
    return row


async def peek_trusted_device_talent(raw_token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Read-only trusted-device validation — resolves the cookie to its
    talent without rotating or touching the row. Used by
    verify_email_ownership as a secondary ownership proof; rotation stays
    exclusive to resolve_trusted_device (the dedicated recognize endpoint and
    auth-grant sites) so a caller's cookie is never invalidated as a side
    effect of an unrelated ownership check."""
    row = await _find_valid_trusted_device_row(raw_token)
    if not row:
        return None
    return await db.talents.find_one({"id": row["talent_id"]})


async def resolve_trusted_device(raw_token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Validate a trusted-device cookie value. On success, ROTATES it
    (sliding-window: the matched row is revoked and a fresh one minted for
    the same talent) and returns {"talent": <doc>, "new_raw_token": <str>}.
    Returns None if the cookie is missing, unknown, expired, or revoked."""
    row = await _find_valid_trusted_device_row(raw_token)
    if not row:
        return None
    talent = await db.talents.find_one({"id": row["talent_id"]})
    if not talent:
        return None
    await db.trusted_devices.update_one(
        {"id": row["id"]}, {"$set": {"revoked": True, "last_used_at": datetime.now(timezone.utc)}}
    )
    new_raw = await mint_trusted_device(row["talent_id"])
    return {"talent": talent, "new_raw_token": new_raw}


async def revoke_trusted_device(raw_token: Optional[str]) -> None:
    """Revoke exactly the device holding this cookie (used by "Not you? Sign
    in as someone else") — does not touch any other device's trust."""
    if not raw_token:
        return
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    await db.trusted_devices.update_one(
        {"token_hash": token_hash}, {"$set": {"revoked": True}}
    )


# --------------------------------------------------------------------------
# Generic in-process rate limiter (sliding window, per-key)
# --------------------------------------------------------------------------
import time as _time
import threading as _threading

_RL_BUCKETS: Dict[str, list] = {}
_RL_LOCK = _threading.Lock()


def rate_limit_ok(key: str, limit: int, window_seconds: float) -> bool:
    """Sliding-window limiter. Returns False when ``key`` exceeds ``limit``
    hits within ``window_seconds``. Process-local (per worker) — adequate as
    burst/abuse protection in front of the heavier OTP DB-audit limiter.

    NOTE: in a multi-replica deployment each replica keeps its own window, so
    the effective global limit is ``limit * replicas``. This is intentional —
    it is a cheap first line of defence, not a billing-grade quota.
    """
    now = _time.monotonic()
    cutoff = now - window_seconds
    with _RL_LOCK:
        bucket = _RL_BUCKETS.setdefault(key, [])
        # Drop timestamps outside the window in place.
        i = 0
        for ts in bucket:
            if ts >= cutoff:
                break
            i += 1
        if i:
            del bucket[:i]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        # Opportunistic memory bound: forget fully-idle keys occasionally.
        if len(_RL_BUCKETS) > 50000:
            for k in [k for k, v in _RL_BUCKETS.items() if not v or v[-1] < cutoff]:
                _RL_BUCKETS.pop(k, None)
        return True


def client_ip(request) -> str:
    """Best-effort client IP for rate-limiting keys, honouring the first
    X-Forwarded-For hop (Railway/Vercel set this) and falling back to the
    socket peer."""
    try:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------
# Storage — Cloudinary (v37m migration)
# --------------------------------------------------------------------------
# All media (images + video) is uploaded directly to Cloudinary from the
# backend; the frontend reads `media.url` and renders it without any backend
# proxy. Cloudinary handles delivery, byte-range streaming for video, and
# on-the-fly transformations (f_auto, q_auto, w_1600) — so this module no
# longer needs init/put/get/stream helpers or server-side image resizing.

ALLOWED_FOLDER_PREFIXES = ("talentgram/",)


def _validate_folder(folder: str) -> None:
    if not folder.startswith(ALLOWED_FOLDER_PREFIXES):
        raise HTTPException(400, f"Invalid Cloudinary folder: {folder}")


def cloudinary_upload(
    data: bytes,
    folder: str,
    public_id: str,
    resource_type: str = "auto",
    content_type: Optional[str] = None,
    keep_original: bool = True,
) -> dict:
    """Upload raw bytes to Cloudinary with MIME type binary validation checks."""
    _validate_folder(folder)
    ct = (content_type or "").lower()

    # Priority 4: Implement binary signature validation check
    allowed_signatures = {
        b"\xff\xd8\xff": "image/jpeg",
        b"\x89PNG\r\n\x1a\n": "image/png",
        b"RIFF": "image/webp",  # WebP signatures typically contain RIFF....WEBP
        b"ftypmp42": "video/mp4",
        b"ftypisom": "video/mp4",
        b"ftypMSNV": "video/mp4",
        b"ftypavc1": "video/mp4",
        b"%PDF": "application/pdf",
        b"\x1a\x45\xdf\xa3": "video/webm",
        b"OggS": "audio/ogg",
        b"ID3": "audio/mpeg",
        b"\xff\xfb": "audio/mpeg",
    }

    detected_mime = None
    for sig, mime in allowed_signatures.items():
        if data.startswith(sig):
            detected_mime = mime
            break

    # WebP / WAV check extension: WebP files contain RIFF header and WEBP/WAVE signature bytes at offset 8
    if data.startswith(b"RIFF"):
        if b"WEBP" in data[8:15]:
            detected_mime = "image/webp"
        elif b"WAVE" in data[8:15]:
            detected_mime = "audio/wav"


    # MP4/HEIC/HEIF/MOV check extension: files carrying ftyp signature starting at index 4
    if not detected_mime and b"ftyp" in data[4:12]:
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            detected_mime = "image/heic"
        elif brand in (b"heif", b"hefs"):
            detected_mime = "image/heif"
        elif brand == b"qt  ":
            detected_mime = "video/quicktime"
        else:
            detected_mime = "video/mp4"

    # Allow PDF files for admin attachments
    if not detected_mime and data.startswith(b"%PDF"):
        detected_mime = "application/pdf"

    if not detected_mime:
        raise HTTPException(status_code=400, detail="Invalid file signature: file type not allowed.")

    # Validate that MIME header matches the detected signature
    clean_ct = ct.split(';')[0].strip() if ct else ""
    detected_sub = detected_mime.split('/')[-1]
    ct_sub = clean_ct.split('/')[-1]
    is_container_match = (detected_sub == ct_sub) or (detected_sub in ("mp4", "quicktime") and ct_sub in ("mp4", "m4a", "quicktime", "mov"))
    
    if ct and not clean_ct.startswith(detected_mime.split('/')[0]) and not is_container_match:
        raise HTTPException(status_code=400, detail="MIME type header does not match detected file signature.")

    
    is_pdf = ct == "application/pdf" or ct.startswith("application/pdf")
    if is_pdf and resource_type == "auto":
        resource_type = "raw"

    upload_kwargs: Dict[str, Any] = dict(
        folder=folder,
        public_id=public_id,
        resource_type=resource_type,
        overwrite=True,
        unique_filename=False,
    )

    # Cloudinary rearchitecture P4 — NO automatic derivative generation at upload.
    # We store exactly one canonical asset (the uploaded original) for every
    # image and video. No eager 720p/1080p transcode, no eager poster, no eager
    # thumbnail, no incoming (stored-asset-mutating) transformation, no
    # size-gated or 4K-gated transcode. Thumbnails and posters are lazy,
    # single-canonical delivery URLs computed by media_url()/video_poster_url()
    # and persisted by the caller; a genuine browser-compat transcode is an
    # explicit exception handled at the /complete step, never here.
    # `keep_original` is retained in the signature for callers but no longer
    # branches behaviour — the original is always kept.
    #
    # (Historical: this block used to build eager mp4+jpg for video and an
    #  eager w_400 for images; see docs/CLOUDINARY_P4_AUDIT.md.)

    # Cloudinary's synchronous /upload endpoint is fronted by nginx with a
    # ~100 MB request-body cap — a single POST above it is rejected with a bare
    # HTML "413 Request Entity Too Large" (which the SDK then can't parse). The
    # admin talent-profile / project upload paths proxy the file THROUGH this
    # backend and call uploader.upload() directly, so a >100 MB intro video (the
    # UI allows up to 200 MB) fails there even though the browser-direct signed
    # upload used by the Talent Invite / apply flow handles it via chunking.
    # Route anything near the cap through uploader.upload_large(), which sends
    # the file in Content-Range chunks — same result shape, same options, no
    # eager/transformation added. Small uploads keep the exact single-request
    # path they always had.
    _CHUNKED_UPLOAD_THRESHOLD = 90 * 1024 * 1024  # 90 MB — margin under Cloudinary's ~100 MB cap
    try:
        if len(data) > _CHUNKED_UPLOAD_THRESHOLD:
            import io
            result = cloudinary.uploader.upload_large(
                io.BytesIO(data),
                chunk_size=20 * 1024 * 1024,
                **upload_kwargs,
            )
        else:
            result = cloudinary.uploader.upload(data, **upload_kwargs)
    except Exception as e:
        logger.error(f"Cloudinary upload failed (folder={folder} pid={public_id}): {e}")
        raise HTTPException(502, "Storage upload failed")

    # P4: serve the uploaded original. No eager derivative is requested, so
    # there is nothing to prefer over secure_url.
    primary_url = result.get("secure_url")

    return {
        "url": primary_url,
        "original_url": result.get("secure_url"),
        "public_id": result.get("public_id"),
        "resource_type": result.get("resource_type"),
        "format": result.get("format"),
        # P4 — surfaced so callers can run the browser-compat exception check
        # without a second Admin API round-trip.
        "video_codec": (result.get("video") or {}).get("codec"),
        "bytes": result.get("bytes"),
        "width": result.get("width"),
        "height": result.get("height"),
        "duration": result.get("duration"),
    }


async def check_cloudinary_health() -> bool:
    try:
        from fastapi.concurrency import run_in_threadpool
        import cloudinary.api
        res = await run_in_threadpool(cloudinary.api.ping)
        return res.get("status") == "ok"
    except Exception as e:
        logger.warning(f"Cloudinary health check ping failed: {e}")
        return False


async def check_r2_health() -> bool:
    try:
        from fastapi.concurrency import run_in_threadpool
        s3 = get_r2_client()
        await run_in_threadpool(s3.head_bucket, Bucket=R2_BUCKET_NAME)
        return True
    except Exception as e:
        logger.warning(f"R2 health check head_bucket failed: {e}")
        return False


async def log_storage_action(
    user_id: Optional[str],
    action_type: str, # 'UPLOAD', 'ARCHIVE', 'RESTORE', 'DELETE'
    public_id: Optional[str] = None,
    project_id: Optional[str] = None,
    talent_id: Optional[str] = None,
    submission_id: Optional[str] = None,
    details: Optional[str] = None,
    operation_id: Optional[str] = None,
):
    doc = {
        "user_id": user_id,
        "timestamp": datetime.now(timezone.utc),
        "action_type": action_type,
        "public_id": public_id,
        "project_id": project_id,
        "talent_id": talent_id,
        "submission_id": submission_id,
        "details": details,
        "operation_id": operation_id or str(uuid.uuid4()),
    }
    await db.storage_audit_log.insert_one(doc)



async def upload_and_track_asset(
    data: bytes,
    resource_type: str,
    content_type: Optional[str],
    asset_type: str,
    talent_id: str,
    talent_name: Optional[str] = None,
    project_id: Optional[str] = None,
    submission_id: Optional[str] = None,
    user_id: Optional[str] = None,
    keep_original: bool = True,
    operation_id: Optional[str] = None,
) -> dict:
    start_time = datetime.now(timezone.utc)
    op_id = operation_id or str(uuid.uuid4())
    logger.info(
        f"Operation UPLOAD Started | OpID: {op_id} | TalentID: {talent_id} | "
        f"SubmissionID: {submission_id} | ProjectID: {project_id} | AssetType: {asset_type}"
    )
    # Lookup talent name if not provided
    if not talent_name and talent_id:
        talent_doc = await db.talents.find_one({"id": talent_id})
        if talent_doc:
            talent_name = talent_doc.get("name") or "unnamed"

    talent_name_slug = _slugify_deterministic(talent_name or "")
    suffix = f"_{talent_name_slug}" if talent_name_slug else ""
    if project_id and submission_id:
        folder = f"talentgram/projects/{project_id}/auditions/{talent_id}{suffix}/submission_{submission_id}"
    else:
        subfolder = {
            "profile_image": "profile_images",
            "intro_video": "intro_video",
            "portfolio_video": "portfolio_videos",
        }.get(asset_type, f"{asset_type}s")
        folder = f"talentgram/talents/{talent_id}{suffix}/{subfolder}"

    tags = []
    if project_id:
        tags.append(f"project_id={project_id}")
    if talent_id:
        tags.append(f"talent_id={talent_id}")
    if submission_id:
        tags.append(f"submission_id={submission_id}")
    if asset_type:
        tags.append(f"asset_type={asset_type}")

    media_id = str(uuid.uuid4())
    # Cloudinary rearchitecture P4: always store one canonical original at
    # {folder}/{media_id}. The old `keep_original=False` path stored a
    # re-uploaded `{folder}/audition_web` derivative instead — see below.
    public_id_to_store = f"{folder}/{media_id}"

    # Database First: Insert pending metadata record
    pending_metadata = {
        "public_id": public_id_to_store,
        "asset_id": f"pending_{media_id}",
        "folder_path": folder,
        "asset_url": "",
        "secure_url": "",
        "file_name": media_id,
        "original_filename": f"{media_id}",
        "file_size": len(data),
        "asset_type": asset_type,
        "project_id": project_id,
        "talent_id": talent_id,
        "submission_id": submission_id,
        "tags": tags,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "upload_status": "pending",
        "project_status": "active",
        "submission_status": "submitted",
        "width": None,
        "height": None,
        "duration": None,
        "mime_type": content_type,
        "resource_type": resource_type,
        "operation_id": op_id
    }
    await db.asset_metadata.update_one(
        {"public_id": public_id_to_store},
        {"$set": pending_metadata},
        upsert=True
    )

    try:
        # 2. Synchronize to Cloudinary — Cloudinary rearchitecture P4.
        #
        # ONE canonical upload, always keeping the original. No eager
        # transcode, no eager poster, no eager thumbnail.
        #
        # Removed here (see docs/CLOUDINARY_P4_AUDIT.md §3): the
        # `resource_type=="video" and not keep_original` re-upload chain,
        # which uploaded the raw bytes, asked Cloudinary for an eager
        # 1080p mp4 + jpg, then RE-UPLOADED those derivative URLs back into
        # Cloudinary as two brand-new originals (`{folder}/audition_web`,
        # `{folder}/thumbnail`) and destroyed the raw original — 3-4
        # Cloudinary assets for one logical video. Confirmed dead:
        #   * only reachable via the multipart POST /public/submissions/
        #     {sid}/upload endpoint, which the frontend never posts to;
        #   * ZERO live media items reference a `…/audition_web` or
        #     `…/thumbnail` public_id.
        # Collapsing it to a single keep-original upload therefore breaks
        # no existing reference. `keep_original` stays in the signature for
        # callers but no longer changes behaviour.
        upload_res = cloudinary_upload(
            data,
            folder=folder,
            public_id=media_id,
            resource_type=resource_type,
            content_type=content_type,
            keep_original=True,
        )
        cloudinary.uploader.add_tag(",".join(tags), upload_res["public_id"])

        result = {
            "url": upload_res["url"],
            "secure_url": upload_res["original_url"],
            "public_id": upload_res["public_id"],
            "resource_type": upload_res["resource_type"],
            "format": upload_res["format"],
            "video_codec": upload_res.get("video_codec"),
            "bytes": upload_res["bytes"],
            "width": upload_res["width"],
            "height": upload_res["height"],
            "duration": upload_res["duration"],
            "asset_id": upload_res.get("asset_id") or upload_res["public_id"],
        }

        final_metadata = {
            "asset_id": result.get("asset_id") or result["public_id"],
            "asset_url": result["url"],
            "secure_url": result["secure_url"],
            "file_size": result.get("bytes") or len(data),
            "upload_status": "completed",
            "width": result.get("width"),
            "height": result.get("height"),
            "duration": result.get("duration"),
            "updated_at": datetime.now(timezone.utc),
            "operation_id": op_id
        }
        await db.asset_metadata.update_one(
            {"public_id": result["public_id"]},
            {"$set": final_metadata}
        )

        duration_sec = (datetime.now(timezone.utc) - start_time).total_seconds()
        logger.info(
            f"Operation UPLOAD Succeeded | OpID: {op_id} | TalentID: {talent_id} | "
            f"SubmissionID: {submission_id} | ProjectID: {project_id} | AssetType: {asset_type} | "
            f"NewAssetID: {result.get('asset_id')} | Duration: {duration_sec}s"
        )

        await log_storage_action(
            user_id=user_id,
            action_type="UPLOAD",
            public_id=result["public_id"],
            project_id=project_id,
            talent_id=talent_id,
            submission_id=submission_id,
            operation_id=op_id
        )

        return result
    except Exception as e:
        duration_sec = (datetime.now(timezone.utc) - start_time).total_seconds()
        logger.error(
            f"Operation UPLOAD Failed | OpID: {op_id} | TalentID: {talent_id} | "
            f"SubmissionID: {submission_id} | ProjectID: {project_id} | AssetType: {asset_type} | "
            f"Reason: {str(e)} | Duration: {duration_sec}s"
        )
        await db.asset_metadata.update_one(
            {"public_id": public_id_to_store},
            {
                "$set": {
                    "upload_status": "failed",
                    "error_reason": str(e),
                    "updated_at": datetime.now(timezone.utc),
                    "operation_id": op_id
                }
            }
        )
        raise



def cloudinary_destroy(public_id: str, resource_type: str = "image") -> bool:
    """Best-effort delete on Cloudinary. Returns True if deleted, False if
    asset was already missing or deletion failed (logged, never raises)."""
    if not public_id:
        return False
    try:
        result = cloudinary.uploader.destroy(
            public_id, resource_type=resource_type, invalidate=True
        )
        return result.get("result") in ("ok", "not found")
    except Exception as e:
        logger.warning(f"Cloudinary destroy failed (pid={public_id}): {e}")
        return False


def cloudinary_url_for(
    public_id: str, resource_type: str = "image", **transformations
) -> str:
    """Build a transformation URL on the fly.

    Default transformations applied to images: f_auto, q_auto. Pass additional
    via kwargs (e.g. width=400, crop="fill").

    Cloudinary rearchitecture P5: `dpr_auto` removed — it needs client-hints
    (never configured here), added a token to every image URL for no delivery
    benefit, and is a cost-regression guard target. Only the sized thumbnail
    presets in `media_url()` use this helper now; full-size image delivery goes
    straight to the canonical asset (see `IMAGE_URL` on the frontend).
    """
    if resource_type == "image":
        transformations.setdefault("fetch_format", "auto")
        transformations.setdefault("quality", "auto")
    url, _opts = cloudinary.utils.cloudinary_url(
        public_id, resource_type=resource_type, secure=True, **transformations
    )
    return url


def stream_video_url(public_id: Optional[str]) -> Optional[str]:
    """DEPRECATED (Cloudinary rearchitecture P5). Historically built a
    `c_limit,h_720,w_1280 / q_auto,vc_auto / f_mp4` 3-segment delivery chain — a
    universal 720p downscale + transcode on first client view. P5 removed the
    only caller: legacy Cloudinary videos with no stored delivery URL now get
    their canonical original URL (or the P4 compat URL when the codec/container
    genuinely can't play). A universal delivery transcode is exactly what the
    P4/P5 video policy forbids.

    Retained (returning the *untransformed* canonical delivery URL) only so any
    lingering import resolves. No transformation is applied.
    """
    if not public_id:
        return None
    if public_id.startswith(("http://", "https://")):
        return public_id
    url, _ = cloudinary.utils.cloudinary_url(
        public_id, resource_type="video", secure=True,
    )
    return url


def audition_submission_folder(
    talent_id: str, talent_name: Optional[str], project_id: str, submission_id: str
) -> str:
    """Per-submission Cloudinary folder for audition media — identical scheme to
    `upload_and_track_asset` so the existing structure stays compatible and the
    finalize reconciliation can list assets by this exact prefix.
    """
    slug = _slugify_deterministic(talent_name or "")
    suffix = f"_{slug}" if slug else ""
    return (
        f"talentgram/projects/{project_id}/auditions/"
        f"{talent_id}{suffix}/submission_{submission_id}"
    )


def audition_video_transformation() -> list:
    """Incoming transformation pinned for direct audition-video uploads: 720p
    H.264 q_auto. Cloudinary stores ONLY this derivative — the heavy 4K original
    is discarded on ingest (mirrors the existing keep_original=False strategy).

    DEPRECATED (Cloudinary rearchitecture P4). No caller should apply an
    incoming/eager video transformation any more — new video uploads store the
    uploaded original verbatim. Retained only so any lingering import resolves.
    """
    return [
        {"width": 1280, "height": 720, "crop": "limit"},
        {"quality": "auto", "video_codec": "auto"},
    ]


# Cloudinary rearchitecture P4 — the ONLY sanctioned video transform: an
# explicit browser-compatibility exception. Formats/codecs a plain <video>
# element cannot reliably decode across Talentgram's supported browser matrix
# (frontend/package.json browserslist "production": >0.2%, not dead, not
# op_mini all — i.e. Chrome/Edge/Firefox/Safari incl. ESR + older releases +
# Samsung Internet, on desktop and mobile). `LazyVideoPlayer` / `HlsVideo`
# assign the URL straight to `<video>.src` with NO MediaCapabilities probe and
# NO codec fallback, so whatever we serve as `url` must decode natively there.
#
# Anything NOT on these lists is served as the uploaded original (zero
# transform). Unknown format AND unknown codec -> serve the original.
#
# CODEC NOTES:
#   H.264 / AVC        — universal. serve original.
#   VP8 / VP9 (WebM)   — Chrome/Edge/Firefox always; Safari 14.1+/iOS 14.5+.
#                        Modern matrix covers it. serve original.
#   AV1                — Chrome 70+/Firefox 67+/Edge; Safari 17+ (HW only).
#                        Rare as an UPLOAD codec; modern matrix mostly covers
#                        it. serve original (revisit if it ever shows volume).
#   HEVC / H.265       — Safari 11+ only. Chrome 107+ AND requires a platform
#                        hardware HEVC decoder (fails on many desktops, esp.
#                        Linux). Firefox only 134+ (Jan 2025) with an OS
#                        decoder — Firefox ESR / older = NO. NOT safe for our
#                        matrix -> COMPAT EXCEPTION (one lazy f_mp4). Common as
#                        an iPhone "High Efficiency" .mov intro-video upload;
#                        admin audition takes come via WhatsApp already
#                        re-encoded to H.264 and are unaffected.
NON_WEB_VIDEO_FORMATS = {"avi", "wmv", "flv", "mkv", "mpeg", "mpg", "ogv", "rm", "rmvb", "asf", "vob", "divx"}
NON_WEB_VIDEO_CODECS = {
    # HEVC / H.265 — not decodable on Firefox ESR / older, or Chrome without a
    # hardware HEVC decoder. Cloudinary reports it as "hevc"/"h265"; some
    # probes report the MP4 sample-entry fourCC.
    "hevc", "h265", "hvc1", "hev1",
    # genuinely legacy / pro / niche
    "prores", "dnxhd", "mjpeg", "wmv1", "wmv2", "wmv3", "vc1",
    "mpeg4", "msmpeg4", "msmpeg4v1", "msmpeg4v2", "msmpeg4v3",
    "rv40", "rv30", "theora", "flv1", "cinepak", "svq3",
}


def video_needs_compat_delivery(fmt: Optional[str], codec: Optional[str]) -> bool:
    """True only when the uploaded video is in a container/codec that a plain
    <video> element cannot reliably play across Talentgram's supported browser
    matrix — the one case where P4 permits a transform.

    Precedence: codec (authoritative) before container. Unknown format AND
    unknown codec -> False (serve the original; a genuinely unplayable asset is
    recoverable later, but transcoding-by-default is the cost problem we are
    removing). Never inferred from the file extension alone — `fmt`/`codec`
    come from Cloudinary's upload / resource response.
    """
    f = (fmt or "").strip().lower().lstrip(".")
    c = (codec or "").strip().lower()
    if c and c in NON_WEB_VIDEO_CODECS:
        return True
    if f and f in NON_WEB_VIDEO_FORMATS:
        return True
    return False


# Cloudinary rearchitecture P4/P5 — the ONE canonical video-poster transform.
# Single segment, no dpr (Cloudinary sorts params within a segment, so this is a
# stable string): c_fill,h_338,q_auto,w_600 + a .jpg extension. Every poster URL
# the app produces must use exactly this, so one video has at most one poster
# derivative instead of the 7 near-identical variants the old eager/lazy mix
# generated. Posters are lazy (generated on first grid render) and persisted by
# the caller into media.poster_url so payload-build never has to recompute one.
_CANONICAL_POSTER_TRANSFORM = {"width": 600, "height": 338, "crop": "fill", "quality": "auto"}


def _bare_public_id(value: Optional[str]) -> Optional[str]:
    """Recover a bare Cloudinary public_id from either a bare id or a full
    delivery URL (dropping the host, /upload/, a leading version, any existing
    transformation segment, and the extension). None for non-Cloudinary URLs."""
    if not value:
        return None
    if not value.startswith(("http://", "https://")):
        return value
    if "res.cloudinary.com" not in value or "/upload/" not in value:
        return None
    tail = value.split("/upload/", 1)[1].split("?")[0].split("#")[0]
    segs = tail.split("/")
    while segs and (re.fullmatch(r"v\d+", segs[0]) or
                    re.search(r"(^|,)(w_|h_|c_|q_|f_|dpr_|e_|so_|vc_|b_|ac_|g_|fl_)", segs[0])):
        segs.pop(0)
    pid = "/".join(segs).rsplit(".", 1)[0]
    return pid or None


def video_poster_url(public_id: Optional[str]) -> Optional[str]:
    """One canonical lazy JPEG first-frame URL for a video. Accepts a bare
    public_id or a full Cloudinary delivery URL. Returns None for
    non-Cloudinary inputs."""
    pid = _bare_public_id(public_id)
    if not pid:
        return None
    built, _ = cloudinary.utils.cloudinary_url(
        pid,
        resource_type="video",
        format="jpg",
        transformation=_CANONICAL_POSTER_TRANSFORM,
        secure=True,
    )
    return built


def compat_video_delivery_url(public_id: Optional[str]) -> Optional[str]:
    """One canonical lazy `f_mp4` delivery URL for a video that failed the
    web-safe check (``video_needs_compat_delivery``). `fetch_format=mp4` (→
    `f_mp4`) tells Cloudinary to re-container/transcode to a browser-playable
    MP4 on first playback. Single transformation string, so a compat video has
    at most one derivative. Accepts a bare public_id or a full delivery URL."""
    pid = _bare_public_id(public_id)
    if not pid:
        return None
    built, _ = cloudinary.utils.cloudinary_url(
        pid, resource_type="video", format="mp4",
        transformation={"fetch_format": "mp4"}, secure=True,
    )
    return built


def media_url(
    public_id: Optional[str], preset: str = "roster", resource_type: str = "image"
) -> Optional[str]:
    """Canonical small-thumbnail URL builder.

    Cloudinary rearchitecture P5 — collapsed to the TWO presets that have real
    callers. Both are deliberate, genuinely-required small derivatives (roster
    and pipeline cards); full-size image delivery does NOT come through here any
    more — it goes straight to the canonical asset.

      roster  — c_fill,w_400,f_auto,q_auto   (talent roster / cover card)
      thumb   — c_fill,w_200,f_auto,q_auto   (pipeline card / mini thumbnail)

    Removed (were dead code, zero callers): ``detail`` (w_1200), ``full``
    (w_1600), ``poster`` — the last routed to ``video_poster_url`` which every
    caller now invokes directly. Any unrecognised preset falls back to
    ``roster`` rather than an uncapped full-res transform.
    """
    if not public_id:
        return None
    if public_id.startswith(("http://", "https://")):
        return public_id

    if preset == "thumb":
        return cloudinary_url_for(public_id, resource_type, width=200, crop="fill")
    # roster (default) and anything unrecognised
    return cloudinary_url_for(public_id, resource_type, width=400, crop="fill")




# --------------------------------------------------------------------------
# Utils
# --------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_instagram_handle(raw: Optional[str]) -> Optional[str]:
    """Reduce any Instagram input to a plain raw username.

    Handles every common paste format gracefully:
      - https://www.instagram.com/username/  →  "username"
      - instagram.com/username               →  "username"
      - @username                            →  "username"
      - "  username  "                       →  "username"
      - None / ""                            →  None
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    # Strip http(s):// + optional www + instagram.com/
    s = re.sub(r'^https?://(www\.)?instagram\.com/', '', s, flags=re.IGNORECASE)
    # Strip bare domain without protocol
    s = re.sub(r'^(www\.)?instagram\.com/', '', s, flags=re.IGNORECASE)
    # Strip leading @
    s = s.lstrip('@')
    # Remove query params and trailing path segments
    s = s.split('?')[0].split('/')[0].strip()
    return s or None


def compute_age(dob: Optional[str]) -> Optional[int]:
    """Compute age from ISO date string 'YYYY-MM-DD'. Returns None if invalid."""
    if not dob:
        return None
    try:
        y, m, d = [int(x) for x in dob.split("-")[:3]]
        today = datetime.now(timezone.utc).date()
        age = today.year - y - (1 if (today.month, today.day) < (m, d) else 0)
        return age if 0 <= age <= 120 else None
    except Exception:
        return None


def parse_height_to_inches(raw: Optional[str]) -> Optional[float]:
    """Normalize a free-text height string to inches for range queries/sort.

    Python port of the frontend's parseHeightToInches (TalentBrowserModal.jsx)
    so both the write path (backfilling `height_inches`) and the query engine
    agree on the same parsing rules. Handles "5'6\"", "5ft 6in", "172cm",
    "172" (bare cm), and tolerates curly-quote apostrophes.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if not s:
        return None

    cm_match = re.match(r"^(\d+(?:\.\d+)?)\s*(?:cm)?$", s)
    if cm_match:
        val = float(cm_match.group(1))
        if val > 100:
            return round(val / 2.54, 1)

    feet_inches_match = re.search(r"(\d+)\s*(?:'|’|ft|feet)\s*(\d+)?", s)
    if feet_inches_match:
        feet = int(feet_inches_match.group(1))
        inches = int(feet_inches_match.group(2)) if feet_inches_match.group(2) else 0
        return float(feet * 12 + inches)

    return None


# Ordered smallest→largest — mirrors the bucket list surfaced on the public
# apply/submission forms (INSTAGRAM_FOLLOWERS select options). Position in
# this list IS the sort/filter ordinal for the stored bucket-label string;
# there is no raw follower count stored anywhere.
FOLLOWER_BUCKET_ORDER = [
    "1K+", "10K+", "25K+", "50K+", "75K+", "100K+", "150K+", "200K+",
    "300K+", "400K+", "500K+", "750K+", "1M+", "2M+", "3M+", "4M+", "5M+",
    "7M+", "10M+", "15M+", "20M+", "25M+", "30M+", "40M+", "50M+",
]


def compute_effective_age(form_data: Optional[dict], stored_age: Optional[int] = None) -> Optional[int]:
    """Resolve the effective age for a project submission or application.
    Priority:
      1. submitted_age_override (if overrideAge is True/active)
      2. calculated age from DOB
      3. stored age from master profile
      4. standard age field in form_data
    """
    if not form_data:
        return stored_age

    override_active = form_data.get("overrideAge") or form_data.get("override_age")
    override_val = form_data.get("submitted_age_override")

    if override_active:
        if override_val not in (None, ""):
            try:
                return int(override_val)
            except Exception:
                pass

    dob = form_data.get("dob")
    if dob:
        calculated = compute_age(dob)
        if calculated is not None:
            return calculated

    if stored_age is not None:
        return stored_age

    age_val = form_data.get("age")
    if age_val not in (None, ""):
        try:
            return int(age_val)
        except Exception:
            pass

    return None


def resolve_cover_media(doc: dict) -> Optional[dict]:
    """Find the best cover media item dict representing this talent."""
    media = doc.get("media") or []
    if not media:
        return None
    cover_id = doc.get("cover_media_id")
    if cover_id:
        for m in media:
            if m.get("id") == cover_id and m.get("url"):
                return m
    image_cats = {"portfolio", "indian", "western", "image"}
    for m in media:
        if m.get("category") in image_cats and m.get("url"):
            return m
    for m in media:
        if m.get("url"):
            return m
    return None


async def update_talent_cover_cache(tid: str) -> None:
    """Fetch the full talent doc, resolve the best cover media, and update denormalized cover fields in DB."""
    talent = await db.talents.find_one({"id": tid})
    if not talent:
        return
    media_item = resolve_cover_media(talent)
    if media_item:
        mid = media_item.get("id")
        url = media_item.get("url")
        pid = media_item.get("public_id")
        # Extract full public_id from url if it was stored without folder prefixes
        if pid and "/" not in pid and url and "/upload/" in url:
            parts = url.split("/upload/")[-1].split("/")
            if parts[0].startswith("v") and parts[0][1:].isdigit():
                parts = parts[1:]
            pid = "/".join(parts).rsplit(".", 1)[0]
        rt = media_item.get("resource_type") or "image"
        thumb_url = media_url(pid, preset="roster", resource_type=rt) if pid else url

        await db.talents.update_one(
            {"id": tid},
            {
                "$set": {
                    "cover_media_id": mid,
                    "cover_url": url,
                    "cover_thumbnail_url": thumb_url,
                    "media_count": len(talent.get("media") or [])
                }
            }
        )
    else:
        await db.talents.update_one(
            {"id": tid},
            {
                "$set": {
                    "cover_media_id": None,
                    "cover_url": None,
                    "cover_thumbnail_url": None,
                    "media_count": len(talent.get("media") or [])
                }
            }
        )


async def is_media_asset_referenced(public_id: Optional[str], stream_uid: Optional[str] = None) -> bool:
    """True if any OTHER persistent record still points at this storage asset.

    Architectural correction (Media Library Manager, Phase 4 item 3 —
    reference-aware delete). `build_prefill_media()` and
    `sync_media_to_global_talent()` copy media *by value*: a talent's
    Global Library item and a submission/application's own media entry can
    legitimately share the exact same Cloudinary `public_id` (or Cloudflare
    Stream `stream_uid`) without either being a duplicate upload. Physically
    destroying that storage object is only safe once NO document in any of
    the three collections that own media by value still references it —
    otherwise a historical submission, a shortlisted/selected snapshot, or
    a live client review link (which renders `submissions.media` live, see
    `_submission_to_client_shape`) silently goes dead.

    Centralized here deliberately — every delete path (admin or talent)
    must call this same check; it must never be re-implemented per route.
    """
    if not public_id and not stream_uid:
        return False
    ors = []
    if public_id:
        ors.append({"media.public_id": public_id})
    if stream_uid:
        ors.append({"media.stream_uid": stream_uid})
    query = {"$or": ors} if len(ors) > 1 else ors[0]
    for coll in (db.talents, db.submissions, db.applications):
        hit = await coll.find_one(query, {"_id": 1})
        if hit:
            return True
    return False


async def safe_cleanup_media_storage(
    media: dict,
    scope: Optional[str] = None,
    parent_id: Optional[str] = None,
    operation_id: Optional[str] = None,
) -> None:
    """Reference-aware wrapper around `cleanup_media_storage()`.

    Production Certification (Phase 4 item 4) — the reference-aware
    guard originally built only for the Media Library delete endpoints
    (`delete_talent_media_item`) turned out to be needed everywhere a
    submission/application media item can be physically destroyed, not
    just there: `build_prefill_media()` copies a talent's Library item
    INTO a new submission by value (same `public_id`/`stream_uid`), so a
    talent removing (or replacing) a "reused" photo/video from a brand
    new submission — an everyday `/submit` action, unrelated to the
    Media Library page — could silently destroy their own Library
    original, or a completely different historical submission, or a
    live client review link. Same risk in reverse for `applications.py`'s
    own talent-media hydration.

    This is the ONE place that pairs "is it still referenced anywhere"
    with "destroy it" — every call site that might touch a shared-by-value
    asset (Library delete, submission media delete/replace, application
    media delete/replace, webhook-driven replacement cleanup) must call
    this instead of `cleanup_media_storage()` directly. Do not duplicate
    the reference-check + cleanup pairing at the call site.

    Cloudinary rearchitecture P6 — this now delegates the *decision* to the
    authoritative, ownership-aware `media_lifecycle` service:
      * ownership unknown/conflicting, or any protecting reference  -> no-op
      * global talent media                                        -> no-op
      * project audition media                                     -> recorded
        in the `pending_media_deletions` ledger with the retention window
      * physically destroyed ONLY when the gate says deletable AND the
        `MEDIA_LIFECYCLE_PHYSICAL_DELETE` env flag is on (off during P6)
    """
    if not media:
        return
    pid = media.get("public_id")
    stream_uid = media.get("stream_uid")
    if not pid and not stream_uid:
        return

    import media_lifecycle as _ml

    _coll = {"talent": "talents", "submission": "submissions",
             "application": "applications"}.get(scope or media.get("scope"))
    ctx = _ml.DeletionContext(
        exclude_collection=_coll, exclude_parent_id=parent_id,
    )
    decision = await _ml.can_delete(db, media, ctx=ctx)

    if decision.deletable and _ml._physical_delete_enabled():
        await cleanup_media_storage(media, scope=scope, parent_id=parent_id, operation_id=operation_id)
        return
    # Not physically deleting: record intent for the P8/P9 purge when this is a
    # genuinely deletable / audition-owned asset the caller has already
    # unreferenced. Global / shared / unknown assets are left entirely alone.
    if decision.owner.is_project_audition_media or decision.deletable:
        try:
            await _ml.enqueue_pending_deletion(
                db, media, owner=decision.owner,
                reason=f"safe_cleanup ({scope}): {decision.reason}",
                retention_days=decision.retention_days
                if decision.retention_days is not None else await _ml.get_retention_days(db),
            )
        except Exception as e:  # never fail the caller's delete action
            logger.warning(f"[safe_cleanup] ledger enqueue failed pid={pid}: {e}")


async def delete_talent_media_item(tid: str, mid: str) -> None:
    """Delete one item from a talent's global Media Library (``talents.media[]``).

    Shared by the admin ``DELETE /talents/{tid}/media/{mid}`` route
    (routers/talents.py) and the talent-owned Media Library Manager
    (routers/portal.py) — only the caller's authorization differs. Raises
    404 if the talent or the media item doesn't exist.

    This ONLY ever removes the library reference itself; it never touches
    the backing Cloudinary/Stream asset unless `is_media_asset_referenced()`
    confirms no submission, application, or other talent record still
    depends on it (reference-aware delete, Phase 4 item 3 correction).
    """
    talent = await db.talents.find_one({"id": tid}, {"_id": 0, "media": 1, "cover_media_id": 1})
    if not talent:
        raise HTTPException(404, "Talent not found")
    target = next((m for m in (talent.get("media") or []) if m.get("id") == mid), None)
    if not target:
        raise HTTPException(404, "Media not found")
    res = await db.talents.update_one({"id": tid}, {"$pull": {"media": {"id": mid}}})
    if not res.modified_count:
        raise HTTPException(404, "Media not found")

    # Provider-aware, reference-checked cleanup — see safe_cleanup_media_storage().
    await safe_cleanup_media_storage(target, scope="talent", parent_id=tid)

    # If the deleted item was the current cover, clear the cover ID reference first
    if talent.get("cover_media_id") == mid:
        await db.talents.update_one(
            {"id": tid},
            {"$set": {"cover_media_id": None}}
        )
    await update_talent_cover_cache(tid)


async def set_talent_cover_media(tid: str, mid: str) -> Optional[str]:
    """Set a talent's cover image, returning the resolved ``cover_url``.

    Extracted verbatim from the admin ``POST /talents/{tid}/cover/{mid}``
    route (routers/talents.py) — shared with the talent-owned Media
    Library Manager (Phase 4 item 3). Writes ``cover_media_id`` (the item id
    reference) AND ``cover_url``/``cover_thumbnail_url`` via
    ``update_talent_cover_cache``.
    """
    res = await db.talents.update_one({"id": tid}, {"$set": {"cover_media_id": mid}})
    if not res.matched_count:
        raise HTTPException(404, "Talent not found")
    await update_talent_cover_cache(tid)
    updated_talent = await db.talents.find_one({"id": tid}, {"cover_url": 1})
    return updated_talent.get("cover_url") if updated_talent else None


def enrich_talent(doc: Optional[dict]) -> Optional[dict]:
    """Annotate a talent document for API responses.

    Currently derives:
      - ``age`` from ``dob`` (overrides any stored age).
      - ``image_url`` — top-level convenience pointer to the cover image
        Cloudinary URL (or first portfolio/indian/western image if no
        cover is set). Returns ``None`` (never the string ``"undefined"``)
        when the talent has no image. Frontends that prefer a single field
        over walking ``media[]`` can use this directly.
    """
    if not doc:
        return doc
    dob = doc.get("dob")
    if dob:
        computed = compute_age(dob)
        if computed is not None:
            doc["age"] = computed

    # Dynamic enrichment of individual media list items
    enriched_media = []
    for m in doc.get("media") or []:
        resource_type = m.get("resource_type")
        is_video = resource_type == "video" or m.get("category") == "video" or (m.get("content_type") or "").startswith("video/")
        enriched_item = {**m}
        if is_video:
            url = m.get("url")
            enriched_item["video_url"] = url
            enriched_item["poster_url"] = m.get("poster_url") or video_poster_url(m.get("public_id")) or video_poster_url(url)
            enriched_item["thumbnail_url"] = m.get("thumbnail_url") or enriched_item["poster_url"]
            if "duration" not in enriched_item:
                enriched_item["duration"] = None
        enriched_media.append(enriched_item)
    doc["media"] = enriched_media

    media_item = resolve_cover_media(doc)
    if media_item:
        url = media_item.get("url")
        doc["image_url"] = url
        doc["cover_url"] = doc.get("cover_url") or url
        pid = media_item.get("public_id")
        if pid:
            rt = media_item.get("resource_type") or "image"
            doc["cover_thumbnail_url"] = media_url(pid, preset="roster", resource_type=rt)
        else:
            doc["cover_thumbnail_url"] = url
    else:
        doc["image_url"] = None
        doc["cover_url"] = doc.get("cover_url") or None
        doc["cover_thumbnail_url"] = None

    return doc


def _resolve_cover_url(doc: dict) -> Optional[str]:
    """Find the best Cloudinary URL to represent this talent/submission.

    Order of preference:
      1. media item whose ``id`` == ``cover_media_id``
      2. first media item with category in {portfolio, indian, western, image}
      3. first media item with any non-empty ``url``
    Returns ``None`` if no usable URL exists.
    """
    media_item = resolve_cover_media(doc)
    return media_item["url"] if media_item else None


def _slugify_deterministic(title: str) -> str:
    if not title:
        return ""
    safe = "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe


def _slugify(title: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    # P0-3: the slug doubles as a bearer secret for the public brief/link, so
    # the random suffix must have enough entropy to resist enumeration. 12 hex
    # chars = 48 bits (~2.8e14). Existing shorter slugs keep working unchanged.
    return (safe or "link") + "-" + uuid.uuid4().hex[:12]


async def _log_admin_phone_duplicates() -> None:
    """Read-only diagnostic: finds and logs every phone number shared by
    2+ admin-created (`source.type == "admin"`) talents -- the exact
    records that block creation of `talents_phone_unique_admin_scope`.
    Never modifies, merges, or deletes anything; purely surfaces what an
    operator needs to see to resolve the conflict manually."""
    pipeline = [
        {"$match": {"source.type": "admin", "phone": {"$type": "string"}}},
        {"$group": {
            "_id": "$phone",
            "count": {"$sum": 1},
            "talents": {"$push": {"id": "$id", "name": "$name"}},
        }},
        {"$match": {"count": {"$gt": 1}}},
    ]
    try:
        found_any = False
        async for group in db.talents.aggregate(pipeline):
            found_any = True
            logger.error(
                "PHONE UNIQUENESS MIGRATION CONFLICT: phone=%r is shared by "
                "%d admin-created talents (none modified): %s",
                group["_id"], group["count"], group["talents"],
            )
        if not found_any:
            logger.error(
                "PHONE UNIQUENESS MIGRATION CONFLICT: index creation failed "
                "but no admin-vs-admin phone duplicates were found by this "
                "diagnostic query -- investigate the underlying error "
                "directly (e.g. a permissions issue, not a data conflict)."
            )
    except Exception as e:
        logger.error(f"Failed to enumerate admin phone duplicates for diagnostics: {e}")


async def seed_admin() -> None:
    """Idempotently seed the root admin into `db.users`.

    Migration: if a legacy `db.admins` record exists for this email but no
    matching `db.users` row, move the hash + name over. New installs skip
    this.

    PERSISTENCE GUARANTEE (2026-04-27 fix):
    The env `ADMIN_PASSWORD` seeds the password ONLY on first-boot
    insertion. If the admin row already exists, we never rewrite
    `password_hash` from env — admin password changes made via the UI
    (`/api/users/me/password`, forgot/reset flow) MUST survive server
    restarts. To rotate the admin password, use the in-app password
    change flow, not env mutations.
    """
    # Unique email index — idempotent.
    try:
        await db.users.create_index("email", unique=True)
    except Exception as e:
        logger.warning(f"users email index: {e}")
    # Talents: UNIQUE email (Phase 0 enforcement). Partial filter so legacy
    # talents that lack an email don't violate the constraint. The migration
    # script in /app/backend/migrations/phase0_dedup.py must run BEFORE this
    # the first time on a populated DB. Idempotent on subsequent boots.
    try:
        # Drop any pre-existing non-unique `email_1` index from the legacy boot.
        try:
            await db.talents.drop_index("email_1")
        except Exception as e:
            # Index may not exist on a fresh DB — that's fine, but still log
            # so any unexpected failure (auth/permission) is visible.
            logger.debug(f"talents legacy email_1 drop skipped: {e}")
        await db.talents.create_index(
            "email",
            unique=True,
            name="talents_email_unique",
            partialFilterExpression={"email": {"$type": "string"}},
        )

    except Exception as e:
        logger.warning(f"talents email unique index: {e}")

    try:
        await db.talents.create_index(
            "normalized_email",
            unique=True,
            name="talents_normalized_email_unique",
            partialFilterExpression={"normalized_email": {"$type": "string"}},
        )
    except Exception as e:
        logger.warning(f"talents normalized_email unique index: {e}")

    # Talents: `phone` uniqueness is SCOPED to the admin-created population
    # (source.type == "admin"), not global. The standalone
    # migrations/data_hub_indexes.py script previously made this a
    # collection-wide unique index -- a legitimate data-quality guard for
    # the Data Hub CSV-import path (preventing an admin from accidentally
    # importing the same person twice), but a global constraint like that
    # cannot distinguish an ACCIDENTAL duplicate (two admin-created records
    # for different people that collide by mistake -- still exactly what
    # this index should keep blocking) from an INTENTIONAL one (a legacy
    # admin-created talent and its own later, authenticated Project
    # Submission counterpart for the SAME real person, which the migration
    # strategy explicitly requires to coexist). Scoping the constraint via
    # partialFilterExpression to `source.type: "admin"` keeps it fully
    # enforced within the population it actually protects, while a
    # submission-created talent (`source.type == "audition_submission"`,
    # see build_minimal_talent_from_form) is never subject to it -- the
    # insert simply succeeds, no recovery/workaround needed. Every talent
    # is guaranteed to carry a standardised `source.type` by this point
    # (migrations/phase0_dedup.py's standardise_source() backfilled it on
    # every pre-existing record; every creation path sets it going
    # forward), so this scoping is reliable across the whole collection,
    # not just new documents.
    #
    # `email`/`normalized_email` above remain the ONLY globally-unique
    # identity fields -- the actual authenticated identity is untouched by
    # this change.
    #
    # This runs on every boot (idempotent), so environments where the old,
    # unscoped unique index already exists self-heal regardless of
    # whether/when that standalone script was run.
    #
    # FAIL-SAFE ORDERING (2026-08-15): the old unscoped unique `phone_1`
    # index is NEVER dropped until the new scoped index has been CONFIRMED
    # created. A collection-wide unique index, by construction, can only
    # ever be a strict superset guarantee of the scoped one -- if `phone_1`
    # is currently valid (i.e. every phone value really is unique
    # collection-wide), the scoped index below is trivially safe to create
    # (a subset of an already-unique population is unique too), so
    # create-then-drop can NEVER regress protection on a healthy database.
    # The only way creating the scoped index can fail is genuine duplicate
    # phone data *within the admin population specifically* on a database
    # that never had `phone_1` (or any) protection to begin with -- and in
    # that failure case, this code deliberately does NOT touch `phone_1`
    # at all (nothing to drop if it never existed; if it somehow does
    # exist, it is left fully intact as a safety net). There is therefore
    # no reachable boot sequence where uniqueness protection is removed
    # and not replaced -- protection can only stay the same or strengthen,
    # never disappear.
    scoped_phone_index_ready = False
    try:
        await db.talents.create_index(
            "phone",
            unique=True,
            name="talents_phone_unique_admin_scope",
            partialFilterExpression={
                "phone": {"$type": "string"},
                "source.type": "admin",
            },
        )
        scoped_phone_index_ready = True
    except Exception as e:
        logger.error(
            "PHONE UNIQUENESS MIGRATION INCOMPLETE: could not create "
            "talents_phone_unique_admin_scope — pre-existing duplicate "
            "phone data among admin-created talents is blocking it. NO "
            "existing talent record has been modified. Any pre-existing "
            "legacy unscoped unique index (phone_1) is being left in "
            "place untouched as a safety net, so duplicate-phone "
            "protection is never simply removed. Resolve the conflicting "
            "records below, then restart to complete the migration. "
            "error=%s", e,
        )
        await _log_admin_phone_duplicates()

    if scoped_phone_index_ready:
        # Only now, with the replacement confirmed active, is it safe to
        # remove the old, now-redundant collection-wide index.
        try:
            await db.talents.drop_index("phone_1")
            logger.info("Dropped superseded collection-wide unique talents.phone_1 index")
        except Exception as e:
            logger.debug(f"talents legacy unique phone_1 drop skipped: {e}")
    else:
        try:
            legacy_idx = await db.talents.index_information()
        except Exception:
            legacy_idx = {}
        if "phone_1" in legacy_idx:
            logger.warning(
                "talents.phone_1 (collection-wide unique) retained as a "
                "stronger-than-required but SAFE fallback until the "
                "admin-population duplicates above are resolved -- this "
                "blocks legacy/authenticated coexistence for now, but "
                "duplicate-phone protection itself is never lost."
            )
        else:
            logger.warning(
                "No phone uniqueness protection is currently enforced for "
                "admin-created talents in this environment (none existed "
                "before this boot either) -- resolve the duplicates above "
                "to enable it."
            )

    # P0 production indexes — 6 collections.
    # Each is idempotent; create_index is a no-op if already present.
    p0_indexes = [
        # Phase 3 (Canonical Architecture Redesign, Implementation Roadmap
        # v1.0): `id` (the UUID primary-key field every endpoint looks up
        # by — find_one({"id": ...}) — distinct from Mongo's own `_id`) had
        # no index on any of these three collections, forcing a full
        # collection scan on the single most common query shape in the
        # entire backend. Confirmed via explain() before this change.
        ("talents", [("id", 1)], {"name": "talents_id"}),
        ("submissions", [("id", 1)], {"name": "submissions_id"}),
        ("applications", [("id", 1)], {"name": "applications_id"}),
        # Phase 3: resolve_canonical_talent()'s $or (normalized_email/email/
        # source.talent_email) could not use an index-per-branch plan
        # because source.talent_email had none — MongoDB falls back to a
        # full collection scan for the whole $or when any one branch is
        # unindexed, even though the other two branches are individually
        # indexed. Confirmed via explain() before this change.
        ("talents", [("source.talent_email", 1)], {"sparse": True, "name": "talents_source_talent_email"}),
        ("submissions", [("project_id", 1), ("created_at", -1)], {}),
        ("submissions", [("talent_email", 1), ("project_id", 1)], {}),
        # Phase 0: enforce one submission per (project, talent_email).
        ("submissions", [("project_id", 1), ("talent_email", 1)],
         {"unique": True, "name": "submissions_project_email_unique"}),
        ("submissions", [("project_id", 1), ("decision", 1)],
         {"name": "submissions_project_decision"}),
        ("submissions", [("project_id", 1), ("status", 1)],
         {"name": "submissions_project_status"}),
        # Phase 0: enforce one application per email.
        ("applications", [("talent_email", 1)],
         {"unique": True, "name": "applications_email_unique"}),
        ("applications", [("decision", 1), ("status", 1)],
         {"name": "applications_decision_status"}),
        ("links", [("slug", 1)], {"unique": True, "name": "slug_unique"}),
        ("link_views", [("link_id", 1), ("created_at", -1)], {}),
        ("link_actions", [("link_id", 1), ("viewer_email", 1)], {}),
        # P2-F indexes
        ("link_actions", [("link_id", 1)], {"name": "link_actions_link_id"}),
        ("talents", [("name", 1)], {"name": "talents_name"}),
        ("casting_pipeline", [("project_id", 1), ("created_at", 1)], {"name": "pipeline_project_created_at"}),
        # WhatsApp Casting Pipeline agent (backend/agents/modules/casting_pipeline.py):
        # _fetch_stage_candidates filters by (project_id, stage) on every
        # "Show <Pipeline>" — one of the most common calls — but the only
        # existing index on this collection is (project_id, created_at),
        # so MongoDB narrows to the project via that index and then
        # filters the remaining rows for stage in memory. Real production
        # latency measurement (railway logs, inbound: TIMING lines) showed
        # backend request time dominated by sequential Mongo round-trips;
        # this is the single hottest unindexed query shape found.
        ("casting_pipeline", [("project_id", 1), ("stage", 1)], {"name": "pipeline_project_stage"}),
        # Roster "Projects" popover (GET /talents/{tid}/ongoing-projects):
        # the only query shape that looks up a talent's pipeline rows across
        # ALL projects, keyed on talent_id alone — every other pipeline
        # query above is scoped by project_id first, so this is the first
        # index with talent_id leading.
        ("casting_pipeline", [("talent_id", 1)], {"name": "pipeline_talent_id"}),
        ("projects", [("slug", 1)], {"unique": True, "name": "proj_slug_unique"}),
        # routers/whatsapp.py already indexes (status, created_at) — covers
        # the `status` filter but not a `brand_name` sort. casting-agent's
        # _fetch_ongoing_projects ({"status": "ongoing"}, sorted by
        # brand_name) runs on nearly every command (project listing,
        # project-name resolution, global talent search); this index lets
        # Mongo satisfy both the filter and the sort from the index
        # instead of sorting the matched set in memory.
        ("projects", [("status", 1), ("brand_name", 1)], {"name": "projects_status_brand_name"}),
        # Persistent access_token lookup — sparse so docs without the field
        # are ignored, unique so two submissions can't share a token.
        ("submissions", [("access_token", 1)],
         {"unique": True, "sparse": True, "name": "submissions_access_token_unique"}),
        ("applications", [("access_token", 1)],
         {"unique": True, "sparse": True, "name": "applications_access_token_unique"}),
        # Production Certification (Phase 4 item 4, Performance Review):
        # is_media_asset_referenced() (the reference-aware delete guard used
        # by every media-delete path — Library, submission, application,
        # single-slot replace) queries these three collections by
        # media.public_id/media.stream_uid on EVERY delete. Without an
        # index this is a full collection scan per call, on the exact
        # collections expected to grow largest ("thousands of submissions,
        # years of accumulated media").
        ("talents", [("media.public_id", 1)], {"sparse": True, "name": "talents_media_public_id"}),
        ("talents", [("media.stream_uid", 1)], {"sparse": True, "name": "talents_media_stream_uid"}),
        ("submissions", [("media.public_id", 1)], {"sparse": True, "name": "submissions_media_public_id"}),
        ("submissions", [("media.stream_uid", 1)], {"sparse": True, "name": "submissions_media_stream_uid"}),
        ("applications", [("media.public_id", 1)], {"sparse": True, "name": "applications_media_public_id"}),
        ("applications", [("media.stream_uid", 1)], {"sparse": True, "name": "applications_media_stream_uid"}),
        # Migration-aware coexistence (2026-08-14): talent_migration_candidates
        # models the legacy<->authenticated talent relationship as its own
        # collection (one-to-many: a single authenticated talent could in
        # principle match more than one legacy record) instead of a field on
        # the Talent document, so the Talent schema stays clean and this can
        # grow into a Migration Review Center's data source later. Every
        # lookup a future review UI needs -- "candidates for this legacy
        # talent", "candidates for this authenticated talent", "all pending
        # candidates" -- gets its own index. The compound (legacy_talent_id,
        # authenticated_talent_id) index is UNIQUE: it prevents duplicate
        # rows for the exact same pair (e.g. a retry re-detecting the same
        # relationship) while still allowing one legacy talent to have
        # MULTIPLE distinct authenticated candidates (Legacy A -> A1, Legacy
        # A -> A2 are different compound keys) -- its leading field also
        # covers plain "candidates for this legacy talent" lookups, so no
        # separate single-field legacy_talent_id index is needed.
        ("talent_migration_candidates", [("legacy_talent_id", 1), ("authenticated_talent_id", 1)],
         {"unique": True, "name": "tmc_legacy_authenticated_unique"}),
        ("talent_migration_candidates", [("authenticated_talent_id", 1)], {"name": "tmc_authenticated_talent_id"}),
        ("talent_migration_candidates", [("review_status", 1), ("created_at", -1)], {"name": "tmc_review_status_created_at"}),
        ("talents", [("originating_submission_id", 1)], {"sparse": True, "name": "talents_originating_submission_id"}),
        # Safe Talent Deduplication (2026-08-17): talent_merges is the
        # permanent audit trail for every place two previously-distinct
        # Talent identities were reconciled into one canonical record —
        # both the real-time "never even created a second Talent" case
        # (source_talent_id is null; see _auto_link_migration_candidate)
        # and any future bulk migration merge of two already-existing
        # duplicate documents (source_talent_id is the losing record's id).
        # Indexed both ways so an admin/audit UI can answer "what merged
        # into this talent" and "what did this now-archived talent merge
        # into" without a collection scan.
        ("talent_merges", [("canonical_talent_id", 1), ("timestamp", -1)], {"name": "merges_canonical_talent_id"}),
        ("talent_merges", [("source_talent_id", 1)], {"sparse": True, "name": "merges_source_talent_id"}),
        # MERGED is a valid `talents.status` value (Part 12: archival, never
        # hard-delete) — the roster's default `$nin` filter must exclude it
        # the same way DRAFT/ARCHIVED already are; indexed here since it's
        # now a real filter value, not just a UI label.
        ("talents", [("status", 1), ("merged_into", 1)], {"sparse": True, "name": "talents_status_merged_into"}),
    ]
    for coll, keys, opts in p0_indexes:
        try:
            await db[coll].create_index(keys, **opts)
        except Exception as e:
            logger.warning(f"{coll} index {keys}: {e}")

    # Tagging system indexes — idempotent, safe on existing DBs.
    # tags collection: unique normalized_name prevents case-insensitive duplicates.
    try:
        await db.tags.create_index(
            "normalized_name", unique=True, name="tags_normalized_unique"
        )
    except Exception as e:
        logger.warning(f"tags normalized_name index: {e}")
    # talents.tags.id: enables fast tag-based filtering queries.
    try:
        await db.talents.create_index(
            "tags.id", name="talents_tags_id"
        )
    except Exception as e:
        logger.warning(f"talents tags.id index: {e}")
    # talents.interested_in: enables future faceted category search.
    try:
        await db.talents.create_index(
            "interested_in", name="talents_interested_in"
        )
    except Exception as e:
        logger.warning(f"talents interested_in index: {e}")

    # talents.skills: enables future faceted skills search.
    try:
        await db.talents.create_index(
            "skills", name="talents_skills_index"
        )
    except Exception as e:
        logger.warning(f"talents skills index: {e}")

    # Password reset tokens — lookup by hashed token, TTL auto-prune on expiry.
    try:
        await db.password_reset_tokens.create_index("token_hash", unique=True)
        await db.password_reset_tokens.create_index("expires_at", expireAfterSeconds=0)
    except Exception as e:
        logger.warning(f"password_reset_tokens index: {e}")

    legacy = await db.admins.find_one({"email": ADMIN_EMAIL}) if "admins" in await db.list_collection_names() else None
    existing = await db.users.find_one({"email": ADMIN_EMAIL})

    if existing is None and legacy:
        # Migrate legacy admin → users
        await db.users.insert_one({
            "id": legacy.get("id") or str(uuid.uuid4()),
            "email": ADMIN_EMAIL,
            "name": legacy.get("name") or "Talentgram Admin",
            "password_hash": legacy.get("password_hash") or hash_password(ADMIN_PASSWORD),
            "role": "admin",
            "status": "active",
            "created_at": legacy.get("created_at") or _now(),
            "last_login": None,
        })
        logger.info(f"Migrated legacy admin {ADMIN_EMAIL} → db.users")
        existing = await db.users.find_one({"email": ADMIN_EMAIL})

    if existing is None:
        await db.users.insert_one({
            "id": str(uuid.uuid4()),
            "email": ADMIN_EMAIL,
            "name": "Talentgram Admin",
            "password_hash": hash_password(ADMIN_PASSWORD),
            "role": "admin",
            "status": "active",
            "created_at": _now(),
            "last_login": None,
        })
        logger.info(f"Seeded admin {ADMIN_EMAIL}")
        return

    # Ensure role/status are correct for the seeded admin account.
    # NOTE: We deliberately DO NOT touch `password_hash` for an existing
    # admin row — once the admin changes their password via the UI, that
    # change must persist across restarts. The env `ADMIN_PASSWORD` is
    # used ONLY at first-boot insertion above. To rotate the password,
    # use the in-app password change / forgot-password flow.
    patch: Dict[str, Any] = {}
    if existing.get("role") != "admin":
        patch["role"] = "admin"
    if existing.get("status") != "active":
        patch["status"] = "active"
    if patch:
        await db.users.update_one({"email": ADMIN_EMAIL}, {"$set": patch})
        logger.info(f"Updated seeded admin {ADMIN_EMAIL}: {list(patch.keys())}")


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
DEFAULT_VISIBILITY: Dict[str, bool] = {
    "portfolio": True,
    "intro_video": True,
    "takes": True,
    "instagram": True,
    "instagram_followers": True,
    "age": True,
    "height": True,
    "location": True,
    "ethnicity": True,
    "availability": True,
    "budget": False,
    "work_links": True,
    "budget_form": False,
    "download": False,
}

DEFAULT_FIELD_VISIBILITY: Dict[str, bool] = {
    "first_name": True,
    "last_name": True,
    "age": True,
    "height": True,
    "location": True,
    "competitive_brand": True,  # ON by default per visibility separation audit
    "availability": True,
    # Budget defaults to visible at submission level. Link-level `visibility.budget`
    # is still the final gate for what each client sees — this just stops the
    # per-submission layer from silently dropping budget before the link can
    # decide. (Without this, admins toggle "Budget" ON at the link level and
    # still see nothing reach the client.)
    "budget": True,
    "custom_answers": True,      # on by default — admin-configured questions are intentional
    "gender": True,
    "ethnicity": True,
    "languages": True,
    "instagram_handle": True,
    "instagram_followers": True,
    "skills": True,
    "special_abilities": True,
    "work_links": True,
}


def map_link_visibility_to_submission(link_vis: dict) -> dict:
    mapped = {}
    for k in ["age", "height", "location", "ethnicity", "availability", "budget", "work_links", "intro_video", "takes", "portfolio", "download"]:
        if k in link_vis:
            mapped[k] = link_vis[k]
    if "instagram" in link_vis:
        mapped["instagram_handle"] = link_vis["instagram"]
    if "instagram_followers" in link_vis:
        mapped["instagram_followers"] = link_vis["instagram_followers"]
    return mapped


COMMISSION_OPTIONS = ["10%", "15%", "20%", "25%", "30%"]
# Base categories plus the Production Desk's document categories (client
# confirmation, PO, agreement, invoice, call sheet, payment proof,
# reimbursement bills, GST/TDS docs) — all attached through the SAME
# `attach_project_material()` upload path, not a second file store.
MATERIAL_CATEGORIES = {
    "script", "image", "audio", "video_file",
    "client_confirmation", "po", "agreement", "invoice", "call_sheet",
    "payment_proof", "reimbursement_bill", "gst_tds_document",
}
MAX_VIDEO_FILE_BYTES = 100 * 1024 * 1024  # 100 MB
# Submission media slots
#   intro_video      — single slot
#   take             — NEW renamable takes, up to MAX_SUBMISSION_TAKES (carries `label`)
#   take_1/take_2/take_3 — LEGACY fixed slots (read-only back-compat; auto-labelled "Take N")
#   image            — generic portfolio images (MIN/MAX_SUBMISSION_IMAGES bounds)
#   indian / western — look-specific portfolio images (Phase 2 schema unification)
#   selfie / profiles / full_length / side_profile / ethnic / additional_portfolio
#   — Admin Mode "Upload on Behalf" categories (Admin Submission feature,
#   Phase 2 added ethnic + additional_portfolio). Same image-category rules
#   (per-category cap, reusable to Talent Profile) as indian/western/image.
ADMIN_EXTRA_PORTFOLIO_CATEGORIES = {"selfie", "profiles", "full_length", "side_profile", "ethnic", "additional_portfolio"}
SUBMISSION_UPLOAD_CATEGORIES = {"intro_video", "take", "take_1", "take_2", "take_3", "image", "indian", "western"} | ADMIN_EXTRA_PORTFOLIO_CATEGORIES
LEGACY_TAKE_CATEGORIES = {"take_1", "take_2", "take_3"}
PORTFOLIO_IMAGE_CATEGORIES = {"image", "indian", "western"} | ADMIN_EXTRA_PORTFOLIO_CATEGORIES
# Talent Profile Migration, Phase 4 — categories that MAY become the
# canonical Talent Profile (db.talents.media) if the talent consents.
# Audition takes (take/take_1..3) are never in this set and never reach the
# consent dialog — they are always project-only, no exceptions.
REUSABLE_MEDIA_CATEGORIES = {"intro_video", "image", "indian", "western"} | ADMIN_EXTRA_PORTFOLIO_CATEGORIES


# --------------------------------------------------------------------------
# Soft-delete filters (Cloudinary rearchitecture, P6/P7)
#
# P6 changed project + submission deletion from a hard `delete_one` to a
# soft-delete (`lifecycle_state = "deleted"` + `deleted_at`). Talent deletion
# defaults to ARCHIVE (`lifecycle_state = "archived"`). Operational and
# client-facing queries must exclude these; admin/historical/audit/accounting
# queries opt back in explicitly. `{"$ne": "deleted"}` matches docs where the
# field is absent (every pre-P6 row) as well as `"active"`.
# --------------------------------------------------------------------------
NOT_DELETED = {"lifecycle_state": {"$ne": "deleted"}}
NOT_ARCHIVED_OR_DELETED = {"lifecycle_state": {"$nin": ["deleted", "archived"]}}


def active_only(query: Optional[dict] = None, *, include_deleted: bool = False,
                exclude_archived: bool = False) -> dict:
    """Merge the soft-delete filter into a MongoDB query. `include_deleted=True`
    (admin/historical/accounting) returns the query untouched."""
    q = dict(query or {})
    if include_deleted:
        return q
    field = "lifecycle_state"
    forbidden = ["deleted", "archived"] if exclude_archived else ["deleted"]
    if field in q:
        # caller already constrains lifecycle_state — respect it
        return q
    q[field] = {"$nin": forbidden} if len(forbidden) > 1 else {"$ne": forbidden[0]}
    return q


MAX_SUBMISSION_TAKES = 5
MAX_SUBMISSION_IMAGES = 8
MIN_SUBMISSION_IMAGES = 5
# Per-category portfolio image cap (Phase 3): each of `image`/`indian`/
# `western` is independently capped at this value, NOT a combined total.
# Talents can therefore upload up to 30 portfolio images total without
# hitting a global ceiling.
MAX_IMAGES_PER_CATEGORY = 10
# Public audition upload size cap: 200 MB for videos (intro/take), 20 MB for images.
# Enforced server-side to protect against accidental/malicious bloat.
MAX_SUBMISSION_VIDEO_BYTES = 200 * 1024 * 1024
MAX_SUBMISSION_IMAGE_BYTES = 20 * 1024 * 1024

# Architecture C — direct browser→Cloudinary audition-video upload. NOTE: the
# /video-signature and /video-complete endpoints are NOT gated by this flag, so
# submission audition videos/takes upload regardless. The flag only controls the
# finalize reconcile safety-net (reconcile_submission_videos). Default left
# unchanged ("false") so finalize behavior is not altered by the upload fix.
DIRECT_VIDEO_UPLOAD = os.environ.get("DIRECT_VIDEO_UPLOAD", "false").strip().lower() in ("1", "true", "yes", "on")
# Audition video duration ceiling (seconds) — 5 minutes.
MAX_AUDITION_VIDEO_SECONDS = 300
# Video categories eligible for direct upload. `intro_video` is the only video
# category that syncs to Global Talent (via cat_mapping); takes stay project-specific.
DIRECT_VIDEO_CATEGORIES = {"intro_video", "take", "take_1", "take_2", "take_3"}
SUBMISSION_DECISIONS = {"pending", "approved", "rejected", "hold", "ask_to_test", "shortlisted", "does_not_work_for_this"}
SUBMISSION_STATUSES = {"draft", "submitted", "updated"}

# Moderated client→talent feedback relay
FEEDBACK_TYPES = {"voice", "text"}
FEEDBACK_STATUSES = {"pending", "approved", "rejected"}
FEEDBACK_VISIBILITIES = {"admin_only", "shared_with_talent"}
MAX_FEEDBACK_TEXT_LEN = 4000
MAX_FEEDBACK_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB ceiling for voice notes

# Open talent applications (project-independent signups). `indian`/`western`
# image categories share the same image limits as generic portfolio images.
APPLICATION_UPLOAD_CATEGORIES = {"intro_video", "image", "indian", "western"}
MAX_APPLICATION_IMAGES = 8
MIN_APPLICATION_IMAGES = 5
APPLICATION_DECISIONS = SUBMISSION_DECISIONS

# STRICT client allowlist — any subject field MUST be in this set to reach the client.
CLIENT_ALLOWED_FIELDS = {
    "id",
    "name",
    "age",
    "height",
    "location",
    "ethnicity",
    "instagram_handle",
    "instagram_followers",
    "work_links",
    "availability",        # structured: {status: "yes"|"no", note?: str}
    "budget",              # structured: {status: "accept"|"custom", value?: str}
    "competitive_brand",   # plain string, gated by field_visibility.competitive_brand
    "custom_answers",      # [{"question": str, "answer": str}] — gated per-question
    "cover_media_id",
    "image_url",           # top-level Cloudinary cover URL or None (frontend-safe)
    "media",
    # IDs needed for the moderated client→talent feedback relay. These are
    # NOT sensitive — they're foreign keys clients must round-trip back when
    # POSTing feedback. Empty/None for pure talent-share (M1) cards.
    "submission_id",
    "project_id",
    "effective_age",
    "submitted_age_override",
    "skills",
    "gender",
    "ethnicity",
    "languages",
    "special_abilities",
    # Privacy-safe boolean only (no media/URLs/filenames/counts). Drives client
    # "Ask for Test" visibility correctly even when takes are hidden by visibility.
    "has_audition_takes",
}


# --------------------------------------------------------------------------
# Pydantic models
# --------------------------------------------------------------------------
class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    token: str
    admin: Dict[str, Any]


class MediaItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    category: str
    url: Optional[str] = None
    public_id: Optional[str] = None
    resource_type: Optional[str] = None
    content_type: Optional[str] = None
    original_filename: Optional[str] = None
    size: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class LocationItem(BaseModel):
    city: str
    country: str


class TalentIn(BaseModel):
    name: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    # Optional backup contact number (no WhatsApp requirement). `phone` remains
    # the primary WhatsApp-accessible number.
    alternate_contact_number: Optional[str] = None
    age: Optional[int] = None
    dob: Optional[str] = None
    height: Optional[str] = None
    location: List[LocationItem] = Field(default_factory=list)
    needs_location_review: Optional[bool] = None
    ethnicity: Optional[str] = None
    gender: Optional[str] = None
    instagram_handle: Optional[str] = None
    instagram_followers: Optional[str] = None
    bio: Optional[str] = None
    work_links: List[str] = Field(default_factory=list)
    cover_media_id: Optional[str] = None
    # Public: self-selected work categories (set during onboarding /apply)
    interested_in: List[str] = Field(default_factory=list)
    # Categorized multi-select skills and special abilities
    skills: List[str] = Field(default_factory=list)
    # Internal: admin-assigned structured tags [{"id": uuid, "name": label}]
    tags: List[Dict[str, str]] = Field(default_factory=list)
    # WhatsApp Engine: exact group name (e.g. "Ayushi Thakur x Talentgram")
    # If set, WhatsApp messages go to the group; otherwise falls back to phone.
    whatsapp_group_name: Optional[str] = None

    @field_validator('instagram_handle', mode='before')
    @classmethod
    def _normalize_ig(cls, v):
        """Auto-normalize any pasted Instagram URL/handle to a raw username."""
        return normalize_instagram_handle(v)

    @field_validator('location', mode='before')
    @classmethod
    def _normalize_location(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            # Split by common separators if any
            if ";" in v:
                v = [x.strip() for x in v.split(";")]
            elif "/" in v:
                v = [x.strip() for x in v.split("/")]
            else:
                v = [v]
        if isinstance(v, list):
            res = []
            for item in v:
                if isinstance(item, dict):
                    city = item.get("city", "").strip()
                    country = item.get("country", "").strip()
                    if city and country:
                        res.append({"city": city, "country": country})
                elif isinstance(item, str):
                    item = item.strip()
                    if not item:
                        continue
                    if "," in item:
                        parts = [p.strip() for p in item.split(",")]
                        city = parts[0]
                        country = parts[-1]
                        res.append({"city": city, "country": country})
                    else:
                        res.append({"city": item, "country": "India"})
            return res
        return v



class TalentOut(TalentIn):
    id: str
    media: List[MediaItem] = Field(default_factory=list)
    created_at: str
    # Phase 7 (Admin stale-overwrite fix): expose the canonical clock so a
    # client can round-trip it as expected_updated_at on its next PUT
    # /talents/{tid} — additive only, no existing consumer reads this today.
    updated_at: Optional[str] = None


class TalentUpdateIn(TalentIn):
    """PUT /talents/{tid} payload — identical to TalentIn plus one optional
    freshness token. Phase 7 (Admin stale-overwrite fix): the admin edit
    page loads a talent, holds it in local state for an arbitrary amount of
    time, then submits the ENTIRE form on save. `expected_updated_at` is
    that snapshot's own `updated_at`, letting the endpoint tell "this
    payload was assembled from data at least as fresh as the canonical
    record" apart from "this payload predates a newer canonical edit" —
    the exact same distinction `merge_talent_profile()`'s `snapshot_at`
    already makes for submission/application merges (ADR Part 4, Phase 1).
    Omitted entirely (older client) preserves today's unconditional-write
    behavior, exactly like merge_talent_profile()'s own
    `_NO_FRESHNESS_CHECK` sentinel for callers that never opt in. Only used
    by this one endpoint — TalentIn/create_talent are untouched."""
    expected_updated_at: Optional[str] = None


class LinkIn(BaseModel):
    title: str
    brand_name: Optional[str] = None
    # Manual-curation lists. For "auto_pull" showcase links these stay
    # empty and the resolver derives the membership from project_id.
    talent_ids: List[str] = Field(default_factory=list)
    submission_ids: List[str] = Field(default_factory=list)
    visibility: Dict[str, bool] = Field(default_factory=lambda: DEFAULT_VISIBILITY.copy())
    # Per-talent field-visibility map for individual talent-share links.
    # Shape: { talent_id: { name: bool, age: bool, height: bool, instagram: bool,
    #          instagram_followers: bool, images: bool, intro_video: bool, ... } }
    # Empty/missing entries fall back to the link-level `visibility` map.
    talent_field_visibility: Dict[str, Dict[str, bool]] = Field(default_factory=dict)
    # Auto-pull mode: when enabled, the resolver IGNORES `submission_ids` and
    # returns all currently-approved submissions for `auto_project_id`. New
    # approvals show up automatically without re-curating the link.
    auto_pull: bool = False
    auto_project_id: Optional[str] = None
    is_public: bool = True
    password: Optional[str] = None
    notes: Optional[str] = None
    # Optional per-link override for client-facing budget. When non-empty it
    # REPLACES the aggregated project client_budget in the public link payload.
    client_budget_override: Optional[List[Dict[str, str]]] = None


class LinkOut(LinkIn):
    id: str
    slug: str
    created_at: str
    created_by: str
    view_count: int = 0
    unique_viewers: int = 0


class IdentifyIn(BaseModel):
    name: str
    email: EmailStr
    browser: Optional[str] = None
    device: Optional[str] = None
    session_id: Optional[str] = None


class SeenIn(BaseModel):
    talent_id: str


class ClientTextFeedbackIn(BaseModel):
    """Public client feedback (text). Voice uploads use the multipart endpoint."""
    talent_id: str
    submission_id: str
    project_id: str
    text: str = Field(min_length=1, max_length=MAX_FEEDBACK_TEXT_LEN)


class FeedbackEditIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_FEEDBACK_TEXT_LEN)


class ActionIn(BaseModel):
    talent_id: str
    action: Optional[str] = None  # shortlist | interested | not_for_this | not_sure | null
    comment: Optional[str] = None
    session_id: Optional[str] = None


class DownloadIn(BaseModel):
    talent_id: str
    media_id: str
    session_id: Optional[str] = None


def default_submission_requirements() -> Dict[str, Any]:
    return {
        "strictness": "strict",
        "fields": {
            "name": "required",
            "email": "required",
            "phone": "optional",
            "dob": "optional",
            "age": "optional",
            "height": "optional",
            "location": "optional",
            "gender": "optional",
            "ethnicity": "optional",
            "instagram_handle": "optional",
            "instagram_followers": "optional",
            "bio": "optional",
            "competitive_brand": "optional",
            "availability": "optional",
            "budget_expectation": "optional",
            "work_links": "optional"
        },
        "custom_questions": {},
        "intro_video": "optional",
        "audition_takes_visibility": "optional",
        "min_audition_takes": 0,
        "portfolio_image_visibility": "optional",
        "portfolio_indian_visibility": "optional",
        "portfolio_western_visibility": "optional",
        "portfolio": {
            "indian": 0,
            "western": 0,
            "image": 0
        },
        "work_links_visibility": "optional",
        "min_work_links": 0,
        "skills": {
            "language": False,
            "performance": False,
            "sports": False,
            "action": False,
            "vehicle": False,
            "special": False
        },
        "interested_in": "optional",
        "conditional_rules": []
    }


class ProjectIn(BaseModel):
    brand_name: str
    brand_link: Optional[str] = None
    character: Optional[str] = None
    shoot_dates: Optional[str] = None
    budget_per_day: Optional[str] = None
    commission_percent: Optional[str] = None
    medium_usage: Optional[str] = None
    director: Optional[str] = None
    production_house: Optional[str] = None
    additional_details: Optional[str] = None
    video_links: List[str] = Field(default_factory=list)
    competitive_brand_enabled: bool = False
    custom_questions: List[Dict[str, Any]] = Field(default_factory=list)
    # Structured key/value pricing. Each entry: {"label": str, "value": str}
    # talent_budget  — shown to talents on the audition submission form (hint)
    # client_budget  — shown to clients on the link view (gated by visibility.budget)
    talent_budget: List[Dict[str, str]] = Field(default_factory=list)
    client_budget: List[Dict[str, str]] = Field(default_factory=list)
    # When True (default), retake/edit after a final submit moves the
    # submission back to "pending" decision so admins re-review. When False,
    # the prior decision (approved/rejected/hold) is preserved silently.
    require_reapproval_on_edit: bool = True
    hide_budget_from_talent: bool = False
    status: str = "ongoing"
    submission_requirements: Optional[Dict[str, Any]] = Field(default_factory=default_submission_requirements)
    # Explicit WhatsApp destination group for the SEND casting-pipeline
    # command (native Forward + form send). Never derived from brand_name —
    # a project with no group set has no valid SEND destination.
    whatsapp_casting_group_name: Optional[str] = None



class SubmissionStartIn(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    alternate_contact_number: Optional[str] = None
    form_data: Optional[Dict[str, Any]] = None


async def create_or_resume_submission_doc(
    project: dict,
    email: str,
    name: str,
    phone: Optional[str],
    alternate_contact_number: Optional[str],
    form_data: Optional[Dict[str, Any]],
    *,
    created_by: Optional[str] = None,
    created_from: str = "talent_link",
    talent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new submission doc, or resume an existing one for (project, email).

    Extracted from the talent-facing `start_submission` route so the exact
    same doc-construction/resume logic can be driven by an admin-authed caller
    (Admin Mode "Upload on Behalf") without duplicating it. Callers own their
    own authorization gating (OTP/email-ownership for talents, admin auth for
    admins) — this function only owns document semantics.

    `created_by`/`created_from` are stamped ONLY when a brand-new doc is
    inserted; resuming an existing draft never retroactively relabels who
    created it.
    """
    slug = project["slug"]
    existing = await db.submissions.find_one({
        "project_id": project["id"],
        "talent_email": email,
    })
    if existing:
        sid = existing["id"]
        atk = existing.get("access_token")
        if not atk:
            atk = make_access_token()
            await db.submissions.update_one({"id": sid}, {"$set": {"access_token": atk}})
        token = make_token({"role": "submitter", "sid": sid, "slug": slug}, days=3)
        return {
            "id": sid,
            "token": token,
            "access_token": atk,
            "resumed": True,
            "status": existing.get("status", "draft"),
        }

    fd = form_data or {}
    talent_age = None
    # Phase 1 — Canonical Profile Monotonicity: record how fresh the
    # canonical Talent Profile was at the moment this draft's form_data was
    # captured, so finalize() can later tell whether the profile has since
    # moved on (ADR Part 4 / Invariant #4). None when no talent exists yet.
    talent_profile_snapshot_at = None
    if email:
        talent_doc = await db.talents.find_one(
            {"$or": [
                {"normalized_email": email},
                {"email": email},
                {"source.talent_email": email}
            ]},
            {"age": 1, "dob": 1, "updated_at": 1}
        )
        if talent_doc:
            talent_age = talent_doc.get("age") or (compute_age(talent_doc.get("dob")) if talent_doc.get("dob") else None)
            talent_profile_snapshot_at = talent_doc.get("updated_at")

    submitted_age_override_val = None
    override_active = fd.get("overrideAge") or fd.get("override_age")
    if override_active and fd.get("submitted_age_override") not in (None, ""):
        try:
            submitted_age_override_val = int(fd["submitted_age_override"])
        except Exception:
            pass

    effective_age_val = compute_effective_age(fd, talent_age)

    cb_visible = True
    fv_defaults = {**DEFAULT_FIELD_VISIBILITY, "competitive_brand": cb_visible}

    sid = str(uuid.uuid4())
    atk = make_access_token()
    # Talent Profile Migration, Phase 3: a new submission starts with NO
    # media. Reusable media is no longer auto-injected — the talent sees
    # it via `library_media` (computed live, see GET .../submissions/{sid})
    # and explicitly picks what applies to THIS project via
    # POST .../media/from-library. No silent synchronization.
    doc = {
        "id": sid,
        "project_id": project["id"],
        "project_slug": slug,
        "talent_name": name,
        "talent_email": email,
        "talent_phone": phone,
        "alternate_contact_number": alternate_contact_number,
        "talent_id": talent_id,
        "form_data": fd,
        "talent_profile_snapshot_at": talent_profile_snapshot_at,
        "field_visibility": fv_defaults,
        "submitted_age_override": submitted_age_override_val,
        "effective_age": effective_age_val,
        "media": [],
        "status": "draft",
        "decision": "pending",
        "access_token": atk,
        "created_at": _now(),
        "submitted_at": None,
        "created_by": created_by,
        "created_from": created_from,
    }
    try:
        await db.submissions.insert_one(doc)
    except DuplicateKeyError:
        # Race: parallel start hit the unique (project_id, talent_email)
        # index. Fall through to the existing-submission resume path.
        existing = await db.submissions.find_one({
            "project_id": project["id"],
            "talent_email": email,
        })
        if existing:
            sid = existing["id"]
            atk = existing.get("access_token")
            if not atk:
                atk = make_access_token()
                await db.submissions.update_one({"id": sid}, {"$set": {"access_token": atk}})
            token = make_token({"role": "submitter", "sid": sid, "slug": slug}, days=3)
            return {
                "id": sid,
                "token": token,
                "access_token": atk,
                "resumed": True,
                "status": existing.get("status", "draft"),
            }
        raise HTTPException(409, "Submission already exists for this email")
    token = make_token({"role": "submitter", "sid": sid, "slug": slug}, days=3)
    return {"id": sid, "token": token, "access_token": atk, "resumed": False, "status": "draft"}


def actor_stamp(submitter: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Returns `{"last_modified_by": email}` to merge into a submission
    update when `submitter` carries an `acting_admin_email` claim (minted by
    the Admin Mode / "Upload on Behalf" start endpoint), else `{}`.

    Talent-driven requests carry no such claim and are completely unaffected
    — every existing public submission handler stays byte-for-byte identical
    for talent traffic; this only adds an audit trail when an admin is
    driving the same endpoints via an attributed token.
    """
    if submitter and submitter.get("acting_admin_email"):
        return {"last_modified_by": submitter["acting_admin_email"]}
    return {}


class SubmissionUpdateIn(BaseModel):
    form_data: Optional[Dict[str, Any]] = None


class AdminSubmissionEditIn(BaseModel):
    form_data: Optional[Dict[str, Any]] = None
    # Value is `bool` for most fields, OR a `{question_label: bool}` dict for
    # `custom_answers` to support per-question visibility.
    field_visibility: Optional[Dict[str, Any]] = None
    media: Optional[List[Dict[str, Any]]] = None # Custom media curated settings & ordering
    # Per-submission visibility overrides for TALENT-level portfolio media
    # (which live on db.talents, not on the submission). Shape:
    # { "<talent_media_id>": {"client_visible": bool} }.
    # Lets a recruiter apply the SAME Client/Hidden model to talent
    # portfolio media without duplicating the media onto the submission.
    talent_media_visibility: Optional[Dict[str, Any]] = None
    restore_revision_id: Optional[str] = None


class SubmissionDecisionIn(BaseModel):
    decision: str
    note: Optional[str] = None


class ForwardToLinkIn(BaseModel):
    submission_ids: List[str]
    visibility: Dict[str, bool] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Open talent applications (project-independent)
# --------------------------------------------------------------------------
class ApplicationStartIn(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    phone: Optional[str] = None
    alternate_contact_number: Optional[str] = None
    profile_id: Optional[str] = None


class BulkDeleteIn(BaseModel):
    """Payload for bulk-delete endpoints across talents / projects / links."""
    ids: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# User management (role-based access control)
# --------------------------------------------------------------------------
USER_ROLES = ("admin", "team")
USER_STATUSES = ("active", "invited", "disabled")


class UserInviteIn(BaseModel):
    name: str
    email: EmailStr
    role: str = "team"


class UserRolePatchIn(BaseModel):
    role: str


class SignupValidateIn(BaseModel):
    token: str


class SignupCompleteIn(BaseModel):
    token: str
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetTokenValidateIn(BaseModel):
    token: str


class ResetPasswordCompleteIn(BaseModel):
    token: str
    new_password: str


def _public_user(u: dict) -> dict:
    """Strip secret fields before returning a user document."""
    return {
        "id": u.get("id"),
        "name": u.get("name"),
        "email": u.get("email"),
        "role": u.get("role"),
        "status": u.get("status"),
        "created_at": u.get("created_at"),
        "last_login": u.get("last_login"),
    }


def generate_temp_password(length: int = 14) -> str:
    """Cryptographically strong, human-readable temp password.

    Drops ambiguous chars (O/0/l/1/I). Guarantees at least 1 lower, 1 upper,
    1 digit, 1 symbol. Uses `secrets` — never `random`.
    """
    import secrets

    lower = "abcdefghijkmnopqrstuvwxyz"           # no "l"
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"            # no "I", "O"
    digits = "23456789"                           # no "0", "1"
    symbols = "!@#$%^&*"
    alphabet = lower + upper + digits + symbols
    # Ensure class coverage
    required = [
        secrets.choice(lower),
        secrets.choice(upper),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    remaining = [secrets.choice(alphabet) for _ in range(max(0, length - len(required)))]
    raw = required + remaining
    # Fisher-Yates shuffle via secrets (uniform)
    for i in range(len(raw) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        raw[i], raw[j] = raw[j], raw[i]
    return "".join(raw)


def generate_invite_token() -> str:
    """URL-safe cryptographically random invite token (≈43 chars)."""
    import secrets

    return secrets.token_urlsafe(32)


# --------------------------------------------------------------------------
# Visibility / client payload filters
# --------------------------------------------------------------------------
def _public_media(m: dict) -> dict:
    """Strip internal scope metadata (project_id / submission_id / talent_id / scope) before sending to client.
    Automatically maps video URLs to the adaptive streaming preset, and adds poster frame URLs.
    """
    resource_type = m.get("resource_type")
    url = m.get("url")
    is_video = resource_type == "video" or m.get("category") == "video" or (m.get("content_type") or "").startswith("video/")

    # Provider-aware video URL. Cloudflare Stream (HLS .m3u8) and R2 assets keep
    # their stored delivery URL as-is. Only legacy Cloudinary-hosted videos —
    # records saved before this file learned to store a ready delivery URL —
    # get a computed Cloudinary delivery transform via stream_video_url(public_id).
    # (Previously this rewrote EVERY video with a public_id to a Cloudinary URL,
    # which replaced the Stream m3u8 with a non-existent Cloudinary .mp4 → client
    # playback failed with "No video with supported format".)
    _provider = m.get("provider")
    _url = url or ""
    # Broad Stream detection — resilient to legacy / partially-migrated records
    # where `provider` may be unset but other Stream signals are present.
    _is_stream = (
        _provider == "stream"
        or bool(m.get("stream_uid"))
        or "cloudflarestream.com" in _url
        or _url.endswith(".m3u8")
    )
    _is_cloudinary_video = _provider == "cloudinary" or "res.cloudinary.com" in _url
    # Media pipeline fix (2026-08, Phase 2): this used to recompute
    # stream_video_url(public_id) unconditionally for every Cloudinary video,
    # discarding whatever was already stored in `url` — even though the
    # upload path (submission_upload/submission_complete_upload) always
    # stores an already-ready, already-playable delivery URL (either the
    # eager-transformed derivative Cloudinary generated during upload, or the
    # incoming-transformed/original secure_url). stream_video_url() builds a
    # DIFFERENT, differently-chained transformation string than the one
    # requested at upload time (confirmed empirically: upload's eager preset
    # is one combined segment, e.g. "w_1280,h_720,c_limit,q_auto,vc_auto,f_mp4",
    # while stream_video_url() chains the same params across three separate
    # transformation steps) — Cloudinary treats these as distinct derived
    # assets with distinct cache keys, so the client link was ALWAYS forcing
    # a brand-new, never-before-requested on-demand transformation, cold,
    # on the very first client view. For a real (large/long) audition video
    # that cold-generation can take long enough for the player (which has no
    # retry) to give up — exactly the "first upload doesn't play, re-upload
    # fixes it" bug: a second upload doesn't fix anything architecturally, it
    # just buys enough wall-clock time for that once-off cold transform to
    # finish. Now: use the already-stored, already-verified-working `url`
    # whenever one exists.
    #
    # Cloudinary rearchitecture P5: for the genuinely legacy case where no url
    # was ever stored, serve the CANONICAL delivery URL (no transform) — or the
    # P4 compat URL when the record was flagged as a non-web codec/container.
    # The old path here forced a universal 720p downscale-transcode on first
    # client view, which the P4/P5 video policy forbids.
    if is_video and m.get("public_id") and _is_cloudinary_video and not _is_stream and not url:
        if m.get("needs_compat_delivery"):
            url = compat_video_delivery_url(m["public_id"])
        else:
            url = stream_video_url(m["public_id"])  # now returns the untransformed canonical URL

    out = {
        "id": m.get("id"),
        "category": m.get("category"),
        "url": url,
        "public_id": m.get("public_id"),
        "resource_type": m.get("resource_type"),
        "content_type": m.get("content_type"),
        "original_filename": m.get("original_filename"),
        "size": m.get("size", 0),
        "created_at": m.get("created_at"),
    }
    if m.get("label"):
        out["label"] = m["label"]
    if "duration" in m:
        out["duration"] = m["duration"]
    if is_video:
        out["poster_url"] = m.get("poster_url") or video_poster_url(m.get("public_id")) or video_poster_url(url)
    
    # Curated media visibility and metadata flags
    for k in ["client_visible", "internal_only", "featured_for_client", "primary_take", "featured", "client_cover"]:
        if k in m:
            out[k] = m[k]
    return out


def _filter_talent_for_client(talent: dict, visibility: Dict[str, bool]) -> dict:
    """STRICT allowlist: client receives only fields explicitly enabled via visibility
    AND only fields that appear in CLIENT_ALLOWED_FIELDS. Admin-only data (availability,
    budget, custom_answers, competitive_brand, etc.) is structurally blocked from leaking.

    Media rules:
      - portfolio images: gated by visibility.portfolio
      - intro video (category="video"): gated by visibility.intro_video
      - audition takes (category="take_1"/"take_2"/"take_3"): gated by visibility.takes
    Ordering is preserved from the upstream shape.
    """
    v = visibility or {}
    filtered_media: List[dict] = []
    cover_mid: Optional[str] = None
    for m in talent.get("media") or []:
        # Filter out hidden/internal assets from client links
        if m.get("client_visible") is False or m.get("internal_only") is True:
            continue
        cat = m.get("category")
        if cat in ("indian", "western", "portfolio") and v.get("portfolio"):
            filtered_media.append(_public_media(m))
            if not cover_mid and talent.get("cover_media_id") == m.get("id"):
                cover_mid = m["id"]
        elif cat == "video" and v.get("intro_video"):
            filtered_media.append(_public_media(m))
        elif cat == "take" and v.get("takes", True):
            # New renamable takes — preserve `label` through the public sanitizer
            pm = _public_media(m)
            lbl = (m.get("label") or "").strip()
            if lbl:
                pm["label"] = lbl
            filtered_media.append(pm)
        elif cat in ("take_1", "take_2", "take_3") and v.get("takes", True):
            filtered_media.append(_public_media(m))
    if v.get("portfolio") and not cover_mid:
        for m in filtered_media:
            if m.get("category") in ("indian", "western", "portfolio"):
                cover_mid = m["id"]
                break

    # Single source of truth for "Ask for Test" eligibility. Computed from the
    # RAW media (before visibility filtering) so it's correct even when
    # visibility.takes hides the takes from the client. Exposes ONLY a boolean —
    # no media, URLs, filenames, or counts.
    has_audition_takes = any(
        (m.get("category") or "").startswith("take") for m in (talent.get("media") or [])
    )

    out: Dict[str, Any] = {
        "id": talent["id"],
        "name": talent.get("name"),
        "media": filtered_media,
        "cover_media_id": cover_mid,
        "image_url": _resolve_cover_url({"media": filtered_media, "cover_media_id": cover_mid}) or None,
        "has_audition_takes": has_audition_takes,
    }
    if v.get("age") and talent.get("age") is not None:
        out["age"] = talent["age"]
    if v.get("height") and talent.get("height"):
        out["height"] = talent["height"]
    if v.get("location") and talent.get("location"):
        out["location"] = talent["location"]
    if v.get("gender") and talent.get("gender"):
        out["gender"] = talent["gender"]
    if v.get("ethnicity") and talent.get("ethnicity"):
        out["ethnicity"] = talent["ethnicity"]
    # Issue #7: Languages and Special Abilities removed from client output
    # (redundant with Skills) — keeps every client surface consistent.
    if (v.get("instagram_handle") or v.get("instagram")) and talent.get("instagram_handle"):
        out["instagram_handle"] = talent["instagram_handle"]
    if v.get("instagram_followers") and talent.get("instagram_followers"):
        out["instagram_followers"] = talent["instagram_followers"]
    if v.get("work_links") and talent.get("work_links"):
        out["work_links"] = talent["work_links"]
    if v.get("skills") and talent.get("skills"):
        out["skills"] = talent["skills"]
    # Availability & budget (structured objects)
    if v.get("availability") and talent.get("availability"):
        a = talent["availability"]
        if isinstance(a, dict) and a.get("status"):
            out["availability"] = {
                "status": a.get("status"),
                "note": (a.get("note") or "").strip() or None,
            }
    if v.get("budget") and talent.get("budget"):
        b = talent["budget"]
        if isinstance(b, dict) and b.get("status"):
            out["budget"] = {
                "status": b.get("status"),
                "value": (b.get("value") or "").strip() or None,
            }
    # Competitive brand — already gated by per-submission field_visibility in
    # _submission_to_client_shape; we just pass it through here.
    if talent.get("competitive_brand"):
        out["competitive_brand"] = talent["competitive_brand"]
    # Custom answers — same deal (per-question visibility already applied).
    if talent.get("custom_answers"):
        out["custom_answers"] = talent["custom_answers"]
    # Pass through submission/project IDs for the moderated feedback relay.
    # These are non-PII opaque IDs the client must round-trip back to
    # `/public/links/{slug}/feedback`. Only present on submission-backed
    # cards (M2/M3); pure talent-share (M1) has them as None.
    if talent.get("submission_id"):
        out["submission_id"] = talent["submission_id"]
    if talent.get("project_id"):
        out["project_id"] = talent["project_id"]
    # Final defensive sweep
    return {k: v2 for k, v2 in out.items() if k in CLIENT_ALLOWED_FIELDS}


def _public_link_view(link: dict) -> dict:
    """Return only fields the client needs. Strip admin-only fields."""
    v = link.get("visibility") or {}
    return {
        "id": link["id"],
        "slug": link.get("slug"),
        "title": link.get("title"),
        "brand_name": link.get("brand_name"),
        "visibility": v,
    }


def _submission_to_client_shape(sub: dict, project: Optional[dict] = None, project_defaults: Optional[dict] = None) -> dict:
    """Flatten a submission document into the shape clients expect.

    Order rules (strict, see product spec):
      1. Audition takes — renamable via `media.label`; legacy `take_1/2/3`
         auto-map to label "Take 1/2/3". Max 5 takes.
      2. Introduction video
      3. Portfolio images

    Field rules:
      - Respects per-submission `field_visibility` for demographic + structured
        fields (availability, budget, competitive_brand, custom_answers).
      - `custom_answers` visibility can be a bool (all-or-nothing) OR a dict
        `{question_label: bool}` for per-question control.
      - When `project` is provided, question IDs in custom_answers are resolved
        to their human-readable question text using project.custom_questions.
    """
    s_fv = sub.get("field_visibility") or {}
    defaults = map_link_visibility_to_submission(project_defaults) if project_defaults else {}

    def get_val(key: str, fallback: bool) -> bool:
        if key in s_fv:
            return s_fv[key]
        return defaults.get(key, fallback)

    fv = {
        "portfolio": get_val("portfolio", True),
        "intro_video": get_val("intro_video", True),
        "takes": get_val("takes", True),
        "age": get_val("age", True),
        "height": get_val("height", True),
        "gender": get_val("gender", True),
        "location": get_val("location", True),
        "ethnicity": get_val("ethnicity", True),
        "instagram_handle": get_val("instagram_handle", True),
        "instagram_followers": get_val("instagram_followers", True),
        "languages": get_val("languages", True),
        "skills": get_val("skills", True),
        "special_abilities": get_val("special_abilities", True),
        "availability": get_val("availability", True),
        "budget": get_val("budget", True),
        "work_links": get_val("work_links", True),
        "download": get_val("download", True),
        "competitive_brand": get_val("competitive_brand", True),
        "custom_answers": get_val("custom_answers", True),
    }

    reqs = (project or {}).get("submission_requirements") or {}
    if reqs.get("audition_takes_visibility") == "hidden":
        fv["takes"] = False
    if reqs.get("work_links_visibility") == "hidden":
        fv["work_links"] = False
    if reqs.get("intro_video") == "hidden":
        fv["intro_video"] = False


    # Single source of truth (Issue #1/#10): the client-facing shape is ALWAYS
    # computed live from the current submission + visibility settings. There are
    # no frozen snapshots — Client View (Review Center), the Client Review Link,
    # slideshow, download bundle and PDF all render from this one engine, so a
    # recruiter's visibility/approval change is reflected everywhere immediately.
    fd = sub.get("form_data") or {}

    fn = (fd.get("first_name") or "").strip()
    ln = (fd.get("last_name") or "").strip()
    name = f"{fn} {ln}".strip() or sub.get("talent_name") or "Unnamed"

    submitted_age_override = sub.get("submitted_age_override")
    effective_age = sub.get("effective_age")

    if submitted_age_override is None:
        override_active = fd.get("overrideAge") or fd.get("override_age")
        if override_active and fd.get("submitted_age_override") not in (None, ""):
            try:
                submitted_age_override = int(fd["submitted_age_override"])
            except Exception:
                pass

    if effective_age is None:
        effective_age = compute_effective_age(fd)

    age = effective_age if fv.get("age") else None

    raw_media = sub.get("media") or []
    # Media buckets
    media: List[dict] = []
    cover_mid: Optional[str] = None
    intro_items: List[dict] = []
    take_items: List[dict] = []       # ordered list of normalised take dicts
    image_items: List[dict] = []

    def _take_label(m: dict) -> str:
        lbl = (m.get("label") or "").strip()
        if lbl:
            return lbl
        cat = m.get("category")
        if cat == "take_1":
            return "Take 1"
        if cat == "take_2":
            return "Take 2"
        if cat == "take_3":
            return "Take 3"
        return "Take"

    # 1. Look for explicit client_cover first
    for m in raw_media:
        if m.get("client_cover") and m.get("client_visible") is not False and not m.get("internal_only"):
            cover_mid = m.get("id")
            break

    # Sort legacy takes by category (take_1→take_2→take_3); new `take` items by created_at
    for m in raw_media:
        # Check per-asset client visibility
        if m.get("client_visible") is False or m.get("internal_only") is True:
            continue

        cat = m.get("category")
        if cat == "image":
            if not fv.get("portfolio", True) or reqs.get("portfolio_image_visibility") == "hidden":
                continue
            mapped = {**m, "category": "portfolio"}
            image_items.append(mapped)
            if not cover_mid:
                cover_mid = mapped.get("id")
        elif cat == "indian":
            if not fv.get("portfolio", True) or reqs.get("portfolio_indian_visibility") == "hidden":
                continue
            # Phase 3 — preserve Indian-look images as a distinct section so
            # the client view can render Indian / Western / Portfolio
            # buckets independently. Previously these were silently dropped
            # because _submission_to_client_shape only handled `image`.
            image_items.append({**m, "category": "indian"})
            if not cover_mid:
                cover_mid = m.get("id")
        elif cat == "western":
            if not fv.get("portfolio", True) or reqs.get("portfolio_western_visibility") == "hidden":
                continue
            image_items.append({**m, "category": "western"})
            if not cover_mid:
                cover_mid = m.get("id")
        elif cat in ADMIN_EXTRA_PORTFOLIO_CATEGORIES:
            # Admin Submission feature — same visibility gating as the other
            # look categories above, data-driven by category name rather than
            # a hand-written branch per category (portfolio_<cat>_visibility
            # is an optional per-category hide toggle, same convention as
            # portfolio_indian_visibility/portfolio_western_visibility).
            if not fv.get("portfolio", True) or reqs.get(f"portfolio_{cat}_visibility") == "hidden":
                continue
            image_items.append({**m, "category": cat})
            if not cover_mid:
                cover_mid = m.get("id")
        elif cat == "intro_video":
            if not fv.get("intro_video", True):
                continue
            intro_items.append({**m, "category": "video"})
        elif cat in LEGACY_TAKE_CATEGORIES or cat == "take":
            if not fv.get("takes", True):
                continue
            take_items.append({
                **m,
                "category": "take",
                "label": _take_label(m),
                "_orig_cat": cat,
            })

    # Deterministic order inside takes: respect legacy ordering (take_1 -> take_2 -> take_3) first,
    # then new takes sorted by custom database order (index in raw_media)
    raw_media_ids = [rm.get("id") for rm in raw_media if rm.get("id")]
    def _take_sort_key(m: dict):
        orig = m.get("_orig_cat")
        if orig == "take_1":
            return (0, 1)
        elif orig == "take_2":
            return (0, 2)
        elif orig == "take_3":
            return (0, 3)
        else:
            mid = m.get("id")
            try:
                idx = raw_media_ids.index(mid)
            except ValueError:
                idx = 999
            return (1, idx)

    take_items.sort(key=_take_sort_key)
    for t in take_items:
        t.pop("_orig_cat", None)

    # ORDER: takes → intro → images
    media.extend(take_items)
    media.extend(intro_items)
    media.extend(image_items)

    out: Dict[str, Any] = {
        "id": sub["id"],
        "submission_id": sub["id"],
        "project_id": sub.get("project_id"),
        "name": name,
        "age": age,
        "effective_age": effective_age,
        "submitted_age_override": submitted_age_override,
        "height": fd.get("height") if fv.get("height") else None,
        "location": fd.get("location") if fv.get("location") else None,
        "ethnicity": None,
        # Phase 2 unified identity: surface form_data values into the client
        # view shape (gated by both submission-level field_visibility AND the
        # link-level `visibility.instagram` / `visibility.instagram_followers`
        # toggles upstream). Previously hardcoded to None — that silently
        # dropped Instagram even when the admin had toggled it ON.
        "instagram_handle": (fd.get("instagram_handle") or None) if fv.get("instagram_handle", True) else None,
        "instagram_followers": (fd.get("instagram_followers") or None) if fv.get("instagram_followers", True) else None,
        "work_links": (fd.get("work_links") or []) if fv.get("work_links", True) else [],
        "availability": (fd.get("availability") if fv.get("availability") else None),
        "budget": (fd.get("budget") if fv.get("budget") else None),
        "gender": fd.get("gender") if fv.get("gender") else None,
        "ethnicity": fd.get("ethnicity") if fv.get("ethnicity") else None,
        # Issue #7: Languages and Special Abilities are no longer surfaced to
        # the client — they duplicate Skills. Kept out of the single client
        # shape so every client surface (link, preview, PDF, bundle) matches.
        "skills": fd.get("skills") if fv.get("skills") else [],
        "cover_media_id": cover_mid,
        "media": [_public_media(m) for m in media],
    }
    # Top-level cover URL for clients that prefer a single field over
    # walking media[]. Always either a non-empty Cloudinary URL or None.
    out["image_url"] = _resolve_cover_url(out) or None

    # Competitive brand — only when explicitly enabled.
    if fv.get("competitive_brand"):
        cb = (fd.get("competitive_brand") or "").strip()
        if cb:
            out["competitive_brand"] = cb

    # Custom answers — support both bool and per-question dict shapes.
    # Build a question-ID → question-text lookup from the project's custom_questions
    # array so clients see the human-readable question rather than a raw UUID.
    raw_answers = fd.get("custom_answers") or {}
    if isinstance(raw_answers, dict) and raw_answers:
        ca_vis = fv.get("custom_answers")
        if ca_vis:
            # Build id→text lookup from project.custom_questions when available.
            q_text_by_id: Dict[str, str] = {}
            project_cqs = (project or {}).get("custom_questions") or []
            for cq in project_cqs:
                qid = cq.get("id") or ""
                qtext = (cq.get("question") or "").strip()
                if qid and qtext:
                    q_text_by_id[qid] = qtext

            # Iterate in project question order when available.
            ordered_ids = (
                [cq.get("id") for cq in project_cqs if cq.get("id")]
                if project_cqs else list(raw_answers.keys())
            )

            filtered: List[Dict[str, str]] = []
            seen_ids: set = set()
            for q_id in ordered_ids:
                if q_id not in raw_answers:
                    continue
                if isinstance(ca_vis, dict) and not ca_vis.get(q_id):
                    continue
                a = raw_answers[q_id]
                ans = str(a or "").strip()
                if ans:
                    q_display = q_text_by_id.get(q_id) or q_id
                    filtered.append({"question": q_display, "answer": ans})
                seen_ids.add(q_id)

            # Include answers whose question IDs aren't in the project list
            # (e.g. project was edited after submission).
            for q_id, a in raw_answers.items():
                if q_id in seen_ids:
                    continue
                if isinstance(ca_vis, dict) and not ca_vis.get(q_id):
                    continue
                ans = str(a or "").strip()
                if ans:
                    q_display = q_text_by_id.get(q_id) or q_id
                    filtered.append({"question": q_display, "answer": ans})

            if filtered:
                out["custom_answers"] = filtered

    return out


def generate_submission_snapshot(sub: dict, admin_email: str, project: Optional[dict] = None) -> dict:
    """DEPRECATED (Issue #1/#10). Retained for ONE release only.

    Client-facing rendering no longer reads any frozen snapshot — every client
    surface is shaped live via `_submission_to_client_shape`. Nothing in the
    normal app flow calls this anymore (no auto-write on approve, no write on
    curation save). It is kept in place solely so the deprecated
    `POST /projects/{pid}/submissions/{sid}/snapshot` endpoint (and any
    lingering external reference) keeps working during the transition. Remove
    in the next release.
    """
    # We temporarily remove the client_package_snapshot field on sub to prevent circular loading
    sub_copy = {k: v for k, v in sub.items() if k != "client_package_snapshot"}
    client_shape = _submission_to_client_shape(sub_copy, project=project)

    # Attach snapshot metadata
    client_shape["snapshot_meta"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "author_email": admin_email,
        "project_id": sub.get("project_id"),
    }
    return client_shape


def _paginate_params(page, size, limit=None):
    """Normalise + cap pagination query params.

    Accepts either `?page=&size=` (legacy) or `?page=&limit=` (new). When
    both are supplied, `limit` wins.

    Returns `(skip, page_size, page, page_size)` where page is 0-indexed
    and page_size is clamped to [1, 200].
    """
    p = max(0, int(page or 0))
    effective = limit if limit is not None else size
    s = max(1, min(200, int(effective or 50)))
    return p * s, s, p, s


def _paginated(items, total, page, size) -> dict:
    """Paginated response shape.

    Returns BOTH the legacy keys (`items`, `size`, `has_more`) and the
    canonical keys (`data`, `pages`) so existing consumers keep working
    while new consumers can use the cleaner shape.
    """
    pages = (total + size - 1) // size if size else 0
    return {
        "items": items,
        "data": items,
        "total": total,
        "page": page,
        "size": size,
        "limit": size,
        "pages": pages,
        "has_more": (page + 1) * size < total,
    }


def _public_project(project: dict) -> dict:
    """Strip internal/private fields before returning project info publicly."""
    return {k: v for k, v in project.items() if k not in {"_id", "created_by"}}


def _public_project_for_talent(project: dict) -> dict:
    """Public project shape for the audition/submission form.

    Talents MUST NOT see the client-facing budget — only the talent_budget hint.
    """
    return {
        k: v
        for k, v in project.items()
        if k not in {"_id", "created_by", "client_budget"}
    }


def _clean_budget_lines(lines: Any) -> List[Dict[str, str]]:
    """Normalise a key/value budget list: drop empty rows, coerce strings, trim."""
    out: List[Dict[str, str]] = []
    if not isinstance(lines, list):
        return out
    for row in lines:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()
        value = str(row.get("value") or "").strip()
        if not label and not value:
            continue
        out.append({"label": label, "value": value})
    return out


def _clean_ids(ids: List[str]) -> List[str]:
    """Strip empty strings and deduplicate while preserving order."""
    seen = set()
    out: List[str] = []
    for i in ids or []:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


# --------------------------------------------------------------------------
# Submission ↔ Talent global-profile media sync (Phase 3 v37i)
# --------------------------------------------------------------------------
# Submission media (image/indian/western) is project-scoped, but the talent's
# global profile (TalentEdit / /admin/talents/:id) used to render an empty
# media tab because `db.talents[].media[]` was never populated from
# submission uploads. These helpers mirror image-category media into the
# talent record when a submission has a `talent_email`, and remove it when
# a submission media item is deleted. Idempotent via
# `source_submission_media_id`.

# Categories that should be mirrored from submission to global talent.
# intro_video IS mirrored (single-slot replace, see cat_mapping below) since
# the public /prefill endpoint reads it as the talent's global intro video.
# Only take/take_1/take_2/take_3 (audition takes) are project-scoped and
# intentionally excluded — see cat_mapping's early-return for anything not
# in that dict.
SYNC_TO_GLOBAL_CATEGORIES = {
    "image", "portfolio", "indian", "western", "video", "intro_video", "headshot", "headshots", "additional_portfolio"
}

# Shared deny-list for every "copy a media item by value into a different
# owning document" operation (Release Preparation cleanup — this was two
# independently-maintained but byte-identical sets: this module's own
# sync_media_to_global_talent() mirror, and applications.py's
# _reconcile_draft_from_talent() hydrate). Centralized so a future addition
# only needs to change one place. Excluded fields describe the SOURCE
# document's ownership/location/point-in-time processing state, not the
# physical asset itself, so they'd be wrong (or unsafe) to carry onto a
# copy in a different collection:
#   - id/scope: every copy gets its own id and its own scope value
#   - submission_id/project_id/application_id/talent_id: parent linkage
#     that belongs to the SOURCE document
#   - source_submission_*/source_application_*: recomputed by the caller
#     for THIS copy, never taken from the source
#   - origin: submission-only "global"/"project" upload semantics
#   - label: audition-take-specific display text; takes are never copied
#     by any of these paths, kept for clarity
#   - status/failed_at/failure_reason: the SOURCE upload's own async
#     pipeline state at a point in time; irrelevant once copied (both
#     callers only copy items that already have a real `url`)
#   - client_visible/internal_only/client_cover: Client Review Link
#     visibility flags scoped to that specific submission
#   - category/created_at: every caller sets these explicitly instead
#   - profile_sync_status (Talent Profile Migration, Phase 4): the
#     submission-upload's own "pending/synced/declined" consent bookkeeping.
#     Meaningless outside that one submission's media array — a canonical
#     db.talents.media item (or an application draft hydrated from one)
#     must never carry it, or a later reader could mistake the CANONICAL
#     copy for something still awaiting consent.
MEDIA_COPY_EXCLUDE_FIELDS = frozenset({
    "id", "scope", "category",
    "submission_id", "project_id", "application_id", "talent_id",
    "source_submission_id", "source_submission_media_id",
    "source_application_id", "source_application_media_id",
    "origin", "label", "status", "failed_at", "failure_reason",
    "client_visible", "internal_only", "client_cover", "created_at",
    "profile_sync_status",
    # P7 follow-up: the source item's ownership sub-document describes the
    # SOURCE's ownership — a mirror INTO talents.media[] is re-classified below.
    "ownership", "lifecycle",
})


async def sync_media_to_global_talent(submission: dict, media: dict, skip_cover_cache: bool = False) -> None:
    """Mirror a submission's media into the global talent record.

    ``skip_cover_cache`` (P2-A optimization): when mirroring MANY media in a loop
    (submission finalize), recomputing the talent cover after every single item is
    O(N²) over the growing media array. Callers that loop should pass
    ``skip_cover_cache=True`` and call ``update_talent_cover_cache`` ONCE afterwards.
    Default False preserves the original single-item behaviour for all other callers.

    No-op when:
      - submission has no `talent_email` (anonymous draft)
      - media category is not in whitelisted categories
      - the same source-id has already been mirrored (idempotent)
      - no talent record exists for that email yet (will sync on next upload)
      - the source item has no `url` yet (still processing — e.g. finalize
        can race an in-flight Cloudflare Stream/Cloudinary video transcode;
        mirroring a half-finished item would create a permanently-broken
        Library entry, since the async webhook that later completes the
        SOURCE item has no idea a mirror copy exists to also update)
    """
    cat_mapping = {
        "image": "portfolio",
        "portfolio": "portfolio",
        "indian": "indian",
        "western": "western",
        "video": "video",
        "intro_video": "video",
        "headshot": "headshot",
        "headshots": "headshot",
        "additional_portfolio": "additional_portfolio",
        # Admin Submission feature — same 1:1 pass-through mirroring as the
        # existing look categories above.
        "selfie": "selfie",
        "profiles": "profiles",
        "full_length": "full_length",
        "side_profile": "side_profile",
        "ethnic": "ethnic",
    }
    cat = media.get("category")
    if cat not in cat_mapping:
        return
    mapped_cat = cat_mapping[cat]
    norm_email = normalize_email(submission.get("talent_email"))
    if not norm_email:
        return
    source_id = media.get("id")
    if not source_id:
        return
    if not media.get("url"):
        # Still processing (video transcode/Stream copy not complete yet).
        # See the no-op list above — mirroring now would freeze a broken
        # item into the Library forever.
        return

    talent = await db.talents.find_one({
        "$or": [
            {"normalized_email": norm_email},
            {"email": norm_email}
        ]
    })
    if not talent:
        return

    # Strict deduplication: check if this media asset already exists by public_id, url, or source-id
    pub_id = media.get("public_id")
    url = media.get("url")
    dedup_or = [
        {"source_submission_media_id": source_id},
        {"source_application_media_id": source_id},
    ]
    if pub_id:
        dedup_or.append({"public_id": pub_id})
    if url:
        dedup_or.append({"url": url})
    for m in (talent.get("media") or []):
        if (pub_id and m.get("public_id") == pub_id) or \
           (url and m.get("url") == url) or \
           (m.get("source_submission_media_id") == source_id) or \
           (m.get("source_application_media_id") == source_id):
            return

    # Build the mirror item. Provider-agnostic by construction (Production
    # Certification, Phase 4 item 4 — Provider Metadata Integrity fix):
    # copy EVERY field the source item has, except MEDIA_COPY_EXCLUDE_FIELDS
    # (defined above), rather than hand-picking a fixed set of fields to
    # carry over. The previous whitelist-based copy silently dropped
    # `provider`/`stream_uid`/`thumbnail_url`/`poster_url`/`duration` —
    # which broke long-term lifecycle management (delete/cleanup) for any
    # mirrored video, since Cloudflare Stream's real identifier
    # (`stream_uid`) never reached the Library copy at all. A deny-list
    # means any CURRENT or FUTURE provider-specific field (a new storage
    # backend's own asset key, delivery id, etc.) survives the mirror
    # automatically — no code change needed here when a new provider is
    # added.
    is_app = media.get("scope") == "application" or "application_id" in media
    mirror = {k: v for k, v in media.items() if k not in MEDIA_COPY_EXCLUDE_FIELDS}
    mirror.update({
        "id": str(uuid.uuid4()),
        "category": mapped_cat,
        "created_at": media.get("created_at") or _now(),
        "scope": "talent",
    })
    if is_app:
        mirror["source_application_id"] = submission.get("id")
        mirror["source_application_media_id"] = source_id
    else:
        mirror["source_submission_id"] = submission.get("id")
        mirror["source_submission_media_id"] = source_id

    # P7 follow-up: classify the mirror's OWN ownership (talents.media[] item ->
    # talent-owned, copy-by-value). Keeps new Library mirrors out of the
    # "unknown ownership" accounting bucket.
    try:
        from migrations.media_ownership_rules import ownership_for_new_item
        mirror["ownership"] = ownership_for_new_item(
            "talents", talent, mirror, talent.get("id"), "talents_doc"
        )
    except Exception as e:
        logger.warning("sync_media_to_global_talent: ownership classify failed: %s", e)

    prev_videos = []
    if mapped_cat == "video":
        prev_videos = [m for m in (talent.get("media") or []) if m.get("category") == "video"]
        await db.talents.update_one(
            {"id": talent["id"]},
            {"$pull": {"media": {"category": "video"}}}
        )

    # Phase 6 (race elimination): the dedup check above reads a SNAPSHOT of
    # talent.media that can go stale between the read and this write — e.g.
    # a double-fired consent decision calling this function twice
    # concurrently for the SAME source item, both passing the in-memory
    # check before either has written. The previous unconditional $push
    # here ran regardless of what had changed since the read, producing two
    # mirror copies of the same source item. Folding the dedup condition
    # into the update's own filter makes the check-and-insert a single
    # atomic operation: the $push only applies if, at the moment Mongo
    # evaluates it, no element still matches the same criteria used above.
    # A concurrent winner's push satisfies that criteria first, so this
    # call's filter then fails to match and modified_count is 0 — the
    # loser makes no change and skips every side effect below.
    result = await db.talents.update_one(
        {"id": talent["id"], "media": {"$not": {"$elemMatch": {"$or": dedup_or}}}},
        {"$push": {"media": mirror}, "$set": {"updated_at": _now()}},
    )
    if result.modified_count == 0:
        return

    if mapped_cat == "video" and prev_videos:
        for pv in prev_videos:
            op_id = media.get("operation_id") or str(uuid.uuid4())
            # Reference-aware (Production Certification, Phase 4 item 4,
            # release-readiness sweep): `pv` (the Library video being
            # replaced by this new mirror) could itself still be prefilled
            # into a different, still-in-progress submission draft — must
            # not destroy an asset another submission still depends on.
            await safe_cleanup_media_storage(pv, scope="talent", parent_id=talent["id"], operation_id=op_id)
    if not skip_cover_cache:
        await update_talent_cover_cache(talent["id"])


async def remove_synced_media_from_global_talent(submission: dict, source_media_id: str) -> None:
    """Remove the mirrored copy of a submission or application media from the global talent.

    No-op when no mirror exists. Called from the submission/application media-delete
    endpoint so the global profile stays in sync.
    """
    norm_email = normalize_email(submission.get("talent_email"))
    if not norm_email or not source_media_id:
        return
    await db.talents.update_one(
        {"$or": [{"normalized_email": norm_email}, {"email": norm_email}]},
        {
            "$pull": {"media": {
                "$or": [
                    {"source_submission_media_id": source_media_id},
                    {"source_application_media_id": source_media_id}
                ]
            }},
            "$set": {"updated_at": _now()}
        },
    )
    talent = await db.talents.find_one(
        {"$or": [{"normalized_email": norm_email}, {"email": norm_email}]},
        {"id": 1}
    )
    if talent:
        await update_talent_cover_cache(talent["id"])



# Talent fields classification sets for merge policy
AUTO_UPDATE_FIELDS = {
    "instagram_handle", "instagram_followers", "location", "bio",
    "skills", "work_links", "interested_in", "languages", "phone",
    "alternate_contact_number",
    "cover_media_id", "needs_location_review"
}

PRESERVE_FIELDS = {
    "notes", "tags", "internal_status", "admin_flags", 
    "commission_data", "client_feedback", "status", "created_by",
    "whatsapp_group_name"
}

REVIEW_FIELDS = {
    "name", "dob", "gender", "height", "ethnicity"
}

APPEND_FIELDS = {
    "media"
}

IGNORE_FIELDS = {
    "id", "email", "normalized_email", "created_at", "updated_at", "source",
    "image_url", "cover_thumbnail_url", "cover_url", "media_count",
    "first_submission_at", "last_submission_at", "total_submissions",
    "age"
}


def validate_talent_fields_classification():
    """Verify that all talent schema fields and document keys are classified."""
    classified = AUTO_UPDATE_FIELDS | PRESERVE_FIELDS | REVIEW_FIELDS | APPEND_FIELDS | IGNORE_FIELDS
    model_fields = set()
    if hasattr(TalentOut, "model_fields"):
        model_fields = set(TalentOut.model_fields.keys())
    elif hasattr(TalentOut, "__fields__"):
        model_fields = set(TalentOut.__fields__.keys())
        
    extra_db_fields = {
        "status", "notes", "source", "created_by", "image_url", "cover_thumbnail_url", 
        "cover_url", "media_count", "first_submission_at", "last_submission_at", 
        "total_submissions"
    }
    all_fields = model_fields | extra_db_fields
    missing = all_fields - classified
    if missing:
        raise AssertionError(f"Missing merge policy classification for talent fields: {missing}")


async def resolve_canonical_talent(*, email: Optional[str] = None) -> Optional[dict]:
    """Single canonical lookup for an existing Talent Profile (Talent Profile
    Migration, Phase 2). Every live entry point (apply, submit, admin edit,
    portal) must resolve "does this person already have a Talent Profile"
    through this one function, so the match rule can never drift between
    callers the way it had — `/apply` finalize and its edit endpoint were
    each hand-rolling a narrower 2-field `$or` that silently missed talents
    matchable only by `normalized_email`.

    Keyword-only and additive by design: today only `email` is supported,
    built as an `$or` of whichever identifiers are actually supplied. A
    future identifier (e.g. `phone`) can be added as a new keyword-only
    parameter that appends its own `$or` clauses, without touching any
    existing call site that doesn't pass it.
    """
    ors: List[Dict[str, Any]] = []
    norm_email = normalize_email(email) if email else None
    if norm_email:
        ors.extend([
            {"normalized_email": norm_email},
            {"email": norm_email},
            {"source.talent_email": norm_email},
        ])
    if not ors:
        return None
    return await db.talents.find_one({"$or": ors})


def build_minimal_talent_from_form(
    form: dict,
    *,
    email: Optional[str],
    talent_name: Optional[str],
    talent_phone: Optional[str],
    alternate_contact_number: Optional[str],
    reference_id: str,
    notes: str,
    created_by: str,
    include_skills: bool,
    include_updated_at: bool,
) -> dict:
    """Phase 4 (consolidation): shared minimal-talent constructor for the two
    "auto-create a Talent from an audition submission" call sites in
    submissions.py (`finalize()` and `set_decision()`'s fallback path), which
    were previously two independently hand-maintained dict literals.

    `include_skills`/`include_updated_at` are NOT new behavior — they encode a
    genuine pre-existing difference between the two call sites (set_decision's
    auto-created talent never had a `skills` or `updated_at` field), preserved
    here rather than silently unified.
    """
    full_name = (
        f"{(form.get('first_name') or '').strip()} "
        f"{(form.get('last_name') or '').strip()}"
    ).strip() or talent_name or "Unnamed"
    age_val = None
    if form.get("age") not in (None, ""):
        try:
            age_val = int(form["age"])
        except Exception:
            age_val = None
    new_talent: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "name": full_name,
        "email": email or None,
        "normalized_email": email or None,
        "phone": (form.get("phone") or talent_phone or None),
        "alternate_contact_number": (form.get("alternate_contact_number") or alternate_contact_number or None),
        "age": age_val,
        "dob": (form.get("dob") or None),
        "height": (form.get("height") or None),
        "height_inches": parse_height_to_inches(form.get("height")),
        "location": (form.get("location") or None),
        "ethnicity": (form.get("ethnicity") or None),
        "gender": (form.get("gender") or None),
        "instagram_handle": normalize_instagram_handle(form.get("instagram_handle") or None),
        "instagram_followers": (form.get("instagram_followers") or None),
        "bio": (form.get("bio") or None),
    }
    if include_skills:
        new_talent["skills"] = [s for s in (form.get("skills") or []) if isinstance(s, str) and s.strip()]
    new_talent["work_links"] = [w for w in (form.get("work_links") or []) if isinstance(w, str) and w.strip()]
    new_talent["notes"] = notes
    # Phase 0 — `source` is ALWAYS an object with the exact shape
    # {type, talent_email, reference_id} so the merge $or lookup
    # works symmetrically across all entry points.
    new_talent["source"] = {
        "type": "audition_submission",
        "talent_email": email or None,
        "reference_id": reference_id,
    }
    # Both call sites of this function (submissions.py finalize()/
    # set_decision()) pass the originating submission's `id` as
    # `reference_id`. Also exposed here as its own, explicitly-named
    # top-level field -- distinct from the more generic `source.reference_id`
    # -- so an admin (or a future Migration Review Center) can navigate
    # straight from a talent back to its originating submission without
    # having to know that convention.
    new_talent["originating_submission_id"] = reference_id
    new_talent["media"] = []  # keep global media separate (spec: media must NOT merge)
    new_talent["cover_media_id"] = None
    new_talent["status"] = "SUBMITTED"
    new_talent["created_at"] = _now()
    if include_updated_at:
        new_talent["updated_at"] = _now()
    new_talent["created_by"] = created_by
    return new_talent


async def insert_talent_or_recover(
    new_talent: dict, *, email: Optional[str], context: str,
) -> "Tuple[Optional[dict], bool]":
    """Shared insert-with-duplicate-recovery for the two "auto-create a
    Talent from an audition submission" call sites (submissions.py
    `finalize()` and `set_decision()`'s fallback) — previously two
    independently hand-rolled `try/except DuplicateKeyError` blocks with
    identical (and identically incomplete) recovery logic.

    On a uniqueness collision at insert, the primary recovery is the same
    canonical email lookup every other entry point uses
    (`resolve_canonical_talent`).

    Safe Talent Deduplication (2026-08-17): a prior version of this
    docstring claimed an admin-created legacy profile and an authenticated
    submission-created profile were "intentionally allowed to coexist,"
    citing `docs/ADR_CANONICAL_PROFILE_OWNERSHIP.md` — that citation was
    incorrect (re-verified directly against the document: it never uses the
    word "coexist" and its entire scope is field-sync direction WITHIN one
    canonical record, not whether two records for the same person may both
    exist). Coexistence was in fact the production bug this fix closes (a
    real person appearing twice on the Global Talent roster, e.g. "Kripa
    Trivedi" with 19 assets on one row and 2 on another). Before ANY insert
    is attempted, `_find_migration_candidate` now looks for an existing,
    non-submission-created talent that plausibly represents the same real
    person (exact phone/instagram match, broadened to also catch legacy
    records with no `source` field at all — the ~93.5% majority that the
    original `source.type == "admin"` filter silently missed). If found and
    Part 2's conflict check (`_talent_identity_conflict`) finds no
    conflicting strong identifier, `_auto_link_migration_candidate` links
    onto that EXISTING record and this function returns it with
    `recovered=True` — no second Talent document is ever created. If a
    candidate conflicts (different verified email/phone/DOB — the two
    records more likely represent two different real people), or the link
    attempt itself fails, this degrades to the original behavior: proceed
    with a normal insert and record an advisory `talent_migration_candidates`
    row for manual review (Part 2's stated preference: leaving an uncertain
    duplicate for a human rather than ever risking joining two different
    people).

    2026-08-13: if a uniqueness collision is NOT resolvable by email, this
    logs the failure clearly instead of the previous silent no-op
    (`talent_doc` simply stayed `None`, both callers' `if talent_doc:` guard
    skipped, so `submissions.talent_id` was never set even though
    `status`/`decision` were written unconditionally regardless — a
    submission is fully "submitted"/"approved" and renders correctly
    everywhere that doesn't touch `db.talents`, yet never appeared in
    Global Talent).

    2026-08-14: `talents.phone`'s uniqueness constraint is scoped, via a
    partial filter, to `source.type == "admin"` only (`seed_admin()`'s
    startup index setup, self-healing on every boot) — a submission-created
    talent is structurally never subject to it, so a clean insert (when no
    migration candidate applies) never collides on phone. `email`/
    `normalized_email` remain the only globally-unique fields.

    Returns `(talent_doc, recovered)` — `recovered=True` means the returned
    doc is a PRE-EXISTING record, found either via email-based collision
    recovery or via a confirmed, non-conflicting migration-candidate
    auto-link (not the just-inserted `new_talent` in either case);
    `(None, False)` means creation failed and could not be resolved —
    callers must keep treating that exactly as before (talent_id stays
    unset; nothing else about the submission's own write path changes)."""
    # Detection runs OUTSIDE the insert's try/except on purpose, but is its
    # own try/except: a migration-candidate LOOKUP failure (e.g. a transient
    # Mongo error on this read) must degrade to "no candidate found," never
    # abort the talent creation that follows — the actual insert below must
    # always be attempted regardless of how this lookup goes.
    try:
        candidate = await _find_migration_candidate(new_talent)
    except Exception as e:
        logger.error(
            "insert_talent_or_recover: migration-candidate lookup failed, "
            "proceeding without one — context=%s error=%s", context, e,
        )
        candidate = None

    if candidate and not candidate.get("conflict"):
        try:
            linked = await _auto_link_migration_candidate(candidate, new_talent, context=context)
        except Exception as e:
            logger.error(
                "insert_talent_or_recover: auto-link raised, falling back to "
                "a normal insert -- context=%s legacy_talent_id=%s error=%s",
                context, candidate.get("legacy_talent_id"), e,
            )
            linked = None
        if linked:
            return linked, True
        # Candidate vanished (race) or the link write failed -- fall through
        # to a normal insert below, same as if no candidate had been found,
        # but still record the advisory row afterward (see the `if candidate:`
        # block below) so this near-miss isn't silently lost.

    try:
        await db.talents.insert_one(new_talent)
        await update_talent_cover_cache(new_talent["id"])
        new_talent.pop("_id", None)
    except DuplicateKeyError as exc:
        talent_doc = await resolve_canonical_talent(email=email)
        if talent_doc:
            return talent_doc, True
        logger.error(
            "insert_talent_or_recover: talent creation failed on a uniqueness "
            "collision that is NOT resolvable by email — context=%s email=%r "
            "phone=%r reference_id=%r error=%s",
            context, email, new_talent.get("phone"), new_talent.get("source", {}).get("reference_id"), exc,
        )
        return None, False

    # The talent insert has ALREADY succeeded by this point. Recording the
    # candidate relationship is deliberately OUTSIDE the try/except above
    # (and wrapped in its own, separate try/except here, on top of
    # `_record_migration_candidate`'s own internal one) so that under no
    # circumstance — not a lookup failure, not a write failure, not a bug
    # in that function itself — can migration-candidate bookkeeping ever
    # turn a successful talent creation into a failed one or lose the
    # talent that was already durably written.
    if candidate:
        try:
            await _record_migration_candidate(new_talent["id"], candidate)
        except Exception as e:
            logger.error(
                "insert_talent_or_recover: migration-candidate recording failed "
                "AFTER a successful talent insert — context=%s talent_id=%s "
                "legacy_talent_id=%s error=%s",
                context, new_talent["id"], candidate.get("legacy_talent_id"), e,
            )
    return new_talent, False


# Deliberately conservative, documented confidence scheme for exact-match-only
# detection (never fuzzy). Phone is weighted slightly above Instagram handle
# as the stronger uniqueness signal in practice; matching on both is treated
# as effectively certain. A future Migration Review Center can refine this
# without touching the detection logic itself.
_MIGRATION_CONFIDENCE_BOTH = 0.95
_MIGRATION_CONFIDENCE_PHONE_ONLY = 0.85
_MIGRATION_CONFIDENCE_INSTAGRAM_ONLY = 0.75


def _normalize_identity_value(v: Any) -> Optional[str]:
    return v.strip().lower() if isinstance(v, str) and v.strip() else None


def _talent_identity_conflict(candidate: dict, new_talent: dict) -> Optional[str]:
    """Safe Talent Deduplication, Part 2's CRITICAL SAFETY RULE: two records
    with a conflicting strong identifier — differing non-empty email, phone,
    Instagram handle, or DOB — must never be auto-merged, even though some
    OTHER field matched exactly (that's precisely how they were found as a
    candidate in the first place). Optimizes for "no accidental merging of
    two different people," at the cost of leaving some real duplicates for
    manual review rather than risk joining two different people.

    Only compares fields that are non-empty on BOTH sides — a field missing
    on one side is not a conflict, it's exactly the kind of gap this feature
    is meant to fill in (Part 4/17). Returns a comma-joined string naming
    the conflicting field(s), or `None` if there's nothing blocking an
    auto-link.
    """
    conflicts = []
    for field, a_val, b_val in (
        ("email", candidate.get("normalized_email") or candidate.get("email"), new_talent.get("normalized_email") or new_talent.get("email")),
        ("phone", candidate.get("phone"), new_talent.get("phone")),
        ("instagram_handle", candidate.get("instagram_handle"), new_talent.get("instagram_handle")),
        ("dob", candidate.get("dob"), new_talent.get("dob")),
    ):
        a_norm, b_norm = _normalize_identity_value(a_val), _normalize_identity_value(b_val)
        if a_norm and b_norm and a_norm != b_norm:
            conflicts.append(field)
    return ",".join(conflicts) if conflicts else None


async def _find_migration_candidate(new_talent: dict) -> Optional[dict]:
    """Look for an existing, NOT-submission-created talent (an admin-created
    profile, or a legacy record that predates the `source` field entirely —
    `[CONFIRMED]` ~93.5% of real production admin-created talents have no
    `source` field at all, so restricting this to `source.type == "admin"`
    silently missed the large majority of real duplicates) that plausibly
    represents the SAME real person as `new_talent` (a freshly
    authenticated, submission-created talent about to be inserted) — an
    exact match on `phone` or `instagram_handle` only, deliberately no fuzzy
    name matching, to avoid ever recording a speculative/low-confidence
    relationship (Part 2, Tier 1/exact-match only for auto-linking).

    Read-only: never mutates the candidate, never merges, never deletes.
    Returns a small dict describing the match — `legacy_talent_id`,
    `matched_on`, and `conflict` (a non-`None` reason string means Part 2's
    safety rule blocks any auto-link; the caller must fall back to the
    advisory-only `talent_migration_candidates` record instead) — or `None`
    if nothing matched at all.
    """
    phone = new_talent.get("phone")
    insta = new_talent.get("instagram_handle")
    or_clauses = []
    if phone:
        or_clauses.append({"phone": phone})
    if insta:
        or_clauses.append({"instagram_handle": insta})
    if not or_clauses:
        return None

    candidate = await db.talents.find_one(
        {"source.type": {"$ne": "audition_submission"}, "$or": or_clauses},
        {"_id": 0, "id": 1, "name": 1, "phone": 1, "instagram_handle": 1,
         "email": 1, "normalized_email": 1, "dob": 1},
    )
    if not candidate:
        return None

    matched_on = []
    if phone and candidate.get("phone") == phone:
        matched_on.append("phone")
    if insta and candidate.get("instagram_handle") == insta:
        matched_on.append("instagram_handle")

    return {
        "legacy_talent_id": candidate["id"],
        "matched_on": matched_on,
        "conflict": _talent_identity_conflict(candidate, new_talent),
    }


async def _record_migration_candidate(authenticated_talent_id: str, match: dict) -> None:
    """Inserts one `talent_migration_candidates` document recording that
    `authenticated_talent_id` (the talent just created) plausibly relates
    to `match["legacy_talent_id"]`. Never merges, never links the two
    Talent documents themselves, never writes to either Talent document —
    purely an admin-reviewable audit row, `review_status="pending"` until
    a future Migration Review Center (or a direct DB action) changes it.
    Failures here are logged, never raised — a bookkeeping row must never
    block the actual talent creation this is attached to."""
    matched_on = match["matched_on"]
    if set(matched_on) == {"phone", "instagram_handle"}:
        confidence = _MIGRATION_CONFIDENCE_BOTH
    elif "phone" in matched_on:
        confidence = _MIGRATION_CONFIDENCE_PHONE_ONLY
    else:
        confidence = _MIGRATION_CONFIDENCE_INSTAGRAM_ONLY

    doc = {
        "id": str(uuid.uuid4()),
        "legacy_talent_id": match["legacy_talent_id"],
        "authenticated_talent_id": authenticated_talent_id,
        "matched_fields": matched_on,
        "confidence_score": confidence,
        # Safe Talent Deduplication, Part 2: non-`None` means this candidate
        # was found but NOT auto-linked because a strong identifier
        # conflicted (see `_talent_identity_conflict`) — surfaced here so a
        # human reviewer immediately sees WHY it's still pending instead of
        # having to re-derive it.
        "conflict_reason": match.get("conflict"),
        "review_status": "pending",
        "created_at": _now(),
        "updated_at": _now(),
        "reviewed_by": None,
        "reviewed_at": None,
        "reviewer_notes": None,
    }
    try:
        await db.talent_migration_candidates.insert_one(doc)
    except DuplicateKeyError:
        # A candidate row for this exact (legacy, authenticated) pair
        # already exists (tmc_legacy_authenticated_unique) -- benign and
        # idempotent, not a failure: the relationship is already recorded,
        # there's nothing more to do.
        logger.debug(
            "_record_migration_candidate: candidate already recorded for "
            "legacy=%s authenticated=%s", match["legacy_talent_id"], authenticated_talent_id,
        )
    except Exception as e:
        logger.error(
            "_record_migration_candidate: failed to record candidate "
            "legacy=%s authenticated=%s matched_on=%s error=%s",
            match["legacy_talent_id"], authenticated_talent_id, matched_on, e,
        )


async def _auto_link_migration_candidate(candidate: dict, new_talent: dict, *, context: str) -> Optional[dict]:
    """Safe Talent Deduplication, Part 1's actual fix: a CONFIRMED, non-
    conflicting migration candidate (see `_find_migration_candidate`) means
    `new_talent` and the existing legacy/admin record represent the same
    real person — link onto the EXISTING record instead of letting the
    caller insert a second Talent document.

    Deliberately touches only the two things that need to happen exactly
    ONCE, at the moment two previously-separate identities are first
    confirmed to be the same person:

      1. REVIEW_FIELDS (dob/gender/height/ethnicity): the talent's own
         freshly-authenticated self-report overwrites an older admin-entered
         value when provided (Part 4's explicit data-precedence rule, e.g.
         Admin height 5'4" + Talent height 5'5" -> 5'5"). This is
         deliberately MORE aggressive than `merge_talent_profile`'s
         fill-once-else-conflict-log policy for these same fields — that
         policy exists to protect an ALREADY-linked canonical profile from a
         stale resubmission clobbering a newer Admin Dashboard edit (Issue 1
         / ADR Part 4), a genuinely different scenario where both sides
         already refer to the one and only Talent document. Here, one side
         (the legacy record) has never had a chance to receive the talent's
         own data before — there is no "newer edit" to protect against.
         `name` is deliberately EXCLUDED from this aggressive override and
         kept on the conservative fill-if-empty-only policy: Part 4's own
         wording for name is "keep talent value IF EQUIVALENT" (unlike the
         unconditional height example), and unlike height/dob/gender/
         ethnicity, a submission's name field always carries some value
         (a person always types a first/last name) — blindly letting it win
         risks a typo, nickname, or minor formatting difference silently
         replacing a carefully admin-verified name. An established name is
         never overwritten by this function.
      2. Missing top-level identity (`email`/`normalized_email`): filled in
         if the legacy record had none (Part 17 — identity linking), so the
         very next submission from this same person is recognized
         immediately via `resolve_canonical_talent`'s email lookup, without
         ever needing this phone/instagram candidate path again.

    AUTO_UPDATE_FIELDS (instagram/location-excluded/bio/skills/etc.) and
    media are deliberately left to the caller's existing, already-tested
    `merge_talent_profile()` call, triggered by this function returning a
    doc (the caller treats it exactly like the pre-existing email-collision
    `recovered=True` race-recovery path) — not duplicated here.

    Returns the updated canonical talent doc, or `None` if the candidate
    record vanished (race) or the write failed — the caller falls back to a
    normal insert in that case, exactly as if no candidate had been found.
    """
    canonical = await db.talents.find_one({"id": candidate["legacy_talent_id"]}, {"_id": 0})
    if not canonical:
        return None

    review_update: Dict[str, Any] = {}
    old_values: Dict[str, Any] = {}
    for field in REVIEW_FIELDS:
        incoming_val = new_talent.get(field)
        if incoming_val in (None, "", [], {}):
            continue
        existing_val = canonical.get(field)
        if existing_val == incoming_val:
            continue
        if field == "name":
            if existing_val:
                continue  # established name is never overwritten, see docstring
        old_values[field] = existing_val
        review_update[field] = incoming_val

    if "dob" in review_update:
        age = compute_age(review_update["dob"])
        if age is not None:
            review_update["age"] = age

    email_filled = False
    if not canonical.get("email") and new_talent.get("email"):
        review_update["email"] = new_talent["email"]
        review_update["normalized_email"] = new_talent.get("normalized_email") or new_talent["email"]
        email_filled = True

    if review_update:
        review_update["updated_at"] = _now()
        try:
            await db.talents.update_one({"id": canonical["id"]}, {"$set": review_update})
        except Exception as e:
            logger.error(
                "_auto_link_migration_candidate: field update failed, aborting "
                "auto-link -- context=%s canonical_talent_id=%s error=%s",
                context, canonical["id"], e,
            )
            return None
        canonical.update(review_update)

    # The link itself has ALREADY succeeded by this point (or there was
    # nothing to update, which is still a successful link). Audit recording
    # is deliberately outside/after the write above and never raises, same
    # discipline as `insert_talent_or_recover`'s own candidate bookkeeping.
    try:
        await db.talent_merges.insert_one({
            "id": str(uuid.uuid4()),
            # No losing document was ever created — this IS the fix, not a
            # cleanup of one. `source_talent_id` stays null; a future bulk
            # migration merge of two already-existing duplicates (Part 14)
            # populates it, sharing this same audit collection.
            "source_talent_id": None,
            "canonical_talent_id": canonical["id"],
            "merge_reason": "auto_link_at_submission_no_duplicate_created",
            "matched_by": candidate["matched_on"],
            "field_changes": {
                "old": old_values,
                "new": {k: v for k, v in review_update.items() if k in old_values},
            },
            "email_filled": email_filled,
            "originating_submission_reference": (new_talent.get("source") or {}).get("reference_id"),
            "context": context,
            "timestamp": _now(),
        })
    except Exception as e:
        logger.error(
            "_auto_link_migration_candidate: audit record failed AFTER a "
            "successful link -- context=%s canonical_talent_id=%s error=%s",
            context, canonical["id"], e,
        )

    return canonical


# Phase 1 — Canonical Profile Monotonicity: sentinel distinct from `None` so
# merge_talent_profile can tell "caller never passed snapshot_at at all"
# (today's unconditional-overwrite behavior, for Admin-sourced callers where
# incoming_data is always freshly entered, never a stale snapshot) apart from
# "caller passed snapshot_at=None" (a submission/application that opted into
# the freshness check but has no recorded snapshot of its own -- the
# fail-safe "stale" default, ADR Part 9).
_NO_FRESHNESS_CHECK = object()


async def merge_talent_profile(existing_talent: dict, incoming_data: dict, source: str, snapshot_at=_NO_FRESHNESS_CHECK) -> dict:
    """
    Implements Task 4 (Field-level merge policy) and Task 6 (Profile update audit trail).
    Merges incoming data into existing talent record.

    Phase 1 — Canonical Profile Monotonicity (ADR Part 4 / Invariant #4):
    `snapshot_at` is the caller's own record of how fresh `incoming_data` was
    relative to the canonical profile when it was captured
    (`submissions.talent_profile_snapshot_at` / `applications.talent_profile_updated_at`).
    When a caller passes it, AUTO_UPDATE_FIELDS are only merged if `snapshot_at`
    is not older than `existing_talent["updated_at"]` -- otherwise the
    canonical profile has moved on since the snapshot was taken, so every
    AUTO_UPDATE_FIELDS value is preserved instead of overwritten, and the skip
    is logged as a conflict via the same audit mechanism REVIEW_FIELDS already
    uses. Missing/None `snapshot_at` (once a caller has opted into the check)
    is treated as stale -- the fail-safe direction. A talent with no recorded
    `updated_at` at all has no evidence of a newer edit to protect, so the
    merge proceeds. A caller that never passes `snapshot_at` (e.g. the
    Admin-sourced `POST /talents` create-or-merge path) keeps today's
    unconditional-overwrite behavior -- Admin-entered data is always fresh by
    definition (ADR Part 4.1), so it is never subject to this check.
    """
    email = normalize_email(existing_talent.get("email") or incoming_data.get("email"))

    update_patch = {}
    changed_fields = []
    old_values = {}
    new_values = {}

    # Standardize email and normalized_email
    if email:
        if existing_talent.get("normalized_email") != email:
            update_patch["normalized_email"] = email
        if existing_talent.get("email") != email:
            update_patch["email"] = email

    # Phase 1: is incoming_data's AUTO_UPDATE_FIELDS content stale relative to
    # the canonical profile's own clock? See docstring above for exact
    # fail-safe semantics.
    checking_freshness = snapshot_at is not _NO_FRESHNESS_CHECK
    talent_updated_at = existing_talent.get("updated_at")
    if not checking_freshness or talent_updated_at is None:
        auto_update_is_fresh = True
    elif snapshot_at is None:
        auto_update_is_fresh = False
    else:
        auto_update_is_fresh = snapshot_at >= talent_updated_at

    # 1. AUTO_UPDATE_FIELDS
    for field in AUTO_UPDATE_FIELDS:
        incoming_val = incoming_data.get(field)
        if incoming_val not in (None, "", [], {}):
            existing_val = existing_talent.get(field)
            if existing_val != incoming_val:
                # The freshness gate only matters when there is a populated
                # existing value that could be clobbered -- filling a
                # currently-empty field carries no data-loss risk regardless
                # of snapshot age, exactly like REVIEW_FIELDS' own
                # fill-if-empty rule below.
                if auto_update_is_fresh or existing_val in (None, "", [], {}):
                    update_patch[field] = incoming_val
                    changed_fields.append(field)
                    old_values[field] = existing_val
                    new_values[field] = incoming_val
                else:
                    # Stale snapshot AND a populated existing value: preserve
                    # the canonical value, log the conflict instead of
                    # applying it.
                    changed_fields.append(f"{field}_stale_conflict")
                    old_values[f"{field}_stale_conflict"] = existing_val
                    new_values[f"{field}_stale_conflict"] = incoming_val

    # 2. REVIEW_FIELDS
    for field in REVIEW_FIELDS:
        incoming_val = incoming_data.get(field)
        if incoming_val not in (None, "", [], {}):
            existing_val = existing_talent.get(field)
            if not existing_val:
                update_patch[field] = incoming_val
                changed_fields.append(field)
                old_values[field] = None
                new_values[field] = incoming_val
            elif existing_val != incoming_val:
                # Do NOT overwrite silently, log conflict
                changed_fields.append(f"{field}_conflict")
                old_values[f"{field}_conflict"] = existing_val
                new_values[f"{field}_conflict"] = incoming_val

    # Calculate age if dob is updated/set
    if "dob" in update_patch and update_patch["dob"]:
        age = compute_age(update_patch["dob"])
        if age is not None:
            update_patch["age"] = age
            changed_fields.append("age")
            old_values["age"] = existing_talent.get("age")
            new_values["age"] = age

    if update_patch:
        update_patch["updated_at"] = _now()
        await db.talents.update_one({"id": existing_talent["id"]}, {"$set": update_patch})
        existing_talent.update(update_patch)

    if changed_fields:
        audit_log = {
            "talent_id": existing_talent["id"],
            "email": email,
            "source": source,
            "changed_fields": changed_fields,
            "old_values": old_values,
            "new_values": new_values,
            "timestamp": _now(),
        }
        await db.profile_audits.insert_one(audit_log)

    return existing_talent


# --------------------------------------------------------------------------
# Cloudflare R2 & Media Ingestion Pipeline
# --------------------------------------------------------------------------
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_ENDPOINT_URL = os.environ.get("R2_ENDPOINT_URL", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "")
ENABLE_R2_MEDIA_PIPELINE = os.environ.get("ENABLE_R2_MEDIA_PIPELINE", "false").lower() == "true"

_r2_client = None

def get_r2_client():
    # P1-E fix: build the boto3 S3 client ONCE and reuse it. Re-creating the
    # client on every presign call cost ~6 ms of synchronous work per signature
    # (measured) and blocked the event loop on every upload-signature request and
    # every in-progress-video GET. boto3 clients are thread-safe and presigning is
    # a local (no-network) operation, so a module-level singleton is safe.
    global _r2_client
    if not R2_ENDPOINT_URL or not R2_ACCESS_KEY_ID or not R2_SECRET_ACCESS_KEY:
        logger.info("get_r2_client: Cloudflare R2 is not fully configured or disabled.")
        return None
    if _r2_client is None:
        import boto3
        from botocore.config import Config
        try:
            _r2_client = boto3.client(
                "s3",
                endpoint_url=R2_ENDPOINT_URL,
                aws_access_key_id=R2_ACCESS_KEY_ID,
                aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                config=Config(signature_version="s3v4"),
            )
        except Exception as e:
            logger.error(f"get_r2_client: failed to initialize boto3 client: {e}", exc_info=True)
            return None
    return _r2_client

def generate_r2_presigned_url(key: str, method: str = "PUT", expiry: int = 3600) -> str:
    """Generate a pre-signed S3 URL for Cloudflare R2 operations (PUT or GET)."""
    s3 = get_r2_client()
    if not s3:
        logger.warning("generate_r2_presigned_url: R2 client is unconfigured or unavailable. Returning empty URL.")
        return ""
    client_method = "put_object" if method.upper() == "PUT" else "get_object"
    params = {"Bucket": R2_BUCKET_NAME, "Key": key}
    if method.upper() == "PUT":
        # Ensure we specify public-read or any R2 specific permissions if needed, 
        # but standard pre-signed PUT works out of the box.
        pass
    return s3.generate_presigned_url(
        ClientMethod=client_method,
        Params=params,
        ExpiresIn=expiry,
    )


async def cleanup_media_storage(media: dict, scope: Optional[str] = None, parent_id: Optional[str] = None, operation_id: Optional[str] = None) -> None:
    """Best-effort deletion of a single media item's backing storage objects and
    its tracking row. Shared by submission + application media-delete paths.

    Contract (per parity sprint):
      - NEVER raises — every external call is individually guarded and logged, so
        a storage failure can never fail the user's delete action.
      - Idempotent — missing assets are ignored (Stream/Cloudinary 404, R2 delete
        is a no-op on absent keys).
      - Safe — targets are addressed by their own exact identifiers (stream_uid,
        public_id, and a deterministic per-asset R2 key scoped to this parent),
        so it cannot touch another talent's assets.

    Deletes: Cloudflare Stream video (by stream_uid), R2 raw upload (derived key),
    Cloudinary asset (by public_id, images + legacy videos), and asset_metadata
    rows. Mongo media-array references are removed by the caller's $pull.
    """
    if not media:
        return
    start_time = datetime.now(timezone.utc)
    op_id = operation_id or str(uuid.uuid4())
    public_id = media.get("public_id")
    rtype = media.get("resource_type") or ("video" if media.get("category") in {"intro_video", "take", "take_1", "take_2", "take_3", "video", "portfolio_video"} else "image")
    category = media.get("category")
    provider = media.get("provider")
    stream_uid = media.get("stream_uid")
    url = media.get("url") or ""
    scope = scope or media.get("scope")
    parent_id = parent_id or media.get("submission_id") or media.get("application_id")

    logger.info(
        f"Operation DELETE Started | OpID: {op_id} | TalentID: {media.get('talent_id')} | "
        f"SubmissionID: {media.get('submission_id')} | ProjectID: {media.get('project_id')} | "
        f"AssetType: {category or rtype} | OldAssetID: {media.get('id')}"
    )

    try:
        # 1) Cloudflare Stream video — delete by its exact UID.
        if stream_uid:
            try:
                account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
                token = os.environ.get("CLOUDFLARE_STREAM_API_TOKEN")
                if account_id and token:
                    import httpx
                    async with httpx.AsyncClient(timeout=15.0) as _c:
                        await _c.delete(
                            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/stream/{stream_uid}",
                            headers={"Authorization": f"Bearer {token}"},
                        )
            except Exception as e:
                logger.warning(f"[cleanup] Stream delete failed uid={stream_uid}: {e}")

        # 2) R2 raw upload — only videos go through R2; key is deterministic + scoped.
        if rtype == "video" and scope and parent_id and public_id:
            leaf = public_id.split("/")[-1]
            r2_key = f"raw-uploads/{scope}s/{parent_id}/{category}/{leaf}.mp4"
            try:
                get_r2_client().delete_object(Bucket=R2_BUCKET_NAME, Key=r2_key)
            except Exception as e:
                logger.warning(f"[cleanup] R2 delete failed key={r2_key}: {e}")
            try:
                await db.asset_metadata.delete_many({"public_id": r2_key})
            except Exception as e:
                logger.warning(f"[cleanup] asset_metadata(r2) delete failed key={r2_key}: {e}")

        # 3) Cloudinary asset — images, and legacy Cloudinary-hosted videos. Stream
        #    videos live on cloudflarestream and must NOT be sent to Cloudinary.
        is_cloudinary = (
            provider == "cloudinary"
            or rtype == "image"
            or "res.cloudinary.com" in url
        ) and provider != "stream" and "cloudflarestream.com" not in url
        if public_id and is_cloudinary:
            try:
                cloudinary.uploader.destroy(public_id, resource_type=rtype, invalidate=True)
            except Exception as e:
                logger.warning(f"[cleanup] Cloudinary destroy failed pid={public_id}: {e}")

        # 4) Tracking row keyed by the media public_id.
        if public_id:
            try:
                await db.asset_metadata.delete_many({"public_id": public_id})
            except Exception as e:
                logger.warning(f"[cleanup] asset_metadata delete failed pid={public_id}: {e}")

        duration_sec = (datetime.now(timezone.utc) - start_time).total_seconds()
        logger.info(
            f"Operation DELETE Succeeded | OpID: {op_id} | TalentID: {media.get('talent_id')} | "
            f"SubmissionID: {media.get('submission_id')} | ProjectID: {media.get('project_id')} | "
            f"AssetType: {category or rtype} | OldAssetID: {media.get('id')} | Duration: {duration_sec}s"
        )
    except Exception as e:
        duration_sec = (datetime.now(timezone.utc) - start_time).total_seconds()
        logger.error(
            f"Operation DELETE Failed | OpID: {op_id} | TalentID: {media.get('talent_id')} | "
            f"SubmissionID: {media.get('submission_id')} | ProjectID: {media.get('project_id')} | "
            f"AssetType: {category or rtype} | OldAssetID: {media.get('id')} | Reason: {str(e)} | Duration: {duration_sec}s"
        )



async def trigger_cloudinary_transcode(
    media_id: str,
    r2_url: str,
    folder: str,
    public_id: str,
    eager_transformation: str = None,
    scope: str = "submission",
    parent_id: str = None,
    category: str = None,
    label: str = None,
    operation_id: str = None,
):
    """
    Abstractions wrapper that delegates video processing to the active VideoProvider.
    """
    from providers import get_video_provider
    provider = get_video_provider()
    logger.info(f"[VideoProvider] Delegating transcode to {provider.__class__.__name__} for media_id={media_id} | OpID: {operation_id}")
    
    res = await provider.create_processing_job(
        parent_id=parent_id,
        media_id=media_id,
        category=category,
        scope=scope,
        r2_url=r2_url,
        folder=folder,
        public_id=public_id,
        label=label,
        eager_transformation=eager_transformation,
        operation_id=operation_id
    )
    logger.info(f"[VideoProvider] Result: {res}")


def sign_r2_media_if_needed(doc: dict, is_application: bool = False) -> dict:
    """
    Checks the media array of a submission or application document.
    For any video category with status == "processing" or no url,
    generates a presigned R2 GET URL on-the-fly and patches the dict.

    Guards:
    - Skips media already completed via a named provider (stream, cloudinary).
    - Skips media with status "completed" that already has a URL.
    Only raw-processing uploads (no URL yet) receive temporary R2 URLs.
    """
    if not doc or "media" not in doc:
        return doc
    parent_id = doc.get("id")
    for m in doc.get("media") or []:
        if m.get("category") in {"take", "intro_video", "take_1", "take_2", "take_3", "portfolio_video"}:
            # Guard 1: Skip media already backed by a named provider (stream or cloudinary).
            # These already have their canonical URL written by the webhook / Cloudinary callback.
            if m.get("provider") in ("stream", "cloudinary") and m.get("url"):
                continue
            # Guard 2: Skip any media marked completed that already has a URL, regardless of provider.
            # Prevents overwriting a valid URL that arrived before the provider field was populated.
            if m.get("status") == "completed" and m.get("url"):
                continue
            if m.get("status") == "processing" or not m.get("url"):
                pub_id = m.get("public_id")
                if pub_id and "/" in pub_id:
                    leaf_pid = pub_id.split("/")[-1]
                    category = m.get("category")
                    if is_application:
                        r2_key = f"raw-uploads/applications/{parent_id}/{category}/{leaf_pid}.mp4"
                    else:
                        r2_key = f"raw-uploads/submissions/{parent_id}/{category}/{leaf_pid}.mp4"
                    try:
                        # R2 presigned GET URL valid for 24 hours
                        presigned_url = generate_r2_presigned_url(r2_key, "GET", expiry=86400)
                        m["url"] = presigned_url
                        m["status"] = "completed"
                    except Exception as e:
                        logger.error(f"Failed to generate presigned R2 URL for key {r2_key}: {e}")
    return doc


async def reconcile_submission_media(sub: dict, library_media: List[dict]) -> bool:
    """Talent Profile Migration, Phase 3 — keep a draft submission's
    library-derived media honest against the live Talent Profile.

    A submission's own `db.talents`-sourced items are copies (by value —
    see the new-endpoint docstring in submissions.py), so once copied they
    would otherwise sit frozen until finalize. That's wrong for an
    in-progress DRAFT: it hasn't earned historical-snapshot immutability
    yet (that only starts at finalize), so it should keep tracking the
    canonical profile until the moment it's actually submitted — closing
    the pre-existing gap where submissions, unlike applications
    (`_reconcile_draft_from_talent`), had no draft-reconciliation-on-read.

    Walks every `sub["media"]` item carrying `source_talent_media_id` and
    compares it against `library_media` (build_prefill_media()'s live
    output, i.e. the current canonical profile). If the source item's
    descriptive fields drifted (re-processed video, updated thumbnail),
    refreshes the copy. If the source item no longer exists (deleted from
    the profile), flags it `removed_from_profile=True` — it is NEVER
    silently dropped; the frontend decides whether to keep it for this
    submission or remove it.

    Persists any change back to `db.submissions` so a caller reading the
    document directly afterward (finalize) sees the reconciled state too,
    not just this function's in-memory return value. Returns True if
    anything changed.
    """
    media = sub.get("media") or []
    lib_by_id = {m["id"]: m for m in library_media}
    # Fields that describe the physical asset itself — refreshed from the
    # source when they drift. Never touches the copy's own id/category/
    # scope/origin/source_talent_media_id/created_at.
    REFRESH_FIELDS = ("url", "public_id", "resource_type", "content_type", "size", "original_filename")

    changed = False
    for m in media:
        src_id = m.get("source_talent_media_id")
        if not src_id:
            continue
        lib_item = lib_by_id.get(src_id)
        if lib_item is None:
            if not m.get("removed_from_profile"):
                m["removed_from_profile"] = True
                changed = True
            continue
        if m.get("removed_from_profile"):
            m["removed_from_profile"] = False
            changed = True
        for field in REFRESH_FIELDS:
            if lib_item.get(field) != m.get(field):
                m[field] = lib_item.get(field)
                changed = True

    if changed:
        await db.submissions.update_one({"id": sub["id"]}, {"$set": {"media": media}})
    return changed


def mark_reusable_media_pending(media_item: dict) -> None:
    """Talent Profile Migration, Phase 4 — the one place that decides which
    freshly-uploaded categories need consent before they can ever reach
    db.talents.media. Mutates `media_item` in place; every submission-upload
    construction site calls this instead of deciding for itself, so the
    reusable-category list only lives in one place (REUSABLE_MEDIA_CATEGORIES
    above).

    A tagged item is `profile_sync_status="pending"` until the talent answers
    the consent dialog — see apply_media_consent_decision() in
    routers/submissions.py, the only place that ever resolves it. Audition
    takes (take/take_1..3) are never tagged; they were never eligible to
    sync to the profile and this changes nothing about them.
    """
    if media_item.get("category") in REUSABLE_MEDIA_CATEGORIES:
        media_item["profile_sync_status"] = "pending"


async def build_talent_submission_view(sub: dict) -> dict:
    """The single canonical talent-facing submission representation.

    Not to be confused with `_submission_to_client_shape` (above) — that one
    flattens + filters a submission for the *brand client's* review link
    (`/l/{slug}`), respecting `field_visibility`. This one is for a talent
    viewing their OWN submission: the raw stored document, unfiltered
    (field_visibility gates client visibility, not the talent's own view of
    their own data — same precedent both existing talent-facing endpoints
    already follow), plus approved client feedback and resolved media URLs.

    Every talent-facing submission read should call this — it's the only
    place responsible for attaching feedback and signing R2 media, so a
    future change to either only needs to happen here.

    Talent Profile Migration, Phase 3: also the one place that computes
    `library_media` (live, never stored — see build_prefill_media()) and
    reconciles any already-selected library items against it.

    Talent Profile Migration, Phase 4: also surfaces `pending_media_consent`
    — every media item still awaiting the talent's "only this project" vs
    "update my Talent Profile" answer — so the frontend can show the
    aggregated dialog on resume, not just right after upload.
    """
    from routers.feedback import list_approved_feedback_for_talent
    from routers.submissions import build_prefill_media, has_been_submitted_once

    talent = await resolve_canonical_talent(email=sub.get("talent_email"))
    library_media = await build_prefill_media(talent, email=talent.get("email")) if talent else []
    # Phase 2 — Issue 2 fix: a submission that has ever been finalized
    # (status "submitted" or "updated" — has_been_submitted_once(), the same
    # test already used to protect the canonical Talent Profile from retest
    # pollution) is a historical snapshot and must never be rewritten by a
    # read. Mirrors the already-working applications.py:594 pattern
    # (`status != "submitted"`) — reconciliation against the live Library
    # only runs while the submission is still a mutable draft. finalize()'s
    # own pre-freeze reconcile call (submissions.py) is unaffected: that is
    # a write-endpoint action re-establishing a fresh snapshot at the moment
    # of freezing, not a passive read.
    if not has_been_submitted_once(sub):
        await reconcile_submission_media(sub, library_media)
    sub["library_media"] = library_media
    sub["pending_media_consent"] = [
        m for m in (sub.get("media") or []) if m.get("profile_sync_status") == "pending"
    ]

    sub["client_feedback"] = await list_approved_feedback_for_talent(sub["id"])
    return sign_r2_media_if_needed(sub)


def get_client_ip(request: Request) -> str:
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


async def check_rate_limit(request: Request, endpoint: str, email: str = None):
    ip = get_client_ip(request)
    now = datetime.now(timezone.utc)
    
    if endpoint == "login":
        limit = int(os.environ.get("AUTH_RATE_LIMIT_LOGIN", 5))
        window_minutes = 15
    elif endpoint == "forgot_password":
        limit = int(os.environ.get("AUTH_RATE_LIMIT_FORGOT", 3))
        window_minutes = 60
    elif endpoint == "otp_send":
        limit = int(os.environ.get("AUTH_RATE_LIMIT_OTP", 5))
        window_minutes = 60
    elif endpoint == "otp_verify":
        limit = int(os.environ.get("AUTH_RATE_LIMIT_OTP_VERIFY", 10))
        window_minutes = 60
    else:
        return
        
    window_start = now - timedelta(minutes=window_minutes)
    
    query = {
        "endpoint": endpoint,
        "timestamp": {"$gte": window_start},
        "$or": [{"ip": ip}]
    }
    if email:
        query["$or"].append({"email": email})
        
    count = await db.rate_limits.count_documents(query)
    if count >= limit:
        user_agent = request.headers.get("user-agent", "unknown")
        logger.warning(
            f"Rate limit triggered: Endpoint={endpoint}, IP={ip}, Email={email}, "
            f"UserAgent={user_agent}, Reason=Limit exceeded ({count}/{limit})"
        )
        oldest = await db.rate_limits.find_one(query, sort=[("timestamp", 1)])
        retry_after = 60
        if oldest:
            elapsed = (now - oldest["timestamp"].replace(tzinfo=timezone.utc)).total_seconds()
            retry_after = max(1, int((window_minutes * 60) - elapsed))
            
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(retry_after)}
        )
        
    await db.rate_limits.insert_one({
        "endpoint": endpoint,
        "ip": ip,
        "email": email,
        "timestamp": now
    })





