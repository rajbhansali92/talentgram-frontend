"""AI Casting Desk — Gate 1 only (Requirement -> Human Approval -> existing
Talentgram project creation).

This router is a thin orchestration layer. It owns exactly one collection
(``casting_desk_sessions``) — a workflow record that holds the pasted
requirement, the AI's structured interpretation, the human's edits, and an
event trail. Everything it *does* is delegated:

    analyse  ->  ai.casting_requirement.analyse()           (1 LLM call)
    approve  ->  routers.projects.create_project()          (unchanged)
             ->  routers.projects.attach_project_material() (unchanged)

No second project model, no copied project-creation logic, no new
requirement system — the created project is byte-for-byte a normal
Talentgram project and shows up on the existing Projects page.

Gates 2-4 (scouting, pipeline, WhatsApp, casting package) are NOT here.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, ValidationError

from core import (
    APP_NAME,
    MATERIAL_CATEGORIES,
    MAX_VIDEO_FILE_BYTES,
    ProjectIn,
    _now,
    cloudinary_destroy,
    cloudinary_upload,
    current_team_or_admin,
    db,
)
from ai import casting_requirement as cr
from ai import client as llm
from ai import extract as material_extract
from routers import projects as projects_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/casting-desk", tags=["AI Casting Desk"])

COLLECTION = "casting_desk_sessions"

MAX_RAW_INPUT_CHARS = 20000
MAX_ATTACHMENTS = 8
MAX_NON_VIDEO_BYTES = 25 * 1024 * 1024
STORED_TEXT_CAP = 20000

STATUS_DRAFT = "draft"
STATUS_ANALYSED = "analysed"
STATUS_CREATING = "creating_project"
STATUS_CREATED = "project_created"
STATUS_ERROR = "error"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class SessionCreateIn(BaseModel):
    raw_input: str = ""


class RawInputPatchIn(BaseModel):
    raw_input: str = ""


class DraftEditsIn(BaseModel):
    # Free-form map of project-draft keys -> override values. Recognised keys:
    # brand_name, character, shoot_dates, budget_per_day, commission_percent,
    # medium_usage, director, production_house, additional_details,
    # video_links (list), competitive_brand_enabled (bool),
    # submission_requirements (full object).
    edits: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _event(action: str, actor: Dict[str, Any], detail: str = "") -> Dict[str, Any]:
    return {"at": _now(), "actor": actor.get("email") or actor.get("id"), "action": action, "detail": detail}


def _new_session(user: Dict[str, Any], raw_input: str) -> Dict[str, Any]:
    now = _now()
    return {
        "id": str(uuid.uuid4()),
        "created_by": user["id"],
        "created_by_email": user.get("email"),
        "status": STATUS_DRAFT,
        "raw_input": raw_input[:MAX_RAW_INPUT_CHARS],
        "attachments": [],
        "extraction": None,
        "human_edits": {},
        "project_draft": None,
        "readiness": None,
        "analysis_model": None,
        "analysed_at": None,
        "project_id": None,
        "project_slug": None,
        "error": None,
        "events": [_event("session_created", user)],
        "created_at": now,
        "updated_at": now,
    }


def _public(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    return doc


def _summary(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": doc["id"],
        "status": doc["status"],
        "brand": (doc.get("project_draft") or {}).get("brand_name")
        or ((doc.get("extraction") or {}).get("fields", {}).get("brand", {}) or {}).get("value")
        or "",
        "raw_input_preview": (doc.get("raw_input") or "")[:140],
        "attachment_count": len(doc.get("attachments") or []),
        "project_id": doc.get("project_id"),
        "created_by_email": doc.get("created_by_email"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


async def _load(sid: str, user: Dict[str, Any], *, for_write: bool = False) -> Dict[str, Any]:
    doc = await db[COLLECTION].find_one({"id": sid})
    if not doc:
        raise HTTPException(404, "Casting Desk session not found")
    if doc["created_by"] != user["id"] and user.get("role") != "admin":
        raise HTTPException(403, "Not your Casting Desk session")
    if for_write and doc["status"] == STATUS_CREATED:
        raise HTTPException(409, "This session already created a project and is locked")
    return doc


async def _save(sid: str, patch: Dict[str, Any], *, event: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    patch = {**patch, "updated_at": _now()}
    update: Dict[str, Any] = {"$set": patch}
    if event:
        update["$push"] = {"events": event}
    await db[COLLECTION].update_one({"id": sid}, update)
    return _public(await db[COLLECTION].find_one({"id": sid}))


def _recompute(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild project_draft + readiness from the stored extraction and
    human_edits. Returns the fields to persist."""
    extraction = doc.get("extraction")
    if not extraction:
        return {"project_draft": None, "readiness": None}
    edits = doc.get("human_edits") or {}
    draft = cr.build_project_draft(extraction, edits)
    readiness = cr.draft_readiness(extraction, draft, edits)
    return {"project_draft": draft, "readiness": readiness}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@router.get("/health")
async def health(_user: dict = Depends(current_team_or_admin)):
    return {
        "llm_configured": llm.is_configured(),
        "model": llm.DEFAULT_MODEL,
        "audio_transcription": material_extract.audio_transcription_available(),
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
@router.post("/sessions", status_code=201)
async def create_session(payload: SessionCreateIn, user: dict = Depends(current_team_or_admin)):
    doc = _new_session(user, (payload.raw_input or "").strip())
    await db[COLLECTION].insert_one(doc)
    return _public(doc)


@router.get("/sessions")
async def list_sessions(limit: int = 30, user: dict = Depends(current_team_or_admin)):
    limit = max(1, min(limit, 100))
    query: Dict[str, Any] = {} if user.get("role") == "admin" else {"created_by": user["id"]}
    rows = await db[COLLECTION].find(query).sort("updated_at", -1).limit(limit).to_list(limit)
    return {"data": [_summary(r) for r in rows]}


@router.get("/sessions/{sid}")
async def get_session(sid: str, user: dict = Depends(current_team_or_admin)):
    return _public(await _load(sid, user))


@router.patch("/sessions/{sid}")
async def patch_raw_input(sid: str, payload: RawInputPatchIn, user: dict = Depends(current_team_or_admin)):
    await _load(sid, user, for_write=True)
    return await _save(
        sid,
        {"raw_input": (payload.raw_input or "").strip()[:MAX_RAW_INPUT_CHARS]},
        event=_event("raw_input_edited", user),
    )


@router.delete("/sessions/{sid}", status_code=204)
async def delete_session(sid: str, user: dict = Depends(current_team_or_admin)):
    doc = await _load(sid, user)
    if doc["status"] == STATUS_CREATED:
        raise HTTPException(409, "A session that created a project is kept as a record and cannot be deleted")
    for att in doc.get("attachments") or []:
        _destroy_staged(att)
    await db[COLLECTION].delete_one({"id": sid})


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
def _rt_for(category: str) -> str:
    return "video" if category == "video_file" else "auto"


def _destroy_staged(att: Dict[str, Any]) -> None:
    pid = att.get("public_id")
    if not pid:
        return
    try:
        cloudinary_destroy(pid, resource_type=att.get("resource_type") or "image")
    except Exception:  # pragma: no cover - cleanup is best effort
        logger.warning("failed to destroy staged casting-desk asset %s", pid)


@router.post("/sessions/{sid}/attachments")
async def add_attachment(
    sid: str,
    file: UploadFile = File(...),
    category: Optional[str] = Form(None),
    user: dict = Depends(current_team_or_admin),
):
    doc = await _load(sid, user, for_write=True)
    if len(doc.get("attachments") or []) >= MAX_ATTACHMENTS:
        raise HTTPException(400, f"At most {MAX_ATTACHMENTS} materials per session")

    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")

    resolved_category = material_extract.classify_material(
        file.filename, file.content_type, category if category in MATERIAL_CATEGORIES else None
    )
    limit = MAX_VIDEO_FILE_BYTES if resolved_category == "video_file" else MAX_NON_VIDEO_BYTES
    if len(data) > limit:
        raise HTTPException(400, f"File too large ({len(data) // (1024*1024)} MB). Max {limit // (1024*1024)} MB.")

    # Extract text for the analyser (best effort, never fatal).
    text, extraction_status = material_extract.extract_material_text(
        resolved_category, data, file.filename or "", file.content_type
    )

    # Stage the asset on Cloudinary now so it survives to the approval step
    # and the user can see it was received. On approval it is re-attached to
    # the real project via the shared projects.attach_project_material path.
    att_id = str(uuid.uuid4())
    ext = ("." + file.filename.rsplit(".", 1)[-1].lower()) if file.filename and "." in file.filename else ""
    try:
        up = cloudinary_upload(
            data,
            folder=f"{APP_NAME}/casting_desk/{sid}",
            public_id=f"{att_id}{ext}",
            resource_type=_rt_for(resolved_category),
            content_type=file.content_type,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("casting-desk attachment upload failed: %s", exc)
        raise HTTPException(502, "Could not store the uploaded file")

    attachment = {
        "id": att_id,
        "category": resolved_category,
        "original_filename": file.filename,
        "content_type": file.content_type,
        "size": up.get("bytes") or len(data),
        "url": up["url"],
        "public_id": up["public_id"],
        "resource_type": up["resource_type"],
        "extracted_text": (text or "")[:STORED_TEXT_CAP],
        "extraction_status": extraction_status,
        "created_at": _now(),
    }
    await db[COLLECTION].update_one(
        {"id": sid},
        {"$push": {"attachments": attachment, "events": _event("material_added", user, f"{resolved_category}: {file.filename}")},
         "$set": {"updated_at": _now()}},
    )
    return _public(await db[COLLECTION].find_one({"id": sid}))


@router.delete("/sessions/{sid}/attachments/{aid}", status_code=200)
async def remove_attachment(sid: str, aid: str, user: dict = Depends(current_team_or_admin)):
    doc = await _load(sid, user, for_write=True)
    att = next((a for a in (doc.get("attachments") or []) if a.get("id") == aid), None)
    if not att:
        raise HTTPException(404, "Attachment not found")
    _destroy_staged(att)
    await db[COLLECTION].update_one(
        {"id": sid},
        {"$pull": {"attachments": {"id": aid}},
         "$push": {"events": _event("material_removed", user, att.get("original_filename") or aid)},
         "$set": {"updated_at": _now()}},
    )
    return _public(await db[COLLECTION].find_one({"id": sid}))


# ---------------------------------------------------------------------------
# Analyse (the single Gate-1 LLM call)
# ---------------------------------------------------------------------------
@router.post("/sessions/{sid}/analyse")
async def analyse_session(sid: str, user: dict = Depends(current_team_or_admin)):
    doc = await _load(sid, user, for_write=True)
    raw = (doc.get("raw_input") or "").strip()
    materials_text = "\n\n".join(
        f"[{a.get('category')} — {a.get('original_filename') or 'material'}]\n{a.get('extracted_text')}"
        for a in (doc.get("attachments") or [])
        if (a.get("extracted_text") or "").strip()
    )
    if not raw and not materials_text:
        raise HTTPException(400, "Add a requirement or a material with readable text before analysing")

    try:
        extraction = await cr.analyse(raw, materials_text)
    except llm.LLMUnavailable as exc:
        await _save(sid, {}, event=_event("analyse_failed", user, str(exc)))
        raise HTTPException(503, f"AI is not available: {exc}")
    except llm.LLMError as exc:
        await _save(sid, {}, event=_event("analyse_failed", user, str(exc)))
        raise HTTPException(502, f"AI could not analyse this requirement: {exc}")

    patch = {
        "extraction": extraction,
        "analysis_model": llm.DEFAULT_MODEL,
        "analysed_at": _now(),
        "status": STATUS_ANALYSED,
        "error": None,
    }
    # keep existing human_edits — re-analysing must not discard corrections
    merged = {**doc, **patch}
    patch.update(_recompute(merged))
    return await _save(sid, patch, event=_event("analysed", user))


# ---------------------------------------------------------------------------
# Human edits
# ---------------------------------------------------------------------------
@router.patch("/sessions/{sid}/draft")
async def edit_draft(sid: str, payload: DraftEditsIn, user: dict = Depends(current_team_or_admin)):
    doc = await _load(sid, user, for_write=True)
    if not doc.get("extraction"):
        raise HTTPException(400, "Analyse the requirement before editing the draft")

    human_edits = {**(doc.get("human_edits") or {})}
    for k, v in (payload.edits or {}).items():
        # None clears an override (fall back to the AI value)
        if v is None:
            human_edits.pop(k, None)
        else:
            human_edits[k] = v

    merged = {**doc, "human_edits": human_edits}
    patch = {"human_edits": human_edits, **_recompute(merged)}
    return await _save(sid, patch, event=_event("draft_edited", user, ", ".join((payload.edits or {}).keys())))


# ---------------------------------------------------------------------------
# GATE 1 — approve -> existing project creation
# ---------------------------------------------------------------------------
async def _fetch_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as hc:
        r = await hc.get(url)
        r.raise_for_status()
        return r.content


@router.post("/sessions/{sid}/approve")
async def approve_session(sid: str, user: dict = Depends(current_team_or_admin)):
    doc = await _load(sid, user, for_write=True)
    if not doc.get("extraction"):
        raise HTTPException(400, "Analyse the requirement before approving")

    # Authoritative recompute — never trust a stale stored draft.
    recomputed = _recompute(doc)
    draft = recomputed["project_draft"]
    readiness = recomputed["readiness"]
    if not draft:
        raise HTTPException(400, "No project draft to approve")
    if not readiness["can_create"]:
        raise HTTPException(400, {"message": "Draft is not ready", "blocking": readiness["blocking"]})

    # Build the EXISTING project payload.
    try:
        project_in = ProjectIn(**cr.draft_to_project_payload(draft))
    except ValidationError as exc:
        await _save(
            sid,
            {"status": STATUS_ERROR, "error": f"Project payload invalid: {exc.errors()}", **recomputed},
            event=_event("approve_failed", user, "validation"),
        )
        raise HTTPException(400, f"The draft does not form a valid project: {exc.errors()}")

    await _save(
        sid,
        {"status": STATUS_CREATING, "error": None, **recomputed},
        event=_event("approved", user, f"brand={draft.get('brand_name')}"),
    )

    # --- existing Talentgram project creation (unchanged) ---
    try:
        created = await projects_router.create_project(payload=project_in, admin=user)
    except HTTPException as exc:
        await _save(sid, {"status": STATUS_ERROR, "error": f"Project creation rejected: {exc.detail}"},
                    event=_event("approve_failed", user, "create_project"))
        raise
    except Exception as exc:  # pragma: no cover
        logger.exception("casting-desk project creation crashed")
        await _save(sid, {"status": STATUS_ERROR, "error": f"Project creation failed: {exc}"},
                    event=_event("approve_failed", user, "create_project"))
        raise HTTPException(500, "Project creation failed unexpectedly")

    project_id = created["id"]
    # Persist the link immediately — a later material failure must never
    # orphan the created project.
    await _save(
        sid,
        {"status": STATUS_CREATED, "project_id": project_id, "project_slug": created.get("slug"), "error": None},
        event=_event("project_created", user, project_id),
    )

    # --- attach materials through the existing shared path ---
    material_results: List[Dict[str, Any]] = []
    for att in doc.get("attachments") or []:
        entry = {"attachment_id": att["id"], "filename": att.get("original_filename"), "category": att.get("category")}
        try:
            data = await _fetch_bytes(att["url"])
            await projects_router.attach_project_material(
                project_id, att["category"], data, att.get("original_filename"), att.get("content_type")
            )
            _destroy_staged(att)
            entry["ok"] = True
        except Exception as exc:
            logger.warning("casting-desk material attach failed (%s): %s", att.get("original_filename"), exc)
            entry["ok"] = False
            entry["error"] = str(exc)
        material_results.append(entry)

    failures = [m for m in material_results if not m.get("ok")]
    session = await _save(
        sid,
        {"material_results": material_results},
        event=_event(
            "materials_attached",
            user,
            f"{len(material_results) - len(failures)}/{len(material_results)} ok",
        ),
    )

    fresh_project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    return {
        "session": session,
        "project_id": project_id,
        "project": fresh_project,
        "materials": material_results,
        "material_failures": len(failures),
    }
