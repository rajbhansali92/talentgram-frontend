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
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret")
APP_NAME = os.environ.get("APP_NAME", "talentgram")
ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

# --------------------------------------------------------------------------
# Cloudinary — primary (and only) media storage as of v37m migration.
# --------------------------------------------------------------------------
import cloudinary  # noqa: E402
import cloudinary.uploader  # noqa: E402
import cloudinary.utils  # noqa: E402

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
    if not data or data.get("role") != "viewer":
        return None
    return data


def decode_submitter(authorization: Optional[str]) -> Optional[Dict[str, Any]]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1]
    data = decode_token(token)
    if not data or data.get("role") != "submitter":
        return None
    return data


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
    """Upload raw bytes to Cloudinary and return the upload result.

    Result includes `secure_url`, `public_id`, `resource_type`, `bytes`, and
    `format`. Raises HTTPException on validation failure or Cloudinary error.

    `resource_type="auto"` lets Cloudinary detect image vs video vs raw
    automatically — appropriate for our mixed audition uploads.
    """
    _validate_folder(folder)
    ct = (content_type or "").lower()
    
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
    Uses: q_auto:good, vc_auto, f_auto
    """
    if not public_id:
        return None
    if public_id.startswith(("http://", "https://")):
        return public_id
    url, _ = cloudinary.utils.cloudinary_url(
        public_id,
        resource_type="video",
        secure=True,
        quality="auto:good",
        video_codec="auto",
        fetch_format="auto"
    )
    return url


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


def _slugify(title: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return (safe or "link") + "-" + uuid.uuid4().hex[:6]


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
    "competitive_brand": False,  # opt-in — brand conflicts are sensitive
    "availability": True,
    # Budget defaults to visible at submission level. Link-level `visibility.budget`
    # is still the final gate for what each client sees — this just stops the
    # per-submission layer from silently dropping budget before the link can
    # decide. (Without this, admins toggle "Budget" ON at the link level and
    # still see nothing reach the client.)
    "budget": True,
    "custom_answers": True,      # on by default — admin-configured questions are intentional
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


class TalentIn(BaseModel):
    name: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    age: Optional[int] = None
    dob: Optional[str] = None
    height: Optional[str] = None
    location: Optional[str] = None
    ethnicity: Optional[str] = None
    gender: Optional[str] = None
    instagram_handle: Optional[str] = None
    instagram_followers: Optional[str] = None
    bio: Optional[str] = None
    work_links: List[str] = Field(default_factory=list)
    cover_media_id: Optional[str] = None
    # Public: self-selected work categories (set during onboarding /apply)
    interested_in: List[str] = Field(default_factory=list)
    # Internal: admin-assigned structured tags [{"id": uuid, "name": label}]
    tags: List[Dict[str, str]] = Field(default_factory=list)


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
    status: str = "ongoing"



class SubmissionStartIn(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    form_data: Optional[Dict[str, Any]] = None


class SubmissionUpdateIn(BaseModel):
    form_data: Optional[Dict[str, Any]] = None


class AdminSubmissionEditIn(BaseModel):
    form_data: Optional[Dict[str, Any]] = None
    # Value is `bool` for most fields, OR a `{question_label: bool}` dict for
    # `custom_answers` to support per-question visibility.
    field_visibility: Optional[Dict[str, Any]] = None
    media: Optional[List[Dict[str, Any]]] = None # Custom media curated settings & ordering
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
    if v.get("ethnicity") and talent.get("ethnicity"):
        out["ethnicity"] = talent["ethnicity"]
    if v.get("instagram") and talent.get("instagram_handle"):
        out["instagram_handle"] = talent["instagram_handle"]
    if v.get("instagram_followers") and talent.get("instagram_followers"):
        out["instagram_followers"] = talent["instagram_followers"]
    if v.get("work_links") and talent.get("work_links"):
        out["work_links"] = talent["work_links"]
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
        
        # If snapshot already has the new array format, or there are no custom answers to process, return the snapshot
        if isinstance(ca_snap, list) and len(ca_snap) > 0:
            return snap
            
        if not ca_snap and not (sub.get("form_data") or {}).get("custom_answers"):
            return snap
        # Snapshot exists but custom_answers are missing — resolve them now
        # and merge into a copy of the snapshot so the rest of the function
        # can return the enriched shape without touching the stored document.
        fd_for_resolve = sub.get("form_data") or {}
        fv_for_resolve = {**DEFAULT_FIELD_VISIBILITY, **(sub.get("field_visibility") or {})}
        raw_answers_snap = fd_for_resolve.get("custom_answers") or {}
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
SYNC_TO_GLOBAL_CATEGORIES = {"image", "indian", "western"}


async def sync_media_to_global_talent(submission: dict, media: dict) -> None:
    """Mirror a submission's image media into the global talent record.

    No-op when:
      - submission has no `talent_email` (anonymous draft)
      - media category is not in whitelisted categories
      - the same source-id has already been mirrored (idempotent)
      - no talent record exists for that email yet (will sync on next upload)
    """
    cat_mapping = {
        "image": "portfolio",
        "indian": "indian",
        "western": "western",
        "video": "video",
        "intro_video": "video"
    }
    cat = media.get("category")
    if cat not in cat_mapping:
        return
    mapped_cat = cat_mapping[cat]
    email = (submission.get("talent_email") or "").lower().strip()
    if not email:
        return
    source_id = media.get("id")
    if not source_id:
        return

    talent = await db.talents.find_one({"email": email})
    if not talent:
        return

    # Strict deduplication: check if this media asset already exists by public_id, url, or source-id
    pub_id = media.get("public_id")
    url = media.get("url")
    for m in (talent.get("media") or []):
        if (pub_id and m.get("public_id") == pub_id) or \
           (url and m.get("url") == url) or \
           (m.get("source_submission_media_id") == source_id):
            return

    # Build the mirror item — preserves Cloudinary url + public_id so the
    # global profile renders identically to the submission. New `id` is
    # generated to keep talent.media ids unique across mirror sources.
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
        "source_submission_id": submission.get("id"),
        "source_submission_media_id": source_id,
    }

    await db.talents.update_one(
        {"id": talent["id"]},
        {"$push": {"media": mirror}},
    )
    await update_talent_cover_cache(talent["id"])


async def remove_synced_media_from_global_talent(submission: dict, source_media_id: str) -> None:
    """Remove the mirrored copy of a submission media from the global talent.

    No-op when no mirror exists. Called from the submission media-delete
    endpoint so the global profile stays in sync.
    """
    email = (submission.get("talent_email") or "").lower().strip()
    if not email or not source_media_id:
        return
    await db.talents.update_one(
        {"email": email},
        {"$pull": {"media": {"source_submission_media_id": source_media_id}}},
    )
    talent = await db.talents.find_one({"email": email}, {"id": 1})
    if talent:
        await update_talent_cover_cache(talent["id"])

