import uuid
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from core import db, current_user, _now, require_role
from .workflow_schemas import (
    TaskIn,
    TaskUpdateIn,
    CommentIn,
    ScoutEntryIn,
    ScoutEntryUpdateIn,
)

router = APIRouter(prefix="/api/workflow", tags=["workflow"])

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
        print("Workflow notification insert failed:", e)

# --------------------------------------------------------------------------
# Tasks APIs
# --------------------------------------------------------------------------
@router.get("/tasks")
async def list_tasks(user: dict = Depends(current_user)):
    role = user.get("role", "team")
    uid = user.get("id")
    
    # Query matching rules
    if role == "admin":
        query = {}
    else:
        query = {"$or": [{"assignee_id": uid}, {"creator_id": uid}]}
        
    tasks = await db.workflow_tasks.find(query, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return tasks

@router.post("/tasks")
async def create_task(payload: TaskIn, user: dict = Depends(current_user)):
    uid = user.get("id")
    tid = str(uuid.uuid4())
    now = _now()
    
    task_doc = {
        "id": tid,
        "title": payload.title.strip(),
        "description": (payload.description or "").strip(),
        "category": payload.category,
        "status": "pending",
        "assignee_id": payload.assignee_id,
        "creator_id": uid,
        "project_id": payload.project_id,
        "subtasks": [s.dict() for s in (payload.subtasks or [])],
        "comments": [],
        "attachments": [a.dict() for a in (payload.attachments or [])],
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
        
    return task_doc

@router.put("/tasks/{tid}")
async def update_task(tid: str, payload: TaskUpdateIn, user: dict = Depends(current_user)):
    uid = user.get("id")
    role = user.get("role", "team")
    
    task = await db.workflow_tasks.find_one({"id": tid}, {"_id": 0})
    if not task:
        raise HTTPException(404, "Task not found")
        
    # Permission logic
    # Team members can only see/edit their own or assigned tasks
    if role != "admin" and task.get("assignee_id") != uid and task.get("creator_id") != uid:
        raise HTTPException(403, "Access denied")
        
    # Check if this task was created by an admin
    creator = await db.users.find_one({"id": task.get("creator_id")})
    creator_is_admin = creator and creator.get("role") == "admin"
    
    # Construct update sets
    update_data = {}
    
    # Core updates (title, category, assignee, project_id, description)
    # Restricted if team member is editing an admin-created task
    is_core_edit = (
        payload.title is not None or
        payload.category is not None or
        payload.assignee_id is not None or
        payload.project_id is not None or
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
                    
    # Subtasks updates (allowed for both)
    if payload.subtasks is not None:
        update_data["subtasks"] = [s.dict() for s in payload.subtasks]
        
    # Attachments updates (allowed for both)
    if payload.attachments is not None:
        update_data["attachments"] = [a.dict() for a in payload.attachments]
        
    if update_data:
        update_data["updated_at"] = _now()
        await db.workflow_tasks.update_one({"id": tid}, {"$set": update_data})
        
    updated_task = await db.workflow_tasks.find_one({"id": tid}, {"_id": 0})
    return updated_task

@router.delete("/tasks/{tid}")
async def delete_task(tid: str, user: dict = Depends(current_user)):
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

@router.post("/tasks/{tid}/comments")
async def add_task_comment(tid: str, payload: CommentIn, user: dict = Depends(current_user)):
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
        "attachments": [a.dict() for a in payload.attachments or []],
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
            
    return comment

# --------------------------------------------------------------------------
# Scouting Pipeline APIs
# --------------------------------------------------------------------------
@router.get("/scouting")
async def list_scout_entries(_: dict = Depends(current_user)):
    entries = await db.workflow_scouts.find({}, {"_id": 0}).sort("created_at", -1).to_list(2000)
    return entries

@router.post("/scouting")
async def create_scout_entry(payload: ScoutEntryIn, user: dict = Depends(current_user)):
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
        "attachments": [],
        "created_at": now,
        "updated_at": now,
    }
    
    await db.workflow_scouts.insert_one(entry_doc)
    return entry_doc

@router.put("/scouting/{sid}")
async def update_scout_entry(sid: str, payload: ScoutEntryUpdateIn, _: dict = Depends(current_user)):
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
        
    if update_data:
        update_data["updated_at"] = _now()
        await db.workflow_scouts.update_one({"id": sid}, {"$set": update_data})
        
    updated = await db.workflow_scouts.find_one({"id": sid}, {"_id": 0})
    return updated

@router.delete("/scouting/{sid}")
async def delete_scout_entry(sid: str, user: dict = Depends(require_role("admin"))):
    entry = await db.workflow_scouts.find_one({"id": sid}, {"_id": 0})
    if not entry:
        raise HTTPException(404, "Scout entry not found")
    await db.workflow_scouts.delete_one({"id": sid})
    return {"ok": True}

# --------------------------------------------------------------------------
# Workflow Notifications APIs
# --------------------------------------------------------------------------
@router.get("/notifications")
async def list_workflow_notifications(user: dict = Depends(current_user)):
    uid = user.get("id")
    notifs = await db.workflow_notifications.find(
        {"user_id": uid, "read_at": None},
        {"_id": 0}
    ).sort("created_at", -1).to_list(100)
    return notifs

@router.post("/notifications/read-all")
async def read_all_workflow_notifications(user: dict = Depends(current_user)):
    uid = user.get("id")
    now = _now()
    res = await db.workflow_notifications.update_many(
        {"user_id": uid, "read_at": None},
        {"$set": {"read_at": now}}
    )
    return {"marked": res.modified_count}
