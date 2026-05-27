"""Talent Portal Endpoints for simplified localStorage-based entry and profile management."""
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from core import db, enrich_talent, _now, update_talent_cover_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["portal"])


class PortalLookupIn(BaseModel):
    email: str


class PortalProfileUpdateIn(BaseModel):
    email: str
    name: str
    phone: Optional[str] = None
    location: Optional[str] = None
    height: Optional[str] = None
    dob: Optional[str] = None
    bio: Optional[str] = None
    instagram_handle: Optional[str] = None
    work_links: List[str] = Field(default_factory=list)
    interested_in: List[str] = Field(default_factory=list)


@router.post("/portal/lookup")
async def portal_lookup(payload: PortalLookupIn):
    email = payload.email.strip().lower()
    talent = await db.talents.find_one({"email": email}, {"_id": 0})
    if not talent:
        return {"exists": False}

    enriched = enrich_talent(talent)
    return {
        "exists": True,
        "talent": {
            "name": enriched.get("name"),
            "email": enriched.get("email"),
            "location": enriched.get("location"),
            "height": enriched.get("height"),
            "dob": enriched.get("dob"),
            "age": enriched.get("age"),
            "bio": enriched.get("bio"),
            "instagram_handle": enriched.get("instagram_handle"),
            "image_url": enriched.get("image_url") or enriched.get("cover_url"),
            "interested_in": enriched.get("interested_in") or [],
        }
    }


@router.get("/portal/profile")
async def portal_get_profile(email: str):
    email = email.strip().lower()
    talent = await db.talents.find_one({"email": email}, {"_id": 0})
    if not talent:
        raise HTTPException(status_code=404, detail="Talent profile not found")
    return enrich_talent(talent)


@router.put("/portal/profile")
async def portal_update_profile(payload: PortalProfileUpdateIn):
    email = payload.email.strip().lower()
    talent = await db.talents.find_one({"email": email})
    if not talent:
        raise HTTPException(status_code=404, detail="Talent profile not found")

    update_fields = {
        "name": payload.name,
        "phone": payload.phone,
        "location": payload.location,
        "height": payload.height,
        "dob": payload.dob,
        "bio": payload.bio,
        "instagram_handle": payload.instagram_handle,
        "work_links": [w.strip() for w in payload.work_links if w.strip()],
        "interested_in": payload.interested_in,
        "last_portal_login": _now(),
    }

    await db.talents.update_one({"id": talent["id"]}, {"$set": update_fields})
    await update_talent_cover_cache(talent["id"])
    
    updated = await db.talents.find_one({"id": talent["id"]}, {"_id": 0})
    return enrich_talent(updated)


@router.get("/portal/projects")
async def portal_projects(email: str):
    email = email.strip().lower()
    try:
        submissions_cursor = db.submissions.find({"talent_email": email})
        submissions = await submissions_cursor.to_list(1000)

        ongoing_list = []
        shortlisted_list = []
        completed_list = []
        seen_project_ids = set()

        for sub in submissions:
            pid = sub["project_id"]
            if pid in seen_project_ids:
                continue
            seen_project_ids.add(pid)

            proj = await db.projects.find_one({"id": pid}, {"_id": 0})
            if not proj:
                continue

            card = {
                "project_id": proj["id"],
                "project_slug": proj["slug"],
                "project_title": proj.get("brand_name") or "Talentgram Campaign",
                "status": sub.get("status", "draft"),
                "decision": sub.get("decision", "pending"),
                "updated_at": sub.get("updated_at") or sub.get("created_at"),
                "project_status": proj.get("status", "ongoing"),
            }

            # Categorization based on status and decision
            if proj.get("status") == "complete" or sub.get("decision") == "approved":
                completed_list.append(card)
            elif sub.get("decision") == "shortlisted":
                shortlisted_list.append(card)
            else:
                ongoing_list.append(card)

        return {
            "ongoing": ongoing_list,
            "shortlisted": shortlisted_list,
            "completed": completed_list,
        }
    except Exception as e:
        logger.error(f"Error fetching portal projects for {email}: {e}")
        raise HTTPException(status_code=500, detail="Database lookup failed")

