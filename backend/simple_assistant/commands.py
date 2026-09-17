"""Simple Assistant — deterministic conversational command layer (Phase 2).

    user message  →  detect intent (regex, no LLM)
                  →  resolve entities (reused nlu matchers + read-only DB)
                  →  clarify if ambiguous  (multi-turn, stateless round-trip)
                  →  build a dry-run ActionPlan  (current + proposed state)
                  →  return it for the UI to preview

**Nothing here executes.** Every DB touch goes through ``readonly_db.RDB``
(raises on any write op). No import of any executor, dispatcher, WhatsApp,
Cloudinary, links, notifications, or LLM module. No collection is written.

Reused from the existing agent platform (all pure, DB-free functions):
  * ``agents.modules.casting_pipeline_nlu``  — resolve_project_by_name,
    parse_talent_selector, resolve_against_candidates, resolve_option_reply,
    split_multi_names, stage_label, Candidate, SelectorResult
Reused read-only:
  * ``routers.casting_pipeline`` — PIPELINE_STAGE_ORDER, _normalise_stage
  * ``simple_assistant.service`` — build_overview, STAGE_LABELS,
    _ACTIVE_PROJECT_STATUSES
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from agents.modules import casting_pipeline_nlu as nlu
from routers.casting_pipeline import PIPELINE_STAGE_ORDER, _normalise_stage
from simple_assistant.plan import ActionPlan, ProposedChange
from simple_assistant.readonly_db import RDB
from simple_assistant.security import sign, verify
from simple_assistant.service import (
    _ACTIVE_PROJECT_STATUSES,
    STAGE_LABELS,
    build_overview,
)

# Intents whose confirmed plan Phase 3 will actually execute. Everything
# else previews only and its confirm returns "not enabled yet".
EXECUTABLE_INTENTS = frozenset({"mark_talent_status", "pipeline_add_remove"})

logger = logging.getLogger(__name__)

CONTEXT_VERSION = 1
_MAX_INLINE_PROJECT_OPTIONS = 15
_TALENT_SCAN_CAP = 20000

# --------------------------------------------------------------------------
# Intent detection — deterministic, regex only.
# --------------------------------------------------------------------------
_MARK_NA_RE = re.compile(
    r"\b(?:not\s+available|un-?available|isn'?t\s+available|aren'?t\s+available|"
    r"is\s+not\s+available|are\s+not\s+available|no\s+longer\s+available|not\s+free|"
    r"won'?t\s+be\s+available)\b",
    re.I,
)
_MARK_NI_RE = re.compile(
    r"\b(?:not\s+interested|un-?interested|isn'?t\s+interested|aren'?t\s+interested|"
    r"is\s+not\s+interested|are\s+not\s+interested|no\s+longer\s+interested)\b",
    re.I,
)
_MARK_VERB_RE = re.compile(
    r"^\s*(?:can you\s+|could you\s+|please\s+|pls\s+)*(?:mark|set|update|change|make|put|move)\b",
    re.I,
)
_STATUS_STRIP_RE = re.compile(
    r"\b(?:as\s+)?(?:not\s+available|un-?available|isn'?t\s+available|aren'?t\s+available|"
    r"is\s+not\s+available|are\s+not\s+available|no\s+longer\s+available|not\s+free|"
    r"won'?t\s+be\s+available|not\s+interested|un-?interested|isn'?t\s+interested|"
    r"aren'?t\s+interested|is\s+not\s+interested|are\s+not\s+interested|no\s+longer\s+interested|"
    r"available|interested)\b",
    re.I,
)
_PRONOUN_RE = re.compile(
    r"\b(?:them|they|her|him|it|these|those|that\s+talent|the\s+talent|this\s+talent|"
    r"all\s+(?:of\s+)?(?:them|three|four|five|two))\b",
    re.I,
)
_FILLER_RE = re.compile(
    r"\b(?:for\s+(?:that|this|the)\s+project|for\s+it|please|kindly|now|both|"
    r"the\s+following|as\s+well)\b",
    re.I,
)
_ADD_RE = re.compile(r"\b(?:add|include|put\s+in|bring\s+in)\b", re.I)
_REMOVE_RE = re.compile(r"\b(?:remove|drop|take\s+off|take\s+out|pull\s+out|exclude)\b", re.I)
_CREATE_PROJECT_RE = re.compile(
    r"\b(?:create|set\s+up|start|draft|make|add)\s+(?:a\s+|the\s+|new\s+)*project\b|"
    r"\bnew\s+project\b|\bproject\s+from\s+(?:this\s+)?brief\b|\bhere'?s?\s+a\s+brief\b",
    re.I,
)
_COUNT_PROJECTS_RE = re.compile(
    r"\bhow\s+many\b.*\bprojects?\b|\bnumber\s+of\b.*\bprojects?\b|"
    r"\bactive\s+projects?\b.*\?|\bhow\s+many\b.*\bactive\b",
    re.I,
)
_PRIORITIES_RE = re.compile(
    r"\b(?:priorit(?:y|ies)|need(?:s)?\s+(?:my\s+)?attention|what'?s?\s+(?:up|pending)|"
    r"today'?s?\s+(?:priorities|focus)|follow[\s-]?ups?)\b",
    re.I,
)
_TESTS_RE = re.compile(
    r"\b(?:tests?\s+received|received\s+(?:their\s+)?tests?|pending\s+tests?|"
    r"who\s+(?:has|have)\s+(?:sent|submitted|done)|find\s+.*\btests?\b|"
    r"awaiting\s+tests?)\b",
    re.I,
)
_PIPELINE_RE = re.compile(r"\bpipeline\b|\bshow\s+me\s+.+\b(?:project|casting)\b", re.I)

_SCOPE_RE_FOR = re.compile(
    r"\b(?:for|on|to|from|in)\s+(?:the\s+)?(.+?)(?:\s+(?:project|campaign|shoot|casting))?\s*(?:[.?!]|,\s|$)",
    re.I,
)
_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "for", "of", "please", "mark", "set",
    "them", "her", "him", "it", "that", "this", "project", "unavailable",
    "available", "interested", "with", "from", "in", "on", "as", "not",
}


def _looks_like_name(tok: str) -> bool:
    t = tok.strip().lower()
    return bool(t) and len(t) > 1 and t not in _STOPWORDS


def _extract_scope(message: str) -> Optional[str]:
    """The project/campaign reference in the message, if any — the LAST
    'for/on/to/from/in <X>' phrase (project references almost always trail
    the command). Returns None if nothing scope-like is present."""
    last = None
    for m in _SCOPE_RE_FOR.finditer(message):
        cand = m.group(1).strip(" .!?,-")
        # Reject obvious non-project tails.
        if cand and cand.lower() not in _STOPWORDS and not _MARK_NA_RE.fullmatch(cand or ""):
            last = cand
    return last


# --------------------------------------------------------------------------
# Intent record
# --------------------------------------------------------------------------
class Intent:
    MARK = "mark_talent_status"
    ADD_REMOVE = "pipeline_add_remove"
    CREATE_PROJECT = "create_project"
    READ = "read_query"
    UNKNOWN = "unknown"
    UPDATE_FIELD = "update_field"
    COMM = "communication"
    SAVE_GROUP = "save_whatsapp_group"
    SUBMISSION = "submission"
    STATUS = "whatsapp_status"


_UPDATE_FIELD_RE = re.compile(
    r"\b(?:set|change|update|edit)\b.+\b(?:'?s|to|=|as)\b|"
    r"\b(?:budget|shoot\s*date|location|commission|email|phone|instagram|character|"
    r"height|age|dob|name)\b.+\b(?:to|=)\b",
    re.I,
)


_SAVE_GROUP_RE = re.compile(
    r"\b(?:group\s+is|whatsapp\s+group\s+is|save\s+.+\bgroup\b|set\s+.+\bwhatsapp\s+group\b)\b", re.I
)


def detect_intent(message: str) -> str:
    msg = message or ""
    from simple_assistant.comm import is_comm_command
    from simple_assistant.inbound_intelligence import is_conversation_query, is_intelligence_query
    from simple_assistant.status import is_retry_request, is_status_query
    from simple_assistant.submission import is_submission_command

    # STATUS first — "check the status of the Google AI send" contains "send",
    # "did X's audition get sent" contains "audition"; neither is a send/ingest.
    # Phase 8 inbound-intelligence and Phase 12 conversation queries route here too.
    if (is_status_query(msg) or is_retry_request(msg) or is_intelligence_query(msg)
            or is_conversation_query(msg)):
        return Intent.STATUS
    if is_submission_command(msg) and not (_MARK_NA_RE.search(msg) or _MARK_NI_RE.search(msg)):
        return Intent.SUBMISSION
    if is_comm_command(msg) and not (_MARK_NA_RE.search(msg) or _MARK_NI_RE.search(msg)):
        return Intent.COMM
    if _SAVE_GROUP_RE.search(msg) and "group" in msg.lower():
        return Intent.SAVE_GROUP
    if _CREATE_PROJECT_RE.search(msg) and ("brief" in msg.lower() or len(msg) > 60 or ":" in msg):
        return Intent.CREATE_PROJECT
    if _MARK_NA_RE.search(msg) or _MARK_NI_RE.search(msg):
        return Intent.MARK
    if _ADD_RE.search(msg) and _REMOVE_RE.search(msg):
        return Intent.ADD_REMOVE
    if (_ADD_RE.search(msg) or _REMOVE_RE.search(msg)) and _SCOPE_RE_FOR.search(msg):
        return Intent.ADD_REMOVE
    if (
        _COUNT_PROJECTS_RE.search(msg)
        or _PRIORITIES_RE.search(msg)
        or _TESTS_RE.search(msg)
        or _PIPELINE_RE.search(msg)
    ):
        return Intent.READ
    if _UPDATE_FIELD_RE.search(msg):
        return Intent.UPDATE_FIELD
    return Intent.UNKNOWN


# --------------------------------------------------------------------------
# Entity resolution (read-only)
# --------------------------------------------------------------------------
async def _active_projects() -> List[Dict[str, str]]:
    docs = await RDB.projects.find(
        {"status": {"$in": list(_ACTIVE_PROJECT_STATUSES)}},
        {"_id": 0, "id": 1, "brand_name": 1},
    ).sort("brand_name", 1).to_list(2000)
    return [{"id": d["id"], "label": d.get("brand_name") or "(untitled project)"} for d in docs]


async def _pipeline_candidates(project_id: str) -> List[nlu.Candidate]:
    rows = await RDB.casting_pipeline.find(
        {"project_id": project_id}, {"_id": 0, "talent_id": 1, "stage": 1}
    ).to_list(5000)
    ids = [r["talent_id"] for r in rows if r.get("talent_id")]
    stage_by_id = {r["talent_id"]: (_normalise_stage(r.get("stage")) or r.get("stage")) for r in rows}
    names: Dict[str, str] = {}
    if ids:
        tdocs = await RDB.talents.find(
            {"id": {"$in": ids}}, {"_id": 0, "id": 1, "name": 1}
        ).to_list(len(ids))
        names = {d["id"]: d.get("name") or "Unknown" for d in tdocs}
    return [
        nlu.Candidate(id=tid, label=names.get(tid, "Unknown"), stage=stage_by_id.get(tid))
        for tid in ids
    ]


async def _global_talent_candidates() -> List[nlu.Candidate]:
    docs = await RDB.talents.find(
        {}, {"_id": 0, "id": 1, "name": 1}
    ).limit(_TALENT_SCAN_CAP).to_list(_TALENT_SCAN_CAP)
    return [nlu.Candidate(id=d["id"], label=d.get("name") or "Unknown") for d in docs]


def _resolve_one_name(name: str, candidates: List[nlu.Candidate]):
    return nlu.resolve_against_candidates(
        nlu.SelectorResult(ok=True, name_query=name), candidates
    )


def _cand_options(cands: List[nlu.Candidate]) -> List[Dict[str, Any]]:
    return [
        {
            "index": i + 1,
            "id": c.id,
            "label": c.label + (f" — {nlu.stage_label(c.stage)}" if c.stage else ""),
            "name": c.label,
        }
        for i, c in enumerate(cands)
    ]


# --------------------------------------------------------------------------
# Result envelope
# --------------------------------------------------------------------------
def _envelope(
    *,
    conversation_id: str,
    state: str,
    message: str,
    intent: Optional[str] = None,
    clarification: Optional[dict] = None,
    plan: Optional[dict] = None,
    answer: Optional[dict] = None,
    comm: Optional[dict] = None,
    sub: Optional[dict] = None,
    status: Optional[dict] = None,
    inbound: Optional[dict] = None,
    intelligence: Optional[dict] = None,
    context: Optional[dict] = None,
    requires_confirmation: bool = False,
) -> dict:
    return {
        "conversation_id": conversation_id,
        "state": state,
        "message": message,
        "intent": intent,
        "clarification": clarification,
        "plan": plan,
        "answer": answer,
        "comm": comm,
        "sub": sub,
        "status": status,
        "inbound": inbound,
        "intelligence": intelligence,
        "context": context,
        "requires_confirmation": requires_confirmation,
    }


_CONFIRMABLE_KINDS = ("confirm_plan", "confirm_comm", "confirm_submission", "confirm_upload", "confirm_ai_response")

# SA-2 (pre-commit audit) — explicit expiry, same 30-minute window
# confirm_ai_response already used (commands.py's AI-draft branch sets its
# own `expires_at` inline; unchanged by this). confirm_submission/
# confirm_upload are out of scope for this change and never get one here —
# a missing `expires_at` is treated as "does not expire" by `_is_expired()`,
# so their behavior is unaffected.
_TTL_KINDS = ("confirm_plan", "confirm_comm")
_PLAN_TTL_MINUTES = 30


def _ctx(conversation_id: str, pending: Optional[dict]) -> Optional[dict]:
    if pending is None:
        return None
    # A confirmable plan gets a stable id (idempotency + audit correlation).
    if pending.get("kind") in _CONFIRMABLE_KINDS and "plan_id" not in pending:
        pending = {**pending, "plan_id": f"sap_{uuid.uuid4().hex}"}
    # SA-2 — a confirm_plan/confirm_comm plan gets an explicit expiry, bound
    # into the same HMAC-signed payload below (sign() covers every key in
    # `pending`, so this is tamper-evident exactly like plan_id already is;
    # no change to security.py's signing/verification logic was needed).
    if pending.get("kind") in _TTL_KINDS and "expires_at" not in pending:
        import datetime as _dt
        pending = {**pending, "expires_at": (
            _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=_PLAN_TTL_MINUTES)
        ).isoformat()}
    # HMAC-sign the pending payload so the client cannot forge or edit a
    # plan/clarification and have the server act on it (see security.py).
    return {
        "v": CONTEXT_VERSION,
        "conversation_id": conversation_id,
        "pending": pending,
        "sig": sign(pending),
    }


def _verified_pending(context: Optional[dict]) -> Optional[dict]:
    """Return context.pending ONLY when the signature checks out — a
    tampered or unsigned context is treated as if no context was sent."""
    if not context or context.get("v") != CONTEXT_VERSION:
        return None
    pending = context.get("pending")
    if pending is None or not verify(pending, context.get("sig")):
        return None
    return pending


def _is_expired(pending: dict) -> bool:
    """SA-2 — True if `pending` carries an `expires_at` that has passed.
    A missing or unparseable `expires_at` is treated as "does not expire"
    (the kinds that never set one — confirm_submission, confirm_upload —
    are unaffected by this check). Mirrors the try/except pattern
    `ai_response_execute.py` already used for `confirm_ai_response`."""
    exp = pending.get("expires_at")
    if not exp:
        return False
    try:
        import datetime as _dt
        return _dt.datetime.now(_dt.timezone.utc) > _dt.datetime.fromisoformat(exp)
    except Exception:
        return False


UNKNOWN_HELP = (
    "I couldn't map that to something I can do yet. In this phase I can:\n"
    "• mark talents unavailable / not interested for a project\n"
    "• add or remove talents from a project's pipeline\n"
    "• draft a project from a brief\n"
    "• answer questions about your projects, pipelines, follow-ups and tests\n"
    "Nothing runs until you review the preview and press Confirm."
)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
async def run_command(
    *, message: str, conversation_id: Optional[str], context: Optional[dict], user: dict,
    file_meta: Optional[dict] = None,
) -> dict:
    cid = conversation_id or (context or {}).get("conversation_id") or f"sa_{uuid.uuid4().hex[:12]}"
    message = (message or "").strip()
    pending = _verified_pending(context)

    if not message:
        return _envelope(
            conversation_id=cid, state="blocked",
            message="Say what you'd like me to look at or prepare.",
        )

    # 1. A reply to a pending clarification?
    if pending and pending.get("kind") in ("clarify_project", "clarify_talent"):
        resumed = await _resume_clarification(cid, message, pending, user)
        if resumed is not None:
            return resumed
        # else: fall through — the message wasn't a pick, treat as fresh command.
    if pending and pending.get("kind") in ("clarify_comm_project", "clarify_comm_talent",
                                           "clarify_comm_destination", "comm_media_partial"):
        resumed = await _resume_comm(cid, message, pending, user)
        if resumed is not None:
            return resumed
    if pending and pending.get("kind") in ("clarify_sub_project", "clarify_sub_talent"):
        resumed = await _resume_submission(cid, message, pending, user, file_meta=file_meta)
        if resumed is not None:
            return resumed
    if pending and pending.get("kind") in ("clarify_status", "clarify_status_project", "clarify_status_talent"):
        resumed = await _resume_status(cid, message, pending, user)
        if resumed is not None:
            return resumed

    intent = detect_intent(message)

    if intent == Intent.STATUS:
        return await _handle_status(cid, message, user)
    if intent == Intent.SUBMISSION or file_meta:
        # A file on the wire is only ever an incoming-audition ingest.
        return await _handle_submission(cid, message, user, file_meta=file_meta)
    if intent == Intent.COMM:
        return await _handle_comm(cid, message, user)
    if intent == Intent.SAVE_GROUP:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.SAVE_GROUP,
            message="I can identify a talent's WhatsApp group, but saving it to the talent profile "
                    "isn't enabled in this phase.",
        )
    if intent == Intent.MARK:
        return await _handle_mark(cid, message, user, carry=None)
    if intent == Intent.ADD_REMOVE:
        return await _handle_add_remove(cid, message, user)
    if intent == Intent.CREATE_PROJECT:
        return _handle_create_project(cid, message)
    if intent == Intent.READ:
        return await _handle_read(cid, message, user)
    if intent == Intent.UPDATE_FIELD:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.UPDATE_FIELD,
            message="I can understand that request, but editing that field isn't enabled yet. "
                    "For now I can mark talents unavailable / not interested and add or remove "
                    "them from a project's pipeline.",
        )

    return _envelope(
        conversation_id=cid, state="blocked", intent="unknown", message=UNKNOWN_HELP,
    )


# --------------------------------------------------------------------------
# mark_talent_status
# --------------------------------------------------------------------------
# "<names> aren't available" / "<names> are not interested" — subject is
# whatever precedes the status verb in that clause.
_SUBJECT_STATUS_RE = re.compile(
    r"(.+?)\s+(?:are|is|was|were|'re|'s)?\s*(?:n'?t|not|no\s+longer)\s+"
    r"(?:available|free|interested)",
    re.I,
)
# "mark/set/... <names> unavailable/not interested/as not available"
_VERB_SUBJECT_RE = re.compile(
    r"\b(?:mark|set|update|change|make|move|put)\s+(.+?)\s+(?:as\s+)?"
    r"(?:un-?available|not\s+available|un-?interested|not\s+interested|unavailable)",
    re.I,
)


def _clean_name_fragment(frag: str) -> str:
    frag = _MARK_VERB_RE.sub(" ", frag)
    frag = re.sub(
        r"\b(?:mark|set|update|change|make|move|put|them|they|her|him|it|as|please|"
        r"unavailable|available|interested|not|un)\b",
        " ", frag, flags=re.I,
    )
    frag = re.sub(r"[.?!,;:]", " ", frag)
    return re.sub(r"\s+", " ", frag).strip(" -")


def _split_names(text: str) -> List[str]:
    out: List[str] = []
    for raw in nlu.split_multi_names(text):
        cleaned = _clean_name_fragment(raw)
        if cleaned and _looks_like_name(cleaned):
            out.append(cleaned)
    return out


def _extract_mark(message: str) -> Optional[Tuple[str, List[str], bool, Optional[str]]]:
    target = (
        "not_interested" if _MARK_NI_RE.search(message)
        else "not_available" if _MARK_NA_RE.search(message)
        else None
    )
    if not target:
        return None
    project_text = _extract_scope(message)
    work = message
    if project_text:
        work = _SCOPE_RE_FOR.sub(" ", work)
    had_pronoun = bool(_PRONOUN_RE.search(work))

    subject = None
    # "<names> aren't available" wins over a trailing "Mark them unavailable".
    m = _SUBJECT_STATUS_RE.search(work)
    if m and _split_names(m.group(1)):
        subject = m.group(1)
    else:
        m = _VERB_SUBJECT_RE.search(work)
        if m:
            subject = m.group(1)

    if subject is None:
        # Fall back: strip every known verb/status/pronoun/filler token.
        subject = _FILLER_RE.sub(" ", work)
        subject = _STATUS_STRIP_RE.sub(" ", subject)

    names = _split_names(subject)
    is_pronoun = had_pronoun and not names
    return target, names, is_pronoun, project_text


async def _handle_mark(cid: str, message: str, user: dict, carry: Optional[dict]) -> dict:
    parsed = _extract_mark(message)
    if not parsed:
        return _envelope(conversation_id=cid, state="blocked", intent=Intent.MARK, message=UNKNOWN_HELP)
    target, names, is_pronoun, project_text = parsed

    if is_pronoun and not (carry or {}).get("last_talent_ids"):
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.MARK,
            message="I'm not sure who you mean by that — name the talent(s) and the project.",
        )
    if not names and not is_pronoun:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.MARK,
            message="Which talent(s) should I mark? e.g. \"Mark Ahana unavailable for Google AI\".",
        )

    # Resolve project.
    projects = await _active_projects()
    proj, clar = _match_project(project_text, projects)
    if clar is not None:
        return _envelope(
            conversation_id=cid, state="clarification", intent=Intent.MARK,
            message=clar["message"], clarification=clar["clarification"],
            context=_ctx(cid, {
                "kind": "clarify_project", "intent": Intent.MARK, "target_stage": target,
                "names": names, "is_pronoun": is_pronoun,
                "options": clar["clarification"]["options"],
            }),
        )
    if proj is None:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.MARK,
            message=f'I couldn\'t find an active project matching "{project_text}".',
        )

    return await _mark_plan_for_project(cid, proj, target, names, is_pronoun, carry)


async def _mark_plan_for_project(
    cid: str, proj: dict, target: str, names: List[str], is_pronoun: bool, carry: Optional[dict],
) -> dict:
    cands = await _pipeline_candidates(proj["id"])
    if not cands:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.MARK,
            message=f'No talents are in the {proj["label"]} pipeline yet.',
        )

    resolved: List[nlu.Candidate] = []
    unresolved: List[str] = []

    if is_pronoun:
        by_id = {c.id: c for c in cands}
        for tid in (carry or {}).get("last_talent_ids", []):
            if tid in by_id:
                resolved.append(by_id[tid])
    else:
        for nm in names:
            r = _resolve_one_name(nm, cands)
            if r.ok and r.talent_ids:
                for tid, lbl in zip(r.talent_ids, r.talent_labels):
                    match = next((c for c in cands if c.id == tid), nlu.Candidate(id=tid, label=lbl))
                    resolved.append(match)
            elif r.ambiguous_candidates:
                opts = _cand_options(r.ambiguous_candidates)
                return _envelope(
                    conversation_id=cid, state="clarification", intent=Intent.MARK,
                    message=f'I found {len(opts)} talents named "{nm}" in {proj["label"]}:',
                    clarification={"kind": "talent", "prompt": "Which one do you mean?", "options": opts},
                    context=_ctx(cid, {
                        "kind": "clarify_talent", "intent": Intent.MARK, "target_stage": target,
                        "project": proj, "resolved_ids": [c.id for c in resolved],
                        "pending_names": names[names.index(nm):], "options": opts,
                    }),
                )
            else:
                unresolved.append(nm)

    # dedupe
    seen: set = set()
    dedup: List[nlu.Candidate] = []
    for c in resolved:
        if c.id not in seen:
            seen.add(c.id)
            dedup.append(c)
    resolved = dedup

    if not resolved:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.MARK,
            message=f'None of those talents are in the {proj["label"]} pipeline: {", ".join(unresolved) or "—"}.',
        )

    return _build_mark_plan(cid, proj, target, resolved, unresolved)


def _build_mark_plan(
    cid: str, proj: dict, target: str, resolved: List[nlu.Candidate], unresolved: List[str],
) -> dict:
    changes: List[ProposedChange] = []
    warnings: List[str] = []
    for c in resolved:
        cur = c.stage
        if cur == target:
            warnings.append(f"{c.label} is already {STAGE_LABELS.get(target, target)} — no change.")
            continue
        if cur == "locked":
            warnings.append(
                f"{c.label} is currently Locked — moving them to "
                f"{STAGE_LABELS.get(target, target)} would remove them from the locked set."
            )
        changes.append(ProposedChange(
            op="update", entity_type="talent", entity_id=c.id, entity_label=c.label,
            field="pipeline_stage",
            current_value=cur, current_label=(STAGE_LABELS.get(cur, cur) if cur else "—"),
            proposed_value=target, proposed_label=STAGE_LABELS.get(target, target),
        ))
    for nm in unresolved:
        warnings.append(f'Couldn\'t find "{nm}" in {proj["label"]} — skipped.')

    n = len(changes)
    if n == 0:
        plan = ActionPlan(
            intent=Intent.MARK, project={"id": proj["id"], "label": proj["label"]},
            summary=f"Nothing to change in {proj['label']}.",
            changes=[], warnings=warnings, requires_confirmation=False,
        )
        return _envelope(
            conversation_id=cid, state="preview", intent=Intent.MARK,
            message=plan.summary, plan=plan.to_dict(), requires_confirmation=False,
            context=_ctx(cid, {"kind": "note", "last_talent_ids": [c.id for c in resolved]}),
        )

    tgt_label = STAGE_LABELS.get(target, target)
    summary = (
        f"I would move {n} talent{'s' if n != 1 else ''} to {tgt_label} in {proj['label']}."
    )
    plan = ActionPlan(
        intent=Intent.MARK, project={"id": proj["id"], "label": proj["label"]},
        summary=summary, changes=changes, warnings=warnings, requires_confirmation=True,
        executable=True,
    )
    plan_d = plan.to_dict()
    return _envelope(
        conversation_id=cid, state="preview", intent=Intent.MARK,
        message=summary + " Ready to make this change?",
        plan=plan_d, requires_confirmation=True,
        context=_ctx(cid, {
            "kind": "confirm_plan", "plan": plan_d,
            "last_talent_ids": [c.id for c in resolved],
        }),
    )


# --------------------------------------------------------------------------
# pipeline_add_remove
# --------------------------------------------------------------------------
def _extract_add_remove(message: str) -> Tuple[List[str], List[str], Optional[str]]:
    project_text = _extract_scope(message)
    work = message
    if project_text:
        work = _SCOPE_RE_FOR.sub(" ", work)
    work = re.sub(r"[.?!;:]", " ", work)

    adds: List[str] = []
    removes: List[str] = []
    rm_m = _REMOVE_RE.search(work)
    add_m = _ADD_RE.search(work)

    if add_m and rm_m:
        if add_m.start() < rm_m.start():
            add_part = work[add_m.end():rm_m.start()]
            rm_part = work[rm_m.end():]
        else:
            rm_part = work[rm_m.end():add_m.start()]
            add_part = work[add_m.end():]
    elif add_m:
        add_part, rm_part = work[add_m.end():], ""
    elif rm_m:
        add_part, rm_part = "", work[rm_m.end():]
    else:
        add_part, rm_part = "", ""

    def _names(part: str) -> List[str]:
        part = re.sub(r"\b(?:and|from|to|the|please)\b", ",", part, flags=re.I)
        return [n.strip() for n in nlu.split_multi_names(part) if _looks_like_name(n)]

    adds = _names(add_part)
    removes = _names(rm_part)
    return adds, removes, project_text


async def _handle_add_remove(cid: str, message: str, user: dict) -> dict:
    adds, removes, project_text = _extract_add_remove(message)
    if not adds and not removes:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.ADD_REMOVE,
            message='Tell me who to add or remove and from which project, e.g. '
                    '"Add Neha and remove Riya from Google AI".',
        )

    projects = await _active_projects()
    proj, clar = _match_project(project_text, projects)
    if clar is not None:
        return _envelope(
            conversation_id=cid, state="clarification", intent=Intent.ADD_REMOVE,
            message=clar["message"], clarification=clar["clarification"],
            context=_ctx(cid, {
                "kind": "clarify_project", "intent": Intent.ADD_REMOVE,
                "adds": adds, "removes": removes,
                "options": clar["clarification"]["options"],
            }),
        )
    if proj is None:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.ADD_REMOVE,
            message=f'I couldn\'t find an active project matching "{project_text}".',
        )

    return await _add_remove_plan(cid, proj, adds, removes)


async def _add_remove_plan(
    cid: str, proj: dict, adds: List[str], removes: List[str],
    *, forced: Optional[Dict[str, str]] = None,
) -> dict:
    pipeline = await _pipeline_candidates(proj["id"])
    roster = await _global_talent_candidates()
    forced = forced or {}  # normalized name -> talent_id (from a resolved clarification)

    def _resolve(nm: str, cands: List[nlu.Candidate]):
        tid = forced.get(nm.strip().lower())
        if tid:
            hit = next((c for c in cands if c.id == tid), None)
            if hit:
                return nlu.ResolvedTalents(ok=True, talent_ids=[hit.id], talent_labels=[hit.label])
        return _resolve_one_name(nm, cands)

    changes: List[ProposedChange] = []
    warnings: List[str] = []

    for nm in removes:
        r = _resolve(nm, pipeline)
        if r.ok and r.talent_ids:
            for tid, lbl in zip(r.talent_ids, r.talent_labels):
                cur = next((c.stage for c in pipeline if c.id == tid), None)
                changes.append(ProposedChange(
                    op="remove", entity_type="talent", entity_id=tid, entity_label=lbl,
                    field="pipeline_membership",
                    current_value=cur, current_label=(STAGE_LABELS.get(cur, cur) if cur else "In pipeline"),
                    proposed_value=None, proposed_label="Removed from pipeline",
                ))
        elif r.ambiguous_candidates:
            opts = _cand_options(r.ambiguous_candidates)
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.ADD_REMOVE,
                message=f'I found {len(opts)} talents named "{nm}" in {proj["label"]}:',
                clarification={"kind": "talent", "prompt": "Which one to remove?", "options": opts},
                context=_ctx(cid, {
                    "kind": "clarify_talent", "intent": Intent.ADD_REMOVE, "sub": "remove",
                    "project": proj, "adds": adds, "removes": removes,
                    "resolved_remove_ids": [c.entity_id for c in changes if c.op == "remove"],
                    "pending_names": removes[removes.index(nm):], "options": opts,
                }),
            )
        else:
            warnings.append(f'"{nm}" isn\'t in the {proj["label"]} pipeline — nothing to remove.')

    pipeline_ids = {c.id for c in pipeline}
    for nm in adds:
        r = _resolve(nm, roster)
        if r.ok and r.talent_ids:
            tid, lbl = r.talent_ids[0], r.talent_labels[0]
            if tid in pipeline_ids:
                cur = next((c.stage for c in pipeline if c.id == tid), None)
                warnings.append(
                    f"{lbl} is already in the {proj['label']} pipeline"
                    f"{f' ({STAGE_LABELS.get(cur, cur)})' if cur else ''} — would be left as-is."
                )
                continue
            changes.append(ProposedChange(
                op="add", entity_type="talent", entity_id=tid, entity_label=lbl,
                field="pipeline_membership",
                current_value=None, current_label="Not in pipeline",
                proposed_value="ask_to_test", proposed_label=STAGE_LABELS.get("ask_to_test", "Ask to test"),
            ))
        elif r.ambiguous_candidates:
            opts = _cand_options(r.ambiguous_candidates)
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.ADD_REMOVE,
                message=f'I found {len(opts)} talents named "{nm}":',
                clarification={"kind": "talent", "prompt": "Which one to add?", "options": opts},
                context=_ctx(cid, {
                    "kind": "clarify_talent", "intent": Intent.ADD_REMOVE, "sub": "add",
                    "project": proj, "adds": adds, "removes": removes,
                    "resolved_add_ids": [c.entity_id for c in changes if c.op == "add"],
                    "resolved_remove_ids": [c.entity_id for c in changes if c.op == "remove"],
                    "pending_names": adds[adds.index(nm):], "options": opts,
                }),
            )
        else:
            warnings.append(f'Couldn\'t find a talent named "{nm}" — skipped.')

    if not changes:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.ADD_REMOVE,
            message="Nothing to change — " + (" ".join(warnings) or "no matching talents."),
        )

    n_add = sum(1 for c in changes if c.op == "add")
    n_rm = sum(1 for c in changes if c.op == "remove")
    bits = []
    if n_add:
        bits.append(f"add {n_add}")
    if n_rm:
        bits.append(f"remove {n_rm}")
    summary = f"In {proj['label']}, I would {' and '.join(bits)} talent{'s' if (n_add + n_rm) != 1 else ''}."
    plan = ActionPlan(
        intent=Intent.ADD_REMOVE, project={"id": proj["id"], "label": proj["label"]},
        summary=summary, changes=changes, warnings=warnings, requires_confirmation=True,
        executable=True,
    )
    plan_d = plan.to_dict()
    return _envelope(
        conversation_id=cid, state="preview", intent=Intent.ADD_REMOVE,
        message=summary + " Ready to make this change?",
        plan=plan_d, requires_confirmation=True,
        context=_ctx(cid, {"kind": "confirm_plan", "plan": plan_d}),
    )


# --------------------------------------------------------------------------
# create_project  (deterministic field extraction, no LLM)
# --------------------------------------------------------------------------
_FIELD_PATTERNS = {
    "brand_name": r"(?:client|brand|company|for)\s*[:\-]?\s*(.+)",
    "character": r"(?:character|role|looking\s+for|requirement)\s*[:\-]?\s*(.+)",
    "shoot_dates": r"(?:shoot(?:\s+date)?s?|dates?|shooting)\s*[:\-]?\s*(.+)",
    "budget_per_day": r"(?:budget|fee|rate|pay)\s*[:\-]?\s*(.+)",
    "medium_usage": r"(?:usage|medium|rights?)\s*[:\-]?\s*(.+)",
    "location": r"(?:location|city|place|based\s+in|in)\s*[:\-]?\s*(.+)",
    "director": r"(?:director|dop)\s*[:\-]?\s*(.+)",
    "production_house": r"(?:production\s+house|production|prod)\s*[:\-]?\s*(.+)",
}
_FIELD_LABELS = {
    "brand_name": "Client / brand", "character": "Character / requirement",
    "shoot_dates": "Shoot dates", "budget_per_day": "Budget", "medium_usage": "Usage",
    "location": "Location", "director": "Director", "production_house": "Production house",
}


def _handle_create_project(cid: str, message: str) -> dict:
    brief = re.sub(_CREATE_PROJECT_RE, " ", message, count=1)
    brief = re.sub(r"^\s*(?:from\s+(?:this\s+)?brief|here'?s?\s+(?:the|a)\s+brief)\s*[:\-]?\s*", "", brief, flags=re.I)
    brief = brief.strip()

    fields: Dict[str, str] = {}
    for line in re.split(r"[\n;]|(?<=[.])\s+", brief):
        line = line.strip(" .\t-")
        if not line:
            continue
        for key, pat in _FIELD_PATTERNS.items():
            if key in fields:
                continue
            m = re.match(pat, line, re.I)
            if m:
                val = m.group(1).strip(" .\t-,")
                if val and val.lower() not in _STOPWORDS:
                    fields[key] = val
                break

    missing = [_FIELD_LABELS[k] for k in ("brand_name", "shoot_dates", "budget_per_day") if k not in fields]
    warnings = []
    if missing:
        warnings.append("Not found in the brief: " + ", ".join(missing) + " — you'd be asked for these.")
    if not fields:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.CREATE_PROJECT,
            message="I couldn't pull structured details out of that brief. Try labelled lines, e.g. "
                    "\"Client: XYZ\", \"Shoot: 18 Sep\", \"Budget: 25000\", \"Location: Mumbai\".",
        )

    display = {_FIELD_LABELS[k]: v for k, v in fields.items()}
    change = ProposedChange(
        op="create", entity_type="project",
        entity_label=fields.get("brand_name") or "New project",
        proposed_label="New project",
        fields=display,
        note="A normal Talentgram project would be created with these details.",
    )
    plan = ActionPlan(
        intent=Intent.CREATE_PROJECT, summary="I would create this project.",
        changes=[change], warnings=warnings, requires_confirmation=True,
    )
    return _envelope(
        conversation_id=cid, state="preview", intent=Intent.CREATE_PROJECT,
        message="Here's what I understood. No project has been created.",
        plan=plan.to_dict(), requires_confirmation=True,
        context=_ctx(cid, {"kind": "confirm_plan", "plan": plan.to_dict()}),
    )


# --------------------------------------------------------------------------
# read_query  (answers from the existing read-only overview)
# --------------------------------------------------------------------------
async def _handle_read(cid: str, message: str, user: dict) -> dict:
    overview = await build_overview(user)
    projects = overview["projects"]
    summ = overview["summary"]

    if _PIPELINE_RE.search(message) and not _PRIORITIES_RE.search(message) and not _TESTS_RE.search(message):
        scope = _extract_scope(message) or re.sub(
            r"\b(?:show\s+me|show|the|pipeline|for|please|what'?s?\s+in)\b", " ", message, flags=re.I
        ).strip(" .?-")
        opts = [{"index": i + 1, "id": p["id"], "label": p["name"]} for i, p in enumerate(projects)]
        match = nlu.resolve_project_by_name(scope, [{"id": p["id"], "label": p["name"]} for p in projects])
        if match.project:
            p = next(pp for pp in projects if pp["id"] == match.project["id"])
            return _envelope(
                conversation_id=cid, state="answer", intent=Intent.READ,
                message=f"{p['name']} — {p['pipeline_total']} talents in the pipeline.",
                answer={"kind": "project_pipeline", "project": p},
            )
        if match.ambiguous or match.suggestions:
            cands = match.ambiguous or match.suggestions
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.READ,
                message=f'I found {len(cands)} projects matching "{scope}":',
                clarification={"kind": "project", "prompt": "Which project do you mean?",
                               "options": [{"index": i + 1, "id": c["id"], "label": c["label"]}
                                           for i, c in enumerate(cands)]},
                context=_ctx(cid, {"kind": "clarify_project", "intent": Intent.READ, "sub": "pipeline",
                                   "options": [{"index": i + 1, "id": c["id"], "label": c["label"]}
                                               for i, c in enumerate(cands)]}),
            )
        return _envelope(
            conversation_id=cid, state="clarification", intent=Intent.READ,
            message="Which project's pipeline?",
            clarification={"kind": "project", "prompt": "Pick a project", "options": opts},
            context=_ctx(cid, {"kind": "clarify_project", "intent": Intent.READ, "sub": "pipeline", "options": opts}),
        )

    if _TESTS_RE.search(message):
        rows = []
        for p in projects:
            for t in p["talents"]:
                if t["test_status"] == "received":
                    rows.append({"talent": t["name"], "project": p["name"], "stage": t["stage_label"]})
        return _envelope(
            conversation_id=cid, state="answer", intent=Intent.READ,
            message=f"{len(rows)} talent{'s' if len(rows) != 1 else ''} across your projects have tests received."
                    + (" (Showing what's loaded in the overview.)" if any(p["talents_truncated"] for p in projects) else ""),
            answer={"kind": "tests_received", "rows": rows, "total": summ["tests_received"]},
        )

    if _PRIORITIES_RE.search(message):
        rows = [
            {"project": p["name"], "follow_ups": p["follow_up_count"],
             "tests_received": p["tests_received"], "pipeline_total": p["pipeline_total"]}
            for p in projects if p["follow_up_count"] or p["tests_received"]
        ]
        rows.sort(key=lambda r: (r["follow_ups"], r["tests_received"]), reverse=True)
        return _envelope(
            conversation_id=cid, state="answer", intent=Intent.READ,
            message=(f"{summ['follow_ups']} talents are awaiting follow-up and "
                     f"{summ['tests_received']} tests have come in across {summ['active_projects']} active projects."),
            answer={"kind": "priorities", "rows": rows, "summary": summ},
        )

    # default: counts
    return _envelope(
        conversation_id=cid, state="answer", intent=Intent.READ,
        message=(f"You have {summ['active_projects']} active project"
                 f"{'s' if summ['active_projects'] != 1 else ''}, "
                 f"{summ['talents_in_pipeline']} talents in pipelines, "
                 f"{summ['tests_received']} tests received, "
                 f"{summ['follow_ups']} awaiting follow-up."),
        answer={"kind": "counts", "summary": summ,
                "projects": [{"id": p["id"], "name": p["name"], "status": p["status"],
                              "pipeline_total": p["pipeline_total"]} for p in projects]},
    )


# --------------------------------------------------------------------------
# Clarification resume
# --------------------------------------------------------------------------
def _match_project(project_text: Optional[str], projects: List[Dict[str, str]]):
    """→ (resolved_project|None, clarification_dict|None). When both are
    None the caller shows a not-found blocked message."""
    if not projects:
        return None, None
    if not project_text:
        opts = [{"index": i + 1, "id": p["id"], "label": p["label"]} for i, p in enumerate(projects)]
        if len(opts) <= _MAX_INLINE_PROJECT_OPTIONS:
            return None, {
                "message": "Which project?",
                "clarification": {"kind": "project", "prompt": "Pick a project", "options": opts},
            }
        return None, {
            "message": "Which project? Name it — e.g. \"...for Google AI\".",
            "clarification": {"kind": "project", "prompt": "Name the project", "options": opts[:_MAX_INLINE_PROJECT_OPTIONS]},
        }
    m = nlu.resolve_project_by_name(project_text, projects)
    if m.project:
        return m.project, None
    cands = m.ambiguous or m.suggestions
    if cands:
        opts = [{"index": i + 1, "id": c["id"], "label": c["label"]} for i, c in enumerate(cands)]
        return None, {
            "message": f'I found {len(opts)} projects matching "{project_text}":',
            "clarification": {"kind": "project", "prompt": "Which project do you mean?", "options": opts},
        }
    return None, None


# --------------------------------------------------------------------------
# communication (Phase 4A) — resolution + preview, external send in comm_execute
# --------------------------------------------------------------------------
async def _handle_comm(cid: str, message: str, user: dict) -> dict:
    from simple_assistant import comm

    parsed = comm.parse_comm(message)
    if parsed is None:
        return _envelope(conversation_id=cid, state="blocked", intent=Intent.COMM, message=UNKNOWN_HELP)
    if parsed.bulk:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.COMM,
            message="Bulk project messaging isn't enabled yet. Ask me to send to one talent at a time.",
        )
    if not parsed.talent_text:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.COMM,
            message='Which talent? e.g. "Send Neha\'s intro to the Google AI casting group".',
        )
    return await _resolve_and_preview_comm(cid, comm.parsed_to_dict(parsed), user, None, None)


async def _resolve_and_preview_comm(
    cid: str, pd: dict, user: dict, forced_talent: Optional[dict], forced_project: Optional[dict],
    *, forced_destination: Optional[dict] = None,
) -> dict:
    from simple_assistant import comm

    parsed = comm.parsed_from_dict(pd)

    # talent
    if forced_talent:
        talent = forced_talent
        candidate_ids = [forced_talent["id"]]
    else:
        roster = await _global_talent_candidates()
        r = _resolve_one_name(parsed.talent_text, roster)
        if r.ambiguous_candidates:
            opts = _cand_options(r.ambiguous_candidates)
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.COMM,
                message=f'I found {len(opts)} talents named "{parsed.talent_text}":',
                clarification={"kind": "talent", "prompt": "Which one do you mean?", "options": opts},
                context=_ctx(cid, {"kind": "clarify_comm_talent", "intent": Intent.COMM,
                                   "parsed": pd, "options": opts,
                                   "project": forced_project}),
            )
        if not (r.ok and r.talent_ids):
            return _envelope(conversation_id=cid, state="blocked", intent=Intent.COMM,
                             message=r.error or f'I couldn\'t find a talent named "{parsed.talent_text}".')
        talent = {"id": r.talent_ids[0], "label": r.talent_labels[0]}
        candidate_ids = list(r.talent_ids)

    # project
    project = forced_project
    if project is None and parsed.project_text:
        projects = await _active_projects()
        proj, clar = _match_project(parsed.project_text, projects)
        if clar is not None:
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.COMM,
                message=clar["message"], clarification=clar["clarification"],
                context=_ctx(cid, {"kind": "clarify_comm_project", "intent": Intent.COMM,
                                   "parsed": pd, "talent": talent, "candidate_ids": candidate_ids,
                                   "options": clar["clarification"]["options"]}),
            )
        project = proj
    if project is None and parsed.project_text is None and parsed.kind != "send_project_info":
        projects = await _active_projects()
        opts = [{"index": i + 1, "id": p["id"], "label": p["label"]} for i, p in enumerate(projects)]
        return _envelope(
            conversation_id=cid, state="clarification", intent=Intent.COMM,
            message="Which project?",
            clarification={"kind": "project", "prompt": "Pick a project", "options": opts},
            context=_ctx(cid, {"kind": "clarify_comm_project", "intent": Intent.COMM,
                               "parsed": pd, "talent": talent, "candidate_ids": candidate_ids, "options": opts}),
        )

    res = await comm.build_plan(
        parsed=parsed, talent=talent,
        project_id=(project or {}).get("id"), project_label=(project or {}).get("label"),
        talent_candidate_ids=candidate_ids,
        forced_destination=forced_destination,
    )
    if res.error:
        return _envelope(conversation_id=cid, state="blocked", intent=Intent.COMM, message=res.error)

    plan_d = res.plan.to_dict()
    if res.clarification:
        kind = res.clarification.get("kind")
        pending_kind = "clarify_comm_destination" if kind == "destination" else "comm_media_partial"
        extra = {}
        if pending_kind == "clarify_comm_destination":
            # carry what build_plan needs to rebuild with the chosen destination
            extra = {"parsed": pd, "talent": talent,
                     "project": {"id": (project or {}).get("id"), "label": (project or {}).get("label")},
                     "candidate_ids": candidate_ids}
        else:
            extra = {"comm_plan": plan_d}
        return _envelope(
            conversation_id=cid, state="clarification", intent=Intent.COMM,
            message=res.clarification["prompt"], clarification=res.clarification, comm=plan_d,
            context=_ctx(cid, {"kind": pending_kind, "intent": Intent.COMM,
                               "options": res.clarification["options"], **extra}),
        )

    return _comm_preview_envelope(cid, res.plan)


def _comm_preview_envelope(cid: str, plan) -> dict:
    plan_d = plan.to_dict()
    verb = {
        "send_media": "send as WhatsApp media",
        "send_profile_link": "send the profile link",
        "send_project_info": "send the project details",
        "attach_media": "prepare (preview only)",
    }.get(plan.action_type, "send")
    msg = f"I would {verb} to {plan.destination_label or plan.destination or 'the destination'}. Nothing has been sent yet."
    if not plan.executable:
        msg = (plan.warnings[-1] if plan.warnings else msg)
    return _envelope(
        conversation_id=cid, state="preview", intent=Intent.COMM, message=msg,
        comm=plan_d, requires_confirmation=plan.requires_confirmation and plan.executable,
        context=_ctx(cid, {"kind": "confirm_comm", "comm_plan": plan_d}) if plan.executable else None,
    )


async def _resume_comm(cid: str, message: str, pending: dict, user: dict) -> Optional[dict]:
    options = pending.get("options") or []
    idx = nlu.resolve_option_reply(message, options)
    if idx is None:
        return None  # not a pick — treat as fresh command
    picked = options[idx - 1]
    pd = pending.get("parsed")

    if pending["kind"] == "clarify_comm_talent":
        talent = {"id": picked["id"], "label": picked.get("name") or picked["label"]}
        return await _resolve_and_preview_comm(cid, pd, user, talent, pending.get("project"))
    if pending["kind"] == "clarify_comm_project":
        project = {"id": picked["id"], "label": picked["label"]}
        return await _resolve_and_preview_comm(cid, pd, user, pending.get("talent"), project)
    if pending["kind"] == "clarify_comm_destination":
        forced = {
            "destination": picked["destination"],
            "destination_type": picked["destination_type"],
            "destination_label": picked["destination_label"],
            "destination_source": picked["destination_source"],
        }
        return await _resolve_and_preview_comm(
            cid, pd, user, pending.get("talent"), pending.get("project"), forced_destination=forced,
        )
    if pending["kind"] == "comm_media_partial":
        if picked["id"] == "cancel":
            return _envelope(conversation_id=cid, state="cancelled", intent=Intent.COMM,
                             message="Cancelled. Nothing was sent.")
        # "send only what's available" — the comm_plan already lists only the
        # resolved media; promote it to executable.
        from simple_assistant.comm_plan import CommunicationPlan, MediaRef
        cp = pending["comm_plan"]
        plan = CommunicationPlan(
            action_type=cp["action_type"], talent=cp["talent"], project=cp["project"],
            submission_id=cp.get("submission_id"), destination=cp["destination"],
            destination_type=cp["destination_type"], destination_label=cp["destination_label"],
            media=[MediaRef(**{k: m.get(k) for k in ("media_id", "category", "label", "url", "poster_url", "kind", "submission_id")}) for m in cp["media"]],
            message=cp.get("message"), warnings=["Sending only the available media, as confirmed."],
            executable=True,
        )
        return _comm_preview_envelope(cid, plan)
    return None


# --------------------------------------------------------------------------
# submission preparation / incoming audition (Phase 4B)
# --------------------------------------------------------------------------
async def _handle_submission(cid: str, message: str, user: dict, *, file_meta: Optional[dict] = None) -> dict:
    from simple_assistant import submission as sub_mod
    from simple_assistant.submission_plan import INGEST

    parsed = sub_mod.parse_submission(message)
    if parsed is None:
        return _envelope(conversation_id=cid, state="blocked", intent=Intent.SUBMISSION, message=UNKNOWN_HELP)
    # A file on the wire is unambiguously an ingest of a new audition file.
    if file_meta:
        parsed.kind = INGEST
    if parsed.kind == INGEST and not file_meta:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.SUBMISSION,
            message=("I don't have a file attached to this command. "
                     "Attach the audition video first, then ask me again."),
        )
    if not parsed.talent_text:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.SUBMISSION,
            message='Which talent? e.g. "Prepare Ahana\'s Google AI submission".',
        )
    pd = {k: getattr(parsed, k) for k in sub_mod.ParsedSub.__slots__}
    return await _resolve_and_preview_submission(cid, pd, user, None, None, file_meta=file_meta)


def _parsed_sub(pd: dict):
    from simple_assistant import submission as sub_mod

    p = sub_mod.ParsedSub()
    for k in sub_mod.ParsedSub.__slots__:
        setattr(p, k, pd.get(k))
    p.media_terms = pd.get("media_terms") or []
    return p


async def _resolve_and_preview_submission(
    cid: str, pd: dict, user: dict, forced_talent: Optional[dict], forced_project: Optional[dict],
    *, file_meta: Optional[dict] = None,
) -> dict:
    from simple_assistant import submission as sub_mod

    parsed = _parsed_sub(pd)

    if forced_talent:
        talent = forced_talent
        candidate_ids = [forced_talent["id"]]
    else:
        roster = await _global_talent_candidates()
        r = _resolve_one_name(parsed.talent_text, roster)
        if r.ambiguous_candidates:
            # A same-name tie is NOT clarified here — Phase 4B's whole point
            # (STEP 2) is to let the authoritative-submission tie-break decide
            # which record actually owns this project's submission. Only if
            # THAT is also ambiguous does build_plan surface a real error.
            candidate_ids = [c.id for c in r.ambiguous_candidates]
            talent = {"id": candidate_ids[0], "label": r.ambiguous_candidates[0].label}
        elif r.ok and r.talent_ids:
            talent = {"id": r.talent_ids[0], "label": r.talent_labels[0]}
            candidate_ids = list(r.talent_ids)
        else:
            return _envelope(conversation_id=cid, state="blocked", intent=Intent.SUBMISSION,
                             message=r.error or f'I couldn\'t find a talent named "{parsed.talent_text}".')

    project = forced_project
    if project is None:
        projects = await _active_projects()
        if parsed.project_text:
            proj, clar = _match_project(parsed.project_text, projects)
            if clar is not None:
                return _envelope(
                    conversation_id=cid, state="clarification", intent=Intent.SUBMISSION,
                    message=clar["message"], clarification=clar["clarification"],
                    context=_ctx(cid, {"kind": "clarify_sub_project", "intent": Intent.SUBMISSION,
                                       "parsed": pd, "talent": talent, "candidate_ids": candidate_ids,
                                       "file_meta": file_meta,
                                       "options": clar["clarification"]["options"]}),
                )
            project = proj
        if project is None:
            # "Ahana has sent her test" with no project — offer the projects she's in
            pipe = await RDB.casting_pipeline.find(
                {"talent_id": {"$in": candidate_ids}}, {"_id": 0, "project_id": 1}
            ).to_list(50)
            pids = {r["project_id"] for r in pipe}
            active = [p for p in projects if p["id"] in pids] or projects
            opts = [{"index": i + 1, "id": p["id"], "label": p["label"]} for i, p in enumerate(active)]
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.SUBMISSION,
                message="Which project's submission?",
                clarification={"kind": "project", "prompt": "Pick a project", "options": opts},
                context=_ctx(cid, {"kind": "clarify_sub_project", "intent": Intent.SUBMISSION,
                                   "parsed": pd, "talent": talent, "candidate_ids": candidate_ids,
                                   "file_meta": file_meta, "options": opts}),
            )

    res = await sub_mod.build_plan(
        parsed=parsed, talent=talent,
        project_id=project["id"], project_label=project["label"],
        talent_candidate_ids=candidate_ids,
        file_meta=file_meta,
    )
    if res.error:
        return _envelope(conversation_id=cid, state="blocked", intent=Intent.SUBMISSION, message=res.error)
    return _submission_preview_envelope(cid, res.plan)


def _submission_preview_envelope(cid: str, plan) -> dict:
    plan_d = plan.to_dict()
    if plan.action_type == "ingest_audition":
        up = plan.upload or {}
        if not plan.executable:
            msg = "I can't upload that yet. " + " ".join(plan.warnings)
            return _envelope(conversation_id=cid, state="preview", intent=Intent.SUBMISSION,
                             message=msg.strip(), sub=plan_d, requires_confirmation=False)
        verb = "replace the" if up.get("replaces") else "add it as"
        return _envelope(
            conversation_id=cid, state="preview", intent=Intent.SUBMISSION,
            message=(f"Incoming audition for {plan.talent['label']} — {plan.project['label']}. "
                     f'I would {verb} {up.get("target_label")} on the submission. '
                     "Nothing has been uploaded yet."),
            sub=plan_d, requires_confirmation=True,
            context=_ctx(cid, {"kind": "confirm_upload", "sub_plan": plan_d}),
        )
    if plan.action_type == "inspect_submission":
        n_missing = len(plan.missing)
        msg = (
            f"{plan.talent['label']} — {plan.project['label']}. "
            f"Submission {plan.submission_status}. "
            + (f"{n_missing} item{'s' if n_missing != 1 else ''} still missing." if n_missing else "Nothing missing.")
        )
        return _envelope(conversation_id=cid, state="preview", intent=Intent.SUBMISSION, message=msg,
                         sub=plan_d, requires_confirmation=False)
    if not plan.proposed:
        msg = "Nothing to attach. " + " ".join(plan.warnings)
        return _envelope(conversation_id=cid, state="preview", intent=Intent.SUBMISSION,
                         message=msg.strip(), sub=plan_d, requires_confirmation=False)
    labels = ", ".join(m.label for m in plan.proposed)
    return _envelope(
        conversation_id=cid, state="preview", intent=Intent.SUBMISSION,
        message=f"I would attach {labels} to {plan.talent['label']}'s {plan.project['label']} submission. "
                "No changes have been made.",
        sub=plan_d, requires_confirmation=True,
        context=_ctx(cid, {"kind": "confirm_submission", "sub_plan": plan_d}),
    )


async def _resume_submission(
    cid: str, message: str, pending: dict, user: dict, *, file_meta: Optional[dict] = None,
) -> Optional[dict]:
    options = pending.get("options") or []
    idx = nlu.resolve_option_reply(message, options)
    if idx is None:
        return None
    picked = options[idx - 1]
    pd = pending.get("parsed")
    # the file's metadata was pinned into the signed clarification context;
    # a fresh upload of the same file overrides it.
    fm = file_meta or pending.get("file_meta")
    if pending["kind"] == "clarify_sub_talent":
        talent = {"id": picked["id"], "label": picked.get("name") or picked["label"]}
        return await _resolve_and_preview_submission(cid, pd, user, talent, pending.get("project"), file_meta=fm)
    if pending["kind"] == "clarify_sub_project":
        project = {"id": picked["id"], "label": picked["label"]}
        return await _resolve_and_preview_submission(cid, pd, user, pending.get("talent"), project, file_meta=fm)
    return None


# --------------------------------------------------------------------------
# WhatsApp delivery-status inspection (Phase 6) — READ-ONLY
# --------------------------------------------------------------------------
async def _handle_status(cid: str, message: str, user: dict) -> dict:
    from simple_assistant import audit
    from simple_assistant import status as sa_status

    # "Retry it." — explicitly not enabled; never creates a job.
    if sa_status.is_retry_request(message) and not sa_status.is_status_query(message):
        await audit.record_status(conversation_id=cid, user=user, query_kind="retry_blocked",
                                  result="retry not enabled")
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.STATUS,
            message="Retry isn't enabled in this phase — I didn't create a new WhatsApp job. "
                    "Ask me for the send's status instead.",
        )

    from simple_assistant import inbound_intelligence as sa_intel

    talent_text, project_text, inbound = sa_status.parse_status(message)

    # Phase 12 — read-only "show me X's recent conversation about Y"
    if sa_intel.is_conversation_query(message):
        i_talent, _hint = sa_intel.parse_intelligence(message)
        return await _resolve_and_show_conversation(
            cid, user, i_talent or talent_text, project_text)

    if sa_intel.is_draft_request(message):
        i_talent, _hint = sa_intel.parse_intelligence(message)
        return await _resolve_and_analyze_inbound(
            cid, user, i_talent or talent_text, project_text, message, draft=True)

    if sa_intel.is_intelligence_query(message):
        i_talent, _hint = sa_intel.parse_intelligence(message)
        return await _resolve_and_analyze_inbound(
            cid, user, i_talent or talent_text, project_text, message)

    if inbound:
        return await _resolve_and_show_inbound(
            cid, user, talent_text, project_text, sa_status.wants_unanswered(message))

    return await _resolve_and_show_status(cid, user, talent_text, project_text, None, None)


async def _resolve_and_show_inbound(
    cid: str, user: dict, talent_text: Optional[str], project_text: Optional[str], want_unanswered: bool,
    *, forced_talent: Optional[dict] = None, forced_project: Optional[dict] = None,
) -> dict:
    """Phase 7 — READ-ONLY inbound WhatsApp reply inspection. Never replies,
    interprets, or mutates. Reads backend/inbound_messages.py."""
    import inbound_messages
    from simple_assistant import audit
    from simple_assistant import status as sa_status

    talent = forced_talent
    if talent is None and talent_text:
        roster = await _global_talent_candidates()
        r = _resolve_one_name(talent_text, roster)
        if r.ambiguous_candidates:
            opts = _cand_options(r.ambiguous_candidates)
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.STATUS,
                message=f'I found {len(opts)} talents named "{talent_text}":',
                clarification={"kind": "talent", "prompt": "Which one?", "options": opts},
                context=_ctx(cid, {"kind": "clarify_status_talent", "intent": Intent.STATUS,
                                   "talent_text": talent_text, "project_text": project_text,
                                   "project": forced_project, "options": opts, "inbound": True}),
            )
        if r.ok and r.talent_ids:
            talent = {"id": r.talent_ids[0], "label": r.talent_labels[0]}

    project = forced_project
    if project is None and project_text:
        proj, clar = _match_project(project_text, await _active_projects())
        if clar is not None:
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.STATUS,
                message=clar["message"], clarification=clar["clarification"],
                context=_ctx(cid, {"kind": "clarify_status_project", "intent": Intent.STATUS,
                                   "talent_text": talent_text, "talent": talent,
                                   "options": clar["clarification"]["options"], "inbound": True,
                                   "want_unanswered": want_unanswered}),
            )
        project = proj

    await audit.record_status(
        conversation_id=cid, user=user, query_kind="inbound",
        talent_id=(talent or {}).get("id"), talent_label=(talent or {}).get("label"),
        project_id=(project or {}).get("id"), project_label=(project or {}).get("label"),
        result="unanswered" if want_unanswered else "read",
    )

    # "unanswered replies for <project>"
    if want_unanswered and project:
        msgs = await inbound_messages.unanswered_for_project(project["id"])
        cards = [await sa_status.build_inbound_card(m) for m in msgs]
        if not cards:
            return _envelope(conversation_id=cid, state="answer", intent=Intent.STATUS,
                             message=f"I don't see any inbound replies for {project['label']} that are still awaiting a Simple Assistant send.")
        return _envelope(
            conversation_id=cid, state="answer", intent=Intent.STATUS,
            message=f"{len(cards)} inbound repl{'y' if len(cards) == 1 else 'ies'} for {project['label']} with no Simple Assistant send after them:",
            answer={"kind": "inbound_list", "rows": sa_status.inbound_list_rows(cards)},
        )

    if not talent and not project:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.STATUS,
            message='Whose reply, or which project? e.g. "Did Ahana reply about Google AI?"',
        )

    # talent (+ optional project) → the latest matching inbound message
    if talent:
        msgs = await inbound_messages.find_for_talent(
            talent["id"], project_id=(project or {}).get("id"), limit=5)
        if not msgs:
            # a message whose SENDER didn't resolve but that name-matches this talent
            maybe = await inbound_messages.find_unresolved_for_talent_name([talent["id"]], limit=3)
            if maybe:
                card = await sa_status.build_inbound_card(maybe[0])
                return _inbound_envelope(
                    cid, card,
                    lead=(f"There's an unresolved WhatsApp message that might be from "
                          f"{talent['label']} — I can't confirm the sender."))
            scope = f" about {project['label']}" if project else ""
            return _envelope(
                conversation_id=cid, state="answer", intent=Intent.STATUS,
                message=f"I don't see a WhatsApp reply from {talent['label']}{scope} yet.",
            )
        card = await sa_status.build_inbound_card(msgs[0])
        return _inbound_envelope(cid, card)

    # project only → recent inbound list
    msgs = await inbound_messages.find_for_project(project["id"])
    cards = [await sa_status.build_inbound_card(m) for m in msgs]
    if not cards:
        return _envelope(conversation_id=cid, state="answer", intent=Intent.STATUS,
                         message=f"I don't see any inbound WhatsApp replies for {project['label']} yet.")
    return _envelope(
        conversation_id=cid, state="answer", intent=Intent.STATUS,
        message=f"Recent inbound WhatsApp replies for {project['label']}:",
        answer={"kind": "inbound_list", "rows": sa_status.inbound_list_rows(cards)},
    )


async def _resolve_and_show_conversation(
    cid: str, user: dict, talent_text: Optional[str], project_text: Optional[str],
    *, forced_talent: Optional[dict] = None, forced_project: Optional[dict] = None,
) -> dict:
    """Phase 12 — READ-ONLY inspection of the recent, bounded, project-scoped
    conversation with one talent. No LLM, no send, no mutation. Never returns
    an unrelated project's messages."""
    from simple_assistant import audit, conversation_context

    talent = forced_talent
    if talent is None and talent_text:
        roster = await _global_talent_candidates()
        r = _resolve_one_name(talent_text, roster)
        if r.ambiguous_candidates:
            opts = _cand_options(r.ambiguous_candidates)
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.STATUS,
                message=f'I found {len(opts)} talents named "{talent_text}":',
                clarification={"kind": "talent", "prompt": "Which one?", "options": opts},
                context=_ctx(cid, {"kind": "clarify_status_talent", "intent": Intent.STATUS,
                                   "talent_text": talent_text, "project_text": project_text,
                                   "project": forced_project, "options": opts, "conversation": True}),
            )
        if r.ok and r.talent_ids:
            talent = {"id": r.talent_ids[0], "label": r.talent_labels[0]}

    project = forced_project
    if project is None and project_text:
        proj, clar = _match_project(project_text, await _active_projects())
        if clar is not None:
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.STATUS,
                message=clar["message"], clarification=clar["clarification"],
                context=_ctx(cid, {"kind": "clarify_status_project", "intent": Intent.STATUS,
                                   "talent_text": talent_text, "talent": talent,
                                   "options": clar["clarification"]["options"], "conversation": True}),
            )
        project = proj

    await audit.record_status(
        conversation_id=cid, user=user, query_kind="conversation",
        action_type="inspect_conversation_context",
        talent_id=(talent or {}).get("id"), talent_label=(talent or {}).get("label"),
        project_id=(project or {}).get("id"), project_label=(project or {}).get("label"),
        result="read" if conversation_context.is_enabled() else "disabled",
    )

    if not conversation_context.is_enabled():
        return _envelope(
            conversation_id=cid, state="answer", intent=Intent.STATUS,
            message="Conversation history isn't enabled on this environment.",
        )
    if not talent:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.STATUS,
            message='Whose conversation? e.g. "Show Ahana\'s recent conversation about Google AI."',
        )
    if not project:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.STATUS,
            message=f"Which project's conversation with {talent['label']}? "
                    "I keep conversation history scoped to one project.",
        )

    proj_doc = await RDB.projects.find_one({"id": project["id"]}, {"_id": 0})
    cc = await conversation_context.build(
        talent_id=talent["id"], project_id=project["id"], project=proj_doc,
    )
    rows = cc.get("recent_messages") or []
    if not rows:
        return _envelope(
            conversation_id=cid, state="answer", intent=Intent.STATUS,
            message=f"I don't have any recent WhatsApp messages between {talent['label']} and "
                    f"{project['label']} in the last {cc.get('window_hours')} hours.",
        )
    env = _envelope(
        conversation_id=cid, state="answer", intent=Intent.STATUS,
        message=f"Recent conversation with {talent['label']} about {project['label']} "
                f"({len(rows)} message{'s' if len(rows) != 1 else ''}"
                + (", older ones not shown" if cc.get("truncated") else "") + "):",
        answer={"kind": "conversation", "talent": talent["label"], "project": project["label"],
                "window_hours": cc.get("window_hours"), "truncated": cc.get("truncated"),
                "messages": rows},
    )
    return env


async def _resolve_and_analyze_inbound(
    cid: str, user: dict, talent_text: Optional[str], project_text: Optional[str], message: str,
    *, forced_talent: Optional[dict] = None, forced_project: Optional[dict] = None, draft: bool = False,
) -> dict:
    """Phase 8 — READ-ONLY deterministic intelligence over a captured inbound
    message. Phase 9 (draft=True) additionally generates a human-approval AI
    reply draft. No LLM, no send, no mutation UNTIL the admin approves."""
    import inbound_messages
    from simple_assistant import audit
    from simple_assistant import inbound_intelligence as sa_intel
    from simple_assistant import status as sa_status

    project_scoped_questions = bool(re.search(r"\b(?:latest|unanswered)\s+questions?\b", message, re.I))

    talent = forced_talent
    if talent is None and talent_text:
        roster = await _global_talent_candidates()
        r = _resolve_one_name(talent_text, roster)
        if r.ambiguous_candidates:
            opts = _cand_options(r.ambiguous_candidates)
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.STATUS,
                message=f'I found {len(opts)} talents named "{talent_text}":',
                clarification={"kind": "talent", "prompt": "Which one?", "options": opts},
                context=_ctx(cid, {"kind": "clarify_status_talent", "intent": Intent.STATUS,
                                   "talent_text": talent_text, "project_text": project_text,
                                   "project": forced_project, "options": opts, "analyze": True, "draft": draft}),
            )
        if r.ok and r.talent_ids:
            talent = {"id": r.talent_ids[0], "label": r.talent_labels[0]}

    project = forced_project
    if project is None and project_text:
        proj, clar = _match_project(project_text, await _active_projects())
        if clar is not None:
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.STATUS,
                message=clar["message"], clarification=clar["clarification"],
                context=_ctx(cid, {"kind": "clarify_status_project", "intent": Intent.STATUS,
                                   "talent_text": talent_text, "talent": talent,
                                   "options": clar["clarification"]["options"], "analyze": True,
                                   "project_questions": project_scoped_questions, "draft": draft}),
            )
        project = proj

    await audit.record_status(
        conversation_id=cid, user=user, query_kind="inbound_intelligence",
        action_type="inspect_inbound_intelligence",
        talent_id=(talent or {}).get("id"), talent_label=(talent or {}).get("label"),
        project_id=(project or {}).get("id"), project_label=(project or {}).get("label"),
        result="analysis disabled" if not sa_intel.is_enabled() else "analyzed",
    )

    # project-scoped "latest / unanswered questions from <project>"
    if project_scoped_questions and project and not talent:
        msgs = await inbound_messages.find_for_project(project["id"], limit=25)
        rows = []
        for m in msgs:
            an = await _analyze_one(m, project, sa_intel, RDB) if sa_intel.is_enabled() else None
            is_q = (an or {}).get("signals", {}).get("is_question") if an else ("?" in (m.get("message_text") or ""))
            if not is_q:
                continue
            later = await inbound_messages.unanswered_for_project(project["id"], limit=200)
            unanswered = any(x.get("id") == m.get("id") for x in later)
            rows.append({
                "when": m.get("received_at").strftime("%b %d, %H:%M") if hasattr(m.get("received_at"), "strftime") else str(m.get("received_at")),
                "talent": (an or {}).get("talent_label") or m.get("sender_name") or "Unresolved",
                "message": (m.get("message_text") or "")[:120],
                "topic": (an or {}).get("topic") or "—",
                "status": "no later Talentgram response" if unanswered else "—",
            })
        if not rows:
            return _envelope(conversation_id=cid, state="answer", intent=Intent.STATUS,
                             message=f"I don't see any inbound questions for {project['label']}.")
        return _envelope(
            conversation_id=cid, state="answer", intent=Intent.STATUS,
            message=f"{len(rows)} inbound question{'s' if len(rows) != 1 else ''} for {project['label']} "
                    "(a message is only flagged when there is no later matching Talentgram response):",
            answer={"kind": "inbound_question_list", "rows": rows},
        )

    if not talent:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.STATUS,
            message='Whose message should I analyze? e.g. "What is Ahana asking about Google AI?"',
        )

    msgs = await inbound_messages.find_for_talent(
        talent["id"], project_id=(project or {}).get("id"), limit=5)
    if not msgs:
        scope = f" about {project['label']}" if project else ""
        return _envelope(
            conversation_id=cid, state="answer", intent=Intent.STATUS,
            message=f"I don't have a captured WhatsApp message from {talent['label']}{scope} to analyze.",
        )

    msg = msgs[0]

    # STEP 12 — ambiguity stays explicit; NEVER pick a candidate project
    if (msg.get("project_resolution") == "ambiguous_project") and not (project or {}).get("id"):
        cand_labels = []
        for pid in (msg.get("project_candidates") or [])[:5]:
            d = await RDB.projects.find_one({"id": pid}, {"_id": 0, "brand_name": 1})
            cand_labels.append({"id": pid, "label": (d or {}).get("brand_name") or pid})
        an = await _analyze_one(msg, None, sa_intel, RDB) if sa_intel.is_enabled() else None
        env = _intelligence_envelope(cid, an, msg, talent, None, sa_intel.is_enabled(),
                                     ambiguous_projects=cand_labels)
        return env

    proj_doc = None
    pid = (project or {}).get("id") or msg.get("project_id")
    if pid:
        proj_doc = await RDB.projects.find_one({"id": pid}, {"_id": 0})

    if not sa_intel.is_enabled():
        card = await sa_status.build_inbound_card(msg)
        env = _inbound_envelope(cid, card)
        env["message"] += "  (Deeper message analysis isn't enabled on this environment.)"
        return env

    an = await _analyze_one(msg, proj_doc, sa_intel, RDB)
    proj_obj = ({"id": proj_doc.get("id"), "label": proj_doc.get("brand_name")} if proj_doc else project)

    if draft:
        return await _generate_ai_draft_envelope(cid, user, an, msg, talent, proj_obj, proj_doc)
    return _intelligence_envelope(cid, an, msg, talent, proj_obj, True)


async def _generate_ai_draft_envelope(cid, user, an, msg, talent, proj_obj, proj_doc) -> dict:
    """Phase 9 — one controlled AI generation + deterministic validation +
    a signed 'confirm_ai_response' plan. Nothing is sent here."""
    import datetime as _dt

    from simple_assistant import ai_response, audit, conversation_context

    base = _intelligence_envelope(cid, an, msg, talent, proj_obj, True)

    if not ai_response.is_enabled():
        base["message"] += "  (AI reply drafting isn't enabled on this environment.)"
        return base

    # Phase 12 — bounded, project-scoped recent-conversation slice (empty
    # unless SA_CONVERSATION_CONTEXT_ENABLED and a project is resolved).
    convo = await conversation_context.build(
        talent_id=(talent or {}).get("id"),
        project_id=(proj_doc or {}).get("id"),
        current_inbound_id=msg.get("id"),
        project=proj_doc,
    )
    if convo.get("recent_messages"):
        base["intelligence"]["recent_conversation"] = {
            "messages": convo["recent_messages"],
            "truncated": convo["truncated"],
            "window_hours": convo["window_hours"],
        }

    ctx = ai_response.build_context(analysis=an, project=proj_doc,
                                    talent_label=(talent or {}).get("label"),
                                    conversation_context=convo)
    gen = await ai_response.generate_draft(ctx)

    text = gen["draft"]["text"] if gen.get("ok") else None
    val_ok, val_reasons = (ai_response.validate_draft(text, ctx) if text else (None, []))
    if gen.get("ok"):
        _result = "draft_ok" if val_ok else "draft_rejected"
    else:
        _result = f"generation_{gen.get('error')}"

    await audit.record_ai(
        action_type="generate_ai_response", executed=False, conversation_id=cid, user=user,
        plan_id=None, inbound_id=msg.get("id"),
        talent_id=(talent or {}).get("id"), talent_label=(talent or {}).get("label"),
        project_id=(proj_obj or {}).get("id"), project_label=(proj_obj or {}).get("label"),
        topic=an.get("topic"), answerability=an.get("answerability"),
        draft_hash=(ai_response.draft_hash(text) if text else None),
        fact_hash=ai_response.fact_hash(ctx),
        result=_result, model=gen.get("model"), provider=gen.get("provider"), latency_ms=gen.get("latency_ms"),
        validation=("passed" if val_ok else ("rejected: " + "; ".join(val_reasons)) if val_reasons else None),
    )

    if not gen.get("ok"):
        base["intelligence"]["ai_draft"] = {"failed": True, "reason": gen.get("reason") or "generation failed"}
        base["message"] = (
            f'"{msg.get("message_text")}" — I couldn\'t generate a response '
            f"({gen.get('error')}). No message was sent."
        )
        return base

    if not val_ok:
        base["intelligence"]["ai_draft"] = {"failed": True, "reason": "The draft didn't pass safety checks: "
                                            + "; ".join(val_reasons)}
        base["message"] = (f'"{msg.get("message_text")}" — I drafted a reply but it failed a safety check '
                           "and cannot be approved. No message was sent.")
        return base

    plan_id = f"sap_{uuid.uuid4().hex}"
    expires_at = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=30)).isoformat()
    pending = {
        "kind": "confirm_ai_response",
        "plan_id": plan_id,
        "inbound_id": msg.get("id"),
        "talent_id": (talent or {}).get("id"),
        "talent_label": (talent or {}).get("label"),
        "project_id": (proj_obj or {}).get("id"),
        "source_received_at": (msg.get("received_at").isoformat()
                               if hasattr(msg.get("received_at"), "isoformat") else str(msg.get("received_at"))),
        "fact_hash": ai_response.fact_hash(ctx),
        "draft_hash": ai_response.draft_hash(text),
        "draft_text": text,
        "expires_at": expires_at,
    }
    base["intelligence"]["ai_draft"] = {
        "text": text,
        "mode": gen["draft"]["mode"],
        "confidence": gen["draft"]["confidence"],
        "insufficient_information": gen["draft"]["insufficient_information"],
        "answered_intents": gen["draft"].get("answered_intents") or [],
        "unanswered_intents": gen["draft"].get("unanswered_intents") or [],
        "grounded": True,
        "needs_review": True,          # STEP 9 — ALWAYS
        "editable": True,
        "no_message_sent": True,
    }
    base["context"] = _ctx(cid, pending)
    base["requires_confirmation"] = True
    base["message"] = (f'AI-drafted reply to {(talent or {}).get("label") or "the talent"} — '
                       "review, edit if you want, then Approve & Send. Nothing has been sent.")
    return base


async def _analyze_one(msg: dict, proj_doc: Optional[dict], sa_intel, rdb) -> dict:
    return sa_intel.analyze(msg, proj_doc)


def _intelligence_envelope(cid, an, msg, talent, project, enabled, *, ambiguous_projects=None) -> dict:
    from simple_assistant import status as sa_status

    card = {
        "inbound_id": msg.get("id"),
        "talent": ({"id": talent["id"], "label": talent["label"]} if talent else None),
        "talent_resolution": msg.get("talent_resolution"),
        "sender_phone_masked": sa_status._mask_phone(msg.get("sender_phone")),
        "project": project,
        "project_resolution": msg.get("project_resolution"),
        "project_candidates": ambiguous_projects or [],
        "group_name": msg.get("group_name"),
        "received_at": msg.get("received_at"),
        "message_text": msg.get("message_text") or "",   # verbatim, authoritative
        "source": "WhatsApp",
        "no_message_sent": True,
        "pipeline_mutation": "none",
    }
    if an:
        card.update({
            "intents": an.get("intents") or [{"topic": an["topic"], "confidence": an["topic_confidence"]}],
            "topic": an["topic"], "topic_confidence": an["topic_confidence"],
            "signals": an["signals"], "project_knowledge": an["project_knowledge"],
            "project_knowledge_multi": an.get("project_knowledge_multi") or {},
            "answerability": an["answerability"], "proposed_response": an["proposed_response"],
            "next_step": an["next_step"], "needs_review": an["needs_review"],
        })

    if ambiguous_projects:
        lines = "\n".join(f"{i + 1}. {p['label']}" for i, p in enumerate(ambiguous_projects))
        head = (f"I found an inbound WhatsApp message from {talent['label'] if talent else 'an unresolved sender'}, "
                f"but I cannot reliably determine the project.\n\nPossible projects:\n{lines}\n\n"
                "No project information was used.")
    elif an:
        rv = " · Needs review" if an["needs_review"] else ""
        who = (talent or {}).get("label") or "Unresolved"
        where = f" — {project['label']}" if project else ""
        tlist = ", ".join(i["topic"] for i in card["intents"])
        head = (f'{who}{where}: "{card["message_text"]}"\n'
                f"Intents: {tlist}{rv} · {an['answerability']}. No message was sent.")
    else:
        head = f'"{card["message_text"]}" — analysis unavailable.'
    return _envelope(conversation_id=cid, state="answer", intent=Intent.STATUS,
                     message=head, intelligence=card)


def _inbound_envelope(cid: str, card: dict, *, lead: Optional[str] = None) -> dict:
    who = card["talent"]["label"] if card.get("talent") else "Unresolved sender"
    where = card["project"]["label"] if card.get("project") else None
    if lead is None:
        head = f'{who}{f" — {where}" if where else ""}: "{card["message_text"]}"'
        if not card.get("talent"):
            head = f'Inbound WhatsApp message from {card["sender_phone_masked"]} — talent unresolved. "{card["message_text"]}"'
        elif not card.get("project") and card.get("project_resolution") == "ambiguous_project":
            head = f'Reply from {who} — project could not be determined reliably. "{card["message_text"]}"'
    else:
        head = lead
    return _envelope(conversation_id=cid, state="answer", intent=Intent.STATUS,
                     message=head, inbound=card)


async def _resolve_and_show_status(
    cid: str, user: dict, talent_text: Optional[str], project_text: Optional[str],
    forced_talent: Optional[dict], forced_project: Optional[dict],
) -> dict:
    from simple_assistant import audit
    from simple_assistant import status as sa_status

    talent = forced_talent
    if talent is None and talent_text:
        roster = await _global_talent_candidates()
        r = _resolve_one_name(talent_text, roster)
        if r.ambiguous_candidates:
            opts = _cand_options(r.ambiguous_candidates)
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.STATUS,
                message=f'I found {len(opts)} talents named "{talent_text}":',
                clarification={"kind": "talent", "prompt": "Which one?", "options": opts},
                context=_ctx(cid, {"kind": "clarify_status_talent", "intent": Intent.STATUS,
                                   "talent_text": talent_text, "project_text": project_text,
                                   "project": forced_project, "options": opts}),
            )
        if r.ok and r.talent_ids:
            talent = {"id": r.talent_ids[0], "label": r.talent_labels[0]}
        else:
            return _envelope(conversation_id=cid, state="blocked", intent=Intent.STATUS,
                             message=r.error or f'I couldn\'t find a talent named "{talent_text}".')

    project = forced_project
    if project is None and project_text:
        projects = await _active_projects()
        proj, clar = _match_project(project_text, projects)
        if clar is not None:
            return _envelope(
                conversation_id=cid, state="clarification", intent=Intent.STATUS,
                message=clar["message"], clarification=clar["clarification"],
                context=_ctx(cid, {"kind": "clarify_status_project", "intent": Intent.STATUS,
                                   "talent_text": talent_text,
                                   "talent": talent, "options": clar["clarification"]["options"]}),
            )
        project = proj

    if not talent and not project:
        return _envelope(
            conversation_id=cid, state="blocked", intent=Intent.STATUS,
            message='Whose send, or which project? e.g. "Did Ahana\'s Google AI send go through?"',
        )

    sends = await sa_status.find_sends(
        talent_id=(talent or {}).get("id"), project_id=(project or {}).get("id"),
    )

    if not sends:
        who = (talent or {}).get("label") or "that talent"
        where = (project or {}).get("label")
        await audit.record_status(conversation_id=cid, user=user, query_kind="delivery",
                                  talent_id=(talent or {}).get("id"), talent_label=(talent or {}).get("label"),
                                  project_id=(project or {}).get("id"), project_label=where,
                                  result="no sends found")
        msg = (f"I don't see a Simple Assistant WhatsApp send for {who}"
               + (f"'s {where} casting" if where else "") + " yet.")
        return _envelope(conversation_id=cid, state="answer", intent=Intent.STATUS, message=msg)

    # talent + project + exactly one send → the status card
    if talent and project and len(sends) == 1:
        card = await sa_status.build_status_card(sends[0])
        await audit.record_status(conversation_id=cid, user=user, query_kind="delivery",
                                  talent_id=talent["id"], talent_label=talent["label"],
                                  project_id=project["id"], project_label=project["label"],
                                  plan_id=card.get("plan_id"), result=card.get("overall"))
        return _status_card_envelope(cid, card)

    # talent + project + multiple sends → clarification (STEP 5)
    if talent and project and len(sends) > 1:
        opts = sa_status.send_options(sends)
        return _envelope(
            conversation_id=cid, state="clarification", intent=Intent.STATUS,
            message=f"I found {len(opts)} recent {project['label']} sends for {talent['label']}:",
            clarification={"kind": "send", "prompt": "Which one should I check?", "options": opts},
            context=_ctx(cid, {"kind": "clarify_status", "intent": Intent.STATUS,
                               "talent": talent, "project": project, "options": opts}),
        )

    # talent-only or project-only → compact recent-activity list
    rows = await sa_status.recent_activity_rows(sends)
    await audit.record_status(conversation_id=cid, user=user, query_kind="activity",
                              talent_id=(talent or {}).get("id"), talent_label=(talent or {}).get("label"),
                              project_id=(project or {}).get("id"), project_label=(project or {}).get("label"),
                              result=f"{len(rows)} sends")
    scope = (talent or {}).get("label") or (project or {}).get("label")
    return _envelope(
        conversation_id=cid, state="answer", intent=Intent.STATUS,
        message=f"Recent WhatsApp sends for {scope}:",
        answer={"kind": "whatsapp_activity", "rows": rows},
    )


def _status_card_envelope(cid: str, card: dict) -> dict:
    ov = card["overall"]
    icon = {"verified": "✓", "sent": "✓", "failed": "✕", "sending": "…", "queued": "•"}.get(ov, "•")
    head = (f"{card['talent']['label']} — {card['project']['label']}: {icon} {ov.upper()}. "
            + card["overall_copy"])
    if ov == "failed" and card.get("failure_reason"):
        head += f"\nReason: {card['failure_reason']}\nNo automatic retry was performed."
    return _envelope(conversation_id=cid, state="answer", intent=Intent.STATUS,
                     message=head, status=card)


async def _resume_status(cid: str, message: str, pending: dict, user: dict) -> Optional[dict]:
    from simple_assistant import audit
    from simple_assistant import status as sa_status

    options = pending.get("options") or []
    idx = nlu.resolve_option_reply(message, options)
    if idx is None:
        return None
    picked = options[idx - 1]

    if pending["kind"] == "clarify_status_talent":
        talent = {"id": picked["id"], "label": picked.get("name") or picked["label"]}
        if pending.get("conversation"):
            return await _resolve_and_show_conversation(
                cid, user, None, pending.get("project_text"),
                forced_talent=talent, forced_project=pending.get("project"))
        if pending.get("analyze"):
            return await _resolve_and_analyze_inbound(
                cid, user, None, pending.get("project_text"), message,
                forced_talent=talent, forced_project=pending.get("project"),
                draft=bool(pending.get("draft")))
        if pending.get("inbound"):
            return await _resolve_and_show_inbound(
                cid, user, None, pending.get("project_text"), False,
                forced_talent=talent, forced_project=pending.get("project"))
        return await _resolve_and_show_status(
            cid, user, None, pending.get("project_text"), talent, pending.get("project"))
    if pending["kind"] == "clarify_status_project":
        project = {"id": picked["id"], "label": picked["label"]}
        if pending.get("conversation"):
            return await _resolve_and_show_conversation(
                cid, user, pending.get("talent_text"), None,
                forced_talent=pending.get("talent"), forced_project=project)
        if pending.get("analyze"):
            return await _resolve_and_analyze_inbound(
                cid, user, pending.get("talent_text"), None,
                "unanswered questions" if pending.get("project_questions") else message,
                forced_talent=pending.get("talent"), forced_project=project,
                draft=bool(pending.get("draft")))
        if pending.get("inbound"):
            return await _resolve_and_show_inbound(
                cid, user, pending.get("talent_text"), None, bool(pending.get("want_unanswered")),
                forced_talent=pending.get("talent"), forced_project=project)
        return await _resolve_and_show_status(
            cid, user, pending.get("talent_text"), None, pending.get("talent"), project)
    if pending["kind"] == "clarify_status":
        # STEP 16 — re-derive the send from its signed plan_id, scoped to the
        # signed talent + project; the client cannot point this at another send.
        talent = pending.get("talent") or {}
        project = pending.get("project") or {}
        want = picked.get("plan_id")
        sends = await sa_status.find_sends(talent_id=talent.get("id"), project_id=project.get("id"))
        row = next((s for s in sends if (s.get("sa_action") or {}).get("plan_id") == want), None)
        if not row:
            return _envelope(conversation_id=cid, state="answer", intent=Intent.STATUS,
                             message="That send is no longer in the recent list. Ask me again for the latest status.")
        card = await sa_status.build_status_card(row)
        await audit.record_status(conversation_id=cid, user=user, query_kind="delivery",
                                  talent_id=talent.get("id"), talent_label=talent.get("label"),
                                  project_id=project.get("id"), project_label=project.get("label"),
                                  plan_id=card.get("plan_id"), result=card.get("overall"))
        return _status_card_envelope(cid, card)
    return None


async def _resume_clarification(cid: str, message: str, pending: dict, user: dict) -> Optional[dict]:
    options = pending.get("options") or []
    idx = nlu.resolve_option_reply(message, options)
    if idx is None:
        # Not a recognizable pick — let the caller treat it as a fresh command,
        # unless it's clearly just a confused reply.
        if len(message.split()) <= 2 and not any(ch.isalpha() for ch in message):
            return _envelope(
                conversation_id=cid, state="clarification", intent=pending.get("intent"),
                message="Sorry, I didn't catch that. Reply with the number or the name.",
                clarification={"kind": pending.get("kind", "").replace("clarify_", ""),
                               "prompt": "Which one?", "options": options},
                context=_ctx(cid, pending),
            )
        return None
    picked = options[idx - 1]

    if pending["kind"] == "clarify_project":
        proj = {"id": picked["id"], "label": picked["label"]}
        intent = pending.get("intent")
        if intent == Intent.MARK:
            return await _mark_plan_for_project(
                cid, proj, pending["target_stage"], pending.get("names", []),
                pending.get("is_pronoun", False), None,
            )
        if intent == Intent.ADD_REMOVE:
            return await _add_remove_plan(cid, proj, pending.get("adds", []), pending.get("removes", []))
        if intent == Intent.READ:
            overview = await build_overview(user)
            p = next((pp for pp in overview["projects"] if pp["id"] == proj["id"]), None)
            if p:
                return _envelope(
                    conversation_id=cid, state="answer", intent=Intent.READ,
                    message=f"{p['name']} — {p['pipeline_total']} talents in the pipeline.",
                    answer={"kind": "project_pipeline", "project": p},
                )
            return _envelope(conversation_id=cid, state="blocked", message="That project is no longer active.")

    if pending["kind"] == "clarify_talent":
        proj = pending["project"]
        intent = pending.get("intent")
        chosen_id, chosen_label = picked["id"], picked.get("name") or picked["label"]
        remaining = list(pending.get("pending_names", []))[1:]
        if intent == Intent.MARK:
            prior_ids = list(pending.get("resolved_ids", [])) + [chosen_id]
            # Re-resolve any remaining names by folding them into a synthetic re-run.
            cands = await _pipeline_candidates(proj["id"])
            resolved = [c for c in cands if c.id in prior_ids] or [
                nlu.Candidate(id=chosen_id, label=chosen_label)
            ]
            for nm in remaining:
                r = _resolve_one_name(nm, cands)
                if r.ok and r.talent_ids:
                    resolved += [c for c in cands if c.id in r.talent_ids]
                elif r.ambiguous_candidates:
                    opts = _cand_options(r.ambiguous_candidates)
                    return _envelope(
                        conversation_id=cid, state="clarification", intent=Intent.MARK,
                        message=f'I found {len(opts)} talents named "{nm}" in {proj["label"]}:',
                        clarification={"kind": "talent", "prompt": "Which one?", "options": opts},
                        context=_ctx(cid, {**pending, "resolved_ids": [c.id for c in resolved],
                                           "pending_names": remaining[remaining.index(nm):], "options": opts}),
                    )
            return _build_mark_plan(cid, proj, pending["target_stage"], _dedupe(resolved), [])
        if intent == Intent.ADD_REMOVE:
            # Lock in the just-picked talent so the re-run can't reproduce the
            # same ambiguity; any *other* still-ambiguous name re-clarifies.
            picked_name = (pending.get("pending_names") or [chosen_label])[0]
            forced = {picked_name.strip().lower(): chosen_id}
            return await _add_remove_plan(
                cid, proj, pending.get("adds", []), pending.get("removes", []), forced=forced,
            )

    return None


def _dedupe(cands: List[nlu.Candidate]) -> List[nlu.Candidate]:
    seen: set = set()
    out: List[nlu.Candidate] = []
    for c in cands:
        if c.id not in seen:
            seen.add(c.id)
            out.append(c)
    return out


# Confirm + execution live in simple_assistant/execute.py (Phase 3).
