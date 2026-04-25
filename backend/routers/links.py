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
    elif not talent_ids and not submission_ids:
        raise HTTPException(400, "Select at least one talent or submission")
    # Constrain talent_field_visibility to the talent_ids attached to the link
    # so we don't accumulate stale entries when admins edit talent lists.
    raw_tfv = payload.talent_field_visibility or {}
    tfv = {tid: dict(raw_tfv[tid]) for tid in raw_tfv if tid in talent_ids and isinstance(raw_tfv[tid], dict)}
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
    admin: dict = Depends(current_team_or_admin),
):
    cursor = db.links.find({}, {"_id": 0}).sort("created_at", -1)
    if page is None:
        links = await cursor.to_list(2000)
    else:
        skip, limit, p, s = _paginate_params(page, size)
        total = await db.links.count_documents({})
        links = await cursor.skip(skip).limit(limit).to_list(limit)
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
    if page is None:
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
    elif not update["talent_ids"] and not update["submission_ids"]:
        raise HTTPException(400, "Select at least one talent or submission")
    raw_tfv = update.get("talent_field_visibility") or {}
    update["talent_field_visibility"] = {
        tid: dict(raw_tfv[tid])
        for tid in raw_tfv
        if tid in update["talent_ids"] and isinstance(raw_tfv[tid], dict)
    }
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
    link = await db.links.find_one({"slug": slug}, {"_id": 0, "id": 1})
    if not link:
        raise HTTPException(404, "Link not found")
    await db.client_states.update_one(
        {"link_id": link["id"], "viewer_email": viewer["email"]},
        {
            "$addToSet": {"seen_talent_ids": payload.talent_id},
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
    # Client viewing-intelligence state — seen list + visit timestamps so the
    # frontend can render Pending / Seen / New / Shortlisted tabs.
    state = await db.client_states.find_one(
        {"link_id": link["id"], "viewer_email": viewer["email"]}, {"_id": 0}
    ) or {}
    client_state = {
        "seen_talent_ids": state.get("seen_talent_ids", []),
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
