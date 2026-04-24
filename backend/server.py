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
    talent_ids: List[str]
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


COMMISSION_OPTIONS = ["10%", "15%", "20%", "25%", "30%"]
MATERIAL_CATEGORIES = {"script", "image", "audio"}


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

    # Aggregate talent summaries
    summary: Dict[str, Dict[str, Any]] = {tid: {"talent_id": tid, "shortlist": 0, "interested": 0, "not_for_this": 0, "not_sure": 0, "comments": []} for tid in link.get("talent_ids", [])}
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


@api.get("/public/links/{slug}")
async def get_public_link(slug: str, authorization: Optional[str] = Header(None)):
    viewer = decode_viewer(authorization)
    if not viewer or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    talents = await db.talents.find(
        {"id": {"$in": link.get("talent_ids", [])}},
        {"_id": 0, "created_by": 0},
    ).to_list(5000)
    # preserve original order
    order = {tid: i for i, tid in enumerate(link.get("talent_ids", []))}
    talents.sort(key=lambda t: order.get(t["id"], 999))
    # Enrich with computed age and STRIP dob — clients must never see DOB
    for t in talents:
        enrich_talent(t)
        t.pop("dob", None)

    # viewer's existing actions
    actions = await db.link_actions.find({
        "link_id": link["id"],
        "viewer_email": viewer["email"],
    }, {"_id": 0}).to_list(5000)
    return {
        "link": link,
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
