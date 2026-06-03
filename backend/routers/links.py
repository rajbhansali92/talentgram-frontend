"""Admin link CRUD + public client link viewer."""
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

class LinkTrackIn(BaseModel):
    event_type: str  # "open" | "view_talent" | "view_media" | "watch_video"
    session_id: str
    talent_id: Optional[str] = None
    media_id: Optional[str] = None
    watch_time: Optional[float] = None


class LinkShareIn(BaseModel):
    talent_id: str
    media_id: Optional[str] = None

from core import (
    ActionIn,
    BulkDeleteIn,
    DEFAULT_VISIBILITY,
    DownloadIn,
    IdentifyIn,
    LinkIn,
    LinkOut,
    SeenIn,
    _clean_budget_lines,
    _clean_ids,
    _filter_talent_for_client,
    _now,
    _paginate_params,
    _paginated,
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


def _require_active_link(link: dict) -> None:
    """Enforce is_public on public client endpoints.

    Called AFTER `link = await db.links.find_one(...)` has already
    established the link exists. If `is_public` is explicitly False,
    we reject with 403 so the client-facing page stops working the
    instant an admin flips the toggle. Missing/absent `is_public`
    still counts as active to preserve backwards compatibility with
    links created before the field was introduced.
    """
    if link.get("is_public") is False:
        raise HTTPException(status_code=403, detail="This link is no longer active")


# --------------------------------------------------------------------------
# Admin link CRUD
# --------------------------------------------------------------------------
@router.post("/links", response_model=LinkOut)
async def create_link(payload: LinkIn, admin: dict = Depends(current_admin)):
    vis = {**DEFAULT_VISIBILITY, **(payload.visibility or {})}
    talent_ids = _clean_ids(payload.talent_ids)
    submission_ids = _clean_ids(payload.submission_ids)
    auto_pull = bool(payload.auto_pull)
    auto_project_id = (payload.auto_project_id or None) if auto_pull else None
    if auto_pull:
        if not auto_project_id:
            raise HTTPException(400, "auto_project_id is required when auto_pull is enabled")
        proj = await db.projects.find_one({"id": auto_project_id}, {"_id": 0, "id": 1})
        if not proj:
            raise HTTPException(400, "auto_project_id does not match any project")
        talent_ids = []
        submission_ids = []
    elif not talent_ids and not submission_ids:
        raise HTTPException(400, "Select at least one talent or submission")
    # Constrain talent_field_visibility to the talent_ids attached to the link
    # so we don't accumulate stale entries when admins edit talent lists.
    raw_tfv = payload.talent_field_visibility or {}
    tfv = {tid: dict(raw_tfv[tid]) for tid in raw_tfv if tid in talent_ids and isinstance(raw_tfv[tid], dict)}
    if auto_pull or not talent_ids:
        tfv = {}
    now_iso = _now()
    # Track when each subject was first added to this link. Drives "new since
    # last visit" detection on the client view (M1/M3 manual links). For M2
    # auto-pull links the resolver derives `added_at` from the submission's
    # decided_at/created_at instead — this map stays empty there.
    subject_added_at = {sid: now_iso for sid in (talent_ids + submission_ids)}
    doc = {
        "id": str(uuid.uuid4()),
        "slug": _slugify(payload.title),
        "title": payload.title,
        "brand_name": payload.brand_name,
        "talent_ids": talent_ids,
        "submission_ids": submission_ids,
        "subject_added_at": subject_added_at,
        "talent_field_visibility": tfv,
        "auto_pull": auto_pull,
        "auto_project_id": auto_project_id,
        "visibility": vis,
        "is_public": payload.is_public,
        "password": payload.password,
        "notes": payload.notes,
        "client_budget_override": _clean_budget_lines(payload.client_budget_override)
        if payload.client_budget_override
        else None,
        "created_at": now_iso,
        "created_by": admin["id"],
    }
    await db.links.insert_one(doc)
    doc.pop("_id", None)
    doc["view_count"] = 0
    doc["unique_viewers"] = 0
    return doc


@router.get("/links")
async def list_links(
    page: Optional[int] = None,
    size: Optional[int] = None,
    limit: Optional[int] = None,
    admin: dict = Depends(current_team_or_admin),
):
    cursor = db.links.find({}, {"_id": 0}).sort("created_at", -1)
    if page is None and limit is None:
        links = await cursor.to_list(2000)
    else:
        skip, page_size, p, s = _paginate_params(page, size, limit)
        total = await db.links.count_documents({})
        links = await cursor.skip(skip).limit(page_size).to_list(page_size)
    # Bulk-aggregate view stats in a single pipeline — avoids N+1 round-trips
    # on Atlas (2 queries per link was timing out on large datasets).
    link_ids = [link["id"] for link in links]
    view_stats: Dict[str, Dict[str, int]] = {}
    if link_ids:
        async for row in db.link_views.aggregate([
            {"$match": {"link_id": {"$in": link_ids}}},
            {"$group": {
                "_id": "$link_id",
                "count": {"$sum": 1},
                "emails": {"$addToSet": "$viewer_email"},
            }},
        ]):
            view_stats[row["_id"]] = {
                "view_count": row["count"],
                "unique_viewers": len(row.get("emails", [])),
            }
    for link in links:
        stats = view_stats.get(link["id"], {})
        link["view_count"] = stats.get("view_count", 0)
        link["unique_viewers"] = stats.get("unique_viewers", 0)
    if page is None and limit is None:
        return links
    return _paginated(links, total, p, s)


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
    update["auto_pull"] = bool(payload.auto_pull)
    update["auto_project_id"] = (payload.auto_project_id or None) if update["auto_pull"] else None
    if update["auto_pull"]:
        if not update["auto_project_id"]:
            raise HTTPException(400, "auto_project_id is required when auto_pull is enabled")
        proj = await db.projects.find_one({"id": update["auto_project_id"]}, {"_id": 0, "id": 1})
        if not proj:
            raise HTTPException(400, "auto_project_id does not match any project")
        update["talent_ids"] = []
        update["submission_ids"] = []
    elif not update["talent_ids"] and not update["submission_ids"]:
        raise HTTPException(400, "Select at least one talent or submission")
    raw_tfv = update.get("talent_field_visibility") or {}
    update["talent_field_visibility"] = {
        tid: dict(raw_tfv[tid])
        for tid in raw_tfv
        if tid in update["talent_ids"] and isinstance(raw_tfv[tid], dict)
    }
    if update["auto_pull"] or not update["talent_ids"]:
        update["talent_field_visibility"] = {}
    # Preserve existing subject_added_at entries; stamp newly-added subjects with `now`.
    existing_link = await db.links.find_one({"id": lid}, {"_id": 0, "subject_added_at": 1})
    prev_added = (existing_link or {}).get("subject_added_at") or {}
    now_iso = _now()
    keep_ids = set(update["talent_ids"]) | set(update["submission_ids"])
    update["subject_added_at"] = {
        sid: prev_added.get(sid, now_iso) for sid in keep_ids
    }
    override = _clean_budget_lines(update.get("client_budget_override"))
    update["client_budget_override"] = override if override else None
    res = await db.links.update_one({"id": lid}, {"$set": update})
    if not res.matched_count:
        raise HTTPException(404, "Link not found")
    link = await db.links.find_one({"id": lid}, {"_id": 0})
    # Defensive: a parallel delete between update_one and find_one would
    # leave `link` as None. Convert to a clean 404 instead of crashing on
    # the next subscript assignment.
    if link is None:
        raise HTTPException(404, "Link not found")
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
    e = await db.link_events.delete_many({"link_id": {"$in": ids}})
    logger.info(
        "BULK DELETE /links by admin=%s removed=%d views=%d actions=%d downloads=%d events=%d",
        admin.get("email"), res.deleted_count, v.deleted_count, a.deleted_count, d.deleted_count, e.deleted_count,
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
            "events": e.deleted_count,
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
    e = await db.link_events.delete_many({"link_id": lid})
    logger.info(
        "DELETE /links/%s succeeded (by %s); cascade views=%d actions=%d downloads=%d events=%d",
        lid, admin.get("email"), v.deleted_count, a.deleted_count, d.deleted_count, e.deleted_count,
    )
    return {
        "ok": True,
        "deleted_id": lid,
        "cascaded": {
            "views": v.deleted_count,
            "actions": a.deleted_count,
            "downloads": d.deleted_count,
            "events": e.deleted_count,
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

    t_ids = link.get("talent_ids", []) or []
    s_ids = link.get("submission_ids", []) or []
    if link.get("auto_pull") and link.get("auto_project_id"):
        auto_pid = link["auto_project_id"]
        auto_subs = await db.submissions.find(
            {"project_id": auto_pid, "decision": "approved"},
            {"id": 1}
        ).to_list(5000)
        s_ids = list(set(s_ids + [s["id"] for s in auto_subs]))
    ordered_ids = t_ids + s_ids

    # P2-F: Run viewers, actions summary, and downloads concurrently.
    # Action SUMMARY is computed in MongoDB via $group — eliminates loading
    # up to 10 000 rows into Python for in-memory counting.
    # Raw actions list is capped at 500 (frontend only shows viewer comments).
    import asyncio

    async def _fetch_viewers():
        return await db.link_views.find(
            {"link_id": lid}, {"_id": 0}
        ).sort("created_at", -1).to_list(5000)

    async def _fetch_actions_raw():
        # Cap raw actions returned in response to 500 most-recent rows.
        # Frontend uses this only for comments display, not aggregate counts.
        return await db.link_actions.find(
            {"link_id": lid}, {"_id": 0}
        ).sort("updated_at", -1).to_list(500)

    async def _fetch_summary_agg():
        # Compute per-talent counts entirely in MongoDB using $group.
        # O(1) Python allocation regardless of action volume.
        pipeline = [
            {"$match": {"link_id": lid}},
            {"$group": {
                "_id": "$talent_id",
                "shortlist":    {"$sum": {"$cond": [{"$eq": ["$action", "shortlist"]}, 1, 0]}},
                "interested":   {"$sum": {"$cond": [{"$eq": ["$action", "interested"]}, 1, 0]}},
                "not_for_this": {"$sum": {"$cond": [{"$eq": ["$action", "not_for_this"]}, 1, 0]}},
                "not_sure":     {"$sum": {"$cond": [{"$eq": ["$action", "not_sure"]}, 1, 0]}},
            }},
        ]
        rows = await db.link_actions.aggregate(pipeline).to_list(10000)
        return {r["_id"]: r for r in rows}

    async def _fetch_downloads():
        return await db.link_downloads.find(
            {"link_id": lid}, {"_id": 0}
        ).sort("created_at", -1).to_list(1000)

    async def _fetch_events():
        return await db.link_events.find(
            {"link_id": lid}, {"_id": 0}
        ).sort("created_at", -1).to_list(1000)

    viewers, actions_raw, agg_by_tid, downloads, events = await asyncio.gather(
        _fetch_viewers(),
        _fetch_actions_raw(),
        _fetch_summary_agg(),
        _fetch_downloads(),
        _fetch_events(),
    )

    subjects: Dict[str, Dict[str, Any]] = {}
    if t_ids:
        for t in await db.talents.find(
            {"id": {"$in": t_ids}},
            {"_id": 0, "id": 1, "name": 1, "cover_media_id": 1, "media": 1},
        ).to_list(len(t_ids)):
            subjects[t["id"]] = {
                "id": t["id"],
                "name": t.get("name"),
                "source": "talent",
                "cover_media_id": t.get("cover_media_id"),
                "media": t.get("media", []),
            }
    if s_ids:
        for s in await db.submissions.find(
            {"id": {"$in": s_ids}},
            {"_id": 0, "id": 1, "project_id": 1, "talent_name": 1,
             "talent_email": 1, "cover_media_id": 1, "media": {"$slice": 10}},
        ).to_list(len(s_ids)):
            shape = _submission_to_client_shape(s)
            subjects[s["id"]] = {
                "id": s["id"],
                "name": shape["name"],
                "source": "submission",
                "project_id": s.get("project_id"),
                "cover_media_id": shape.get("cover_media_id"),
                "media": shape.get("media", []),
            }

    # Build summary from aggregation result + comments from capped raw list.
    summary: Dict[str, Dict[str, Any]] = {
        tid: {
            "talent_id": tid,
            "shortlist":    agg_by_tid.get(tid, {}).get("shortlist", 0),
            "interested":   agg_by_tid.get(tid, {}).get("interested", 0),
            "not_for_this": agg_by_tid.get(tid, {}).get("not_for_this", 0),
            "not_sure":     agg_by_tid.get(tid, {}).get("not_sure", 0),
            "comments": [],
        }
        for tid in ordered_ids
    }
    # Collect comments from the capped raw actions list.
    for a in actions_raw:
        tid = a.get("talent_id")
        if tid and a.get("comment"):
            if tid not in summary:
                summary[tid] = {
                    "talent_id": tid,
                    "shortlist": 0, "interested": 0,
                    "not_for_this": 0, "not_sure": 0, "comments": [],
                }
            summary[tid]["comments"].append({
                "viewer_email": a.get("viewer_email"),
                "viewer_name": a.get("viewer_name"),
                "comment": a["comment"],
                "updated_at": a.get("updated_at"),
            })

    return {
        "link": link,
        "viewers": viewers,
        "actions": actions_raw,
        "downloads": downloads,
        "events": events,
        "summary": list(summary.values()),
        "subjects": subjects,
        "view_count": len(viewers),
        "unique_viewers": len({v.get("viewer_email") for v in viewers if v.get("viewer_email")}),
    }


# --------------------------------------------------------------------------
# Public client endpoints
# --------------------------------------------------------------------------
@router.post("/public/links/{slug}/identify")
async def identify_viewer(slug: str, payload: IdentifyIn):
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    _require_active_link(link)
    viewer_id = str(uuid.uuid4())
    email = payload.email.lower()
    now = _now()
    await db.link_views.insert_one({
        "id": viewer_id,
        "link_id": link["id"],
        "slug": slug,
        "viewer_email": email,
        "viewer_name": payload.name,
        "created_at": now,
    })
    # Rotate visit timestamps so "what's new since last time" can be computed.
    # First identify in this link → prev_visit_at stays None.
    state = await db.client_states.find_one(
        {"link_id": link["id"], "viewer_email": email}, {"_id": 0}
    )
    if state:
        await db.client_states.update_one(
            {"link_id": link["id"], "viewer_email": email},
            {"$set": {
                "prev_visit_at": state.get("last_visit_at"),
                "last_visit_at": now,
                "updated_at": now,
            }},
        )
    else:
        await db.client_states.insert_one({
            "link_id": link["id"],
            "viewer_email": email,
            "viewer_name": payload.name,
            "seen_talent_ids": [],
            "prev_visit_at": None,
            "last_visit_at": now,
            "updated_at": now,
            "created_at": now,
        })
    token = make_token({
        "role": "viewer",
        "slug": slug,
        "email": email,
        "name": payload.name,
        "viewer_id": viewer_id,
    }, days=7)
    return {"token": token}


@router.post("/public/links/{slug}/seen")
async def mark_seen(
    slug: str,
    payload: SeenIn,
    authorization: Optional[str] = Header(None),
):
    """Mark a talent as viewed by the current client. Idempotent ($addToSet)."""
    viewer = decode_viewer(authorization)
    if not viewer or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")
    link = await db.links.find_one({"slug": slug}, {"_id": 0, "id": 1, "is_public": 1})
    if not link:
        raise HTTPException(404, "Link not found")
    _require_active_link(link)
    await db.client_states.update_one(
        {"link_id": link["id"], "viewer_email": viewer["email"]},
        {
            "$addToSet": {"seen_talent_ids": payload.talent_id},
            "$set": {"updated_at": _now()},
        },
        upsert=True,
    )
    return {"ok": True}


@router.post("/public/links/{slug}/reviewed")
async def mark_reviewed(
    slug: str,
    payload: SeenIn,
    authorization: Optional[str] = Header(None),
):
    """Mark a talent as explicitly reviewed by the current client. Idempotent ($addToSet)."""
    viewer = decode_viewer(authorization)
    if not viewer or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")
    link = await db.links.find_one({"slug": slug}, {"_id": 0, "id": 1, "is_public": 1})
    if not link:
        raise HTTPException(404, "Link not found")
    _require_active_link(link)
    await db.client_states.update_one(
        {"link_id": link["id"], "viewer_email": viewer["email"]},
        {
            "$addToSet": {"reviewed_talent_ids": payload.talent_id},
            "$set": {"updated_at": _now()},
        },
        upsert=True,
    )
    return {"ok": True}


@router.get("/public/links/{slug}")
async def get_public_link(slug: str, authorization: Optional[str] = Header(None)):
    viewer = decode_viewer(authorization)
    if not viewer or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    _require_active_link(link)
    visibility = {**DEFAULT_VISIBILITY, **(link.get("visibility") or {})}
    talent_field_visibility = link.get("talent_field_visibility") or {}

    talent_ids = link.get("talent_ids", []) or []
    submission_ids = link.get("submission_ids", []) or []

    # Auto-pull mode: ignore the curated submission_ids and return ALL
    # currently-approved submissions for the linked project. Lets new
    # approvals appear automatically without re-curating the link.
    if link.get("auto_pull") and link.get("auto_project_id"):
        auto_pid = link["auto_project_id"]
        auto_subs = await db.submissions.find(
            {"project_id": auto_pid, "decision": "approved"},
            {"_id": 0},
        ).sort("created_at", -1).to_list(5000)
        # Effective list — order is "newest first" so the freshest approvals
        # bubble to the top of the client view automatically.
        submission_ids = [s["id"] for s in auto_subs]
        raw_subs = auto_subs
    else:
        raw_subs = []

    raw_talents: List[dict] = []
    if talent_ids:
        raw_talents = await db.talents.find(
            {"id": {"$in": talent_ids}},
            {"_id": 0, "created_by": 0},
        ).to_list(5000)

    if not (link.get("auto_pull") and link.get("auto_project_id")):
        if submission_ids:
            raw_subs = await db.submissions.find(
                {"id": {"$in": submission_ids}},
                {"_id": 0},
            ).to_list(5000)

    t_order = {tid: i for i, tid in enumerate(talent_ids)}
    raw_talents.sort(key=lambda t: t_order.get(t["id"], 999))
    if not link.get("auto_pull"):
        s_order = {sid: i for i, sid in enumerate(submission_ids)}
        raw_subs.sort(key=lambda s: s_order.get(s["id"], 999))

    # Apply per-talent field-visibility overrides for individual share links.
    # If an entry exists in `talent_field_visibility[talent_id]`, it OVERRIDES
    # the link-level visibility for that talent only. Other talents fall back
    # to the link-level visibility as before.
    talents: List[dict] = []
    enriched_talents = [enrich_talent(t) for t in raw_talents]
    for t in enriched_talents:
        per_t = talent_field_visibility.get(t["id"])
        eff_vis = {**visibility, **per_t} if per_t else visibility
        talents.append(_filter_talent_for_client(t, eff_vis))
    for s in raw_subs:
        # Submission objects already carry their own field_visibility; the
        # link-level visibility is applied as the outer envelope (kept the
        # existing behaviour to avoid breaking M2 semantics).
        shape = _submission_to_client_shape(s)
        talents.append(_filter_talent_for_client(shape, visibility))

    # Client-facing project budget (gated by visibility.budget)
    project_budget: List[Dict[str, Any]] = []
    # Client-facing project shoot dates (gated by visibility.availability) —
    # surfaces existing `projects.shoot_dates` so the talent's availability
    # response can render alongside the project's actual shoot window.
    # Sibling array so it stays available even when the budget toggle is off.
    project_shoot_dates: List[Dict[str, Any]] = []
    # Aggregate unique projects ONCE — both budget + shoot_dates branches
    # consume it. Preserves first-seen order for stable rendering.
    seen_pids: List[str] = []
    if raw_subs:
        for s in raw_subs:
            pid = s.get("project_id")
            if pid and pid not in seen_pids:
                seen_pids.append(pid)
    project_meta_by_id: Dict[str, dict] = {}
    if seen_pids:
        proj_docs = await db.projects.find(
            {"id": {"$in": seen_pids}},
            {"_id": 0, "id": 1, "brand_name": 1, "client_budget": 1, "shoot_dates": 1, "talent_budget": 1, "budget_per_day": 1},
        ).to_list(500)
        project_meta_by_id = {p["id"]: p for p in proj_docs}
    if visibility.get("budget"):
        override = link.get("client_budget_override")
        if override:
            project_budget = [{
                "project_id": None,
                "brand_name": link.get("brand_name") or link.get("title"),
                "lines": override,
            }]
        else:
            for pid in seen_pids:
                proj = project_meta_by_id.get(pid)
                if not proj:
                    continue
                lines = proj.get("client_budget") or []
                if lines or proj.get("talent_budget") or proj.get("budget_per_day"):
                    project_budget.append({
                        "project_id": pid,
                        "brand_name": proj.get("brand_name"),
                        "lines": lines,
                        "talent_budget": proj.get("talent_budget") or [],
                        "budget_per_day": proj.get("budget_per_day"),
                    })
    if visibility.get("availability", True):
        for pid in seen_pids:
            proj = project_meta_by_id.get(pid)
            if not proj:
                continue
            sd = (proj.get("shoot_dates") or "").strip()
            if sd:
                project_shoot_dates.append({
                    "project_id": pid,
                    "brand_name": proj.get("brand_name"),
                    "shoot_dates": sd,
                })

    actions = await db.link_actions.find({
        "link_id": link["id"],
        "viewer_email": viewer["email"],
    }, {"_id": 0}).to_list(5000)
    # Client viewing-intelligence state — seen list + visit timestamps so the
    # frontend can render Pending / Seen / New / Shortlisted tabs.
    state = await db.client_states.find_one(
        {"link_id": link["id"], "viewer_email": viewer["email"]}, {"_id": 0}
    ) or {}
    client_state = {
        "seen_talent_ids": state.get("seen_talent_ids", []),
        "reviewed_talent_ids": state.get("reviewed_talent_ids", []),
        "last_visit_at": state.get("last_visit_at"),
        "prev_visit_at": state.get("prev_visit_at"),
    }
    # Per-subject added_at: for manual links read from `link.subject_added_at`;
    # for auto-pull the resolver derives it from submission decided_at/created_at
    # so freshly-approved submissions surface as "New" automatically.
    subject_added_at: Dict[str, str] = dict(link.get("subject_added_at") or {})
    if link.get("auto_pull"):
        for s in raw_subs:
            sid = s["id"]
            subject_added_at[sid] = (
                s.get("decided_at") or s.get("submitted_at") or s.get("created_at") or link.get("created_at")
            )
    return {
        "link": _public_link_view(link),
        "talents": talents,
        "actions": actions,
        "project_budget": project_budget,
        "project_shoot_dates": project_shoot_dates,
        "viewer": {"email": viewer["email"], "name": viewer["name"]},
        "client_state": client_state,
        "subject_added_at": subject_added_at,
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
    _require_active_link(link)

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
    _require_active_link(link)
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


@router.post("/public/links/{slug}/track")
async def track_link_event(
    slug: str,
    payload: LinkTrackIn,
    authorization: Optional[str] = Header(None),
):
    """Securely tracks opened/viewed/watched client analytics inside link document."""
    link = await db.links.find_one({"slug": slug})
    if not link:
        raise HTTPException(404, "Link not found")
        
    now = _now()
    analytics = link.get("analytics") or {}
    
    if not analytics:
        analytics = {
            "opened_at": now,
            "last_viewed_at": now,
            "total_views": 0,
            "unique_views": [],  # session_ids
            "viewed_talents": {},  # talent_id -> count
            "viewed_media": {},  # media_id -> count
            "viewed_primary_take": {},  # media_id -> count
            "watch_durations": {},  # media_id -> total seconds
        }
        
    analytics["last_viewed_at"] = now
    if not analytics.get("opened_at"):
        analytics["opened_at"] = now
        
    # Increment view count
    if payload.event_type == "open":
        analytics["total_views"] = analytics.get("total_views", 0) + 1
        uniq = analytics.get("unique_views") or []
        if payload.session_id not in uniq:
            uniq.append(payload.session_id)
            analytics["unique_views"] = uniq
            
    elif payload.event_type == "view_talent" and payload.talent_id:
        vt = analytics.get("viewed_talents") or {}
        vt[payload.talent_id] = vt.get(payload.talent_id, 0) + 1
        analytics["viewed_talents"] = vt
        
    elif payload.event_type == "view_media" and payload.media_id:
        vm = analytics.get("viewed_media") or {}
        vm[payload.media_id] = vm.get(payload.media_id, 0) + 1
        analytics["viewed_media"] = vm
        
    elif payload.event_type == "watch_video" and payload.media_id:
        wd = analytics.get("watch_durations") or {}
        prev_dur = wd.get(payload.media_id, 0.0)
        wd[payload.media_id] = prev_dur + (payload.watch_time or 0.0)
        analytics["watch_durations"] = wd
        
    await db.links.update_one({"slug": slug}, {"$set": {"analytics": analytics}})

    # Decode viewer securely if possible
    viewer = None
    if authorization:
        try:
            viewer = decode_viewer(authorization)
        except Exception:
            pass

    # Log granular non-creepy timeline events to db.link_events
    event_doc = {
        "id": str(uuid.uuid4()),
        "link_id": link["id"],
        "slug": slug,
        "event_type": payload.event_type,
        "session_id": payload.session_id,
        "talent_id": payload.talent_id,
        "media_id": payload.media_id,
        "watch_time": payload.watch_time,
        "viewer_email": viewer.get("email") if viewer else None,
        "viewer_name": viewer.get("name") if viewer else None,
        "created_at": now,
    }
    await db.link_events.insert_one(event_doc)

    return {"ok": True}


from datetime import datetime, timedelta, timezone
import io
import zipfile
import httpx
from fastapi.responses import StreamingResponse

def privatize_name(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return "Unnamed"
    parts = s.split()
    if len(parts) == 1:
        return parts[0]
    first = parts[0]
    last_initial = parts[-1][0].upper()
    return f"{first} {last_initial}."

def get_safe_filename(talent_name: str, media: dict, idx: int) -> str:
    import os
    name = privatize_name(talent_name)
    cat = media.get("category") or "media"
    label = media.get("label") or ""
    url = media.get("url") or ""
    _, ext = os.path.splitext(url.split("?")[0])
    if not ext:
        ext = ".jpg" if cat in ("portfolio", "indian", "western") else ".mp4"
    
    if cat == "video":
        return f"{name} - Introduction{ext}"
    elif cat in ("take", "take_1", "take_2", "take_3"):
        lbl = label.strip() if label else ""
        if lbl:
            safe_lbl = "".join(c for c in lbl if c.isalnum() or c in (" ", "-", "_")).strip()
            return f"{name} - {safe_lbl}{ext}"
        else:
            return f"{name} - Take {idx}"
    else:
        return f"{name} - {cat.capitalize()} {idx}{ext}"

def get_bundle_filename(talent_name: str, media: dict, idx: int) -> str:
    import os
    folder_name = privatize_name(talent_name).replace(".", "").strip()
    cat = media.get("category") or "media"
    label = media.get("label") or ""
    url = media.get("url") or ""
    _, ext = os.path.splitext(url.split("?")[0])
    if not ext:
        ext = ".jpg" if cat in ("portfolio", "indian", "western") else ".mp4"
    
    if cat == "video":
        filename = f"Introduction{ext}"
    elif cat in ("take", "take_1", "take_2", "take_3"):
        lbl = label.strip() if label else ""
        if lbl:
            safe_lbl = "".join(c for c in lbl if c.isalnum() or c in (" ", "-", "_")).strip()
            filename = f"{safe_lbl}{ext}"
        else:
            filename = f"Take {idx}{ext}"
    else:
        filename = f"{cat.capitalize()} {idx}{ext}"
        
    return f"{folder_name}/{filename}"


@router.post("/public/links/{slug}/share")
async def create_share_link(
    slug: str,
    payload: LinkShareIn,
    authorization: Optional[str] = Header(None),
):
    viewer = decode_viewer(authorization)
    if not viewer or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    _require_active_link(link)

    # Verify that the talent is indeed associated with this link
    talent_id = payload.talent_id
    talent_ids = link.get("talent_ids", []) or []
    submission_ids = link.get("submission_ids", []) or []

    if link.get("auto_pull") and link.get("auto_project_id"):
        auto_pid = link["auto_project_id"]
        auto_subs = await db.submissions.find(
            {"project_id": auto_pid, "decision": "approved"},
            {"_id": 0},
        ).to_list(5000)
        submission_ids = [s["id"] for s in auto_subs]

    is_valid = False
    if talent_id in talent_ids or talent_id in submission_ids:
        is_valid = True
    else:
        sub_docs = await db.submissions.find(
            {"id": {"$in": submission_ids}},
            {"_id": 0}
        ).to_list(5000)
        if any(s["id"] == talent_id or s.get("talent_id") == talent_id for s in sub_docs):
            is_valid = True

    if not is_valid:
        raise HTTPException(404, "Talent not found in this link")

    share_id = str(uuid.uuid4())
    now_dt = datetime.now(timezone.utc)
    expires_dt = now_dt + timedelta(hours=48)
    
    share_doc = {
        "id": share_id,
        "link_id": link["id"],
        "slug": slug,
        "talent_id": talent_id,
        "media_id": payload.media_id,
        "created_at": _now(),
        "expires_at": expires_dt.isoformat(),
    }
    await db.link_shares.insert_one(share_doc)
    
    return {
        "share_id": share_id,
        "expires_at": share_doc["expires_at"],
    }


@router.get("/public/shares/{share_id}")
async def get_share_preview(share_id: str):
    share = await db.link_shares.find_one({"id": share_id}, {"_id": 0})
    if not share:
        raise HTTPException(404, "Share not found")
        
    now_iso = _now()
    if share.get("expires_at") and share["expires_at"] < now_iso:
        raise HTTPException(410, "This preview link has expired")

    slug = share["slug"]
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Associated link not found")
    _require_active_link(link)

    talent_id = share["talent_id"]
    media_id = share.get("media_id")

    talent_ids = link.get("talent_ids", []) or []
    submission_ids = link.get("submission_ids", []) or []

    if link.get("auto_pull") and link.get("auto_project_id"):
        auto_pid = link["auto_project_id"]
        auto_subs = await db.submissions.find(
            {"project_id": auto_pid, "decision": "approved"},
            {"_id": 0},
        ).to_list(5000)
        submission_ids = [s["id"] for s in auto_subs]

    is_sub = False
    talent_doc = None
    if talent_id in submission_ids:
        sub = await db.submissions.find_one({"id": talent_id}, {"_id": 0})
        if not sub:
            raise HTTPException(404, "Talent not found")
        talent_doc = _submission_to_client_shape(sub)
        is_sub = True
    elif talent_id in talent_ids:
        direct_talent = await db.talents.find_one({"id": talent_id}, {"_id": 0})
        if not direct_talent:
            raise HTTPException(404, "Talent not found")
        talent_doc = direct_talent
    else:
        sub_docs = await db.submissions.find(
            {"id": {"$in": submission_ids}},
            {"_id": 0}
        ).to_list(5000)
        matching_sub = next((s for s in sub_docs if s["id"] == talent_id or s.get("talent_id") == talent_id), None)
        if not matching_sub:
            raise HTTPException(404, "Talent not found in this link")
        talent_doc = _submission_to_client_shape(matching_sub)
        is_sub = True

    visibility = {**DEFAULT_VISIBILITY, **(link.get("visibility") or {})}
    if not is_sub:
        talent_doc = enrich_talent(talent_doc)
        per_t = (link.get("talent_field_visibility") or {}).get(talent_doc["id"])
        eff_vis = {**visibility, **per_t} if per_t else visibility
        filtered_talent = _filter_talent_for_client(talent_doc, eff_vis)
    else:
        filtered_talent = _filter_talent_for_client(talent_doc, visibility)

    if media_id:
        filtered_media = [m for m in filtered_talent.get("media", []) if m.get("id") == media_id]
        if not filtered_media:
            raise HTTPException(404, "Shared media is not available")
        filtered_talent["media"] = filtered_media

    return {
        "link": _public_link_view(link),
        "talent": filtered_talent,
        "share_id": share_id,
        "expires_at": share["expires_at"],
    }


@router.get("/public/links/{slug}/download/talent/{talent_id}")
async def download_talent_zip(
    slug: str,
    talent_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = None,
):
    t = token
    if not t and authorization and authorization.lower().startswith("bearer "):
        t = authorization.split(" ", 1)[1]
    if not t:
        raise HTTPException(401, "Identity required")
    viewer = decode_token(t)
    if not viewer or viewer.get("role") != "viewer" or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")

    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    _require_active_link(link)
    
    if not link.get("visibility", {}).get("download"):
        raise HTTPException(403, "Downloads disabled")

    talent_ids = link.get("talent_ids", []) or []
    submission_ids = link.get("submission_ids", []) or []

    if link.get("auto_pull") and link.get("auto_project_id"):
        auto_pid = link["auto_project_id"]
        auto_subs = await db.submissions.find(
            {"project_id": auto_pid, "decision": "approved"},
            {"_id": 0},
        ).to_list(5000)
        submission_ids = [s["id"] for s in auto_subs]
        
    is_sub = False
    talent_doc = None
    if talent_id in talent_ids:
        direct_talent = await db.talents.find_one({"id": talent_id}, {"_id": 0})
        if not direct_talent:
            raise HTTPException(404, "Talent not found")
        talent_doc = direct_talent
    elif talent_id in submission_ids:
        sub = await db.submissions.find_one({"id": talent_id}, {"_id": 0})
        if not sub:
            raise HTTPException(404, "Talent/Submission not found")
        talent_doc = _submission_to_client_shape(sub)
        is_sub = True
    else:
        sub_docs = await db.submissions.find(
            {"id": {"$in": submission_ids}},
            {"_id": 0}
        ).to_list(5000)
        matching_sub = next((s for s in sub_docs if s["id"] == talent_id or s.get("talent_id") == talent_id), None)
        if not matching_sub:
            raise HTTPException(404, "Talent not found in this link")
        talent_doc = _submission_to_client_shape(matching_sub)
        is_sub = True

    if not is_sub:
        talent_doc = enrich_talent(talent_doc)
        per_t = (link.get("talent_field_visibility") or {}).get(talent_doc["id"])
        eff_vis = {**link.get("visibility", {}), **per_t} if per_t else link.get("visibility", {})
        filtered_talent = _filter_talent_for_client(talent_doc, eff_vis)
    else:
        filtered_talent = _filter_talent_for_client(talent_doc, link.get("visibility", {}))

    media_list = filtered_talent.get("media", [])
    if not media_list:
        raise HTTPException(404, "No downloadable media for this talent")

    zip_items = []
    counts = {}
    for m in media_list:
        cat = m.get("category") or "media"
        counts[cat] = counts.get(cat, 0) + 1
        fn = get_safe_filename(filtered_talent.get("name") or "Talent", m, counts[cat])
        zip_items.append({"filename": fn, "url": m["url"]})

    async def event_generator():
        buffer = io.BytesIO()
        read_offset = 0
        async with httpx.AsyncClient() as client:
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in zip_items:
                    filename = item["filename"]
                    url = item["url"]
                    try:
                        with zf.open(filename, "w") as dest:
                            async with client.stream("GET", url) as response:
                                if response.status_code == 200:
                                    async for chunk in response.iter_bytes(chunk_size=65536):
                                        dest.write(chunk)
                                        buffer.seek(0, io.SEEK_END)
                                        current_pos = buffer.tell()
                                        if current_pos > read_offset:
                                            buffer.seek(read_offset)
                                            yield_chunk = buffer.read(current_pos - read_offset)
                                            read_offset = current_pos
                                            yield yield_chunk
                    except Exception as e:
                        logger.error(f"Error zipping {filename} from {url}: {e}")
            zf.close()
            buffer.seek(0, io.SEEK_END)
            current_pos = buffer.tell()
            if current_pos > read_offset:
                buffer.seek(read_offset)
                yield buffer.read(current_pos - read_offset)

    safe_name = privatize_name(filtered_talent.get("name")).replace(".", "").strip()
    zip_filename = f"{safe_name}_Portfolio.zip"
    return StreamingResponse(
        event_generator(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
    )


@router.get("/public/links/{slug}/download/bundle")
async def download_campaign_bundle_zip(
    slug: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = None,
):
    t = token
    if not t and authorization and authorization.lower().startswith("bearer "):
        t = authorization.split(" ", 1)[1]
    if not t:
        raise HTTPException(401, "Identity required")
    viewer = decode_token(t)
    if not viewer or viewer.get("role") != "viewer" or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")

    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    _require_active_link(link)
    
    if not link.get("visibility", {}).get("download"):
        raise HTTPException(403, "Downloads disabled")

    talent_ids = link.get("talent_ids", []) or []
    submission_ids = link.get("submission_ids", []) or []

    if link.get("auto_pull") and link.get("auto_project_id"):
        auto_pid = link["auto_project_id"]
        auto_subs = await db.submissions.find(
            {"project_id": auto_pid, "decision": "approved"},
            {"_id": 0},
        ).to_list(5000)
        submission_ids = [s["id"] for s in auto_subs]
        raw_subs = auto_subs
    else:
        raw_subs = []

    raw_talents: List[dict] = []
    if talent_ids:
        raw_talents = await db.talents.find(
            {"id": {"$in": talent_ids}},
            {"_id": 0, "created_by": 0},
        ).to_list(5000)

    if not (link.get("auto_pull") and link.get("auto_project_id")):
        if submission_ids:
            raw_subs = await db.submissions.find(
                {"id": {"$in": submission_ids}},
                {"_id": 0},
            ).to_list(5000)

    t_order = {tid: i for i, tid in enumerate(talent_ids)}
    raw_talents.sort(key=lambda t: t_order.get(t["id"], 999))
    if not link.get("auto_pull"):
        s_order = {sid: i for i, sid in enumerate(submission_ids)}
        raw_subs.sort(key=lambda s: s_order.get(s["id"], 999))

    visibility = {**DEFAULT_VISIBILITY, **(link.get("visibility") or {})}
    talent_field_visibility = link.get("talent_field_visibility") or {}

    talents: List[dict] = []
    enriched_talents = [enrich_talent(t) for t in raw_talents]
    for t in enriched_talents:
        per_t = talent_field_visibility.get(t["id"])
        eff_vis = {**visibility, **per_t} if per_t else visibility
        talents.append(_filter_talent_for_client(t, eff_vis))
    for s in raw_subs:
        shape = _submission_to_client_shape(s)
        talents.append(_filter_talent_for_client(shape, visibility))

    zip_items = []
    for t_doc in talents:
        t_name = t_doc.get("name") or "Talent"
        media_list = t_doc.get("media", [])
        if not media_list:
            continue
        counts = {}
        for m in media_list:
            cat = m.get("category") or "media"
            counts[cat] = counts.get(cat, 0) + 1
            fn = get_bundle_filename(t_name, m, counts[cat])
            zip_items.append({"filename": fn, "url": m["url"]})

    if not zip_items:
        raise HTTPException(404, "No downloadable media for this campaign")

    async def event_generator():
        buffer = io.BytesIO()
        read_offset = 0
        async with httpx.AsyncClient() as client:
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in zip_items:
                    filename = item["filename"]
                    url = item["url"]
                    try:
                        with zf.open(filename, "w") as dest:
                            async with client.stream("GET", url) as response:
                                if response.status_code == 200:
                                    async for chunk in response.iter_bytes(chunk_size=65536):
                                        dest.write(chunk)
                                        buffer.seek(0, io.SEEK_END)
                                        current_pos = buffer.tell()
                                        if current_pos > read_offset:
                                            buffer.seek(read_offset)
                                            yield_chunk = buffer.read(current_pos - read_offset)
                                            read_offset = current_pos
                                            yield yield_chunk
                    except Exception as e:
                        logger.error(f"Error zipping {filename} from {url}: {e}")
            zf.close()
            buffer.seek(0, io.SEEK_END)
            current_pos = buffer.tell()
            if current_pos > read_offset:
                buffer.seek(read_offset)
                yield buffer.read(current_pos - read_offset)

    campaign_name = "".join(c for c in link.get("title", "Campaign") if c.isalnum() or c in (" ", "-", "_")).strip()
    zip_filename = f"{campaign_name}_Campaign_Bundle.zip"
    return StreamingResponse(
        event_generator(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
    )
