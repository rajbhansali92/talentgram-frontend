import uuid
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from core import db, current_user, _now, require_role
from .workflow_schemas import (
    TaskIn,
    TaskUpdateIn,
    CommentIn,
    ScoutEntryIn,
    ScoutEntryUpdateIn,
)
import scout_capture

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workflow", tags=["workflow"])

# --------------------------------------------------------------------------
# Recursive MongoDB JSON Safe Serializer Helper
# --------------------------------------------------------------------------
def _to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "dict"):
        return _to_dict(obj.dict())
    if hasattr(obj, "model_dump"):
        return _to_dict(obj.model_dump())
    if isinstance(obj, list):
        return [_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj

# --------------------------------------------------------------------------
# Isolated Helper: Trigger notification inside workflow module
# --------------------------------------------------------------------------
async def trigger_workflow_notification(
    user_id: str,
    title: str,
    task_id: Optional[str] = None,
    scout_id: Optional[str] = None,
):
    try:
        await db.workflow_notifications.insert_one({
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "title": title,
            "task_id": task_id,
            "scout_id": scout_id,
            "read_at": None,
            "created_at": _now(),
        })
    except Exception as e:
        logger.error("Workflow notification insert failed: %s", e)

# --------------------------------------------------------------------------
# Tasks APIs
# --------------------------------------------------------------------------
@router.get("/tasks")
async def list_tasks(user: dict = Depends(current_user)):
    try:
        role = user.get("role", "team")
        uid = user.get("id")
        
        # Query matching rules
        if role == "admin":
            query = {}
        else:
            query = {"$or": [{"assignee_id": uid}, {"creator_id": uid}]}
            
        tasks = await db.workflow_tasks.find(query, {"_id": 0}).sort("created_at", -1).to_list(1000)
        return _to_dict(tasks)
    except Exception as e:
        logger.error("Error listing workflow tasks: %s", e)
        raise HTTPException(500, detail=f"Failed to fetch tasks: {str(e)}")

@router.post("/tasks")
async def create_task(payload: TaskIn, user: dict = Depends(current_user)):
    try:
        uid = user.get("id")
        tid = str(uuid.uuid4())
        now = _now()
        
        # Enforce clean stripped values and map flat project references
        task_doc = {
            "id": tid,
            "title": payload.title.strip(),
            "description": (payload.description or "").strip(),
            "category": payload.category,
            "status": "pending",
            "assignee_id": payload.assignee_id,
            "creator_id": uid,
            "project_id": payload.project_id,
            "project_name": (payload.project_name or "").strip(),
            "subtasks": _to_dict(payload.subtasks or []),
            "comments": [],
            "attachments": _to_dict(payload.attachments or []),
            "created_at": now,
            "updated_at": now,
        }
        
        await db.workflow_tasks.insert_one(task_doc)
        
        # Notify assignee if task created and assigned to someone else
        if payload.assignee_id and payload.assignee_id != uid:
            await trigger_workflow_notification(
                user_id=payload.assignee_id,
                title=f"New task assigned: {payload.title}",
                task_id=tid,
            )
            
        return _to_dict(task_doc)
    except Exception as e:
        logger.error("Error creating workflow task: %s", e)
        raise HTTPException(400, detail=f"Failed to log task: {str(e)}")

@router.put("/tasks/{tid}")
async def update_task(tid: str, payload: TaskUpdateIn, user: dict = Depends(current_user)):
    try:
        uid = user.get("id")
        role = user.get("role", "team")
        
        task = await db.workflow_tasks.find_one({"id": tid}, {"_id": 0})
        if not task:
            raise HTTPException(404, "Task not found")
            
        # Permission logic
        if role != "admin" and task.get("assignee_id") != uid and task.get("creator_id") != uid:
            raise HTTPException(403, "Access denied")
            
        # Check if this task was created by an admin
        creator = await db.users.find_one({"id": task.get("creator_id")})
        creator_is_admin = creator and creator.get("role") == "admin"
        
        # Construct update sets
        update_data = {}
        
        # Core updates (title, category, assignee, project_id, description)
        is_core_edit = (
            payload.title is not None or
            payload.category is not None or
            payload.assignee_id is not None or
            payload.project_id is not None or
            payload.project_name is not None or
            payload.description is not None
        )
        if is_core_edit and creator_is_admin and role != "admin":
            raise HTTPException(403, "Cannot edit core properties of admin-created tasks")
            
        if payload.title is not None:
            update_data["title"] = payload.title.strip()
        if payload.description is not None:
            update_data["description"] = payload.description.strip()
        if payload.category is not None:
            update_data["category"] = payload.category
        if payload.project_id is not None:
            update_data["project_id"] = payload.project_id
        if payload.project_name is not None:
            update_data["project_name"] = payload.project_name.strip()
            
        # Assignee transition trigger
        if payload.assignee_id is not None:
            prev_assignee = task.get("assignee_id")
            update_data["assignee_id"] = payload.assignee_id
            if payload.assignee_id and payload.assignee_id != prev_assignee and payload.assignee_id != uid:
                await trigger_workflow_notification(
                    user_id=payload.assignee_id,
                    title=f"Task assigned to you: {task.get('title')}",
                    task_id=tid,
                )
                
        # Status updates (allowed for both admin & team)
        if payload.status is not None:
            prev_status = task.get("status")
            update_data["status"] = payload.status
            
            # Trigger status change notification
            if prev_status != payload.status:
                recipients = {task.get("assignee_id"), task.get("creator_id")}
                for r in recipients:
                    if r and r != uid:
                        await trigger_workflow_notification(
                            user_id=r,
                            title=f"Task status changed to {payload.status}: {task.get('title')}",
                            task_id=tid,
                        )
                        
        # Subtasks updates
        if payload.subtasks is not None:
            update_data["subtasks"] = _to_dict(payload.subtasks)
            
        # Attachments updates
        if payload.attachments is not None:
            update_data["attachments"] = _to_dict(payload.attachments)
            
        if update_data:
            update_data["updated_at"] = _now()
            await db.workflow_tasks.update_one({"id": tid}, {"$set": update_data})
            
        updated_task = await db.workflow_tasks.find_one({"id": tid}, {"_id": 0})
        return _to_dict(updated_task)
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error("Error updating workflow task: %s", e)
        raise HTTPException(400, detail=f"Update failed: {str(e)}")

@router.delete("/tasks/{tid}")
async def delete_task(tid: str, user: dict = Depends(current_user)):
    try:
        uid = user.get("id")
        role = user.get("role", "team")
        
        task = await db.workflow_tasks.find_one({"id": tid}, {"_id": 0})
        if not task:
            raise HTTPException(404, "Task not found")
            
        # Check admin or creator-based deletion
        creator = await db.users.find_one({"id": task.get("creator_id")})
        creator_is_admin = creator and creator.get("role") == "admin"
        
        if role != "admin":
            if task.get("creator_id") != uid:
                raise HTTPException(403, "Cannot delete tasks you did not create")
            if creator_is_admin:
                raise HTTPException(403, "Cannot delete admin-created tasks")
                
        await db.workflow_tasks.delete_one({"id": tid})
        return {"ok": True}
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error("Error deleting workflow task: %s", e)
        raise HTTPException(400, detail=f"Deletion failed: {str(e)}")

@router.post("/tasks/{tid}/comments")
async def add_task_comment(tid: str, payload: CommentIn, user: dict = Depends(current_user)):
    try:
        uid = user.get("id")
        role = user.get("role", "team")
        
        task = await db.workflow_tasks.find_one({"id": tid}, {"_id": 0})
        if not task:
            raise HTTPException(404, "Task not found")
            
        if role != "admin" and task.get("assignee_id") != uid and task.get("creator_id") != uid:
            raise HTTPException(403, "Access denied")
            
        cid = str(uuid.uuid4())
        now = _now()
        
        comment = {
            "id": cid,
            "author_id": uid,
            "author_name": user.get("name", "User"),
            "text": payload.text.strip(),
            "attachments": _to_dict(payload.attachments or []),
            "created_at": now,
        }
        
        await db.workflow_tasks.update_one(
            {"id": tid},
            {"$push": {"comments": comment}, "$set": {"updated_at": now}}
        )
        
        # Notify team coordinates (assignee & creator)
        recipients = {task.get("assignee_id"), task.get("creator_id")}
        for r in recipients:
            if r and r != uid:
                await trigger_workflow_notification(
                    user_id=r,
                    title=f"New comment from {user.get('name')}: {task.get('title')}",
                    task_id=tid,
                )
                
        return _to_dict(comment)
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error("Error adding task comment: %s", e)
        raise HTTPException(400, detail=f"Comment failed: {str(e)}")

# --------------------------------------------------------------------------
# Scouting Pipeline APIs
# --------------------------------------------------------------------------
@router.get("/scouting")
async def list_scout_entries(_: dict = Depends(current_user)):
    try:
        entries = await db.workflow_scouts.find({}, {"_id": 0}).sort("created_at", -1).to_list(2000)
        return _to_dict(entries)
    except Exception as e:
        logger.error("Error listing scouting queue: %s", e)
        raise HTTPException(500, detail="Failed to fetch scouting entries")

@router.post("/scouting")
async def create_scout_entry(payload: ScoutEntryIn, user: dict = Depends(current_user)):
    try:
        sid = str(uuid.uuid4())
        now = _now()
        
        entry_doc = {
            "id": sid,
            "instagram_link": payload.instagram_link.strip(),
            "phone": payload.phone.strip(),
            "name": (payload.name or "").strip(),
            "notes": (payload.notes or "").strip(),
            "assigned_id": payload.assigned_id,
            "status": payload.status,
            # AI Scout Capture — structured fields (optional)
            "instagram_username": (payload.instagram_username or "").strip().lstrip("@").lower() or None,
            "followers_count": payload.followers_count,
            "category": (payload.category or "").strip() or None,
            "location": (payload.location or "").strip() or None,
            "manager_name": (payload.manager_name or "").strip() or None,
            "manager_phone": (payload.manager_phone or "").strip() or None,
            "capture_audit_id": payload.capture_audit_id,
            "source": "ai_capture" if payload.capture_audit_id else "manual",
            "attachments": [],
            "created_at": now,
            "updated_at": now,
        }
        
        await db.workflow_scouts.insert_one(entry_doc)
        # insert_one mutates entry_doc in place, adding a bson ObjectId under
        # "_id" which is NOT JSON-serializable — returning it makes FastAPI 500
        # AFTER the row was already written (the "false failed-to-save" bug).
        entry_doc.pop("_id", None)
        return _to_dict(entry_doc)
    except Exception as e:
        logger.error("Error creating scout entry: %s", e)
        raise HTTPException(400, detail=f"Failed to create scout log: {str(e)}")

@router.post("/scouting/ai-capture")
async def scout_ai_capture(
    files: List[UploadFile] = File(...),
    user: dict = Depends(current_user),
):
    """AI Scout Capture — local OCR + entity extraction + duplicate detection.

    Accepts one or more screenshots (PNG/JPG/JPEG/WEBP). Runs free, self-hosted
    EasyOCR + regex/heuristic extraction, normalises the fields, checks for an
    existing talent or scout match, and writes an audit record. Does NOT create a
    scout entry — the review modal confirms (and optionally edits) before saving
    via POST /scouting.
    """
    # Upload-limit guards BEFORE reading bytes / starting OCR (cheap rejection).
    if not files:
        raise HTTPException(400, "At least one screenshot is required.")
    if len(files) > scout_capture.MAX_IMAGES:
        raise HTTPException(400, f"At most {scout_capture.MAX_IMAGES} screenshots per capture.")
    for f in files:
        # Starlette populates .size from the multipart part's content-length when
        # available; reject oversized files before buffering them into memory.
        if f.size is not None and f.size > scout_capture.MAX_IMAGE_BYTES:
            raise HTTPException(400, f"{f.filename or 'Screenshot'} exceeds the 10 MB limit.")

    try:
        payload = []
        for f in files:
            data = await f.read()
            payload.append((f.filename or "screenshot", f.content_type or "", data))
        result = await scout_capture.run_capture(payload, user_id=user.get("id"))
        return result
    except scout_capture.ScoutCaptureError as e:
        raise HTTPException(e.status_code, detail=e.detail)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("scout ai-capture failed: %s", e)
        raise HTTPException(500, detail="AI capture failed unexpectedly")


@router.put("/scouting/{sid}")
async def update_scout_entry(sid: str, payload: ScoutEntryUpdateIn, _: dict = Depends(current_user)):
    try:
        entry = await db.workflow_scouts.find_one({"id": sid}, {"_id": 0})
        if not entry:
            raise HTTPException(404, "Scout entry not found")
            
        update_data = {}
        if payload.instagram_link is not None:
            update_data["instagram_link"] = payload.instagram_link.strip()
        if payload.phone is not None:
            update_data["phone"] = payload.phone.strip()
        if payload.name is not None:
            update_data["name"] = payload.name.strip()
        if payload.notes is not None:
            update_data["notes"] = payload.notes.strip()
        if payload.assigned_id is not None:
            update_data["assigned_id"] = payload.assigned_id
        if payload.status is not None:
            update_data["status"] = payload.status
        if payload.instagram_username is not None:
            update_data["instagram_username"] = payload.instagram_username.strip().lstrip("@").lower() or None
        if payload.followers_count is not None:
            update_data["followers_count"] = payload.followers_count
        if payload.category is not None:
            update_data["category"] = payload.category.strip() or None
        if payload.location is not None:
            update_data["location"] = payload.location.strip() or None
        if payload.manager_name is not None:
            update_data["manager_name"] = payload.manager_name.strip() or None
        if payload.manager_phone is not None:
            update_data["manager_phone"] = payload.manager_phone.strip() or None

        if update_data:
            update_data["updated_at"] = _now()
            await db.workflow_scouts.update_one({"id": sid}, {"$set": update_data})
            
        updated = await db.workflow_scouts.find_one({"id": sid}, {"_id": 0})
        return _to_dict(updated)
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error("Error updating scout entry: %s", e)
        raise HTTPException(400, detail="Update failed")

@router.delete("/scouting/{sid}")
async def delete_scout_entry(sid: str, user: dict = Depends(require_role("admin"))):
    try:
        entry = await db.workflow_scouts.find_one({"id": sid}, {"_id": 0})
        if not entry:
            raise HTTPException(404, "Scout entry not found")
        await db.workflow_scouts.delete_one({"id": sid})
        return {"ok": True}
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error("Error deleting scout entry: %s", e)
        raise HTTPException(400, detail="Deletion failed")

# --------------------------------------------------------------------------
# Workflow Notifications APIs
# --------------------------------------------------------------------------
@router.get("/notifications")
async def list_workflow_notifications(user: dict = Depends(current_user)):
    try:
        uid = user.get("id")
        notifs = await db.workflow_notifications.find(
            {"user_id": uid, "read_at": None},
            {"_id": 0}
        ).sort("created_at", -1).to_list(100)
        return _to_dict(notifs)
    except Exception as e:
        logger.error("Error listing workflow notifications: %s", e)
        raise HTTPException(500, detail="Failed to fetch workflow alerts")

@router.post("/notifications/read-all")
async def read_all_workflow_notifications(user: dict = Depends(current_user)):
    try:
        uid = user.get("id")
        now = _now()
        res = await db.workflow_notifications.update_many(
            {"user_id": uid, "read_at": None},
            {"$set": {"read_at": now}}
        )
        return {"marked": res.modified_count}
    except Exception as e:
        logger.error("Error clearing workflow notifications: %s", e)
        raise HTTPException(500, detail="Failed to clear alerts")
