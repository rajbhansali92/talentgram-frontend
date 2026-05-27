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

class ScoutEntryIn(BaseModel):
    instagram_link: str
    phone: str
    name: Optional[str] = ""
    notes: Optional[str] = ""
    assigned_id: Optional[str] = None
    status: str = "not_contacted"

class ScoutEntryUpdateIn(BaseModel):
    instagram_link: Optional[str] = None
    phone: Optional[str] = None
    name: Optional[str] = None
    notes: Optional[str] = None
    assigned_id: Optional[str] = None
    status: Optional[str] = None
