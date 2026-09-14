"""Agent Registry + Intent Registry.

Two layers of registration:
  1. In-process: each domain module calls `register_agent()` once at import
     time with its `AgentDefinition` (agent_id + intents). This is pure
     Python, no DB — it's the code-level "what can this agent do."
  2. DB-backed config (`whatsapp_agent_config` collection): which WhatsApp
     GROUP NAME(S) route to a given agent_id, and which sender phone
     numbers are allowed to issue commands to it. This is admin-editable
     at runtime without a redeploy — rename a group, add a second group
     for the same agent, add/remove an allowed number — none of it touches
     code. An agent_id is never derived from a group name; the mapping is
     the only place the two are connected.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from core import db
from agents.models import AgentDefinition

logger = logging.getLogger(__name__)

_AGENTS: Dict[str, AgentDefinition] = {}

CONFIG_COLLECTION = "whatsapp_agent_config"

# Multi-worker support (2026-09-13) — the id every existing config doc
# implicitly belongs to (they predate the worker_id field entirely). See
# worker_match_filter's docstring for why this must stay a fallback filter,
# never a backfill write.
DEFAULT_WORKER_ID = "default"


def worker_match_filter(worker_id: str) -> dict:
    """Mongo filter fragment matching documents that belong to `worker_id`.

    Every whatsapp_agent_config doc created before multi-worker support
    simply has no `worker_id` field at all — treating that absence as
    "belongs to the default worker" (rather than running a one-time backfill
    write) is what lets Worker 1's existing group mappings keep resolving
    with zero migration and zero risk to production. A non-default
    worker_id has no legacy documents to account for, so it's a plain
    equality match."""
    if worker_id == DEFAULT_WORKER_ID:
        return {"$or": [{"worker_id": DEFAULT_WORKER_ID}, {"worker_id": {"$exists": False}}]}
    return {"worker_id": worker_id}


def register_agent(agent: AgentDefinition) -> None:
    """Register (or replace) an agent definition. Idempotent — safe to call
    on every import/reload."""
    _AGENTS[agent.agent_id] = agent
    logger.info("agent registered: %s (%s intents)", agent.agent_id, len(agent.intents))


def get_agent(agent_id: str) -> Optional[AgentDefinition]:
    return _AGENTS.get(agent_id)


def list_agents() -> List[AgentDefinition]:
    return list(_AGENTS.values())


def get_intent(agent: AgentDefinition, intent_id: str):
    for intent in agent.intents:
        if intent.intent_id == intent_id:
            return intent
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_group_name(name: str) -> str:
    return " ".join((name or "").strip().split()).lower()


async def seed_agent_config(
    agent_id: str,
    *,
    group_names: List[str],
    allowed_senders: Optional[List[str]] = None,
    security_mode: Optional[str] = None,
    worker_id: str = DEFAULT_WORKER_ID,
) -> None:
    """Seed a default config doc for an agent if one doesn't exist yet.
    Never overwrites an existing (possibly admin-edited) config.

    `security_mode` is optional and omitted from the doc when None, so
    existing callers (e.g. crm-agent, which relies on the "allowlist"
    default applied by is_sender_allowed) are unaffected — only an agent
    that explicitly wants "group_members" access needs to pass it.

    `worker_id` defaults to the pre-existing worker's id, so every current
    call site (agents/__init__.py's ensure_agents_ready) keeps seeding
    exactly the configs it always has, unchanged. An agent that should also
    be reachable from a second worker's number needs its own config doc —
    call this again with a different worker_id (agent_id + worker_id is the
    real key now, not agent_id alone)."""
    existing = await db[CONFIG_COLLECTION].find_one({
        "agent_id": agent_id, **worker_match_filter(worker_id),
    })
    if existing:
        return
    doc = {
        "agent_id": agent_id,
        "worker_id": worker_id,
        "group_names": group_names,
        "allowed_senders": allowed_senders or [],
        "active": True,
        "created_at": _now(),
        "updated_at": _now(),
    }
    if security_mode is not None:
        doc["security_mode"] = security_mode
    await db[CONFIG_COLLECTION].insert_one(doc)
    logger.info("seeded whatsapp_agent_config for %s (worker=%s): groups=%s",
                agent_id, worker_id, group_names)


async def get_agent_config(agent_id: str, worker_id: str = DEFAULT_WORKER_ID) -> Optional[dict]:
    return await db[CONFIG_COLLECTION].find_one({
        "agent_id": agent_id, **worker_match_filter(worker_id),
    })


async def find_agents_with_empty_group_names() -> List[str]:
    """Read-only health check (2026-08-25, production incident) — an
    `active: True` agent whose group_names is empty is functionally dead
    (resolve_agent_for_group can never match ANY group for it) but was
    never flagged anywhere: seed_agent_config() only writes a default on
    first creation and deliberately never overwrites an existing doc, so a
    config that drifted to an empty list (however that happened — an admin
    edit, a diagnostic script, manual DB surgery) stays silently broken
    across every future restart. A real command sent into that agent's
    intended WhatsApp group then produces no reply and no error anywhere.
    Called from ensure_agents_ready() on every startup so this state is
    visible in logs immediately rather than discovered by a user's message
    going unanswered."""
    broken = []
    cursor = db[CONFIG_COLLECTION].find({"active": True})
    async for cfg in cursor:
        if not (cfg.get("group_names") or []):
            broken.append(cfg["agent_id"])
    return broken


async def resolve_agent_for_group(
    group_name: str, worker_id: str = DEFAULT_WORKER_ID,
) -> Optional[Tuple[AgentDefinition, dict]]:
    """Given the WhatsApp group a message arrived in, find the (agent,
    config) it should route to, or None if no active agent owns that
    group. This is the *only* place group names are matched against
    agents — everywhere else in the platform operates on agent_id.

    `worker_id` scopes the match to configs belonging to the WORKER the
    message actually arrived through (defaults to the pre-existing worker,
    so every caller that doesn't pass one — dispatcher.py's production
    path before the transport sends its own identity, every existing test
    — keeps resolving exactly as before). This is what prevents two
    different workers/numbers with identically-named groups from ever
    cross-routing to each other's agent: a group name is only ever unique
    *within* one worker's own mapped set, never globally."""
    target = _norm_group_name(group_name)
    if not target:
        return None
    cursor = db[CONFIG_COLLECTION].find({"active": True, **worker_match_filter(worker_id)})
    async for cfg in cursor:
        names = [_norm_group_name(g) for g in (cfg.get("group_names") or [])]
        if target in names:
            agent = get_agent(cfg["agent_id"])
            if agent:
                return agent, cfg
    return None


def is_sender_allowed(
    config: dict, phone: str, *, is_group_member: Optional[bool] = None
) -> bool:
    """Security gate for who may issue commands to an agent. Branches on the
    agent's `security_mode` (default "allowlist") so different agents can use
    different boundaries without changing this function's callers:

      - "allowlist" (default): phone must be in `allowed_senders`. An empty
        allowlist accepts no one — fail closed, never open.
      - "group_members": the sender must currently be a participant of the
        WhatsApp group the message arrived in. The Agent Platform has no way
        to know this itself (it doesn't talk to WhatsApp) — the transport
        layer determines live membership and reports it via `is_group_member`.
        Fail closed: if the transport couldn't determine membership
        (`is_group_member is None`), access is denied, same as "can't verify
        => don't guess" everywhere else in this system.
    """
    mode = config.get("security_mode") or "allowlist"
    if mode == "group_members":
        return bool(is_group_member)
    allowed = config.get("allowed_senders") or []
    if not allowed:
        # An agent with an empty allowlist accepts no one — allowlists must
        # be explicitly populated. Fail closed, never open.
        return False
    return phone in allowed
