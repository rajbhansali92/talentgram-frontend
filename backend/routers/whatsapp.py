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
from typing import Any, Dict, List, Optional

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


class BatchIn(BaseModel):
    project_id: str
    template_id: str
    pipeline_stages: List[str]
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

        group_name = talent.get("whatsapp_group_name")
        phone = talent.get("phone")

        if group_name:
            recipients.append({
                "talent_id": tid,
                "talent_name": talent.get("name", ""),
                "destination_type": "group",
                "destination": group_name,
                "stage": row["stage"],
            })
        elif phone:
            recipients.append({
                "talent_id": tid,
                "talent_name": talent.get("name", ""),
                "destination_type": "number",
                "destination": phone,
                "stage": row["stage"],
            })
        else:
            unresolvable.append({
                "talent_id": tid,
                "talent_name": talent.get("name", ""),
                "reason": "No whatsapp_group_name and no phone number",
            })

    return {
        "recipients": recipients,
        "total": len(recipients),
        "unresolvable": unresolvable,
    }


# ---------------------------------------------------------------------------
# ── BATCHES ────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _render_message(template_body: str, variable_data: Dict[str, str], talent_name: str) -> str:
    """Substitute template variables. Always injects talent_name."""
    data = {**variable_data, "talent_name": talent_name}
    result = template_body
    for key, value in data.items():
        result = result.replace("{{" + key + "}}", value)
    return result


@router.post("/batches", status_code=201)
async def create_batch(payload: BatchIn, admin: dict = Depends(current_team_or_admin)):
    """Create a batch (dry-run or live).

    Dry-run batches resolve recipients + render messages but do NOT send.
    Live batches create pending jobs that the worker will pick up.
    """
    # Validate project
    project = await db.projects.find_one({"id": payload.project_id}, {"_id": 0})
    if not project:
        raise HTTPException(404, "Project not found")

    # Validate template
    template = await db.whatsapp_templates.find_one({"id": payload.template_id}, {"_id": 0})
    if not template:
        raise HTTPException(404, "Template not found")

    # Resolve recipients
    pipeline_rows = await db.casting_pipeline.find(
        {"project_id": payload.project_id, "stage": {"$in": payload.pipeline_stages}},
        {"_id": 0, "talent_id": 1, "stage": 1},
    ).to_list(5000)

    if not pipeline_rows:
        raise HTTPException(400, "No talents found in selected pipeline stages")

    talent_ids = list({r["talent_id"] for r in pipeline_rows})
    talents_cursor = db.talents.find(
        {"id": {"$in": talent_ids}},
        {"_id": 0, "id": 1, "name": 1, "phone": 1, "whatsapp_group_name": 1},
    )
    talents = await talents_cursor.to_list(5000)
    talent_map = {t["id"]: t for t in talents}

    # Build jobs
    job_status = "dry_run_preview" if payload.is_dry_run else "pending"
    now = _utcnow()
    batch_id = _new_id()
    jobs = []
    skipped = []

    seen_talent_ids: set = set()
    for row in pipeline_rows:
        tid = row["talent_id"]
        if tid in seen_talent_ids:
            continue
        seen_talent_ids.add(tid)

        talent = talent_map.get(tid)
        if not talent:
            skipped.append({"talent_id": tid, "reason": "Not found"})
            continue

        group_name = talent.get("whatsapp_group_name")
        phone = talent.get("phone")
        talent_name = talent.get("name", "")

        if group_name:
            dest_type = "group"
            destination = group_name
        elif phone:
            dest_type = "number"
            destination = phone
        else:
            skipped.append({
                "talent_id": tid,
                "talent_name": talent_name,
                "reason": "No group name or phone number",
            })
            continue

        message_body = _render_message(
            template["body_text"], payload.variable_data, talent_name
        )

        jobs.append({
            "id": _new_id(),
            "batch_id": batch_id,
            "talent_id": tid,
            "talent_name": talent_name,
            "destination_type": dest_type,
            "destination": destination,
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

    if not jobs:
        raise HTTPException(
            400,
            f"No sendable recipients found. {len(skipped)} talent(s) had no destination.",
        )

    # Create batch document
    batch_doc = {
        "id": batch_id,
        "project_id": payload.project_id,
        "project_name": project.get("brand_name", ""),
        "template_id": payload.template_id,
        "template_slug": template.get("slug", ""),
        "pipeline_stages": payload.pipeline_stages,
        "variable_data": payload.variable_data,
        "media_url": payload.media_url,
        "is_dry_run": payload.is_dry_run,
        "status": "dry_run_complete" if payload.is_dry_run else "pending",
        "min_delay_sec": payload.min_delay_sec,
        "max_delay_sec": payload.max_delay_sec,
        "total_jobs": len(jobs),
        "sent_count": 0,
        "failed_count": 0,
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

    if action == "pause":
        if current_status != "running":
            raise HTTPException(400, f"Cannot pause a batch with status '{current_status}'")
        new_status = "paused"
    elif action == "resume":
        if current_status != "paused":
            raise HTTPException(400, f"Cannot resume a batch with status '{current_status}'")
        new_status = "running"
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
        {"id": batch_id}, {"$set": {"status": new_status}}
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

    # Ensure batch is in running state if it was paused/failed
    batch = await db.whatsapp_batches.find_one({"id": batch_id})
    if batch and batch.get("status") in ("failed", "paused"):
        await db.whatsapp_batches.update_one(
            {"id": batch_id}, {"$set": {"status": "running"}}
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
    allowed_keys = {"min_delay_sec", "max_delay_sec", "max_retries", "circuit_breaker_threshold"}
    if key not in allowed_keys:
        raise HTTPException(400, f"Unknown config key '{key}'")

    await db.whatsapp_config.update_one(
        {"key": key},
        {"$set": {"key": key, "value": payload.value}},
        upsert=True,
    )
    await _write_audit("config_updated", admin["id"], metadata={"key": key, "value": payload.value})
    return {"key": key, "value": payload.value}
