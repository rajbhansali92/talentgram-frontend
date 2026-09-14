"""Canonical inbound-WhatsApp message capture + talent/project linking.

Phase 7 of Simple Assistant. This is infrastructure, NOT Simple-Assistant
code: it lives in the backend service layer (next to notifications.py /
drive_backup.py) and is written to by the EXISTING inbound entry point
(routers/agents_whatsapp.py::inbound_message — the seam the Playwright
worker already POSTs every group message to). Simple Assistant only READS
from it.

Nothing here sends, replies, retries, interprets, or mutates pipeline
state. No LLM. The stored `message_text` is the verbatim inbound text.

Collection: whatsapp_inbound_messages
  * dedup: unique index on `message_key` (the WhatsApp message id, or a
    deterministic hash when the transport didn't supply one) — an atomic
    insert guard, so a concurrent re-delivery of the same id cannot create
    a second row.
  * retention: permanent, no TTL — the same standard `interactions` (the
    comm timeline) and `whatsapp_agent_audit_log` already use for
    communication history. Growth is naturally bounded: the worker only
    scans Agent-Registry groups, and dedup prevents reprocessing.

Feature-flagged: SA_INBOUND_CAPTURE_ENABLED (default OFF). When off the
inbound endpoint behaves exactly as before — capture is a no-op.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from core import db
from agents.registry import DEFAULT_WORKER_ID, worker_match_filter
from routers.whatsapp import _normalize_phone  # the app's own E.164-ish normalizer (STEP 5)

logger = logging.getLogger(__name__)

COLLECTION = "whatsapp_inbound_messages"

# Conservative context window for project inference from a recent outbound
# casting send (STEP 11). A talent's reply to a specific send normally lands
# within hours; 72h covers weekend gaps + async worker delivery lag while
# refusing to associate a message 4+ days later with a long-past send.
PROJECT_WINDOW_HOURS = int(os.environ.get("SA_INBOUND_PROJECT_WINDOW_HOURS", "72"))

_SA_AGENT_ID = "simple-assistant"
_ACTIVE_PROJECT_STATUSES = ("ongoing", "hold", "locked")


def is_capture_enabled() -> bool:
    return os.environ.get("SA_INBOUND_CAPTURE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# phone
# --------------------------------------------------------------------------
def normalize_phone(raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """→ (normalized | None, key | None). `key` is the last 10 digits — the
    match-tolerant form (a number stored as +91XXXXXXXXXX, 91XXXXXXXXXX,
    0XXXXXXXXXX or bare XXXXXXXXXX all share it)."""
    norm = _normalize_phone(raw)
    digits = "".join(ch for ch in (norm or str(raw or "")) if ch.isdigit())
    key = digits[-10:] if len(digits) >= 10 else (digits or None)
    return norm, key


async def resolve_talent_by_phone(raw_phone: str) -> Tuple[Optional[str], str, List[str]]:
    """sender phone → canonical talent. → (talent_id | None,
    'resolved'|'unresolved'|'ambiguous', candidate_ids)."""
    norm, key = normalize_phone(raw_phone)
    if not key:
        return None, "unresolved", []

    variants = {key, f"+91{key}", f"91{key}", f"0{key}"}
    if norm:
        variants.add(norm)
    if raw_phone:
        variants.add(str(raw_phone).strip())

    docs = await db.talents.find(
        {"phone": {"$in": list(variants)}}, {"_id": 0, "id": 1, "status": 1}
    ).to_list(50)
    if not docs:
        # suffix fallback — an unusual stored format ("+91 " with a space, etc.)
        docs = await db.talents.find(
            {"phone": {"$regex": f"{key}$"}}, {"_id": 0, "id": 1, "status": 1}
        ).to_list(50)

    live = [d for d in docs if (d.get("status") or "").upper() not in ("MERGED", "ARCHIVED", "DELETED")]
    pool = live or docs
    ids = sorted({d["id"] for d in pool if d.get("id")})
    if not ids:
        return None, "unresolved", []
    if len(ids) == 1:
        return ids[0], "resolved", []
    return None, "ambiguous", ids


# --------------------------------------------------------------------------
# group
# --------------------------------------------------------------------------
async def resolve_group(
    group_name: Optional[str], worker_id: str = DEFAULT_WORKER_ID,
) -> Tuple[Optional[str], str, Optional[str]]:
    """→ (project_id | None, kind, talent_hint_id | None).
    kind: 'project_casting' | 'talent_own' | 'registered' | 'direct' | 'unknown'.

    `worker_id` (multi-worker support, 2026-09-13) scopes the
    whatsapp_agent_config lookup below to the worker the inbound message
    actually arrived through — passed by capture_inbound, itself given the
    SAME worker_id the real routing decision (agents.dispatcher.
    handle_inbound_message) uses for this exact message, never guessed.
    Defaults to the pre-existing worker so every current caller (four
    existing test files that construct kwargs without worker_id) is
    unaffected."""
    g = (group_name or "").strip()
    if not g:
        return None, "direct", None
    proj = await db.projects.find_one({"whatsapp_casting_group_name": g}, {"_id": 0, "id": 1})
    if proj:
        return proj["id"], "project_casting", None
    tal = await db.talents.find_one({"whatsapp_group_name": g}, {"_id": 0, "id": 1})
    if tal:
        return None, "talent_own", tal["id"]
    cfg = await db.whatsapp_agent_config.find_one(
        {"group_names": g, **worker_match_filter(worker_id)}, {"_id": 0, "agent_id": 1},
    )
    if cfg:
        return None, "registered", None
    return None, "unknown", None


# --------------------------------------------------------------------------
# project (STEP 9 / 10 / 11)
# --------------------------------------------------------------------------
async def resolve_project(
    *, talent_id: Optional[str], group_project_id: Optional[str], received_at: datetime,
) -> Tuple[Optional[str], str, List[str]]:
    """→ (project_id | None,
    'casting_group'|'recent_outbound'|'ambiguous_project'|'unresolved'|'none',
    candidate_ids)."""
    # A — the message came from a project's casting group. Reliable.
    if group_project_id:
        return group_project_id, "casting_group", []

    if not talent_id:
        return None, "none", []

    # B — a recent Simple-Assistant casting send to THIS talent, within the
    # conservative window. Evidence-based, never a lifetime association.
    since = received_at - timedelta(hours=PROJECT_WINDOW_HOURS)
    rows = await db.whatsapp_agent_audit_log.find(
        {"agent_id": _SA_AGENT_ID, "sa_action.action_type": "send_media",
         "sa_action.talent_id": talent_id, "timestamp": {"$gte": since}},
        {"_id": 0, "sa_action": 1, "timestamp": 1},
    ).sort("timestamp", -1).to_list(50)
    recent_pids = sorted({(r.get("sa_action") or {}).get("project_id")
                          for r in rows if (r.get("sa_action") or {}).get("project_id")})
    if len(recent_pids) == 1:
        return recent_pids[0], "recent_outbound", []
    if len(recent_pids) > 1:
        return None, "ambiguous_project", recent_pids

    # C — no direct evidence. Surface the talent's active pipeline projects
    # as *candidates* only; never auto-assign one.
    pipe = await db.casting_pipeline.find(
        {"talent_id": talent_id}, {"_id": 0, "project_id": 1}
    ).to_list(200)
    pids = {p["project_id"] for p in pipe if p.get("project_id")}
    if not pids:
        return None, "unresolved", []
    active = await db.projects.find(
        {"id": {"$in": list(pids)}, "status": {"$in": list(_ACTIVE_PROJECT_STATUSES)}},
        {"_id": 0, "id": 1},
    ).to_list(200)
    cands = sorted({p["id"] for p in active})
    if not cands:
        return None, "unresolved", []
    return None, "ambiguous_project", cands


# --------------------------------------------------------------------------
# capture (STEP 3 / 4 / 13)
# --------------------------------------------------------------------------
def _message_key(message_id: Optional[str], group_name: Optional[str],
                 sender_phone: str, text: str, received_at: datetime) -> str:
    if message_id and str(message_id).strip():
        return f"wamid:{str(message_id).strip()}"
    # deterministic fallback so a transport that can't read the WhatsApp id
    # still dedups on re-delivery of the identical event
    basis = f"{group_name or ''}|{sender_phone}|{text}|{received_at.strftime('%Y%m%dT%H%M')}"
    return "hash:" + hashlib.sha256(basis.encode()).hexdigest()[:40]


async def capture_inbound(
    *,
    message_id: Optional[str],
    sender_phone: str,
    text: str,
    sender_name: Optional[str] = None,
    group_name: Optional[str] = None,
    media_type: Optional[str] = None,
    replied_to_message_id: Optional[str] = None,
    received_at: Optional[datetime] = None,
    source: str = "whatsapp_worker",
    worker_id: str = DEFAULT_WORKER_ID,
) -> Optional[Dict[str, Any]]:
    """Persist ONE canonical inbound record, deduped by message key. Returns
    the stored (or pre-existing) doc, or None if capture is disabled / the
    payload is unusable. Never raises — a capture failure must not affect
    the inbound endpoint's response.

    `worker_id` (multi-worker support, 2026-09-13): threaded into
    resolve_group's own worker-scoped whatsapp_agent_config lookup below.
    Defaults to the pre-existing worker, so the four existing test files
    that call capture_inbound(**kwargs) without it are unaffected."""
    if not is_capture_enabled():
        return None
    try:
        rcv = received_at or _now()
        key = _message_key(message_id, group_name, sender_phone, text or "", rcv)

        existing = await db[COLLECTION].find_one({"message_key": key}, {"_id": 0})
        if existing:
            return existing

        norm, phone_key = normalize_phone(sender_phone)
        group_pid, group_kind, group_talent_hint = await resolve_group(group_name, worker_id)
        talent_id, talent_res, talent_cands = await resolve_talent_by_phone(sender_phone)
        # a talent-own group whose sender phone didn't resolve — trust the
        # group's owner as a hint but keep the resolution honest
        if talent_res == "unresolved" and group_kind == "talent_own" and group_talent_hint:
            talent_id, talent_res = group_talent_hint, "resolved"

        project_id, project_res, project_cands = await resolve_project(
            talent_id=talent_id, group_project_id=group_pid, received_at=rcv,
        )

        doc = {
            "id": f"inb_{uuid.uuid4().hex[:12]}",
            "message_key": key,
            "message_id": (str(message_id).strip() if message_id else None),
            "direction": "in",
            "source": source,
            "received_at": rcv,
            "captured_at": _now(),
            "sender_phone": sender_phone,
            "sender_phone_normalized": norm,
            "sender_phone_key": phone_key,
            "sender_name": (sender_name or None),
            "group_name": (group_name or None),
            "group_kind": group_kind,
            "worker_id": worker_id,
            "message_text": text or "",
            "media_type": (media_type or None),
            "replied_to_message_id": (replied_to_message_id or None),
            "talent_id": talent_id,
            "talent_resolution": talent_res,
            "talent_candidates": talent_cands,
            "project_id": project_id,
            "project_resolution": project_res,
            "project_candidates": project_cands,
        }

        try:
            await db[COLLECTION].insert_one(dict(doc))
        except Exception as exc:  # DuplicateKeyError on a concurrent race
            if "duplicate key" in str(exc).lower() or "E11000" in str(exc):
                return await db[COLLECTION].find_one({"message_key": key}, {"_id": 0})
            raise
        return doc
    except Exception:
        logger.exception("inbound_messages: capture failed (non-fatal)")
        return None


# --------------------------------------------------------------------------
# read layer (Simple Assistant only — auth enforced by the caller)
# --------------------------------------------------------------------------
async def find_for_talent(talent_id: str, *, project_id: Optional[str] = None, limit: int = 10) -> List[dict]:
    q: Dict[str, Any] = {"talent_id": talent_id}
    if project_id:
        q["project_id"] = project_id
    return await db[COLLECTION].find(q, {"_id": 0}).sort("received_at", -1).to_list(limit)


async def find_for_project(project_id: str, *, limit: int = 25) -> List[dict]:
    return await db[COLLECTION].find(
        {"project_id": project_id}, {"_id": 0}
    ).sort("received_at", -1).to_list(limit)


async def find_unresolved_for_talent_name(talent_ids: List[str], *, limit: int = 10) -> List[dict]:
    """Messages whose sender couldn't be resolved but that name-match a
    talent the admin asked about — surfaced as 'possibly from' only."""
    return await db[COLLECTION].find(
        {"talent_resolution": {"$in": ["unresolved", "ambiguous"]},
         "talent_candidates": {"$in": talent_ids}}, {"_id": 0},
    ).sort("received_at", -1).to_list(limit)


async def unanswered_for_project(project_id: str, *, limit: int = 25) -> List[dict]:
    """Inbound messages (resolved talent + this project) with NO Simple
    Assistant casting send to that talent AFTER the message. Conservative:
    only messages the system can actually reason about are considered."""
    msgs = await db[COLLECTION].find(
        {"project_id": project_id, "talent_id": {"$ne": None}},
        {"_id": 0},
    ).sort("received_at", -1).to_list(200)
    out = []
    for m in msgs:
        later = await db.whatsapp_agent_audit_log.find_one({
            "agent_id": _SA_AGENT_ID, "sa_action.action_type": "send_media",
            "sa_action.talent_id": m["talent_id"], "sa_action.project_id": project_id,
            "timestamp": {"$gt": m["received_at"]},
        })
        if not later:
            out.append(m)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# indexes — call from core.py's bootstrap (idempotent)
# --------------------------------------------------------------------------
INDEXES = [
    ([("message_key", 1)], {"unique": True, "name": "inbound_message_key_unique"}),
    ([("sender_phone_key", 1), ("received_at", -1)], {"name": "inbound_sender_phone_key"}),
    ([("talent_id", 1), ("received_at", -1)], {"sparse": True, "name": "inbound_talent_received_at"}),
    ([("project_id", 1), ("received_at", -1)], {"sparse": True, "name": "inbound_project_received_at"}),
    ([("group_name", 1), ("received_at", -1)], {"name": "inbound_group_received_at"}),
    ([("received_at", -1)], {"name": "inbound_received_at"}),
]
