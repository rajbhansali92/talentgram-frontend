import logging
import re
import os
import uuid
import httpx
import asyncio
import boto3
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
import cloudinary.api
import cloudinary.uploader
import cloudinary.exceptions
from core import (
    current_team_or_admin,
    require_role,
    db,
    log_storage_action,
    cloudinary_destroy,
    cleanup_media_storage,
    get_r2_client,
    R2_BUCKET_NAME,
    _now,
    remove_synced_media_from_global_talent,
    check_cloudinary_health,
    check_r2_health,
    LEGACY_TAKE_CATEGORIES,
)

router = APIRouter(prefix="/api/admin/cloudinary", tags=["cloudinary-admin"])

async def assert_providers_healthy():
    """Cloudinary is the only provider any current media actually lives on
    (confirmed: zero submissions/talents/applications reference an R2-backed
    asset in production — R2 uploads were fully retired 2026-08-09/10), so
    only a Cloudinary outage blocks a deletion. R2 is checked for visibility/
    logging only and never blocks: cleanup_media_storage()'s own R2 delete
    call is already best-effort and never raises, so a real R2 outage cannot
    leave a Cloudinary deletion half-done. This also fixes a live production
    bug — the connected Cloudflare account currently returns 403 Forbidden
    on R2 head_bucket (R2 isn't entitled on the account), which meant every
    single admin storage-deletion action was unconditionally 503ing before
    this fix, regardless of Cloudinary's own health."""
    from core import ENABLE_R2_MEDIA_PIPELINE, R2_ENDPOINT_URL
    cld_ok = await check_cloudinary_health()
    if not cld_ok:
        logger.error("assert_providers_healthy: Cloudinary is unreachable")
        raise HTTPException(
            status_code=503,
            detail="Storage cleanup aborted. Cloudinary is currently unreachable. No changes have been made."
        )
    if ENABLE_R2_MEDIA_PIPELINE or R2_ENDPOINT_URL:
        r2_ok = await check_r2_health()
        if not r2_ok:
            logger.warning("assert_providers_healthy: Cloudflare R2 is unreachable or misconfigured — proceeding, since no current media is R2-backed")
logger = logging.getLogger(__name__)

# Quotas and defaults (in bytes)
CLOUDINARY_QUOTA_DEFAULT = 25 * 1024 * 1024 * 1024  # 25 GB
R2_QUOTA_DEFAULT = 100 * 1024 * 1024 * 1024         # 100 GB

def safe_get_usage(metric: Any, default_limit: int = 0) -> tuple:
    """Extract (usage, limit) from a Cloudinary metric value defensively."""
    if isinstance(metric, dict):
        usage = metric.get("usage")
        limit = metric.get("limit")
        try:
            u = int(usage) if usage is not None else 0
        except (ValueError, TypeError):
            u = 0
        try:
            l = int(limit) if limit is not None else default_limit
        except (ValueError, TypeError):
            l = default_limit
        return u, l
    elif isinstance(metric, (int, float)):
        return int(metric), default_limit
    else:
        return 0, default_limit

def fetch_cloudinary_usage_sync() -> Dict[str, Any]:
    try:
        res = cloudinary.api.usage()
        return res if isinstance(res, dict) else {}
    except Exception as e:
        logger.warning(f"Failed to fetch Cloudinary usage: {e}")
        return {}

def fetch_r2_objects_sync() -> tuple:
    s3 = get_r2_client()
    if not s3:
        return 0, 0
    total_size = 0
    object_count = 0
    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=R2_BUCKET_NAME):
            if 'Contents' in page:
                for obj in page['Contents']:
                    total_size += obj['Size']
                    object_count += 1
    except Exception as e:
        logger.warning(f"Error listing R2 objects in health check: {e}")
        raise e
    return total_size, object_count

def list_r2_physical_objects_sync() -> List[Dict[str, Any]]:
    s3 = get_r2_client()
    if not s3:
        return []
    objects = []
    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=R2_BUCKET_NAME):
            if 'Contents' in page:
                for obj in page['Contents']:
                    objects.append({
                        "key": obj['Key'],
                        "size": obj['Size'],
                        "last_modified": obj['LastModified']
                    })
    except Exception as e:
        logger.warning(f"Error in list_r2_physical_objects_sync: {e}")
    return objects

def list_cloudinary_physical_resources_sync() -> List[Dict[str, Any]]:
    resources = []
    try:
        # Fetch both images and videos
        for rtype in ["image", "video", "raw"]:
            next_cursor = None
            while True:
                res = cloudinary.api.resources(
                    resource_type=rtype,
                    max_results=500,
                    next_cursor=next_cursor
                )
                for item in res.get("resources", []):
                    resources.append({
                        "public_id": item["public_id"],
                        "size": item.get("bytes") or 0,
                        "resource_type": rtype,
                        "url": item.get("secure_url") or item.get("url")
                    })
                next_cursor = res.get("next_cursor")
                if not next_cursor:
                    break
    except Exception as e:
        logger.warning(f"Error in list_cloudinary_physical_resources_sync: {e}")
    return resources

def classify_media_item(m: Dict[str, Any]) -> str:
    """Bucket a submission/application media item into the same category
    keys the Storage Console displays. Admin-added media is always bucketed
    as admin_uploads regardless of its underlying category, matching the
    prior asset_metadata-based classification's behavior."""
    if m.get("scope") == "admin_added" or m.get("admin_added"):
        return "admin_uploads"
    cat = m.get("category")
    if cat == "take" or cat in LEGACY_TAKE_CATEGORIES:
        return "audition_videos"
    if cat == "intro_video":
        return "intro_videos"
    if cat == "indian":
        return "indian_look_images"
    if cat == "western":
        return "western_look_images"
    return "portfolio_images"


def new_category_buckets() -> Dict[str, Dict[str, Any]]:
    return {
        "audition_videos": {"size": 0, "count": 0, "label": "Audition Videos"},
        "intro_videos": {"size": 0, "count": 0, "label": "Introduction Videos"},
        "portfolio_images": {"size": 0, "count": 0, "label": "Portfolio (General) Images"},
        "indian_look_images": {"size": 0, "count": 0, "label": "Indian Look Images"},
        "western_look_images": {"size": 0, "count": 0, "label": "Western Look Images"},
        "voice_notes": {"size": 0, "count": 0, "label": "Voice Notes"},
        "admin_uploads": {"size": 0, "count": 0, "label": "Admin Uploads"},
    }


async def compute_category_breakdown() -> Dict[str, Dict[str, Any]]:
    """Accuracy fix (Phase 5): category sizes now come directly from the
    `size` field stored on submissions.media[] / applications.media[] at
    upload time — the same byte count Cloudinary returned in its upload
    response (see attach_video_media / video_complete / submission_upload
    in routers/submissions.py). This replaces the previous dependency on
    db.asset_metadata.file_size, which is never populated for the direct
    video-upload path (video-signature -> attach_video_media only ever
    flips upload_status, it never copies over the byte size) — the exact
    reason Audition Videos / Introduction Videos always rendered as 0 Bytes
    despite real files existing. Voice notes are tracked separately below
    since their size *is* written correctly at insert time
    (routers/feedback.py) and they don't live in submissions.media[]."""
    categories = new_category_buckets()

    pipeline = [
        {"$match": {"media": {"$exists": True, "$ne": []}}},
        {"$unwind": "$media"},
        {"$project": {"category": "$media.category", "size": {"$ifNull": ["$media.size", 0]},
                       "scope": "$media.scope", "admin_added": "$media.admin_added"}},
    ]
    for coll in (db.submissions, db.applications):
        try:
            items = await coll.aggregate(pipeline).to_list(length=200000)
            for item in items:
                bucket = classify_media_item(item)
                categories[bucket]["size"] += item.get("size") or 0
                categories[bucket]["count"] += 1
        except Exception as e:
            logger.error(f"Mongo: media aggregate failed for {coll.name}: {e}")

    # Voice notes: still sourced from asset_metadata, whose file_size *is*
    # written correctly at insert time by routers/feedback.py.
    try:
        vn_cursor = db.asset_metadata.aggregate([
            {"$match": {"asset_type": "voice_note"}},
            {"$group": {"_id": None, "total_size": {"$sum": "$file_size"}, "count": {"$sum": 1}}}
        ])
        vn_list = await vn_cursor.to_list(length=1)
        if vn_list:
            categories["voice_notes"]["size"] = vn_list[0].get("total_size") or 0
            categories["voice_notes"]["count"] = vn_list[0].get("count") or 0
    except Exception as e:
        logger.error(f"Mongo: voice_notes aggregate failed: {e}")

    voice_feedback_count = 0
    try:
        voice_feedback_count = await db.feedback.count_documents({"type": "voice"})
    except Exception as e:
        logger.error(f"Mongo: feedback count_documents failed: {e}")
    if categories["voice_notes"]["count"] < voice_feedback_count:
        # Legacy voice feedback rows predating asset_metadata tracking —
        # counted, but their size genuinely isn't known without a live
        # Cloudinary lookup, so we don't fabricate an estimate for them.
        categories["voice_notes"]["count"] = voice_feedback_count

    return categories


@router.get("/summary")
async def get_storage_summary(admin: dict = Depends(require_role("admin"))):
    """Fast top-level Cloudinary account usage — a single live Admin API
    call (~1s measured), no per-object listing. This is what the Storage
    Console's top cards render on initial load (Phase 6 performance fix);
    the exhaustive per-object scan formerly fired on every page load lives
    only behind GET /health now, triggered explicitly by Re-Scan."""
    cld_live = await run_in_threadpool(fetch_cloudinary_usage_sync)
    if not cld_live:
        return {"status": "unavailable", "storage_bytes": 0, "object_count": 0}

    storage_bytes, _ = safe_get_usage(cld_live.get("storage"))
    object_count, _ = safe_get_usage(cld_live.get("objects"))
    bandwidth_bytes, _ = safe_get_usage(cld_live.get("bandwidth"))
    credits = cld_live.get("credits") or {}

    return {
        "status": "healthy",
        "plan": cld_live.get("plan"),
        "last_updated": cld_live.get("last_updated"),
        "storage_bytes": storage_bytes,
        "object_count": object_count,
        "bandwidth_bytes": bandwidth_bytes,
        "resources": cld_live.get("resources"),
        "derived_resources": cld_live.get("derived_resources"),
        # Cloudinary's Pay-As-You-Go plans meter usage in "credits" (storage +
        # bandwidth + transformations combined), not a flat storage GB cap —
        # there is no fixed "25 GB limit" to compute a percentage against.
        # Surface the real credit usage instead of fabricating one.
        "credits_used": credits.get("usage"),
        "credits_limit": credits.get("limit"),
        "credits_used_percent": credits.get("used_percent"),
    }


@router.get("/analytics")
async def get_storage_analytics(admin: dict = Depends(require_role("admin"))):
    """Compute aggregates over tracked assets metadata across Cloudinary and R2."""
    import time
    start_time = time.monotonic()
    op_id = str(uuid.uuid4())

    # 1. Cloudinary Usage
    cld_live = {}
    cld_status = "healthy"
    cld_err_reason = None
    try:
        cld_live = await run_in_threadpool(fetch_cloudinary_usage_sync)
        if not cld_live:
            cld_status = "unavailable"
            cld_err_reason = "Empty response returned from Cloudinary API"
            logger.warning(
                f"Provider: Cloudinary | Reason: {cld_err_reason} | "
                f"Operation ID: {op_id} | Endpoint: /analytics | "
                f"Duration: {time.monotonic() - start_time:.4f}s"
            )
    except Exception as e:
        cld_status = "unavailable"
        cld_err_reason = str(e)
        logger.error(
            f"Provider: Cloudinary | Reason: {cld_err_reason} | "
            f"Operation ID: {op_id} | Endpoint: /analytics | "
            f"Duration: {time.monotonic() - start_time:.4f}s"
        )
        
    cld_used, cld_quota = safe_get_usage(cld_live.get("storage"), CLOUDINARY_QUOTA_DEFAULT)
    cld_count, _ = safe_get_usage(cld_live.get("objects"))
    cld_bandwidth_used, _ = safe_get_usage(cld_live.get("bandwidth"))
    cld_requests_used, _ = safe_get_usage(cld_live.get("requests"))
    
    # 2. Cloudflare R2 Usage
    r2_used, r2_count = 0, 0
    r2_status = "healthy"
    r2_err_reason = None
    
    from core import R2_ENDPOINT_URL, ENABLE_R2_MEDIA_PIPELINE
    if not R2_ENDPOINT_URL:
        r2_status = "disabled"
        r2_err_reason = "R2_ENDPOINT_URL is not configured"
        logger.info(
            f"Provider: Cloudflare R2 | Reason: {r2_err_reason} | "
            f"Operation ID: {op_id} | Endpoint: /analytics | "
            f"Duration: {time.monotonic() - start_time:.4f}s"
        )
    elif not ENABLE_R2_MEDIA_PIPELINE:
        r2_status = "disabled"
        r2_err_reason = "ENABLE_R2_MEDIA_PIPELINE is set to false"
        logger.info(
            f"Provider: Cloudflare R2 | Reason: {r2_err_reason} | "
            f"Operation ID: {op_id} | Endpoint: /analytics | "
            f"Duration: {time.monotonic() - start_time:.4f}s"
        )
    else:
        try:
            r2_used, r2_count = await run_in_threadpool(fetch_r2_objects_sync)
        except Exception as e:
            r2_status = "unavailable"
            r2_err_reason = str(e)
            logger.error(
                f"Provider: Cloudflare R2 | Reason: {r2_err_reason} | "
                f"Operation ID: {op_id} | Endpoint: /analytics | "
                f"Duration: {time.monotonic() - start_time:.4f}s"
            )
            
    r2_quota = R2_QUOTA_DEFAULT
    
    # 3. Combined Metrics
    total_used = cld_used + r2_used
    total_quota = cld_quota + r2_quota
    total_objects = cld_count + r2_count
    
    # Category breakdown — see compute_category_breakdown() docstring for why
    # this reads submissions/applications.media[].size instead of asset_metadata.
    categories = await compute_category_breakdown()

    # Per-project totals, sourced the same way (real upload-time byte sizes),
    # used for both "Top Campaigns" and archived-project storage below.
    project_totals: Dict[Optional[str], int] = {}
    proj_pipeline = [
        {"$match": {"media": {"$exists": True, "$ne": []}}},
        {"$unwind": "$media"},
        {"$group": {"_id": "$project_id", "total_size": {"$sum": {"$ifNull": ["$media.size", 0]}}}},
    ]
    for coll in (db.submissions, db.applications):
        try:
            items = await coll.aggregate(proj_pipeline).to_list(length=10000)
            for item in items:
                pid = item.get("_id")
                project_totals[pid] = project_totals.get(pid, 0) + (item.get("total_size") or 0)
        except Exception as e:
            logger.error(f"Mongo: project totals aggregate failed for {coll.name}: {e}")

    archived_storage = 0
    try:
        archived_ids = await db.projects.find({"status": "archived"}, {"id": 1}).to_list(length=10000)
        archived_storage = sum(project_totals.get(p["id"], 0) for p in archived_ids)
    except Exception as e:
        logger.error(f"Mongo: archived storage lookup failed: {e}")

    top_projects_sorted = sorted(
        (p for p in project_totals.items() if p[0]), key=lambda kv: kv[1], reverse=True
    )[:5]
    enriched_top_projects = []
    for pid, size in top_projects_sorted:
        name = pid
        try:
            proj = await db.projects.find_one({"id": pid}, {"name": 1, "brand_name": 1})
            if isinstance(proj, dict):
                name = proj.get("brand_name") or proj.get("name") or pid
        except Exception:
            pass
        enriched_top_projects.append({"project_id": pid, "name": name, "size": size})

    # Top storage consuming talents — same real-byte-size source.
    talent_totals: Dict[str, int] = {}
    talent_pipeline = [
        {"$match": {"media": {"$exists": True, "$ne": []}}},
        {"$unwind": "$media"},
        {"$group": {"_id": {"$ifNull": ["$talent_id", "unknown_talent"]}, "total_size": {"$sum": {"$ifNull": ["$media.size", 0]}}}},
    ]
    for coll in (db.submissions, db.applications):
        try:
            items = await coll.aggregate(talent_pipeline).to_list(length=10000)
            for item in items:
                tid = item.get("_id") or "unknown_talent"
                talent_totals[tid] = talent_totals.get(tid, 0) + (item.get("total_size") or 0)
        except Exception as e:
            logger.error(f"Mongo: talent totals aggregate failed for {coll.name}: {e}")

    top_talents_sorted = sorted(talent_totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
    enriched_top_talents = []
    for tid, size in top_talents_sorted:
        name = tid
        if tid != "unknown_talent":
            try:
                talent = await db.talents.find_one({"id": tid}, {"name": 1})
                if isinstance(talent, dict):
                    name = talent.get("name") or tid
            except Exception:
                pass
        enriched_top_talents.append({"talent_id": tid, "name": name, "size": size})

    return {
        "total_storage": total_used,
        "total_quota": total_quota,
        "total_object_count": total_objects,
        "providers": {
            "cloudinary": {
                "name": "Cloudinary",
                "status": cld_status,
                "error_reason": cld_err_reason,
                "used_bytes": cld_used,
                "quota": cld_quota,
                "remaining_capacity": max(0, cld_quota - cld_used),
                "object_count": cld_count,
                "bandwidth_used": cld_bandwidth_used,
                "api_usage": cld_requests_used
            },
            "cloudflare_r2": {
                "name": "Cloudflare R2",
                "status": r2_status,
                "error_reason": r2_err_reason,
                "used_bytes": r2_used,
                "quota": r2_quota,
                "remaining_capacity": max(0, r2_quota - r2_used),
                "object_count": r2_count,
                "bandwidth_used": 0,
                "api_usage": 0
            }
        },
        "categories": categories,
        "permanent_storage": categories["intro_videos"]["size"] + categories["portfolio_images"]["size"] + categories["indian_look_images"]["size"] + categories["western_look_images"]["size"],
        "temporary_storage": categories["audition_videos"]["size"] + categories["voice_notes"]["size"] + categories["admin_uploads"]["size"],
        "archived_storage": archived_storage,
        "top_projects": enriched_top_projects,
        "top_talents": enriched_top_talents,
        "average_audition_size": categories["audition_videos"]["size"] / max(1, categories["audition_videos"]["count"]),
        "total_auditions": categories["audition_videos"]["count"]
    }

async def aggregate_project_talent_totals() -> Dict[str, Dict[str, Any]]:
    """Per-project: real total bytes (from media[].size, see
    compute_category_breakdown docstring) + the set of distinct talents with
    media in that project. Cheap single-pass Mongo aggregation (~0.2s
    measured against production data) — no live Cloudinary calls."""
    totals: Dict[str, Dict[str, Any]] = {}
    pipeline = [
        {"$match": {"media": {"$exists": True, "$ne": []}}},
        {"$unwind": "$media"},
        {"$group": {
            "_id": "$project_id",
            "total_size": {"$sum": {"$ifNull": ["$media.size", 0]}},
            "asset_count": {"$sum": 1},
            "talent_ids": {"$addToSet": {"$ifNull": ["$talent_id", "unknown_talent"]}},
        }},
    ]
    for coll in (db.submissions, db.applications):
        try:
            items = await coll.aggregate(pipeline).to_list(length=10000)
            for item in items:
                pid = item.get("_id")
                if pid not in totals:
                    totals[pid] = {"total_size": 0, "asset_count": 0, "talent_ids": set()}
                totals[pid]["total_size"] += item.get("total_size") or 0
                totals[pid]["asset_count"] += item.get("asset_count") or 0
                totals[pid]["talent_ids"].update(item.get("talent_ids") or [])
        except Exception as e:
            logger.error(f"Mongo: project/talent totals aggregate failed for {coll.name}: {e}")
    return totals


@router.get("/projects")
async def get_projects_storage(admin: dict = Depends(require_role("admin"))):
    """Retrieve storage breakdowns for all projects: real total bytes and
    distinct talent count. Talent/media detail is intentionally NOT included
    here — it's loaded lazily via GET /projects/{id}/talents only when the
    admin expands a project (Phase 6 performance fix)."""
    totals = await aggregate_project_talent_totals()

    cursor_projects = db.projects.find({}, {"id": 1, "name": 1, "brand_name": 1, "status": 1, "created_at": 1})
    projects = await cursor_projects.to_list(length=1000)

    result = []
    found_project_ids = set()

    for proj in projects:
        pid = proj["id"]
        found_project_ids.add(pid)
        info = totals.get(pid, {"total_size": 0, "asset_count": 0, "talent_ids": set()})

        name = proj.get("brand_name") or proj.get("name")
        if not name or not name.strip():
            name = "Untitled Project"

        result.append({
            "project_id": pid,
            "name": name,
            "status": proj.get("status") or "active",
            "talent_count": len(info["talent_ids"]),
            "asset_count": info["asset_count"],
            "total_storage": info["total_size"],
            "last_activity": proj.get("created_at"),
        })

    # Include deleted projects that still have storage files.
    for pid, info in totals.items():
        if pid and pid not in found_project_ids:
            result.append({
                "project_id": pid,
                "name": "Deleted Project",
                "status": "deleted",
                "talent_count": len(info["talent_ids"]),
                "asset_count": info["asset_count"],
                "total_storage": info["total_size"],
                "last_activity": None,
            })

    result.sort(key=lambda r: r["total_storage"], reverse=True)
    return result


@router.get("/projects/{project_id}/talents")
async def get_project_talent_storage(project_id: str, admin: dict = Depends(require_role("admin"))):
    """Per-talent storage breakdown within one project — lazily loaded when
    an admin expands a project row. Category split (audition/intro/images)
    matches the same real-byte-size source as /analytics."""
    pipeline = [
        {"$match": {"project_id": project_id, "media": {"$exists": True, "$ne": []}}},
        {"$unwind": "$media"},
        {"$project": {
            "talent_id": {"$ifNull": ["$talent_id", "unknown_talent"]},
            "talent_name": 1,
            "category": "$media.category",
            "scope": "$media.scope",
            "admin_added": "$media.admin_added",
            "size": {"$ifNull": ["$media.size", 0]},
        }},
    ]
    by_talent: Dict[str, Dict[str, Any]] = {}
    for coll in (db.submissions, db.applications):
        try:
            agg_items = await coll.aggregate(pipeline).to_list(length=50000)
            for item in agg_items:
                tid = item.get("talent_id") or "unknown_talent"
                if tid not in by_talent:
                    by_talent[tid] = {
                        "talent_id": tid,
                        "talent_name": item.get("talent_name"),
                        "total": 0,
                        "audition_videos": 0,
                        "intro_videos": 0,
                        "images": 0,
                    }
                bucket = classify_media_item(item)
                size = item.get("size") or 0
                by_talent[tid]["total"] += size
                if bucket == "audition_videos":
                    by_talent[tid]["audition_videos"] += size
                elif bucket == "intro_videos":
                    by_talent[tid]["intro_videos"] += size
                else:
                    by_talent[tid]["images"] += size
                if not by_talent[tid]["talent_name"] and item.get("talent_name"):
                    by_talent[tid]["talent_name"] = item.get("talent_name")
        except Exception as e:
            logger.error(f"Mongo: project talent breakdown failed for {coll.name}: {e}")

    talent_ids = [tid for tid in by_talent if tid != "unknown_talent"]
    if talent_ids:
        try:
            names = await db.talents.find({"id": {"$in": talent_ids}}, {"id": 1, "name": 1}).to_list(length=len(talent_ids))
            name_map = {t["id"]: t.get("name") for t in names}
            for tid, row in by_talent.items():
                if not row["talent_name"] and tid in name_map:
                    row["talent_name"] = name_map[tid]
        except Exception as e:
            logger.error(f"Mongo: talent name lookup failed: {e}")

    rows = list(by_talent.values())
    for row in rows:
        row["talent_name"] = row["talent_name"] or "Unnamed Talent"
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


@router.get("/projects/{project_id}/talents/{talent_id}")
async def get_project_talent_media_detail(project_id: str, talent_id: str, admin: dict = Depends(require_role("admin"))):
    """Full media list for one talent within one project — the detail view
    that backs the delete actions. Any item still missing a real byte size
    (a handful of legacy records — see compute_category_breakdown) gets a
    targeted live Cloudinary lookup here, where the set is small and bounded,
    rather than during the bulk project/talent list aggregation."""
    match: Dict[str, Any] = {"project_id": project_id}
    match["talent_id"] = None if talent_id == "unknown_talent" else talent_id

    items: List[Dict[str, Any]] = []
    for coll, scope_label in ((db.submissions, "submission"), (db.applications, "application")):
        try:
            docs = await coll.find(match, {"id": 1, "media": 1}).to_list(length=1000)
        except Exception as e:
            logger.error(f"Mongo: talent media detail failed for {coll.name}: {e}")
            continue
        for doc in docs:
            for m in doc.get("media", []):
                bucket = classify_media_item(m)
                size = m.get("size") or 0
                if not size and m.get("public_id"):
                    try:
                        rtype = m.get("resource_type") or "image"
                        full_pid = resolve_full_public_id(m)
                        res = await run_in_threadpool(cloudinary.api.resource, full_pid, resource_type=rtype)
                        size = res.get("bytes") or 0
                    except Exception as e:
                        logger.warning(f"Live Cloudinary size lookup failed for {m.get('public_id')}: {e}")
                items.append({
                    "media_id": m.get("id"),
                    "parent_id": doc.get("id"),
                    "parent_scope": scope_label,
                    "public_id": m.get("public_id"),
                    "resource_type": m.get("resource_type"),
                    "category": bucket,
                    "raw_category": m.get("category"),
                    "size": size,
                    "url": m.get("url"),
                    "thumbnail_url": m.get("thumbnail_url"),
                    "original_filename": m.get("original_filename"),
                    "created_at": m.get("created_at"),
                })

    return {"project_id": project_id, "talent_id": talent_id, "media": items}

@router.post("/projects/{project_id}/archive")
async def archive_project(project_id: str, admin: dict = Depends(require_role("admin"))):
    """Update project status to archived first in database, then synchronize."""
    await db.projects.update_one({"id": project_id}, {"$set": {"status": "archived"}})
    await db.asset_metadata.update_many({"project_id": project_id}, {"$set": {"project_status": "archived"}})
    await log_storage_action(user_id=admin.get("id"), action_type="ARCHIVE", project_id=project_id)
    return {"status": "success", "message": "Project archived successfully"}

@router.post("/projects/{project_id}/restore")
async def restore_project(project_id: str, admin: dict = Depends(require_role("admin"))):
    """Update project status to active first in database, then synchronize."""
    await db.projects.update_one({"id": project_id}, {"$set": {"status": "active"}})
    await db.asset_metadata.update_many({"project_id": project_id}, {"$set": {"project_status": "active"}})
    await log_storage_action(user_id=admin.get("id"), action_type="RESTORE", project_id=project_id)
    return {"status": "success", "message": "Project restored successfully"}

@router.delete("/projects/{project_id}/auditions")
async def delete_project_audition_videos(project_id: str, admin: dict = Depends(require_role("admin"))):
    """Delete all audition videos for a project (remove objects from R2/Cloudinary and database references)."""
    # P6 (media lifecycle): audition takes are project-owned ephemeral media —
    # route each through the ownership-aware gate. The local reference is pulled;
    # physical destruction happens only when the gate says deletable AND the P6
    # physical-delete flag is on. Otherwise each take is recorded PENDING_DELETION
    # with the configured retention window.
    from media_lifecycle import delete_if_safe, DeletionContext, get_retention_days
    retention_days = await get_retention_days(db)
    submissions = await db.submissions.find({"project_id": project_id}).to_list(length=10000)
    processed = 0
    outcomes: Dict[str, int] = {}
    for sub in submissions:
        audition_media = [m for m in sub.get("media", []) if m.get("category") in ("take", "take_1", "take_2", "take_3")]
        for am in audition_media:
            res = await delete_if_safe(
                db, am,
                ctx=DeletionContext(actor=admin.get("id"),
                                    project_deletion=project_id,
                                    exclude_collection="submissions", exclude_parent_id=sub["id"]),
                collection_name="submissions", parent_id=sub["id"],
                destroyer=lambda m, _sid=sub["id"]: cleanup_media_storage(m, scope="submission", parent_id=_sid),
                retention_days=retention_days,
            )
            outcomes[res.get("outcome")] = outcomes.get(res.get("outcome"), 0) + 1
            processed += 1
        await db.submissions.update_one(
            {"id": sub["id"]},
            {"$pull": {"media": {"category": {"$in": ["take", "take_1", "take_2", "take_3"]}}}}
        )
    await db.asset_metadata.delete_many({
        "project_id": project_id,
        "asset_type": "audition_video"
    })
    await log_storage_action(user_id=admin.get("id"), action_type="DELETE_AUDITIONS", project_id=project_id)
    return {"status": "success", "processed": processed, "outcomes": outcomes,
            "retention_days": retention_days,
            "message": f"{processed} audition take(s) for project {project_id} processed through the media lifecycle gate."}

@router.delete("/projects/{project_id}/voice-notes")
async def delete_project_voice_notes(project_id: str, admin: dict = Depends(require_role("admin"))):
    """Delete all voice-note feedback for a project (remove stored recordings and associated database records)."""
    await assert_providers_healthy()
    # 1. Fetch voice notes feedback
    feedbacks = await db.feedback.find({"project_id": project_id, "type": "voice"}).to_list(length=10000)
    deleted_count = 0
    for fb in feedbacks:
        # Create media wrapper for cleanup
        media_wrapper = {
            "public_id": fb.get("content_url").split("/")[-1].split(".")[0] if fb.get("content_url") else fb.get("id"),
            "url": fb.get("content_url"),
            "resource_type": "video",
            "category": "voice",
            "provider": "cloudinary"
        }
        await cleanup_media_storage(media_wrapper, scope="feedback", parent_id=fb.get("submission_id"))
        deleted_count += 1
    # 2. Delete feedback records from DB
    await db.feedback.delete_many({"project_id": project_id, "type": "voice"})
    # 3. Delete from asset_metadata
    await db.asset_metadata.delete_many({
        "project_id": project_id,
        "asset_type": "voice_note"
    })
    await log_storage_action(user_id=admin.get("id"), action_type="DELETE_VOICE_NOTES", project_id=project_id)
    return {"status": "success", "message": f"Successfully deleted {deleted_count} voice notes for project {project_id}."}

@router.delete("/projects/{project_id}")
async def delete_project_assets(project_id: str, admin: dict = Depends(require_role("admin"))):
    """Delete project-specific ephemeral media only: audition takes + voice notes.

    MEDIA-LIFECYCLE POLICY: this endpoint must NEVER delete introduction videos,
    profile media, portfolio/look media, or project images (admin uploads). It
    therefore no longer removes `admin_upload` / `admin_added` media — that media
    has an arbitrary category (it can be an image = "project image", or a video)
    and is protected until dedicated per-type manual deletion exists. Talent
    master assets were never touched here. Net effect: takes + voice notes only.
    """
    await assert_providers_healthy()
    # Delete ONLY audition takes and voice-note feedback (both are already
    # category-scoped in their own endpoints). Nothing else is removed.
    await delete_project_audition_videos(project_id, admin)
    await delete_project_voice_notes(project_id, admin)

    # Mark the project's ephemeral media as purged (takes + voice removed).
    await db.projects.update_one({"id": project_id}, {"$set": {"status": "purged"}})
    await log_storage_action(user_id=admin.get("id"), action_type="DELETE", project_id=project_id)
    return {"status": "success", "message": f"Project {project_id} audition takes and voice notes deleted."}


def resolve_full_public_id(media_item: Dict[str, Any]) -> Optional[str]:
    """Live-testing discovery (Phase 9): `submission_sign_upload` returns a
    bare `public_id` (just the media UUID, no folder) in its response, and
    `submission_complete_upload` stores that bare value verbatim on
    `media.public_id` — but Cloudinary actually created the asset at
    `{folder}/{public_id}` (folder + public_id are signed as separate
    params, which Cloudinary joins server-side). A destroy() call using the
    bare id silently no-ops: Cloudinary returns {"result": "not found"} for
    a non-matching public_id rather than raising, so the delete looked
    successful while leaving the real asset (and its storage cost) behind.
    Confirmed empirically: 100% of a freshly-uploaded test submission's
    media had this bare form stored, while the accompanying `url` always
    carries the true full path — so the URL is the reliable source here.
    Existing correctly-full public_ids (already containing '/') pass
    through untouched."""
    public_id = media_item.get("public_id")
    if not public_id:
        return None
    if "/" in public_id:
        return public_id
    url = media_item.get("url") or ""
    m = re.search(r"/upload/(?:[^/]+/)*?v\d+/(.+?)\.[a-zA-Z0-9]+(?:\?.*)?$", url)
    if m:
        return m.group(1)
    return public_id


async def count_other_references(public_id: str, exclude_scope: str, exclude_parent_id: str) -> int:
    """Shared/global-asset protection (Phase 4): count how many OTHER
    submissions/applications/talent records reference this exact Cloudinary
    public_id, excluding the one record the caller is about to delete from.
    Media synced to a talent's global profile shares the same public_id with
    its originating submission (confirmed empirically — 348 of 364 talent
    media public_ids are also referenced by a submission in production data),
    and 123 public_ids are referenced by more than one submission via the
    Media Library reuse feature. A reference count > 0 here means the
    physical Cloudinary asset must NOT be destroyed — only the one local
    reference being deleted should be removed."""
    count = 0
    for coll, scope in ((db.submissions, "submission"), (db.applications, "application")):
        query: Dict[str, Any] = {"media.public_id": public_id}
        if scope == exclude_scope:
            query["id"] = {"$ne": exclude_parent_id}
        try:
            count += await coll.count_documents(query)
        except Exception as e:
            logger.error(f"Mongo: reference count failed for {coll.name}: {e}")
    try:
        count += await db.talents.count_documents({"media.public_id": public_id})
    except Exception as e:
        logger.error(f"Mongo: talent reference count failed: {e}")
    return count


async def delete_one_media_item(parent_coll, parent_id: str, media_item: Dict[str, Any], admin_id: str) -> Dict[str, Any]:
    """Remove one media item from a submission (Cloudinary rearchitecture, P6).

    P6 change: the decision to physically destroy the backing Cloudinary asset
    is delegated to the ownership-aware ``media_lifecycle`` gate instead of the
    local ``count_other_references`` heuristic. The local reference is always
    pulled; the asset is only ever physically destroyed when the gate says
    deletable AND the P6 physical-delete flag is on (off during rollout) —
    otherwise audition media is recorded as PENDING_DELETION and everything
    else is left untouched. Never raises.
    """
    from media_lifecycle import delete_if_safe, DeletionContext

    public_id = media_item.get("public_id")
    mid = media_item.get("id")
    coll_name = getattr(parent_coll, "name", "submissions")

    outcome = None
    try:
        res = await delete_if_safe(
            db, media_item,
            ctx=DeletionContext(actor=admin_id,
                                exclude_collection=coll_name, exclude_parent_id=parent_id),
            collection_name=coll_name, parent_id=parent_id,
            destroyer=lambda m: cleanup_media_storage(
                {**m, "public_id": resolve_full_public_id(m) or m.get("public_id")},
                scope="submission", parent_id=parent_id),
        )
        outcome = res.get("outcome")
    except Exception as e:
        logger.warning(f"[delete_one_media_item] lifecycle gate failed pid={public_id}: {e}")

    await parent_coll.update_one({"id": parent_id}, {"$pull": {"media": {"id": mid}}})

    await log_storage_action(
        user_id=admin_id,
        action_type="DELETE_MEDIA_ITEM",
        details=f"public_id={public_id} lifecycle_outcome={outcome}",
    )
    return {"media_id": mid, "public_id": public_id, "lifecycle_outcome": outcome,
            "physically_deleted": outcome == "deleted"}


@router.delete("/projects/{project_id}/talents/{talent_id}/auditions")
async def delete_talent_auditions(project_id: str, talent_id: str, admin: dict = Depends(require_role("admin"))):
    """Delete all audition takes for ONE talent within ONE project (never
    the whole project). Audition takes are never shared/global, so this is
    always safe to physically destroy on Cloudinary."""
    await assert_providers_healthy()
    match: Dict[str, Any] = {"project_id": project_id, "talent_id": None if talent_id == "unknown_talent" else talent_id}
    subs = await db.submissions.find(match).to_list(length=1000)
    results = []
    for sub in subs:
        take_media = [m for m in sub.get("media", []) if m.get("category") == "take" or m.get("category") in LEGACY_TAKE_CATEGORIES]
        for m in take_media:
            results.append(await delete_one_media_item(db.submissions, sub["id"], m, admin.get("id")))
    return {"status": "success", "deleted": results}


@router.delete("/projects/{project_id}/talents/{talent_id}/intro-video")
async def delete_talent_intro_video(project_id: str, talent_id: str, admin: dict = Depends(require_role("admin"))):
    """Delete the introduction video for ONE talent within ONE project.
    intro_video IS a reusable/global-syncable category, so this goes through
    shared-asset protection — it will only pull the local reference (not
    destroy the Cloudinary asset) if the talent's global profile still uses
    the same public_id."""
    await assert_providers_healthy()
    match: Dict[str, Any] = {"project_id": project_id, "talent_id": None if talent_id == "unknown_talent" else talent_id}
    subs = await db.submissions.find(match).to_list(length=1000)
    results = []
    for sub in subs:
        intro_media = [m for m in sub.get("media", []) if m.get("category") == "intro_video"]
        for m in intro_media:
            results.append(await delete_one_media_item(db.submissions, sub["id"], m, admin.get("id")))
    return {"status": "success", "deleted": results}


class ImageDeleteRequest(BaseModel):
    media_ids: List[str]


@router.post("/projects/{project_id}/talents/{talent_id}/images/delete")
async def delete_talent_images(project_id: str, talent_id: str, payload: ImageDeleteRequest, admin: dict = Depends(require_role("admin"))):
    """Delete specific, admin-selected images for ONE talent within ONE
    project, by exact media id — never by filename/category alone. Each
    goes through shared-asset protection."""
    await assert_providers_healthy()
    if not payload.media_ids:
        return {"status": "success", "deleted": []}
    match: Dict[str, Any] = {"project_id": project_id, "talent_id": None if talent_id == "unknown_talent" else talent_id}
    subs = await db.submissions.find(match).to_list(length=1000)
    wanted = set(payload.media_ids)
    results = []
    for sub in subs:
        target_media = [m for m in sub.get("media", []) if m.get("id") in wanted]
        for m in target_media:
            results.append(await delete_one_media_item(db.submissions, sub["id"], m, admin.get("id")))
    return {"status": "success", "deleted": results}


@router.get("/health")
async def get_storage_health(admin: dict = Depends(require_role("admin"))):
    """Scan and identify orphaned assets, broken references, duplicate media, and unused files."""
    
    # 1. Fetch physical items from providers
    r2_physical = await run_in_threadpool(list_r2_physical_objects_sync)
    cld_physical = await run_in_threadpool(list_cloudinary_physical_resources_sync)
    
    # Build maps/sets of physical assets
    r2_phys_keys = {item["key"] for item in r2_physical}
    cld_phys_ids = {item["public_id"] for item in cld_physical}
    
    # 2. Fetch DB entities
    metadata_list = await db.asset_metadata.find({}).to_list(length=100000)
    submissions = await db.submissions.find({}).to_list(length=10000)
    talents = await db.talents.find({}).to_list(length=10000)
    feedbacks = await db.feedback.find({}).to_list(length=10000)
    
    # Gather database referenced keys/public_ids
    db_referenced_ids = set()
    db_metadata_ids = set()
    
    for doc in metadata_list:
        pid = doc.get("public_id")
        if pid:
            db_metadata_ids.add(pid)
            db_referenced_ids.add(pid)
            
    for sub in submissions:
        for m in sub.get("media", []):
            pid = m.get("public_id")
            if pid:
                db_referenced_ids.add(pid)
                
    for tal in talents:
        for m in tal.get("media", []):
            pid = m.get("public_id")
            if pid:
                db_referenced_ids.add(pid)
                
    for fb in feedbacks:
        if fb.get("content_url"):
            # try to extract public_id
            leaf = fb.get("content_url").split("/")[-1].split(".")[0]
            db_referenced_ids.add(leaf)

    # 3. Compute Health Issues
    orphaned_assets = []
    broken_references = []
    duplicate_media = {}
    unused_files = []
    
    # A. Orphaned Assets: physically present but no DB reference
    for item in r2_physical:
        key = item["key"]
        # check if key or base name is referenced
        leaf = key.split("/")[-1].split(".")[0]
        if key not in db_referenced_ids and leaf not in db_referenced_ids:
            orphaned_assets.append({
                "provider": "Cloudflare R2",
                "key": key,
                "size": item["size"],
                "type": "video"
            })
            
    for item in cld_physical:
        pid = item["public_id"]
        if pid not in db_referenced_ids:
            orphaned_assets.append({
                "provider": "Cloudinary",
                "key": pid,
                "size": item["size"],
                "type": item["resource_type"]
            })
            
    # B. Broken References: DB references whose physical files are missing
    for doc in metadata_list:
        pid = doc.get("public_id")
        is_r2_key = pid.startswith("raw-uploads/")
        
        if is_r2_key:
            if pid not in r2_phys_keys:
                broken_references.append({
                    "id": doc.get("id"),
                    "public_id": pid,
                    "provider": "Cloudflare R2",
                    "asset_type": doc.get("asset_type"),
                    "size": doc.get("file_size") or 0
                })
        else:
            if pid not in cld_phys_ids:
                broken_references.append({
                    "id": doc.get("id"),
                    "public_id": pid,
                    "provider": "Cloudinary",
                    "asset_type": doc.get("asset_type"),
                    "size": doc.get("file_size") or 0
                })
                
    # C. Duplicate Media: same public_id/url referenced multiple times
    public_id_counts = {}
    for sub in submissions:
        for m in sub.get("media", []):
            pid = m.get("public_id")
            if pid:
                public_id_counts[pid] = public_id_counts.get(pid, 0) + 1
    for tal in talents:
        for m in tal.get("media", []):
            pid = m.get("public_id")
            if pid:
                public_id_counts[pid] = public_id_counts.get(pid, 0) + 1
                
    for pid, count in public_id_counts.items():
        if count > 1:
            duplicate_media[pid] = count
            
    # D. Unused Files: in metadata table but marked failed or from deleted projects
    for doc in metadata_list:
        if doc.get("status") == "failed" or doc.get("upload_status") == "failed":
            unused_files.append({
                "id": doc.get("id"),
                "public_id": doc.get("public_id"),
                "reason": "Failed Upload"
            })
        elif doc.get("project_status") == "purged":
            unused_files.append({
                "id": doc.get("id"),
                "public_id": doc.get("public_id"),
                "reason": "Purged Project Asset"
            })

    return {
        "status": "healthy" if not (orphaned_assets or broken_references or duplicate_media or unused_files) else "action_required",
        "orphaned_count": len(orphaned_assets),
        "broken_count": len(broken_references),
        "duplicate_count": len(duplicate_media),
        "unused_count": len(unused_files),
        "orphaned_assets": orphaned_assets[:100],
        "broken_references": broken_references[:100],
        "duplicate_media": [{"public_id": k, "references": v} for k, v in list(duplicate_media.items())[:100]],
        "unused_files": unused_files[:100]
    }

# ---------------------------------------------------------------------------
# DISABLED 2026-08-30 (Cloudinary rearchitecture, P0.5 — production safety).
#
# This endpoint was a one-click, no-confirmation, no-dry-run, un-batched
# mass-delete: it ran `cloudinary.uploader.destroy()` on every Cloudinary
# resource whose public_id was absent from a reference set built with an
# incomplete heuristic (category-name leaves matched as ids; whichever
# MongoDB the backend was pointed at treated as authoritative), plus
# collection-wide `db.submissions/talents.update_many({}, {"$pull": ...})`
# against historical media arrays. Forensic audit (docs/CLOUDINARY_PHASE0_
# VERIFICATION.md) measured ~3,400 originals + ~7,500 derived that a scan
# would flag as "orphaned" — a single click would have destroyed them.
#
# It is intentionally kept registered (not deleted) so a stale cached
# frontend gets an explicit 410 instead of a confusing 404. The safe
# replacement is a dry-run manifest → per-category human approval →
# batched, reference-checked deletion with an audit log (see the doc,
# section E / P8). Do NOT re-enable this without that design.
# ---------------------------------------------------------------------------
@router.post("/health/cleanup", deprecated=True)
async def run_storage_cleanup(admin: dict = Depends(require_role("admin"))):
    """DISABLED. Destructive one-click cleanup permanently removed for safety.

    Always returns HTTP 410 Gone and performs no work — reads nothing,
    deletes nothing, mutates nothing.
    """
    logger.warning(
        "Blocked call to deprecated destructive endpoint POST /health/cleanup "
        f"by admin={admin.get('email') or admin.get('id')}"
    )
    raise HTTPException(
        status_code=410,
        detail=(
            "The one-click storage cleanup has been permanently disabled for "
            "production safety. A safe replacement (dry-run manifest, ownership "
            "and reference checks, explicit per-batch confirmation, audit log) "
            "is being built. No assets or database references were touched."
        ),
    )

@router.delete("/talents/{talent_id}")
async def delete_talent_assets(talent_id: str, talent_name: str, admin: dict = Depends(require_role("admin"))):
    """Talent-asset removal (Cloudinary rearchitecture, P6 — media lifecycle).

    P6 change: the old implementation recomputed a folder slug
    (``{talent_id}_{slug}``) — which drifts from the upload-time slug — and ran
    ``delete_resources_by_prefix`` on it, a blind folder-scoped mass-delete.
    Folder location is NOT ownership. Deletion now flows per-item through the
    ownership-aware ``media_lifecycle`` gate, keyed on the stored canonical
    ``public_id`` (never a recomputed slug). Nothing is physically destroyed
    while the P6 physical-delete flag is off.
    """
    from media_lifecycle import talent_hard_delete_blockers, enqueue_pending_deletion, classify_owner, get_retention_days
    from datetime import datetime, timezone

    talent = await db.talents.find_one({"id": talent_id}, {"_id": 0})
    if not talent:
        raise HTTPException(404, "Talent not found")

    blockers = await talent_hard_delete_blockers(db, talent_id)
    if blockers:
        raise HTTPException(409, {
            "message": "Talent assets cannot be purged while dependencies exist.",
            "blockers": blockers,
        })

    now = datetime.now(timezone.utc)
    retention_days = await get_retention_days(db)
    enqueued = 0
    for m in (talent.get("media") or []):
        if m.get("public_id") or m.get("stream_uid"):
            await enqueue_pending_deletion(
                db, m, owner=classify_owner(m),
                reason=f"delete_talent_assets: talent {talent_id}, no blocking dependencies",
                retention_days=retention_days, actor=admin.get("id"), now=now,
            )
            enqueued += 1

    await db.asset_metadata.delete_many({"talent_id": talent_id})
    await log_storage_action(user_id=admin.get("id"), action_type="DELETE", talent_id=talent_id)
    return {"status": "success", "talent_id": talent_id,
            "media_pending_deletion": enqueued, "cloudinary_assets_deleted": 0,
            "message": f"Talent {talent_id}: {enqueued} media items recorded for retention-gated purge; 0 assets destroyed."}
