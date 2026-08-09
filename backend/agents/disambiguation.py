"""Shared Interactive Disambiguation Engine (Sprint 1, 2026-08-09).

Generic, agent-agnostic replacement for the "please try again with the
exact name" dead-end: when a domain module's own resolver (project name,
template name, talent name, CRM contact-type, saved list — any of them)
finds MULTIPLE legitimate candidates instead of one, it can hand them to
this module instead of just formatting a text error. This module stores
one pending-choice document, formats the numbered prompt, and resolves the
user's next reply against the candidate list — digit, circled digit,
ordinal word, "option N", "#N", or the candidate's own name/label, plain or
inside a WhatsApp quoted reply (the user's own typed reply text already
carries this — see dispatcher.py's "disambiguating" step, which is the
only caller that threads a live `text` value in here).

This module knows NOTHING about projects, templates, talents, CRM, or
saved lists — `entity_type` is an opaque label the caller chose, only used
to pick a plural noun for the prompt header ("I found multiple ___s.").
`field_key` is likewise opaque — the caller's own `collected` dict key that
should be overwritten with the resolved candidate's `label` once picked.
Nothing here reaches into any agents/modules/* file, and nothing in
agents/modules/* needs to reach back into dispatcher.py — the contract is
entirely this module's public functions, mirroring the existing
agents/conversation.py and agents/tasks.py state-store pattern (one
document per (agent_id, phone), TTL-indexed, upsert-and-replace).

State stored (deliberately nothing more, per the sprint spec):
  waiting_for_disambiguation — implicit: a document exists at all, and the
    parent conversation's `step` is "disambiguating" (set by the caller).
  entity_type    — opaque string, e.g. "project" / "template" / "talent" /
                    "crm_source" / "saved_list".
  candidate list — [{"id": ..., "label": ...}, ...], at most 10 (①-⑩).
  original parsed intent — `intent_id`, so the caller can look the
                    IntentDefinition back up once resolved.
  remaining fields — `collected`, the same dict the parent conversation
                    already had, snapshotted so dispatcher.py doesn't need
                    to re-read the conversation document separately.
  expires_at     — TTL-indexed; an abandoned disambiguation self-cleans,
                    exactly like whatsapp_conversations already does.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from core import db
from agents import name_match

COLLECTION = "whatsapp_agent_disambiguation"

# Shorter than the parent conversation's 30-minute TTL — once a numbered
# choice is shown, the user is expected to reply promptly; a stale pick
# against a candidate list that might no longer be accurate (e.g. a project
# renamed in between) is worse than just asking them to start over.
DEFAULT_TTL_MINUTES = 10

MAX_CANDIDATES = 10

_ENTITY_LABELS = {
    "project": "projects",
    "template": "templates",
    "talent": "talents",
    "crm_source": "CRM contacts",
    "saved_list": "saved lists",
}
_ENTITY_SINGULAR_NOUN = {
    "project": "project name",
    "template": "template name",
    "talent": "name",
    "crm_source": "category name",
    "saved_list": "list name",
}

_CIRCLED_DIGITS = "①②③④⑤⑥⑦⑧⑨⑩"
_CIRCLED_TO_INDEX = {ch: i + 1 for i, ch in enumerate(_CIRCLED_DIGITS)}

_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Candidate:
    id: str
    label: str


def entity_label(entity_type: str) -> str:
    return _ENTITY_LABELS.get(entity_type, entity_type)


def format_prompt(entity_type: str, candidates: List[Candidate]) -> str:
    lines = [f"I found multiple {entity_label(entity_type)}."]
    lines.append("")
    for i, c in enumerate(candidates):
        marker = _CIRCLED_DIGITS[i] if i < len(_CIRCLED_DIGITS) else f"{i + 1})"
        lines.append(f"{marker} {c.label}")
    lines.append("")
    lines.append("Reply with:")
    for i in range(len(candidates)):
        marker = _CIRCLED_DIGITS[i] if i < len(_CIRCLED_DIGITS) else f"{i + 1})"
        lines.append(marker)
    lines.append("")
    lines.append(f"or simply type the {_ENTITY_SINGULAR_NOUN.get(entity_type, 'name')}.")
    return "\n".join(lines)


async def start(
    *,
    agent_id: str,
    phone: str,
    entity_type: str,
    candidates: List[Candidate],
    intent_id: str,
    field_key: str,
    collected: Dict[str, str],
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> None:
    now = _now()
    doc = {
        "agent_id": agent_id,
        "phone": phone,
        "entity_type": entity_type,
        "candidates": [{"id": c.id, "label": c.label} for c in candidates[:MAX_CANDIDATES]],
        "intent_id": intent_id,
        "field_key": field_key,
        "collected": collected,
        "created_at": now,
        "expires_at": now + timedelta(minutes=ttl_minutes),
    }
    await db[COLLECTION].replace_one({"agent_id": agent_id, "phone": phone}, doc, upsert=True)


async def get_pending(agent_id: str, phone: str) -> Optional[dict]:
    return await db[COLLECTION].find_one({"agent_id": agent_id, "phone": phone})


def is_expired(doc: dict) -> bool:
    expires_at = doc.get("expires_at")
    if not expires_at:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return _now() >= expires_at


async def clear(agent_id: str, phone: str) -> None:
    await db[COLLECTION].delete_one({"agent_id": agent_id, "phone": phone})


_LEADING_DIGIT_RE = re.compile(r"^\s*(?:#|option\s+)?(\d+)\s*[).:]?\s*$", re.IGNORECASE)
_ORDINAL_RE = re.compile(
    r"^\s*(?:the\s+)?(" + "|".join(_ORDINAL_WORDS) + r")(?:\s+one)?\s*$", re.IGNORECASE,
)


def resolve_reply(text: str, candidates: List[Candidate]) -> Optional[Candidate]:
    """Accepts, in order: a bare circled digit; a bare/#-prefixed/"option
    N"-prefixed plain digit; an ordinal word ("second", "the second one");
    or the candidate's own name/label (exact, then fuzzy via the same
    shared tiered matcher every other name lookup on this platform uses —
    no separate fuzzy algorithm here). Returns None on no confident match
    (out-of-range index, or a label that doesn't resolve to exactly one
    candidate) — the caller re-prompts rather than guessing."""
    raw = (text or "").strip()
    if not raw or not candidates:
        return None

    if raw in _CIRCLED_TO_INDEX:
        idx = _CIRCLED_TO_INDEX[raw]
        return candidates[idx - 1] if 1 <= idx <= len(candidates) else None

    m = _LEADING_DIGIT_RE.match(raw)
    if m:
        idx = int(m.group(1))
        return candidates[idx - 1] if 1 <= idx <= len(candidates) else None

    m = _ORDINAL_RE.match(raw)
    if m:
        idx = _ORDINAL_WORDS[m.group(1).lower()]
        return candidates[idx - 1] if 1 <= idx <= len(candidates) else None

    match = name_match.tiered_name_match(raw, candidates, lambda c: c.label, id_fn=lambda c: c.id)
    if match.item is not None:
        return match.item
    return None
