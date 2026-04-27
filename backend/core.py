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
import requests
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
EMERGENT_KEY = os.environ.get("EMERGENT_LLM_KEY")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@talentgram.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Admin@123")

STORAGE_URL = "https://integrations.emergentagent.com/objstore/api/v1/storage"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("talentgram")

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
mongo_client = AsyncIOMotorClient(MONGO_URL)
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
# Storage
# --------------------------------------------------------------------------
_storage_key: Optional[str] = None


def init_storage() -> Optional[str]:
    global _storage_key
    if _storage_key:
        return _storage_key
    try:
        resp = requests.post(
            f"{STORAGE_URL}/init",
            json={"emergent_key": EMERGENT_KEY},
            timeout=30,
        )
        resp.raise_for_status()
        _storage_key = resp.json()["storage_key"]
        logger.info("Storage initialized")
    except Exception as e:
        logger.error(f"Storage init failed: {e}")
    return _storage_key


def put_object(path: str, data: bytes, content_type: str) -> dict:
    key = init_storage()
    if not key:
        raise HTTPException(status_code=503, detail="Storage unavailable")
    resp = requests.put(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key, "Content-Type": content_type},
        data=data,
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()


def resize_image_bytes(data: bytes, max_width: int = 1600, quality: int = 85) -> Optional[bytes]:
    """Generate a display-optimised JPEG copy of a source image.

    Returns the JPEG bytes if the source is a decodable image, or ``None`` if
    the bytes are not a valid image (in which case callers should skip the
    resize step and keep only the original). Width is capped at ``max_width``;
    taller portraits preserve aspect ratio. Animated sources (GIF/WEBP) are
    flattened to the first frame — acceptable for portfolio thumbnails.
    """
    try:
        from PIL import Image  # local import keeps server startup light
        import io as _io
        img = Image.open(_io.BytesIO(data))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if w > max_width:
            new_h = int(h * (max_width / float(w)))
            img = img.resize((max_width, new_h), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning("resize_image_bytes skipped: %s", e)
        return None


def get_object(path: str) -> tuple[bytes, str]:
    """Buffered fetch — kept for small payloads and back-compat. For large
    media (video), prefer `stream_object` which never loads the full body."""
    key = init_storage()
    if not key:
        raise HTTPException(status_code=503, detail="Storage unavailable")
    resp = requests.get(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key},
        timeout=120,
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="File not found")
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")


def stream_object(path: str, range_header: Optional[str] = None):
    """Stream a file from Emergent Object Store to the client.

    The upstream Emergent Object Store currently ignores the `Range` header
    and always returns `200 OK` with the full body. That breaks Safari's
    `<video>` element (which refuses to play unless it sees `206 Partial
    Content`) and breaks seek interactions in every browser (each seek
    re-downloads the entire file).

    This function therefore does **server-side range slicing** when the
    client requested a range and upstream replied with 200:

      • Parse the `Range` header (`bytes=START-END`, `bytes=START-`, `bytes=-N`).
      • Stream the upstream body chunk-by-chunk and yield only the requested
        slice. Memory cost is bounded by the chunk size (256 KB).
      • Respond with `206 Partial Content` + a proper `Content-Range` header
        and a `Content-Length` equal to the slice length.

    If upstream actually does honor the Range (returns 206), we forward
    that response unchanged.

    `Cache-Control` is *force-set* (not setdefault) to override the
    `no-store, no-cache, must-revalidate` that Cloudflare-fronted upstream
    sends — that header was forcing the browser to refetch the entire file
    every play, compounding the no-Range pain.

    Returns `(iterator, headers, status_code)`.
    """
    key = init_storage()
    if not key:
        raise HTTPException(status_code=503, detail="Storage unavailable")

    upstream_headers = {"X-Storage-Key": key}
    if range_header:
        upstream_headers["Range"] = range_header

    resp = requests.get(
        f"{STORAGE_URL}/objects/{path}",
        headers=upstream_headers,
        stream=True,
        timeout=600,
    )
    if resp.status_code == 404:
        resp.close()
        raise HTTPException(status_code=404, detail="File not found")
    resp.raise_for_status()

    upstream_status = resp.status_code
    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    upstream_len_str = resp.headers.get("Content-Length")
    try:
        upstream_len = int(upstream_len_str) if upstream_len_str else None
    except ValueError:
        upstream_len = None
    etag = resp.headers.get("ETag")
    last_modified = resp.headers.get("Last-Modified")

    # ── Case 1 ─ Upstream honored the Range and returned 206. Pass through.
    if upstream_status == 206:
        passthrough: Dict[str, str] = {
            k: v
            for k, v in resp.headers.items()
            if k.lower()
            in (
                "content-type",
                "content-length",
                "content-range",
                "etag",
                "last-modified",
            )
        }
        passthrough["Accept-Ranges"] = "bytes"
        passthrough["Cache-Control"] = "public, max-age=86400"

        def _passthrough_iter():
            try:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        yield chunk
            finally:
                resp.close()

        return _passthrough_iter(), passthrough, 206

    # ── Case 2 ─ Range was requested but upstream returned 200 + full body.
    #             Slice locally and synthesize a proper 206 response.
    if range_header and upstream_status == 200 and upstream_len is not None:
        m = re.match(r"^\s*bytes=(\d*)-(\d*)\s*$", range_header)
        if m:
            start_s, end_s = m.group(1), m.group(2)
            if start_s == "" and end_s == "":
                start, end = None, None  # malformed
            elif start_s == "":
                # Suffix range — last N bytes.
                n = int(end_s)
                start = max(0, upstream_len - n)
                end = upstream_len - 1
            elif end_s == "":
                start = int(start_s)
                end = upstream_len - 1
            else:
                start = int(start_s)
                end = int(end_s)

            # Validate. If unsatisfiable, RFC requires 416 with a
            # `Content-Range: bytes */<size>` header.
            if start is None or start >= upstream_len:
                resp.close()
                raise HTTPException(
                    status_code=416,
                    detail="Requested range not satisfiable",
                    headers={"Content-Range": f"bytes */{upstream_len}"},
                )

            # Clamp upper bound to the actual file size.
            start = max(0, start)
            end = max(start, min(end, upstream_len - 1))
            slice_len = end - start + 1

            def _sliced_iter():
                """Yield only bytes [start, end] from the streamed upstream body.

                Walks the chunk stream once, drops chunks fully before the
                window, slices chunks that overlap the window edges, and
                stops as soon as the window is satisfied.
                """
                consumed = 0  # bytes seen so far (exclusive of current chunk)
                try:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        chunk_start = consumed
                        chunk_end = consumed + len(chunk)  # exclusive
                        consumed = chunk_end
                        if chunk_end <= start:
                            continue
                        if chunk_start > end:
                            break
                        lo = max(0, start - chunk_start)
                        hi = min(len(chunk), end + 1 - chunk_start)
                        out = chunk[lo:hi]
                        if out:
                            yield out
                        if consumed > end:
                            break
                finally:
                    resp.close()

            headers = {
                "Content-Type": content_type,
                "Content-Length": str(slice_len),
                "Content-Range": f"bytes {start}-{end}/{upstream_len}",
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=86400",
            }
            if etag:
                headers["ETag"] = etag
            if last_modified:
                headers["Last-Modified"] = last_modified
            return _sliced_iter(), headers, 206

    # ── Case 3 ─ Full body (no Range requested OR malformed Range).
    #             We support seeking via slicing now, so we DO advertise
    #             Accept-Ranges: bytes here.
    headers = {
        "Content-Type": content_type,
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=86400",
    }
    if upstream_len is not None:
        headers["Content-Length"] = str(upstream_len)
    if etag:
        headers["ETag"] = etag
    if last_modified:
        headers["Last-Modified"] = last_modified

    def _full_iter():
        try:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return _full_iter(), headers, 200


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


def enrich_talent(doc: Optional[dict]) -> Optional[dict]:
    """If dob is set, always derive age from it (overrides any stored age)."""
    if not doc:
        return doc
    dob = doc.get("dob")
    if dob:
        computed = compute_age(dob)
        if computed is not None:
            doc["age"] = computed
    return doc


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
        except Exception:
            pass
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
        # Phase 0: enforce one application per email.
        ("applications", [("talent_email", 1)],
         {"unique": True, "name": "applications_email_unique"}),
        ("links", [("slug", 1)], {"unique": True, "name": "slug_unique"}),
        ("link_views", [("link_id", 1), ("created_at", -1)], {}),
        ("link_actions", [("link_id", 1), ("viewer_email", 1)], {}),
        ("projects", [("slug", 1)], {"unique": True, "name": "proj_slug_unique"}),
    ]
    for coll, keys, opts in p0_indexes:
        try:
            await db[coll].create_index(keys, **opts)
        except Exception as e:
            logger.warning(f"{coll} index {keys}: {e}")

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
    "custom_answers": False,     # opt-in — typed answers may contain PII
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
# Public audition upload size cap: 150 MB for videos (intro/take), 25 MB for images.
# Enforced server-side to protect against accidental/malicious bloat.
MAX_SUBMISSION_VIDEO_BYTES = 150 * 1024 * 1024
MAX_SUBMISSION_IMAGE_BYTES = 25 * 1024 * 1024
# Target width when generating the display-optimised JPEG copy.
IMAGE_RESIZE_MAX_WIDTH = 1600
SUBMISSION_DECISIONS = {"pending", "approved", "rejected", "hold"}
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
    "media",
    # IDs needed for the moderated client→talent feedback relay. These are
    # NOT sensitive — they're foreign keys clients must round-trip back when
    # POSTing feedback. Empty/None for pure talent-share (M1) cards.
    "submission_id",
    "project_id",
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
    storage_path: str
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


class SubmissionDecisionIn(BaseModel):
    decision: str


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
    """Strip internal scope metadata (project_id / submission_id / talent_id / scope) before sending to client."""
    out = {
        "id": m.get("id"),
        "category": m.get("category"),
        "storage_path": m.get("storage_path"),
        "content_type": m.get("content_type"),
        "original_filename": m.get("original_filename"),
        "size": m.get("size", 0),
        "created_at": m.get("created_at"),
    }
    # Display-optimised 1600px JPEG variant (images only). Frontend prefers this
    # path for the portfolio view; the original remains available for download.
    if m.get("resized_storage_path"):
        out["resized_storage_path"] = m["resized_storage_path"]
    if m.get("label"):
        out["label"] = m["label"]
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


def _submission_to_client_shape(sub: dict) -> dict:
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
    """
    fd = sub.get("form_data") or {}
    # Merge defaults with stored visibility so newly added keys (e.g.
    # competitive_brand, custom_answers) inherit safe defaults for
    # submissions created before those keys existed.
    fv = {**DEFAULT_FIELD_VISIBILITY, **(sub.get("field_visibility") or {})}

    fn = (fd.get("first_name") or "").strip()
    ln = (fd.get("last_name") or "").strip()
    name = f"{fn} {ln}".strip() or sub.get("talent_name") or "Unnamed"

    age: Optional[int] = None
    if fv.get("age") and fd.get("age") not in (None, ""):
        try:
            age = int(fd["age"])
        except Exception:
            age = None

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

    # Sort legacy takes by category (take_1→take_2→take_3); new `take` items by created_at
    raw_media = sub.get("media") or []
    for m in raw_media:
        cat = m.get("category")
        if cat == "image":
            mapped = {**m, "category": "portfolio"}
            image_items.append(mapped)
            if not cover_mid:
                cover_mid = mapped.get("id")
        elif cat == "intro_video":
            intro_items.append({**m, "category": "video"})
        elif cat in LEGACY_TAKE_CATEGORIES or cat == "take":
            take_items.append({
                **m,
                "category": "take",
                "label": _take_label(m),
            })

    # Deterministic order inside takes: legacy first (take_1/2/3), then new takes by created_at
    def _take_sort_key(m: dict):
        # Pull original legacy ordering hint
        raw_cat = next(
            (rm.get("category") for rm in raw_media if rm.get("id") == m.get("id")),
            "take",
        )
        legacy_order = {"take_1": 0, "take_2": 1, "take_3": 2}
        return (
            legacy_order.get(raw_cat, 10),
            m.get("created_at") or "",
        )

    take_items.sort(key=_take_sort_key)

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
        "media": media,
    }

    # Competitive brand — only when explicitly enabled.
    if fv.get("competitive_brand"):
        cb = (fd.get("competitive_brand") or "").strip()
        if cb:
            out["competitive_brand"] = cb

    # Custom answers — support both bool and per-question dict shapes.
    raw_answers = fd.get("custom_answers") or {}
    if isinstance(raw_answers, dict) and raw_answers:
        ca_vis = fv.get("custom_answers")
        if ca_vis:
            filtered: List[Dict[str, str]] = []
            for q, a in raw_answers.items():
                if isinstance(ca_vis, dict):
                    if not ca_vis.get(q):
                        continue
                # else (bool True) — include all non-empty answers
                ans = str(a or "").strip()
                if ans:
                    filtered.append({"question": str(q), "answer": ans})
            if filtered:
                out["custom_answers"] = filtered

    return out


def _paginate_params(page, size):
    """Normalise + cap `?page=&size=` query params.

    Returns `(skip, limit, page, size)` where:
    - page is 0-indexed
    - size is clamped to [1, 200]
    - skip = page * size
    """
    p = max(0, int(page or 0))
    s = max(1, min(200, int(size or 50)))
    return p * s, s, p, s


def _paginated(items, total, page, size) -> dict:
    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
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
