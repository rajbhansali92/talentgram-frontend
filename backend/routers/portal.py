"""Talent Portal Endpoints for simplified localStorage-based entry and profile management."""
import logging
from typing import Any, Dict, List, Optional, Union
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from core import (
    db,
    enrich_talent,
    _now,
    update_talent_cover_cache,
    normalize_instagram_handle,
    current_portal_talent,
    resolve_canonical_talent,
    TalentIn,
    build_talent_submission_view,
    delete_talent_media_item,
    set_talent_cover_media,
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
    # Accepts a plain string (existing clients, e.g. current PortalProfile.jsx)
    # or the structured [{city, country}] shape (LocationSelector-based
    # clients). Always normalized to the structured shape before being
    # persisted to db.talents — see `location` field_validator below. Reuses
    # TalentIn's own location parser rather than duplicating the logic.
    location: Optional[Union[str, List[Dict[str, str]]]] = None
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

    @field_validator('location', mode='before')
    @classmethod
    def _normalize_location(cls, v):
        # Reuses TalentIn's own location parser so the portal never writes a
        # shape to db.talents that diverges from the master schema
        # (List[LocationItem] — see core.py:1726). Was previously typed as a
        # plain string here, which silently overwrote the structured
        # [{city, country}] value on db.talents with a raw string on every
        # portal profile save — see docs/TALENT_MIGRATION_PLAN.md Phase 1
        # item 1.
        return TalentIn._normalize_location(v)


@router.post("/portal/lookup")
async def portal_lookup(payload: PortalLookupIn):
    """Pre-authentication recognition check for the gateway.

    Deliberately UNauthenticated (it runs before OTP), so it returns only the
    minimal non-sensitive fields needed to render the "Is this you?" card.
    Full PII (DOB, height, bio, skills, location, contact) is no longer exposed
    here — that requires an authenticated portal session.
    """
    email = payload.email.strip().lower()
    # Uses the canonical resolver (matches on normalized_email/email/
    # source.talent_email) rather than a bare email match, so this
    # recognition check doesn't miss talents matchable only by one of the
    # other identifiers — same rule every other lookup in the codebase uses.
    talent = await resolve_canonical_talent(email=email)
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
    #
    # PATCH semantics: only fields the caller actually included in the request
    # body are written — `exclude_unset` distinguishes "field omitted" (leave
    # existing value untouched) from "field explicitly sent" (including an
    # explicit `null`, which IS present in the input and therefore included
    # here). Previously every field was pulled unconditionally from `payload`,
    # so an omitted field silently fell back to the Pydantic default (None /
    # empty list) and overwrote the stored value with it on every save.
    #
    # Explicit `null` clears a field only where the master schema itself
    # allows it: `phone`/`bio`/`instagram_handle` are Optional[str] on
    # TalentIn, so null is a legitimate cleared value; `location`'s own
    # normalizer (above) already maps null to `[]`, matching TalentIn's
    # default. `work_links`/`interested_in`/`skills` are plain (non-Optional)
    # `List[str]` on this model, so Pydantic itself rejects an explicit null
    # for those with a 422 before this handler ever runs — no extra
    # field-specific rejection logic needed here.
    provided = payload.model_dump(exclude_unset=True)
    writable_fields = {"phone", "location", "bio", "instagram_handle", "work_links", "interested_in", "skills"}
    update_fields = {k: v for k, v in provided.items() if k in writable_fields}

    if "work_links" in update_fields:
        update_fields["work_links"] = [w.strip() for w in update_fields["work_links"] if w.strip()]

    # Phase 0 — Canonical Metadata Foundation: stamp updated_at only when a
    # writable field's value actually differs from what's currently stored,
    # mirroring merge_talent_profile()'s diff-based stamping discipline
    # (core.py) — a resave of identical values (or a bare login-refresh with
    # no field changes) must not advance the canonical clock, or the
    # freshness comparison a stale-draft finalize relies on becomes
    # meaningless.
    if any(talent.get(k) != v for k, v in update_fields.items()):
        update_fields["updated_at"] = _now()

    update_fields["last_portal_login"] = _now()

    await db.talents.update_one({"id": talent["id"]}, {"$set": update_fields})
    await update_talent_cover_cache(talent["id"])
    
    updated = await db.talents.find_one({"id": talent["id"]}, {"_id": 0})
    return enrich_talent(updated)


# ---------------------------------------------------------------------------
# Media Library Manager (Phase 4 item 3) — talent-owned management of the
# reusable Global Media Library (`talents.media[]`) already built by Phase
# 4.1 (prefill foundation) and Phase 4.2 (picker UI). No new storage, sync,
# or upload logic: both routes below call the exact same deletion/cover
# helpers the pre-existing admin routes use (`routers/talents.py`), just
# authorized via `current_portal_talent` instead of `current_admin` — the
# talent's own id comes from their session token, never from the URL, so a
# talent can only ever touch their own media.
# ---------------------------------------------------------------------------

@router.delete("/portal/media/{mid}")
async def portal_delete_media(mid: str, talent: dict = Depends(current_portal_talent)):
    await delete_talent_media_item(talent["id"], mid)
    return {"ok": True}


@router.post("/portal/media/{mid}/cover")
async def portal_set_cover(mid: str, talent: dict = Depends(current_portal_talent)):
    cover_url = await set_talent_cover_media(talent["id"], mid)
    return {"ok": True, "cover_url": cover_url}


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


@router.get("/portal/projects/{slug}/submission")
async def portal_submission(slug: str, talent: dict = Depends(current_portal_talent)):
    """Read-only: the authenticated talent's own submission for one project.

    Exists to unlock Dashboard consumers (Submission Summary, Smart
    Checklist, Requirement/Readiness Engine — all frontend, none of that
    logic lives here) with the same canonical submission representation
    already used by the public submitter-JWT/access_token endpoints — see
    build_talent_submission_view() in core.py, the single place responsible
    for attaching feedback and signing R2 media. This endpoint adds no new
    shaping, no new auth primitive: ownership is the same project_id +
    talent_email match /portal/projects already uses.
    """
    project = await db.projects.find_one({"slug": slug}, {"_id": 0, "id": 1})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    email = (talent.get("email") or "").strip().lower()
    sub = await db.submissions.find_one(
        {"project_id": project["id"], "talent_email": email}, {"_id": 0}
    )
    if not sub:
        raise HTTPException(status_code=404, detail="No submission found for this project")

    return await build_talent_submission_view(sub)

