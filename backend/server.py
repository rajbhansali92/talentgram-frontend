import os
import uuid
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import (
    FastAPI, APIRouter, HTTPException, Depends, UploadFile, File, Form,
    Header, Query, Response
)
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field


# --------------------------------------------------------------------------
# Bootstrap
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

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("talentgram")

app = FastAPI(title="Talentgram Portfolio Engine")
api = APIRouter(prefix="/api")
bearer = HTTPBearer(auto_error=False)

# --------------------------------------------------------------------------
# Storage helpers
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
        data=data, timeout=180,
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
# Auth helpers
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    token: str
    admin: Dict[str, Any]


class MediaItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    category: str  # indian | western | portfolio | video
    storage_path: str
    content_type: str
    original_filename: Optional[str] = None
    size: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class TalentIn(BaseModel):
    name: str
    age: Optional[int] = None
    dob: Optional[str] = None  # ISO "YYYY-MM-DD"; if present, age is derived
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


DEFAULT_VISIBILITY = {
    "portfolio": True,
    "intro_video": True,
    "instagram": True,
    "instagram_followers": True,
    "age": True,
    "height": True,
    "location": True,
    "ethnicity": True,
    "work_links": True,
    "budget_form": False,
    "download": False,
}


class LinkIn(BaseModel):
    title: str  # "Talentgram x Nike" etc
    brand_name: Optional[str] = None
    talent_ids: List[str] = Field(default_factory=list)
    submission_ids: List[str] = Field(default_factory=list)
    visibility: Dict[str, bool] = Field(default_factory=lambda: DEFAULT_VISIBILITY.copy())
    is_public: bool = True
    password: Optional[str] = None  # if private, still need identity gate anyway
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


# ---- Projects (audition engine) ----
class ProjectIn(BaseModel):
    brand_name: str
    brand_link: Optional[str] = None
    character: Optional[str] = None
    shoot_dates: Optional[str] = None
    budget_per_day: Optional[str] = None
    commission_percent: Optional[str] = None  # e.g. "15%"
    medium_usage: Optional[str] = None
    director: Optional[str] = None
    production_house: Optional[str] = None
    additional_details: Optional[str] = None
    video_links: List[str] = Field(default_factory=list)
    competitive_brand_enabled: bool = False
    custom_questions: List[Dict[str, Any]] = Field(default_factory=list)  # [{id, question, type}]


COMMISSION_OPTIONS = ["10%", "15%", "20%", "25%", "30%"]
MATERIAL_CATEGORIES = {"script", "image", "audio"}
SUBMISSION_UPLOAD_CATEGORIES = {"intro_video", "take_1", "take_2", "take_3", "image"}
MAX_SUBMISSION_IMAGES = 8
SUBMISSION_DECISIONS = {"pending", "approved", "rejected"}


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
    decision: str  # pending | approved | rejected


class ForwardToLinkIn(BaseModel):
    submission_ids: List[str]
    visibility: Dict[str, bool] = Field(default_factory=dict)


# Default form-field visibility when forwarding to client
DEFAULT_FIELD_VISIBILITY = {
    "first_name": True,
    "last_name": True,
    "age": True,
    "height": True,
    "location": True,
    "competitive_brand": False,  # internal by default
    "availability": False,  # internal by default
    "budget": False,  # internal by default
    "custom_answers": False,  # internal by default
}


MIN_SUBMISSION_IMAGES = 5


# --------------------------------------------------------------------------
# Helpers
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


async def seed_admin():
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
# Auth routes
# --------------------------------------------------------------------------
@api.post("/auth/login", response_model=TokenOut)
async def login(payload: LoginIn):
    user = await db.admins.find_one({"email": payload.email.lower()})
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = make_token({"email": user["email"], "role": "admin", "id": user["id"]})
    return {
        "token": token,
        "admin": {"email": user["email"], "name": user.get("name"), "id": user["id"]},
    }


@api.get("/auth/me")
async def me(admin: dict = Depends(current_admin)):
    return admin


# --------------------------------------------------------------------------
# Upload + file serving
# --------------------------------------------------------------------------
@api.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    admin: dict = Depends(current_admin),
):
    ext = (file.filename or "bin").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "bin"
    path = f"{APP_NAME}/uploads/{admin['id']}/{uuid.uuid4()}.{ext}"
    data = await file.read()
    result = put_object(path, data, file.content_type or "application/octet-stream")
    return {
        "path": result["path"],
        "size": result.get("size", len(data)),
        "content_type": file.content_type or "application/octet-stream",
        "original_filename": file.filename,
    }


@api.get("/files/{path:path}")
async def download_file(path: str):
    # Files are referenced by UUID paths. Public by design for client portfolio viewing.
    data, content_type = get_object(path)
    return Response(content=data, media_type=content_type, headers={
        "Cache-Control": "public, max-age=86400",
    })


# --------------------------------------------------------------------------
# Talent routes (admin)
# --------------------------------------------------------------------------
@api.post("/talents", response_model=TalentOut)
async def create_talent(payload: TalentIn, admin: dict = Depends(current_admin)):
    doc = payload.model_dump()
    doc.update({
        "id": str(uuid.uuid4()),
        "media": [],
        "created_at": _now(),
        "created_by": admin["id"],
    })
    await db.talents.insert_one(doc)
    doc.pop("_id", None)
    doc.pop("created_by", None)
    return enrich_talent(doc)


@api.get("/talents")
async def list_talents(
    q: Optional[str] = None,
    admin: dict = Depends(current_admin),
):
    query: Dict[str, Any] = {}
    if q:
        query["name"] = {"$regex": q, "$options": "i"}
    talents = await db.talents.find(query, {"_id": 0, "created_by": 0}).sort("created_at", -1).to_list(2000)
    return [enrich_talent(t) for t in talents]


@api.get("/talents/{tid}")
async def get_talent(tid: str, admin: dict = Depends(current_admin)):
    t = await db.talents.find_one({"id": tid}, {"_id": 0, "created_by": 0})
    if not t:
        raise HTTPException(404, "Talent not found")
    return enrich_talent(t)


@api.put("/talents/{tid}", response_model=TalentOut)
async def update_talent(tid: str, payload: TalentIn, admin: dict = Depends(current_admin)):
    update = payload.model_dump()
    res = await db.talents.update_one({"id": tid}, {"$set": update})
    if not res.matched_count:
        raise HTTPException(404, "Talent not found")
    t = await db.talents.find_one({"id": tid}, {"_id": 0, "created_by": 0})
    return enrich_talent(t)


@api.delete("/talents/{tid}")
async def delete_talent(tid: str, admin: dict = Depends(current_admin)):
    res = await db.talents.delete_one({"id": tid})
    if not res.deleted_count:
        raise HTTPException(404, "Talent not found")
    return {"ok": True}


@api.post("/talents/{tid}/media", response_model=TalentOut)
async def add_media(
    tid: str,
    category: str = Form(...),
    file: UploadFile = File(...),
    admin: dict = Depends(current_admin),
):
    if category not in {"indian", "western", "portfolio", "video"}:
        raise HTTPException(400, "Invalid category")
    talent = await db.talents.find_one({"id": tid})
    if not talent:
        raise HTTPException(404, "Talent not found")

    ext = (file.filename or "bin").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "bin"
    path = f"{APP_NAME}/talents/{tid}/{uuid.uuid4()}.{ext}"
    data = await file.read()
    result = put_object(path, data, file.content_type or "application/octet-stream")

    media = {
        "id": str(uuid.uuid4()),
        "category": category,
        "storage_path": result["path"],
        "content_type": file.content_type or "application/octet-stream",
        "original_filename": file.filename,
        "size": result.get("size", len(data)),
        "created_at": _now(),
    }
    await db.talents.update_one({"id": tid}, {"$push": {"media": media}})
    # set cover if none
    if not talent.get("cover_media_id") and category in {"indian", "western", "portfolio"}:
        await db.talents.update_one({"id": tid}, {"$set": {"cover_media_id": media["id"]}})
    t = await db.talents.find_one({"id": tid}, {"_id": 0, "created_by": 0})
    return enrich_talent(t)


@api.delete("/talents/{tid}/media/{mid}")
async def delete_media(tid: str, mid: str, admin: dict = Depends(current_admin)):
    res = await db.talents.update_one({"id": tid}, {"$pull": {"media": {"id": mid}}})
    if not res.modified_count:
        raise HTTPException(404, "Media not found")
    return {"ok": True}


@api.post("/talents/{tid}/cover/{mid}")
async def set_cover(tid: str, mid: str, admin: dict = Depends(current_admin)):
    res = await db.talents.update_one({"id": tid}, {"$set": {"cover_media_id": mid}})
    if not res.matched_count:
        raise HTTPException(404, "Talent not found")
    return {"ok": True}


# --------------------------------------------------------------------------
# Link routes (admin)
# --------------------------------------------------------------------------
@api.post("/links", response_model=LinkOut)
async def create_link(payload: LinkIn, admin: dict = Depends(current_admin)):
    vis = {**DEFAULT_VISIBILITY, **(payload.visibility or {})}
    doc = {
        "id": str(uuid.uuid4()),
        "slug": _slugify(payload.title),
        "title": payload.title,
        "brand_name": payload.brand_name,
        "talent_ids": payload.talent_ids,
        "submission_ids": payload.submission_ids,
        "visibility": vis,
        "is_public": payload.is_public,
        "password": payload.password,
        "notes": payload.notes,
        "created_at": _now(),
        "created_by": admin["id"],
    }
    await db.links.insert_one(doc)
    doc.pop("_id", None)
    doc["view_count"] = 0
    doc["unique_viewers"] = 0
    return doc


@api.get("/links")
async def list_links(admin: dict = Depends(current_admin)):
    links = await db.links.find({}, {"_id": 0}).sort("created_at", -1).to_list(2000)
    # compute analytics counts
    for link in links:
        link["view_count"] = await db.link_views.count_documents({"link_id": link["id"]})
        link["unique_viewers"] = len(await db.link_views.distinct("viewer_email", {"link_id": link["id"]}))
    return links


@api.get("/links/{lid}")
async def get_link(lid: str, admin: dict = Depends(current_admin)):
    link = await db.links.find_one({"id": lid}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    link["view_count"] = await db.link_views.count_documents({"link_id": lid})
    link["unique_viewers"] = len(await db.link_views.distinct("viewer_email", {"link_id": lid}))
    return link


@api.put("/links/{lid}", response_model=LinkOut)
async def update_link(lid: str, payload: LinkIn, admin: dict = Depends(current_admin)):
    vis = {**DEFAULT_VISIBILITY, **(payload.visibility or {})}
    update = payload.model_dump()
    update["visibility"] = vis
    res = await db.links.update_one({"id": lid}, {"$set": update})
    if not res.matched_count:
        raise HTTPException(404, "Link not found")
    link = await db.links.find_one({"id": lid}, {"_id": 0})
    link["view_count"] = await db.link_views.count_documents({"link_id": lid})
    link["unique_viewers"] = len(await db.link_views.distinct("viewer_email", {"link_id": lid}))
    return link


@api.delete("/links/{lid}")
async def delete_link(lid: str, admin: dict = Depends(current_admin)):
    await db.links.delete_one({"id": lid})
    await db.link_views.delete_many({"link_id": lid})
    await db.link_actions.delete_many({"link_id": lid})
    await db.link_downloads.delete_many({"link_id": lid})
    return {"ok": True}


@api.post("/links/{lid}/duplicate", response_model=LinkOut)
async def duplicate_link(lid: str, admin: dict = Depends(current_admin)):
    orig = await db.links.find_one({"id": lid}, {"_id": 0})
    if not orig:
        raise HTTPException(404, "Link not found")
    new = {**orig}
    new["id"] = str(uuid.uuid4())
    new["slug"] = _slugify(orig["title"])
    new["created_at"] = _now()
    await db.links.insert_one(new)
    new.pop("_id", None)
    new["view_count"] = 0
    new["unique_viewers"] = 0
    return new


@api.get("/links/{lid}/results")
async def link_results(lid: str, admin: dict = Depends(current_admin)):
    link = await db.links.find_one({"id": lid}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    viewers = await db.link_views.find({"link_id": lid}, {"_id": 0}).sort("created_at", -1).to_list(5000)
    actions = await db.link_actions.find({"link_id": lid}, {"_id": 0}).sort("updated_at", -1).to_list(10000)
    downloads = await db.link_downloads.find({"link_id": lid}, {"_id": 0}).sort("created_at", -1).to_list(10000)

    # Build subject (talent/submission) registry for name resolution
    t_ids = link.get("talent_ids", []) or []
    s_ids = link.get("submission_ids", []) or []
    subjects: Dict[str, Dict[str, Any]] = {}
    if t_ids:
        for t in await db.talents.find({"id": {"$in": t_ids}}, {"_id": 0, "id": 1, "name": 1, "cover_media_id": 1, "media": 1}).to_list(5000):
            subjects[t["id"]] = {
                "id": t["id"],
                "name": t.get("name"),
                "source": "talent",
                "cover_media_id": t.get("cover_media_id"),
                "media": t.get("media", []),
            }
    if s_ids:
        for s in await db.submissions.find({"id": {"$in": s_ids}}, {"_id": 0}).to_list(5000):
            shape = _submission_to_client_shape(s)
            subjects[s["id"]] = {
                "id": s["id"],
                "name": shape["name"],
                "source": "submission",
                "project_id": s.get("project_id"),
                "cover_media_id": shape.get("cover_media_id"),
                "media": shape.get("media", []),
            }

    ordered_ids = t_ids + s_ids
    summary: Dict[str, Dict[str, Any]] = {
        tid: {"talent_id": tid, "shortlist": 0, "interested": 0, "not_for_this": 0, "not_sure": 0, "comments": []}
        for tid in ordered_ids
    }
    for a in actions:
        tid = a.get("talent_id")
        if tid not in summary:
            summary[tid] = {"talent_id": tid, "shortlist": 0, "interested": 0, "not_for_this": 0, "not_sure": 0, "comments": []}
        act = a.get("action")
        if act in summary[tid]:
            summary[tid][act] += 1
        if a.get("comment"):
            summary[tid]["comments"].append({
                "viewer_email": a.get("viewer_email"),
                "viewer_name": a.get("viewer_name"),
                "comment": a["comment"],
                "updated_at": a.get("updated_at"),
            })
    return {
        "link": link,
        "viewers": viewers,
        "actions": actions,
        "downloads": downloads,
        "summary": list(summary.values()),
        "subjects": subjects,
        "view_count": len(viewers),
        "unique_viewers": len({v["viewer_email"] for v in viewers}),
    }


# --------------------------------------------------------------------------
# Public client routes
# --------------------------------------------------------------------------
def decode_viewer(authorization: Optional[str]) -> Optional[Dict[str, Any]]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1]
    data = decode_token(token)
    if not data or data.get("role") != "viewer":
        return None
    return data


@api.post("/public/links/{slug}/identify")
async def identify_viewer(slug: str, payload: IdentifyIn):
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    viewer_id = str(uuid.uuid4())
    await db.link_views.insert_one({
        "id": viewer_id,
        "link_id": link["id"],
        "slug": slug,
        "viewer_email": payload.email.lower(),
        "viewer_name": payload.name,
        "created_at": _now(),
    })
    token = make_token({
        "role": "viewer",
        "slug": slug,
        "email": payload.email.lower(),
        "name": payload.name,
        "viewer_id": viewer_id,
    }, days=7)
    return {"token": token}


def _filter_talent_for_client(talent: dict, visibility: Dict[str, bool]) -> dict:
    """STRICT allowlist: client receives only fields explicitly enabled via visibility.
    No raw talent document is ever returned. Fields not toggled on are never included."""
    v = visibility or {}
    # Filter media strictly by visibility: portfolio → images, intro_video → video
    filtered_media: List[dict] = []
    cover_mid: Optional[str] = None
    for m in talent.get("media") or []:
        cat = m.get("category")
        if cat in ("indian", "western", "portfolio") and v.get("portfolio"):
            filtered_media.append(m)
            if not cover_mid and talent.get("cover_media_id") == m.get("id"):
                cover_mid = m["id"]
        elif cat == "video" and v.get("intro_video"):
            filtered_media.append(m)
    # Fallback cover: first visible portfolio image if declared cover was filtered out
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
    # Demographics — only if explicitly toggled on AND value exists
    if v.get("age") and talent.get("age") is not None:
        out["age"] = talent["age"]
    if v.get("height") and talent.get("height"):
        out["height"] = talent["height"]
    if v.get("location") and talent.get("location"):
        out["location"] = talent["location"]
    if v.get("ethnicity") and talent.get("ethnicity"):
        out["ethnicity"] = talent["ethnicity"]
    # Social
    if v.get("instagram") and talent.get("instagram_handle"):
        out["instagram_handle"] = talent["instagram_handle"]
    if v.get("instagram_followers") and talent.get("instagram_followers"):
        out["instagram_followers"] = talent["instagram_followers"]
    # Work links
    if v.get("work_links") and talent.get("work_links"):
        out["work_links"] = talent["work_links"]
    # NEVER included: dob, gender, bio, source, created_at, created_by, instagram_handle
    # unless toggled above. Fields such as gender/bio are internal-only for now.
    return out


def _public_link_view(link: dict) -> dict:
    """Return only fields the client needs. Strip admin-only fields (notes, password, created_by)."""
    v = link.get("visibility") or {}
    return {
        "id": link["id"],
        "slug": link.get("slug"),
        "title": link.get("title"),
        "brand_name": link.get("brand_name"),
        "visibility": v,
    }


def _submission_to_client_shape(sub: dict) -> dict:
    """Flatten a submission document into the same shape clients expect for a talent.
    Respects submission-level `field_visibility` (admin's per-field toggles during review).
    Submissions are NEVER copied into the master `talents` collection — this is a read-side projection."""
    fd = sub.get("form_data") or {}
    fv = sub.get("field_visibility") or {**DEFAULT_FIELD_VISIBILITY}

    # Name assembly
    fn = (fd.get("first_name") or "").strip()
    ln = (fd.get("last_name") or "").strip()
    name = f"{fn} {ln}".strip() or sub.get("talent_name") or "Unnamed"

    # Age
    age: Optional[int] = None
    if fv.get("age") and fd.get("age") not in (None, ""):
        try:
            age = int(fd["age"])
        except Exception:
            age = None

    # Media: image -> portfolio, intro_video -> video, takes excluded entirely
    media: List[dict] = []
    cover_mid: Optional[str] = None
    for m in sub.get("media") or []:
        cat = m.get("category")
        if cat == "image":
            mapped = {**m, "category": "portfolio"}
            media.append(mapped)
            if not cover_mid:
                cover_mid = mapped.get("id")
        elif cat == "intro_video":
            media.append({**m, "category": "video"})
        # take_1/2/3 intentionally dropped — internal review only

    return {
        "id": sub["id"],  # submission id acts as subject id on link
        "name": name,
        "age": age,
        "height": fd.get("height") if fv.get("height") else None,
        "location": fd.get("location") if fv.get("location") else None,
        # Fields not currently collected in the submission form:
        "ethnicity": None,
        "instagram_handle": None,
        "instagram_followers": None,
        "work_links": [],
        "cover_media_id": cover_mid,
        "media": media,
    }


@api.get("/public/links/{slug}")
async def get_public_link(slug: str, authorization: Optional[str] = Header(None)):
    viewer = decode_viewer(authorization)
    if not viewer or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    visibility = {**DEFAULT_VISIBILITY, **(link.get("visibility") or {})}

    # Load master-DB talents (legacy/manual picks) AND referenced submissions (audition flow)
    talent_ids = link.get("talent_ids", []) or []
    submission_ids = link.get("submission_ids", []) or []

    raw_talents = []
    if talent_ids:
        raw_talents = await db.talents.find(
            {"id": {"$in": talent_ids}},
            {"_id": 0, "created_by": 0},
        ).to_list(5000)

    raw_subs = []
    if submission_ids:
        raw_subs = await db.submissions.find(
            {"id": {"$in": submission_ids}},
            {"_id": 0},
        ).to_list(5000)

    # Build ordered subjects: talents first (in link order), then submissions (in link order)
    t_order = {tid: i for i, tid in enumerate(talent_ids)}
    raw_talents.sort(key=lambda t: t_order.get(t["id"], 999))
    s_order = {sid: i for i, sid in enumerate(submission_ids)}
    raw_subs.sort(key=lambda s: s_order.get(s["id"], 999))

    subjects: List[dict] = [enrich_talent(t) for t in raw_talents]
    subjects.extend(_submission_to_client_shape(s) for s in raw_subs)

    # Apply strict link-level visibility filter (allowlist) to every subject
    talents = [_filter_talent_for_client(it, visibility) for it in subjects]

    # viewer's existing actions on this link
    actions = await db.link_actions.find({
        "link_id": link["id"],
        "viewer_email": viewer["email"],
    }, {"_id": 0}).to_list(5000)
    return {
        "link": _public_link_view(link),
        "talents": talents,
        "actions": actions,
        "viewer": {"email": viewer["email"], "name": viewer["name"]},
    }


@api.post("/public/links/{slug}/action")
async def record_action(
    slug: str,
    payload: ActionIn,
    authorization: Optional[str] = Header(None),
):
    viewer = decode_viewer(authorization)
    if not viewer or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")

    filt = {
        "link_id": link["id"],
        "viewer_email": viewer["email"],
        "talent_id": payload.talent_id,
    }
    existing = await db.link_actions.find_one(filt, {"_id": 0})
    doc = {
        **filt,
        "viewer_name": viewer["name"],
        "action": payload.action,
        "comment": payload.comment if payload.comment is not None else (existing.get("comment") if existing else None),
        "updated_at": _now(),
    }
    if not existing:
        doc["id"] = str(uuid.uuid4())
        doc["created_at"] = _now()
    await db.link_actions.update_one(filt, {"$set": doc}, upsert=True)
    return {"ok": True}


@api.post("/public/links/{slug}/download-log")
async def log_download(
    slug: str,
    payload: DownloadIn,
    authorization: Optional[str] = Header(None),
):
    viewer = decode_viewer(authorization)
    if not viewer or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    if not link.get("visibility", {}).get("download"):
        raise HTTPException(403, "Downloads disabled")
    await db.link_downloads.insert_one({
        "id": str(uuid.uuid4()),
        "link_id": link["id"],
        "slug": slug,
        "viewer_email": viewer["email"],
        "viewer_name": viewer["name"],
        "talent_id": payload.talent_id,
        "media_id": payload.media_id,
        "created_at": _now(),
    })
    return {"ok": True}


# --------------------------------------------------------------------------
# Project routes (Audition Engine — Part 1: Project Creation)
# --------------------------------------------------------------------------
@api.post("/projects")
async def create_project(payload: ProjectIn, admin: dict = Depends(current_admin)):
    if payload.commission_percent and payload.commission_percent not in COMMISSION_OPTIONS:
        raise HTTPException(400, "Invalid commission_percent")
    doc = payload.model_dump()
    doc.update({
        "id": str(uuid.uuid4()),
        "slug": _slugify(payload.brand_name),
        "materials": [],  # list of MaterialItem
        "created_at": _now(),
        "created_by": admin["id"],
    })
    await db.projects.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.get("/projects")
async def list_projects(admin: dict = Depends(current_admin)):
    items = await db.projects.find({}, {"_id": 0}).sort("created_at", -1).to_list(2000)
    return items


@api.get("/projects/{pid}")
async def get_project(pid: str, admin: dict = Depends(current_admin)):
    p = await db.projects.find_one({"id": pid}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Project not found")
    return p


@api.put("/projects/{pid}")
async def update_project(pid: str, payload: ProjectIn, admin: dict = Depends(current_admin)):
    if payload.commission_percent and payload.commission_percent not in COMMISSION_OPTIONS:
        raise HTTPException(400, "Invalid commission_percent")
    res = await db.projects.update_one({"id": pid}, {"$set": payload.model_dump()})
    if not res.matched_count:
        raise HTTPException(404, "Project not found")
    p = await db.projects.find_one({"id": pid}, {"_id": 0})
    return p


@api.delete("/projects/{pid}")
async def delete_project(pid: str, admin: dict = Depends(current_admin)):
    res = await db.projects.delete_one({"id": pid})
    if not res.deleted_count:
        raise HTTPException(404, "Project not found")
    return {"ok": True}


@api.post("/projects/{pid}/material")
async def add_material(
    pid: str,
    category: str = Form(...),
    file: UploadFile = File(...),
    admin: dict = Depends(current_admin),
):
    if category not in MATERIAL_CATEGORIES:
        raise HTTPException(400, "Invalid category (script|image|audio)")
    project = await db.projects.find_one({"id": pid})
    if not project:
        raise HTTPException(404, "Project not found")
    ext = (file.filename or "bin").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "bin"
    path = f"{APP_NAME}/projects/{pid}/{uuid.uuid4()}.{ext}"
    data = await file.read()
    result = put_object(path, data, file.content_type or "application/octet-stream")
    material = {
        "id": str(uuid.uuid4()),
        "category": category,
        "storage_path": result["path"],
        "content_type": file.content_type or "application/octet-stream",
        "original_filename": file.filename,
        "size": result.get("size", len(data)),
        "created_at": _now(),
    }
    await db.projects.update_one({"id": pid}, {"$push": {"materials": material}})
    p = await db.projects.find_one({"id": pid}, {"_id": 0})
    return p


@api.delete("/projects/{pid}/material/{mid}")
async def delete_material(pid: str, mid: str, admin: dict = Depends(current_admin)):
    res = await db.projects.update_one({"id": pid}, {"$pull": {"materials": {"id": mid}}})
    if not res.modified_count:
        raise HTTPException(404, "Material not found")
    return {"ok": True}


@api.get("/projects/meta/commission-options")
async def commission_options(admin: dict = Depends(current_admin)):
    return {"options": COMMISSION_OPTIONS}


# --------------------------------------------------------------------------
# Talent Submission routes (public + admin review)
# --------------------------------------------------------------------------
def _public_project(project: dict) -> dict:
    """Strip internal/private fields before returning project info publicly."""
    out = {k: v for k, v in project.items() if k not in {"_id", "created_by"}}
    return out


def decode_submitter(authorization: Optional[str]) -> Optional[Dict[str, Any]]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1]
    data = decode_token(token)
    if not data or data.get("role") != "submitter":
        return None
    return data


@api.get("/public/projects/{slug}")
async def public_project(slug: str):
    project = await db.projects.find_one({"slug": slug}, {"_id": 0, "created_by": 0})
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@api.post("/public/projects/{slug}/submission")
async def start_submission(slug: str, payload: SubmissionStartIn):
    project = await db.projects.find_one({"slug": slug})
    if not project:
        raise HTTPException(404, "Project not found")
    sid = str(uuid.uuid4())
    doc = {
        "id": sid,
        "project_id": project["id"],
        "project_slug": slug,
        "talent_name": payload.name,
        "talent_email": payload.email.lower(),
        "talent_phone": payload.phone,
        "form_data": payload.form_data or {},
        "field_visibility": {**DEFAULT_FIELD_VISIBILITY},
        "media": [],
        "status": "draft",
        "decision": "pending",
        "created_at": _now(),
        "submitted_at": None,
    }
    await db.submissions.insert_one(doc)
    token = make_token({"role": "submitter", "sid": sid, "slug": slug}, days=3)
    return {"id": sid, "token": token}


@api.put("/public/submissions/{sid}")
async def submission_update(
    sid: str,
    payload: SubmissionUpdateIn,
    authorization: Optional[str] = Header(None),
):
    submitter = decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    if sub.get("status") == "submitted":
        raise HTTPException(400, "Submission already finalized")
    update: Dict[str, Any] = {}
    if payload.form_data is not None:
        update["form_data"] = {**(sub.get("form_data") or {}), **payload.form_data}
        # Keep talent_name synced with first+last if provided
        fn = payload.form_data.get("first_name") or update["form_data"].get("first_name")
        ln = payload.form_data.get("last_name") or update["form_data"].get("last_name")
        if fn or ln:
            update["talent_name"] = f"{fn or ''} {ln or ''}".strip() or sub.get("talent_name")
    if update:
        await db.submissions.update_one({"id": sid}, {"$set": update})
    updated = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return updated


@api.post("/public/submissions/{sid}/upload")
async def submission_upload(
    sid: str,
    category: str = Form(...),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    submitter = decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    if category not in SUBMISSION_UPLOAD_CATEGORIES:
        raise HTTPException(400, "Invalid category")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    if sub.get("status") == "submitted":
        raise HTTPException(400, "Submission already finalized")

    # Enforce image cap
    if category == "image":
        existing = sum(1 for m in sub.get("media", []) if m["category"] == "image")
        if existing >= MAX_SUBMISSION_IMAGES:
            raise HTTPException(400, f"Image limit reached ({MAX_SUBMISSION_IMAGES})")

    # Single-slot categories: replace if exists
    single_slot = {"intro_video", "take_1", "take_2", "take_3"}
    if category in single_slot:
        # Remove previous entry in same slot
        await db.submissions.update_one(
            {"id": sid}, {"$pull": {"media": {"category": category}}}
        )

    ext = (file.filename or "bin").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "bin"
    path = f"{APP_NAME}/submissions/{sid}/{uuid.uuid4()}.{ext}"
    data = await file.read()
    result = put_object(path, data, file.content_type or "application/octet-stream")
    media = {
        "id": str(uuid.uuid4()),
        "category": category,
        "storage_path": result["path"],
        "content_type": file.content_type or "application/octet-stream",
        "original_filename": file.filename,
        "size": result.get("size", len(data)),
        "created_at": _now(),
    }
    await db.submissions.update_one({"id": sid}, {"$push": {"media": media}})
    updated = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return updated


@api.delete("/public/submissions/{sid}/media/{mid}")
async def submission_delete_media(
    sid: str, mid: str, authorization: Optional[str] = Header(None)
):
    submitter = decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    if sub.get("status") == "submitted":
        raise HTTPException(400, "Submission already finalized")
    await db.submissions.update_one({"id": sid}, {"$pull": {"media": {"id": mid}}})
    return {"ok": True}


@api.post("/public/submissions/{sid}/finalize")
async def submission_finalize(sid: str, authorization: Optional[str] = Header(None)):
    submitter = decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    form = sub.get("form_data") or {}
    for field in ("first_name", "last_name", "height", "location"):
        if not (form.get(field) or "").strip():
            raise HTTPException(400, f"{field.replace('_',' ').title()} is required")
    # Availability
    avail = form.get("availability") or {}
    if isinstance(avail, str):
        avail = {"status": "yes" if avail else "", "note": avail}
    status = (avail.get("status") or "").strip()
    if status not in {"yes", "no"}:
        raise HTTPException(400, "Please confirm your availability")
    if status == "no" and not (avail.get("note") or "").strip():
        raise HTTPException(400, "Please share your alternate availability")
    # Budget
    budget = form.get("budget") or {}
    if isinstance(budget, str):
        budget = {"status": "accept" if budget else "", "value": budget}
    bstatus = (budget.get("status") or "").strip()
    if bstatus not in {"accept", "custom"}:
        raise HTTPException(400, "Please confirm the budget")
    if bstatus == "custom" and not (budget.get("value") or "").strip():
        raise HTTPException(400, "Please enter your expected budget")
    media = sub.get("media", [])
    has_intro = any(m["category"] == "intro_video" for m in media)
    has_take1 = any(m["category"] == "take_1" for m in media)
    img_count = sum(1 for m in media if m["category"] == "image")
    if not has_intro:
        raise HTTPException(400, "Introduction video is required")
    if not has_take1:
        raise HTTPException(400, "Take 1 is required")
    if img_count < MIN_SUBMISSION_IMAGES:
        raise HTTPException(400, f"At least {MIN_SUBMISSION_IMAGES} images are required (you have {img_count})")
    await db.submissions.update_one(
        {"id": sid},
        {"$set": {"status": "submitted", "submitted_at": _now()}},
    )
    return {"ok": True}


@api.get("/public/submissions/{sid}")
async def public_submission(sid: str, authorization: Optional[str] = Header(None)):
    submitter = decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    if not sub:
        raise HTTPException(404, "Submission not found")
    return sub


# ---- Admin review ----
@api.get("/projects/{pid}/submissions")
async def list_submissions(pid: str, admin: dict = Depends(current_admin)):
    subs = await db.submissions.find(
        {"project_id": pid}, {"_id": 0}
    ).sort("created_at", -1).to_list(5000)
    return subs


@api.post("/projects/{pid}/submissions/{sid}/decision")
async def set_decision(
    pid: str,
    sid: str,
    payload: SubmissionDecisionIn,
    admin: dict = Depends(current_admin),
):
    if payload.decision not in SUBMISSION_DECISIONS:
        raise HTTPException(400, "Invalid decision")
    res = await db.submissions.update_one(
        {"id": sid, "project_id": pid},
        {"$set": {"decision": payload.decision, "decided_at": _now()}},
    )
    if not res.matched_count:
        raise HTTPException(404, "Submission not found")
    return {"ok": True}


@api.put("/projects/{pid}/submissions/{sid}")
async def admin_edit_submission(
    pid: str,
    sid: str,
    payload: AdminSubmissionEditIn,
    admin: dict = Depends(current_admin),
):
    """Admin can edit form_data and toggle per-field visibility for the client view."""
    sub = await db.submissions.find_one({"id": sid, "project_id": pid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    update: Dict[str, Any] = {}
    if payload.form_data is not None:
        update["form_data"] = {**(sub.get("form_data") or {}), **payload.form_data}
        fn = update["form_data"].get("first_name") or sub.get("form_data", {}).get("first_name")
        ln = update["form_data"].get("last_name") or sub.get("form_data", {}).get("last_name")
        if fn or ln:
            update["talent_name"] = f"{fn or ''} {ln or ''}".strip() or sub.get("talent_name")
    if payload.field_visibility is not None:
        current_fv = sub.get("field_visibility") or {**DEFAULT_FIELD_VISIBILITY}
        update["field_visibility"] = {**current_fv, **payload.field_visibility}
    if update:
        await db.submissions.update_one({"id": sid}, {"$set": update})
    out = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return out


@api.delete("/projects/{pid}/submissions/{sid}")
async def delete_submission(
    pid: str, sid: str, admin: dict = Depends(current_admin)
):
    res = await db.submissions.delete_one({"id": sid, "project_id": pid})
    if not res.deleted_count:
        raise HTTPException(404, "Submission not found")
    return {"ok": True}


@api.post("/projects/{pid}/forward-to-link")
async def forward_to_link(
    pid: str,
    payload: ForwardToLinkIn,
    admin: dict = Depends(current_admin),
):
    """Generate a client portfolio link that REFERENCES approved submissions directly.
    Submissions stay inside the project — they are never copied into the master `talents` collection.
    This prevents duplicate profiles when the same talent applies to multiple projects."""
    if not payload.submission_ids:
        raise HTTPException(400, "Select at least one submission")
    project = await db.projects.find_one({"id": pid}, {"_id": 0})
    if not project:
        raise HTTPException(404, "Project not found")

    # Validate: every selected submission must exist in this project AND be approved
    approved = await db.submissions.find(
        {
            "id": {"$in": payload.submission_ids},
            "project_id": pid,
            "decision": "approved",
        },
        {"_id": 0, "id": 1},
    ).to_list(5000)
    approved_ids = {s["id"] for s in approved}
    if not approved_ids:
        raise HTTPException(400, "No approved submissions match the selection")

    # Preserve admin's selection order
    ordered_submission_ids = [sid for sid in payload.submission_ids if sid in approved_ids]

    vis = {**DEFAULT_VISIBILITY, **(payload.visibility or {})}
    title = f"Talentgram x {project['brand_name']}"
    link_doc = {
        "id": str(uuid.uuid4()),
        "slug": _slugify(title),
        "title": title,
        "brand_name": project["brand_name"],
        "talent_ids": [],
        "submission_ids": ordered_submission_ids,
        "visibility": vis,
        "is_public": True,
        "password": None,
        "notes": f"Forwarded from project: {project['brand_name']}",
        "created_at": _now(),
        "created_by": admin["id"],
    }
    await db.links.insert_one(link_doc)
    link_doc.pop("_id", None)
    link_doc["view_count"] = 0
    link_doc["unique_viewers"] = 0
    return link_doc


# --------------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------------
@api.get("/")
async def root():
    return {"app": "talentgram", "ok": True}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    await seed_admin()
    init_storage()


@app.on_event("shutdown")
async def on_shutdown():
    client.close()
