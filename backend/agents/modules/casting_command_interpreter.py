"""Gemini Command Interpreter — Phase 1 AI assistance for ADD/MOVE/SHARE
(2026-09-22).

WHAT THIS MODULE IS: a narrowly-scoped, provider-bounded INTERPRETATION
layer that turns an otherwise-unrecognized free-text WhatsApp message into
a structured, ordered list of candidate ADD/MOVE/SHARE commands — plain
free-text phrases only, never database IDs, never a resolved talent/
project/stage record. It is called from exactly one place:
`casting_pipeline._resolve_bare_reply`, as the ABSOLUTE LAST resort, only
after every existing deterministic heuristic in that function has already
failed to claim the message (see that function's own docstring for the
full precedence chain this sits behind).

WHAT THIS MODULE IS NOT: it never resolves a talent/project/stage name to
a real database record, never checks permissions, never builds or shows a
confirmation card, never writes to the database, never sends a WhatsApp
message, and never decides an ambiguous match. Every one of those remains
exclusively the job of the EXISTING, unmodified `casting_pipeline.py`
resolvers (`_resolve_add_selection`/`_resolve_move_selection`/
`_resolve_share`), the existing Shared Disambiguation Engine, and the
existing confirm/edit/cancel conversation state machine. This module's
only output is the SAME shape of `{field_key: raw_text_value}` dict the
deterministic regex extractors (`_extract_add_fields`/`_extract_move_
fields`/`_extract_share_or_send_fields`) already produce for a message
the existing parser DID understand — so every single downstream step
(missing-field questions, syntax validation, DB resolution, ambiguity,
confirmation, execution, compound-plan handling) is 100% reused, unchanged,
with zero new resolver/confirmation/execution code anywhere in this file.

WHY resolve_bare_reply is the right integration point: `agents/models.py`'s
`AgentDefinition.resolve_bare_reply` docstring already describes exactly
this scenario — "Last-resort interpreter for a message that opened no
conversation and matched no intent trigger... dispatcher.py calls this
ONLY in that exact case". `casting_pipeline._resolve_bare_reply` is already
wired to `whatsapp-campaign-agent` (the WhatsApp group "Talentgram
Scouting Agent", which is where ADD/MOVE/SHARE actually live — see that
module's own "Talentgram Scouting Agent consolidation" comments) and
already implements several deterministic last-resort heuristics in
precedence order, ending in `return None` (silent "unrelated chatter,
ignore") when nothing claims the message. This module is added as ONE
more heuristic at the very end of that same chain — no dispatcher.py
change, no new AgentDefinition field, no new conversation-state shape.

FEATURE FLAG: `GEMINI_ADD_MOVE_SHARE_ENABLED` (default OFF), checked first
in `interpret_message` below, mirroring simple_assistant/__init__.py's
`is_enabled()`/`is_execution_enabled()` pattern exactly (env var, "1"/
"true"/"yes"/"on", fresh read every call, never cached at import time).
Gemini never executes anything itself, so — unlike Simple Assistant's two
independent flags (one exposes, one allows mutation) — a single flag is
enough here: the existing SEND/ADD/MOVE/SHARE executors already have their
own, completely separate confirmation/permission gates this module never
touches or bypasses.

FAILURE IS ALWAYS SILENT AND NON-FATAL: `interpret_message` never raises.
Any failure (flag off, not configured, timeout, API error, malformed JSON,
schema violation, unsupported intent, empty response) returns None, which
`_resolve_bare_reply` treats exactly like "no deterministic heuristic
claimed this either" — falls through to the existing `DispatchResult(
handled=False)` silent-drop, i.e. today's baseline behavior. Gemini is
never a hard dependency of this platform.

DEDUPLICATION: `agents/dispatcher.py`'s `handle_inbound_message` and
`agents/models.py`'s `ExecContext` were both extended (additively, default
None, zero behavior change for every existing caller) to thread the real
WhatsApp message id down to this module, mirroring the ONE existing
per-message idempotency pattern in this codebase (`inbound_messages.
_message_key` — real id preferred, deterministic hash fallback, unique
index + insert-race guard) rather than inventing a new one. See
`_message_key`/`_claim`/`_store_result` below. This guards against a
worker/webhook retry or duplicate delivery triggering a second Gemini API
call for the SAME logical message — it does not (and does not attempt to)
fix the platform's separate, pre-existing lack of dispatch-level
conversation-start idempotency, which is out of scope for this feature.

USAGE METRICS / LOGGING: every attempt (skipped, cache-hit, timeout,
error, or successful result) logs one structured `%`-style line, matching
`agents/dispatcher.py`'s own `dispatch_timing`/`op_trace` logging
convention — grep-able across Railway logs, never an f-string. See each
`logger.info`/`logger.warning` call below for the exact fields logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pymongo.errors import DuplicateKeyError

from core import db
from ai import gemini_client

logger = logging.getLogger(__name__)

CACHE_COLLECTION = "whatsapp_gemini_command_cache"
# Comfortably covers a worker/webhook retry window (seconds to low tens of
# seconds in practice) without lingering indefinitely — TTL-indexed, same
# self-cleaning pattern every other agents/* state collection already uses
# (see agents/disambiguation.py's own CACHE_TTL-equivalent reasoning).
CACHE_TTL_SEC = 600

_SUPPORTED_INTENTS = ("ADD", "MOVE", "SHARE")


def is_enabled() -> bool:
    """Feature flag — OFF by default, read fresh on every call (never
    cached at import time), mirroring simple_assistant/__init__.py's
    is_enabled()/is_execution_enabled() exactly. Independently disableable
    without touching the deterministic ADD/MOVE/SHARE system at all —
    turning this off simply means _resolve_bare_reply's existing chain of
    heuristics ends one step earlier, exactly as it did before this
    feature existed."""
    return os.environ.get("GEMINI_ADD_MOVE_SHARE_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _timeout_sec() -> float:
    return float(os.environ.get("GEMINI_ADD_MOVE_SHARE_TIMEOUT_SEC", "8"))


def _model() -> Optional[str]:
    return os.environ.get("GEMINI_ADD_MOVE_SHARE_MODEL") or None  # None -> gemini_client.DEFAULT_MODEL


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_indexes() -> None:
    """Called once at app startup (server.py), same wiring pattern as
    media_assignment/media_send/mark_intent/submission_action_queue's own
    ensure_indexes calls. Safe to call on every boot — idempotent."""
    await db[CACHE_COLLECTION].create_index("message_key", unique=True, name="uniq_message_key")
    await db[CACHE_COLLECTION].create_index("expires_at", expireAfterSeconds=0, name="cache_ttl")


# ---------------------------------------------------------------------------
# Cheap local pre-filter — spend zero Gemini calls on messages that plainly
# aren't attempting an ADD/MOVE/SHARE command at all (ordinary group
# chatter: "good morning", "thanks!", "😂😂", "call me later"). Mirrors
# casting_pipeline._looks_like_share_attempt's own philosophy (a narrow,
# additive, false-negative-tolerant signal, never a false-positive risk to
# real deterministic behavior since this only ever gates whether Gemini is
# even ATTEMPTED — a message this filter rejects simply falls through to
# today's existing "unrelated chatter, ignore" baseline, no regression).
# ---------------------------------------------------------------------------
_ACTION_WORD_RE = re.compile(
    r"\b("
    # Word-stem prefixes (\w* below), not exact-word matches, so ordinary
    # inflections ("added", "adding", "shared", "sharing", "moved") are
    # still caught — a strict \bword\b boundary would miss every one of
    # these (real ADD/MOVE/SHARE example phrasing in this feature's own
    # spec includes "Priya should be added to Hinge").
    r"add\w*|attach\w*|put|mov\w*|shift\w*|transfer\w*|relocate\w*|shortlist\w*|"
    r"approv\w*|reject\w*|hold|lock\w*|"
    r"shar\w*|send\w*|forward\w*|deliver\w*|messag\w*|broadcast\w*|push\w*|dispatch\w*|"
    r"ready|pipeline|stage|profile|casting\s*call"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_possible_command(text: str) -> bool:
    stripped = (text or "").strip()
    if len(stripped) < 4:
        return False
    return bool(_ACTION_WORD_RE.search(stripped))


# ---------------------------------------------------------------------------
# Structured-output schema — deliberately narrow: every field is a plain
# string/null, NEVER an id/number-typed "resolved entity" field. This is
# the concrete enforcement of "Gemini must not invent database facts" —
# the schema itself has no place to put one.
# ---------------------------------------------------------------------------
def _command_item_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": list(_SUPPORTED_INTENTS) + ["UNKNOWN"]},
            "talent_name": {"type": ["string", "null"]},
            "project_name": {"type": ["string", "null"]},
            "stage_name": {"type": ["string", "null"]},
            "recipient_description": {"type": ["string", "null"]},
            "template_or_message": {"type": ["string", "null"]},
            "missing_fields": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
        "required": [
            "intent", "talent_name", "project_name", "stage_name",
            "recipient_description", "template_or_message", "missing_fields", "confidence",
        ],
    }


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"commands": {"type": "array", "items": _command_item_schema()}},
    "required": ["commands"],
}


def _build_system_prompt() -> str:
    # Real stage vocabulary, never invented — see module docstring.
    from agents.modules import casting_pipeline_nlu as nlu
    from routers.casting_pipeline import PIPELINE_STAGE_ORDER

    stage_lines = "\n".join(f"- {nlu.stage_label(s)}" for s in PIPELINE_STAGE_ORDER)
    return f"""You are a language-interpretation layer in front of a WhatsApp talent-casting bot. You do NOT execute anything, look up any database, or decide any identity — a separate, deterministic system does all of that after you.

Your ONLY job: read one WhatsApp message and identify which of THREE commands it contains — ADD, MOVE, SHARE — extracting, as close to verbatim from the message as possible, the plain-text names/phrases involved.

The real pipeline stages this system understands (use one of these exact labels when the message clearly means one of them; otherwise copy the user's own stage words unchanged rather than forcing an incorrect match):
{stage_lines}

ADD — adds a talent to a project's pipeline as a brand-new entry. Needs: talent_name, project_name.
MOVE — moves an ALREADY-pipelined talent to a different stage. Needs: talent_name, stage_name. project_name is optional context (only if the message names a project explicitly).
SHARE — shares/forwards/sends a casting call, template, or custom message to a named talent (or several) or to everyone currently in a named pipeline stage. Needs: recipient_description (who — a name, or "everyone in <stage>"). project_name and template_or_message are optional (only if explicitly present).

RULES — follow exactly, every time:
1. NEVER invent a talent name, project name, or stage name that was not written, or unambiguously implied, in the message. If a required piece is missing, leave that field null and add its name to missing_fields — never guess a value to fill it in.
2. NEVER output an ID of any kind (no talent IDs, project IDs, stage IDs, phone numbers, WhatsApp group names, URLs, or budgets) — you have no database access and must not pretend to.
3. If the message uses a pronoun ("her"/"him"/"them"/"this talent"/"that talent") that clearly refers to someone NAMED EARLIER IN THIS SAME MESSAGE, resolve talent_name to that person's actual name. If the pronoun could only refer to someone from an earlier, separate WhatsApp conversation (no name anywhere in THIS message), set talent_name to the literal pronoun word itself ("her"/"him"/"them") rather than guessing an identity — a separate system may resolve that from conversation memory; you cannot see it.
4. If the message contains MORE THAN ONE command (e.g. "Add Priya to Hinge and move her to Follow Up"), return them as separate objects in "commands", in the order they were said, applying rule 3's pronoun handling across the commands you return (an entity named in command 1 may be referred to by pronoun in command 2).
5. If you cannot confidently identify ANY of ADD/MOVE/SHARE anywhere in the message, return exactly one command object with intent "UNKNOWN" and every other field null/empty.
6. Typos and abbreviations are expected and should be interpreted normally (e.g. "mve"->MOVE, "FU"->Follow Up) — but never let a typo correction change WHICH database entity is meant; only correct the ENGLISH, never guess between two different real names.
7. confidence is your own 0-1 estimate for diagnostics only — it is logged, never used to skip any validation.
8. Do not add commentary, explanation, or markdown — respond with the structured JSON only."""


def _build_user_prompt(text: str, session: Optional[Dict[str, Any]]) -> str:
    context_line = ""
    last_talent = (session or {}).get("last_talent_label")
    if last_talent:
        context_line = (
            f"\n\n(For reference only, NOT part of the message: the most recently discussed talent in this "
            f"conversation, from an EARLIER separate message, was \"{last_talent}\" — only use this if a pronoun "
            f"in the message below cannot refer to anyone named within the message itself; per rule 3, in that "
            f"case still return the literal pronoun word, never this name, so the existing system resolves it "
            f"the same safe way it always does.)"
        )
    return f'WhatsApp message to interpret:\n"""\n{text}\n"""{context_line}'


def _clean_str(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v or None


def _validate_and_extract_commands(raw: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Schema-valid response -> ordered list of {intent, ...free-text
    phrases...} dicts, UNKNOWN/malformed entries dropped, never guessed
    into something else. Returns None (not an empty list) when nothing
    usable survives — the caller treats None identically to a Gemini
    failure, never a false claim on the message."""
    commands = raw.get("commands") if isinstance(raw, dict) else None
    if not isinstance(commands, list) or not commands:
        return None
    out: List[Dict[str, Any]] = []
    for c in commands:
        if not isinstance(c, dict):
            continue
        intent = c.get("intent")
        if intent not in _SUPPORTED_INTENTS:
            continue  # UNKNOWN, or a malformed/unsupported value — never guessed into a real intent
        out.append({
            "intent": intent,
            "talent_name": _clean_str(c.get("talent_name")),
            "project_name": _clean_str(c.get("project_name")),
            "stage_name": _clean_str(c.get("stage_name")),
            "recipient_description": _clean_str(c.get("recipient_description")),
            "template_or_message": _clean_str(c.get("template_or_message")),
            "confidence": c.get("confidence") if isinstance(c.get("confidence"), (int, float)) else None,
        })
    return out or None


# ---------------------------------------------------------------------------
# Deduplication — mirrors inbound_messages._message_key's exact pattern
# (real WhatsApp message id preferred, hash fallback), applied here as its
# own, independent cache rather than reusing that Simple-Assistant-owned
# collection (different lifecycle/TTL/shape; see module docstring).
# ---------------------------------------------------------------------------
def _message_key(*, message_id: Optional[str], agent_id: str, phone: str, group_name: str, text: str) -> str:
    if message_id and str(message_id).strip():
        return f"wamid:{agent_id}:{str(message_id).strip()}"
    basis = f"{agent_id}|{group_name or ''}|{phone}|{text}"
    return "hash:" + hashlib.sha256(basis.encode()).hexdigest()[:40]


async def _get_cached(key: str) -> Optional[Dict[str, Any]]:
    return await db[CACHE_COLLECTION].find_one({"message_key": key, "status": "done"})


async def _claim(key: str) -> bool:
    """Atomic claim via the unique index's own insert-race guard — the SAME
    proven pattern inbound_messages.capture_inbound uses (E11000 -> someone
    else already has it). True = this call may proceed to actually invoke
    Gemini; False = a concurrent turn for the identical message already
    claimed it (still pending) or finished it (in which case _get_cached,
    always checked first, would already have returned it)."""
    try:
        await db[CACHE_COLLECTION].insert_one({
            "message_key": key, "status": "pending", "commands": None,
            "created_at": _now(), "expires_at": _now() + timedelta(seconds=CACHE_TTL_SEC),
        })
        return True
    except DuplicateKeyError:
        return False


async def _store_result(key: str, commands: Optional[List[Dict[str, Any]]]) -> None:
    await db[CACHE_COLLECTION].update_one(
        {"message_key": key},
        {"$set": {
            "status": "done", "commands": commands,
            "expires_at": _now() + timedelta(seconds=CACHE_TTL_SEC),
        }},
    )


async def interpret_message(
    text: str,
    *,
    agent_id: str,
    phone: str,
    group_name: str,
    message_id: Optional[str] = None,
    session: Optional[Dict[str, Any]] = None,
) -> Optional[List[Dict[str, Any]]]:
    """The single public entry point. Returns an ordered list of validated
    command dicts, or None on ANY failure/non-match (flag off, not
    configured, filtered out, timeout, API error, malformed/empty/
    unsupported response) — never raises, never a hard dependency.
    `casting_pipeline._resolve_bare_reply` is the only caller."""
    if not is_enabled():
        return None
    if not gemini_client.is_configured():
        logger.info("gemini_fallback_skip agent=%s reason=not_configured", agent_id)
        return None
    if not _looks_like_possible_command(text):
        return None

    key = _message_key(message_id=message_id, agent_id=agent_id, phone=phone, group_name=group_name, text=text)

    cached = await _get_cached(key)
    if cached is not None:
        logger.info("gemini_fallback_cache_hit agent=%s key=%s", agent_id, key)
        return cached.get("commands")

    if not await _claim(key):
        logger.info("gemini_fallback_dedup_prevented agent=%s key=%s", agent_id, key)
        return None

    start = time.monotonic()
    try:
        raw = await asyncio.wait_for(
            gemini_client.call_tool_json(
                system=_build_system_prompt(),
                user=_build_user_prompt(text, session),
                tool_name="interpret_casting_command",
                tool_description="Interpret a WhatsApp message as ADD/MOVE/SHARE casting-pipeline command(s).",
                input_schema=_RESPONSE_SCHEMA,
                max_tokens=1024,
                model=_model(),
            ),
            timeout=_timeout_sec(),
        )
    except asyncio.TimeoutError:
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.warning("gemini_fallback_timeout agent=%s key=%s latency_ms=%d timeout_sec=%.1f",
                        agent_id, key, latency_ms, _timeout_sec())
        await _store_result(key, None)
        return None
    except (gemini_client.LLMUnavailable, gemini_client.LLMError) as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.warning("gemini_fallback_error agent=%s key=%s latency_ms=%d error=%s",
                        agent_id, key, latency_ms, str(exc)[:200])
        await _store_result(key, None)
        return None
    except Exception:
        # Never let an unexpected Gemini-path failure become an application
        # error — the whole point of this module is that it can fail
        # completely safely (see module docstring's "FAILURE IS ALWAYS
        # SILENT AND NON-FATAL").
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.exception("gemini_fallback_unexpected_error agent=%s key=%s latency_ms=%d", agent_id, key, latency_ms)
        await _store_result(key, None)
        return None

    latency_ms = int((time.monotonic() - start) * 1000)
    commands = _validate_and_extract_commands(raw)
    logger.info(
        "gemini_fallback_result agent=%s key=%s latency_ms=%d model=%s command_count=%d intents=%s",
        agent_id, key, latency_ms, (_model() or gemini_client.DEFAULT_MODEL),
        len(commands or []), [c.get("intent") for c in (commands or [])],
    )
    await _store_result(key, commands)
    return commands
