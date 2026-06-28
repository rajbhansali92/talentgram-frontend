"""Shared primitives: config, DB, storage, auth, utils, constants, models, visibility filters.

Everything that multiple routers need lives here to keep router modules pure of plumbing.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field, field_validator

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
    data = {**payload, "exp": datetime.now(timezone.utc) + timedelta(days=days)}
    return jwt.encode(data, JWT_SECRET, algorithm="HS256")


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
    user = await db.users.find_one(
        {"email": data.get("email")},
        {"_id": 0, "password_hash": 0, "invite_token": 0},
    )
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


async def verify_email_ownership(authorization: Optional[str], email: str) -> bool:
    """Return True only if the caller has *proven ownership* of ``email``.

    This is the gate that protects the otherwise-public start/prefill flows
    (`/public/apply`, `/public/projects/{slug}/submission`, `/public/prefill`)
    against anonymous PII disclosure, draft hijack and destructive resets.

    Three accepted, revocation-aware credential forms — all of which can only
    exist *after* a real ownership proof (OTP / Google) or a prior verified
    session:

    1. A signature-valid, non-expired **portal token** (role ``portal``) whose
       ``email`` claim matches. Portal tokens are minted exclusively by the OTP
       and Google verification paths, so possession == prior proof of ownership.
       Forgery requires the server-side ``JWT_SECRET``.
    2. A valid **submitter** credential (JWT or opaque ``access_token``) already
       bound to an application/submission whose ``talent_email`` matches. This
       preserves legitimate cross-device "resume" without re-OTP.

    A completely anonymous caller (no/invalid token) returns ``False``.
    """
    target = normalize_email(email)
    if not target:
        return False
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    token = authorization.split(" ", 1)[1]

    # --- Form 1: portal token (pure JWT check, no DB) ----------------------
    data = decode_token(token)
    if data and data.get("role") == "portal":
        if normalize_email(data.get("email")) == target:
            return True

    # --- Form 2: existing submitter credential bound to this email ---------
    submitter = await decode_submitter(authorization)
    if submitter:
        sid = submitter.get("sid")
        if submitter.get("kind") == "application":
            doc = await db.applications.find_one({"id": sid}, {"talent_email": 1})
        else:
            doc = await db.submissions.find_one({"id": sid}, {"talent_email": 1})
        if doc and normalize_email(doc.get("talent_email")) == target:
            return True

    return False


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
        b"%PDF": "application/pdf"
    }

    detected_mime = None
    for sig, mime in allowed_signatures.items():
        if data.startswith(sig):
            detected_mime = mime
            break

    # WebP check extension: WebP files contain RIFF header and WEBP signature bytes at offset 8
    if data.startswith(b"RIFF") and b"WEBP" in data[8:15]:
        detected_mime = "image/webp"

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
    if ct and not ct.startswith(detected_mime.split('/')[0]):
        raise HTTPException(status_code=400, detail="MIME type header does not match detected file signature.")
    
    is_pdf = ct == "application/pdf" or ct.startswith("application/pdf")
    if is_pdf and resource_type == "auto":
        resource_type = "raw"
        
    is_video = resource_type == "video" or (
        resource_type == "auto"
        and ct.startswith("video/")
    )
    is_image = resource_type == "image" or (
        resource_type == "auto"
        and ct.startswith("image/")
    )

    upload_kwargs: Dict[str, Any] = dict(
        folder=folder,
        public_id=public_id,
        resource_type=resource_type,
        overwrite=True,
        unique_filename=False,
    )

    if is_video:
        # If we do NOT want to keep the original (e.g. keep_original is False) AND video > 300MB,
        # we apply an INCOMING transformation to make Cloudinary write the 720p H.264 MP4 derivative AS the original asset.
        # This completely discards the heavy original file immediately on upload, saving massive long-term storage costs.
        if not keep_original and len(data) > 300_000_000:
            upload_kwargs["transformation"] = [
                {"width": 1280, "height": 720, "crop": "limit"},
                {"quality": "auto", "video_codec": "auto"},
            ]
            upload_kwargs["format"] = "mp4"
            # Eagerly generate only the video poster frame to control transformation generation costs.
            upload_kwargs["eager"] = [
                {
                    "format": "jpg",
                    "transformation": [
                        {"width": 600, "height": 338, "crop": "fill", "dpr": "auto"},
                        {"quality": "auto"},
                    ],
                }
            ]
            upload_kwargs["eager_async"] = False
        else:
            # Synchronous H.264 MP4 720p derivative and poster frame.
            upload_kwargs["eager"] = [
                {
                    "format": "mp4",
                    "transformation": [
                        {"width": 1280, "height": 720, "crop": "limit"},
                        {"quality": "auto", "video_codec": "auto"},
                    ],
                },
                {
                    "format": "jpg",
                    "transformation": [
                        {"width": 600, "height": 338, "crop": "fill", "dpr": "auto"},
                        {"quality": "auto"},
                    ],
                }
            ]
            upload_kwargs["eager_async"] = False
    elif is_image:
        # Eagerly generate ONLY the roster thumbnail preset to prevent dynamic transform cost explosion.
        # Larger detail/lightbox views are dynamically generated on-demand.
        upload_kwargs["eager"] = [
            {
                "width": 400,
                "crop": "fill",
                "dpr": "auto",
                "fetch_format": "auto",
                "quality": "auto",
            }
        ]
        upload_kwargs["eager_async"] = False

    try:
        result = cloudinary.uploader.upload(data, **upload_kwargs)
    except Exception as e:
        logger.error(f"Cloudinary upload failed (folder={folder} pid={public_id}): {e}")
        raise HTTPException(502, "Storage upload failed")

    # Prefer the eager (720p MP4) URL for videos so we serve compressed.
    # Fall back to the original `secure_url` if eager generation failed.
    primary_url = result.get("secure_url")
    eager_list = result.get("eager") or []
    if is_video and eager_list:
        # Filter for mp4 format secure_url
        compressed = next((x.get("secure_url") for x in eager_list if x.get("format") == "mp4"), None)
        if not compressed:
            # Otherwise fall back to the first eager item
            compressed = eager_list[0].get("secure_url")
        if compressed:
            primary_url = compressed

    return {
        "url": primary_url,
        "original_url": result.get("secure_url"),
        "public_id": result.get("public_id"),
        "resource_type": result.get("resource_type"),
        "format": result.get("format"),
        "bytes": result.get("bytes"),
        "width": result.get("width"),
        "height": result.get("height"),
        "duration": result.get("duration"),
    }


async def log_storage_action(
    user_id: Optional[str],
    action_type: str, # 'UPLOAD', 'ARCHIVE', 'RESTORE', 'DELETE'
    public_id: Optional[str] = None,
    project_id: Optional[str] = None,
    talent_id: Optional[str] = None,
    submission_id: Optional[str] = None
):
    doc = {
        "user_id": user_id,
        "timestamp": datetime.now(timezone.utc),
        "action_type": action_type,
        "public_id": public_id,
        "project_id": project_id,
        "talent_id": talent_id,
        "submission_id": submission_id
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
) -> dict:
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
    public_id_to_store = f"{folder}/{media_id}" if keep_original else f"{folder}/audition_web"

    # Database First: Insert pending metadata record
    pending_metadata = {
        "public_id": public_id_to_store,
        "asset_id": f"pending_{media_id}",
        "folder_path": folder,
        "asset_url": "",
        "secure_url": "",
        "file_name": "audition_web" if not keep_original else media_id,
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
        "resource_type": resource_type
    }
    await db.asset_metadata.update_one(
        {"public_id": public_id_to_store},
        {"$set": pending_metadata},
        upsert=True
    )

    try:
        # 2. Synchronize to Cloudinary
        if resource_type == "video" and not keep_original:
            temp_public_id = f"original_{media_id}"
            temp_result = cloudinary_upload(
                data,
                folder=folder,
                public_id=temp_public_id,
                resource_type="video",
                content_type=content_type,
                keep_original=True
            )

            eager_trans = [
                {"width": 1920, "height": 1080, "crop": "limit", "video_codec": "h264", "bit_rate": "5m", "quality": "auto"},
            ]
            cloudinary_upload_args = {
                "folder": folder,
                "public_id": temp_public_id,
                "resource_type": "video",
                "overwrite": True,
                "unique_filename": False,
                "eager": [
                    {
                        "format": "mp4",
                        "transformation": eager_trans
                    },
                    {
                        "format": "jpg",
                        "transformation": [{"width": 600, "height": 338, "crop": "fill", "quality": "auto"}]
                    }
                ],
                "tags": tags
            }
            res = cloudinary.uploader.upload(data, **cloudinary_upload_args)

            mp4_url = None
            jpg_url = None
            for eager_item in res.get("eager", []):
                if eager_item.get("format") == "mp4":
                    mp4_url = eager_item.get("secure_url")
                elif eager_item.get("format") == "jpg":
                    jpg_url = eager_item.get("secure_url")

            if not mp4_url:
                mp4_url = res.get("secure_url")

            final_video_res = cloudinary.uploader.upload(
                mp4_url,
                folder=folder,
                public_id="audition_web",
                resource_type="video",
                tags=tags
            )

            if jpg_url:
                final_thumb_res = cloudinary.uploader.upload(
                    jpg_url,
                    folder=folder,
                    public_id="thumbnail",
                    resource_type="image",
                    tags=tags
                )
            else:
                final_thumb_res = {}

            cloudinary_destroy(f"{folder}/{temp_public_id}", resource_type="video")

            result = {
                "url": final_video_res.get("secure_url"),
                "secure_url": final_video_res.get("secure_url"),
                "public_id": final_video_res.get("public_id"),
                "resource_type": "video",
                "format": final_video_res.get("format"),
                "bytes": final_video_res.get("bytes"),
                "width": final_video_res.get("width"),
                "height": final_video_res.get("height"),
                "duration": final_video_res.get("duration"),
                "asset_id": final_video_res.get("asset_id"),
                "thumbnail_url": final_thumb_res.get("secure_url")
            }
        else:
            upload_res = cloudinary_upload(
                data,
                folder=folder,
                public_id=media_id,
                resource_type=resource_type,
                content_type=content_type,
                keep_original=True
            )
            cloudinary.uploader.add_tag(",".join(tags), upload_res["public_id"])

            result = {
                "url": upload_res["url"],
                "secure_url": upload_res["original_url"],
                "public_id": upload_res["public_id"],
                "resource_type": upload_res["resource_type"],
                "format": upload_res["format"],
                "bytes": upload_res["bytes"],
                "width": upload_res["width"],
                "height": upload_res["height"],
                "duration": upload_res["duration"],
                "asset_id": upload_res.get("asset_id") or upload_res["public_id"]
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
            "updated_at": datetime.now(timezone.utc)
        }
        await db.asset_metadata.update_one(
            {"public_id": result["public_id"]},
            {"$set": final_metadata}
        )

        await log_storage_action(
            user_id=user_id,
            action_type="UPLOAD",
            public_id=result["public_id"],
            project_id=project_id,
            talent_id=talent_id,
            submission_id=submission_id
        )

        return result
    except Exception as e:
        await db.asset_metadata.update_one(
            {"public_id": public_id_to_store},
            {
                "$set": {
                    "upload_status": "failed",
                    "error_reason": str(e),
                    "updated_at": datetime.now(timezone.utc)
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

    Default transformations applied to images: f_auto, q_auto, dpr_auto. Pass
    additional via kwargs (e.g. width=1600, crop="limit").
    """
    if resource_type == "image":
        transformations.setdefault("fetch_format", "auto")
        transformations.setdefault("quality", "auto")
        transformations.setdefault("dpr", "auto")
    url, _opts = cloudinary.utils.cloudinary_url(
        public_id, resource_type=resource_type, secure=True, **transformations
    )
    return url


def stream_video_url(public_id: Optional[str]) -> Optional[str]:
    """Build a optimized video URL for a video.
    Matches the eager transformation pre-generated on upload:
    c_limit,h_720,w_1280/q_auto,vc_auto/f_mp4
    """
    if not public_id:
        return None
    if public_id.startswith(("http://", "https://")):
        return public_id
    url, _ = cloudinary.utils.cloudinary_url(
        public_id,
        resource_type="video",
        secure=True,
        transformation=[
            {"width": 1280, "height": 720, "crop": "limit"},
            {"quality": "auto", "video_codec": "auto"},
            {"fetch_format": "mp4"}
        ]
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
    is discarded on ingest (mirrors the existing keep_original=False strategy)."""
    return [
        {"width": 1280, "height": 720, "crop": "limit"},
        {"quality": "auto", "video_codec": "auto"},
    ]


def video_poster_url(public_id: Optional[str]) -> Optional[str]:
    """Cloudinary video thumbnail: extract first frame as JPEG/AVIF."""
    if not public_id:
        return None
    if public_id.startswith(("http://", "https://")):
        url = public_id
        if "res.cloudinary.com" in url:
            base, ext = os.path.splitext(url)
            if "?" in ext:
                ext = ext.split("?")[0]
            jpg_url = base + ".jpg"
            if "/video/upload/" in jpg_url:
                # Add default width and quality transformations
                jpg_url = jpg_url.replace("/video/upload/", "/video/upload/w_600,h_338,c_fill,q_auto/")
            return jpg_url
        return None
    url, _ = cloudinary.utils.cloudinary_url(
        public_id,
        resource_type="video",
        format="jpg",
        transformation=[
            {"width": 600, "height": 338, "crop": "fill", "dpr": "auto"},
            {"quality": "auto"}
        ],
        secure=True
    )
    return url


def media_url(
    public_id: Optional[str], preset: str = "detail", resource_type: str = "image"
) -> Optional[str]:
    """Build a transformation URL on the fly.

    Presets:
      roster     — w_400, c_fill, f_auto, q_auto   (roster card ~200px wide @2x)
      thumb      — w_200, c_fill, f_auto, q_auto   (pipeline card / mini thumbnail)
      detail     — w_1200, c_limit, f_auto, q_auto (detail page, mobile-friendly)
      full       — w_1600, c_limit, f_auto, q_auto (lightbox / full-res view)
      poster     — w_600, h_338, c_fill, f_auto, q_auto (video poster)
    """
    if not public_id:
        return None
    if public_id.startswith(("http://", "https://")):
        return public_id

    if preset == "roster":
        return cloudinary_url_for(public_id, resource_type, width=400, crop="fill")
    elif preset == "thumb":
        return cloudinary_url_for(public_id, resource_type, width=200, crop="fill")
    elif preset == "detail":
        return cloudinary_url_for(public_id, resource_type, width=1200, crop="limit")
    elif preset == "full":
        return cloudinary_url_for(public_id, resource_type, width=1600, crop="limit")
    elif preset == "poster":
        return video_poster_url(public_id)

    return cloudinary_url_for(public_id, resource_type)




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

    # P0 production indexes — 6 collections.
    # Each is idempotent; create_index is a no-op if already present.
    p0_indexes = [
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
        ("projects", [("slug", 1)], {"unique": True, "name": "proj_slug_unique"}),
        # Persistent access_token lookup — sparse so docs without the field
        # are ignored, unique so two submissions can't share a token.
        ("submissions", [("access_token", 1)],
         {"unique": True, "sparse": True, "name": "submissions_access_token_unique"}),
        ("applications", [("access_token", 1)],
         {"unique": True, "sparse": True, "name": "applications_access_token_unique"}),
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

COMMISSION_OPTIONS = ["10%", "15%", "20%", "25%", "30%"]
MATERIAL_CATEGORIES = {"script", "image", "audio", "video_file"}
MAX_VIDEO_FILE_BYTES = 100 * 1024 * 1024  # 100 MB
# Submission media slots
#   intro_video      — single slot
#   take             — NEW renamable takes, up to MAX_SUBMISSION_TAKES (carries `label`)
#   take_1/take_2/take_3 — LEGACY fixed slots (read-only back-compat; auto-labelled "Take N")
#   image            — generic portfolio images (MIN/MAX_SUBMISSION_IMAGES bounds)
#   indian / western — look-specific portfolio images (Phase 2 schema unification)
SUBMISSION_UPLOAD_CATEGORIES = {"intro_video", "take", "take_1", "take_2", "take_3", "image", "indian", "western"}
LEGACY_TAKE_CATEGORIES = {"take_1", "take_2", "take_3"}
PORTFOLIO_IMAGE_CATEGORIES = {"image", "indian", "western"}
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
    url: str
    public_id: Optional[str] = None
    resource_type: Optional[str] = None
    content_type: str
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


class DownloadIn(BaseModel):
    talent_id: str
    media_id: str


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
        "min_audition_takes": 0,
        "portfolio": {
            "indian": 0,
            "western": 0,
            "image": 0
        },
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



class SubmissionStartIn(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    alternate_contact_number: Optional[str] = None
    form_data: Optional[Dict[str, Any]] = None


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
    # { "<talent_media_id>": {"client_visible": bool, "internal_only": bool} }.
    # Lets a recruiter apply the SAME Client/Hidden/Internal model to talent
    # portfolio media without duplicating the media onto the submission.
    talent_media_visibility: Optional[Dict[str, Any]] = None
    restore_revision_id: Optional[str] = None
    regenerate_snapshot: Optional[bool] = None


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
    
    if is_video and m.get("public_id"):
        url = stream_video_url(m["public_id"]) or url

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

    out: Dict[str, Any] = {
        "id": talent["id"],
        "name": talent.get("name"),
        "media": filtered_media,
        "cover_media_id": cover_mid,
        "image_url": _resolve_cover_url({"media": filtered_media, "cover_media_id": cover_mid}) or None,
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
    if v.get("languages") and talent.get("languages"):
        out["languages"] = talent["languages"]
    if (v.get("instagram_handle") or v.get("instagram")) and talent.get("instagram_handle"):
        out["instagram_handle"] = talent["instagram_handle"]
    if v.get("instagram_followers") and talent.get("instagram_followers"):
        out["instagram_followers"] = talent["instagram_followers"]
    if v.get("work_links") and talent.get("work_links"):
        out["work_links"] = talent["work_links"]
    if v.get("skills") and talent.get("skills"):
        out["skills"] = talent["skills"]
    if v.get("special_abilities") and talent.get("special_abilities"):
        out["special_abilities"] = talent["special_abilities"]
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


def _submission_to_client_shape(sub: dict, project: Optional[dict] = None) -> dict:
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
    if sub.get("client_package_snapshot"):
        snap = sub["client_package_snapshot"]
        ca_snap = snap.get("custom_answers")

        # If snapshot already has correct array format with data, return it as-is.
        if isinstance(ca_snap, list) and len(ca_snap) > 0:
            return snap

        # Snapshot exists but custom_answers are absent OR in legacy dict format.
        # Always attempt to rebuild from live form_data so that:
        #   (a) snapshots frozen before custom questions existed get enriched, and
        #   (b) legacy dict-format answers ({uuid: answer}) are converted to arrays.
        fd_for_resolve = sub.get("form_data") or {}
        fv_for_resolve = {**DEFAULT_FIELD_VISIBILITY, **(sub.get("field_visibility") or {})}
        raw_answers_snap = fd_for_resolve.get("custom_answers") or {}

        # Only rebuild when there are actual answers and visibility is enabled.
        if isinstance(raw_answers_snap, dict) and raw_answers_snap and fv_for_resolve.get("custom_answers"):
            q_text_by_id_snap: Dict[str, str] = {}
            project_cqs_snap = (project or {}).get("custom_questions") or []
            for cq in project_cqs_snap:
                qid = cq.get("id") or ""
                qtext = (cq.get("question") or "").strip()
                if qid and qtext:
                    q_text_by_id_snap[qid] = qtext
            ordered_ids_snap = (
                [cq.get("id") for cq in project_cqs_snap if cq.get("id")]
                if project_cqs_snap else list(raw_answers_snap.keys())
            )
            ca_vis_snap = fv_for_resolve.get("custom_answers")
            filtered_snap: List[Dict[str, str]] = []
            seen_snap: set = set()
            for q_id in ordered_ids_snap:
                if q_id not in raw_answers_snap:
                    continue
                if isinstance(ca_vis_snap, dict) and not ca_vis_snap.get(q_id):
                    continue
                ans = str(raw_answers_snap[q_id] or "").strip()
                if ans:
                    filtered_snap.append({"question": q_text_by_id_snap.get(q_id) or q_id, "answer": ans})
                seen_snap.add(q_id)
            for q_id, a in raw_answers_snap.items():
                if q_id in seen_snap:
                    continue
                if isinstance(ca_vis_snap, dict) and not ca_vis_snap.get(q_id):
                    continue
                ans = str(a or "").strip()
                if ans:
                    filtered_snap.append({"question": q_text_by_id_snap.get(q_id) or q_id, "answer": ans})
            if filtered_snap:
                return {**snap, "custom_answers": filtered_snap}

        # No custom answers to inject — return snapshot as-is.
        return snap

    fd = sub.get("form_data") or {}
    # Merge defaults with stored visibility so newly added keys (e.g.
    # competitive_brand, custom_answers) inherit safe defaults for
    # submissions created before those keys existed.
    fv = {**DEFAULT_FIELD_VISIBILITY, **(sub.get("field_visibility") or {})}

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
            mapped = {**m, "category": "portfolio"}
            image_items.append(mapped)
            if not cover_mid:
                cover_mid = mapped.get("id")
        elif cat == "indian":
            # Phase 3 — preserve Indian-look images as a distinct section so
            # the client view can render Indian / Western / Portfolio
            # buckets independently. Previously these were silently dropped
            # because _submission_to_client_shape only handled `image`.
            image_items.append({**m, "category": "indian"})
            if not cover_mid:
                cover_mid = m.get("id")
        elif cat == "western":
            image_items.append({**m, "category": "western"})
            if not cover_mid:
                cover_mid = m.get("id")
        elif cat == "intro_video":
            intro_items.append({**m, "category": "video"})
        elif cat in LEGACY_TAKE_CATEGORIES or cat == "take":
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
        "languages": fd.get("languages") if fv.get("languages") else [],
        "skills": fd.get("skills") if fv.get("skills") else [],
        "special_abilities": fd.get("special_abilities") if fv.get("special_abilities") else None,
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
    """Compile an immutable copy of the client-facing shape along with recruiter metadata."""
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
# Audition-only categories (intro_video, take, take_*) are project-scoped
# and intentionally NOT mirrored.
SYNC_TO_GLOBAL_CATEGORIES = {
    "image", "portfolio", "indian", "western", "video", "intro_video", "headshot", "headshots", "additional_portfolio"
}


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
        "additional_portfolio": "additional_portfolio"
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
    for m in (talent.get("media") or []):
        if (pub_id and m.get("public_id") == pub_id) or \
           (url and m.get("url") == url) or \
           (m.get("source_submission_media_id") == source_id) or \
           (m.get("source_application_media_id") == source_id):
            return

    # Build the mirror item — preserves Cloudinary url + public_id so the
    # global profile renders identically to the submission/application. New `id` is
    # generated to keep talent.media ids unique across mirror sources.
    is_app = media.get("scope") == "application" or "application_id" in media
    mirror = {
        "id": str(uuid.uuid4()),
        "category": mapped_cat,
        "url": url,
        "public_id": pub_id,
        "resource_type": media.get("resource_type"),
        "mime": media.get("mime"),
        "content_type": media.get("content_type"),
        "size": media.get("size"),
        "created_at": media.get("created_at") or _now(),
        "scope": "talent",
    }
    if is_app:
        mirror["source_application_id"] = submission.get("id")
        mirror["source_application_media_id"] = source_id
    else:
        mirror["source_submission_id"] = submission.get("id")
        mirror["source_submission_media_id"] = source_id

    if mapped_cat == "video":
        await db.talents.update_one(
            {"id": talent["id"]},
            {"$pull": {"media": {"category": "video"}}}
        )

    await db.talents.update_one(
        {"id": talent["id"]},
        {"$push": {"media": mirror}, "$set": {"updated_at": _now()}},
    )
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
    "id", "email", "normalized_email", "created_at", "source", 
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


async def merge_talent_profile(existing_talent: dict, incoming_data: dict, source: str) -> dict:
    """
    Implements Task 4 (Field-level merge policy) and Task 6 (Profile update audit trail).
    Merges incoming data into existing talent record.
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

    # 1. AUTO_UPDATE_FIELDS
    for field in AUTO_UPDATE_FIELDS:
        incoming_val = incoming_data.get(field)
        if incoming_val not in (None, "", [], {}):
            existing_val = existing_talent.get(field)
            if existing_val != incoming_val:
                update_patch[field] = incoming_val
                changed_fields.append(field)
                old_values[field] = existing_val
                new_values[field] = incoming_val

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
    if _r2_client is None:
        import boto3
        from botocore.config import Config
        _r2_client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT_URL,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4"),
        )
    return _r2_client

def generate_r2_presigned_url(key: str, method: str = "PUT", expiry: int = 3600) -> str:
    """Generate a pre-signed S3 URL for Cloudflare R2 operations (PUT or GET)."""
    s3 = get_r2_client()
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
):
    """
    Abstractions wrapper that delegates video processing to the active VideoProvider.
    """
    from providers import get_video_provider
    provider = get_video_provider()
    logger.info(f"[VideoProvider] Delegating transcode to {provider.__class__.__name__} for media_id={media_id}")
    
    res = await provider.create_processing_job(
        parent_id=parent_id,
        media_id=media_id,
        category=category,
        scope=scope,
        r2_url=r2_url,
        folder=folder,
        public_id=public_id,
        label=label,
        eager_transformation=eager_transformation
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





