"""Shared primitives: config, DB, storage, auth, utils, constants, models, visibility filters.

Everything that multiple routers need lives here to keep router modules pure of plumbing.
"""
from __future__ import annotations

import logging
import os
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


async def current_admin(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
) -> Dict[str, Any]:
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = decode_token(credentials.credentials)
    if not data or data.get("role") != "admin":
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await db.admins.find_one({"email": data.get("email")}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="Admin not found")
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


def get_object(path: str) -> tuple[bytes, str]:
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
    existing = await db.admins.find_one({"email": ADMIN_EMAIL})
    if existing:
        return
    await db.admins.insert_one({
        "id": str(uuid.uuid4()),
        "email": ADMIN_EMAIL,
        "name": "Talentgram Admin",
        "password_hash": hash_password(ADMIN_PASSWORD),
        "created_at": _now(),
    })
    logger.info(f"Seeded admin {ADMIN_EMAIL}")


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
    "competitive_brand": False,
    "availability": True,
    "budget": False,
    "custom_answers": False,
}

COMMISSION_OPTIONS = ["10%", "15%", "20%", "25%", "30%"]
MATERIAL_CATEGORIES = {"script", "image", "audio", "video_file"}
MAX_VIDEO_FILE_BYTES = 100 * 1024 * 1024  # 100 MB
SUBMISSION_UPLOAD_CATEGORIES = {"intro_video", "take_1", "take_2", "take_3", "image"}
MAX_SUBMISSION_IMAGES = 8
MIN_SUBMISSION_IMAGES = 5
SUBMISSION_DECISIONS = {"pending", "approved", "rejected"}

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
    "availability",  # structured: {status: "yes"|"no", note?: str}
    "budget",        # structured: {status: "accept"|"custom", value?: str}
    "cover_media_id",
    "media",
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
    talent_ids: List[str] = Field(default_factory=list)
    submission_ids: List[str] = Field(default_factory=list)
    visibility: Dict[str, bool] = Field(default_factory=lambda: DEFAULT_VISIBILITY.copy())
    is_public: bool = True
    password: Optional[str] = None
    notes: Optional[str] = None


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


class SubmissionStartIn(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    form_data: Optional[Dict[str, Any]] = None


class SubmissionUpdateIn(BaseModel):
    form_data: Optional[Dict[str, Any]] = None


class AdminSubmissionEditIn(BaseModel):
    form_data: Optional[Dict[str, Any]] = None
    field_visibility: Optional[Dict[str, bool]] = None


class SubmissionDecisionIn(BaseModel):
    decision: str


class ForwardToLinkIn(BaseModel):
    submission_ids: List[str]
    visibility: Dict[str, bool] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Visibility / client payload filters
# --------------------------------------------------------------------------
def _public_media(m: dict) -> dict:
    """Strip internal scope metadata (project_id / submission_id / talent_id / scope) before sending to client."""
    return {
        "id": m.get("id"),
        "category": m.get("category"),
        "storage_path": m.get("storage_path"),
        "content_type": m.get("content_type"),
        "original_filename": m.get("original_filename"),
        "size": m.get("size", 0),
        "created_at": m.get("created_at"),
    }


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
    """Flatten a submission document into the shape clients expect for a talent.
    Respects submission-level field_visibility; takes are always dropped."""
    fd = sub.get("form_data") or {}
    fv = sub.get("field_visibility") or {**DEFAULT_FIELD_VISIBILITY}

    fn = (fd.get("first_name") or "").strip()
    ln = (fd.get("last_name") or "").strip()
    name = f"{fn} {ln}".strip() or sub.get("talent_name") or "Unnamed"

    age: Optional[int] = None
    if fv.get("age") and fd.get("age") not in (None, ""):
        try:
            age = int(fd["age"])
        except Exception:
            age = None

    media: List[dict] = []
    cover_mid: Optional[str] = None
    # Preserve deterministic order: intro first, takes 1→2→3, then images
    intro_items: List[dict] = []
    take_items: Dict[str, dict] = {}
    image_items: List[dict] = []
    for m in sub.get("media") or []:
        cat = m.get("category")
        if cat == "image":
            mapped = {**m, "category": "portfolio"}
            image_items.append(mapped)
            if not cover_mid:
                cover_mid = mapped.get("id")
        elif cat == "intro_video":
            intro_items.append({**m, "category": "video"})
        elif cat in ("take_1", "take_2", "take_3"):
            # Keep original take_N category so client can label them
            take_items[cat] = m
    # Deterministic order
    media.extend(intro_items)
    for key in ("take_1", "take_2", "take_3"):
        if key in take_items:
            media.append(take_items[key])
    media.extend(image_items)

    return {
        "id": sub["id"],
        "name": name,
        "age": age,
        "height": fd.get("height") if fv.get("height") else None,
        "location": fd.get("location") if fv.get("location") else None,
        "ethnicity": None,
        "instagram_handle": None,
        "instagram_followers": None,
        "work_links": [],
        "availability": (fd.get("availability") if fv.get("availability") else None),
        "budget": (fd.get("budget") if fv.get("budget") else None),
        "cover_media_id": cover_mid,
        "media": media,
    }


def _public_project(project: dict) -> dict:
    """Strip internal/private fields before returning project info publicly."""
    return {k: v for k, v in project.items() if k not in {"_id", "created_by"}}


def _clean_ids(ids: List[str]) -> List[str]:
    """Strip empty strings and deduplicate while preserving order."""
    seen = set()
    out: List[str] = []
    for i in ids or []:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out
