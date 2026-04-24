"""Admin link CRUD + public client link viewer."""
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from core import (
    ActionIn,
    BulkDeleteIn,
    DEFAULT_VISIBILITY,
    DownloadIn,
    IdentifyIn,
    LinkIn,
    LinkOut,
    _clean_budget_lines,
    _clean_ids,
    _filter_talent_for_client,
    _now,
    _public_link_view,
    _slugify,
    _submission_to_client_shape,
    current_admin,
    current_team_or_admin,
    db,
    decode_viewer,
    enrich_talent,
    make_token,
)

router = APIRouter(prefix="/api", tags=["links"])
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Admin link CRUD
# --------------------------------------------------------------------------
@router.post("/links", response_model=LinkOut)
async def create_link(payload: LinkIn, admin: dict = Depends(current_admin)):
    vis = {**DEFAULT_VISIBILITY, **(payload.visibility or {})}
    talent_ids = _clean_ids(payload.talent_ids)
    submission_ids = _clean_ids(payload.submission_ids)
    if not talent_ids and not submission_ids:
        raise HTTPException(400, "Select at least one talent or submission")
    doc = {
        "id": str(uuid.uuid4()),
        "slug": _slugify(payload.title),
        "title": payload.title,
        "brand_name": payload.brand_name,
        "talent_ids": talent_ids,
        "submission_ids": submission_ids,
        "visibility": vis,
        "is_public": payload.is_public,
        "password": payload.password,
        "notes": payload.notes,
        "client_budget_override": _clean_budget_lines(payload.client_budget_override)
        if payload.client_budget_override
        else None,
        "created_at": _now(),
        "created_by": admin["id"],
    }
    await db.links.insert_one(doc)
    doc.pop("_id", None)
    doc["view_count"] = 0
    doc["unique_viewers"] = 0
    return doc


@router.get("/links")
async def list_links(admin: dict = Depends(current_team_or_admin)):
    links = await db.links.find({}, {"_id": 0}).sort("created_at", -1).to_list(2000)
    for link in links:
        link["view_count"] = await db.link_views.count_documents({"link_id": link["id"]})
        link["unique_viewers"] = len(await db.link_views.distinct("viewer_email", {"link_id": link["id"]}))
    return links


@router.get("/links/{lid}")
async def get_link(lid: str, admin: dict = Depends(current_team_or_admin)):
    link = await db.links.find_one({"id": lid}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    link["view_count"] = await db.link_views.count_documents({"link_id": lid})
    link["unique_viewers"] = len(await db.link_views.distinct("viewer_email", {"link_id": lid}))
    return link


@router.put("/links/{lid}", response_model=LinkOut)
async def update_link(lid: str, payload: LinkIn, admin: dict = Depends(current_admin)):
    vis = {**DEFAULT_VISIBILITY, **(payload.visibility or {})}
    update = payload.model_dump()
    update["visibility"] = vis
    update["talent_ids"] = _clean_ids(payload.talent_ids)
    update["submission_ids"] = _clean_ids(payload.submission_ids)
    if not update["talent_ids"] and not update["submission_ids"]:
        raise HTTPException(400, "Select at least one talent or submission")
    override = _clean_budget_lines(update.get("client_budget_override"))
    update["client_budget_override"] = override if override else None
    res = await db.links.update_one({"id": lid}, {"$set": update})
    if not res.matched_count:
        raise HTTPException(404, "Link not found")
    link = await db.links.find_one({"id": lid}, {"_id": 0})
    link["view_count"] = await db.link_views.count_documents({"link_id": lid})
    link["unique_viewers"] = len(await db.link_views.distinct("viewer_email", {"link_id": lid}))
    return link


@router.post("/links/bulk-delete")
async def bulk_delete_links(
    payload: BulkDeleteIn, admin: dict = Depends(current_admin)
):
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        raise HTTPException(400, "No ids provided")
    logger.info(
        "BULK DELETE /links by admin=%s count=%d ids=%s",
        admin.get("email"), len(ids), ids[:10],
    )
    res = await db.links.delete_many({"id": {"$in": ids}})
    v = await db.link_views.delete_many({"link_id": {"$in": ids}})
    a = await db.link_actions.delete_many({"link_id": {"$in": ids}})
    d = await db.link_downloads.delete_many({"link_id": {"$in": ids}})
    logger.info(
        "BULK DELETE /links by admin=%s removed=%d views=%d actions=%d downloads=%d",
        admin.get("email"), res.deleted_count, v.deleted_count, a.deleted_count, d.deleted_count,
    )
    return {
        "ok": True,
        "requested": len(ids),
        "deleted": res.deleted_count,
        "missing": len(ids) - res.deleted_count,
        "cascaded": {
            "views": v.deleted_count,
            "actions": a.deleted_count,
            "downloads": d.deleted_count,
        },
    }


@router.delete("/links/{lid}")
async def delete_link(lid: str, admin: dict = Depends(current_admin)):
    logger.info(
        "DELETE /links/%s requested by admin=%s (role=%s)",
        lid, admin.get("email"), admin.get("role"),
    )
    res = await db.links.delete_one({"id": lid})
    if not res.deleted_count:
        logger.warning("DELETE /links/%s failed — not found", lid)
        raise HTTPException(404, "Link not found")
    v = await db.link_views.delete_many({"link_id": lid})
    a = await db.link_actions.delete_many({"link_id": lid})
    d = await db.link_downloads.delete_many({"link_id": lid})
    logger.info(
        "DELETE /links/%s succeeded (by %s); cascade views=%d actions=%d downloads=%d",
        lid, admin.get("email"), v.deleted_count, a.deleted_count, d.deleted_count,
    )
    return {
        "ok": True,
        "deleted_id": lid,
        "cascaded": {
            "views": v.deleted_count,
            "actions": a.deleted_count,
            "downloads": d.deleted_count,
        },
    }


@router.post("/links/{lid}/duplicate", response_model=LinkOut)
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


@router.get("/links/{lid}/results")
async def link_results(lid: str, admin: dict = Depends(current_team_or_admin)):
    link = await db.links.find_one({"id": lid}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    viewers = await db.link_views.find({"link_id": lid}, {"_id": 0}).sort("created_at", -1).to_list(5000)
    actions = await db.link_actions.find({"link_id": lid}, {"_id": 0}).sort("updated_at", -1).to_list(10000)
    downloads = await db.link_downloads.find({"link_id": lid}, {"_id": 0}).sort("created_at", -1).to_list(10000)

    t_ids = link.get("talent_ids", []) or []
    s_ids = link.get("submission_ids", []) or []
    subjects: Dict[str, Dict[str, Any]] = {}
    if t_ids:
        for t in await db.talents.find(
            {"id": {"$in": t_ids}},
            {"_id": 0, "id": 1, "name": 1, "cover_media_id": 1, "media": 1},
        ).to_list(5000):
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
# Public client endpoints
# --------------------------------------------------------------------------
@router.post("/public/links/{slug}/identify")
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


@router.get("/public/links/{slug}")
async def get_public_link(slug: str, authorization: Optional[str] = Header(None)):
    viewer = decode_viewer(authorization)
    if not viewer or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    visibility = {**DEFAULT_VISIBILITY, **(link.get("visibility") or {})}

    talent_ids = link.get("talent_ids", []) or []
    submission_ids = link.get("submission_ids", []) or []

    raw_talents: List[dict] = []
    if talent_ids:
        raw_talents = await db.talents.find(
            {"id": {"$in": talent_ids}},
            {"_id": 0, "created_by": 0},
        ).to_list(5000)

    raw_subs: List[dict] = []
    if submission_ids:
        raw_subs = await db.submissions.find(
            {"id": {"$in": submission_ids}},
            {"_id": 0},
        ).to_list(5000)

    t_order = {tid: i for i, tid in enumerate(talent_ids)}
    raw_talents.sort(key=lambda t: t_order.get(t["id"], 999))
    s_order = {sid: i for i, sid in enumerate(submission_ids)}
    raw_subs.sort(key=lambda s: s_order.get(s["id"], 999))

    subjects: List[dict] = [enrich_talent(t) for t in raw_talents]
    subjects.extend(_submission_to_client_shape(s) for s in raw_subs)

    talents = [_filter_talent_for_client(it, visibility) for it in subjects]

    # Client-facing project budget (gated by visibility.budget)
    project_budget: List[Dict[str, Any]] = []
    if visibility.get("budget"):
        override = link.get("client_budget_override")
        if override:
            project_budget = [{
                "project_id": None,
                "brand_name": link.get("brand_name") or link.get("title"),
                "lines": override,
            }]
        elif raw_subs:
            # Aggregate unique projects from submissions (preserve first-seen order)
            seen_pids: List[str] = []
            for s in raw_subs:
                pid = s.get("project_id")
                if pid and pid not in seen_pids:
                    seen_pids.append(pid)
            if seen_pids:
                projects = await db.projects.find(
                    {"id": {"$in": seen_pids}},
                    {"_id": 0, "id": 1, "brand_name": 1, "client_budget": 1},
                ).to_list(500)
                by_id = {p["id"]: p for p in projects}
                for pid in seen_pids:
                    proj = by_id.get(pid)
                    if not proj:
                        continue
                    lines = proj.get("client_budget") or []
                    if lines:
                        project_budget.append({
                            "project_id": pid,
                            "brand_name": proj.get("brand_name"),
                            "lines": lines,
                        })

    actions = await db.link_actions.find({
        "link_id": link["id"],
        "viewer_email": viewer["email"],
    }, {"_id": 0}).to_list(5000)
    return {
        "link": _public_link_view(link),
        "talents": talents,
        "actions": actions,
        "project_budget": project_budget,
        "viewer": {"email": viewer["email"], "name": viewer["name"]},
    }


@router.post("/public/links/{slug}/action")
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


@router.post("/public/links/{slug}/download-log")
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
