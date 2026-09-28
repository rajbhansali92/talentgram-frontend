import asyncio
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from core import db, current_user, current_team_or_admin, _now, require_role
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
# Workflow → WhatsApp group notifications (Talentgram Workflow group)
#
# Reuses the EXACT same fire-and-forget queue mechanism already used for
# internal submission notifications (see
# routers.submissions._enqueue_internal_whatsapp_notification_task /
# enqueue_internal_whatsapp_notification) — same db.whatsapp_batches /
# db.whatsapp_jobs collections, same recipient_kind="INTERNAL_GROUP" /
# destination_type="group" shape, same asyncio.create_task fire-and-forget
# wrapping with all errors swallowed+logged so a WhatsApp/Mongo hiccup can
# never fail or roll back the Workflow mutation that triggered it. No new
# WhatsApp system, no new worker, no new queue.
# --------------------------------------------------------------------------
WORKFLOW_APP_URL = "https://review.talentgramagency.com/admin/workflow"


def _workflow_task_link(task_id: str) -> str:
    return f"{WORKFLOW_APP_URL}?task={task_id}"


def _fmt_event_ts(iso: Optional[str]) -> str:
    """Event date/time, always with year — e.g. '28 Sep 2026, 8:32 PM'."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        hour12 = dt.strftime("%I").lstrip("0") or "12"
        return f"{dt.strftime('%d %b %Y')}, {hour12}:{dt.strftime('%M %p')}"
    except Exception:
        return iso


def _fmt_due(iso: Optional[str]) -> Optional[str]:
    """Due date, no year (matches the brief's own examples) — e.g. '29 Sep, 5:00 PM'."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        hour12 = dt.strftime("%I").lstrip("0") or "12"
        return f"{dt.strftime('%d %b')}, {hour12}:{dt.strftime('%M %p')}"
    except Exception:
        return iso


async def _resolve_user_names(user_ids: set) -> Dict[str, str]:
    ids = [uid for uid in user_ids if uid]
    if not ids:
        return {}
    docs = await db.users.find({"id": {"$in": ids}}, {"_id": 0, "id": 1, "name": 1, "email": 1}).to_list(len(ids))
    return {d["id"]: (d.get("name") or d.get("email") or "Unknown") for d in docs}


async def _workflow_notification_group_name() -> str:
    cfg = await db.whatsapp_config.find_one({"key": "workflow_notification_group_name"})
    value = cfg.get("value") if cfg else None
    return value or "Talentgram Workflow"


async def _enqueue_workflow_whatsapp_notification_task(message_body: str, source_id: str):
    try:
        group_name = await _workflow_notification_group_name()
        timestamp = _now()
        batch_id = str(uuid.uuid4())
        batch_doc = {
            "id": batch_id,
            "source_type": "WORKFLOW_NOTIFICATION",
            "source_label": "Workflow Notification",
            "project_id": None,
            "project_name": None,
            "template_id": "workflow_notification",
            "template_slug": "workflow_notification",
            "variable_data": {},
            "media_url": None,
            "is_dry_run": False,
            "status": "pending",
            "total_jobs": 1,
            "sent_count": 0,
            "failed_count": 0,
            "unconfirmed_count": 0,
            "created_by": "system",
            "created_at": timestamp,
            "started_at": None,
            "completed_at": None,
        }
        job_doc = {
            "id": str(uuid.uuid4()),
            "batch_id": batch_id,
            "template_id": "workflow_notification",
            "template_name": "Workflow Notification",
            "source": "WORKFLOW_NOTIFICATION",
            "source_id": source_id,
            "recipient_kind": "INTERNAL_GROUP",
            "recipient_id": "workflow_notification_group",
            "talent_id": None,
            "talent_name": group_name,
            "destination_type": "group",
            "destination": group_name,
            "message_body": message_body,
            "media_url": None,
            "is_dry_run": False,
            "status": "pending",
            "attempt_count": 0,
            "last_attempted_at": None,
            "sent_at": None,
            "error_message": None,
            "worker_picked_at": None,
            "created_at": timestamp,
        }
        await db.whatsapp_batches.insert_one(batch_doc)
        await db.whatsapp_jobs.insert_one(job_doc)
        logger.info(f"Enqueued workflow WhatsApp notification for {source_id}")
    except Exception as e:
        logger.warning(f"Error enqueuing workflow WhatsApp notification: {e}", exc_info=True)


def enqueue_workflow_whatsapp_notification(message_body: str, source_id: str):
    """Fire-and-forget — never awaited by the caller, never allowed to
    raise into the request path. A Workflow mutation must always succeed
    even if WhatsApp/Mongo is unavailable. `source_id` is a free-form trace
    tag (a task id for Workflow tasks, a "talent_id:project_id" pair for
    Workflow Calls) — shared by both routers.workflow and
    routers.workflow_calls, same queue, same infrastructure, no second
    notification system."""
    try:
        asyncio.create_task(_enqueue_workflow_whatsapp_notification_task(message_body, source_id))
    except Exception as e:
        logger.warning(f"Failed to schedule workflow WhatsApp notification: {e}", exc_info=True)


# --------------------------------------------------------------------------
# ONE shared message formatter (2026-09-28 refinement pass) — every event
# type below builds its message by calling this, instead of each hand-
# rolling its own line list. Handles the three things every event needs
# identically: omitting an empty Project line (never "Project: —"/"None"),
# blank-line spacing between logical sections, and the closing link block.
# `blocks` is an ordered list of line-groups; empty/falsy groups are
# skipped, and a single blank line separates each remaining group — this
# is what lets NEW CHECKLIST ITEM put its content before the actor lines
# while TASK UPDATED puts "Updated by" before its Changes list, without
# two different formatters.
# --------------------------------------------------------------------------
def _format_workflow_message(
    header: str,
    *,
    subject_label: str,  # "Task" or "Talent"
    subject_name: str,
    project_name: Optional[str],
    blocks: list,
    link: str,
    link_label: str = "Open Task:",
) -> str:
    top = [header, f"{subject_label}: {subject_name}"]
    if project_name:
        top.append(f"Project: {project_name}")
    groups = [top] + [b for b in blocks if b] + [[link_label, link]]
    return "\n\n".join("\n".join(g) for g in groups)


def _build_new_task_message(task: dict, assignee_name: str, creator_name: str) -> str:
    meta = [f"Assigned to: {assignee_name}", f"Created by: {creator_name}"]
    due_fmt = _fmt_due(task.get("due_at"))
    if due_fmt:
        meta.append(f"Due: {due_fmt}")
    if task.get("priority"):
        meta.append(f"Priority: {task['priority'].title()}")
    return _format_workflow_message(
        "🆕 *NEW TASK*", subject_label="Task", subject_name=task.get("title"),
        project_name=task.get("project_name"),
        blocks=[meta, [f"Created: {_fmt_event_ts(task.get('created_at'))}"]],
        link=_workflow_task_link(task["id"]),
    )


def _build_new_checklist_item_message(task: dict, checklist_text: str, actor_name: str, assignee_name: str, event_ts: str) -> str:
    meta = [f"Added by: {actor_name}", f"Assigned to: {assignee_name}"]
    due_fmt = _fmt_due(task.get("due_at"))
    if due_fmt:
        meta.append(f"Due: {due_fmt}")
    return _format_workflow_message(
        "📋 *NEW CHECKLIST ITEM*", subject_label="Task", subject_name=task.get("title"),
        project_name=task.get("project_name"),
        blocks=[["Checklist:", checklist_text], meta, [f"Added: {_fmt_event_ts(event_ts)}"]],
        link=_workflow_task_link(task["id"]),
    )


def _build_task_completed_message(task: dict, actor_name: str, event_ts: str) -> str:
    return _format_workflow_message(
        "✅ *TASK COMPLETED*", subject_label="Task", subject_name=task.get("title"),
        project_name=task.get("project_name"),
        blocks=[[f"Completed by: {actor_name}"], [f"Completed: {_fmt_event_ts(event_ts)}"]],
        link=_workflow_task_link(task["id"]),
    )


def _build_checklist_completed_message(task: dict, checklist_text: str, actor_name: str, event_ts: str) -> str:
    return _format_workflow_message(
        "✅ *CHECKLIST COMPLETED*", subject_label="Task", subject_name=task.get("title"),
        project_name=task.get("project_name"),
        blocks=[["Completed:", checklist_text], [f"Completed by: {actor_name}"], [f"Completed: {_fmt_event_ts(event_ts)}"]],
        link=_workflow_task_link(task["id"]),
    )


def _build_checklist_updated_message(task: dict, old_text: str, new_text: str, actor_name: str, event_ts: str) -> str:
    return _format_workflow_message(
        "✏️ *CHECKLIST UPDATED*", subject_label="Task", subject_name=task.get("title"),
        project_name=task.get("project_name"),
        blocks=[
            ["Checklist:", new_text],
            [f"Updated by: {actor_name}"],
            ["Changes:", f"• Text: {old_text} → {new_text}"],
            [f"Updated: {_fmt_event_ts(event_ts)}"],
        ],
        link=_workflow_task_link(task["id"]),
    )


def _build_task_assigned_message(task: dict, old_assignee_name: str, new_assignee_name: str, actor_name: str, event_ts: str) -> str:
    return _format_workflow_message(
        "👤 *TASK ASSIGNED*", subject_label="Task", subject_name=task.get("title"),
        project_name=task.get("project_name"),
        blocks=[
            [f"Assigned to: {new_assignee_name}", f"Previously: {old_assignee_name}", f"Assigned by: {actor_name}"],
            [f"Assigned: {_fmt_event_ts(event_ts)}"],
        ],
        link=_workflow_task_link(task["id"]),
    )


def _build_task_updated_message(task: dict, change_lines: list, actor_name: str, event_ts: str) -> str:
    return _format_workflow_message(
        "✏️ *TASK UPDATED*", subject_label="Task", subject_name=task.get("title"),
        project_name=task.get("project_name"),
        blocks=[
            [f"Updated by: {actor_name}"],
            ["Changes:"] + [f"• {c}" for c in change_lines],
            [f"Updated: {_fmt_event_ts(event_ts)}"],
        ],
        link=_workflow_task_link(task["id"]),
    )


def _build_task_update_comment_message(task: dict, comment_text: str, actor_name: str, event_ts: str) -> str:
    """PART 3 — an existing task comment/update note (POST /tasks/{id}/comments)
    reported through the same WhatsApp event system, using the app's own
    existing "Updates" field (no new comment mechanism invented)."""
    return _format_workflow_message(
        "📝 *TASK UPDATE*", subject_label="Task", subject_name=task.get("title"),
        project_name=task.get("project_name"),
        blocks=[[f"Update by: {actor_name}", "Update:", f'"{comment_text}"'], [f"Updated: {_fmt_event_ts(event_ts)}"]],
        link=_workflow_task_link(task["id"]),
    )


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


def _today_bounds_utc():
    """Start/end of "today" in UTC, as ISO 8601 strings matching core._now()'s
    own format — every due_at is written in this same shape, so a plain
    lexicographic string range query is correct (no datetime parsing
    needed) and matches the string-comparison convention already used
    elsewhere in this codebase for *_at fields."""
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


@router.get("/tasks/production")
async def list_production_tasks(
    project_id: Optional[str] = None,
    talent_id: Optional[str] = None,
    scope: Optional[str] = None,  # "pending" | "today" | "upcoming" | None (all)
    admin: dict = Depends(current_team_or_admin),
):
    """Project/talent-scoped task query for Production Desk + the
    Management Agent — deliberately NOT scoped to assignee/creator like
    GET /tasks (that endpoint is a personal to-do view; this is a
    project-wide OPERATIONAL view, matching how every other Production
    Desk read works — no per-user filtering). Reads the SAME
    db.workflow_tasks collection GET /tasks and the admin Workflow page
    already use; a task created here shows up there and vice versa —
    there is no second task store.
    """
    if not project_id and not talent_id:
        raise HTTPException(400, "project_id or talent_id is required")

    query: Dict[str, Any] = {}
    if project_id:
        query["project_id"] = project_id
    if talent_id:
        query["talent_id"] = talent_id

    if scope == "pending":
        query["status"] = {"$in": ["pending", "in_progress"]}
    elif scope == "today":
        start, end = _today_bounds_utc()
        query["due_at"] = {"$gte": start, "$lt": end}
        query["status"] = {"$in": ["pending", "in_progress"]}
    elif scope == "upcoming":
        _, end_today = _today_bounds_utc()
        query["due_at"] = {"$gte": end_today}
        query["status"] = {"$in": ["pending", "in_progress"]}
    elif scope not in (None, "all"):
        raise HTTPException(400, 'scope must be one of "pending", "today", "upcoming", "all"')

    tasks = await db.workflow_tasks.find(query, {"_id": 0}).sort("due_at", 1).to_list(500)
    return _to_dict(tasks)


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
            # Production Management Desk — additive, optional (see
            # workflow_schemas.TaskIn). None for every existing caller.
            "talent_id": payload.talent_id,
            "due_at": payload.due_at,
            "priority": payload.priority,
            "created_at": now,
            "updated_at": now,
        }
        
        await db.workflow_tasks.insert_one(task_doc)
        # PRE-EXISTING BUG (found and fixed while extending this endpoint,
        # 2026-09): Motor's insert_one() mutates the passed dict in place,
        # adding an `_id` ObjectId — every call to this endpoint was
        # crashing the response's JSON serialization with "'ObjectId'
        # object is not iterable" (confirmed present before this file's
        # Production Management Desk changes too, via `git stash`).
        # list_tasks/update_task were unaffected because they always
        # re-fetch with an explicit {"_id": 0} projection; this is the
        # only place that returned the in-memory dict directly.
        task_doc.pop("_id", None)

        # Notify assignee if task created and assigned to someone else
        if payload.assignee_id and payload.assignee_id != uid:
            await trigger_workflow_notification(
                user_id=payload.assignee_id,
                title=f"New task assigned: {payload.title}",
                task_id=tid,
            )

        # WhatsApp group notification — fire-and-forget, never blocks/fails
        # task creation (see enqueue_workflow_whatsapp_notification).
        name_map = await _resolve_user_names({uid, payload.assignee_id})
        creator_name = name_map.get(uid, user.get("name") or user.get("email") or "Someone")
        assignee_name = name_map.get(payload.assignee_id, "Unassigned") if payload.assignee_id else "Unassigned"
        enqueue_workflow_whatsapp_notification(
            _build_new_task_message(task_doc, assignee_name, creator_name), tid,
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
            payload.description is not None or
            payload.talent_id is not None or
            payload.due_at is not None or
            payload.priority is not None
        )
        if is_core_edit and creator_is_admin and role != "admin":
            raise HTTPException(403, "Cannot edit core properties of admin-created tasks")
            
        # WhatsApp change-tracking — accumulated alongside the existing
        # field-by-field update_data construction below; a "before" value is
        # already in `task` (fetched above) so no extra query is needed. Only
        # a field actually changing value is ever appended — a save that
        # resends the same value produces zero lines, matching the "no
        # notification for no-op changes" requirement.
        change_lines: List[str] = []

        if payload.title is not None:
            new_title = payload.title.strip()
            if new_title != (task.get("title") or ""):
                change_lines.append(f"Title: {task.get('title')} → {new_title}")
            update_data["title"] = new_title
        if payload.description is not None:
            new_desc = payload.description.strip()
            if new_desc != (task.get("description") or ""):
                change_lines.append("Description updated")
            update_data["description"] = new_desc
        if payload.category is not None:
            if payload.category != task.get("category"):
                change_lines.append(f"Category: {(task.get('category') or '').title()} → {payload.category.title()}")
            update_data["category"] = payload.category
        project_changed = payload.project_id is not None and payload.project_id != task.get("project_id")
        if payload.project_id is not None:
            update_data["project_id"] = payload.project_id
        if payload.project_name is not None:
            update_data["project_name"] = payload.project_name.strip()
        if project_changed:
            old_pname = task.get("project_name") or "(none)"
            new_pname = (payload.project_name or "").strip() or "(none)"
            change_lines.append(f"Project: {old_pname} → {new_pname}")
        if payload.talent_id is not None:
            update_data["talent_id"] = payload.talent_id
        if payload.due_at is not None:
            if payload.due_at != task.get("due_at"):
                change_lines.append(f"Due date: {_fmt_due(task.get('due_at')) or 'None'} → {_fmt_due(payload.due_at) or 'None'}")
            update_data["due_at"] = payload.due_at
        if payload.priority is not None:
            if payload.priority != task.get("priority"):
                old_p = (task.get("priority") or "none").title()
                new_p = (payload.priority or "none").title()
                change_lines.append(f"Priority: {old_p} → {new_p}")
            update_data["priority"] = payload.priority

        # Assignee transition trigger
        assignee_changed = False
        prev_assignee = task.get("assignee_id")
        if payload.assignee_id is not None:
            update_data["assignee_id"] = payload.assignee_id
            assignee_changed = payload.assignee_id != prev_assignee
            if payload.assignee_id and assignee_changed and payload.assignee_id != uid:
                await trigger_workflow_notification(
                    user_id=payload.assignee_id,
                    title=f"Task assigned to you: {task.get('title')}",
                    task_id=tid,
                )

        # Status updates (allowed for both admin & team)
        status_changed_to_completed = False
        if payload.status is not None:
            prev_status = task.get("status")
            update_data["status"] = payload.status

            # Trigger status change notification
            if prev_status != payload.status:
                if payload.status == "completed":
                    status_changed_to_completed = True
                else:
                    # Non-completion transitions (e.g. reopened, in_progress)
                    # are folded into the general TASK UPDATED message below
                    # rather than a dedicated event type, per the brief's own
                    # "avoid duplicate/fragmented notifications" guidance.
                    change_lines.append(
                        f"Status: {(prev_status or '').replace('_', ' ').title()} → {payload.status.replace('_', ' ').title()}"
                    )
                recipients = {task.get("assignee_id"), task.get("creator_id")}
                for r in recipients:
                    if r and r != uid:
                        await trigger_workflow_notification(
                            user_id=r,
                            title=f"Task status changed to {payload.status}: {task.get('title')}",
                            task_id=tid,
                        )

        # Subtasks updates — the frontend always sends the FULL modified
        # array (see workflow_schemas.TaskUpdateIn), so a checklist item's
        # add/complete/text-edit/removal is detected by diffing the old and
        # new arrays by id, not from any separate per-item endpoint.
        added_subtasks: List[dict] = []
        completed_subtasks: List[dict] = []
        edited_subtasks: List[tuple] = []
        if payload.subtasks is not None:
            new_subtasks = _to_dict(payload.subtasks)
            update_data["subtasks"] = new_subtasks
            old_subtasks_by_id = {s["id"]: s for s in (task.get("subtasks") or [])}
            new_ids = {s["id"] for s in new_subtasks}
            for s in new_subtasks:
                old = old_subtasks_by_id.get(s["id"])
                if old is None:
                    added_subtasks.append(s)
                elif (not old.get("completed")) and s.get("completed"):
                    completed_subtasks.append(s)
                elif old.get("text") != s.get("text") and old.get("completed") == s.get("completed"):
                    edited_subtasks.append((old, s))
            for old_id, old in old_subtasks_by_id.items():
                if old_id not in new_ids:
                    change_lines.append(f"Checklist removed: {old.get('text')}")

        # Attachments updates
        if payload.attachments is not None:
            update_data["attachments"] = _to_dict(payload.attachments)

        if update_data:
            update_data["updated_at"] = _now()
            await db.workflow_tasks.update_one({"id": tid}, {"$set": update_data})

        updated_task = await db.workflow_tasks.find_one({"id": tid}, {"_id": 0})

        # WhatsApp group notifications — fire-and-forget, computed from the
        # diffs above; never blocks/fails the update itself (see
        # enqueue_workflow_whatsapp_notification's own error containment).
        event_ts = updated_task.get("updated_at") or _now()
        name_map = await _resolve_user_names({uid, prev_assignee, payload.assignee_id})
        actor_name = name_map.get(uid, user.get("name") or user.get("email") or "Someone")

        for s in added_subtasks:
            assignee_name = name_map.get(updated_task.get("assignee_id"), "Unassigned") if updated_task.get("assignee_id") else "Unassigned"
            enqueue_workflow_whatsapp_notification(
                _build_new_checklist_item_message(updated_task, s["text"], actor_name, assignee_name, event_ts), tid,
            )
        for s in completed_subtasks:
            enqueue_workflow_whatsapp_notification(
                _build_checklist_completed_message(updated_task, s["text"], actor_name, event_ts), tid,
            )
        for old, new in edited_subtasks:
            enqueue_workflow_whatsapp_notification(
                _build_checklist_updated_message(updated_task, old["text"], new["text"], actor_name, event_ts), tid,
            )
        if status_changed_to_completed:
            enqueue_workflow_whatsapp_notification(
                _build_task_completed_message(updated_task, actor_name, event_ts), tid,
            )
        if assignee_changed and not change_lines:
            old_assignee_name = name_map.get(prev_assignee, "Unassigned") if prev_assignee else "Unassigned"
            new_assignee_name = name_map.get(payload.assignee_id, "Unassigned") if payload.assignee_id else "Unassigned"
            enqueue_workflow_whatsapp_notification(
                _build_task_assigned_message(updated_task, old_assignee_name, new_assignee_name, actor_name, event_ts), tid,
            )
        elif assignee_changed:
            old_assignee_name = name_map.get(prev_assignee, "Unassigned") if prev_assignee else "Unassigned"
            new_assignee_name = name_map.get(payload.assignee_id, "Unassigned") if payload.assignee_id else "Unassigned"
            change_lines.append(f"Assigned to: {old_assignee_name} → {new_assignee_name}")
        if change_lines:
            enqueue_workflow_whatsapp_notification(
                _build_task_updated_message(updated_task, change_lines, actor_name, event_ts), tid,
            )

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

        # WhatsApp group notification (PART 3) — the app's existing
        # comment/update field, reported through the same event system.
        enqueue_workflow_whatsapp_notification(
            _build_task_update_comment_message(task, comment["text"], user.get("name") or user.get("email") or "Someone", now),
            tid,
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
