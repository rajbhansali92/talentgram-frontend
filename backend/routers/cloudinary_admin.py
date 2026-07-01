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
import cloudinary.api
import cloudinary.uploader
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
    check_r2_health
)

router = APIRouter(prefix="/api/admin/cloudinary", tags=["cloudinary-admin"])

async def assert_providers_healthy():
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
            logger.error("assert_providers_healthy: Cloudflare R2 is unreachable or misconfigured")
            raise HTTPException(
                status_code=503,
                detail="Storage cleanup aborted. Cloudflare R2 is currently unreachable or misconfigured. No changes have been made."
            )
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
    
    # Category Breakdowns from asset_metadata + fallback feedback count
    pipeline = [
        {
            "$group": {
                "_id": {
                    "asset_type": "$asset_type",
                    "category": "$category"
                },
                "total_size": {"$sum": "$file_size"},
                "count": {"$sum": 1}
            }
        }
    ]
    
    by_category_raw = []
    try:
        cursor = db.asset_metadata.aggregate(pipeline)
        by_category_raw = await cursor.to_list(length=500)
    except Exception as e:
        logger.error(f"Mongo: asset_metadata aggregate breakdown failed: {e}")
        
    # Organize categories
    categories = {
        "audition_videos": {"size": 0, "count": 0, "label": "Audition Videos"},
        "intro_videos": {"size": 0, "count": 0, "label": "Introduction Videos"},
        "portfolio_images": {"size": 0, "count": 0, "label": "Portfolio (General) Images"},
        "indian_look_images": {"size": 0, "count": 0, "label": "Indian Look Images"},
        "western_look_images": {"size": 0, "count": 0, "label": "Western Look Images"},
        "voice_notes": {"size": 0, "count": 0, "label": "Voice Notes"},
        "admin_uploads": {"size": 0, "count": 0, "label": "Admin Uploads"},
    }
    
    if isinstance(by_category_raw, list):
        for item in by_category_raw:
            if not isinstance(item, dict):
                continue
            grp = item.get("_id") or {}
            if not isinstance(grp, dict):
                grp = {}
            atype = grp.get("asset_type")
            cat = grp.get("category")
            size = item.get("total_size") or 0
            count = item.get("count") or 0
            
            if atype == "audition_video" or cat in ("take", "take_1", "take_2", "take_3"):
                categories["audition_videos"]["size"] += size
                categories["audition_videos"]["count"] += count
            elif atype == "intro_video" or cat == "intro_video":
                categories["intro_videos"]["size"] += size
                categories["intro_videos"]["count"] += count
            elif cat == "indian":
                categories["indian_look_images"]["size"] += size
                categories["indian_look_images"]["count"] += count
            elif cat == "western":
                categories["western_look_images"]["size"] += size
                categories["western_look_images"]["count"] += count
            elif cat in ("image", "portfolio") or (atype == "profile_image" and cat not in ("indian", "western")):
                categories["portfolio_images"]["size"] += size
                categories["portfolio_images"]["count"] += count
            elif atype == "voice_note" or cat == "voice":
                categories["voice_notes"]["size"] += size
                categories["voice_notes"]["count"] += count
            elif atype == "admin_upload" or cat == "admin":
                categories["admin_uploads"]["size"] += size
                categories["admin_uploads"]["count"] += count
            else:
                # default fallback to general portfolio
                categories["portfolio_images"]["size"] += size
                categories["portfolio_images"]["count"] += count

    # Add legacy voice notes from feedback collection if not already captured
    voice_feedback_count = 0
    try:
        voice_feedback_count = await db.feedback.count_documents({"type": "voice"})
    except Exception as e:
        logger.error(f"Mongo: feedback count_documents failed: {e}")
        
    if categories["voice_notes"]["count"] < voice_feedback_count:
        diff_count = voice_feedback_count - categories["voice_notes"]["count"]
        # estimate 500KB per legacy audio
        categories["voice_notes"]["size"] += diff_count * 500 * 1024
        categories["voice_notes"]["count"] += diff_count

    # Archived project storage
    pipeline_archived = [
        {"$match": {"project_status": "archived"}},
        {"$group": {"_id": None, "total_size": {"$sum": "$file_size"}}}
    ]
    archived_storage = 0
    try:
        cursor_archived = db.asset_metadata.aggregate(pipeline_archived)
        archived_list = await cursor_archived.to_list(length=1)
        if isinstance(archived_list, list) and archived_list:
            archived_storage = archived_list[0].get("total_size") or 0
    except Exception as e:
        logger.error(f"Mongo: archived storage aggregate failed: {e}")

    # Top storage consuming projects
    pipeline_projects = [
        {"$group": {"_id": "$project_id", "total_size": {"$sum": "$file_size"}}},
        {"$sort": {"total_size": -1}},
        {"$limit": 5}
    ]
    top_projects = []
    try:
        cursor_projects = db.asset_metadata.aggregate(pipeline_projects)
        top_projects = await cursor_projects.to_list(length=5)
    except Exception as e:
        logger.error(f"Mongo: top projects aggregate failed: {e}")
        
    enriched_top_projects = []
    if isinstance(top_projects, list):
        for tp in top_projects:
            if not isinstance(tp, dict):
                continue
            pid = tp.get("_id")
            size = tp.get("total_size") or 0
            name = pid or "Permanent / System"
            if pid:
                try:
                    proj = await db.projects.find_one({"id": pid}, {"name": 1, "brand_name": 1})
                    if isinstance(proj, dict):
                        name = proj.get("brand_name") or proj.get("name") or pid
                except Exception:
                    pass
            else:
                name = "Permanent / System"
            enriched_top_projects.append({
                "project_id": pid,
                "name": name,
                "size": size
            })

    # Top storage consuming talents
    pipeline_talents = [
        {"$group": {"_id": "$talent_id", "total_size": {"$sum": "$file_size"}}},
        {"$sort": {"total_size": -1}},
        {"$limit": 5}
    ]
    top_talents = []
    try:
        cursor_talents = db.asset_metadata.aggregate(pipeline_talents)
        top_talents = await cursor_talents.to_list(length=5)
    except Exception as e:
        logger.error(f"Mongo: top talents aggregate failed: {e}")
        
    enriched_top_talents = []
    if isinstance(top_talents, list):
        for tt in top_talents:
            if not isinstance(tt, dict):
                continue
            tid = tt.get("_id")
            size = tt.get("total_size") or 0
            name = tid or "Unnamed / System"
            if tid:
                try:
                    talent = await db.talents.find_one({"id": tid}, {"name": 1})
                    if isinstance(talent, dict):
                        name = talent.get("name") or tid
                except Exception:
                    pass
            enriched_top_talents.append({
                "talent_id": tid,
                "name": name,
                "size": size
            })

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

@router.get("/projects")
async def get_projects_storage(admin: dict = Depends(require_role("admin"))):
    """Retrieve detailed storage breakdowns for all projects."""
    pipeline = [
        {
            "$group": {
                "_id": "$project_id",
                "total_size": {"$sum": "$file_size"},
                "asset_count": {"$sum": 1}
            }
        }
    ]
    cursor = db.asset_metadata.aggregate(pipeline)
    project_sizes = await cursor.to_list(length=1000)
    sizes_dict = {item["_id"]: item for item in project_sizes if item["_id"]}

    cursor_projects = db.projects.find({}, {"id": 1, "name": 1, "status": 1, "created_at": 1})
    projects = await cursor_projects.to_list(length=1000)

    result = []
    for proj in projects:
        pid = proj["id"]
        size_info = sizes_dict.get(pid, {"total_size": 0, "asset_count": 0})
        
        pipeline_subs = [
            {"$match": {"project_id": pid}},
            {"$group": {"_id": "$submission_id"}}
        ]
        cursor_subs = db.asset_metadata.aggregate(pipeline_subs)
        subs = await cursor_subs.to_list(length=1000)
        
        result.append({
            "project_id": pid,
            "name": proj.get("name") or "Unnamed Campaign",
            "status": proj.get("status") or "active",
            "total_auditions": len(subs),
            "total_storage": size_info["total_size"],
            "last_activity": proj.get("created_at")
        })

    return result

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
    await assert_providers_healthy()
    # 1. Fetch all submissions for the project
    submissions = await db.submissions.find({"project_id": project_id}).to_list(length=10000)
    deleted_count = 0
    for sub in submissions:
        # Find takes / audition videos
        audition_media = [m for m in sub.get("media", []) if m.get("category") in ("take", "take_1", "take_2", "take_3")]
        for am in audition_media:
            await cleanup_media_storage(am, scope="submission", parent_id=sub["id"])
            deleted_count += 1
        # Pull from submission media array
        await db.submissions.update_one(
            {"id": sub["id"]},
            {"$pull": {"media": {"category": {"$in": ["take", "take_1", "take_2", "take_3"]}}}}
        )
    # Remove from asset_metadata
    await db.asset_metadata.delete_many({
        "project_id": project_id,
        "asset_type": "audition_video"
    })
    await log_storage_action(user_id=admin.get("id"), action_type="DELETE_AUDITIONS", project_id=project_id)
    return {"status": "success", "message": f"Successfully deleted {deleted_count} audition videos for project {project_id}."}

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
    """Removes the entire project folder and all sub-audition assets and voice notes, updating project status to purged."""
    await assert_providers_healthy()
    # 1. Delete audition videos and voice notes
    await delete_project_audition_videos(project_id, admin)
    await delete_project_voice_notes(project_id, admin)
    
    # 2. Delete admin uploads for this project
    admin_assets = await db.asset_metadata.find({"project_id": project_id, "asset_type": "admin_upload"}).to_list(length=1000)
    for aa in admin_assets:
        await cleanup_media_storage(aa, scope="submission", parent_id=aa.get("submission_id"))
        
    await db.asset_metadata.delete_many({"project_id": project_id, "asset_type": "admin_upload"})
    
    # 3. Pull admin added media from submission docs
    await db.submissions.update_many(
        {"project_id": project_id},
        {"$pull": {"media": {"scope": "admin_added"}}}
    )
    
    # 4. Set project to purged
    await db.projects.update_one({"id": project_id}, {"$set": {"status": "purged"}})
    await log_storage_action(user_id=admin.get("id"), action_type="DELETE", project_id=project_id)
    return {"status": "success", "message": f"Project {project_id} assets purged successfully."}

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

@router.post("/health/cleanup")
async def run_storage_cleanup(admin: dict = Depends(require_role("admin"))):
    """One-click repair and cleanup action for storage health violations."""
    await assert_providers_healthy()
    
    # 1. Fetch physical items
    r2_physical = await run_in_threadpool(list_r2_physical_objects_sync)
    cld_physical = await run_in_threadpool(list_cloudinary_physical_resources_sync)
    
    r2_phys_keys = {item["key"] for item in r2_physical}
    cld_phys_ids = {item["public_id"] for item in cld_physical}
    
    # 2. Fetch DB entities
    metadata_list = await db.asset_metadata.find({}).to_list(length=100000)
    submissions = await db.submissions.find({}).to_list(length=10000)
    talents = await db.talents.find({}).to_list(length=10000)
    feedbacks = await db.feedback.find({}).to_list(length=10000)
    
    db_referenced_ids = set()
    for doc in metadata_list:
        pid = doc.get("public_id")
        if pid:
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
            leaf = fb.get("content_url").split("/")[-1].split(".")[0]
            db_referenced_ids.add(leaf)

    cleaned_orphaned = 0
    cleaned_broken = 0
    cleaned_unused = 0

    # A. Delete Orphaned physical files
    for item in r2_physical:
        key = item["key"]
        leaf = key.split("/")[-1].split(".")[0]
        if key not in db_referenced_ids and leaf not in db_referenced_ids:
            try:
                get_r2_client().delete_object(Bucket=R2_BUCKET_NAME, Key=key)
                cleaned_orphaned += 1
            except Exception as e:
                logger.warning(f"Health Cleanup: failed to delete R2 orphaned key {key}: {e}")
                
    for item in cld_physical:
        pid = item["public_id"]
        if pid not in db_referenced_ids:
            try:
                cloudinary.uploader.destroy(pid, resource_type=item["resource_type"], invalidate=True)
                cleaned_orphaned += 1
            except Exception as e:
                logger.warning(f"Health Cleanup: failed to destroy Cloudinary resource {pid}: {e}")

    # B. Remove Broken References from database
    for doc in metadata_list:
        pid = doc.get("public_id")
        is_r2_key = pid.startswith("raw-uploads/")
        is_broken = False
        
        if is_r2_key:
            if pid not in r2_phys_keys:
                is_broken = True
        else:
            if pid not in cld_phys_ids:
                is_broken = True
                
        if is_broken:
            await db.asset_metadata.delete_one({"id": doc.get("id")})
            await db.submissions.update_many({}, {"$pull": {"media": {"public_id": pid}}})
            await db.talents.update_many({}, {"$pull": {"media": {"public_id": pid}}})
            cleaned_broken += 1

    # C. Delete Unused / Failed files
    for doc in metadata_list:
        if doc.get("status") == "failed" or doc.get("upload_status") == "failed" or doc.get("project_status") == "purged":
            pid = doc.get("public_id")
            wrapper = {
                "public_id": pid,
                "resource_type": doc.get("resource_type") or "video",
                "category": doc.get("category"),
                "provider": "r2" if pid.startswith("raw-uploads/") else "cloudinary"
            }
            op_id = str(uuid.uuid4())
            await cleanup_media_storage(wrapper, scope="submission", parent_id=doc.get("submission_id"), operation_id=op_id)
            cleaned_unused += 1

    await log_storage_action(
        user_id=admin.get("id"),
        action_type="HEALTH_CLEANUP",
        details=f"Cleaned {cleaned_orphaned} orphaned files, {cleaned_broken} broken references, {cleaned_unused} unused files.",
        operation_id=str(uuid.uuid4())
    )

    return {
        "status": "success",
        "cleaned_orphaned": cleaned_orphaned,
        "cleaned_broken": cleaned_broken,
        "cleaned_unused": cleaned_unused
    }

@router.delete("/talents/{talent_id}")
async def delete_talent_assets(talent_id: str, talent_name: str, admin: dict = Depends(require_role("admin"))):
    """Removes the entire permanent talent folder and all associated assets."""
    await db.asset_metadata.delete_many({"talent_id": talent_id})
    try:
        talent_slug = re.sub(r'[^a-zA-Z0-9_]', '', talent_name.lower().replace(' ', '_'))
        folder_prefix = f"talentgram/talents/{talent_id}_{talent_slug}/"
        cloudinary.api.delete_resources_by_prefix(folder_prefix)
        cloudinary.api.delete_folder(f"talentgram/talents/{talent_id}_{talent_slug}")
    except Exception as e:
        logger.warning(f"Failed to fully delete Cloudinary talent folder {talent_id}: {e}")

    await log_storage_action(user_id=admin.get("id"), action_type="DELETE", talent_id=talent_id)
    return {"status": "success", "message": f"Talent {talent_id} assets deleted successfully"}
