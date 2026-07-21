"""WhatsApp Engine — FastAPI Router.

All endpoints are under /api/whatsapp/* and require admin authentication
(current_team_or_admin dependency — same as all other admin routes).

New MongoDB collections (never modifies existing ones):
  whatsapp_templates
  whatsapp_batches
  whatsapp_jobs
  whatsapp_sessions
  whatsapp_audit_log
  whatsapp_config

Reads (read-only) from:
  db.projects
  db.talents
  db.casting_pipeline
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from core import _now, current_team_or_admin, current_admin, db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/whatsapp", tags=["WhatsApp Engine"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


async def _write_audit(
    event_type: str,
    actor: str,
    *,
    batch_id: Optional[str] = None,
    job_id: Optional[str] = None,
    talent_id: Optional[str] = None,
    talent_name: Optional[str] = None,
    destination: Optional[str] = None,
    destination_type: Optional[str] = None,
    message_preview: Optional[str] = None,
    is_dry_run: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Append an immutable audit log entry."""
    doc = {
        "id": _new_id(),
        "event_type": event_type,
        "batch_id": batch_id,
        "job_id": job_id,
        "talent_id": talent_id,
        "talent_name": talent_name,
        "destination": destination,
        "destination_type": destination_type,
        "message_preview": message_preview[:200] if message_preview else None,
        "is_dry_run": is_dry_run,
        "actor": actor,
        "metadata": metadata or {},
        "timestamp": _utcnow(),
    }
    await db.whatsapp_audit_log.insert_one(doc)


async def _ensure_indexes() -> None:
    """Create MongoDB indexes for all WhatsApp Engine collections.
    Called once at startup via server.py lifespan.
    """
    # whatsapp_jobs — worker poll + per-talent history
    await db.whatsapp_jobs.create_index(
        [("status", 1), ("is_dry_run", 1), ("created_at", 1)],
        name="job_poll_idx",
    )
    await db.whatsapp_jobs.create_index([("batch_id", 1)], name="job_batch_idx")
    await db.whatsapp_jobs.create_index(
        [("talent_id", 1), ("created_at", -1)], name="job_talent_history_idx"
    )

    # whatsapp_batches
    await db.whatsapp_batches.create_index(
        [("project_id", 1), ("created_at", -1)], name="batch_project_idx"
    )
    await db.whatsapp_batches.create_index(
        [("status", 1), ("created_at", -1)], name="batch_status_idx"
    )

    # whatsapp_audit_log
    await db.whatsapp_audit_log.create_index(
        [("timestamp", -1)], name="audit_timestamp_idx"
    )
    await db.whatsapp_audit_log.create_index(
        [("talent_id", 1), ("timestamp", -1)], name="audit_talent_idx"
    )
    await db.whatsapp_audit_log.create_index(
        [("batch_id", 1), ("timestamp", -1)], name="audit_batch_idx"
    )

    # whatsapp_templates
    await db.whatsapp_templates.create_index([("slug", 1)], unique=True, name="template_slug_idx")

    # whatsapp_pins (Slice 3) — one pin per (user, project)
    await db.whatsapp_pins.create_index(
        [("user_id", 1), ("project_id", 1)], unique=True, name="pin_user_project_idx"
    )
    # projects filtered/sorted search (Slice 3)
    await db.projects.create_index(
        [("status", 1), ("created_at", -1)], name="proj_status_created_idx"
    )

    # interactions / unified comm timeline (Slice 4)
    await db.interactions.create_index(
        [("subject_type", 1), ("subject_id", 1), ("created_at", -1)], name="timeline_subject_idx"
    )
    await db.interactions.create_index(
        [("client_id", 1), ("created_at", -1)], name="interactions_client_idx"
    )
    # RC-audit H1: worker upserts the timeline row keyed by job_id on every send.
    await db.interactions.create_index(
        [("job_id", 1)], unique=True, sparse=True, name="interactions_job_idx"
    )

    logger.info("whatsapp: MongoDB indexes ensured")


async def _seed_templates() -> None:
    """Insert default templates if collection is empty."""
    count = await db.whatsapp_templates.count_documents({})
    if count > 0:
        return

    defaults = [
        {
            "name": "Casting Call",
            "slug": "casting_call",
            "body_text": (
                "Hi {{talent_name}} 👋\n\n"
                "We'd love to have you for *{{project_name}}*!\n\n"
                "📅 Shoot Dates: {{shoot_dates}}\n"
                "💰 Budget: {{budget}}\n\n"
                "To proceed, please confirm your availability and submit your details here:\n"
                "{{submission_link}}\n\n"
                "— Team Talentgram 🎬"
            ),
            "variables": ["talent_name", "project_name", "shoot_dates", "budget", "submission_link"],
            "media_type": "none",
            "media_url": None,
            "media_cloudinary_id": None,
            "is_custom": False,
        },
        {
            "name": "Follow Up",
            "slug": "follow_up",
            "body_text": (
                "Hi {{talent_name}} 👋\n\n"
                "Just following up on *{{project_name}}*.\n\n"
                "We haven't heard back yet — are you still interested?\n"
                "{{submission_link}}\n\n"
                "— Team Talentgram 🎬"
            ),
            "variables": ["talent_name", "project_name", "submission_link"],
            "media_type": "none",
            "media_url": None,
            "media_cloudinary_id": None,
            "is_custom": False,
        },
        {
            "name": "Additional Details",
            "slug": "additional_details",
            "body_text": (
                "Hi {{talent_name}} 👋\n\n"
                "Thanks for your interest in *{{project_name}}*!\n\n"
                "We need a few more details from you. Please visit:\n"
                "{{submission_link}}\n\n"
                "— Team Talentgram 🎬"
            ),
            "variables": ["talent_name", "project_name", "submission_link"],
            "media_type": "none",
            "media_url": None,
            "media_cloudinary_id": None,
            "is_custom": False,
        },
        {
            "name": "Shortlisted",
            "slug": "shortlisted",
            "body_text": (
                "Hi {{talent_name}} 🎉\n\n"
                "Great news — you've been *shortlisted* for *{{project_name}}*!\n\n"
                "📅 Shoot Dates: {{shoot_dates}}\n"
                "💰 Budget: {{budget}}\n\n"
                "Please confirm your availability ASAP.\n\n"
                "— Team Talentgram 🎬"
            ),
            "variables": ["talent_name", "project_name", "shoot_dates", "budget"],
            "media_type": "none",
            "media_url": None,
            "media_cloudinary_id": None,
            "is_custom": False,
        },
        {
            "name": "Locked",
            "slug": "locked",
            "body_text": (
                "Hi {{talent_name}} 🔒\n\n"
                "You've been *confirmed and locked* for *{{project_name}}*!\n\n"
                "📅 Shoot Dates: {{shoot_dates}}\n"
                "📍 Location: {{location}}\n"
                "💰 Budget: {{budget}}\n\n"
                "Please keep these dates free. We'll share the full brief shortly.\n\n"
                "— Team Talentgram 🎬"
            ),
            "variables": ["talent_name", "project_name", "shoot_dates", "location", "budget"],
            "media_type": "none",
            "media_url": None,
            "media_cloudinary_id": None,
            "is_custom": False,
        },
        {
            "name": "Thank You",
            "slug": "thank_you",
            "body_text": (
                "Hi {{talent_name}} 🙏\n\n"
                "Thank you for your participation in *{{project_name}}*!\n\n"
                "It was a pleasure working with you. We'll be in touch for future projects.\n\n"
                "— Team Talentgram 🎬"
            ),
            "variables": ["talent_name", "project_name"],
            "media_type": "none",
            "media_url": None,
            "media_cloudinary_id": None,
            "is_custom": False,
        },
        {
            "name": "Custom Message",
            "slug": "custom",
            "body_text": "{{message}}",
            "variables": ["message"],
            "media_type": "none",
            "media_url": None,
            "media_cloudinary_id": None,
            "is_custom": True,
        },
    ]

    now = _utcnow()
    for t in defaults:
        t.update({"id": _new_id(), "created_by": "system", "created_at": now, "updated_at": now})

    await db.whatsapp_templates.insert_many(defaults)
    logger.info("whatsapp: seeded %d default templates", len(defaults))


async def _seed_config() -> None:
    """Insert default config entries if missing."""
    defaults = [
        ("min_delay_sec", "8"),
        ("max_delay_sec", "15"),
        ("max_retries", "3"),
        ("circuit_breaker_threshold", "5"),
    ]
    for key, value in defaults:
        await db.whatsapp_config.update_one(
            {"key": key}, {"$setOnInsert": {"key": key, "value": value}}, upsert=True
        )
    logger.info("whatsapp: config defaults ensured")


async def ensure_whatsapp_ready() -> None:
    """Called from server.py startup. Idempotent."""
    await _ensure_indexes()
    await _seed_templates()
    await _seed_config()


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class TemplateIn(BaseModel):
    name: str
    slug: str
    body_text: str
    variables: List[str] = Field(default_factory=list)
    media_type: str = "none"
    media_url: Optional[str] = None
    media_cloudinary_id: Optional[str] = None
    is_custom: bool = False


class ManualContact(BaseModel):
    name: str = ""
    phone: str


class SourceParams(BaseModel):
    # PROJECT
    project_id: Optional[str] = None
    pipeline_stages: List[str] = Field(default_factory=list)
    # Optional narrowing for PROJECT source — when set, only these talent_ids
    # (still within pipeline_stages) are resolved. Empty/omitted preserves the
    # original "every talent in these stages" behavior untouched. Added for
    # single-talent triggers (e.g. a pipeline card's Follow-up Reminder
    # button) that need the exact same PROJECT routing/template/delivery path
    # without pulling in every other talent in that stage.
    talent_ids: List[str] = Field(default_factory=list)
    # CRM (Marketing)
    contact_type: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    q: Optional[str] = None
    client_ids: List[str] = Field(default_factory=list)
    select_all_filtered: bool = False
    # MANUAL
    contacts: List[ManualContact] = Field(default_factory=list)
    # SAVED LISTS
    contact_list_ids: List[str] = Field(default_factory=list)
    group_list_ids: List[str] = Field(default_factory=list)


class ResolveIn(BaseModel):
    source_type: str  # PROJECT | CRM | MANUAL
    source_params: SourceParams = Field(default_factory=SourceParams)
    excluded_recipient_ids: List[str] = Field(default_factory=list)


class BatchIn(BaseModel):
    # v2 source-typed targeting (Feature 6). Either provide source_type +
    # source_params, OR the legacy project_id + pipeline_stages shortcut below.
    source_type: Optional[str] = None
    source_params: Optional[SourceParams] = None
    excluded_recipient_ids: List[str] = Field(default_factory=list)
    # Legacy project-only shortcut — still accepted (back-compat).
    project_id: Optional[str] = None
    pipeline_stages: List[str] = Field(default_factory=list)
    # Common
    template_id: str
    variable_data: Dict[str, str] = Field(default_factory=dict)
    media_url: Optional[str] = None
    is_dry_run: bool = False
    min_delay_sec: int = 8
    max_delay_sec: int = 15


class ConfigUpdateIn(BaseModel):
    value: str


class BatchActionIn(BaseModel):
    action: str  # "pause" | "resume" | "cancel"


# ---------------------------------------------------------------------------
# ── TEMPLATES ──────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@router.get("/templates")
async def list_templates(admin: dict = Depends(current_team_or_admin)):
    """Return all message templates."""
    docs = await db.whatsapp_templates.find({}, {"_id": 0}).sort("created_at", 1).to_list(200)
    return docs


@router.get("/templates/{template_id}")
async def get_template(template_id: str, admin: dict = Depends(current_team_or_admin)):
    doc = await db.whatsapp_templates.find_one({"id": template_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Template not found")
    return doc


@router.post("/templates", status_code=201)
async def create_template(payload: TemplateIn, admin: dict = Depends(current_team_or_admin)):
    existing = await db.whatsapp_templates.find_one({"slug": payload.slug})
    if existing:
        raise HTTPException(400, f"Template slug '{payload.slug}' already exists")
    doc = payload.model_dump()
    doc.update({
        "id": _new_id(),
        "created_by": admin["id"],
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    })
    await db.whatsapp_templates.insert_one(doc)
    doc.pop("_id", None)
    await _write_audit("template_created", admin["id"],
                       metadata={"template_id": doc["id"], "slug": doc["slug"]})
    return doc


@router.put("/templates/{template_id}")
async def update_template(
    template_id: str,
    payload: TemplateIn,
    admin: dict = Depends(current_team_or_admin),
):
    existing = await db.whatsapp_templates.find_one({"id": template_id})
    if not existing:
        raise HTTPException(404, "Template not found")
    updates = payload.model_dump()
    updates["updated_at"] = _utcnow()
    await db.whatsapp_templates.update_one({"id": template_id}, {"$set": updates})
    await _write_audit("template_edited", admin["id"],
                       metadata={"template_id": template_id})
    return {**{k: v for k, v in existing.items() if k != "_id"}, **updates}


@router.delete("/templates/{template_id}", status_code=204)
async def delete_template(template_id: str, admin: dict = Depends(current_admin)):
    """Only admins (not team) can delete templates."""
    doc = await db.whatsapp_templates.find_one({"id": template_id})
    if not doc:
        raise HTTPException(404, "Template not found")
    if not doc.get("is_custom", True):
        raise HTTPException(400, "Cannot delete built-in templates")
    await db.whatsapp_templates.delete_one({"id": template_id})


# Catalog powering the editor's "Available Variables" panel (Part 5). Each entry
# is click-to-insert. `auto_resolved` tells the editor which of these are filled
# in automatically so they can be hidden from the "Inject Custom Variables" panel
# (Part 2). Anything NOT listed in auto_resolved (e.g. instagram, location) stays
# a manual input across every source.
VARIABLE_CATALOG = [
    {"category": "Talent", "variables": [
        {"key": "first_name", "label": "First name"},
        {"key": "full_name", "label": "Full name"},
        {"key": "talent_name", "label": "Full name (legacy alias)"},
        {"key": "phone", "label": "Phone"},
        {"key": "instagram", "label": "Instagram"},
    ]},
    {"category": "Project", "variables": [
        {"key": "project_name", "label": "Project name"},
        {"key": "shoot_dates", "label": "Shoot dates"},
        {"key": "budget", "label": "Budget"},
        {"key": "location", "label": "Location"},
        {"key": "submission_link", "label": "Submission link"},
    ]},
    {"category": "Sender", "variables": [
        {"key": "sender_name", "label": "Sender name"},
        {"key": "sender_email", "label": "Sender email"},
    ]},
    {"category": "System", "variables": [
        {"key": "current_date", "label": "Current date"},
        {"key": "current_time", "label": "Current time"},
    ]},
]


@router.get("/variables")
async def list_variables(admin: dict = Depends(current_team_or_admin)):
    """Variable catalog for the template editor + the set the backend resolves
    automatically (so the UI knows which to hide from Inject Custom Variables)."""
    return {
        "catalog": VARIABLE_CATALOG,
        "auto_resolved": {
            # Always auto-resolved regardless of source.
            "always": AUTO_RECIPIENT_VARS + AUTO_SENDER_VARS + AUTO_SYSTEM_VARS,
            # Additionally auto-resolved only when the source is Project Pipeline.
            "project_source": AUTO_PROJECT_VARS,
        },
    }


# ---------------------------------------------------------------------------
# ── PROJECT / PIPELINE HELPERS ─────────────────────────────────────────────
# ---------------------------------------------------------------------------

@router.get("/projects")
async def list_projects_for_wa(admin: dict = Depends(current_team_or_admin)):
    """Thin wrapper — returns same project list as /api/projects."""
    docs = await db.projects.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    return docs


@router.get("/projects/{project_id}/pipeline-summary")
async def get_pipeline_summary(
    project_id: str,
    admin: dict = Depends(current_team_or_admin),
):
    """Return talent counts per pipeline stage for a given project.
    Used by the WhatsApp Engine stage selector.
    """
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    if not project:
        raise HTTPException(404, "Project not found")

    pipeline_rows = await db.casting_pipeline.find(
        {"project_id": project_id}, {"_id": 0, "talent_id": 1, "stage": 1}
    ).to_list(5000)

    stage_counts: Dict[str, int] = {}
    for row in pipeline_rows:
        s = row.get("stage", "unknown")
        stage_counts[s] = stage_counts.get(s, 0) + 1

    return {
        "project_id": project_id,
        "project_name": project.get("brand_name", ""),
        "stage_counts": stage_counts,
        "total": len(pipeline_rows),
    }


# ── PROJECT PICKER (Feature 4) — server-side search / recent / pins ──────────
def _project_card(p: dict) -> dict:
    return {
        "id": p.get("id"),
        "name": p.get("brand_name", ""),
        "brand_name": p.get("brand_name", ""),
        "status": p.get("status"),
        "slug": p.get("slug"),
        "created_at": p.get("created_at"),
    }


@router.get("/projects/search")
async def search_projects(
    q: Optional[str] = None,
    status: Optional[str] = None,
    offset: int = 0,
    limit: int = 30,
    admin: dict = Depends(current_team_or_admin),
):
    """Paginated, server-side project search (name/brand + status). Replaces the
    full-list dropdown so the picker scales to hundreds of projects."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status
    if q:
        rgx = {"$regex": _re.escape(q), "$options": "i"}
        query["$or"] = [{"brand_name": rgx}, {"slug": rgx}]
    total = await db.projects.count_documents(query)
    docs = await db.projects.find(query, {"_id": 0}).sort("created_at", -1) \
        .skip(offset).limit(limit).to_list(limit)
    return {
        "items": [_project_card(p) for p in docs],
        "total": total, "offset": offset, "limit": limit,
        "next_offset": (offset + limit) if (offset + limit) < total else None,
    }


@router.get("/projects/recent")
async def recent_projects(limit: int = 10, admin: dict = Depends(current_team_or_admin)):
    """Most recently created projects (quick-pick section)."""
    limit = max(1, min(limit, 50))
    docs = await db.projects.find({}, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(limit)
    return {"items": [_project_card(p) for p in docs]}


@router.get("/projects/pins")
async def list_pinned_projects(admin: dict = Depends(current_team_or_admin)):
    """Projects pinned by the current admin, hydrated with project data."""
    pins = await db.whatsapp_pins.find(
        {"user_id": admin["id"]}, {"_id": 0, "project_id": 1}
    ).sort("created_at", -1).to_list(200)
    pids = [p["project_id"] for p in pins]
    if not pids:
        return {"items": []}
    pmap = {p["id"]: p for p in await db.projects.find({"id": {"$in": pids}}, {"_id": 0}).to_list(200)}
    return {"items": [_project_card(pmap[pid]) for pid in pids if pid in pmap]}


@router.post("/projects/pins/{project_id}", status_code=201)
async def pin_project(project_id: str, admin: dict = Depends(current_team_or_admin)):
    if not await db.projects.find_one({"id": project_id}, {"_id": 0, "id": 1}):
        raise HTTPException(404, "Project not found")
    await db.whatsapp_pins.update_one(
        {"user_id": admin["id"], "project_id": project_id},
        {"$setOnInsert": {"created_at": _utcnow()}},
        upsert=True,
    )
    return {"pinned": True, "project_id": project_id}


@router.delete("/projects/pins/{project_id}", status_code=204)
async def unpin_project(project_id: str, admin: dict = Depends(current_team_or_admin)):
    await db.whatsapp_pins.delete_one({"user_id": admin["id"], "project_id": project_id})


@router.get("/projects/{project_id}/resolve-recipients")
async def resolve_recipients(
    project_id: str,
    stages: str,  # comma-separated stage names
    admin: dict = Depends(current_team_or_admin),
):
    """Resolve the full recipient list for given stages.
    Returns per-talent destination_type and destination for preview.
    """
    stage_list = [s.strip() for s in stages.split(",") if s.strip()]
    if not stage_list:
        raise HTTPException(400, "At least one stage required")

    pipeline_rows = await db.casting_pipeline.find(
        {"project_id": project_id, "stage": {"$in": stage_list}},
        {"_id": 0, "talent_id": 1, "stage": 1},
    ).to_list(5000)

    if not pipeline_rows:
        return {"recipients": [], "total": 0, "unresolvable": []}

    talent_ids = list({r["talent_id"] for r in pipeline_rows})
    talents = await db.talents.find(
        {"id": {"$in": talent_ids}},
        {"_id": 0, "id": 1, "name": 1, "phone": 1, "whatsapp_group_name": 1},
    ).to_list(5000)
    talent_map = {t["id"]: t for t in talents}

    recipients = []
    unresolvable = []

    seen_talent_ids = set()
    for row in pipeline_rows:
        tid = row["talent_id"]
        if tid in seen_talent_ids:
            continue
        seen_talent_ids.add(tid)

        talent = talent_map.get(tid)
        if not talent:
            unresolvable.append({"talent_id": tid, "reason": "Talent record not found"})
            continue

        name = talent.get("name", "")
        group_name = (talent.get("whatsapp_group_name") or "").strip()
        phone = (talent.get("phone") or "").strip()
        dest_type, destination, reason = _resolve_destination(talent)
        _log_routing_decision(name, phone, group_name, dest_type, destination)

        if dest_type:
            recipients.append({
                "talent_id": tid,
                "talent_name": name,
                "phone": phone,                       # FEATURE 4: shown on resolve screen
                "whatsapp_group_name": group_name,    # FEATURE 4: shown on resolve screen
                "destination_type": dest_type,
                "destination": destination,
                "stage": row["stage"],
            })
        else:
            unresolvable.append({
                "talent_id": tid,
                "talent_name": name,
                "phone": phone,
                "whatsapp_group_name": group_name,
                "reason": reason,
            })

    return {
        "recipients": recipients,
        "total": len(recipients),
        "unresolvable": unresolvable,
    }


# ---------------------------------------------------------------------------
# ── BATCHES ────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _resolve_destination(talent: dict) -> Tuple[str, Optional[str], Optional[str]]:
    """FEATURE 2 — single source of routing truth (the talent record decides).

    Group if `whatsapp_group_name` is set and non-empty, else phone. Returns
    (destination_type, destination, reason_if_unresolvable). destination_type is
    the internal value ('group' | 'number'); display is mapped to GROUP/PHONE.
    """
    group = (talent.get("whatsapp_group_name") or "").strip()
    phone = (talent.get("phone") or "").strip()
    if group:
        return "group", group, None
    if phone:
        return "number", phone, None
    return "", None, "No whatsapp_group_name and no phone number"


def _log_routing_decision(name: str, phone: str, group: str, dest_type: str, dest: Optional[str]) -> None:
    """FEATURE 3 — audit the routing decision before every send/resolution."""
    display = "GROUP" if dest_type == "group" else ("PHONE" if dest_type == "number" else "UNRESOLVABLE")
    logger.info(
        "WHATSAPP ROUTING DECISION | Talent: %s | Phone: %s | WhatsApp Group: %s | "
        "Resolved Destination Type: %s | Resolved Destination: %s",
        name or "", phone or "", group or "", display, dest or "",
    )


def _render_message(template_body: str, data: Dict[str, Any]) -> str:
    """Substitute {{key}} placeholders from `data`.

    Unknown placeholders are intentionally left untouched (back-compat — existing
    templates with manually-filled variables behave exactly as before). `None`
    values render as an empty string so an unset auto-variable doesn't leak a raw
    placeholder into the message.
    """
    result = template_body
    for key, value in data.items():
        result = result.replace("{{" + key + "}}", "" if value is None else str(value))
    return result


def _first_name(full_name: Optional[str]) -> str:
    """First word of a stored name. 'Sahal Mansuri' -> 'Sahal'."""
    parts = (full_name or "").strip().split()
    return parts[0] if parts else ""


def _recipient_variables(name: str, phone: str) -> Dict[str, str]:
    """Per-recipient placeholders. `talent_name` is kept as the canonical alias
    every existing template relies on; `full_name` is the new equivalent.

    `talent_name` now resolves to the recipient's FIRST NAME ONLY — every
    template greets with "Hi {{talent_name}}", so this is the single place
    that changes the sender-facing greeting for every campaign source
    (PROJECT/CRM/MANUAL/SAVED_LISTS) and both preview and live send, since
    they all render through this same function. `full_name` is left as the
    actual full name — its name promises that, and no template currently
    uses it, so nothing regresses by keeping it intact."""
    name = name or ""
    return {
        "talent_name": _first_name(name),   # back-compat placeholder — first name only
        "full_name": name,                  # alias of the real full name (unchanged)
        "first_name": _first_name(name),
        "phone": phone or "",
    }


def _sender_variables(admin: Dict[str, Any]) -> Dict[str, str]:
    """Sender placeholders — the authenticated admin running the campaign."""
    return {
        "sender_name": (admin.get("name") or "").strip(),
        "sender_email": (admin.get("email") or "").strip(),
    }


def _system_variables() -> Dict[str, str]:
    """Date/time placeholders, rendered in a human-friendly format."""
    now = datetime.now(timezone.utc)
    return {
        "current_date": now.strftime("%d %b %Y"),
        "current_time": now.strftime("%I:%M %p").lstrip("0"),
    }


def _project_variables(project: Dict[str, Any]) -> Dict[str, str]:
    """Auto-resolved project placeholders (Part 2). Derived from the project
    document so the admin never types them for a Project Pipeline campaign."""
    slug = (project.get("slug") or "").strip()
    budget = (project.get("budget_per_day") or "").strip()
    if not budget:
        # Fall back to the structured talent-facing budget list, if present.
        entries = project.get("talent_budget") or []
        budget = ", ".join(
            (e.get("value") or "").strip()
            for e in entries
            if isinstance(e, dict) and (e.get("value") or "").strip()
        )
    return {
        "project_name": (project.get("brand_name") or "").strip(),
        "shoot_dates": (project.get("shoot_dates") or "").strip(),
        "budget": budget,
        "submission_link": f"https://submit.talentgramagency.com/submit/{slug}" if slug else "",
    }


# Variables the backend resolves automatically — the editor hides these from the
# "Inject Custom Variables" panel so the admin is only asked for what cannot be
# derived. Exposed via GET /whatsapp/variables for the frontend.
AUTO_RECIPIENT_VARS = ["talent_name", "full_name", "first_name", "phone"]
AUTO_SENDER_VARS = ["sender_name", "sender_email"]
AUTO_SYSTEM_VARS = ["current_date", "current_time"]
AUTO_PROJECT_VARS = ["project_name", "shoot_dates", "budget", "submission_link"]


# ===========================================================================
# FEATURE 6 — UNIFIED RECIPIENT RESOLUTION ENGINE
# One engine for PROJECT | CRM | MANUAL. Output is source-agnostic, so the
# worker never needs to know where a recipient came from.
#   { name, phone, whatsapp_group_name, destination_type, destination,
#     source, source_id, recipient_kind, recipient_id }
# ===========================================================================
import re as _re  # noqa: E402

_PHONE_RE = _re.compile(r"^\+?\d{7,15}$")


def _normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Strip formatting, keep an optional leading +. Returns an E.164-ish string
    or None if it cannot be a valid international number (Feature 5 validation)."""
    if not raw:
        return None
    s = str(raw).strip()
    plus = s.startswith("+")
    digits = _re.sub(r"\D", "", s)
    if not digits:
        return None
    cand = ("+" + digits) if plus else digits
    return cand if _PHONE_RE.match(cand) else None


def _make_recipient(*, name, phone, group_name, source, source_id, kind, recipient_id):
    """Route a raw contact and return (dest_type, destination, reason, recipient)."""
    dest_type, destination, reason = _resolve_destination(
        {"name": name, "phone": phone, "whatsapp_group_name": group_name}
    )
    rec = {
        "name": name or "",
        "phone": phone or "",
        "whatsapp_group_name": group_name or "",
        "destination_type": dest_type,
        "destination": destination,
        "source": source,
        "source_id": source_id,
        "recipient_kind": kind,
        "recipient_id": recipient_id,
    }
    return dest_type, destination, reason, rec


async def resolve_recipients_engine(source_type: str, params: "SourceParams",
                                    excluded_ids=None) -> dict:
    """Resolve recipients for PROJECT | CRM | MANUAL into the unified shape.
    Returns {recipients[], unresolvable[], counts{resolved, sending, excluded}}.
    Dedups by recipient_id; applies excluded_recipient_ids."""
    excluded = set(excluded_ids or [])
    recipients: List[dict] = []
    unresolvable: List[dict] = []
    seen: set = set()

    def _add(dest_type, destination, reason, rec):
        rid = rec["recipient_id"]
        if rid in seen:
            return
        seen.add(rid)
        if not dest_type:
            unresolvable.append({
                "name": rec["name"], "phone": rec["phone"],
                "recipient_id": rid, "source": rec["source"],
                "reason": reason or "Unresolvable",
            })
            return
        _log_routing_decision(rec["name"], rec["phone"], rec["whatsapp_group_name"],
                              dest_type, destination)
        recipients.append(rec)

    if source_type == "PROJECT":
        if not params.project_id:
            raise HTTPException(400, "project_id required for PROJECT source")
        pipeline_query = {"project_id": params.project_id, "stage": {"$in": params.pipeline_stages}}
        if params.talent_ids:
            pipeline_query["talent_id"] = {"$in": params.talent_ids}
        rows = await db.casting_pipeline.find(
            pipeline_query,
            {"_id": 0, "talent_id": 1},
        ).to_list(5000)
        tids = list({r["talent_id"] for r in rows})
        tmap = {t["id"]: t for t in await db.talents.find(
            {"id": {"$in": tids}},
            {"_id": 0, "id": 1, "name": 1, "phone": 1, "whatsapp_group_name": 1},
        ).to_list(5000)}
        for tid in tids:
            t = tmap.get(tid)
            if not t:
                continue
            _add(*_make_recipient(
                name=t.get("name", ""), phone=(t.get("phone") or "").strip(),
                group_name=(t.get("whatsapp_group_name") or "").strip(),
                source="PROJECT", source_id=params.project_id,
                kind="TALENT", recipient_id=tid))

    elif source_type == "CRM":
        from bson import ObjectId
        query: Dict[str, Any] = {"archived": {"$ne": True}, "deleted": {"$ne": True}}
        if params.contact_type:
            query["contact_type"] = params.contact_type
        if params.tags:
            query["tags"] = {"$in": params.tags}
        if params.q:
            rgx = {"$regex": _re.escape(params.q), "$options": "i"}
            query["$or"] = [{"name": rgx}, {"company_name": rgx}]
        if params.client_ids and not params.select_all_filtered:
            oids = []
            for cid in params.client_ids:
                try:
                    oids.append(ObjectId(cid))
                except Exception:
                    pass
            query["_id"] = {"$in": oids}
        clients = await db.clients.find(
            query, {"name": 1, "phone_number": 1, "contact_type": 1}
        ).to_list(10000)
        for c in clients:
            cid = str(c["_id"])
            phone = _normalize_phone(c.get("phone_number"))
            _add(*_make_recipient(
                name=c.get("name", ""), phone=phone or "", group_name="",
                source="CRM", source_id=(params.contact_type or "all"),
                kind="CRM_CLIENT", recipient_id=cid))

    elif source_type == "MANUAL":
        for mc in params.contacts:
            phone = _normalize_phone(mc.phone)
            rid = "manual:" + (phone or (mc.phone or "").strip().lower())
            if rid in seen:
                continue
            if not phone:
                seen.add(rid)
                unresolvable.append({
                    "name": mc.name or "", "phone": mc.phone or "",
                    "recipient_id": rid, "source": "MANUAL",
                    "reason": "Invalid phone number",
                })
                continue
            _add(*_make_recipient(
                name=mc.name or "", phone=phone, group_name="",
                source="MANUAL", source_id=None, kind="MANUAL", recipient_id=rid))
    elif source_type == "SAVED_LISTS":
        if not params.contact_list_ids and not params.group_list_ids:
            raise HTTPException(400, "contact_list_ids or group_list_ids required for SAVED_LISTS source")
        lists = await db.whatsapp_contact_lists.find(
            {"id": {"$in": params.contact_list_ids}, "deleted": {"$ne": True}},
            {"_id": 0, "id": 1, "name": 1, "contacts": 1}
        ).to_list(1000)
        _cl_order = {lid: i for i, lid in enumerate(params.contact_list_ids)}
        lists.sort(key=lambda d: _cl_order.get(d["id"], len(_cl_order)))
        for lst in lists:
            list_id = lst["id"]
            for c in lst.get("contacts") or []:
                raw_phone = c.get("phone")
                phone = _normalize_phone(raw_phone)
                rid = "saved_list:" + (phone or (raw_phone or "").strip().lower())
                if rid in seen:
                    continue
                if not phone:
                    seen.add(rid)
                    unresolvable.append({
                        "name": c.get("name") or "", "phone": raw_phone or "",
                        "recipient_id": rid, "source": "SAVED_LISTS",
                        "reason": "Invalid phone number",
                    })
                    continue
                _add(*_make_recipient(
                    name=c.get("name") or "", phone=phone, group_name="",
                    source="SAVED_LISTS", source_id=list_id, kind="SAVED_LIST", recipient_id=rid))
        if params.group_list_ids:
            glists = await db.whatsapp_group_lists.find(
                {"id": {"$in": params.group_list_ids}, "deleted": {"$ne": True}},
                {"_id": 0, "id": 1, "name": 1, "groups": 1}
            ).to_list(1000)
            _gl_order = {lid: i for i, lid in enumerate(params.group_list_ids)}
            glists.sort(key=lambda d: _gl_order.get(d["id"], len(_gl_order)))
            for glst in glists:
                glist_id = glst["id"]
                for g in glst.get("groups") or []:
                    gname = (g.get("group_name") or "").strip()
                    if not gname:
                        continue
                    rid = "saved_group:" + gname.casefold()
                    if rid in seen:
                        continue
                    _add(*_make_recipient(
                        name="", phone="", group_name=gname,
                        source="SAVED_LISTS", source_id=glist_id,
                        kind="SAVED_GROUP", recipient_id=rid))
    else:
        raise HTTPException(400, f"Unknown source_type {source_type!r}")

    before = len(recipients)
    recipients = [r for r in recipients if r["recipient_id"] not in excluded]
    return {
        "recipients": recipients,
        "unresolvable": unresolvable,
        "counts": {
            "resolved": before,
            "sending": len(recipients),
            "excluded": before - len(recipients),
        },
    }


def _batch_source(payload: "BatchIn"):
    """Resolve the effective (source_type, SourceParams) from a v2 batch payload,
    falling back to the legacy project_id + pipeline_stages shortcut."""
    if payload.source_type:
        return payload.source_type, (payload.source_params or SourceParams())
    if payload.project_id:
        return "PROJECT", SourceParams(project_id=payload.project_id,
                                       pipeline_stages=payload.pipeline_stages)
    raise HTTPException(400, "Provide source_type+source_params or project_id+pipeline_stages")


@router.post("/resolve")
async def resolve_targets(payload: ResolveIn, admin: dict = Depends(current_team_or_admin)):
    """FEATURE 6 — unified recipient preview for PROJECT | CRM | MANUAL.
    Returns normalized recipients + counts for the resolve/exclusion screen."""
    return await resolve_recipients_engine(
        payload.source_type, payload.source_params, payload.excluded_recipient_ids
    )


# ── COMMUNICATION TIMELINE (Feature 2) — polymorphic, one timeline ──────────
@router.get("/timeline")
async def get_timeline(
    subject_type: str,     # TALENT | CRM_CLIENT | MANUAL | CLIENT
    subject_id: str,
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(current_team_or_admin),
):
    """Unified communication timeline for any subject (talent / CRM contact /
    future client). Reads the (extended) interactions collection. Back-compatible
    with legacy CRM rows that only carry client_id."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    clauses: List[dict] = [{"subject_type": subject_type, "subject_id": subject_id}]
    if subject_type == "CRM_CLIENT":
        # Legacy interactions predate subject_type — match on client_id too.
        try:
            from bson import ObjectId
            clauses.append({"client_id": ObjectId(subject_id)})
        except Exception:
            pass
    query = clauses[0] if len(clauses) == 1 else {"$or": clauses}
    total = await db.interactions.count_documents(query)
    docs = await db.interactions.find(query).sort("created_at", -1) \
        .skip(offset).limit(limit).to_list(limit)
    items = [{
        "id": str(d.get("_id")),
        "subject_type": d.get("subject_type") or ("CRM_CLIENT" if d.get("client_id") else None),
        "subject_id": d.get("subject_id") or (str(d["client_id"]) if d.get("client_id") else None),
        "type": d.get("type"),
        "channel": d.get("channel") or d.get("type"),
        "direction": d.get("direction"),
        "template_name": d.get("template_name"),
        "status": d.get("status"),
        "preview": d.get("preview") or d.get("notes"),
        "batch_id": d.get("batch_id"),
        "created_at": d.get("created_at"),
    } for d in docs]
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "next_offset": (offset + limit) if (offset + limit) < total else None}


# ── CRM SOURCE (Feature 1) — server-side filtered + paginated ───────────────
@router.get("/crm/contact-types")
async def crm_contact_types(admin: dict = Depends(current_team_or_admin)):
    """Distinct contact_type values for the CRM filter dropdown."""
    types = await db.clients.distinct(
        "contact_type", {"archived": {"$ne": True}, "deleted": {"$ne": True}}
    )
    return {"contact_types": sorted([t for t in types if t])}


@router.get("/crm/contacts")
async def crm_contacts(
    contact_type: Optional[str] = None,
    tags: Optional[str] = None,   # comma-separated
    q: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    admin: dict = Depends(current_team_or_admin),
):
    """Paginated CRM contacts for the WhatsApp target picker. Server-side
    filtered by contact_type / tags / text — never returns the full collection."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    query: Dict[str, Any] = {"archived": {"$ne": True}, "deleted": {"$ne": True}}
    if contact_type:
        query["contact_type"] = contact_type
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            query["tags"] = {"$in": tag_list}
    if q:
        rgx = {"$regex": _re.escape(q), "$options": "i"}
        query["$or"] = [{"name": rgx}, {"company_name": rgx}]
    total = await db.clients.count_documents(query)
    docs = await db.clients.find(
        query, {"name": 1, "company_name": 1, "phone_number": 1, "contact_type": 1, "tags": 1}
    ).sort("last_contacted_date", -1).skip(offset).limit(limit).to_list(limit)
    items = [{
        "id": str(d["_id"]),
        "name": d.get("name"),
        "company_name": d.get("company_name"),
        "phone": _normalize_phone(d.get("phone_number")),
        "phone_raw": d.get("phone_number"),
        "contact_type": d.get("contact_type"),
        "tags": d.get("tags") or [],
    } for d in docs]
    return {
        "items": items, "total": total, "offset": offset, "limit": limit,
        "next_offset": (offset + limit) if (offset + limit) < total else None,
    }


# ── MANUAL CONTACTS (Feature 5) — validate before send ──────────────────────
class ManualValidateIn(BaseModel):
    contacts: List[ManualContact] = Field(default_factory=list)


@router.post("/manual/validate")
async def manual_validate(payload: ManualValidateIn, admin: dict = Depends(current_team_or_admin)):
    """Validate manual contacts: phone + country code, dedup. Returns a preview."""
    valid: List[dict] = []
    invalid: List[dict] = []
    duplicates: List[dict] = []
    seen: Dict[str, str] = {}
    for mc in payload.contacts:
        norm = _normalize_phone(mc.phone)
        if not norm:
            invalid.append({"name": mc.name or "", "phone": mc.phone or "",
                            "reason": "Invalid phone / missing country code"})
            continue
        if norm in seen:
            duplicates.append({"name": mc.name or "", "phone": norm})
            continue
        seen[norm] = mc.name or ""
        valid.append({"name": mc.name or "", "phone": norm, "destination_type": "number"})
    return {
        "valid": valid, "invalid": invalid, "duplicates": duplicates,
        "counts": {"valid": len(valid), "invalid": len(invalid), "duplicates": len(duplicates)},
    }


@router.post("/batches", status_code=201)
async def create_batch(payload: BatchIn, admin: dict = Depends(current_team_or_admin)):
    """Create a batch (dry-run or live).

    Dry-run batches resolve recipients + render messages but do NOT send.
    Live batches create pending jobs that the worker will pick up.
    """
    # Validate template
    template = await db.whatsapp_templates.find_one({"id": payload.template_id}, {"_id": 0})
    if not template:
        raise HTTPException(404, "Template not found")
    _raw = template.get("body_text", "")
    logger.info("whatsapp: RAW TEMPLATE id=%s (len=%d): %r", payload.template_id, len(_raw), _raw)

    # FEATURE 6: resolve recipients via the unified engine (PROJECT|CRM|MANUAL).
    source_type, params = _batch_source(payload)
    resolved = await resolve_recipients_engine(
        source_type, params, payload.excluded_recipient_ids
    )
    rec_list = resolved["recipients"]
    skipped = resolved["unresolvable"]
    if not rec_list:
        raise HTTPException(400, f"No sendable recipients found ({len(skipped)} unresolvable).")

    # Human-readable source label for campaign history (Feature 7).
    source_label = ""
    # Resolved variable context shared by every recipient in this batch.
    # Precedence (lowest → highest): manual values typed by the admin, then
    # auto-resolved project vars (only for a Project source — Part 2), then
    # sender (Part 4) and system vars. Per-recipient vars are layered in the loop.
    base_vars: Dict[str, str] = {
        k: v for k, v in (payload.variable_data or {}).items() if v is not None
    }
    if source_type == "PROJECT" and params.project_id:
        proj = await db.projects.find_one({"id": params.project_id}, {"_id": 0})
        if proj:
            source_label = proj.get("brand_name", "") or ""
            base_vars.update(_project_variables(proj))
    elif source_type == "CRM":
        source_label = params.contact_type or "All CRM contacts"
    elif source_type == "MANUAL":
        source_label = f"{len(rec_list)} manual contact(s)"
    base_vars.update(_sender_variables(admin))
    base_vars.update(_system_variables())

    job_status = "dry_run_preview" if payload.is_dry_run else "pending"
    now = _utcnow()
    batch_id = _new_id()
    jobs = []
    for rec in rec_list:
        rec_vars = {**base_vars, **_recipient_variables(rec["name"], rec.get("phone", ""))}
        message_body = _render_message(template["body_text"], rec_vars)
        logger.info("whatsapp: COMPILED MESSAGE recipient=%r (len=%d): %r",
                    rec["name"], len(message_body), message_body)
        jobs.append({
            "id": _new_id(),
            "batch_id": batch_id,
            # Template ref for the comm timeline (Slice 4).
            "template_id": payload.template_id,
            "template_name": template.get("name") or template.get("slug") or "",
            # Source attribution (Feature 6/7). talent_id kept for back-compat.
            "source": rec["source"],
            "source_id": rec["source_id"],
            "recipient_kind": rec["recipient_kind"],
            "recipient_id": rec["recipient_id"],
            "talent_id": rec["recipient_id"] if rec["recipient_kind"] == "TALENT" else None,
            "talent_name": rec["name"],
            "destination_type": rec["destination_type"],
            "destination": rec["destination"],
            "message_body": message_body,
            "media_url": payload.media_url,
            "is_dry_run": payload.is_dry_run,
            "status": job_status,
            "attempt_count": 0,
            "last_attempted_at": None,
            "sent_at": None,
            "error_message": None,
            "worker_picked_at": None,
            "created_at": now,
        })

    # Create batch document
    batch_doc = {
        "id": batch_id,
        "source_type": source_type,          # Feature 7
        "source_label": source_label,
        # Legacy fields retained for existing UI/back-compat.
        "project_id": params.project_id,
        "project_name": source_label if source_type == "PROJECT" else "",
        "template_id": payload.template_id,
        "template_name": template.get("name") or template.get("slug") or "",
        "template_slug": template.get("slug", ""),
        "pipeline_stages": params.pipeline_stages,
        "variable_data": payload.variable_data,
        "media_url": payload.media_url,
        "is_dry_run": payload.is_dry_run,
        "status": "dry_run_complete" if payload.is_dry_run else "pending",
        "min_delay_sec": payload.min_delay_sec,
        "max_delay_sec": payload.max_delay_sec,
        "total_jobs": len(jobs),
        "sent_count": 0,
        "verified_count": 0,
        "failed_count": 0,
        # Circuit-breaker window start: the breaker only counts consecutive
        # failures with last_attempted_at >= this. Resume re-stamps it so a
        # resumed batch gets a fresh window (breaker stays enabled, history kept).
        "breaker_window_start": now,
        "pause_reason": None,
        "excluded_count": resolved["counts"]["excluded"],
        "skipped_recipients": skipped,
        "created_by": admin["id"],
        "created_at": now,
        "started_at": None,
        "completed_at": None,
    }

    await db.whatsapp_batches.insert_one(batch_doc)
    if jobs:
        await db.whatsapp_jobs.insert_many(jobs)

    batch_doc.pop("_id", None)

    await _write_audit(
        "dry_run_previewed" if payload.is_dry_run else "batch_created",
        admin["id"],
        batch_id=batch_id,
        is_dry_run=payload.is_dry_run,
        metadata={
            "project_id": payload.project_id,
            "template_slug": template.get("slug"),
            "total_jobs": len(jobs),
            "skipped": len(skipped),
        },
    )

    return {
        "batch": batch_doc,
        "jobs": [
            {k: v for k, v in j.items() if k != "_id"}
            for j in jobs
        ],
        "skipped": skipped,
    }


@router.get("/batches")
async def list_batches(
    project_id: Optional[str] = None,
    limit: int = 50,
    admin: dict = Depends(current_team_or_admin),
):
    filt: Dict[str, Any] = {}
    if project_id:
        filt["project_id"] = project_id
    docs = (
        await db.whatsapp_batches.find(filt, {"_id": 0})
        .sort("created_at", -1)
        .limit(limit)
        .to_list(limit)
    )
    return docs


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: str, admin: dict = Depends(current_team_or_admin)):
    doc = await db.whatsapp_batches.find_one({"id": batch_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Batch not found")
    return doc


@router.post("/batches/{batch_id}/action")
async def batch_action(
    batch_id: str,
    payload: BatchActionIn,
    admin: dict = Depends(current_team_or_admin),
):
    """Pause, resume, or cancel a running batch."""
    batch = await db.whatsapp_batches.find_one({"id": batch_id})
    if not batch:
        raise HTTPException(404, "Batch not found")

    action = payload.action
    current_status = batch.get("status")
    extra_set: Dict[str, Any] = {}

    if action == "pause":
        if current_status != "running":
            raise HTTPException(400, f"Cannot pause a batch with status '{current_status}'")
        new_status = "paused"
        extra_set["pause_reason"] = "Paused by admin"
        extra_set["paused_at"] = _utcnow()
    elif action == "resume":
        if current_status != "paused":
            raise HTTPException(400, f"Cannot resume a batch with status '{current_status}'")
        new_status = "running"
        # Fresh circuit-breaker window so the pre-pause failures no longer count.
        # History is preserved; the breaker still fires after N NEW failures.
        extra_set["breaker_window_start"] = _utcnow()
        extra_set["pause_reason"] = None
        extra_set["resumed_at"] = _utcnow()
    elif action == "cancel":
        if current_status in ("completed", "failed"):
            raise HTTPException(400, "Batch is already finished")
        new_status = "failed"
        # Mark remaining pending/sending jobs as skipped
        await db.whatsapp_jobs.update_many(
            {"batch_id": batch_id, "status": {"$in": ["pending", "sending"]}},
            {"$set": {"status": "skipped", "error_message": "Batch cancelled by admin"}},
        )
    else:
        raise HTTPException(400, f"Unknown action '{action}'")

    await db.whatsapp_batches.update_one(
        {"id": batch_id}, {"$set": {"status": new_status, **extra_set}}
    )

    await _write_audit(
        f"batch_{action}d",
        admin["id"],
        batch_id=batch_id,
        metadata={"previous_status": current_status, "new_status": new_status},
    )

    return {"batch_id": batch_id, "status": new_status}


# ---------------------------------------------------------------------------
# ── JOBS (delivery tracking) ───────────────────────────────────────────────
# ---------------------------------------------------------------------------

@router.get("/batches/{batch_id}/jobs")
async def list_jobs(
    batch_id: str,
    status_filter: Optional[str] = None,
    admin: dict = Depends(current_team_or_admin),
):
    filt: Dict[str, Any] = {"batch_id": batch_id}
    if status_filter:
        filt["status"] = status_filter
    docs = (
        await db.whatsapp_jobs.find(filt, {"_id": 0})
        .sort("created_at", 1)
        .to_list(5000)
    )
    return docs


@router.post("/batches/{batch_id}/jobs/{job_id}/retry")
async def retry_job(
    batch_id: str,
    job_id: str,
    admin: dict = Depends(current_team_or_admin),
):
    """Manually re-queue a failed job."""
    job = await db.whatsapp_jobs.find_one({"id": job_id, "batch_id": batch_id})
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") not in ("failed", "skipped"):
        raise HTTPException(400, "Only failed or skipped jobs can be retried")

    await db.whatsapp_jobs.update_one(
        {"id": job_id},
        {"$set": {"status": "pending", "error_message": None, "worker_picked_at": None}},
    )

    # Ensure batch is in running state if it was paused/failed/completed —
    # completed is included because a batch reaches it once every job hits a
    # terminal state (sent/failed), even if some failed; without resuming it
    # here, poll_and_process_jobs' batch query (status in [running, pending])
    # never looks at this batch again, so the retried job sits in "pending"
    # forever and is never picked up.
    batch = await db.whatsapp_batches.find_one({"id": batch_id})
    if batch and batch.get("status") in ("failed", "paused", "completed"):
        await db.whatsapp_batches.update_one(
            {"id": batch_id},
            {"$set": {"status": "running"}, "$unset": {"completed_at": ""}},
        )

    await _write_audit(
        "job_retried",
        admin["id"],
        batch_id=batch_id,
        job_id=job_id,
        talent_id=job.get("talent_id"),
        talent_name=job.get("talent_name"),
    )

    return {"job_id": job_id, "status": "pending"}


# ---------------------------------------------------------------------------
# ── SESSION ────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@router.get("/session")
async def get_session_status(admin: dict = Depends(current_team_or_admin)):
    """Returns current WhatsApp session state (QR, authenticated, error)."""
    doc = await db.whatsapp_sessions.find_one({"id": "default"}, {"_id": 0})
    if not doc:
        return {
            "id": "default",
            "status": "disconnected",
            "qr_code_base64": None,
            "last_heartbeat": None,
            "authenticated_at": None,
            "error_message": None,
        }

    # Clear QR if expired
    qr_expires = doc.get("qr_expires_at")
    if qr_expires and doc.get("status") == "qr_pending":
        try:
            exp_dt = datetime.fromisoformat(qr_expires)
            if exp_dt < datetime.now(timezone.utc):
                doc["qr_code_base64"] = None
        except Exception:
            pass

    return {k: v for k, v in doc.items() if k != "_id"}


@router.post("/session/clear-qr", status_code=204)
async def clear_qr(admin: dict = Depends(current_team_or_admin)):
    """Clear the QR code from the session document (after display)."""
    await db.whatsapp_sessions.update_one(
        {"id": "default"},
        {"$set": {"qr_code_base64": None}},
    )


@router.post("/session/reset", status_code=204)
async def reset_session(admin: dict = Depends(current_admin)):
    """Request the worker to wipe its persisted WhatsApp session and re-link.

    Sets `reset_requested` on the singleton session doc. The worker honors it
    on its next (re)start by clearing the Chromium profile in WA_SESSION_DIR,
    after which WhatsApp Web shows a fresh QR. Admin-only (destructive — forces
    re-authentication). The worker must be (re)started/redeployed to pick it up.
    """
    await db.whatsapp_sessions.update_one(
        {"id": "default"},
        {"$set": {
            "reset_requested": True,
            "status": "qr_pending",
            "qr_code_base64": None,
            "qr_expires_at": None,
            "authenticated_at": None,
            "last_heartbeat": None,
            "error_message": None,
        }},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# ── AUDIT LOG ──────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@router.get("/audit-log")
async def get_audit_log(
    batch_id: Optional[str] = None,
    talent_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 100,
    admin: dict = Depends(current_team_or_admin),
):
    """Fetch audit log entries, optionally filtered."""
    filt: Dict[str, Any] = {}
    if batch_id:
        filt["batch_id"] = batch_id
    if talent_id:
        filt["talent_id"] = talent_id
    if event_type:
        filt["event_type"] = event_type

    docs = (
        await db.whatsapp_audit_log.find(filt, {"_id": 0})
        .sort("timestamp", -1)
        .limit(min(limit, 500))
        .to_list(min(limit, 500))
    )
    return docs


# ---------------------------------------------------------------------------
# ── CONFIG ─────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@router.get("/config")
async def get_config(admin: dict = Depends(current_team_or_admin)):
    docs = await db.whatsapp_config.find({}, {"_id": 0}).to_list(50)
    return {d["key"]: d["value"] for d in docs}


@router.put("/config/{key}")
async def update_config(
    key: str,
    payload: ConfigUpdateIn,
    admin: dict = Depends(current_admin),
):
    allowed_keys = {
        "min_delay_sec",
        "max_delay_sec",
        "max_retries",
        "circuit_breaker_threshold",
        "internal_notification_group_name",
    }
    if key not in allowed_keys:
        raise HTTPException(400, f"Unknown config key '{key}'")

    await db.whatsapp_config.update_one(
        {"key": key},
        {"$set": {"key": key, "value": payload.value}},
        upsert=True,
    )
    await _write_audit("config_updated", admin["id"], metadata={"key": key, "value": payload.value})
    return {"key": key, "value": payload.value}


# ---------------------------------------------------------------------------
# ── CONTACT LISTS ──────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class ContactIn(BaseModel):
    name: str = ""
    phone: str

class ContactListIn(BaseModel):
    name: str
    description: Optional[str] = ""
    contacts: List[ContactIn] = Field(default_factory=list)


@router.get("/contact-lists")
async def list_contact_lists(admin: dict = Depends(current_team_or_admin)):
    docs = await db.whatsapp_contact_lists.find(
        {"deleted": {"$ne": True}},
        {"_id": 0}
    ).sort("created_at", -1).to_list(1000)
    return docs


@router.post("/contact-lists")
async def create_contact_list(payload: ContactListIn, admin: dict = Depends(current_team_or_admin)):
    if not payload.name.strip():
        raise HTTPException(400, "Contact list name cannot be empty")

    normalized_contacts = []
    seen_phones = set()
    for c in payload.contacts:
        norm = _normalize_phone(c.phone)
        if not norm:
            raise HTTPException(400, f"Invalid phone number format: {c.phone}")
        if norm in seen_phones:
            continue
        seen_phones.add(norm)
        normalized_contacts.append({
            "name": c.name.strip(),
            "phone": norm
        })

    list_id = _new_id()
    now = _utcnow()
    doc = {
        "id": list_id,
        "name": payload.name.strip(),
        "description": (payload.description or "").strip(),
        "contacts": normalized_contacts,
        "deleted": False,
        "created_at": now,
        "updated_at": now,
        "created_by": admin.get("email") or "admin"
    }

    await db.whatsapp_contact_lists.insert_one(doc)
    await _write_audit("contact_list_created", admin.get("id", "admin"), metadata={"list_id": list_id, "name": payload.name})
    
    # Remove _id if it was added
    doc.pop("_id", None)
    return doc


@router.get("/contact-lists/{list_id}")
async def get_contact_list(list_id: str, admin: dict = Depends(current_team_or_admin)):
    doc = await db.whatsapp_contact_lists.find_one(
        {"id": list_id, "deleted": {"$ne": True}},
        {"_id": 0}
    )
    if not doc:
        raise HTTPException(404, "Contact list not found")
    return doc


@router.put("/contact-lists/{list_id}")
async def update_contact_list(list_id: str, payload: ContactListIn, admin: dict = Depends(current_team_or_admin)):
    if not payload.name.strip():
        raise HTTPException(400, "Contact list name cannot be empty")

    doc = await db.whatsapp_contact_lists.find_one(
        {"id": list_id, "deleted": {"$ne": True}}
    )
    if not doc:
        raise HTTPException(404, "Contact list not found")

    normalized_contacts = []
    seen_phones = set()
    for c in payload.contacts:
        norm = _normalize_phone(c.phone)
        if not norm:
            raise HTTPException(400, f"Invalid phone number format: {c.phone}")
        if norm in seen_phones:
            continue
        seen_phones.add(norm)
        normalized_contacts.append({
            "name": c.name.strip(),
            "phone": norm
        })

    now = _utcnow()
    update_data = {
        "name": payload.name.strip(),
        "description": (payload.description or "").strip(),
        "contacts": normalized_contacts,
        "updated_at": now
    }

    await db.whatsapp_contact_lists.update_one(
        {"id": list_id},
        {"$set": update_data}
    )
    await _write_audit("contact_list_updated", admin.get("id", "admin"), metadata={"list_id": list_id, "name": payload.name})
    
    # Return updated document
    doc.update(update_data)
    doc.pop("_id", None)
    return doc


@router.delete("/contact-lists/{list_id}")
async def delete_contact_list(list_id: str, admin: dict = Depends(current_team_or_admin)):
    res = await db.whatsapp_contact_lists.update_one(
        {"id": list_id, "deleted": {"$ne": True}},
        {"$set": {"deleted": True, "updated_at": _utcnow()}}
    )
    if not res.matched_count:
        raise HTTPException(404, "Contact list not found")

    await _write_audit("contact_list_deleted", admin.get("id", "admin"), metadata={"list_id": list_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# ── GROUP LISTS ───────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class GroupEntryIn(BaseModel):
    group_name: str

class GroupListIn(BaseModel):
    name: str
    description: Optional[str] = ""
    groups: List[GroupEntryIn] = Field(default_factory=list)


@router.get("/group-lists")
async def list_group_lists(admin: dict = Depends(current_team_or_admin)):
    docs = await db.whatsapp_group_lists.find(
        {"deleted": {"$ne": True}},
        {"_id": 0}
    ).sort("created_at", -1).to_list(1000)
    return docs


@router.post("/group-lists")
async def create_group_list(payload: GroupListIn, admin: dict = Depends(current_team_or_admin)):
    if not payload.name.strip():
        raise HTTPException(400, "Group list name cannot be empty")

    seen_names = set()
    normalized_groups = []
    for g in payload.groups:
        gname = g.group_name.strip()
        if not gname:
            continue
        key = gname.casefold()
        if key in seen_names:
            continue
        seen_names.add(key)
        normalized_groups.append({"group_name": gname})

    list_id = _new_id()
    now = _utcnow()
    doc = {
        "id": list_id,
        "name": payload.name.strip(),
        "description": (payload.description or "").strip(),
        "groups": normalized_groups,
        "deleted": False,
        "created_at": now,
        "updated_at": now,
        "created_by": admin.get("email") or "admin"
    }

    await db.whatsapp_group_lists.insert_one(doc)
    await _write_audit("group_list_created", admin.get("id", "admin"),
                       metadata={"list_id": list_id, "name": payload.name})
    doc.pop("_id", None)
    return doc


@router.get("/group-lists/{list_id}")
async def get_group_list(list_id: str, admin: dict = Depends(current_team_or_admin)):
    doc = await db.whatsapp_group_lists.find_one(
        {"id": list_id, "deleted": {"$ne": True}},
        {"_id": 0}
    )
    if not doc:
        raise HTTPException(404, "Group list not found")
    return doc


@router.put("/group-lists/{list_id}")
async def update_group_list(list_id: str, payload: GroupListIn, admin: dict = Depends(current_team_or_admin)):
    if not payload.name.strip():
        raise HTTPException(400, "Group list name cannot be empty")

    doc = await db.whatsapp_group_lists.find_one(
        {"id": list_id, "deleted": {"$ne": True}}
    )
    if not doc:
        raise HTTPException(404, "Group list not found")

    seen_names = set()
    normalized_groups = []
    for g in payload.groups:
        gname = g.group_name.strip()
        if not gname:
            continue
        key = gname.casefold()
        if key in seen_names:
            continue
        seen_names.add(key)
        normalized_groups.append({"group_name": gname})

    now = _utcnow()
    update_data = {
        "name": payload.name.strip(),
        "description": (payload.description or "").strip(),
        "groups": normalized_groups,
        "updated_at": now
    }

    await db.whatsapp_group_lists.update_one(
        {"id": list_id},
        {"$set": update_data}
    )
    await _write_audit("group_list_updated", admin.get("id", "admin"),
                       metadata={"list_id": list_id, "name": payload.name})
    doc.update(update_data)
    doc.pop("_id", None)
    return doc


@router.delete("/group-lists/{list_id}")
async def delete_group_list(list_id: str, admin: dict = Depends(current_team_or_admin)):
    res = await db.whatsapp_group_lists.update_one(
        {"id": list_id, "deleted": {"$ne": True}},
        {"$set": {"deleted": True, "updated_at": _utcnow()}}
    )
    if not res.matched_count:
        raise HTTPException(404, "Group list not found")

    await _write_audit("group_list_deleted", admin.get("id", "admin"),
                       metadata={"list_id": list_id})
    return {"ok": True}
