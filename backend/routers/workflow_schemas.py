from typing import Any, Dict, List, Optional
from pydantic import BaseModel

class SubtaskItem(BaseModel):
    id: str
    text: str
    completed: bool = False
    completed_at: Optional[str] = None

class CommentAttachment(BaseModel):
    type: str  # "image" | "link"
    url: str
    name: Optional[str] = None

class CommentIn(BaseModel):
    text: str
    attachments: Optional[List[CommentAttachment]] = []

class TaskAttachment(BaseModel):
    type: str  # "image" | "link"
    url: str
    name: Optional[str] = None

class TaskIn(BaseModel):
    title: str
    description: Optional[str] = ""
    category: str = "general"  # "general" | "project" | "scouting" | "finance"
    assignee_id: Optional[str] = None
    project_id: Optional[str] = None  # lightweight flat reference ID
    project_name: Optional[str] = ""  # lightweight flat reference Name
    subtasks: Optional[List[SubtaskItem]] = []
    attachments: Optional[List[TaskAttachment]] = []
    # Production Management Desk (additive, back-compatible — every
    # existing General/Scouting/Finance-category task simply never sets
    # these). talent_id is the SAME id used everywhere else in the app
    # (db.talents.id / casting_pipeline.talent_id) — no new relationship
    # type. due_at is an ISO 8601 datetime string, same shape as every
    # other *_at field in this codebase (core._now()'s own format).
    talent_id: Optional[str] = None
    due_at: Optional[str] = None
    priority: Optional[str] = None  # "low" | "normal" | "high" — informational, not enforced

class TaskUpdateIn(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None  # "pending" | "in_progress" | "completed" | "archived"
    assignee_id: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    subtasks: Optional[List[SubtaskItem]] = None
    attachments: Optional[List[TaskAttachment]] = None
    talent_id: Optional[str] = None
    due_at: Optional[str] = None
    priority: Optional[str] = None

class ScoutEntryIn(BaseModel):
    instagram_link: str
    phone: str
    name: Optional[str] = ""
    notes: Optional[str] = ""
    assigned_id: Optional[str] = None
    status: str = "not_contacted"
    # AI Scout Capture — structured fields (optional; additive, back-compatible)
    instagram_username: Optional[str] = None
    followers_count: Optional[int] = None
    category: Optional[str] = None
    location: Optional[str] = None
    manager_name: Optional[str] = None
    manager_phone: Optional[str] = None
    capture_audit_id: Optional[str] = None  # links the row to its extraction audit

class ScoutEntryUpdateIn(BaseModel):
    instagram_link: Optional[str] = None
    phone: Optional[str] = None
    name: Optional[str] = None
    notes: Optional[str] = None
    assigned_id: Optional[str] = None
    status: Optional[str] = None
    instagram_username: Optional[str] = None
    followers_count: Optional[int] = None
    category: Optional[str] = None
    location: Optional[str] = None
    manager_name: Optional[str] = None
    manager_phone: Optional[str] = None
