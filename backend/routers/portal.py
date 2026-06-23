"""Talent Portal Endpoints for simplified localStorage-based entry and profile management."""
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from core import (
    db,
    enrich_talent,
    _now,
    update_talent_cover_cache,
    normalize_instagram_handle,
    current_portal_talent,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["portal"])


class PortalLookupIn(BaseModel):
    email: str


class PortalProfileUpdateIn(BaseModel):
    # `email` is accepted for backwards-compat with existing clients but is
    # IGNORED for authorization — the target talent is always derived from the
    # authenticated portal token, never from this field.
    email: Optional[str] = None
    name: str
    phone: Optional[str] = None
    location: Optional[str] = None
    height: Optional[str] = None
    dob: Optional[str] = None
    bio: Optional[str] = None
    instagram_handle: Optional[str] = None
    work_links: List[str] = Field(default_factory=list)
    interested_in: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)

    @field_validator('instagram_handle', mode='before')
    @classmethod
    def _normalize_ig(cls, v):
        """Auto-normalize any pasted Instagram URL/handle to a raw username."""
        return normalize_instagram_handle(v)


@router.post("/portal/lookup")
async def portal_lookup(payload: PortalLookupIn):
    """Pre-authentication recognition check for the gateway.

    Deliberately UNauthenticated (it runs before OTP), so it returns only the
    minimal non-sensitive fields needed to render the "Is this you?" card.
    Full PII (DOB, height, bio, skills, location, contact) is no longer exposed
    here — that requires an authenticated portal session.
    """
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
            "image_url": enriched.get("image_url") or enriched.get("cover_url"),
        }
    }


@router.get("/portal/profile")
async def portal_get_profile(talent: dict = Depends(current_portal_talent)):
    # Identity comes from the authenticated portal token; no email param.
    fresh = await db.talents.find_one({"id": talent["id"]}, {"_id": 0})
    if not fresh:
        raise HTTPException(status_code=404, detail="Talent profile not found")
    return enrich_talent(fresh)


@router.put("/portal/profile")
async def portal_update_profile(
    payload: PortalProfileUpdateIn,
    talent: dict = Depends(current_portal_talent),
):
    # Always update the authenticated talent — payload.email is ignored.
    #
    # IMPORTANT: `name`, `dob`, and `height` are admin-controlled REVIEW fields
    # (see 03_BUSINESS_RULES.md — "Admin is source of truth"). The talent portal
    # must NEVER overwrite them on the canonical record, so they are deliberately
    # excluded from this $set even though the request model still accepts them
    # (kept for backwards-compat, mirroring how `email` is accepted-but-ignored).
    # Only the AUTO_UPDATE / talent-owned fields below are persisted.
    update_fields = {
        "phone": payload.phone,
        "location": payload.location,
        "bio": payload.bio,
        "instagram_handle": payload.instagram_handle,
        "work_links": [w.strip() for w in payload.work_links if w.strip()],
        "interested_in": payload.interested_in,
        "skills": payload.skills,
        "last_portal_login": _now(),
    }

    await db.talents.update_one({"id": talent["id"]}, {"$set": update_fields})
    await update_talent_cover_cache(talent["id"])
    
    updated = await db.talents.find_one({"id": talent["id"]}, {"_id": 0})
    return enrich_talent(updated)


@router.get("/portal/projects")
async def portal_projects(talent: dict = Depends(current_portal_talent)):
    # Derive the lookup email from the authenticated token, not the query.
    email = (talent.get("email") or "").strip().lower()
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

