"""WhatsApp Engine — Worker Registry.

Multi-worker support (2026-09-13): today's single authenticated WhatsApp
number is `worker_id="default"` — this module introduces the registry that
lets a second (or Nth) independently-authenticated number exist alongside it
without touching Worker 1's behavior. See docs/ audit for the full design;
in short:

  - `whatsapp_workers` is pure registry METADATA (id/label/session_instance).
    It never stores live status — that stays on `whatsapp_sessions` (one doc
    per worker_id, same collection Worker 1 already uses under the literal
    id "default"). GET endpoints here join the two at read time so there is
    never a second place live status can drift out of sync.
  - Every other WhatsApp collection (`whatsapp_jobs`, `whatsapp_batches`,
    `whatsapp_agent_config`, `whatsapp_config`) is being taught to carry an
    optional `worker_id` field elsewhere; an absent field always means
    `"default"` — no backfill migration is ever required for Worker 1's
    existing documents.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from core import _now, current_admin, current_team_or_admin, db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/whatsapp/workers", tags=["WhatsApp Workers"])

COLLECTION = "whatsapp_workers"

# The literal id Worker 1 has always used (hardcoded across session.py/
# worker.py/whatsapp.py before this migration) — kept as a real constant
# (not just a string literal sprinkled around) so every caller that needs
# "the id of the pre-existing worker" refers to the same source of truth.
DEFAULT_WORKER_ID = "default"


def worker_match_filter(worker_id: str) -> dict:
    """Mongo filter fragment matching documents belonging to `worker_id`.

    whatsapp_jobs/whatsapp_batches/whatsapp_config documents created before
    multi-worker support have no `worker_id` field at all — treating that
    absence as "belongs to the default worker" (rather than running a
    backfill write against potentially-large collections) is what lets
    Worker 1's existing/in-flight data keep working with zero migration.
    Mirrors agents/registry.py's identical helper (kept as a separate small
    copy rather than a shared import — routers/ and agents/ don't otherwise
    depend on each other, and this is six lines, not worth a new shared
    module for)."""
    if worker_id == DEFAULT_WORKER_ID:
        return {"$or": [{"worker_id": DEFAULT_WORKER_ID}, {"worker_id": {"$exists": False}}]}
    return {"worker_id": worker_id}


# Stable, caller-supplied worker ids (2026-09-13, Worker 2 rollout) — a
# lowercase slug, e.g. "worker-2", matching the exact scheme used
# throughout every worker-scoped query filter in this codebase
# (registry.worker_match_filter, worker_scope_filter in whatsapp.py/
# worker.py) which all compare worker_id by simple string equality, so any
# printable value would technically "work" — this pattern exists to keep
# ids human-readable and to rule out anything that could be misread as a
# Mongo operator, a path segment surprise, or literal whitespace.
_WORKER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")


class WorkerCreateIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    # Optional stable literal id, e.g. "worker-2". Omitted (the default)
    # falls back to an auto-generated id — kept only for callers that don't
    # care about a specific literal name; the two workers this rollout
    # actually needs ("default", already seeded, and "worker-2") are both
    # meant to be created with an explicit id, never the auto-generated form.
    worker_id: Optional[str] = Field(default=None, max_length=64)


def _new_worker_id() -> str:
    return "wa-" + uuid.uuid4().hex[:8]


def _validate_worker_id(worker_id: str) -> str:
    """Normalizes and validates a caller-supplied worker id. Raises 400 for
    anything malformed, and separately (400, not a generic 500) for the one
    reserved value — 'default' is Worker 1's permanent identity, seeded at
    startup; a second worker can never register under it."""
    normalized = worker_id.strip().lower()
    if not _WORKER_ID_PATTERN.match(normalized):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid worker_id '{worker_id}' — must be 1-64 characters, "
                "lowercase letters/digits/hyphens only, not starting or ending with a hyphen."
            ),
        )
    if normalized == DEFAULT_WORKER_ID:
        raise HTTPException(
            status_code=400,
            detail=f"'{DEFAULT_WORKER_ID}' is reserved for the existing Worker 1 identity.",
        )
    return normalized


async def _ensure_indexes() -> None:
    await db[COLLECTION].create_index([("id", 1)], unique=True, name="worker_id_unique")
    logger.info("whatsapp_workers: MongoDB indexes ensured")


async def _seed_default_worker() -> None:
    """Idempotent — insert the Worker 1 registry row if it doesn't exist yet.
    Never overwrites an existing doc (mirrors agents/registry.py's
    seed_agent_config pattern), so re-running this on every startup is safe."""
    existing = await db[COLLECTION].find_one({"id": DEFAULT_WORKER_ID})
    if existing:
        return
    doc = {
        "id": DEFAULT_WORKER_ID,
        "label": "Worker 1",
        "session_instance": DEFAULT_WORKER_ID,
        "active": True,
        # Sending-permission gate (2026-09-14, Worker 2 incident follow-up) —
        # Worker 1 is the pre-existing, already-live production number, so
        # its seed explicitly permits sending from day one. This only
        # affects a genuinely fresh seed (the real production doc already
        # exists and is backfilled separately, once, as an explicit
        # deployment step — this default is what a NEW environment gets).
        "sending_enabled": True,
        "created_at": _now(),
        "created_by": "system_migration",
    }
    await db[COLLECTION].insert_one(doc)
    logger.info("whatsapp_workers: seeded default worker registry row")


async def ensure_workers_ready() -> None:
    """Called from server.py startup, alongside whatsapp.ensure_whatsapp_ready(). Idempotent."""
    await _ensure_indexes()
    await _seed_default_worker()


async def worker_exists(worker_id: str) -> bool:
    doc = await db[COLLECTION].find_one({"id": worker_id}, {"_id": 1})
    return doc is not None


async def require_worker(worker_id: str) -> dict:
    """Backend validation of worker identity (raises 404 for an unknown
    worker_id) — reused by the worker-scoped session/batch/config/agent
    endpoints in whatsapp.py and agents_whatsapp.py so a typo'd or
    unregistered worker_id can never silently create an orphan document."""
    doc = await db[COLLECTION].find_one({"id": worker_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail=f"Unknown worker_id '{worker_id}'")
    return doc


async def _session_doc_for(worker_id: str) -> dict:
    doc = await db.whatsapp_sessions.find_one({"id": worker_id}, {"_id": 0})
    if doc:
        # Clear an expired QR from the response (mirrors the existing
        # singleton GET /api/whatsapp/session behavior) so a stale image
        # never lingers in either the worker-tab card or the session panel.
        qr_expires = doc.get("qr_expires_at")
        if qr_expires and doc.get("status") == "qr_pending":
            try:
                exp_dt = datetime.fromisoformat(qr_expires)
                if exp_dt < datetime.now(timezone.utc):
                    doc["qr_code_base64"] = None
            except Exception:
                pass
        return doc
    return {
        "id": worker_id,
        "status": "disconnected",
        "qr_code_base64": None,
        "last_heartbeat": None,
        "authenticated_at": None,
        "error_message": None,
        "generation": None,
        "session_id": None,
        "connected_phone_number": None,
        "listener_status": None,
        "dispatcher_status": None,
        "reply_pipeline_status": None,
        "worker_ready": False,
        "last_incoming_at": None,
        "last_processed_at": None,
        "last_reply_at": None,
    }


async def _enrich(worker_doc: dict) -> dict:
    session = await _session_doc_for(worker_doc["id"])
    return {**worker_doc, "session": session}


@router.get("")
async def list_workers(admin: dict = Depends(current_team_or_admin)) -> List[Dict[str, Any]]:
    """Every registered worker, each enriched with its live session status.
    Guaranteed to contain at least `DEFAULT_WORKER_ID` once ensure_workers_ready()
    has run once at startup."""
    docs = await db[COLLECTION].find({}, {"_id": 0}).sort("created_at", 1).to_list(100)
    return [await _enrich(d) for d in docs]


@router.get("/{worker_id}")
async def get_worker(worker_id: str, admin: dict = Depends(current_team_or_admin)) -> Dict[str, Any]:
    doc = await require_worker(worker_id)
    return await _enrich(doc)


@router.post("", status_code=201)
async def create_worker(payload: WorkerCreateIn, admin: dict = Depends(current_admin)) -> Dict[str, Any]:
    """Admin-only — provisions a new worker registry row. Does NOT start a
    browser session or a Railway service; those are separate infrastructure
    steps (a new whatsapp-worker Railway service must be deployed with
    WORKER_ID set to the id returned here). This call only reserves the
    identity so the new worker's session/QR endpoints have something to
    validate against the moment that service boots.

    `worker_id`, when supplied, is used verbatim (after validation) instead
    of an auto-generated one — e.g. "worker-2", matching the literal
    WORKER_ID env var the corresponding Railway service will be deployed
    with. Duplicates are rejected explicitly (409), both via an upfront
    check and the unique index as a race-safe backstop — never silently
    overwritten and never allowed to collide."""
    if payload.worker_id:
        worker_id = _validate_worker_id(payload.worker_id)
        if await db[COLLECTION].find_one({"id": worker_id}, {"_id": 1}):
            raise HTTPException(status_code=409, detail=f"Worker id '{worker_id}' already exists")
    else:
        worker_id = _new_worker_id()

    doc = {
        "id": worker_id,
        "label": payload.label.strip(),
        "session_instance": worker_id,
        "active": True,
        # Sending-permission gate — a newly registered worker can never send
        # real traffic until an admin explicitly calls
        # POST /{worker_id}/enable-sending as its own, separate, deliberate
        # action. Registration alone (this route) must never imply send
        # permission — see _create_batch_internal's fail-closed check.
        "sending_enabled": False,
        "created_at": _now(),
        "created_by": admin["id"],
    }
    try:
        await db[COLLECTION].insert_one(doc)
    except DuplicateKeyError:
        # The upfront find_one above covers the common case; this backstop
        # closes the race between that check and the insert (two concurrent
        # creation requests for the same id) — the unique index on `id`
        # (see _ensure_indexes) is the actual source of truth for
        # uniqueness, this is just a friendlier error than a raw 500.
        raise HTTPException(status_code=409, detail=f"Worker id '{worker_id}' already exists")
    logger.info("whatsapp_workers: created worker %s (%s)", worker_id, doc["label"])
    doc.pop("_id", None)
    return await _enrich(doc)


# ---------------------------------------------------------------------------
# ── WORKER-SCOPED SESSION ────────────────────────────────────────────────
#
# Parallel, worker_id-parameterized versions of the existing singleton
# routes in routers/whatsapp.py (GET /session, POST /session/clear-qr,
# POST /session/reset, all hardcoded to {"id": "default"}). Those routes
# are left completely untouched so the current frontend and Worker 1's
# production behavior are unaffected; these new routes are what a worker
# tab UI (or a second worker's own tooling) targets going forward.
# ---------------------------------------------------------------------------

@router.get("/{worker_id}/session")
async def get_worker_session(worker_id: str, admin: dict = Depends(current_team_or_admin)) -> Dict[str, Any]:
    await require_worker(worker_id)
    return await _session_doc_for(worker_id)


@router.post("/{worker_id}/session/clear-qr", status_code=204)
async def clear_worker_qr(worker_id: str, admin: dict = Depends(current_team_or_admin)):
    await require_worker(worker_id)
    await db.whatsapp_sessions.update_one(
        {"id": worker_id},
        {"$set": {"qr_code_base64": None}},
        upsert=True,
    )


@router.post("/{worker_id}/session/reset", status_code=204)
async def reset_worker_session(worker_id: str, admin: dict = Depends(current_admin)):
    """Request that worker's own whatsapp-worker process wipe its persisted
    session and re-link — identical semantics to the existing singleton
    reset, just targeted at this worker's own session doc (and, by
    extension, its own SESSION_DIR/volume — never another worker's)."""
    await require_worker(worker_id)
    await db.whatsapp_sessions.update_one(
        {"id": worker_id},
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
# ── SENDING PERMISSION (2026-09-14, Worker 2 incident follow-up) ───────────
#
# A worker can be registered and fully authenticated (real WhatsApp session,
# real heartbeat) while still being unable to send a single real message —
# registration and authentication only ever establish IDENTITY, never send
# permission. The actual gate lives in whatsapp.py's _create_batch_internal
# (checked against this same `sending_enabled` field); this route is the
# ONLY way that field is ever set to True, and it is a deliberate,
# stand-alone admin action, never implied by any other route above.
# ---------------------------------------------------------------------------

@router.post("/{worker_id}/enable-sending")
async def enable_worker_sending(worker_id: str, admin: dict = Depends(current_admin)) -> Dict[str, Any]:
    """Admin-only. Flips ONLY this worker's `sending_enabled` to True — no
    other field, document, or collection is touched, and nothing is sent,
    queued, retried, or replayed by this call. Sending remains blocked for
    every other worker until this is called for them explicitly."""
    doc = await require_worker(worker_id)
    await db[COLLECTION].update_one({"id": worker_id}, {"$set": {"sending_enabled": True}})
    logger.info("whatsapp_workers: sending enabled for worker %s by admin %s", worker_id, admin["id"])
    updated = {**doc, "sending_enabled": True}
    return await _enrich(updated)
