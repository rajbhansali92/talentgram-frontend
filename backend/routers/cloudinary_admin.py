import logging
import re
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
import cloudinary.api
from core import current_team_or_admin, require_role, db, log_storage_action, cloudinary_destroy

router = APIRouter(prefix="/api/admin/cloudinary", tags=["cloudinary-admin"])
logger = logging.getLogger(__name__)


@router.get("/analytics")
async def get_storage_analytics(admin: dict = Depends(require_role("admin"))):
    """Compute aggregates over tracked assets metadata."""
    # Compute total, permanent vs temporary ratio, type distribution
    pipeline = [
        {
            "$group": {
                "_id": "$asset_type",
                "total_size": {"$sum": "$file_size"},
                "count": {"$sum": 1}
            }
        }
    ]
    cursor = db.asset_metadata.aggregate(pipeline)
    by_type = await cursor.to_list(length=100)

    total_storage = 0
    permanent_storage = 0
    temporary_storage = 0
    by_type_dict = {}

    for item in by_type:
        t = item["_id"]
        sz = item["total_size"]
        by_type_dict[t] = {
            "size": sz,
            "count": item["count"]
        }
        total_storage += sz
        if t in {"profile_image", "intro_video", "portfolio_video"}:
            permanent_storage += sz
        else:
            temporary_storage += sz

    # Archived project storage
    pipeline_archived = [
        {"$match": {"project_status": "archived"}},
        {"$group": {"_id": None, "total_size": {"$sum": "$file_size"}}}
    ]
    cursor_archived = db.asset_metadata.aggregate(pipeline_archived)
    archived_list = await cursor_archived.to_list(length=1)
    archived_storage = archived_list[0]["total_size"] if archived_list else 0

    # Top storage consuming projects
    pipeline_projects = [
        {"$group": {"_id": "$project_id", "total_size": {"$sum": "$file_size"}}},
        {"$sort": {"total_size": -1}},
        {"$limit": 5}
    ]
    cursor_projects = db.asset_metadata.aggregate(pipeline_projects)
    top_projects = await cursor_projects.to_list(length=5)
    
    # Enrich top projects with names
    enriched_top_projects = []
    for tp in top_projects:
        pid = tp["_id"]
        if pid:
            proj = await db.projects.find_one({"id": pid}, {"name": 1})
            name = proj.get("name") if proj else pid
        else:
            name = "Permanent / System"
        enriched_top_projects.append({
            "project_id": pid,
            "name": name,
            "size": tp["total_size"]
        })

    # Top storage consuming talents
    pipeline_talents = [
        {"$group": {"_id": "$talent_id", "total_size": {"$sum": "$file_size"}}},
        {"$sort": {"total_size": -1}},
        {"$limit": 5}
    ]
    cursor_talents = db.asset_metadata.aggregate(pipeline_talents)
    top_talents = await cursor_talents.to_list(length=5)
    
    # Enrich top talents with names
    enriched_top_talents = []
    for tt in top_talents:
        tid = tt["_id"]
        talent = await db.talents.find_one({"id": tid}, {"name": 1})
        name = talent.get("name") if talent else tid
        enriched_top_talents.append({
            "talent_id": tid,
            "name": name,
            "size": tt["total_size"]
        })

    # Average storage per audition
    pipeline_auditions = [
        {"$match": {"asset_type": "audition_video"}},
        {"$group": {"_id": None, "avg_size": {"$avg": "$file_size"}, "count": {"$sum": 1}}}
    ]
    cursor_auditions = db.asset_metadata.aggregate(pipeline_auditions)
    auditions_stats = await cursor_auditions.to_list(length=1)
    avg_audition_size = auditions_stats[0]["avg_size"] if auditions_stats else 0
    total_auditions = auditions_stats[0]["count"] if auditions_stats else 0

    return {
        "total_storage": total_storage,
        "permanent_storage": permanent_storage,
        "temporary_storage": temporary_storage,
        "archived_storage": archived_storage,
        "storage_by_asset_type": by_type_dict,
        "top_projects": enriched_top_projects,
        "top_talents": enriched_top_talents,
        "average_audition_size": avg_audition_size,
        "total_auditions": total_auditions
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
        
        # Calculate total auditions by counting unique submission IDs
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
    # 1. Update database status
    await db.projects.update_one({"id": project_id}, {"$set": {"status": "archived"}})
    await db.asset_metadata.update_many({"project_id": project_id}, {"$set": {"project_status": "archived"}})
    
    # 2. Update audit log
    await log_storage_action(
        user_id=admin.get("id"),
        action_type="ARCHIVE",
        project_id=project_id
    )
    return {"status": "success", "message": "Project archived successfully"}


@router.post("/projects/{project_id}/restore")
async def restore_project(project_id: str, admin: dict = Depends(require_role("admin"))):
    """Update project status to active first in database, then synchronize."""
    # 1. Update database status
    await db.projects.update_one({"id": project_id}, {"$set": {"status": "active"}})
    await db.asset_metadata.update_many({"project_id": project_id}, {"$set": {"project_status": "active"}})
    
    # 2. Update audit log
    await log_storage_action(
        user_id=admin.get("id"),
        action_type="RESTORE",
        project_id=project_id
    )
    return {"status": "success", "message": "Project restored successfully"}


@router.delete("/projects/{project_id}")
async def delete_project_assets(project_id: str, admin: dict = Depends(require_role("admin"))):
    """Removes the entire project folder and all sub-audition assets."""
    # Database First: Delete records
    assets = await db.asset_metadata.find({"project_id": project_id}).to_list(length=10000)
    
    # Update project status to purged
    await db.projects.update_one({"id": project_id}, {"$set": {"status": "purged"}})
    await db.asset_metadata.delete_many({"project_id": project_id})
    
    # 2. Synchronize to Cloudinary (Bulk Delete)
    # Cloudinary folder operations
    try:
        folder_prefix = f"talentgram/projects/{project_id}/"
        cloudinary.api.delete_resources_by_prefix(folder_prefix)
        # Attempt to delete the subfolders as well
        cloudinary.api.delete_folder(f"talentgram/projects/{project_id}")
    except Exception as e:
        logger.warning(f"Failed to fully delete Cloudinary project folder {project_id}: {e}")

    # 3. Write audit log
    await log_storage_action(
        user_id=admin.get("id"),
        action_type="DELETE",
        project_id=project_id
    )
    return {"status": "success", "message": f"Project {project_id} deleted successfully"}


@router.delete("/talents/{talent_id}")
async def delete_talent_assets(talent_id: str, talent_name: str, admin: dict = Depends(require_role("admin"))):
    """Removes the entire permanent talent folder."""
    # Database First: Delete records
    await db.asset_metadata.delete_many({"talent_id": talent_id})
    
    # 2. Synchronize to Cloudinary (Bulk Delete)
    try:
        talent_slug = re.sub(r'[^a-zA-Z0-9_]', '', talent_name.lower().replace(' ', '_'))
        folder_prefix = f"talentgram/talents/{talent_id}_{talent_slug}/"
        cloudinary.api.delete_resources_by_prefix(folder_prefix)
        # Attempt to delete the folder
        cloudinary.api.delete_folder(f"talentgram/talents/{talent_id}_{talent_slug}")
    except Exception as e:
        logger.warning(f"Failed to fully delete Cloudinary talent folder {talent_id}: {e}")

    # 3. Write audit log
    await log_storage_action(
        user_id=admin.get("id"),
        action_type="DELETE",
        talent_id=talent_id
    )
    return {"status": "success", "message": f"Talent {talent_id} assets deleted successfully"}
