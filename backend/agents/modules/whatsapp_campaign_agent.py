"""WhatsApp Campaign Orchestration Agent — the third registered WhatsApp
agent. Translates a natural-language WhatsApp command into the SAME
`BatchIn` object the web app's campaign UI builds, and calls the EXISTING
compile-preview / launch function (`routers.whatsapp.create_batch`)
directly, in-process — zero duplicate recipient resolution, template
rendering, job queueing, or delivery tracking. `create_batch` already IS
both the preview and the launch call, distinguished only by `is_dry_run`.

Scoped to its own WhatsApp group ("Talentgram WhatsApp Agent"), completely
independent of casting-agent/crm-agent — see agents/__init__.py's
seed_agent_config call for the group binding.

2026-08-08 architecture change: this agent no longer gates on a literal
trigger phrase like "send campaign"/"broadcast". It now works like
casting-agent's own NLU — a broad set of action-verb synonyms opens the
ONE intent (SEND_REQUIREMENT), and free-text entity extraction pulls out
WHO to send to and WHAT to send, independent of exact phrasing. The old
"send campaign to <project> using <template>" grammar still works
byte-for-byte (kept as an explicit extraction tier) — it's simply now one
of several phrasings that map to the same intent, not the only one.

Message Source (v1 scope, decided explicitly — see AskUserQuestion during
this sprint): resolves ONLY via `whatsapp_templates`, either an explicit
template name/slug or a bare project-ish word that happens to fuzzy-match a
template name (production evidence: real templates are routinely named
after the project they're for, e.g. a template literally named "toyota
glanza"). There is no free-text/custom-message send path in this codebase
(`BatchIn.template_id` is required) and no "last generated requirement" /
message-history / quoted-reply resolution — building those would mean
inventing new send infrastructure, which this sprint deliberately does not
do. A source phrase that resolves to nothing falls through to the existing
generic "which template?" missing-field question, the same graceful
degradation every intent on this platform already has.

Recipient resolution reuses the REAL existing engine end to end
(`routers.whatsapp.resolve_recipients_engine` via `create_batch`) for
EVERY source type it supports — PROJECT, MANUAL, CRM, and SAVED_LISTS —
the only new code is the free-text routing logic that decides WHICH
existing source_type+params a recipient phrase means: a phone number, one
or more named talents (reusing casting-agent's own hardened talent-
selector/fuzzy-match machinery, routed as MANUAL so per-talent WhatsApp-
group routing still applies), an entire project's pipeline (reusing
resolve_project_by_name, same as before), a CRM contact_type category
(reusing the same distinct-values query the web app's own CRM filter
dropdown runs), or a saved contact/group list (small matchers in
agents/name_match.py, the SAME shared 4-tier shape templates already
used, not a new algorithm). None of these branches write to
whatsapp_batches/whatsapp_jobs directly or render a message body
themselves — every one of them ends by handing source_type+SourceParams to
the same create_batch() call the web app's own Preview/Send buttons use.

Attribution: every campaign launched via WhatsApp is created_by the single
seeded ADMIN_EMAIL account (there is no phone-to-user mapping in this
codebase) — the real WhatsApp sender is still fully traceable via
agents.audit.log_turn, which dispatcher.py already writes for every turn.

Ambiguity handling (2026-08-09, Sprint 1): a genuinely UNRESOLVABLE source
or recipient still ends the attempt with a clear message. A source or
recipient that matched MULTIPLE legitimate candidates instead opens an
interactive clarification via the shared, agent-agnostic
agents/disambiguation.py engine — a numbered list, the user replies with a
digit/circled digit/ordinal word/name, and the original command resumes
automatically with no retyping. See _resolve_source/_resolve_recipient's
AmbiguousEntity returns and _build_send_requirement_confirmation's use of
disambiguation.start.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from core import db, ADMIN_EMAIL

from routers.whatsapp import BatchIn, SourceParams, ManualContact, create_batch, _normalize_phone
from routers.casting_pipeline import PIPELINE_STAGE_ORDER

from agents.models import (
    AgentDefinition,
    ExecContext,
    ExecResult,
    FieldSpec,
    IntentDefinition,
    ValidationResult,
)
from agents.registry import register_agent
from agents import conversation, disambiguation, name_match
from agents.modules import casting_pipeline_nlu as nlu
from agents.modules.casting_pipeline import (
    _fetch_ongoing_projects,
    _fetch_all_talent_candidates,
    _format_instagram_link,
    # SHARE Production Readiness (2026-09-08) — the ONE source of truth
    # for SHARE's own canonical syntax examples, owned by SHARE's real
    # implementation; every SHARE instructional error (near-miss
    # guidance, missing-field questions, legacy-syntax redirect) renders
    # this SAME text, so HELP_TEXT below does too — never a second,
    # independently hand-typed manual that can drift out of sync.
    SHARE_HELP_EXAMPLES,
    # SHARE Instagram Link (Production fix, 2026-09-09) — same single-
    # source-of-truth pattern as SHARE_HELP_EXAMPLES above, for the
    # Instagram content_type's own canonical examples.
    SHARE_INSTAGRAM_HELP_EXAMPLES,
    # SHARE ownership/routing (Production fix, 2026-09-05) — the real,
    # already-production-ready SHARE implementation (templates, custom
    # messages, pipeline-stage targeting, ambiguity/edit/cancel, the
    # Pipeline Check gate) lives entirely in casting_pipeline.py and is
    # reused here UNCHANGED, never duplicated. Standalone SHARE now
    # belongs to THIS agent/group exclusively — see SHARE_INTENT's own
    # registration below and casting_pipeline.py's SHARE_REROUTE_INTENT
    # for what Casting Pipeline shows instead.
    SHARE_INTENT,
    # SEND_INTENT (casting.send, Production fix 2026-09-06) — registered
    # here too (triggers=[], never directly reachable — see its own
    # comment in casting_pipeline.py) purely so registry.get_intent(agent,
    # "casting.send") resolves when SHARE_INTENT hands a media-classified
    # message off to it. Its own resolution/executor logic is completely
    # unchanged and untouched.
    SEND_INTENT,
    # Talentgram Scouting Agent consolidation (Production fix, 2026-09-06)
    # — ADD/MOVE/QUERY(SHOW/TESTED)/UPLOAD/UNDO, unchanged, imported and
    # registered on THIS agent so the whole Casting Pipeline command
    # surface is reachable from ONE WhatsApp group. Casting Pipeline
    # (casting-agent) keeps ONLY the approved compound ADD->MOVE->SHARE
    # workflow's underlying engine; its own top-level group is redirect-
    # only (CASTING_REDIRECT_INTENT) — see casting_pipeline.py.
    QUERY_INTENT,
    MOVE_INTENT,
    ADD_INTENT,
    UPLOAD_INTENT,
    UNDO_INTENT,
    _resolve_bare_reply,
)

AGENT_ID = "whatsapp-campaign-agent"

logger = logging.getLogger(__name__)

# Action-verb synonyms — ANY of these opens the intent (broadened trigger
# gate, not a single literal phrase). The old compound phrases are kept as
# explicit aliases so "Send campaign to X using Y" still opens the same
# intent it always did — they're longer, so parser.detect_trigger's
# longest-match tie-break naturally still prefers them when present, though
# it no longer matters which one wins since both route here now.
#
# "share" removed (Production fix, 2026-09-05) — it used to double as a
# SEND_VERBS synonym for this generic broadcast intent, colliding with the
# NEW, structured SHARE command ("Share the casting call for Hinge with
# Nikita Tiwari") now registered as its own intent below. "send"/
# "forward"/"broadcast"/etc. still open this intent exactly as before —
# nothing else about general template broadcasting changed.
SEND_VERBS = [
    "send", "forward", "deliver", "message",
    "broadcast", "push", "dispatch",
]
LEGACY_CAMPAIGN_PHRASES = [
    "send campaign", "launch campaign", "start campaign",
    "run campaign", "send broadcast",
]
SEND_TRIGGERS = LEGACY_CAMPAIGN_PHRASES + SEND_VERBS


# ---------------------------------------------------------------------------
# Template matching — thin wrapper around the shared tiered matcher
# (agents/modules/name_match.py). This IS the orchestration layer's own
# job: translating free text into an existing template_id, not a duplicate
# of the templates API or create_batch itself.
# ---------------------------------------------------------------------------
@dataclass
class TemplateMatch:
    template: Optional[Dict[str, str]] = None
    ambiguous: Optional[List[Dict[str, str]]] = None
    error: Optional[str] = None


@dataclass
class AmbiguousEntity:
    """Carries a multi-candidate resolution outcome up to
    _build_send_requirement_confirmation, which hands it to the shared
    disambiguation engine (agents/disambiguation.py) instead of formatting
    a dead-end text error. `entity_type` is one of the engine's known
    opaque labels ("project"/"template"/"talent"/"crm_source"/
    "saved_list"); `field_key` is which `collected` field to overwrite
    with the resolved label once the user picks.

    `extra_collected` (Multi Manual Recipients sprint, 2026-08-09) — extra
    private keys to fold into the `collected` snapshot disambiguation.start
    stores, alongside (never instead of) `field_key`'s normal overwrite.
    Used only by the multi-recipient resolver to carry "which of several
    names is pending, and what were the others" state across the
    disambiguation round trip — see _resolve_multi_recipient_names /
    _resume_pending_multi_recipient. None for every other ambiguity kind,
    completely unaffected."""
    entity_type: str
    field_key: str
    candidates: List["disambiguation.Candidate"]
    extra_collected: Optional[Dict[str, str]] = None


_TEMPLATE_FUZZY_CUTOFF = 0.6
_TEMPLATE_AUTOCORRECT_CUTOFF = 0.8
_TEMPLATE_AMBIGUITY_MARGIN = 0.05


def resolve_template_by_name(name_query: str, templates: List[Dict[str, str]]) -> TemplateMatch:
    def _label(t: Dict[str, str]) -> str:
        return t.get("name") or t.get("slug") or ""

    m = name_match.tiered_name_match(
        name_query, templates, _label,
        id_fn=lambda t: t["id"], what="template",
        fuzzy_cutoff=_TEMPLATE_FUZZY_CUTOFF,
        autocorrect_cutoff=_TEMPLATE_AUTOCORRECT_CUTOFF,
        ambiguity_margin=_TEMPLATE_AMBIGUITY_MARGIN,
    )
    if m.item is not None:
        return TemplateMatch(template=m.item)
    if m.ambiguous:
        return TemplateMatch(ambiguous=m.ambiguous)
    return TemplateMatch(error=m.error)


async def _fetch_templates() -> List[Dict[str, str]]:
    # Excludes the seeded slug="custom" template (name "Custom Message",
    # body_text="{{message}}") — that template is reachable ONLY via the
    # dedicated _fetch_custom_template() helper (Custom Message/Instagram
    # send modes), never by ordinary fuzzy template-name matching. Without
    # this exclusion, a malformed "send custom message to X" (no quotes,
    # falls through to requirement mode) could fuzzy-match this template's
    # name and silently send the literal unsubstituted string "{{message}}".
    return await db.whatsapp_templates.find(
        {"slug": {"$ne": "custom"}}, {"_id": 0, "id": 1, "name": 1, "slug": 1}
    ).sort("created_at", 1).to_list(200)


async def _fetch_custom_template() -> Optional[Dict[str, str]]:
    """The seeded built-in template with slug="custom" and
    body_text="{{message}}" (routers/whatsapp.py) — the render target for
    both Custom Message and Instagram Profile sends. Both modes populate
    the {{message}} placeholder via BatchIn.variable_data (see
    _build_batch_in), never a new template or send path."""
    return await db.whatsapp_templates.find_one(
        {"slug": "custom"}, {"_id": 0, "id": 1, "name": 1, "slug": 1}
    )


async def _fetch_contact_lists() -> List[Dict[str, str]]:
    return await db.whatsapp_contact_lists.find(
        {"deleted": {"$ne": True}}, {"_id": 0, "id": 1, "name": 1}
    ).sort("created_at", -1).to_list(500)


async def _fetch_group_lists() -> List[Dict[str, str]]:
    return await db.whatsapp_group_lists.find(
        {"deleted": {"$ne": True}}, {"_id": 0, "id": 1, "name": 1}
    ).sort("created_at", -1).to_list(500)


async def _fetch_crm_contact_types() -> List[str]:
    """Same distinct-values query the web app's own CRM filter dropdown
    (`GET /crm/contact-types`) already runs — reused directly, not
    reimplemented, so a new contact_type value never needs a code change
    on either side to become sendable."""
    types = await db.clients.distinct(
        "contact_type", {"archived": {"$ne": True}, "deleted": {"$ne": True}}
    )
    return sorted([t for t in types if t])


async def _service_admin() -> dict:
    """The actor create_batch's `admin` param needs (created_by, sender-
    name template variables, audit) — the same bootstrap-seeded admin
    account core.py's own startup already guarantees exists (ADMIN_EMAIL).
    No new user/mapping infrastructure: the real WhatsApp sender is
    traceable separately via the dispatcher's own audit.log_turn call,
    not via this field."""
    admin = await db.users.find_one({"email": ADMIN_EMAIL}, {"_id": 0})
    if not admin:
        raise RuntimeError(f"seeded admin account {ADMIN_EMAIL!r} not found")
    return admin


# ---------------------------------------------------------------------------
# Entity extraction — intent detection + entity extraction, not trigger-
# phrase matching. Three tiers, tried in order:
#
#   1. Explicit "using <template>" clause (the original, still fully
#      supported campaign grammar) — template extracted+removed first, then
#      whatever follows "to"/"with" in what's left is the recipient, then a
#      stage word (if any) left over before that is scanned last. Anchored
#      on "using" specifically (not "with") so it can never collide with
#      "Share X with Y" (tier 3), which also uses "with" but to mean the
#      RECIPIENT connector, not a template connector.
#   2. "<verb> <recipient> the <source>" inverted shape ("Message Ahana the
#      Toyota requirement") — only tried when no to/with connector exists
#      at all, so it can't misfire on phrasing tier 3 already handles.
#   3. Generic "<source> to|with <recipient>" shape — covers every other
#      natural-language example (Send/Share/Forward/Deliver/Push/Dispatch
#      X to/with Y).
#
# A command that doesn't fit any of these just leaves fields unextracted —
# it falls through to the existing missing-field question flow
# (dispatcher.py), the same graceful degradation every other intent here
# already has, not a hard failure.
# ---------------------------------------------------------------------------
_LEGACY_TEMPLATE_RE = re.compile(
    r"\busing\s+(?:the\s+)?(.+?)(?:\s+template\b)?\s*[.!?]*$",
    re.IGNORECASE | re.DOTALL,
)
_TO_OR_WITH_RE = re.compile(r"\b(?:to|with)\s+(.+)$", re.IGNORECASE | re.DOTALL)

# ---------------------------------------------------------------------------
# Pipeline/stage recipient support (2026-08-09) — "Follow Up pipeline of
# Toyota Glanza" / "Approved pipeline of Toyota Glanza" / "Selected list" /
# "Followup pipeline Toyota" (no connector) / "Follow Up of Toyota" (no
# pipeline/stage/list word at all). Splits a recipient phrase that LOOKS
# like a stage reference into (raw_stage_phrase, project_phrase) — the
# stage phrase is NOT validated/normalized here (that's centralized in
# _resolve_recipient via the reused nlu.match_stage_phrase, the same
# function/alias table casting-agent's own stage matching already uses —
# no duplicated stage synonym table). project_phrase is "" when no
# project was named at all (e.g. "Selected list" alone).
# ---------------------------------------------------------------------------
_PIPELINE_CONNECTOR_RE = re.compile(r"\b(?:pipelines?|stages?|lists?)\b", re.IGNORECASE)
_OF_PROJECT_RE = re.compile(r"\b(?:of|for)\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _split_stage_and_project(recipient_text: str) -> Optional["tuple[str, str]"]:
    text = (recipient_text or "").strip()
    if not text:
        return None
    project_phrase = ""
    remainder = text
    of_m = _OF_PROJECT_RE.search(text)
    if of_m:
        project_phrase = of_m.group(1).strip(" .!?")
        remainder = text[:of_m.start()].strip()

    conn_m = _PIPELINE_CONNECTOR_RE.search(remainder)
    if not conn_m:
        # No explicit pipeline/stage/list word. Only treat this as a stage
        # reference when an explicit "of/for <project>" tail was ALSO
        # found ("Follow Up of Toyota") — a bare phrase with neither a
        # connector word nor an "of" tail is indistinguishable from a
        # plain project/talent reference and must not be hijacked (e.g.
        # "to Toyota Glanza" alone must keep resolving as a whole-project
        # recipient, unchanged).
        if not of_m:
            return None
        stage_phrase = remainder.strip()
        return (stage_phrase, project_phrase) if stage_phrase else None

    stage_phrase = remainder[:conn_m.start()].strip()
    after = remainder[conn_m.end():].strip(" ,")
    if not project_phrase and after:
        # Bare juxtaposition, no "of"/"for" at all: "Followup pipeline Toyota".
        project_phrase = after
    if not stage_phrase:
        return None
    return (stage_phrase, project_phrase)


# A stage was named but no project ("Selected list" / "Follow Up pipeline"
# alone) — recipient_query can't be left empty (the field is required, so
# an empty value would trigger the generic plain-text "Who should this go
# to?" missing-field question instead of ever reaching build_confirmation,
# where the richer "ask which project, numbered" disambiguation lives).
# This sentinel keeps the field non-empty so the turn proceeds to
# build_confirmation; _resolve_recipient recognizes it and resolves across
# every ongoing project (auto-resolving if there's only one) instead of
# treating it as a literal project name.
_ALL_PROJECTS_SENTINEL = "__ANY_ONGOING_PROJECT__"

# Instagram Profile Send (recipient omitted) — "share Pankuri's instagram"
# names no one to send to, meaning "answer inline, in this chat" rather
# than "send to someone else". recipient_query can't be left empty (same
# reasoning as _ALL_PROJECTS_SENTINEL above — an empty value would trigger
# the generic "Who should this go to?" question instead of ever reaching
# build_confirmation). _resolve_instagram_target recognizes this sentinel
# and short-circuits to a direct reply — never passed to _resolve_recipient,
# which stays completely unaware of it.
_REPLY_IN_CHAT_SENTINEL = "__REPLY_IN_SAME_CHAT__"


def _strip_leading_trigger_preserve_newlines(text: str, triggers: List[str]) -> str:
    """nlu._strip_leading_trigger collapses ALL whitespace in the whole
    text (`" ".join(text.strip().split())`) — harmless for casting-agent's
    Move (a talent selector never needs multi-line structure), but fatal
    for Multi Manual Recipients' newline-separated name lists ("Send X to
    \\nAhana\\nKripa\\nRaj" would arrive at the recipient parser as one
    space-joined blob). Reuses the SAME shared function purely to
    identify WHICH trigger word matched (so trigger recognition itself is
    still the one existing implementation, not reimplemented), then
    strips only that verb off the front of the ORIGINAL text, leaving
    every internal newline untouched."""
    trig, _mangled = nlu._strip_leading_trigger(text or "", triggers)
    stripped = (text or "").strip()
    if trig is None:
        return stripped
    # "-" added to the strip charset (2026-08-20) alongside the existing
    # " :\n\t" — nlu._strip_leading_trigger now also recognizes a trigger
    # glued directly to "-" ("send-Talent-...", no space), and this needs
    # to strip that same separator off, or it's left dangling at the
    # front of the remainder (breaking every downstream hyphen-field split).
    return stripped[len(trig):].lstrip(" :-\n\t")


# ---------------------------------------------------------------------------
# Send Mode detection (2026-08-11, hardened 2026-08-12) — decides whether a
# "send"-triggered message is a stored requirement/template (existing,
# unchanged grammar), a literal custom message, or an Instagram-profile
# request. Checked once, right after the trigger verb is stripped, before
# any of the three existing extraction tiers run.
#
# A NEW competing intent (its own trigger list) still can't reliably win
# here — see the module docstring's addendum: the distinguishing signal
# for "instagram"/quoted-text can appear ANYWHERE in the message, not as a
# fixed prefix, and both would collide with the existing broad SEND_VERBS
# list either way. What CAN and does get an explicit trigger-phrase check,
# inside this same content-based dispatch, is the single most common
# custom-message phrasing ("send custom message ...") — see
# _CUSTOM_MESSAGE_PHRASE_RE below — so that phrasing's mode is never left
# solely to quote-detection succeeding.
# ---------------------------------------------------------------------------
_INSTAGRAM_KEYWORD_RE = re.compile(r"\binsta(?:gram)?\b", re.IGNORECASE)
# (2026-08-12, production incident) "send custom message" must win over
# template detection OUTRIGHT, not just via quote-sniffing — a real
# production message reached this trigger phrase and, for whatever
# specific reason in that live conversation, never got picked up as
# custom_message mode, silently falling through to template matching
# instead ("I couldn't find a template matching 'custom message...'"). An
# explicit phrase check removes any dependency on quote detection working
# perfectly for this specific, most-common phrasing — if the command
# BEGINS with "custom message" (after the verb), it IS custom_message
# mode, full stop; the quote/colon parsing below only decides what the
# body and recipient are, not whether this is custom_message mode at all.
_CUSTOM_MESSAGE_PHRASE_RE = re.compile(r"^\s*custom\s+message\b", re.IGNORECASE)
# Every quote character variant treated interchangeably as an opening OR
# closing delimiter — straight ASCII, and the curly left/right variants
# iOS/Android autocorrect commonly substitutes. Deliberately NOT a
# regex capture group (see _find_quote_span below) — a character-class
# based "nearest pair" match (the previous implementation) cannot survive
# a quote character embedded INSIDE the message body itself (explicitly
# required: "quotes inside the body"), since [^"]* stops at the FIRST
# embedded quote and truncates the payload.
_QUOTE_CHARS = "\"“”"
_QUOTE_CHAR_RE = re.compile("[" + re.escape(_QUOTE_CHARS) + "]")


def _find_paren_span(remainder: str) -> Optional["tuple[int, int, int, int]"]:
    """Bare-parenthesis delimiter — recognized ONLY when the message
    STARTS with "(" (ignoring leading whitespace), mirroring the sprint's
    own examples ("Send (Hi, please confirm your availability.) to X").
    Deliberately NOT triggered by an incidental paren anywhere ELSE in an
    ordinary sentence ("Send Reminder template to Ahana (the lead)") —
    that's prose, not a message delimiter, and must keep resolving as
    template mode, completely unaffected. Same first-open-to-last-close
    "one opaque payload" shape as _find_quote_span, just a different
    delimiter character and a stricter leading-position gate (parens are
    common in ordinary text in a way quote characters aren't, so this
    needs the extra precision to avoid misfiring)."""
    stripped = remainder.lstrip()
    if not stripped.startswith("("):
        return None
    offset = len(remainder) - len(stripped)
    closes = [i for i, ch in enumerate(remainder) if ch == ")"]
    if not closes:
        return None
    last = closes[-1]
    if last <= offset:
        return None
    return offset, offset + 1, last, last + 1


def _find_quote_span(remainder: str) -> Optional["tuple[int, int, int, int]"]:
    """FIRST-to-LAST quote-character span in the whole message — everything
    between them is treated as one opaque payload, never tokenized or
    inspected, regardless of how many quote characters appear INSIDE it
    (bullets, nested quotes, inch marks, etc. all pass through untouched).
    Only the outermost pair marks where the payload starts/ends; "Only
    after the closing quote should recipient parsing begin" is exactly
    what this gives the caller — everything after the LAST quote char (or
    before the first, for the quote-after-recipient phrasing) is fair game
    for recipient parsing, nothing in between ever is.

    Returns (open_start, open_end, close_start, close_end), or None if
    fewer than two quote characters are present at all."""
    matches = list(_QUOTE_CHAR_RE.finditer(remainder))
    if len(matches) < 2:
        return None
    first, last = matches[0], matches[-1]
    if last.start() <= first.end():
        return None
    return first.start(), first.end(), last.start(), last.end()


# ---------------------------------------------------------------------------
# Bulk Multi-Command Sends (2026-08-17) — "send X\\n\\nsend Y\\n\\nsend
# Z\\n\\nand confirm" as several fully independent commands in one
# message, sharing one confirmation/one "and confirm". Splitting mirrors
# casting-agent's split_actions rule 1 (a blank line, OR a later line
# independently starting with a recognized SEND trigger, each start a new
# chunk) — there is no rule-2 "and"-chaining equivalent here, since
# campaign sends never chain into each other the way casting's Add/Move
# do. QUOTE/PAREN-AWARE: a custom message's own body may span multiple
# lines and, in principle, could contain a line that happens to start
# with a trigger word ("Send this today, please") — tracked via a
# running open/close parity so a mid-quote line is NEVER mistaken for a
# new command boundary, which would otherwise corrupt the message body
# (a correctness requirement, not just tidiness — the whole message must
# reach create_batch verbatim).
# ---------------------------------------------------------------------------
def _starts_with_any_send_trigger(line: str, triggers: List[str]) -> bool:
    low = line.strip().lower()
    return any(low == t or low.startswith(t + " ") or low.startswith(t + ":") for t in triggers)


def _split_send_commands(text: str) -> List[str]:
    """Splits a message into independent send commands.

    Unlike casting-agent's Add/Move grammar (always one line per command),
    a single custom-message command legitimately spans multiple
    blank-line-separated sections: trigger line, blank, quoted body,
    blank lines between paragraphs INSIDE the quote, blank line, then a
    trailing "to X, Y, Z" recipient clause. A bare blank line is therefore
    NEVER treated as a boundary on its own — only a line that itself
    starts with a recognized SEND trigger word (and isn't inside an open
    quote/paren span) begins a new command.
    """
    all_triggers = sorted({t.lower() for t in SEND_TRIGGERS}, key=len, reverse=True)
    lines = (text or "").split("\n")
    chunks: List[List[str]] = [[]]
    quote_parity = 0
    paren_depth = 0
    for raw_line in lines:
        line = raw_line.strip()
        inside_open_span = (quote_parity % 2 == 1) or paren_depth > 0
        if not line:
            if chunks[-1]:
                chunks[-1].append(raw_line)
        elif chunks[-1] and not inside_open_span and _starts_with_any_send_trigger(line, all_triggers):
            chunks.append([line])
        else:
            chunks[-1].append(line)
        quote_parity += len(_QUOTE_CHAR_RE.findall(raw_line))
        paren_depth = max(0, paren_depth + raw_line.count("(") - raw_line.count(")"))
    chunks = [c for c in chunks if c]
    return ["\n".join(c).strip("\n") for c in chunks] if chunks else [text or ""]


def _detect_send_mode(remainder: str) -> str:
    if _INSTAGRAM_KEYWORD_RE.search(remainder):
        return "instagram"
    if _CUSTOM_MESSAGE_PHRASE_RE.match(remainder):
        return "custom_message"
    if _find_quote_span(remainder):
        return "custom_message"
    if _find_paren_span(remainder):
        return "custom_message"
    first_nl = remainder.find("\n")
    first_line = remainder[:first_nl] if first_nl != -1 else remainder
    if first_line.rstrip().endswith(":"):
        return "custom_message"
    return "requirement"


def _apply_recipient_split(out: Dict[str, str], recipient_part: str) -> None:
    """Shared by every custom-message recipient-clause shape below — same
    stage/project splitting Tier 3 (template mode) already applies to its
    own recipient clause (_split_stage_and_project), so "to the Follow Up
    list of Project A"/"to Follow Up and Approved for Project A and B"
    resolve identically for a custom message as they already do for a
    template send. A recipient clause that ISN'T stage-shaped (a talent
    list, a project name, a phone number, ...) is left completely
    untouched, exactly as before this existed."""
    if not recipient_part:
        return
    split = _split_stage_and_project(recipient_part)
    if split:
        stage_phrase, project_phrase = split
        out["stage_query"] = stage_phrase
        out["recipient_query"] = project_phrase or _ALL_PROJECTS_SENTINEL
        if project_phrase:
            out["project_query"] = project_phrase
    else:
        out["recipient_query"] = recipient_part


def _extract_custom_message_fields(remainder: str) -> Dict[str, str]:
    """Custom Message mode — the quoted/parenthesized/colon-delimited span
    is the EXACT text to send, verbatim (internal whitespace/newlines/
    blank lines/emoji/URLs/bullets/commas/embedded quotes/unicode
    untouched; only the shared FieldSpec.validate layer trims OUTER
    whitespace later, same as every other field on this intent)."""
    out: Dict[str, str] = {}
    span = _find_quote_span(remainder)
    if span:
        open_start, open_end, close_start, close_end = span
        out["source_query"] = remainder[open_end:close_start]
        # The recipient connector is searched OUTSIDE the quoted span only
        # — a "to"/"with" appearing INSIDE the message body itself (e.g.
        # "...call me tomorrow with your portfolio") must never be misread
        # as the recipient clause. The quote can come before OR after the
        # recipient list ("send custom message \"...\" to Riya" vs. "send
        # this to\nRiya\n\n\"text\""), so both sides are tried.
        after = remainder[close_end:]
        before = remainder[:open_start]
        # A bare "(" / ")" immediately wrapping the quoted span ("Send
        # (\"Hi...\") to X") is pure delimiter wrapping, not part of
        # either the message or the recipient clause — stripped before
        # searching either side for a "to"/"with" connector, so it can
        # never end up glued onto the recipient text.
        after_stripped = after.lstrip()
        if after_stripped.startswith(")"):
            after = after_stripped[1:]
        before_rstripped = before.rstrip()
        if before_rstripped.endswith("("):
            before = before_rstripped[:len(before_rstripped) - 1]
        r_m = _TO_OR_WITH_RE.search(after) or _TO_OR_WITH_RE.search(before)
        if r_m:
            _apply_recipient_split(out, r_m.group(1).strip(" .!?\n"))
        return out

    # Bare-parenthesis shape, no quotes at all ("Send (Hi, please confirm
    # your availability.) to X") — same opaque-payload treatment as a
    # quoted span, just delimited by () instead of quote characters.
    paren_span = _find_paren_span(remainder)
    if paren_span:
        open_start, open_end, close_start, close_end = paren_span
        out["source_query"] = remainder[open_end:close_start]
        after = remainder[close_end:]
        before = remainder[:open_start]
        r_m = _TO_OR_WITH_RE.search(after) or _TO_OR_WITH_RE.search(before)
        if r_m:
            _apply_recipient_split(out, r_m.group(1).strip(" .!?\n"))
        return out

    # Colon-body shape: "message Raj and Karan:\n<verbatim rest>".
    first_nl = remainder.find("\n")
    first_line = remainder[:first_nl] if first_nl != -1 else remainder
    stripped_first = first_line.rstrip()
    if stripped_first.endswith(":"):
        colon_idx = len(stripped_first) - 1
        _apply_recipient_split(out, first_line[:colon_idx].strip())
        out["source_query"] = remainder[colon_idx + 1:]
    return out


# "X's instagram" / "X's insta" — possessive subject reference.
_INSTA_POSSESSIVE_RE = re.compile(r"^(.*?)[\'’]s\s+insta(?:gram)?\b.*$", re.IGNORECASE | re.DOTALL)
# "instagram profile(s)/link(s) of|for X" — explicit connector shape.
_INSTA_OF_RE = re.compile(
    r"\binsta(?:gram)?\b\s*(?:profiles?|links?)?\s*(?:of|for)\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_INSTA_FILLER_RE = re.compile(r"\b(?:insta(?:gram)?|profiles?|links?)\b", re.IGNORECASE)
_LEADING_OF_FOR_RE = re.compile(r"^\s*(?:of|for)\s+", re.IGNORECASE)


def _extract_instagram_fields(remainder: str) -> Dict[str, str]:
    """Instagram Profile Send mode. `source_query` carries the subject
    talent name/list (whose Instagram), reusing the SAME field slot
    requirement-mode uses for "what to send" — `_resolve_instagram_target`
    is the only place that interprets it differently. `recipient_query`
    is empty/absent only when no "to X" clause was found at all, in which
    case it's set to _REPLY_IN_CHAT_SENTINEL so the required-field check
    never blocks asking a question that doesn't apply."""
    out: Dict[str, str] = {}
    r_m = _TO_OR_WITH_RE.search(remainder)
    if r_m:
        head = remainder[:r_m.start()].strip()
        recipient_part = r_m.group(1).strip(" .!?")
        if recipient_part:
            out["recipient_query"] = recipient_part
    else:
        head = remainder.strip()

    poss_m = _INSTA_POSSESSIVE_RE.match(head)
    if poss_m:
        subject = poss_m.group(1).strip()
    else:
        of_m = _INSTA_OF_RE.search(head)
        if of_m:
            subject = of_m.group(1).strip(" .!?")
        else:
            subject = _LEADING_OF_FOR_RE.sub("", _INSTA_FILLER_RE.sub("", head).strip()).strip()
    if subject:
        out["source_query"] = subject

    if "recipient_query" not in out:
        out["recipient_query"] = _REPLY_IN_CHAT_SENTINEL
    return out


# ---------------------------------------------------------------------------
# Simplified Command Language (2026-08-17) — "send - Talent(s) - Template -
# Project(s)" / "send - Template - Project(s) - Pipeline(s)" / "send custom
# message "..." - ..." / "send instagram - Talent(s) - Recipient" as the
# PREFERRED syntax, detected on the already-trigger-stripped remainder
# BEFORE any natural-language mode detection runs. Returns the SAME
# collected-field shape the natural-language tiers below already produce
# (source_query/recipient_query/project_query/stage_query/send_mode) —
# zero new resolution logic; _resolve_recipient_multi_aware's existing
# routing (project_query+recipient_query -> talents-narrowed-by-project;
# stage_query set -> project x stage union; recipient_query alone -> its
# own project-vs-talent detection) already handles every shape this
# produces. Returns None for anything that doesn't match one of the exact
# shapes below, falling straight through to natural-language parsing.
# ---------------------------------------------------------------------------
# "talentgram" (2026-08-19) — a new alias for the same Instagram Profile
# Send action ("send talentgram - Talent - Recipient" == "send instagram -
# Talent - Recipient"). Safe as a bare alternative here (unlike adding it
# to _INSTAGRAM_KEYWORD_RE below, which searches anywhere in free-text
# natural language and could false-trigger on an unrelated sentence that
# happens to mention the platform by name) because this regex only ever
# matches the FIRST word right after the "send"/"share"/etc. trigger verb
# has already been stripped — never a false positive mid-sentence.
_SIMPLE_INSTAGRAM_RE = re.compile(r"^\s*(?:insta(?:gram)?|talentgram)\b(?:\s+link)?\s*", re.IGNORECASE)


def _simple_field_looks_like_stages(text: str) -> Optional[str]:
    """Does this hyphen-grammar field name a stage (or comma/"and"-
    separated list of stages), as opposed to a project reference?
    Checked on the FIRST split fragment only — stage names never
    genuinely contain "and"/comma themselves, so one representative
    fragment is enough to tell "Follow Up,Approved" (all stages) apart
    from a project name that happens to be multi-valued too. Returns the
    ORIGINAL raw text (still comma/and-joined — _resolve_pipeline_stages/
    _resolve_project_stage_union do the actual per-fragment splitting) on
    a match, else None."""
    fragments = nlu.split_multi_names(text) or [text]
    stage_match = nlu.match_stage_phrase(fragments[0], PIPELINE_STAGE_ORDER)
    return text if stage_match.key else None


def parse_simple_send_command(remainder: str) -> Optional[Dict[str, str]]:
    stripped = (remainder or "").strip()

    # Custom message — quote/paren span reused verbatim from the existing
    # extraction (same "commas inside the message are never separators"
    # guarantee), followed by " - Talent(s)" or " - Project(s) - Pipeline(s)".
    span = _find_quote_span(stripped) or _find_paren_span(stripped)
    is_custom_phrase = bool(_CUSTOM_MESSAGE_PHRASE_RE.match(stripped))
    if span or is_custom_phrase:
        if not span:
            return None  # bare "custom message" phrase, no quote at all -> let natural language ask what to send
        open_start, open_end, close_start, close_end = span
        message_text = stripped[open_end:close_start]
        after = stripped[close_end:].lstrip()
        if not after.startswith("-"):
            return None
        rest = after[1:].strip()
        # Whitespace-tolerant (2026-08-20) — this shape is genuinely
        # either 1 or 2 fields, decided by content, so the count has to
        # be auto-detected (see nlu._split_hyphen_fields_auto's docstring
        # for why a naive "try 1, then 2" would be unsafe here).
        parts = nlu._split_hyphen_fields_auto(rest)
        if len(parts) == 1 and parts[0]:
            return {"send_mode": "custom_message", "source_query": message_text, "recipient_query": parts[0]}
        if len(parts) == 2 and all(parts):
            stage_field = _simple_field_looks_like_stages(parts[1])
            if stage_field:
                return {
                    "send_mode": "custom_message", "source_query": message_text,
                    "project_query": parts[0], "stage_query": stage_field,
                    "recipient_query": _ALL_PROJECTS_SENTINEL,
                }
        return None

    # Instagram — "instagram|insta(gram)?( link)? - Talent(s) - Recipient".
    insta_m = _SIMPLE_INSTAGRAM_RE.match(stripped)
    if insta_m:
        rest = stripped[insta_m.end():].strip()
        if rest.startswith("-"):
            rest = rest[1:].strip()
        elif rest:
            # "instagram of X" / "instagram X" natural phrasing, not the
            # hyphen grammar at all -> let the existing Instagram
            # extraction (which already handles those) take it instead.
            return None
        parts = nlu._split_hyphen_fields_auto(rest) if rest else []
        if parts and len(parts) == 2 and all(parts):
            return {"send_mode": "instagram", "source_query": parts[0], "recipient_query": parts[1]}
        if parts and len(parts) == 1 and parts[0]:
            return {"send_mode": "instagram", "source_query": parts[0], "recipient_query": _REPLY_IN_CHAT_SENTINEL}
        return None

    # Saved template — "Talent(s) - Template - Project(s)" (template
    # optional: "Talent(s) - Project(s)" leaves source_query unset, so the
    # existing generic "What should I send?" question asks for it, same
    # as any other missing-field case — no new default-template system)
    # or "Template - Project(s) - Pipeline(s)".
    parts = nlu._split_hyphen_fields(stripped, 2) or nlu._split_hyphen_fields(stripped, 3)
    if parts and len(parts) == 2 and all(parts):
        return {"recipient_query": parts[0], "project_query": parts[1]}
    if parts and len(parts) == 3 and all(parts):
        stage_field = _simple_field_looks_like_stages(parts[2])
        if stage_field:
            return {
                "source_query": parts[0], "project_query": parts[1],
                "stage_query": stage_field, "recipient_query": _ALL_PROJECTS_SENTINEL,
            }
        return {"recipient_query": parts[0], "source_query": parts[1], "project_query": parts[2]}
    return None


def _extract_one_send_command_fields(chunk_text: str) -> Dict[str, str]:
    """Extraction for ONE independent send command's raw text — the
    Simplified Command Language grammar first, then the full natural-
    language tier cascade, unchanged from before Bulk Multi-Command Sends
    existed. Never touches "and confirm" — that's stripped once, globally,
    by the caller (extract_send_requirement_fields), before the message is
    even split into per-command chunks."""
    remainder = _strip_leading_trigger_preserve_newlines(chunk_text, SEND_TRIGGERS)

    simple = parse_simple_send_command(remainder)
    if simple is not None:
        # Old Interface Deprecation (2026-08-30) — the structured hyphen
        # grammar ("send - Talent - Template - Project", "instagram -
        # Talent - Recipient", "custom message "..." - Talent") this
        # recognizes still WORKS underneath (parse_simple_send_command
        # itself is completely untouched — the spec explicitly says "do
        # not delete underlying execution functionality"), but is no
        # longer the advertised/accepted user-facing interface: redirect
        # to the new natural-language format instead of silently
        # executing it. SOURCE/RECIPIENT get a placeholder (never shown —
        # see _send_requirement_try_auto_execute's redirect, which fires
        # before either field is ever read) purely so the generic
        # missing-field flow doesn't intercept this first.
        return {
            SOURCE_QUERY_FIELD.key: _PLAN_PLACEHOLDER,
            RECIPIENT_QUERY_FIELD.key: _PLAN_PLACEHOLDER,
            LEGACY_SYNTAX_FIELD.key: "1",
        }

    send_mode = _detect_send_mode(remainder)
    if send_mode == "instagram":
        out = _extract_instagram_fields(remainder)
        out["send_mode"] = "instagram"
        return out
    if send_mode == "custom_message":
        out = _extract_custom_message_fields(remainder)
        out["send_mode"] = "custom_message"
        return out

    out: Dict[str, str] = {}

    # Tier 1: legacy explicit-template grammar.
    t_m = _LEGACY_TEMPLATE_RE.search(remainder)
    if t_m:
        source_query = t_m.group(1).strip(" .!?")
        if source_query:
            out["source_query"] = source_query
            head = remainder[:t_m.start()].strip()
            r_m = _TO_OR_WITH_RE.search(head)
            if r_m:
                recipient_query = r_m.group(1).strip(" .!?")
                if recipient_query:
                    out["recipient_query"] = recipient_query
                    head = head[:r_m.start()].strip()
            if head:
                stage_key, _ambig, _rest = nlu.extract_stage_phrase(head, PIPELINE_STAGE_ORDER)
                if stage_key:
                    out["stage_query"] = stage_key
            return out

    # Tier 2: "<verb> <recipient> the <source>" inverted shape — only when
    # there's no to/with connector anywhere (tier 3 owns that shape).
    if not _TO_OR_WITH_RE.search(remainder):
        idx = remainder.lower().rfind(" the ")
        if idx > 0:
            recipient_part = remainder[:idx].strip(" .!?")
            source_part = remainder[idx + len(" the "):].strip(" .!?")
            if recipient_part and source_part:
                out["recipient_query"] = recipient_part
                out["source_query"] = source_part
                return out

    # Tier 3: generic "<source> to|with <recipient>" shape.
    m = _TO_OR_WITH_RE.search(remainder)
    if m:
        recipient_part = m.group(1).strip(" .!?")
        source_part = remainder[:m.start()].strip(" .!?")
        if recipient_part:
            split = _split_stage_and_project(recipient_part)
            if split:
                stage_phrase, project_phrase = split
                out["stage_query"] = stage_phrase  # validated centrally in _resolve_recipient
                out["recipient_query"] = project_phrase or _ALL_PROJECTS_SENTINEL
                if project_phrase:
                    out["project_query"] = project_phrase
            else:
                out["recipient_query"] = recipient_part
        if source_part:
            out["source_query"] = source_part

    return out


def extract_send_requirement_fields(text: str) -> Dict[str, str]:
    # "...and confirm" — stripped FIRST, before mode detection/trigger
    # parsing/multi-command splitting, exactly like casting-agent's
    # preprocess_command does, so it applies ONCE, globally, to every
    # command in the message rather than needing to be repeated per
    # command. Reuses casting_pipeline_nlu's implementation verbatim — no
    # second "and confirm" recognizer.
    raw_text, auto_confirm = nlu.strip_and_confirm(text or "")

    chunks = _split_send_commands(raw_text)
    if len(chunks) > 1:
        # Bulk Multi-Command Sends (2026-08-17) — each chunk is a fully
        # independent send (its own template/message, its own recipients);
        # unlike casting-agent's plan engine there is no cross-command
        # fan-out/dedup concern here (see _execute_send_plan), so a plan
        # step is just that command's own already-existing extraction
        # output, unmodified.
        steps = [_extract_one_send_command_fields(c) for c in chunks]
        out: Dict[str, str] = {
            PLAN_FIELD.key: json.dumps(steps),
            # Required fields must be non-empty for the generic engine's
            # missing-field check to ever reach build_confirmation/
            # try_auto_execute at all — the real per-command values live
            # inside the plan JSON above, exactly like casting-agent's own
            # _PLAN_PLACEHOLDER convention.
            SOURCE_QUERY_FIELD.key: _PLAN_PLACEHOLDER,
            RECIPIENT_QUERY_FIELD.key: _PLAN_PLACEHOLDER,
        }
        if auto_confirm:
            out[AUTO_CONFIRM_FIELD.key] = "1"
        return out

    out = _extract_one_send_command_fields(chunks[0] if chunks else raw_text)
    if auto_confirm:
        out[AUTO_CONFIRM_FIELD.key] = "1"
    return out


def _validate_query_text(raw: str) -> ValidationResult:
    v = (raw or "").strip()
    if not v:
        return ValidationResult(ok=False, error="I didn't catch that — please try again.")
    return ValidationResult(ok=True, value=v)


SOURCE_QUERY_FIELD = FieldSpec(
    key="source_query", label="Message Source",
    question="What should I send? (a project name or a template name)",
    validate=_validate_query_text, aliases=["source", "template", "message"],
)
RECIPIENT_QUERY_FIELD = FieldSpec(
    key="recipient_query", label="Recipients",
    question="Who should this go to?",
    validate=_validate_query_text, aliases=["recipient", "recipients", "to"],
)
# Optional — never blocks confirmation. Only meaningful when the recipient
# resolves to a whole PROJECT (absent means "every stage"). Kept as a real
# field so the generic "Key = value" edit parser can still match a
# "Pipeline = Approved" edit line against its label/aliases. Carries RAW
# (possibly multi-item, "Follow Up and Approved") text — actual splitting
# into individual stage keys happens at resolution time, in
# _resolve_pipeline_stages, the same "extraction is a pure text transform,
# resolution does the DB-backed matching" split every other field here
# already follows.
STAGE_QUERY_FIELD = FieldSpec(
    key="stage_query", label="Pipeline",
    question="Which pipeline stage?",
    validate=_validate_query_text, aliases=["stage", "pipeline"],
    required=False,
)
# Bulk Multi-Target Sends (2026-08-17) — an explicit project reference
# carried PARALLEL to recipient_query, populated whenever a project
# reference was found either on the recipient side ("...to the Follow Up
# list of Project A") or the source side ("Follow Up template for Project
# A..."). Raw (possibly multi-item) text, same deferred-resolution
# philosophy as STAGE_QUERY_FIELD — see _resolve_multi_project_names.
# Never required: its ABSENCE is exactly today's single-project behaviour,
# completely unaffected.
PROJECT_QUERY_FIELD = FieldSpec(
    key="project_query", label="Project",
    question="Which project?",
    validate=_validate_query_text, aliases=["project", "projects"],
    required=False,
)
# Private metadata, never asked about (question="") and never blocks
# confirmation (required=False) — a real FieldSpec purely so dispatcher.py
# carries send_mode forward in `collected` across every turn (bare extra
# keys returned by extract_fields that don't match a declared FieldSpec.key
# are silently dropped — see dispatcher.py's collected-building loop).
# Absent/empty means "requirement" (the original, only, mode).
SEND_MODE_FIELD = FieldSpec(
    key="send_mode", label="Mode", question="",
    validate=_validate_query_text, required=False,
)
# "...and confirm" (2026-08-17) — same convention casting-agent already
# established (casting_pipeline_nlu.strip_and_confirm): skip the approval
# card and send immediately. "confirm" has no other meaning anywhere in
# this agent's own vocabulary (no pipeline-stage concept exists here at
# all — this agent only ever SENDS messages), so there is no keyword
# collision to resolve, unlike casting-agent's own "confirm". Never
# required, never asked about — absence means "show the confirmation card"
# (100% of existing behaviour, unaffected).
AUTO_CONFIRM_FIELD = FieldSpec(
    key="_auto_confirm", label="AutoConfirm", question="",
    validate=_validate_query_text, required=False,
)
# Bulk Multi-Command Sends (2026-08-17) — a JSON list of independent
# per-command extraction-field dicts (see extract_send_requirement_fields'
# multi-chunk branch), mirroring casting-agent's own PLAN_FIELD. Never
# required/asked about; absent means "not a multi-command message" — the
# single-command path (100% of existing behaviour) is completely
# unaffected by this field's mere existence.
PLAN_FIELD = FieldSpec(
    key="_plan", label="Plan", question="",
    validate=_validate_query_text, required=False,
)
_PLAN_PLACEHOLDER = "__plan__"
# Old Interface Deprecation (2026-08-30) — set by _extract_one_send_command_
# fields when the message matched the OLD hyphen grammar ("send - Talent -
# Template - Project", "instagram - Talent - Recipient", "custom message
# "..." - Talent"). Same hidden-field convention as AUTO_CONFIRM_FIELD/
# PLAN_FIELD above; never required/asked about, absent means "ordinary
# natural-language command" (100% of existing behaviour unaffected).
LEGACY_SYNTAX_FIELD = FieldSpec(
    key="_legacy_syntax", label="LegacySyntax", question="",
    validate=_validate_query_text, required=False,
)


# ---------------------------------------------------------------------------
# Message source resolution — try the raw phrase first (so an explicit
# template name that happens to contain a "filler" word like "Custom
# Message" still matches exactly), then a filler-stripped version (so
# "Toyota requirement" resolves via the bare project-ish word "Toyota"
# against a template literally named after that project — confirmed real
# production pattern, not a hypothetical).
#
# "this"/"that"/"last generated"/"yesterday's"/etc. are deliberately NOT
# treated as filler to strip — those name a capability this engine doesn't
# have (quoted-message reuse, message history, on-the-fly requirement
# generation — see the module docstring's v1 scope). A phrase that's
# ENTIRELY made of these reference/history words is caught before template
# matching even runs, so it gets an explicit "not supported, name a
# template instead" reply — never a silent no-match, and never a stray
# fuzzy match against an unrelated template.
# ---------------------------------------------------------------------------
_SOURCE_FILLER_WORDS = {
    "requirement", "requirements", "brief", "briefs", "message", "template",
    "the", "content", "info", "information",
}
_UNSUPPORTED_REFERENCE_WORDS = {"this", "that", "it"}
_UNSUPPORTED_HISTORY_WORDS = {
    "last", "latest", "previous", "prior", "yesterday", "yesterdays",
    "earlier", "history", "recent", "generated",
}
_UNSUPPORTED_SOURCE_REPLY = (
    "I can't reuse an earlier message, a quoted reply, or message history — "
    "there's no message-history lookup wired up for this agent. Please name "
    "an existing WhatsApp template instead (e.g. \"Follow Up\")."
)


def _strip_source_filler(q: str) -> str:
    tokens = [t for t in q.split() if t.lower() not in _SOURCE_FILLER_WORDS]
    return " ".join(tokens).strip()


_SOURCE_FOR_PROJECT_RE = re.compile(r"^(.*?)\s+(?:for|of)\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _strip_source_project_clause(q: str) -> "tuple[str, str]":
    """Best-effort split of a trailing "for/of <project(s)>" clause off a
    source phrase — "Follow Up template for Project A and Project B" ->
    ("Follow Up template", "Project A and Project B"). Purely speculative:
    _resolve_requirement_target only commits to this split when the
    REMAINING head text still resolves to a real template, tried ONLY
    after the raw and filler-stripped phrases have both already failed to
    match anything — so a template whose own name genuinely contains
    "for"/"of" (matched in full on the first attempt) is never touched."""
    m = _SOURCE_FOR_PROJECT_RE.match(q)
    if not m:
        return q, ""
    head, tail = m.group(1).strip(), m.group(2).strip()
    return (head, tail) if head else (q, "")


_TOKEN_PUNCT_RE = re.compile(r"[.,!?'\"]")


def _unsupported_source_reply(q: str) -> Optional[str]:
    tokens = {_TOKEN_PUNCT_RE.sub("", t).lower() for t in q.split()}
    tokens.discard("")
    non_filler = tokens - _SOURCE_FILLER_WORDS
    if not non_filler:
        return None
    if non_filler <= _UNSUPPORTED_REFERENCE_WORDS:
        return _UNSUPPORTED_SOURCE_REPLY
    if non_filler & _UNSUPPORTED_HISTORY_WORDS:
        return _UNSUPPORTED_SOURCE_REPLY
    return None


async def _resolve_source(source_query: str) -> TemplateMatch:
    q = (source_query or "").strip()
    if not q:
        return TemplateMatch(error="What should I send? (a project name or a template name)")
    unsupported = _unsupported_source_reply(q)
    if unsupported:
        return TemplateMatch(error=unsupported)
    templates = await _fetch_templates()
    match = resolve_template_by_name(q, templates)
    if match.template or match.ambiguous:
        return match
    stripped = _strip_source_filler(q)
    if stripped and stripped.lower() != q.lower():
        match2 = resolve_template_by_name(stripped, templates)
        if match2.template or match2.ambiguous:
            return match2
    return match


# ---------------------------------------------------------------------------
# Recipient resolution — decides WHICH existing source_type+SourceParams a
# free-text recipient phrase means. Every branch below ends in the SAME
# real recipient engine (resolve_recipients_engine, called from inside
# create_batch) — this function only picks the routing, never resolves a
# recipient's phone/group itself.
# ---------------------------------------------------------------------------
@dataclass
class _RecipientTarget:
    ok: bool
    source_type: Optional[str] = None
    source_params: Optional[SourceParams] = None
    display_label: str = ""
    error: Optional[str] = None
    ambiguous: Optional[AmbiguousEntity] = None
    # Set only when resolved via an EXPLICIT single pipeline stage (not the
    # "whole project, every stage" default) — lets the confirmation-card
    # builder show the richer Pipeline/Project/Stage/count summary instead
    # of the generic per-recipient list.
    project_label: Optional[str] = None
    pipeline_stage_label: Optional[str] = None
    # Bulk Multi-Target Sends (2026-08-17) — non-blocking note surfaced on
    # the confirmation card ("Couldn't find project: X") when a multi-
    # project/multi-talent resolution partially failed but still found AT
    # LEAST one real recipient — see _resolve_project_stage_union /
    # _resolve_talents_narrowed_by_projects. None (every existing single-
    # target resolution) means nothing to show, unaffected.
    warning: Optional[str] = None
    # Production fix (2026-08-19) — set instead of source_type/source_params
    # when 2+ named projects each contribute real recipients (a named-talent
    # send across several projects, or a stage-union send across several
    # projects): [(project_id, project_label, talent_ids)], one entry per
    # project with at least one match. talent_ids is the specific narrowed
    # set for a named-talent send, or [] (no narrowing — every talent in the
    # given stages) for a stage-union send. _create_batch_for_target fans
    # this out into one source_type="PROJECT" create_batch call PER entry —
    # each renders THAT project's own {{project_name}}/{{shoot_dates}}/
    # {{budget}}/{{submission_link}} etc. via the exact same, unmodified
    # PROJECT-source rendering path create_batch already uses for a single-
    # project send, instead of collapsing everyone into one MANUAL-source
    # batch that skips project-variable rendering entirely (the bug this
    # fixes). None (the overwhelmingly common case — a single project, or
    # no project at all) means "one ordinary create_batch call," unaffected.
    multi_project_targets: Optional[List[Tuple[str, str, List[str]]]] = None
    # Pipeline stages shared by EVERY entry in multi_project_targets — the
    # explicit stage_list for a stage-union send (_resolve_project_stage_
    # union), or None for a named-talent send (_resolve_talents_narrowed_
    # by_projects, which narrows by talent_ids instead and always searches
    # every stage). Ignored when multi_project_targets is None.
    multi_project_stage_list: Optional[List[str]] = None


_PHONE_RE = re.compile(r"^[\d\s\+\-\(\)]{7,}$")
_NAME_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _fuzzy_match_is_safe(
    query_fragment: str, matched_label: str, *, ignore_words: Optional[set] = None,
) -> bool:
    """(2026-08-09, live production incident + one caught in testing)
    "Ami Trivedi" fuzzy-matched "Kripa Trivedi" — the ONLY talent sharing
    the surname "Trivedi" — and a real WhatsApp message went to the wrong
    real person. The SAME shape of bug was then reproduced against
    resolve_project_by_name too: a disambiguation reply "ZList... Alpha"
    (naming a saved contact list) matched a real, unrelated project
    "QATEST Project Alpha" purely because both share the single word
    "Alpha" — resolve_project_by_name's per-token fuzzy tier
    (_project_name_similarity's "best_token" component, added 2026-08-05
    for a different, legitimate purpose) scores a single exactly-matching
    token as 1.0 even when every other token is unrelated. Both matchers'
    "lone survivor above the cutoff auto-accepts" rule is correct and
    heavily relied on for casting-agent's MOVE/ADD (internal pipeline
    edits with an UNDO) — not reversible when the result becomes an
    external send target instead.

    Deliberately NOT touching nlu.resolve_against_candidates or
    resolve_project_by_name themselves — both are heavily tested and
    casting-agent depends on their exact tolerance; this risk is specific
    to call sites that turn a match straight into a WhatsApp send target.
    Instead: an extra, local gate on the result before trusting it —
    every significant word the user actually typed must appear in the
    matched name/label (a surname-only query like "Trivedi" alone still
    legitimately matches
    a single same-surnamed talent; a two-word query where only the surname
    overlaps does not).

    `ignore_words` (2026-08-17, Bulk Multi-Target Sends) — optional set of
    words dropped from BOTH sides before the subset check, passed by
    PROJECT-context callers as casting_pipeline_nlu's own
    _PROJECT_FILLER_WORDS (the exact same list resolve_project_by_name
    already uses to tolerate "Toyota Glanza Campaign" for a project
    literally named "Toyota Glanza") — without this, this gate would
    silently defeat that tolerance by rejecting "campaign"/"project"/
    "film" as an unmatched query word even when the underlying matcher
    correctly treated it as filler. None (every existing TALENT-context
    caller) preserves the exact original token-subset check byte-for-byte
    — names have no filler-word concept."""
    q_tokens = set(_NAME_TOKEN_RE.findall((query_fragment or "").lower()))
    l_tokens = set(_NAME_TOKEN_RE.findall((matched_label or "").lower()))
    if ignore_words:
        q_tokens -= ignore_words
        l_tokens -= ignore_words
    if not q_tokens:
        return False
    return q_tokens <= l_tokens


async def _resolve_pipeline_stage(stage_query: str) -> "tuple[Optional[str], Optional[_RecipientTarget]]":
    """Normalizes a raw stage phrase (from either extraction tier) via the
    REUSED casting-pipeline stage matcher — no separate stage synonym
    table. Returns (normalized_key, None) on success, or (None,
    _RecipientTarget) with either an `ambiguous` (routes through the
    shared disambiguation engine, same as project/talent/CRM/saved-list)
    or a plain `error` (unrecognized stage) already filled in, which the
    caller returns immediately."""
    if not stage_query:
        return None, None
    # A leading "the"/"a"/"an" ("the Follow Up list" -> stage_phrase "the
    # Follow Up") makes the shared matcher treat the phrase as merely
    # fuzzy-similar to a real stage rather than a confident exact match —
    # stripped here, local to this agent's OWN stage resolution, rather
    # than loosening casting_pipeline_nlu.match_stage_phrase itself (which
    # casting-agent depends on unchanged).
    stripped_query = re.sub(r"^\s*(?:the|a|an)\s+", "", stage_query, flags=re.IGNORECASE)
    stage_match = nlu.match_stage_phrase(stripped_query or stage_query, PIPELINE_STAGE_ORDER)
    if stage_match.key:
        return stage_match.key, None
    if stage_match.ambiguous and len(stage_match.ambiguous) >= 2:
        # Genuine ambiguity (2+ real, comparably-close stages) is
        # disambiguation-worthy. A SINGLE weak fuzzy suggestion (e.g.
        # "Selected" scoring just above the cutoff against "Rejected" —
        # spelling-similar, not remotely the same word) is NOT: that's a
        # guess, not a choice, and matches this session's established
        # "don't trust a lone low-confidence coincidence" safety principle
        # (see _fuzzy_match_is_safe) — falls through to the same clear
        # "I don't recognize this stage" error a totally unmatched phrase
        # gets, rather than offering one probably-wrong option to pick.
        return None, _RecipientTarget(
            ok=False,
            ambiguous=AmbiguousEntity(
                entity_type="pipeline_stage", field_key="stage_query",
                candidates=[disambiguation.Candidate(id=lbl, label=lbl) for lbl in stage_match.ambiguous],
            ),
        )
    valid = ", ".join(nlu.stage_label(s) for s in PIPELINE_STAGE_ORDER)
    return None, _RecipientTarget(
        ok=False,
        error=f'I don\'t recognize the stage "{stage_query}" — valid stages are: {valid}.',
    )


# ---------------------------------------------------------------------------
# Bulk Multi-Target Sends (2026-08-17) — one-or-many PROJECTS x one-or-many
# STAGES, resolved as a full, deduplicated recipient set BEFORE any send:
# Template/message -> Projects -> Stages -> Recipient sets -> Unique
# recipients -> (existing, unmodified) confirmation/create_batch. Every
# per-project/per-stage recipient lookup below still goes through the SAME
# resolve_recipients_engine call (via create_batch's dry-run) the original
# single-project path already made — this section's only new work is
# looping over N projects/stages and merging + deduplicating the results,
# never a parallel recipient-resolution implementation.
# ---------------------------------------------------------------------------
_PENDING_MULTI_STAGE_PICK_KEY = "_pending_multi_stage_pick"
_PENDING_MULTI_STAGE_FRAGMENTS_KEY = "_pending_multi_stage_fragments"
_PENDING_MULTI_STAGE_INDEX_KEY = "_pending_multi_stage_index"


async def _resolve_pipeline_stages(
    stage_query: str,
) -> "tuple[Optional[List[str]], Optional[_RecipientTarget]]":
    """Multi-stage counterpart of _resolve_pipeline_stage — "Follow Up and
    Approved" -> ["follow_up", "approved"]. A single stage (no "and"/comma
    present — the overwhelmingly common case) delegates straight to
    _resolve_pipeline_stage unchanged, so its existing ambiguous/error
    shape is completely unaffected. For 2+ stage fragments: each resolves
    independently, but an unrecognized OR ambiguous fragment stops
    immediately rather than being silently dropped — stages are a small,
    fixed, well-known vocabulary (unlike a talent/project name), so a bad
    stage word is virtually always a typo worth fixing outright. An
    ambiguous fragment still preserves every OTHER already-resolved
    fragment across the disambiguation round trip (same fragments+index
    resume pattern _resolve_multi_project_names/_resume_pending_multi_
    project use, just for stages)."""
    fragments = nlu.split_multi_names(stage_query) if stage_query else []
    if len(fragments) <= 1:
        key, err = await _resolve_pipeline_stage(stage_query)
        if err:
            return None, err
        return ([key] if key else None), None

    resolved_keys: List[str] = []
    for idx, frag in enumerate(fragments):
        key, err = await _resolve_pipeline_stage(frag)
        if err:
            if err.ambiguous:
                return None, _RecipientTarget(
                    ok=False,
                    ambiguous=AmbiguousEntity(
                        entity_type="pipeline_stage", field_key=_PENDING_MULTI_STAGE_PICK_KEY,
                        candidates=err.ambiguous.candidates,
                        extra_collected={
                            _PENDING_MULTI_STAGE_FRAGMENTS_KEY: json.dumps(fragments),
                            _PENDING_MULTI_STAGE_INDEX_KEY: str(idx),
                        },
                    ),
                )
            return None, err
        if key and key not in resolved_keys:
            resolved_keys.append(key)
    return resolved_keys, None


async def _resume_pending_multi_stage(collected: dict, ctx: ExecContext) -> dict:
    """Mirror of _resume_pending_multi_project, for a multi-stage list
    where one stage word was ambiguous. A no-op when no multi-stage
    disambiguation is pending."""
    picked_label = collected.get(_PENDING_MULTI_STAGE_PICK_KEY)
    fragments_json = collected.get(_PENDING_MULTI_STAGE_FRAGMENTS_KEY)
    index_raw = collected.get(_PENDING_MULTI_STAGE_INDEX_KEY)
    if not picked_label or fragments_json is None or index_raw is None:
        return collected
    try:
        fragments = json.loads(fragments_json)
        index = int(index_raw)
    except (TypeError, ValueError):
        fragments = None
        index = -1
    new_collected = dict(collected)
    for key in (_PENDING_MULTI_STAGE_PICK_KEY, _PENDING_MULTI_STAGE_FRAGMENTS_KEY, _PENDING_MULTI_STAGE_INDEX_KEY):
        new_collected.pop(key, None)
    if isinstance(fragments, list) and 0 <= index < len(fragments):
        fragments[index] = picked_label
        new_collected["stage_query"] = ", ".join(fragments)
    await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, collected=new_collected)
    return new_collected


@dataclass
class _MultiProjectResolution:
    resolved: List[Tuple[str, str]] = field(default_factory=list)  # (project_id, project_label)
    not_found: List[str] = field(default_factory=list)
    # (fragment, ambiguous candidates [{"id","label"}], index-into-fragments)
    # — set only for the FIRST ambiguous fragment; mirrors
    # _MultiRecipientResolution.ambiguous exactly.
    ambiguous: Optional[Tuple[str, List[Dict[str, str]], int]] = None


async def _resolve_multi_project_names(
    fragments: List[str], projects: List[Dict[str, str]],
) -> _MultiProjectResolution:
    """Resolves each project-name fragment INDEPENDENTLY against the SAME
    project matcher every other project reference in this codebase uses
    (nlu.resolve_project_by_name + the safety gate) — no separate
    matching logic. Scans the WHOLE list before returning (mirrors
    _resolve_multi_recipient_names' own "never mask a not-found by an
    earlier ambiguity" rule), pausing on the FIRST ambiguous fragment
    only."""
    out = _MultiProjectResolution()
    seen_ids: set = set()
    for idx, frag in enumerate(fragments):
        match = nlu.resolve_project_by_name(frag, projects)
        if match.project and _fuzzy_match_is_safe(
            frag, match.project["label"], ignore_words=nlu._PROJECT_FILLER_WORDS,
        ):
            pid = match.project["id"]
            if pid not in seen_ids:
                seen_ids.add(pid)
                out.resolved.append((pid, match.project["label"]))
            continue
        if match.ambiguous:
            if out.ambiguous is None:
                out.ambiguous = (frag, match.ambiguous, idx)
            continue
        out.not_found.append(frag)
    return out


_PENDING_MULTI_PROJECT_PICK_KEY = "_pending_multi_project_pick"
_PENDING_MULTI_PROJECT_FRAGMENTS_KEY = "_pending_multi_project_fragments"
_PENDING_MULTI_PROJECT_INDEX_KEY = "_pending_multi_project_index"


async def _resume_pending_multi_project(collected: dict, ctx: ExecContext) -> dict:
    """Mirror of _resume_pending_multi_recipient, for a multi-project list
    where one project name was ambiguous. A no-op when no multi-project
    disambiguation is pending."""
    picked_label = collected.get(_PENDING_MULTI_PROJECT_PICK_KEY)
    fragments_json = collected.get(_PENDING_MULTI_PROJECT_FRAGMENTS_KEY)
    index_raw = collected.get(_PENDING_MULTI_PROJECT_INDEX_KEY)
    if not picked_label or fragments_json is None or index_raw is None:
        return collected
    try:
        fragments = json.loads(fragments_json)
        index = int(index_raw)
    except (TypeError, ValueError):
        fragments = None
        index = -1
    new_collected = dict(collected)
    for key in (_PENDING_MULTI_PROJECT_PICK_KEY, _PENDING_MULTI_PROJECT_FRAGMENTS_KEY, _PENDING_MULTI_PROJECT_INDEX_KEY):
        new_collected.pop(key, None)
    if isinstance(fragments, list) and 0 <= index < len(fragments):
        fragments[index] = picked_label
        new_collected["project_query"] = ", ".join(fragments)
    await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, collected=new_collected)
    return new_collected


async def _probe_template() -> Optional[Dict[str, str]]:
    """Any real, always-present template works to run a resolution-only
    dry-run preview (jobs/routing only — the actual template used for the
    real send is target.template, resolved separately and unaffected).
    Reuses the seeded custom-message template rather than inventing a
    second bootstrap-guaranteed record."""
    return await _fetch_custom_template()


async def _resolve_project_stage_union(project_query: str, stage_query: str) -> _RecipientTarget:
    """Send template/message -> one-or-many PROJECTS x one-or-many
    STAGES. A project fragment that doesn't resolve is reported as a
    warning rather than blocking the whole send, as long as AT LEAST one
    project resolved — "don't lose confidently resolved recipients while
    waiting on one unresolved name" (see _resolve_talent_names' matching
    fix), generalized to projects here too."""
    stage_keys, stage_err = await _resolve_pipeline_stages(stage_query)
    if stage_err:
        return stage_err
    stage_list = stage_keys if stage_keys else list(PIPELINE_STAGE_ORDER)
    stage_label_str = " + ".join(nlu.stage_label(s) for s in stage_keys) if stage_keys else "All stages"

    q = (project_query or "").strip()
    projects = await _fetch_ongoing_projects()
    not_found_projects: List[str] = []

    if not q or q == _ALL_PROJECTS_SENTINEL:
        if not projects:
            return _RecipientTarget(ok=False, error="There are no ongoing projects to send to.")
        if len(projects) == 1:
            resolved_projects = [(projects[0]["id"], projects[0]["label"])]
        else:
            return _RecipientTarget(
                ok=False,
                ambiguous=AmbiguousEntity(
                    entity_type="project", field_key="project_query",
                    candidates=[disambiguation.Candidate(id=p["id"], label=p["label"]) for p in projects],
                ),
            )
    else:
        fragments = nlu.split_multi_names(q)
        multi = await _resolve_multi_project_names(fragments, projects)
        if multi.ambiguous:
            frag, amb_candidates, idx = multi.ambiguous
            return _RecipientTarget(
                ok=False,
                ambiguous=AmbiguousEntity(
                    entity_type="project", field_key=_PENDING_MULTI_PROJECT_PICK_KEY,
                    candidates=[disambiguation.Candidate(id=c["id"], label=c["label"]) for c in amb_candidates],
                    extra_collected={
                        _PENDING_MULTI_PROJECT_FRAGMENTS_KEY: json.dumps(fragments),
                        _PENDING_MULTI_PROJECT_INDEX_KEY: str(idx),
                    },
                ),
            )
        if not multi.resolved:
            return _RecipientTarget(ok=False, error=f'I couldn\'t find a project matching "{q}".')
        resolved_projects = multi.resolved
        not_found_projects = multi.not_found

    if len(resolved_projects) == 1:
        # The overwhelmingly common case — a single project (whether named
        # explicitly or auto-picked as the only ongoing one) — stays on
        # the EXACT SAME source_type="PROJECT" + SourceParams(project_id,
        # pipeline_stages) path _resolve_recipient's own stage handling
        # always used before this existed: real talent_ids are preserved
        # (Exclude/Include-by-name, campaign-history "Project" attribution,
        # and every other PROJECT-source-specific behavior stay intact),
        # and no extra dry-run probe round trip is spent. The MANUAL-merge
        # path below exists ONLY for a genuine 2+-project union, where
        # SourceParams.project_id (singular) has no way to represent more
        # than one project at all.
        pid, plabel = resolved_projects[0]
        warning = f"Couldn't find project: {', '.join(not_found_projects)}" if not_found_projects else None
        return _RecipientTarget(
            ok=True, source_type="PROJECT",
            source_params=SourceParams(project_id=pid, pipeline_stages=stage_list),
            display_label=f"{plabel} — {stage_label_str}",
            project_label=plabel, pipeline_stage_label=stage_label_str,
            warning=warning,
        )

    admin = await _service_admin()
    probe_template = await _probe_template()
    if not probe_template:
        return _RecipientTarget(
            ok=False, error="The built-in custom-message template is missing — please contact an admin.",
        )

    # NOTE (2026-08-19 production-fix audit): this MANUAL-merge path has
    # the SAME latent rendering gap _resolve_talents_narrowed_by_projects'
    # fix closes (a MANUAL-source create_batch call never renders
    # {{project_name}}/{{shoot_dates}}/{{budget}}/{{submission_link}}).
    # Deliberately left AS-IS here: this function's whole point is ONE
    # shared message per person across the named projects' matching
    # stage(s), deduplicated by talent — an existing, intentionally-tested
    # behavior (test_template_multiple_projects_multiple_stages_dedup: a
    # talent in the SAME stage across 2 named projects must be queued
    # exactly once). Fanning this out per-project like the named-talent
    # fix would either break that dedup guarantee or require inventing a
    # new "which project wins" policy nobody asked for — out of scope for
    # a targeted fix to the reported bug. Flagged as a known limitation.
    merged: Dict[str, dict] = {}
    for pid, _plabel in resolved_projects:
        try:
            preview = await create_batch(
                BatchIn(
                    source_type="PROJECT",
                    source_params=SourceParams(project_id=pid, pipeline_stages=stage_list),
                    template_id=probe_template["id"], is_dry_run=True,
                ),
                admin=admin,
            )
        except HTTPException:
            continue
        for job in preview["jobs"]:
            merged.setdefault(job["recipient_id"], job)

    project_labels = ", ".join(plabel for _pid, plabel in resolved_projects)
    if not merged:
        note = f" (also couldn't find: {', '.join(not_found_projects)})" if not_found_projects else ""
        return _RecipientTarget(
            ok=False, error=f"No recipients found for {stage_label_str} across {project_labels}.{note}",
        )

    contacts = [
        ManualContact(
            name=job.get("talent_name") or "",
            phone=job["destination"] if job.get("destination_type") == "number" else "",
            whatsapp_group_name=job["destination"] if job.get("destination_type") == "group" else "",
            talent_id=job.get("talent_id") or job.get("recipient_id") or "",
        )
        for job in merged.values()
    ]
    warning = f"Couldn't find project: {', '.join(not_found_projects)}" if not_found_projects else None
    return _RecipientTarget(
        ok=True, source_type="MANUAL", source_params=SourceParams(contacts=contacts),
        display_label=f"{project_labels} — {stage_label_str}",
        project_label=project_labels, pipeline_stage_label=stage_label_str,
        warning=warning,
    )


async def _resolve_talents_narrowed_by_projects(recipient_query: str, project_query: str) -> _RecipientTarget:
    """"Send the Follow Up template for Project A, B and C to Talent A,
    Talent B and Talent C" — resolves the named talent(s) via the SAME
    shared talent resolver every other recipient clause here uses, and the
    named project(s) via the SAME project matcher every stage/project
    reference here already uses, then narrows the send to whichever of
    those talents actually belong to one of the named projects' pipelines
    (SourceParams.talent_ids — an existing, purpose-built narrowing field,
    added for this exact "specific talents within a specific project"
    shape, not new infrastructure). A named talent who isn't part of ANY
    of the named projects is reported as a warning, never silently
    dropped without explanation."""
    talents = await _resolve_talent_names(
        recipient_query,
        single_ambiguous_field_key="recipient_query",
        multi_pick_field_key=_PENDING_MULTI_RECIPIENT_PICK_KEY,
        multi_fragments_key=_PENDING_MULTI_RECIPIENT_FRAGMENTS_KEY,
        multi_index_key=_PENDING_MULTI_RECIPIENT_INDEX_KEY,
    )
    if talents.ambiguous:
        return _RecipientTarget(ok=False, ambiguous=talents.ambiguous)
    if not talents.ok:
        return _RecipientTarget(
            ok=False, error=talents.error or f'I couldn\'t figure out who "{recipient_query}" refers to.',
        )

    projects = await _fetch_ongoing_projects()
    fragments = nlu.split_multi_names(project_query)
    multi = await _resolve_multi_project_names(fragments, projects)
    if multi.ambiguous:
        frag, amb_candidates, idx = multi.ambiguous
        return _RecipientTarget(
            ok=False,
            ambiguous=AmbiguousEntity(
                entity_type="project", field_key=_PENDING_MULTI_PROJECT_PICK_KEY,
                candidates=[disambiguation.Candidate(id=c["id"], label=c["label"]) for c in amb_candidates],
                extra_collected={
                    _PENDING_MULTI_PROJECT_FRAGMENTS_KEY: json.dumps(fragments),
                    _PENDING_MULTI_PROJECT_INDEX_KEY: str(idx),
                },
            ),
        )
    if not multi.resolved:
        return _RecipientTarget(ok=False, error=f'I couldn\'t find a project matching "{project_query}".')

    admin = await _service_admin()
    probe_template = await _probe_template()
    if not probe_template:
        return _RecipientTarget(
            ok=False, error="The built-in custom-message template is missing — please contact an admin.",
        )

    # Production fix (2026-08-19) — resolved PER PROJECT, never merged into
    # one deduplicated set. The earlier version deduped by recipient_id
    # across projects (so a talent named for 2+ projects only ever got ONE
    # message) AND always finished by collapsing into a single MANUAL-
    # source create_batch call, which never renders {{project_name}}/
    # {{shoot_dates}}/{{budget}}/{{submission_link}} at all (those are only
    # populated for source_type="PROJECT" — see routers/whatsapp.py's
    # create_batch). Every talent named for project P must get a message
    # rendered with P's OWN values — never another named project's, and
    # never unrendered. Keeping one (project_id, project_label, talent_ids)
    # entry per project with at least one match, and letting
    # _create_batch_for_target issue one real source_type="PROJECT"
    # create_batch call per entry, reuses that exact same, unmodified
    # rendering path — once per project — instead of inventing a second one.
    talent_id_list = list(dict.fromkeys(talents.talent_ids))
    per_project: List[Tuple[str, str, List[str]]] = []
    all_found_ids: set = set()
    for pid, plabel in multi.resolved:
        try:
            preview = await create_batch(
                BatchIn(
                    source_type="PROJECT",
                    source_params=SourceParams(
                        project_id=pid, pipeline_stages=list(PIPELINE_STAGE_ORDER),
                        talent_ids=talent_id_list,
                    ),
                    template_id=probe_template["id"], is_dry_run=True,
                ),
                admin=admin,
            )
        except HTTPException:
            continue
        found_ids = [j["recipient_id"] for j in preview["jobs"]]
        if found_ids:
            per_project.append((pid, plabel, found_ids))
            all_found_ids.update(found_ids)

    project_labels = ", ".join(plabel for _pid, plabel in multi.resolved)
    missing = [label for tid, label in zip(talents.talent_ids, talents.talent_labels) if tid not in all_found_ids]
    warning_parts = []
    if talents.warning:
        warning_parts.append(talents.warning)
    if multi.not_found:
        warning_parts.append(f"Couldn't find project: {', '.join(multi.not_found)}")
    if missing:
        warning_parts.append(f"Not part of {project_labels}: {', '.join(missing)}")
    warning = " | ".join(warning_parts) if warning_parts else None

    if not per_project:
        return _RecipientTarget(
            ok=False, error=f"None of the named talent(s) are part of {project_labels}'s pipeline.",
        )

    display_label = ", ".join(dict.fromkeys(talents.talent_labels))

    if len(per_project) == 1:
        # The common case — every matched talent belongs to (at most) one
        # of the named projects — stays on a single ordinary
        # source_type="PROJECT" target, same as _resolve_project_stage_
        # union's own single-project shortcut: no multi-batch fan-out
        # needed, one create_batch call, unchanged shape.
        pid, plabel, tids = per_project[0]
        return _RecipientTarget(
            ok=True, source_type="PROJECT",
            source_params=SourceParams(project_id=pid, pipeline_stages=list(PIPELINE_STAGE_ORDER), talent_ids=tids),
            display_label=display_label, project_label=plabel, warning=warning,
        )

    return _RecipientTarget(
        ok=True, display_label=display_label, project_label=project_labels, warning=warning,
        multi_project_targets=per_project,
    )


async def _resolve_recipient_multi_aware(
    recipient_query: str, stage_query: str, project_query: str,
) -> _RecipientTarget:
    """Entry point every send-target resolver now calls instead of
    _resolve_recipient directly — routes to the multi-project/multi-stage
    union resolver or the talents-narrowed-by-project resolver ONLY when
    this command's shape actually calls for one of them; every other
    shape (single project, named talent(s) with no project, phone number,
    CRM category, saved list) delegates straight through to the existing,
    completely unmodified _resolve_recipient — zero risk to any
    already-working, already-tested path."""
    if stage_query:
        return await _resolve_project_stage_union(project_query or recipient_query, stage_query)

    if project_query and recipient_query and recipient_query != _ALL_PROJECTS_SENTINEL:
        # A project reference is already known (e.g. a source-side "for
        # Project(s)" clause — item 2/3's shape), but the recipient clause
        # itself carries no "list"/"pipeline" connector word for
        # _split_stage_and_project to have recognized ("...to Follow Up
        # and Approved", with the project clause elsewhere in the
        # sentence). Still worth checking whether the recipient text IS
        # itself a stage (or stage list) before assuming it names
        # talent(s) — but ONLY commits when every fragment confidently
        # resolves as a real stage, so an ordinary talent list ("to Ahana
        # and Priya") keeps resolving as talents, unaffected.
        probe_keys, probe_err = await _resolve_pipeline_stages(recipient_query)
        if probe_keys and not probe_err:
            return await _resolve_project_stage_union(project_query, recipient_query)
        return await _resolve_talents_narrowed_by_projects(recipient_query, project_query)

    if recipient_query and recipient_query != _ALL_PROJECTS_SENTINEL:
        # Bare multi-project reference with no stage at all ("to Project A
        # and Project B") -> whole-pipeline union across all named
        # projects, but ONLY when every fragment we can classify is
        # confidently project-shaped — never guessed over a talent list
        # that happens to share the same "X and Y" text shape ("to Ahana
        # and Priya" must keep resolving as talents, exactly as before).
        fragments = nlu.split_multi_names(recipient_query)
        if len(fragments) >= 2:
            projects = await _fetch_ongoing_projects()
            probe = await _resolve_multi_project_names(fragments, projects)
            fully_project_shaped = (
                bool(probe.resolved) and not probe.not_found
                and (len(probe.resolved) + (1 if probe.ambiguous else 0)) == len(fragments)
            )
            if fully_project_shaped:
                return await _resolve_project_stage_union(recipient_query, "")

    return await _resolve_recipient(recipient_query, stage_query)


# ---------------------------------------------------------------------------
# Multi Manual Recipients (2026-08-09) — "Send Reminder to Ahana, Kripa,
# Raj", newline-separated lists, "and", "&". Every name is still resolved
# through nlu.resolve_against_candidates (the SAME single-name tier
# casting-agent's Move/Add already use) — this section only decides WHICH
# TEXT SPAN is one recipient's name and loops over them independently, so
# per-name ambiguity is never lost (see the module-level gap this closes:
# resolve_against_candidates's OWN multi-name branch flattens ambiguity
# into an unusable combined error string — deliberately NOT used here for
# that reason; nlu.parse_talent_selector/resolve_against_candidates
# themselves are untouched, still exactly what casting-agent depends on).
# ---------------------------------------------------------------------------
_RECIPIENT_LIST_SPLIT_RE = re.compile(r",|\n|\band\b", re.IGNORECASE)


def _split_recipient_names(text: str) -> List[str]:
    """Pure text splitter — no matching/fuzzy logic of any kind. "&" is
    never a plausible fragment of a real person's name in this system
    (same assumption already made for "and"/"talent" as noise words in
    casting_pipeline_nlu.py), so it's normalized to a comma first, then
    comma/newline/"and" are one splitting grammar. Empty fragments
    (blank lines, trailing separators) are dropped."""
    normalized = (text or "").replace("&", ",")
    parts = _RECIPIENT_LIST_SPLIT_RE.split(normalized)
    return [p.strip(" .!?") for p in parts if p.strip(" .!?")]


@dataclass
class _MultiRecipientResolution:
    resolved: List[Tuple[str, str]] = None  # (talent_id, talent_label), deduped, fragment order
    not_found: List[str] = None             # raw fragments that matched no talent at all
    unsafe: List[Tuple[str, str]] = None    # (fragment, would-be label) that failed the safety gate
    # (fragment, candidates, index-into-the-original-fragment-list) — set
    # only when exactly the FIRST ambiguous fragment is found; the caller
    # pauses there (one disambiguation at a time, same as every other
    # ambiguity on this platform) rather than trying to resolve several
    # at once.
    ambiguous: Optional[Tuple[str, List["nlu.Candidate"], int]] = None

    def __post_init__(self):
        if self.resolved is None:
            self.resolved = []
        if self.not_found is None:
            self.not_found = []
        if self.unsafe is None:
            self.unsafe = []


async def _resolve_multi_recipient_names(fragments: List[str]) -> _MultiRecipientResolution:
    """Resolves each fragment INDEPENDENTLY (sprint requirement) against
    the same global talent pool ADD_INTENT/the single-name recipient tier
    already search. Scans the WHOLE list before returning — so a not-found
    name elsewhere in the list is never masked by pausing on an earlier
    ambiguity, matching "never silently skip" for the harder-to-recover
    case (a genuinely unresolvable name) over the softer, resumable case
    (a name with a real answer waiting to be picked)."""
    candidates = await _fetch_all_talent_candidates()
    out = _MultiRecipientResolution()
    seen_ids: set = set()
    for idx, frag in enumerate(fragments):
        one = nlu.resolve_against_candidates(nlu.SelectorResult(ok=True, name_query=frag), candidates)
        if one.ambiguous_candidates:
            if out.ambiguous is None:
                out.ambiguous = (frag, one.ambiguous_candidates, idx)
            continue
        if not one.ok or not one.talent_ids:
            out.not_found.append(frag)
            continue
        label = one.talent_labels[0]
        if not _fuzzy_match_is_safe(frag, label):
            out.unsafe.append((frag, label))
            continue
        tid = one.talent_ids[0]
        if tid not in seen_ids:
            seen_ids.add(tid)
            out.resolved.append((tid, label))
    return out


async def _build_manual_contacts(resolved: List[Tuple[str, str]]) -> "tuple[List[ManualContact], List[str]]":
    """Talent id/label pairs -> ManualContact list, same phone/group
    extraction the existing single-name Tier 3 already does (not
    duplicated logic — this is plain data shaping, no matching). Returns
    (contacts, labels_with_no_phone_or_group)."""
    ids = [tid for tid, _label in resolved]
    if not ids:
        return [], []
    docs = await db.talents.find(
        {"id": {"$in": ids}}, {"_id": 0, "id": 1, "name": 1, "phone": 1, "whatsapp_group_name": 1},
    ).to_list(len(ids))
    tmap = {d["id"]: d for d in docs}
    contacts: List[ManualContact] = []
    no_contact_info: List[str] = []
    for tid, label in resolved:
        d = tmap.get(tid)
        if not d:
            continue
        group_name = (d.get("whatsapp_group_name") or "").strip()
        phone = (d.get("phone") or "").strip()
        if not group_name and not phone:
            no_contact_info.append(label)
            continue
        contacts.append(ManualContact(
            name=d.get("name") or "", phone=phone, whatsapp_group_name=group_name, talent_id=tid,
        ))
    return contacts, no_contact_info


# Private, never-user-facing collected keys carrying multi-recipient
# disambiguation state across a round trip through the shared engine —
# same pattern as _pending_exclude_name/_pending_include_name (E below),
# just list-shaped instead of a single string.
_PENDING_MULTI_RECIPIENT_PICK_KEY = "_pending_multi_recipient_pick"
_PENDING_MULTI_RECIPIENT_FRAGMENTS_KEY = "_pending_multi_recipient_fragments"
_PENDING_MULTI_RECIPIENT_INDEX_KEY = "_pending_multi_recipient_index"


async def _resume_pending_multi_recipient(collected: dict, ctx: ExecContext) -> dict:
    """Mirror of _resume_pending_recipient_edit, for a multi-recipient send
    where one name was ambiguous. The shared disambiguation engine has
    already written the picked candidate's exact label into
    _pending_multi_recipient_pick (dispatcher.py's _advance_disambiguation
    — see agents/disambiguation.py). Substitutes that label back into the
    ORIGINAL fragment list at the ambiguous position and rewrites
    `recipient_query` to the reconstructed, now-fully-resolvable text —
    letting the normal _resolve_recipient path re-derive everything fresh,
    exactly like every other turn (no separate resolution logic needed
    here). A no-op when no multi-recipient disambiguation is pending."""
    picked_label = collected.get(_PENDING_MULTI_RECIPIENT_PICK_KEY)
    fragments_json = collected.get(_PENDING_MULTI_RECIPIENT_FRAGMENTS_KEY)
    index_raw = collected.get(_PENDING_MULTI_RECIPIENT_INDEX_KEY)
    if not picked_label or fragments_json is None or index_raw is None:
        return collected
    try:
        fragments = json.loads(fragments_json)
        index = int(index_raw)
    except (TypeError, ValueError):
        fragments = None
        index = -1
    new_collected = dict(collected)
    for key in (
        _PENDING_MULTI_RECIPIENT_PICK_KEY,
        _PENDING_MULTI_RECIPIENT_FRAGMENTS_KEY,
        _PENDING_MULTI_RECIPIENT_INDEX_KEY,
    ):
        new_collected.pop(key, None)
    if isinstance(fragments, list) and 0 <= index < len(fragments):
        fragments[index] = picked_label
        new_collected["recipient_query"] = ", ".join(fragments)
    await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, collected=new_collected)
    return new_collected


# Instagram Profile Send's subject-name mirror of the three keys above.
# _resume_pending_multi_recipient always rewrites `recipient_query` — reusing
# it as-is for an ambiguous SUBJECT name (whose Instagram, not who receives
# it) would corrupt the recipient field, so this is a small, near-identical
# duplication targeting `source_query` instead — same shared disambiguation
# engine, same nlu.resolve_against_candidates/_fuzzy_match_is_safe, only the
# destination key differs.
_PENDING_MULTI_SUBJECT_PICK_KEY = "_pending_multi_subject_pick"
_PENDING_MULTI_SUBJECT_FRAGMENTS_KEY = "_pending_multi_subject_fragments"
_PENDING_MULTI_SUBJECT_INDEX_KEY = "_pending_multi_subject_index"


async def _resume_pending_multi_subject(collected: dict, ctx: ExecContext) -> dict:
    """Mirror of _resume_pending_multi_recipient, for Instagram Profile
    Send's "whose profile" list — writes the resolved fragment list back
    into source_query instead of recipient_query. A no-op when no
    multi-subject disambiguation is pending."""
    picked_label = collected.get(_PENDING_MULTI_SUBJECT_PICK_KEY)
    fragments_json = collected.get(_PENDING_MULTI_SUBJECT_FRAGMENTS_KEY)
    index_raw = collected.get(_PENDING_MULTI_SUBJECT_INDEX_KEY)
    if not picked_label or fragments_json is None or index_raw is None:
        return collected
    try:
        fragments = json.loads(fragments_json)
        index = int(index_raw)
    except (TypeError, ValueError):
        fragments = None
        index = -1
    new_collected = dict(collected)
    for key in (
        _PENDING_MULTI_SUBJECT_PICK_KEY,
        _PENDING_MULTI_SUBJECT_FRAGMENTS_KEY,
        _PENDING_MULTI_SUBJECT_INDEX_KEY,
    ):
        new_collected.pop(key, None)
    if isinstance(fragments, list) and 0 <= index < len(fragments):
        fragments[index] = picked_label
        new_collected["source_query"] = ", ".join(fragments)
    await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, collected=new_collected)
    return new_collected


# ---------------------------------------------------------------------------
# Shared Talent Resolver (2026-08-12) — the ONE talent-name lookup path on
# this intent. Used by BOTH _resolve_recipient's Tier 3 ("who receives
# this") and _resolve_instagram_target ("whose Instagram to send") — a
# name resolves identically in either context: same tiered exact/
# normalized/token/fuzzy matching (nlu.parse_talent_selector +
# nlu.resolve_against_candidates), same typo/partial-name tolerance, same
# post-match safety gate (_fuzzy_match_is_safe), same multi-name splitting
# and per-fragment disambiguation (_resolve_multi_recipient_names), same
# shared disambiguation engine for "which one did you mean?". Instagram
# Profile Send does NOT implement its own lookup anywhere — this function
# is that reuse, not a parallel approximation of it.
# ---------------------------------------------------------------------------
@dataclass
class _TalentNamesResolution:
    ok: bool
    talent_ids: List[str] = field(default_factory=list)
    talent_labels: List[str] = field(default_factory=list)
    error: Optional[str] = None
    ambiguous: Optional[AmbiguousEntity] = None
    # True only for "genuinely no match at all — not even a weak fuzzy
    # suggestion" on a SINGLE name. The one outcome _resolve_recipient
    # treats as non-terminal (falls through to try the text as a CRM
    # category or saved list instead — a lone word/phrase could
    # legitimately be either). Every other failure is terminal for every
    # caller, including a 2+-name list (never a project/CRM/saved-list
    # name) and a safety-gate rejection (a real match too risky to trust).
    not_found: bool = False
    # Partial-Failure-Tolerant Multi-Recipient Resolution (2026-08-17) — set
    # alongside ok=True when a 2+-name list had SOME confidently-resolved
    # names AND some genuinely not-found/unsafe ones. The confidently-
    # resolved names are never discarded just because one other name in
    # the same list couldn't be found — see the multi-fragment branch
    # below. None (every single-name resolution, and every multi-name
    # resolution where every fragment resolved) means nothing to warn
    # about, unaffected.
    warning: Optional[str] = None


async def _resolve_talent_names(
    q: str, *,
    single_ambiguous_field_key: str,
    multi_pick_field_key: str,
    multi_fragments_key: str,
    multi_index_key: str,
    strict_ambiguous_safety_gate: bool = False,
) -> _TalentNamesResolution:
    """Resolves `q` — one name, or 2+ comma/newline/"and"/"&"-separated
    names — against the SAME global talent pool ADD_INTENT/every other
    talent lookup on this platform searches. `single_ambiguous_field_key`
    is which `collected` key the shared disambiguation engine overwrites
    once a single ambiguous name is picked (callers name their own field
    here — "recipient_query" for _resolve_recipient, "source_query" for
    Instagram's subject); the three `multi_*` keys are the private
    pending-state markers a caller's OWN resume function uses for the
    2+-name ambiguity round trip (see _resume_pending_multi_recipient /
    _resume_pending_multi_subject — mirrors of each other, one per caller,
    since the shared engine writes into whichever field_key it's given).

    `strict_ambiguous_safety_gate` (2026-08-20 production fix) — off by
    default, preserving every existing caller's behavior byte-for-byte
    (this function's field is genuinely a talent name for those callers,
    where a tie between two typo/first-name-tolerant fuzzy matches is a
    real, wanted disambiguation — see
    test_disambiguation_talent_ambiguity_resolved_via_exact_name). Passed
    True ONLY by _resolve_recipient_only's talent-fallback tier, where `q`
    is a free-text RECIPIENT identifier tried against talents merely as a
    last resort after CRM/saved-list/saved-group all miss — there, a tie
    on pure single-token character overlap with no real relationship to
    either candidate (e.g. "Mixxi App x Talentgram Agency" tying a
    "...Dubai Talent"-suffixed name against a "Nancy..." one purely via
    "talentgram"~"talent"/"agency"~"nancy") is noise, not a genuine
    choice, and must not be surfaced as one."""
    selector = nlu.parse_talent_selector(q)
    if not (selector.ok and not selector.everyone and not selector.ordinals):
        return _TalentNamesResolution(ok=False, not_found=True)

    # Multi Manual Recipients (2026-08-09) — 2+ separated names is
    # unambiguously a LIST (never a single project/CRM/saved-list name,
    # which never legitimately contains a separator like this). A single
    # name — the overwhelmingly common case — falls through below.
    fragments = _split_recipient_names(q)
    if len(fragments) >= 2:
        multi = await _resolve_multi_recipient_names(fragments)
        if multi.not_found or multi.unsafe:
            problems = list(multi.not_found) + [frag for frag, _label in multi.unsafe]
            if multi.resolved:
                # Partial-Failure-Tolerant Multi-Recipient Resolution
                # (2026-08-17) — confidently-resolved names are preserved
                # and the send proceeds with them; the unresolved ones are
                # surfaced as a warning rather than blocking everyone.
                # Matches the ambiguous-fragment case just below, which
                # already resumes with the rest of the list intact — this
                # extends the same "don't lose what's already confidently
                # known" principle to the not-found/unsafe case too.
                return _TalentNamesResolution(
                    ok=True,
                    talent_ids=[tid for tid, _label in multi.resolved],
                    talent_labels=[label for _tid, label in multi.resolved],
                    warning=f"Couldn't find: {', '.join(problems)}",
                )
            return _TalentNamesResolution(ok=False, error=f"Couldn't find:\n\n{chr(10).join(problems)}")
        if multi.ambiguous:
            frag, amb_candidates, idx = multi.ambiguous
            return _TalentNamesResolution(
                ok=False,
                ambiguous=AmbiguousEntity(
                    entity_type="talent", field_key=multi_pick_field_key,
                    candidates=[disambiguation.Candidate(id=c.id, label=c.label) for c in amb_candidates],
                    extra_collected={
                        multi_fragments_key: json.dumps(fragments),
                        multi_index_key: str(idx),
                    },
                ),
            )
        if not multi.resolved:
            # Unreachable in practice — _resolve_multi_recipient_names
            # places every fragment into resolved/not_found/unsafe/
            # ambiguous, and fragments is non-empty here, so at least one
            # of the checks above already returned. Kept as a defensive
            # terminal error, never a silent pass-through.
            return _TalentNamesResolution(ok=False, error=f'I couldn\'t figure out who "{q}" refers to.')
        return _TalentNamesResolution(
            ok=True,
            talent_ids=[tid for tid, _label in multi.resolved],
            talent_labels=[label for _tid, label in multi.resolved],
        )

    candidates = await _fetch_all_talent_candidates()
    resolved = nlu.resolve_against_candidates(selector, candidates)
    if resolved.ok and resolved.talent_ids:
        # Safety gate (see _fuzzy_match_is_safe docstring) — every
        # resolved label must genuinely contain what the user typed, not
        # just share one word (e.g. a surname) with it.
        safety_fragments = nlu.split_multi_names(q)
        unsafe = [
            label for label in resolved.talent_labels
            if not any(_fuzzy_match_is_safe(frag, label) for frag in safety_fragments)
        ]
        if unsafe:
            return _TalentNamesResolution(
                ok=False,
                error=(
                    f'"{q}" matched "{", ".join(unsafe)}" but that name doesn\'t look like '
                    f"a real match — please use the exact full name to avoid sending to the "
                    f"wrong person."
                ),
            )
        return _TalentNamesResolution(ok=True, talent_ids=resolved.talent_ids, talent_labels=resolved.talent_labels)
    if resolved.ambiguous_candidates:
        if strict_ambiguous_safety_gate:
            # See this function's docstring — scoped to the recipient-
            # fallback caller only, never the callers for whom `q` is
            # genuinely a talent-name field.
            safety_fragments = nlu.split_multi_names(q)
            safe_candidates = [
                c for c in resolved.ambiguous_candidates
                if any(_fuzzy_match_is_safe(frag, c.label) for frag in safety_fragments)
            ]
            if not safe_candidates:
                return _TalentNamesResolution(ok=False, not_found=True)
            if len(safe_candidates) == 1:
                c = safe_candidates[0]
                return _TalentNamesResolution(ok=True, talent_ids=[c.id], talent_labels=[c.label])
            resolved_ambiguous_candidates = safe_candidates
        else:
            resolved_ambiguous_candidates = resolved.ambiguous_candidates
        return _TalentNamesResolution(
            ok=False,
            ambiguous=AmbiguousEntity(
                entity_type="talent", field_key=single_ambiguous_field_key,
                candidates=[
                    disambiguation.Candidate(id=c.id, label=c.label)
                    for c in resolved_ambiguous_candidates
                ],
            ),
        )
    return _TalentNamesResolution(ok=False, not_found=True)


# ---------------------------------------------------------------------------
# Dedicated Recipient Resolver (Production fix, 2026-08-19) — a namespace
# separate from the talent/project resolvers used throughout this file, for
# a field that names WHO receives a message but is never the campaign's
# talent AUDIENCE itself — today, only Instagram Profile Send's "share with
# X" clause ("send instagram - Angela - Akash Castingtree": Angela is the
# SUBJECT, resolved via _resolve_talent_names exactly as before; "Akash
# Castingtree" is the RECIPIENT).
#
# Root cause this fixes: _resolve_instagram_target used to call the
# general _resolve_recipient below for its recipient clause too — but that
# function's tier order tries TALENTS (Tier 3) before it ever tries an
# individual CRM contact by name (which it never actually did at all —
# its own Tier 4 only matches a CRM contact-TYPE CATEGORY like "Brand
# Managers", never a specific person's name). A real CRM contact named
# "Akash Castingtree" could therefore get silently matched against a
# similarly-named TALENT instead of ever being recognized as who she
# actually is.
#
# Order here: phone number -> CRM contact (by name) -> saved contact list ->
# saved group list -> known talent (fallback) -> not found, clearly. CRM/
# saved-list/phone are tried WITH PRIORITY, ahead of talent, so a genuine
# CRM contact is never shadowed by a similarly-named talent — but talent
# stays a valid LAST-RESORT match (spec: "Do not require every recipient
# to exist in the CRM"), preserving every existing "share with a fellow
# talent" workflow. A live WhatsApp Web contact/group search (querying the
# connected WhatsApp session directly, mid-conversation) is NOT wired up in
# this pass — there is no existing backend<->worker channel for a
# synchronous search request today (the worker only ever searches WhatsApp
# Web's own sidebar at actual SEND time, as part of delivering an already-
# queued job — see whatsapp-worker/sender.py's group-search step). Flagged
# as a known limitation in the not-found error rather than silently
# pretending to support it.
# ---------------------------------------------------------------------------
async def _fetch_crm_clients_for_matching() -> List[Dict[str, str]]:
    """Every non-archived, non-deleted CRM contact, name+phone+id only —
    the candidate pool an individual recipient name is matched against,
    via the SAME tiered matcher (agents/name_match.py) every other name
    lookup in this file already uses. Fetched in full and matched in
    Python, exactly like _fetch_all_talent_candidates already does for
    talents — CRM contact counts on this platform are in the hundreds,
    not thousands."""
    docs = await db.clients.find(
        {"archived": {"$ne": True}, "deleted": {"$ne": True}},
        {"name": 1, "phone_number": 1},
    ).to_list(5000)
    return [
        {"id": str(d["_id"]), "name": d.get("name") or "", "phone": d.get("phone_number") or ""}
        for d in docs if (d.get("name") or "").strip()
    ]


async def _resolve_recipient_only(query: str) -> _RecipientTarget:
    """See module section comment above — the dedicated RECIPIENT
    resolver. Never calls any project matcher; talent matching is tried
    only as a last-resort fallback, well after CRM/saved-list/phone."""
    q = (query or "").strip()
    if not q:
        return _RecipientTarget(ok=False, error="Who should this go to?")

    if _PHONE_RE.match(q):
        phone = _normalize_phone(q)
        if phone:
            return _RecipientTarget(
                ok=True, source_type="MANUAL",
                source_params=SourceParams(contacts=[ManualContact(name="", phone=phone)]),
                display_label=phone,
            )
        return _RecipientTarget(ok=False, error=f'"{q}" doesn\'t look like a valid phone number.')

    clients = await _fetch_crm_clients_for_matching()
    if clients:
        c_match = name_match.tiered_name_match(
            q, clients, lambda c: c["name"], id_fn=lambda c: c["id"], what="CRM contact",
        )
        if c_match.item:
            phone = _normalize_phone(c_match.item.get("phone") or "") or ""
            return _RecipientTarget(
                ok=True, source_type="MANUAL",
                source_params=SourceParams(contacts=[ManualContact(name=c_match.item["name"], phone=phone)]),
                display_label=c_match.item["name"],
            )
        if c_match.ambiguous:
            return _RecipientTarget(
                ok=False,
                ambiguous=AmbiguousEntity(
                    entity_type="crm_contact", field_key="recipient_query",
                    candidates=[disambiguation.Candidate(id=c["id"], label=c["label"]) for c in c_match.ambiguous],
                ),
            )

    lists = await _fetch_contact_lists()
    cl_match = name_match.tiered_name_match(
        q, lists, lambda it: it.get("name") or "", id_fn=lambda it: it["id"], what="saved list",
    )
    if cl_match.item:
        return _RecipientTarget(
            ok=True, source_type="SAVED_LISTS",
            source_params=SourceParams(contact_list_ids=[cl_match.item["id"]]),
            display_label=cl_match.item["name"],
        )
    if cl_match.ambiguous:
        return _RecipientTarget(
            ok=False,
            ambiguous=AmbiguousEntity(
                entity_type="saved_list", field_key="recipient_query",
                candidates=[disambiguation.Candidate(id=c["id"], label=c["label"]) for c in cl_match.ambiguous],
            ),
        )

    glists = await _fetch_group_lists()
    gl_match = name_match.tiered_name_match(
        q, glists, lambda it: it.get("name") or "", id_fn=lambda it: it["id"], what="saved group",
    )
    if gl_match.item:
        return _RecipientTarget(
            ok=True, source_type="SAVED_LISTS",
            source_params=SourceParams(group_list_ids=[gl_match.item["id"]]),
            display_label=gl_match.item["name"],
        )
    if gl_match.ambiguous:
        return _RecipientTarget(
            ok=False,
            ambiguous=AmbiguousEntity(
                entity_type="saved_list", field_key="recipient_query",
                candidates=[disambiguation.Candidate(id=c["id"], label=c["label"]) for c in gl_match.ambiguous],
            ),
        )

    # Fallback tier — a known TALENT (has a phone/WhatsApp group on file in
    # this system already, same as any other stored WhatsApp contact).
    # Deliberately LAST, not first: CRM/saved-list/phone are tried with
    # priority above so a genuine CRM contact is never shadowed by a
    # similarly-named talent (the exact bug this resolver fixes) — but a
    # recipient still doesn't have to exist in the CRM at all (spec: "Do
    # not require every recipient to exist in the CRM"), and several
    # existing workflows already share an Instagram link with a fellow
    # talent by name, which this preserves. Reuses the SAME shared talent
    # resolver every other talent-field lookup in this file uses — no
    # separate matching logic.
    talents = await _resolve_talent_names(
        q,
        single_ambiguous_field_key="recipient_query",
        multi_pick_field_key=_PENDING_MULTI_RECIPIENT_PICK_KEY,
        multi_fragments_key=_PENDING_MULTI_RECIPIENT_FRAGMENTS_KEY,
        multi_index_key=_PENDING_MULTI_RECIPIENT_INDEX_KEY,
        strict_ambiguous_safety_gate=True,
    )
    if talents.ambiguous:
        return _RecipientTarget(ok=False, ambiguous=talents.ambiguous)
    if talents.ok:
        contacts, no_contact_info = await _build_manual_contacts(
            list(zip(talents.talent_ids, talents.talent_labels))
        )
        if contacts:
            return _RecipientTarget(
                ok=True, source_type="MANUAL",
                source_params=SourceParams(contacts=contacts),
                display_label=", ".join(talents.talent_labels),
                warning=talents.warning,
            )
        return _RecipientTarget(
            ok=False,
            error=f'{" and ".join(no_contact_info)} — no phone number or WhatsApp group on file.',
        )
    if talents.error:
        return _RecipientTarget(ok=False, error=talents.error)

    return _RecipientTarget(
        ok=False,
        error=(
            f'I couldn\'t find "{q}" as a CRM contact, saved list, talent, or phone number. '
            "If they're a WhatsApp contact not yet saved anywhere, add them as a CRM "
            "contact or a saved recipient list first, then resend this command."
        ),
    )


async def _resolve_recipient(recipient_query: str, stage_query: str) -> _RecipientTarget:
    normalized_stage, stage_error = await _resolve_pipeline_stage(stage_query)
    if stage_error:
        return stage_error
    stage_label_str = nlu.stage_label(normalized_stage) if normalized_stage else "All stages"

    q = (recipient_query or "").strip()

    if q == _ALL_PROJECTS_SENTINEL:
        # A stage was named but no project — ask which project, across
        # every ongoing one (auto-resolving when there's only one),
        # exactly like the worked example in the sprint spec.
        projects = await _fetch_ongoing_projects()
        if not projects:
            return _RecipientTarget(ok=False, error="There are no ongoing projects to send to.")
        if len(projects) == 1:
            p = projects[0]
            stage_list = [normalized_stage] if normalized_stage else list(PIPELINE_STAGE_ORDER)
            return _RecipientTarget(
                ok=True, source_type="PROJECT",
                source_params=SourceParams(project_id=p["id"], pipeline_stages=stage_list),
                display_label=f'{p["label"]} — {stage_label_str}',
                project_label=p["label"], pipeline_stage_label=stage_label_str if normalized_stage else None,
            )
        return _RecipientTarget(
            ok=False,
            ambiguous=AmbiguousEntity(
                entity_type="project", field_key="recipient_query",
                candidates=[disambiguation.Candidate(id=p["id"], label=p["label"]) for p in projects],
            ),
        )

    if not q:
        return _RecipientTarget(ok=False, error="Who should this go to?")

    # Tier 1: phone number.
    if _PHONE_RE.match(q):
        phone = _normalize_phone(q)
        if phone:
            return _RecipientTarget(
                ok=True, source_type="MANUAL",
                source_params=SourceParams(contacts=[ManualContact(name="", phone=phone)]),
                display_label=phone,
            )
        return _RecipientTarget(ok=False, error=f'"{q}" doesn\'t look like a valid phone number.')

    # Tier 2: an entire project (send to its pipeline) — tried before named
    # talents so the original campaign grammar ("... to Toyota Glanza")
    # keeps resolving to the whole project, unchanged.
    projects = await _fetch_ongoing_projects()
    proj_match = nlu.resolve_project_by_name(q, projects)
    # Safety gate (see _fuzzy_match_is_safe docstring — this is the exact
    # "ZList... Alpha" false-matched "QATEST Project Alpha" bug caught in
    # testing) — an unsafe single-token-coincidence match is NOT hard-
    # rejected here like the talent tier does; it just falls through to
    # try the remaining tiers, since a project match this weak is just as
    # likely to actually be a talent/CRM/saved-list name instead.
    if proj_match.project and _fuzzy_match_is_safe(
        q, proj_match.project["label"], ignore_words=nlu._PROJECT_FILLER_WORDS,
    ):
        stage_list = [normalized_stage] if normalized_stage else list(PIPELINE_STAGE_ORDER)
        return _RecipientTarget(
            ok=True, source_type="PROJECT",
            source_params=SourceParams(project_id=proj_match.project["id"], pipeline_stages=stage_list),
            display_label=f'{proj_match.project["label"]} — {stage_label_str}',
            project_label=proj_match.project["label"],
            pipeline_stage_label=stage_label_str if normalized_stage else None,
        )
    if proj_match.ambiguous:
        # A genuine tie (multiple REAL matches) is disambiguation-worthy.
        # `.suggestions` (a weak fuzzy "did you mean" below the confident
        # bar) is deliberately NOT gated here — same as before this sprint,
        # it falls through to the talent tier next, since a suggestion
        # this weak is just as likely to actually be a talent name — UNLESS
        # a stage was explicitly named, in which case "talent" makes no
        # sense as an interpretation (see the stage-set short-circuit
        # below `.suggestions` also respects this).
        return _RecipientTarget(
            ok=False,
            ambiguous=AmbiguousEntity(
                entity_type="project", field_key="recipient_query",
                candidates=[disambiguation.Candidate(id=c["id"], label=c["label"]) for c in proj_match.ambiguous],
            ),
        )
    if normalized_stage:
        # A stage was explicitly named ("Follow Up pipeline of X") but X
        # didn't resolve to a real project — falling through to try X as a
        # talent/CRM/saved-list name next doesn't make sense (a stage only
        # ever applies to a project), and would just produce a confusing
        # "I couldn't figure out who X refers to" at the end of the chain.
        return _RecipientTarget(ok=False, error=f'I couldn\'t find a project matching "{q}".')

    # Tier 3: one or more named talents — global pool (same candidate set
    # ADD_INTENT already searches; a talent being messaged directly needn't
    # be in any particular project's pipeline). Delegates entirely to the
    # ONE shared talent-name resolver (_resolve_talent_names) — the exact
    # same fuzzy-matching/safety-gate/disambiguation behavior Instagram
    # Profile Send's subject resolution also goes through, so the same
    # name resolves identically in both places.
    talents = await _resolve_talent_names(
        q,
        single_ambiguous_field_key="recipient_query",
        multi_pick_field_key=_PENDING_MULTI_RECIPIENT_PICK_KEY,
        multi_fragments_key=_PENDING_MULTI_RECIPIENT_FRAGMENTS_KEY,
        multi_index_key=_PENDING_MULTI_RECIPIENT_INDEX_KEY,
    )
    if talents.ambiguous:
        return _RecipientTarget(ok=False, ambiguous=talents.ambiguous)
    if talents.ok:
        contacts, no_contact_info = await _build_manual_contacts(
            list(zip(talents.talent_ids, talents.talent_labels))
        )
        if contacts:
            return _RecipientTarget(
                ok=True, source_type="MANUAL",
                source_params=SourceParams(contacts=contacts),
                display_label=", ".join(talents.talent_labels),
                warning=talents.warning,
            )
        return _RecipientTarget(
            ok=False,
            error=f'{" and ".join(no_contact_info)} — no phone number or WhatsApp group on file.',
        )
    if talents.error:
        return _RecipientTarget(ok=False, error=talents.error)
    # talents.not_found (selector itself invalid, or a single name with
    # genuinely no match) — falls through to try q as a CRM category or
    # saved list next, unchanged fallthrough behavior.

    # Tier 4: a CRM contact-type category ("send ... to Brand Managers") —
    # bounded to REAL, known contact_type values (not a loose free-text CRM
    # search), same reasoning as the project/talent tiers: a deterministic
    # match against a real named category, not a fuzzy guess that could
    # silently resolve to the wrong (or an empty) audience.
    contact_types = await _fetch_crm_contact_types()
    if contact_types:
        ct_match = name_match.tiered_name_match(
            q, contact_types, lambda t: t, id_fn=lambda t: t, what="CRM contact type",
        )
        if ct_match.item:
            return _RecipientTarget(
                ok=True, source_type="CRM",
                source_params=SourceParams(contact_type=ct_match.item),
                display_label=f"CRM — {ct_match.item}",
            )
        if ct_match.ambiguous:
            # (2026-08-09) Previously fell through silently to the saved-
            # list tiers below, never surfacing the ambiguity at all.
            return _RecipientTarget(
                ok=False,
                ambiguous=AmbiguousEntity(
                    entity_type="crm_source", field_key="recipient_query",
                    candidates=[disambiguation.Candidate(id=c["id"], label=c["label"]) for c in ct_match.ambiguous],
                ),
            )

    # Tier 5: a saved contact list.
    lists = await _fetch_contact_lists()
    cl_match = name_match.tiered_name_match(
        q, lists, lambda it: it.get("name") or "", id_fn=lambda it: it["id"], what="saved list",
    )
    if cl_match.item:
        return _RecipientTarget(
            ok=True, source_type="SAVED_LISTS",
            source_params=SourceParams(contact_list_ids=[cl_match.item["id"]]),
            display_label=cl_match.item["name"],
        )
    if cl_match.ambiguous:
        return _RecipientTarget(
            ok=False,
            ambiguous=AmbiguousEntity(
                entity_type="saved_list", field_key="recipient_query",
                candidates=[disambiguation.Candidate(id=c["id"], label=c["label"]) for c in cl_match.ambiguous],
            ),
        )

    # Tier 6: a saved WhatsApp group list.
    glists = await _fetch_group_lists()
    gl_match = name_match.tiered_name_match(
        q, glists, lambda it: it.get("name") or "", id_fn=lambda it: it["id"], what="saved group",
    )
    if gl_match.item:
        return _RecipientTarget(
            ok=True, source_type="SAVED_LISTS",
            source_params=SourceParams(group_list_ids=[gl_match.item["id"]]),
            display_label=gl_match.item["name"],
        )
    if gl_match.ambiguous:
        return _RecipientTarget(
            ok=False,
            ambiguous=AmbiguousEntity(
                entity_type="saved_list", field_key="recipient_query",
                candidates=[disambiguation.Candidate(id=c["id"], label=c["label"]) for c in gl_match.ambiguous],
            ),
        )

    return _RecipientTarget(
        ok=False,
        error=f'I couldn\'t figure out who "{q}" refers to — a talent, project, phone number, or saved list.',
    )


# ---------------------------------------------------------------------------
# Shared resolution — called from both the confirmation-card builder and
# the executor (re-resolved fresh at approval time, same pattern
# casting-agent's MOVE/ADD intents already use).
# ---------------------------------------------------------------------------
@dataclass
class _SendTarget:
    ok: bool
    source_type: Optional[str] = None
    source_params: Optional[SourceParams] = None
    recipient_label: str = ""
    template: Optional[Dict[str, str]] = None
    error: Optional[str] = None
    ambiguous: Optional[AmbiguousEntity] = None
    project_label: Optional[str] = None
    pipeline_stage_label: Optional[str] = None
    # Custom Message / Instagram Profile modes only — the verbatim text to
    # render via the custom template's {{message}} placeholder (see
    # _build_batch_in). None for requirement mode (unaffected).
    literal_message: Optional[str] = None
    # Instagram Profile mode only, no recipient named ("share Pankuri's
    # instagram") — answer inline in this same chat instead of sending via
    # create_batch to someone else. literal_message carries the rendered
    # reply text in this case.
    reply_in_chat: bool = False
    # Instagram Profile mode only — display label for "whose profile(s)",
    # shown on the confirmation card in place of a template name.
    subject_label: str = ""
    # See _RecipientTarget.warning — carried through unchanged.
    warning: Optional[str] = None
    # See _RecipientTarget.multi_project_targets / multi_project_stage_list
    # — carried through unchanged.
    multi_project_targets: Optional[List[Tuple[str, str, List[str]]]] = None
    multi_project_stage_list: Optional[List[str]] = None


async def _resolve_send_target(collected: Dict[str, str]) -> _SendTarget:
    """Dispatches on collected["send_mode"] (absent/empty -> "requirement",
    the original and only mode before this). Every branch returns the same
    _SendTarget shape, so the confirmation-card builder, the executor, and
    _current_recipient_candidates (Preview/Summary/Exclude/Include/
    pagination) all keep working unchanged regardless of mode."""
    send_mode = collected.get("send_mode") or "requirement"
    if send_mode == "custom_message":
        return await _resolve_custom_message_target(collected)
    if send_mode == "instagram":
        return await _resolve_instagram_target(collected)
    return await _resolve_requirement_target(collected)


async def _resolve_requirement_target(collected: Dict[str, str]) -> _SendTarget:
    source_query = (collected.get("source_query") or "").strip()
    recipient_query = (collected.get("recipient_query") or "").strip()
    stage_query = (collected.get("stage_query") or "").strip()
    project_query = (collected.get("project_query") or "").strip()

    tmpl_match = await _resolve_source(source_query)
    if not project_query:
        # Speculative "for/of <project(s)>" split, tried whenever no
        # project_query was already extracted on the recipient side — "the
        # Follow Up template for Project A" (item 1/2/3's shape: the
        # project clause sits inside the SOURCE text, before "to"). Tried
        # UNCONDITIONALLY (not just when the raw attempt already failed):
        # the shared template matcher's generous substring tolerance can
        # otherwise match the RAW, un-split text anyway (a real template
        # named "X" is a substring of "X for Project A"), silently
        # swallowing the "for Project A" clause as harmless trailing noise
        # instead of extracting it — this always prefers the cleaner split
        # interpretation whenever ITS head resolves too, so the project
        # clause is never lost just because the noisy raw text happened to
        # match as well. A template whose own real name genuinely contains
        # "for"/"of" is still safe: the split's HEAD (e.g. "Thanks" out of
        # "Thanks for your time") only wins when it ALSO resolves to a
        # real template — an unrelated fragment essentially never does.
        head, project_tail = _strip_source_project_clause(source_query)
        if project_tail:
            retry_match = await _resolve_source(head)
            if retry_match.template or retry_match.ambiguous:
                tmpl_match = retry_match
                project_query = project_tail

    if tmpl_match.error:
        return _SendTarget(ok=False, error=tmpl_match.error)
    if tmpl_match.ambiguous:
        return _SendTarget(
            ok=False,
            ambiguous=AmbiguousEntity(
                entity_type="template", field_key="source_query",
                candidates=[
                    disambiguation.Candidate(id=c.get("id", c["label"]), label=c["label"])
                    for c in tmpl_match.ambiguous
                ],
            ),
        )

    # Source resolved cleanly — only now check the recipient, so at most
    # ONE disambiguation is ever open at a time (source first, then
    # recipient), matching the engine's single-pending-choice state model.
    recipient = await _resolve_recipient_multi_aware(recipient_query, stage_query, project_query)
    if not recipient.ok:
        if recipient.ambiguous:
            return _SendTarget(ok=False, ambiguous=recipient.ambiguous)
        return _SendTarget(ok=False, error=recipient.error)

    return _SendTarget(
        ok=True, source_type=recipient.source_type, source_params=recipient.source_params,
        recipient_label=recipient.display_label, template=tmpl_match.template,
        project_label=recipient.project_label, pipeline_stage_label=recipient.pipeline_stage_label,
        warning=recipient.warning, multi_project_targets=recipient.multi_project_targets,
        multi_project_stage_list=recipient.multi_project_stage_list,
    )


async def _resolve_custom_message_target(collected: Dict[str, str]) -> _SendTarget:
    """Custom Message mode — source_query holds the exact quoted/colon-body
    text (see _extract_custom_message_fields); recipient resolution reuses
    _resolve_recipient_multi_aware (project/stage/talent(s)/phone/CRM/
    saved-list, same disambiguation/safety-gate machinery as every other
    send, now multi-project/multi-stage aware too)."""
    message_text = collected.get("source_query") or ""
    if not message_text:
        return _SendTarget(ok=False, error="What should the message say?")

    custom_template = await _fetch_custom_template()
    if not custom_template:
        return _SendTarget(
            ok=False,
            error="The built-in custom-message template is missing — please contact an admin.",
        )

    recipient_query = (collected.get("recipient_query") or "").strip()
    stage_query = (collected.get("stage_query") or "").strip()
    project_query = (collected.get("project_query") or "").strip()
    recipient = await _resolve_recipient_multi_aware(recipient_query, stage_query, project_query)
    if not recipient.ok:
        if recipient.ambiguous:
            return _SendTarget(ok=False, ambiguous=recipient.ambiguous)
        return _SendTarget(ok=False, error=recipient.error)

    return _SendTarget(
        ok=True, source_type=recipient.source_type, source_params=recipient.source_params,
        recipient_label=recipient.display_label, template=custom_template,
        literal_message=message_text,
        project_label=recipient.project_label, pipeline_stage_label=recipient.pipeline_stage_label,
        warning=recipient.warning, multi_project_targets=recipient.multi_project_targets,
        multi_project_stage_list=recipient.multi_project_stage_list,
    )


async def _talent_instagram_by_id(ids: List[str]) -> Dict[str, dict]:
    """Mirrors _talent_names_by_id below, but also projects
    instagram_handle — _fetch_all_talent_candidates only carries id+name,
    which every OTHER resolution path needs and this one additionally
    needs the stored handle for."""
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    docs = await db.talents.find(
        {"id": {"$in": ids}}, {"_id": 0, "id": 1, "name": 1, "instagram_handle": 1},
    ).to_list(len(ids))
    return {d["id"]: d for d in docs}


def _format_instagram_send_body(resolved: List[Tuple[str, str]], tmap: Dict[str, dict]) -> str:
    """Numbered "1.\\nName\\nURL" (or "...\\nInstagram profile not
    available." when missing) — shared by both the recipient-send and
    reply-in-chat paths so the exact same text renders either way."""
    lines: List[str] = []
    for i, (tid, label) in enumerate(resolved, start=1):
        handle = (tmap.get(tid) or {}).get("instagram_handle")
        url = _format_instagram_link(handle)
        if i > 1:
            lines.append("")
        lines.append(f"{i}.")
        lines.append(label)
        lines.append(url if url else "Instagram profile not available.")
    return "\n".join(lines)


async def _resolve_instagram_target(collected: Dict[str, str]) -> _SendTarget:
    """Instagram Profile Send mode — source_query holds the subject
    talent name/list (whose Instagram, see _extract_instagram_fields).
    Resolved via the SAME shared _resolve_talent_names function
    _resolve_recipient's Tier 3 uses — not a separate lookup: a single
    subject name goes through the exact single-name tier (exact/
    normalized/token/fuzzy matching + safety gate), a multi-name list
    through the exact same per-fragment multi-name + disambiguation path
    Multi Manual Recipients uses for recipients. No recipient named ->
    reply_in_chat=True (answer inline, never calls _resolve_recipient or
    create_batch at all)."""
    subject_query = (collected.get("source_query") or "").strip()
    if not subject_query:
        return _SendTarget(ok=False, error="Whose Instagram profile should I send?")

    talents = await _resolve_talent_names(
        subject_query,
        single_ambiguous_field_key="source_query",
        multi_pick_field_key=_PENDING_MULTI_SUBJECT_PICK_KEY,
        multi_fragments_key=_PENDING_MULTI_SUBJECT_FRAGMENTS_KEY,
        multi_index_key=_PENDING_MULTI_SUBJECT_INDEX_KEY,
    )
    if talents.ambiguous:
        return _SendTarget(ok=False, ambiguous=talents.ambiguous)
    if not talents.ok:
        # Unlike _resolve_recipient's Tier 3, Instagram has no CRM/saved-
        # list fallback — a subject is always a talent name, so any
        # non-ambiguous failure (including not_found) is terminal here.
        return _SendTarget(ok=False, error=talents.error or f"Couldn't find:\n\n{subject_query}")

    resolved_pairs = list(zip(talents.talent_ids, talents.talent_labels))
    tmap = await _talent_instagram_by_id(talents.talent_ids)
    message_text = _format_instagram_send_body(resolved_pairs, tmap)
    subject_label = ", ".join(talents.talent_labels)

    recipient_query = (collected.get("recipient_query") or "").strip()
    if not recipient_query or recipient_query == _REPLY_IN_CHAT_SENTINEL:
        return _SendTarget(ok=True, reply_in_chat=True, literal_message=message_text, subject_label=subject_label)

    custom_template = await _fetch_custom_template()
    if not custom_template:
        return _SendTarget(
            ok=False,
            error="The built-in custom-message template is missing — please contact an admin.",
        )
    # Production fix (2026-08-19) — the RECIPIENT here (who receives the
    # profile link) is never the campaign's talent audience, so it goes
    # through the dedicated recipient resolver (CRM contact by name ->
    # saved list -> phone number), never the talent resolver. See that
    # function's module comment for the exact bug this closes.
    recipient = await _resolve_recipient_only(recipient_query)
    if not recipient.ok:
        if recipient.ambiguous:
            return _SendTarget(ok=False, ambiguous=recipient.ambiguous)
        return _SendTarget(ok=False, error=recipient.error)

    return _SendTarget(
        ok=True, source_type=recipient.source_type, source_params=recipient.source_params,
        recipient_label=recipient.display_label, template=custom_template,
        literal_message=message_text, subject_label=subject_label,
    )


def _truncate(s: str, limit: int = 300) -> str:
    s = s or ""
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


def _delivery_summary(jobs: List[dict]) -> str:
    groups = sum(1 for j in jobs if j.get("destination_type") == "group")
    numbers = sum(1 for j in jobs if j.get("destination_type") == "number")
    delivery_parts = []
    if groups:
        delivery_parts.append(f"{groups} WhatsApp Group{'s' if groups != 1 else ''}")
    if numbers:
        delivery_parts.append(f"{numbers} Phone Number{'s' if numbers != 1 else ''}")
    return ", ".join(delivery_parts) if delivery_parts else "(none)"


# ---------------------------------------------------------------------------
# Show Recipient List (2026-08-09) — the confirmation card's bare
# "Recipients: N" count, replaced with the actual numbered list, paginated
# past 20. Numbers are assigned from the FULL resolved set
# (_current_recipient_candidates(..., apply_exclusions=False) — existing,
# unmodified helper), not from whatever create_batch currently returns
# WITH exclusions applied — a recipient's number must never shift just
# because an earlier one got excluded (sprint requirement: "Indexes shown
# to the user must remain stable after exclusions"). The DISPLAYED list
# still only shows currently-sendable (non-excluded) recipients; excluded
# ones simply leave a gap in the numbering rather than causing everyone
# after them to renumber down.
# ---------------------------------------------------------------------------
_RECIPIENT_PAGE_SIZE = 20


def _stable_sorted_jobs(jobs: List[dict]) -> List[dict]:
    """resolve_recipients_engine's PROJECT branch (routers/whatsapp.py,
    unmodified — out of scope to touch) dedupes talent ids through a
    Python set() with no explicit Mongo sort, so its OWN return order
    isn't guaranteed stable across a server restart. Sorting here, in OUR
    code, the same way casting-agent's own pipeline listing already does
    (alphabetical, case-insensitive, by display name — see
    casting_pipeline.py's _handle_pipeline_query) makes recipient
    NUMBERING deterministic and stable regardless of upstream iteration
    order, without touching the shared engine at all. The ONE place both
    the confirmation card's numbered list and numbered Exclude/Include
    commands derive ordinals from — both must use this exact same
    ordering or "Exclude 5" could target a different person than the "5."
    the user is looking at."""
    return sorted(jobs, key=lambda j: ((j.get("talent_name") or "").strip().lower(), j["recipient_id"]))


async def _render_numbered_recipient_lines(collected: dict) -> "tuple[List[str], int]":
    """Returns (lines, total_active) — `lines` has no title of its own
    (each confirmation-card layout prepends its own "Recipients (N)" /
    "RECIPIENTS" header, see _build_send_requirement_confirmation)."""
    _target, stable_jobs = await _current_recipient_candidates(collected, apply_exclusions=False)
    stable_jobs = _stable_sorted_jobs(stable_jobs)
    excluded_ids = set(collected.get("excluded_ids") or [])
    active = [(i + 1, job) for i, job in enumerate(stable_jobs) if job["recipient_id"] not in excluded_ids]
    total = len(active)
    if total == 0:
        return ["(no recipients resolved)"], 0

    show_all = str(collected.get("recipient_show_all") or "") == "1"
    show_remaining = str(collected.get("recipient_show_remaining") or "") == "1"
    try:
        page = int(collected.get("recipient_page") or "1")
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)

    if show_all or total <= _RECIPIENT_PAGE_SIZE:
        shown = active
    elif show_remaining:
        # "Show Remaining" — continue from wherever the user was (current
        # page's start), unlimited from there, as opposed to "Show All"
        # (which always restarts from #1).
        start = (page - 1) * _RECIPIENT_PAGE_SIZE
        shown = active[start:] or active
    else:
        start = (page - 1) * _RECIPIENT_PAGE_SIZE
        shown = active[start:start + _RECIPIENT_PAGE_SIZE]
        if not shown:
            # Page beyond the end (e.g. exclusions shrank the active set
            # since this page number was set) — fall back to page 1 rather
            # than showing an empty page.
            shown = active[:_RECIPIENT_PAGE_SIZE]

    # A raw phone-number recipient (Tier 1 of _resolve_recipient — no
    # talent record at all) has no talent_name — fall back to the
    # destination itself so the line is never blank.
    lines = [f"{ordinal}. {job.get('talent_name') or job.get('destination') or 'Unknown'}" for ordinal, job in shown]
    if len(shown) < total:
        lines.append("")
        lines.append(f"Showing {len(shown)} of {total} recipients.")
    return lines, total


async def _set_recipient_page(
    collected: dict, ctx: ExecContext, *, page: int, show_all: bool = False, show_remaining: bool = False,
) -> str:
    """The one place Next/Previous/Page N/Show All/Show Remaining all
    funnel through — only ever touches recipient_page/recipient_show_all/
    recipient_show_remaining, never excluded_ids/included_ids, so paging
    can never lose an exclusion (sprint requirement)."""
    new_collected = dict(collected)
    new_collected["recipient_page"] = str(max(1, page))
    new_collected["recipient_show_all"] = "1" if show_all else ""
    new_collected["recipient_show_remaining"] = "1" if show_remaining else ""
    await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, collected=new_collected)
    return await _build_send_requirement_confirmation(new_collected, ctx)


async def _change_recipient_page(collected: dict, ctx: ExecContext, *, delta: int) -> str:
    try:
        current = int(collected.get("recipient_page") or "1")
    except (TypeError, ValueError):
        current = 1
    return await _set_recipient_page(collected, ctx, page=current + delta)


async def _build_batch_in(target: "_SendTarget", collected: dict, *, is_dry_run: bool) -> BatchIn:
    """Single place every create_batch() call in this module builds its
    payload — threads excluded_ids into the EXISTING
    BatchIn.excluded_recipient_ids / resolve_recipients_engine exclusion
    support (routers/whatsapp.py, unmodified) so Interactive Campaign
    Editing needs zero new filtering logic of its own.

    target.literal_message (Custom Message / Instagram Profile modes only)
    is threaded through as variable_data["message"] — the SAME
    BatchIn.variable_data field the web app's campaign UI already uses for
    manually-injected variables, rendered by the existing, unmodified
    _render_message via the seeded custom template's {{message}}
    placeholder. Requirement mode never sets literal_message, so this is
    variable_data={} exactly as before — unaffected."""
    variable_data = {"message": target.literal_message} if target.literal_message is not None else {}
    return BatchIn(
        source_type=target.source_type, source_params=target.source_params,
        excluded_recipient_ids=list(collected.get("excluded_ids") or []),
        template_id=target.template["id"], is_dry_run=is_dry_run,
        variable_data=variable_data,
    )


async def _create_batch_for_target(
    target: "_SendTarget", collected: dict, *, is_dry_run: bool, admin: Optional[dict] = None,
) -> Dict[str, Any]:
    """The one place every create_batch() call for a resolved send target
    is actually made — every existing caller (confirmation-card preview,
    live executor, plan-step executor, Exclude/Include's recipient lookup)
    now goes through here instead of calling create_batch directly.

    target.multi_project_targets is None for the overwhelmingly common
    case (single project, CRM, saved list, phone number, or no project at
    all) — behaves EXACTLY as before, one create_batch call, unchanged.

    When it's set (2+ named projects each contributed real recipients —
    see _resolve_talents_narrowed_by_projects / _resolve_project_stage_
    union), this fans out into one source_type="PROJECT" create_batch call
    PER project instead. Each call reuses the exact same, unmodified
    PROJECT-source rendering path (routers/whatsapp.py's create_batch ->
    _project_variables) a single-project send already uses — so every
    project's own {{project_name}}/{{shoot_dates}}/{{budget}}/
    {{submission_link}} (and any other template variable) renders from
    THAT project's own data, never another named project's, and never left
    as a raw unrendered placeholder. A project with zero matching
    recipients (create_batch raises for an empty batch) is simply skipped,
    exactly like the pre-existing per-project probe already tolerated.

    The resulting jobs are then MERGED under the first sub-call's real
    `whatsapp_batches` document (its own is deleted) — every existing
    caller/consumer (this module's own "Batch ID: <id>" reply text, web-UI
    campaign history, every pre-existing test) still sees exactly ONE
    batch for one send command, never a new multi-batch concept to know
    about. Only the per-recipient RENDERING was ever wrong; a "send" is
    still just "a batch" to everyone outside this function."""
    admin = admin or await _service_admin()
    if not target.multi_project_targets:
        result = await create_batch(await _build_batch_in(target, collected, is_dry_run=is_dry_run), admin=admin)
        return {"jobs": result["jobs"], "skipped": result["skipped"], "batch_ids": [result["batch"]["id"]]}

    variable_data = {"message": target.literal_message} if target.literal_message is not None else {}
    excluded_ids = list(collected.get("excluded_ids") or [])
    stage_list = target.multi_project_stage_list or list(PIPELINE_STAGE_ORDER)
    jobs: List[dict] = []
    skipped: List[dict] = []
    sub_batch_ids: List[str] = []
    for pid, _plabel, talent_ids in target.multi_project_targets:
        sub_batch_in = BatchIn(
            source_type="PROJECT",
            source_params=SourceParams(
                project_id=pid, pipeline_stages=stage_list, talent_ids=talent_ids,
            ),
            excluded_recipient_ids=excluded_ids,
            template_id=target.template["id"], is_dry_run=is_dry_run,
            variable_data=variable_data,
        )
        try:
            result = await create_batch(sub_batch_in, admin=admin)
        except HTTPException:
            continue
        jobs.extend(result["jobs"])
        skipped.extend(result["skipped"])
        sub_batch_ids.append(result["batch"]["id"])

    if not sub_batch_ids:
        return {"jobs": jobs, "skipped": skipped, "batch_ids": []}

    primary_id, extra_ids = sub_batch_ids[0], sub_batch_ids[1:]
    if extra_ids:
        await db.whatsapp_jobs.update_many(
            {"batch_id": {"$in": extra_ids}}, {"$set": {"batch_id": primary_id}},
        )
        await db.whatsapp_batches.update_one(
            {"id": primary_id}, {"$set": {"total_jobs": len(jobs)}},
        )
        await db.whatsapp_batches.delete_many({"id": {"$in": extra_ids}})
        for j in jobs:
            j["batch_id"] = primary_id
    return {"jobs": jobs, "skipped": skipped, "batch_ids": [primary_id]}


async def _talent_names_by_id(ids: List[str]) -> Dict[str, str]:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    docs = await db.talents.find(
        {"id": {"$in": ids}}, {"_id": 0, "id": 1, "name": 1}
    ).to_list(len(ids))
    return {d["id"]: d.get("name") or d["id"] for d in docs}


async def _resume_pending_recipient_edit(collected: dict, ctx: ExecContext) -> dict:
    """Interactive Campaign Editing (2026-08-09) — if the last turn opened
    a disambiguation round for an ambiguous "Exclude X"/"Include X" (see
    _apply_recipient_edit), the shared engine's own resolution (dispatcher.
    py's _advance_disambiguation) has already written the picked
    candidate's exact label into one of these two private keys and
    resumed here via the normal confirming-step render. Applies it (now
    unambiguous) and clears the marker. A no-op (returns `collected`
    unchanged) when neither key is present — the overwhelmingly common
    case."""
    pending_exclude = collected.get("_pending_exclude_name")
    pending_include = collected.get("_pending_include_name")
    if not pending_exclude and not pending_include:
        return collected
    new_collected = dict(collected)
    new_collected.pop("_pending_exclude_name", None)
    new_collected.pop("_pending_include_name", None)
    _, jobs = await _current_recipient_candidates(new_collected, apply_exclusions=False)
    result = await _resolve_named_recipient(pending_exclude or pending_include, jobs)
    if result.job:
        excluded_ids = list(new_collected.get("excluded_ids") or [])
        included_ids = list(new_collected.get("included_ids") or [])
        rid = result.job["recipient_id"]
        if pending_exclude:
            if rid not in excluded_ids:
                excluded_ids.append(rid)
            if rid in included_ids:
                included_ids.remove(rid)
        else:
            if rid in excluded_ids:
                excluded_ids.remove(rid)
            if rid not in included_ids:
                included_ids.append(rid)
        new_collected["excluded_ids"] = excluded_ids
        new_collected["included_ids"] = included_ids
    await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, collected=new_collected)
    return new_collected


def _deserialize_send_plan(raw: Optional[str]) -> List[Dict[str, str]]:
    try:
        steps = json.loads(raw or "[]")
        return steps if isinstance(steps, list) else []
    except (TypeError, ValueError):
        return []


def _describe_send_target(step_fields: Dict[str, str], target: "_SendTarget") -> str:
    mode = step_fields.get("send_mode") or "requirement"
    if mode == "custom_message":
        return _truncate(target.literal_message or "", 60)
    if mode == "instagram":
        return f"Instagram profile(s): {target.subject_label}"
    return (target.template.get("name") or target.template.get("slug") or "") if target.template else ""


async def _build_send_plan_confirmation(collected: dict, ctx: ExecContext) -> str:
    """Bulk Multi-Command Sends (2026-08-17) — preview for several
    independent send commands sharing one approval. Mirrors casting-
    agent's plan-preview convention exactly: an ambiguous or unresolvable
    command is shown INLINE as an error line rather than opening an
    interactive disambiguation round WITHIN the plan (casting-agent's own
    plan engine has the same limitation — resend that one command alone
    to disambiguate it). Every command is re-resolved fresh here (same
    "no stale resolution" rule the single-command path already follows)."""
    steps = _deserialize_send_plan(collected.get(PLAN_FIELD.key))
    lines = ["You are about to run this plan:", ""]
    for i, step_fields in enumerate(steps, start=1):
        target = await _resolve_send_target(step_fields)
        if target.ok and target.reply_in_chat:
            lines.append(f"{i}. Instagram profile(s) for {target.subject_label} — answered inline, nothing queued")
            continue
        if not target.ok:
            if target.ambiguous:
                lines.append(
                    f"{i}. Multiple matches found for this command — resend it alone to pick one."
                )
            else:
                lines.append(f"{i}. {target.error}")
            continue
        desc = _describe_send_target(step_fields, target)
        lines.append(f"{i}. {desc} → {target.recipient_label}")
    lines.append("")
    lines.append("Reply:")
    lines.append("1 → Approve")
    lines.append("2 → Edit")
    lines.append("3 → Cancel")
    return "\n".join(lines)


async def _execute_send_plan(collected: dict, ctx: ExecContext) -> ExecResult:
    """Runs every command SEQUENTIALLY — each wrapped in its own try/
    except so one failing/ambiguous command never aborts the rest (same
    partial-failure tolerance casting-agent's _execute_plan already
    guarantees). No cross-command dedup: unlike casting's talent/project
    fan-out, two independent sends are never meant to merge into one —
    each keeps its own template/message and its own create_batch call."""
    steps = _deserialize_send_plan(collected.get(PLAN_FIELD.key))
    summary_lines = ["Completed", ""]
    any_success = False
    admin = await _service_admin()

    for i, step_fields in enumerate(steps, start=1):
        try:
            target = await _resolve_send_target(step_fields)
        except Exception:
            logger.exception("send plan step resolution failed index=%s", i)
            summary_lines += [f"✗ Command {i}", "", "Something went wrong resolving this command.", ""]
            continue
        if target.ok and target.reply_in_chat:
            summary_lines += [f"✓ Command {i}", "", target.literal_message or "", ""]
            any_success = True
            continue
        if not target.ok:
            summary_lines += [f"✗ Command {i}", "", target.error or "Could not resolve this command.", ""]
            continue
        try:
            result = await _create_batch_for_target(target, step_fields, is_dry_run=False, admin=admin)
        except HTTPException as exc:
            summary_lines += [f"✗ Command {i}", "", f"Couldn't send that: {exc.detail}", ""]
            continue
        except Exception:
            logger.exception("send plan step execution failed index=%s", i)
            summary_lines += [f"✗ Command {i}", "", "Something went wrong sending this.", ""]
            continue
        queued = len(result["jobs"])
        desc = _describe_send_target(step_fields, target)
        summary_lines += [
            f"✓ Command {i}", "", f"{desc} → {target.recipient_label}",
            f"Queued {queued} message(s)", "",
        ]
        any_success = True

    return ExecResult(ok=any_success, message="\n".join(summary_lines).rstrip())


async def _build_send_requirement_confirmation(collected: dict, ctx: ExecContext) -> str:
    if collected.get(PLAN_FIELD.key):
        return await _build_send_plan_confirmation(collected, ctx)
    collected = await _resume_pending_recipient_edit(collected, ctx)
    collected = await _resume_pending_multi_recipient(collected, ctx)
    collected = await _resume_pending_multi_subject(collected, ctx)
    collected = await _resume_pending_multi_project(collected, ctx)
    collected = await _resume_pending_multi_stage(collected, ctx)
    target = await _resolve_send_target(collected)
    if target.ok and target.reply_in_chat:
        # Instagram Profile Send, no recipient named ("share Pankuri's
        # instagram") — nothing is being sent to anyone else, so this
        # answers inline instead of opening a confirmation card. Mirrors
        # the unresolvable-error short-circuit right below (clear + return
        # text directly), just for a successful, not a failed, outcome.
        await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
        return target.literal_message or ""
    if not target.ok:
        if target.ambiguous:
            start_collected = collected
            if target.ambiguous.extra_collected:
                start_collected = {**collected, **target.ambiguous.extra_collected}
            await disambiguation.start(
                agent_id=ctx.agent_id, phone=ctx.sender_phone,
                entity_type=target.ambiguous.entity_type,
                candidates=target.ambiguous.candidates,
                intent_id=SEND_REQUIREMENT_INTENT.intent_id,
                field_key=target.ambiguous.field_key,
                collected=start_collected,
            )
            # Overrides the "confirming" step dispatcher.py just set right
            # before calling this hook — same override-after-the-fact
            # pattern ExecResult.needs_clarification already uses for
            # "editing" step, just for the new "disambiguating" step.
            await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, step="disambiguating")
            return disambiguation.format_prompt(target.ambiguous.entity_type, target.ambiguous.candidates)
        # Genuinely unresolvable — end this attempt cleanly.
        await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
        return target.error

    try:
        preview = await _create_batch_for_target(target, collected, is_dry_run=True)
    except HTTPException as exc:
        await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
        return f"Couldn't prepare that: {exc.detail}"

    jobs = preview["jobs"]
    skipped = preview["skipped"]
    template_label = target.template.get("name") or target.template.get("slug") or ""

    # Two card layouts exist below (the richer explicit-pipeline-stage
    # summary, and the generic one) — each already used a DIFFERENT label
    # for this same value even before Custom Message/Instagram existed
    # ("Template" vs. "MESSAGE SOURCE"), so both need their own mode-aware
    # label, not one shared between them.
    send_mode = collected.get("send_mode") or "requirement"
    if send_mode == "custom_message":
        action_label = "Send Custom Message"
        pipeline_header_label = header_label = "MESSAGE"
        header_value = _truncate(target.literal_message or "")
    elif send_mode == "instagram":
        action_label = "Send Instagram Profile(s)"
        pipeline_header_label = header_label = "INSTAGRAM PROFILE(S)"
        header_value = target.subject_label
    else:
        action_label = "Send Requirement"
        pipeline_header_label, header_label = "Template", "MESSAGE SOURCE"
        header_value = template_label

    excluded_ids = collected.get("excluded_ids") or []
    included_ids = collected.get("included_ids") or []
    name_map = await _talent_names_by_id(list(excluded_ids) + list(included_ids))
    excluded_names = [name_map.get(i, i) for i in excluded_ids]
    included_names = [name_map.get(i, i) for i in included_ids]
    edit_lines: List[str] = []
    if excluded_names:
        edit_lines += ["", "Excluded", ", ".join(excluded_names)]
    if included_names:
        edit_lines += ["", "Included", ", ".join(included_names)]

    recipient_lines, total_active = await _render_numbered_recipient_lines(collected)

    if target.pipeline_stage_label:
        # An EXPLICIT single pipeline stage was resolved (not the "whole
        # project, every stage" default) — the richer Project/Stage
        # summary from the sprint spec, now WITH the numbered recipient
        # list too (previously just a bare count here).
        destination = _delivery_summary(jobs)
        lines = [
            pipeline_header_label, header_value, "",
            "Recipient Type", "Pipeline", "",
            "Project", target.project_label or "", "",
            "Stage", target.pipeline_stage_label, "",
            f"Recipients ({total_active})", "",
        ]
        lines += recipient_lines
        lines += ["", "Destination", destination]
        lines += edit_lines
        if skipped:
            lines += ["", f"⚠ {len(skipped)} skipped (no phone/group on file)"]
        if target.warning:
            lines += ["", f"⚠ {target.warning}"]
        if jobs:
            lines += ["", "Sample message", "", _truncate(jobs[0]["message_body"])]
        lines += ["", "Reply", "1 Approve", "2 Edit", "3 Cancel"]
        return "\n".join(lines)

    delivery = _delivery_summary(jobs)

    lines = [
        "ACTION",
        action_label,
        "",
        header_label,
        header_value,
        "",
        f"RECIPIENTS ({total_active})",
        "",
    ]
    lines += recipient_lines
    lines += ["", "DELIVERY", delivery]
    lines += edit_lines
    if skipped:
        lines += ["", f"⚠ {len(skipped)} skipped (no phone/group on file)"]
    if target.warning:
        lines += ["", f"⚠ {target.warning}"]
    if jobs:
        lines += ["", "Sample message", "", _truncate(jobs[0]["message_body"])]
    lines += ["", "Reply", "1 Approve", "2 Edit", "3 Cancel"]
    return "\n".join(lines)


async def _send_requirement_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    if collected.get(PLAN_FIELD.key):
        return await _execute_send_plan(collected, ctx)
    target = await _resolve_send_target(collected)
    if target.ok and target.reply_in_chat:
        # Defensive only — _build_send_requirement_confirmation already
        # short-circuits a reply_in_chat target before any "Approve" reply
        # is possible, so this executor should never actually be reached
        # for one. Fail clearly rather than calling create_batch with
        # nothing to send.
        return ExecResult(
            ok=False, error="send_requirement_reply_in_chat_no_batch",
            message="Nothing to send — that was already answered inline.",
        )
    if not target.ok:
        if target.ambiguous:
            # (2026-08-09, production-readiness audit) Re-resolving fresh
            # at approval time (see module comment above) means a match
            # that was clean when the confirmation card was built COULD in
            # principle become ambiguous again if the underlying data
            # changed in between (e.g. a same-named record was added).
            # Re-opening a disambiguation round from inside an approval
            # turn is out of scope — just fail clearly instead of leaving
            # `message` as None, which would reach the WhatsApp worker as
            # a blank/crashing send.
            return ExecResult(
                ok=False, error="send_requirement_became_ambiguous",
                message="That selection is no longer unique — please resend your command.",
            )
        return ExecResult(ok=False, error="send_requirement_resolution_failed", message=target.error)

    try:
        result = await _create_batch_for_target(target, collected, is_dry_run=False)
    except HTTPException as exc:
        return ExecResult(
            ok=False, error="send_requirement_launch_failed",
            message=f"Couldn't send that: {exc.detail}",
        )

    batch_ids = result["batch_ids"]
    queued = len(result["jobs"])
    template_label = target.template.get("name") or target.template.get("slug") or ""
    message = (
        "Sent.\n\n"
        f"Message Source\n{template_label}\n\n"
        f"Recipients\n{target.recipient_label}\n\n"
        f"Queued {queued} message(s) — delivery happens over the next few minutes.\n\n"
        f"Batch ID: {', '.join(batch_ids)}"
    )
    return ExecResult(ok=True, message=message, data={"batch_id": batch_ids[0] if batch_ids else None, "queued": queued})


async def _send_requirement_try_auto_execute(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    """"...and confirm" — mirrors casting-agent's _move_try_auto_execute/
    _add_try_auto_execute exactly: skip the approval card and send
    immediately, but ONLY once resolution is fully clean. If resolution is
    still ambiguous/erroring, return None so the normal confirmation flow
    runs instead (which sets up disambiguation as usual); AUTO_CONFIRM_
    FIELD persists in `collected` across that continuation, so this check
    re-fires and auto-sends once the ambiguity resolves — no extra state
    needed for "keep going automatically after resolving the ambiguity,
    without asking for a second approval". For a multi-command plan, mirrors
    casting-agent's own plan try_auto_execute: run the plan directly (its
    own per-command try/except already tolerates a partial failure), no
    "fully clean" pre-check — the executed summary reports ✗ for whichever
    individual commands couldn't resolve, exactly like approving one
    manually would."""
    if collected.get(LEGACY_SYNTAX_FIELD.key):
        # Old Interface Deprecation (2026-08-30) — fires unconditionally
        # (not gated on "and confirm"), before anything else: never shows
        # a confirmation card for the old hyphen grammar, never executes
        # it, just explains the new format. See
        # _extract_one_send_command_fields for where this gets set.
        return ExecResult(
            ok=True,
            message=(
                "I use the new command format now.\n\n"
                "Try:\n"
                "Send Kripa the casting call for Parachute Jasmine Oil\n\n"
                "Send HELP to see every way to write a command."
            ),
        )
    if not collected.get(AUTO_CONFIRM_FIELD.key):
        return None
    if collected.get(PLAN_FIELD.key):
        return await _execute_send_plan(collected, ctx)
    target = await _resolve_send_target(collected)
    if not target.ok or target.reply_in_chat:
        # Still ambiguous/erroring, or an Instagram reply-in-chat (which
        # has nothing to approve at all and is handled by its own
        # short-circuit in _build_send_requirement_confirmation) — either
        # way, fall through to the normal flow rather than sending.
        return None
    return await _send_requirement_executor(collected, ctx)


# ---------------------------------------------------------------------------
# Interactive Campaign Editing (2026-08-09) — Exclude/Include/Change
# template/Preview/Summary, typed directly on top of an already-shown
# confirmation card, BEFORE create_batch() is ever called with is_dry_run=
# False. No new conversation engine (see agents/models.py's
# handle_confirming_reply docstring) — everything here mutates the SAME
# `collected` dict conversation.py already persists for the "confirming"
# step; create_batch() itself is called only from _build_send_requirement_
# confirmation (dry-run, for every card re-render) and
# _send_requirement_executor (live, on "Send") — both already existed
# before this sprint, neither is duplicated or bypassed.
# ---------------------------------------------------------------------------
@dataclass
class _NamedRecipientResult:
    job: Optional[dict] = None
    error: Optional[str] = None
    ambiguous: Optional[List["nlu.Candidate"]] = None


async def _current_recipient_candidates(
    collected: dict, *, apply_exclusions: bool = True,
) -> "tuple[Optional[_SendTarget], List[dict]]":
    """Re-resolves the pending campaign and runs a fresh dry-run preview —
    the SAME call _build_send_requirement_confirmation itself always
    makes (no duplicate resolution/rendering).

    `apply_exclusions=True` (the default — Preview/Summary/card-rendering
    callers) applies whatever excluded_ids are already recorded, so those
    surfaces always reflect the CURRENT sendable state.

    `apply_exclusions=False` (name-RESOLUTION callers — see
    _resolve_named_recipient) deliberately does NOT filter — an already-
    excluded talent must still be findable by name, or "Exclude X" could
    never detect "already excluded", and "Include X" (which by definition
    targets someone currently excluded) could never find her at all."""
    target = await _resolve_send_target(collected)
    if not target.ok:
        return None, []
    lookup_collected = collected if apply_exclusions else {k: v for k, v in collected.items() if k != "excluded_ids"}
    try:
        preview = await _create_batch_for_target(target, lookup_collected, is_dry_run=True)
    except HTTPException:
        return target, []
    return target, preview["jobs"]


async def _resolve_named_recipient(name_query: str, jobs: List[dict]) -> _NamedRecipientResult:
    """Resolves free text against the CURRENT campaign's OWN recipient
    pool only (never the global talent database) — reuses the exact same
    selector parsing (nlu.parse_talent_selector/resolve_against_candidates)
    and the safety gate added after the Ami/Kripa Trivedi incident
    (_fuzzy_match_is_safe), so "Exclude Ahana" can never remove — or
    "Include" reference — someone who isn't genuinely, safely identified
    among today's actual recipients."""
    candidates = [nlu.Candidate(id=j["recipient_id"], label=j["talent_name"]) for j in jobs]
    selector = nlu.parse_talent_selector(name_query)
    if not selector.ok or selector.everyone or selector.ordinals:
        return _NamedRecipientResult(error=f'I couldn\'t find "{name_query}" among the current recipients.')
    resolved = nlu.resolve_against_candidates(selector, candidates)
    if resolved.ambiguous_candidates:
        return _NamedRecipientResult(ambiguous=resolved.ambiguous_candidates)
    if not resolved.ok or not resolved.talent_ids:
        return _NamedRecipientResult(error=f'I couldn\'t find "{name_query}" among the current recipients.')
    fragments = nlu.split_multi_names(name_query)
    label = resolved.talent_labels[0]
    if not any(_fuzzy_match_is_safe(frag, label) for frag in fragments):
        return _NamedRecipientResult(
            error=f'"{name_query}" doesn\'t look like a safe match for "{label}" — please use the exact name.'
        )
    job = next((j for j in jobs if j["recipient_id"] == resolved.talent_ids[0]), None)
    if not job:
        return _NamedRecipientResult(error=f'I couldn\'t find "{name_query}" among the current recipients.')
    return _NamedRecipientResult(job=job)


# Numbered Exclude/Include (Sprint 2: Show Recipient List, 2026-08-09) —
# "Exclude 5", "Exclude 3,5,8", "Exclude 2-8". "Exclude 2 4 8" (space-
# separated, no commas) needs commas inserted between the numbers before
# nlu.parse_talent_selector's own ordinal/range grammar — built for
# casting-agent's "Move 2,4,5,8"/"1-25" shapes — will recognize it; this
# reshapes ONLY the separators, the actual ordinal/range PARSING is 100%
# that existing function, not reimplemented.
_NUMERIC_LIST_RE = re.compile(r"^[\d,\-\s]+$")


def _normalize_numeric_list_text(text: str) -> str:
    return re.sub(r"(\d)\s+(?=\d)", r"\1,", (text or "").strip())


async def _apply_recipient_edit_by_ordinal(
    ordinals: List[int], stable_jobs: List[dict], collected: dict, ctx: ExecContext, *, exclude: bool,
) -> str:
    """Numbers refer to the SAME stable ordering _render_numbered_recipient_
    lines assigns (see _stable_sorted_jobs) — an out-of-range number
    aborts the whole batch with no partial effect (same "never guess,
    never partially apply an invalid selection" rule casting-agent's own
    ordinal move selection already follows). A number that's already in
    the requested state is applied to whatever ELSE in the batch is a
    genuine new change (never a silent no-op, but also never a hard block
    on the rest of a valid batch just because one number was redundant —
    matches casting-agent's own "already in destination stage" bulk-move
    handling)."""
    max_ord = len(stable_jobs)
    out_of_range = [n for n in ordinals if n < 1 or n > max_ord]
    if out_of_range:
        return f"Only {max_ord} recipient(s) are listed — #{out_of_range[0]} is out of range."

    excluded_ids = list(collected.get("excluded_ids") or [])
    included_ids = list(collected.get("included_ids") or [])
    already: List[str] = []
    changed: List[str] = []

    for n in ordinals:
        job = stable_jobs[n - 1]
        rid = job["recipient_id"]
        name = job.get("talent_name") or job.get("destination") or "Unknown"
        if exclude:
            if rid in excluded_ids:
                already.append(name)
                continue
            excluded_ids.append(rid)
            if rid in included_ids:
                included_ids.remove(rid)
        else:
            if rid not in excluded_ids:
                already.append(name)
                continue
            excluded_ids.remove(rid)
            if rid not in included_ids:
                included_ids.append(rid)
        changed.append(name)

    if not changed:
        verb = "excluded" if exclude else "included"
        return f"{', '.join(already)} {'is' if len(already) == 1 else 'are'} already {verb}."

    new_collected = dict(collected)
    new_collected["excluded_ids"] = excluded_ids
    new_collected["included_ids"] = included_ids
    await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, collected=new_collected)
    return await _build_send_requirement_confirmation(new_collected, ctx)


async def _apply_recipient_edit(name_query: str, collected: dict, ctx: ExecContext, *, exclude: bool) -> str:
    logger.info(
        "recipient_edit_handler_called phone=%s exclude=%s name_query=%r",
        ctx.sender_phone, exclude, name_query,
    )
    target, jobs = await _current_recipient_candidates(collected, apply_exclusions=False)
    if target is None:
        return "I couldn't re-check the current campaign — please try again, or Cancel and start over."

    stripped_query = (name_query or "").strip()
    if _NUMERIC_LIST_RE.match(stripped_query):
        selector = nlu.parse_talent_selector(_normalize_numeric_list_text(stripped_query))
        if selector.ok and selector.ordinals:
            logger.info(
                "recipient_edit_handler_called phone=%s path=numeric ordinals=%s",
                ctx.sender_phone, selector.ordinals,
            )
            return await _apply_recipient_edit_by_ordinal(
                selector.ordinals, _stable_sorted_jobs(jobs), collected, ctx, exclude=exclude,
            )
        return selector.error or f'I couldn\'t understand "{name_query}" as a recipient number.'

    names = nlu.split_multi_names(name_query)
    excluded_ids = list(collected.get("excluded_ids") or [])
    included_ids = list(collected.get("included_ids") or [])
    action_label = "excluded" if exclude else "included"

    for nm in names:
        result = await _resolve_named_recipient(nm, jobs)
        if result.ambiguous:
            # Reuses the SAME shared disambiguation engine every other
            # ambiguity on this platform goes through — never a separate
            # clarification flow. field_key is a private marker (see
            # _resume_pending_recipient_edit) rather than "recipient_query"
            # itself: picking a candidate here must EXCLUDE/INCLUDE them,
            # not replace the whole campaign's recipient.
            await disambiguation.start(
                agent_id=ctx.agent_id, phone=ctx.sender_phone, entity_type="talent",
                candidates=[disambiguation.Candidate(id=c.id, label=c.label) for c in result.ambiguous],
                intent_id=SEND_REQUIREMENT_INTENT.intent_id,
                field_key="_pending_exclude_name" if exclude else "_pending_include_name",
                collected=collected,
            )
            await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, step="disambiguating")
            return disambiguation.format_prompt(
                "talent", [disambiguation.Candidate(id=c.id, label=c.label) for c in result.ambiguous],
            )
        if result.error:
            return result.error

        rid = result.job["recipient_id"]
        name = result.job["talent_name"]
        if exclude:
            if rid in excluded_ids:
                return f"{name} is already excluded."
            excluded_ids.append(rid)
            if rid in included_ids:
                included_ids.remove(rid)
        else:
            if rid not in excluded_ids:
                return f"{name} hasn't been excluded — nothing to include."
            excluded_ids.remove(rid)
            if rid not in included_ids:
                included_ids.append(rid)

    new_collected = dict(collected)
    new_collected["excluded_ids"] = excluded_ids
    new_collected["included_ids"] = included_ids
    await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, collected=new_collected)
    return await _build_send_requirement_confirmation(new_collected, ctx)


async def _apply_template_change(template_query: str, collected: dict, ctx: ExecContext) -> str:
    tmpl_match = await _resolve_source(template_query)
    if tmpl_match.template:
        new_collected = dict(collected)
        new_collected["source_query"] = (
            tmpl_match.template.get("name") or tmpl_match.template.get("slug") or template_query
        )
        await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, collected=new_collected)
        return await _build_send_requirement_confirmation(new_collected, ctx)
    if tmpl_match.ambiguous:
        candidates = [disambiguation.Candidate(id=c.get("id", c["label"]), label=c["label"]) for c in tmpl_match.ambiguous]
        await disambiguation.start(
            agent_id=ctx.agent_id, phone=ctx.sender_phone, entity_type="template",
            candidates=candidates, intent_id=SEND_REQUIREMENT_INTENT.intent_id,
            field_key="source_query", collected=collected,
        )
        await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, step="disambiguating")
        return disambiguation.format_prompt("template", candidates)
    return f'I couldn\'t find a template matching "{template_query}" — the current campaign is unchanged.'


async def _render_preview(collected: dict) -> str:
    target, jobs = await _current_recipient_candidates(collected)
    if target is None:
        return "I couldn't prepare a preview right now — please try again."
    if not jobs:
        return "No recipients to preview — everyone has been excluded."
    j = jobs[0]
    return f"Preview — exactly what {j['talent_name']} would receive:\n\n{j['message_body']}"


async def _render_summary(collected: dict) -> str:
    target, jobs = await _current_recipient_candidates(collected)
    if target is None:
        return "I couldn't prepare a summary right now — please try again."
    template_label = (target.template.get("name") or target.template.get("slug") or "") if target.template else ""
    excluded_ids = collected.get("excluded_ids") or []
    included_ids = collected.get("included_ids") or []
    name_map = await _talent_names_by_id(list(excluded_ids) + list(included_ids))
    lines = ["Project", target.project_label or target.recipient_label or "", ""]
    if target.pipeline_stage_label:
        lines += ["Pipeline", target.pipeline_stage_label, ""]
    lines += [
        "Template", template_label, "",
        "Recipient Count", str(len(jobs)), "",
        "Excluded", ", ".join(name_map.get(i, i) for i in excluded_ids) or "(none)", "",
        "Included", ", ".join(name_map.get(i, i) for i in included_ids) or "(none)",
    ]
    return "\n".join(lines)


_LEADING_CONNECTOR_RE = re.compile(r"^\s*(?:also|and|then)\s+", re.IGNORECASE)
# "delete" added (Sprint 2, 2026-08-09) — a synonym the spec explicitly
# lists ("Delete 5"); purely additive, every existing exclude synonym
# still works exactly as before.
_EXCLUDE_TRIGGERS = ["exclude", "remove", "skip", "leave out", "don't send to", "do not send to", "delete"]
# "add back" added (Sprint 2) — covers "Add back 5"/"Add back Kripa" (the
# trigger word sits at the FRONT, unlike "Add <Name> back" below, where it
# sits in the middle) via the same longest-match-first prefix-strip every
# other trigger here already uses.
_INCLUDE_TRIGGERS = ["include", "restore", "undo", "add back"]
# "Add <Name> back" — the name sits BETWEEN the trigger words, not after
# them, so it can't use the simple prefix-strip _strip_leading_phrase every
# other trigger list uses.
_ADD_BACK_RE = re.compile(r"^\s*add\s+(.+?)\s+back\s*$", re.IGNORECASE)
_CHANGE_TEMPLATE_RE = re.compile(
    r"^\s*(?:change template to|switch template to|use)\s+(.+)$", re.IGNORECASE | re.DOTALL,
)
_PREVIEW_TRIGGERS = {"preview", "show preview", "preview message", "what will they receive"}
_SUMMARY_TRIGGERS = {"summary", "show recipients", "who will receive this"}

# Pagination (Sprint 2: Show Recipient List, 2026-08-09) — only meaningful
# once a campaign has more than _RECIPIENT_PAGE_SIZE recipients; harmless
# no-ops (a re-render identical to the current one) otherwise. State lives
# in collected["recipient_page"]/["recipient_show_all"] — never touches
# excluded_ids/included_ids, so paging can never lose an exclusion.
_NEXT_PAGE_TRIGGERS = {"next", "next page"}
_PREV_PAGE_TRIGGERS = {"previous", "previous page", "prev", "prev page"}
_SHOW_ALL_TRIGGERS = {"show all", "show all recipients"}
_SHOW_REMAINING_TRIGGERS = {"show remaining", "show remaining recipients"}
_PAGE_N_RE = re.compile(r"^\s*page\s+(\d+)\s*$", re.IGNORECASE)


def _strip_leading_phrase(text: str, triggers: List[str]) -> Optional[str]:
    """Same longest-match-first shape as _strip_leading_trigger, plus a
    tolerant leading "also"/"and"/"then" strip for natural multi-turn
    editing ("Also exclude Kripa")."""
    t = _LEADING_CONNECTOR_RE.sub("", text or "").strip()
    low = t.lower()
    best: Optional[str] = None
    for trig in triggers:
        tl = trig.lower()
        if low == tl or low.startswith(tl + " ") or low.startswith(tl + ":"):
            if best is None or len(tl) > len(best):
                best = tl
    if best is None:
        return None
    return t[len(best):].strip(" :")


# (2026-08-09, production routing bug fix) Every confirmation card this
# agent renders ends with "Reply / 1 Approve / 2 Edit / 3 Cancel" — its
# OWN stated instructions. But a bare "2"/"edit"/"change" is exactly what
# agents/parser.py's generic parse_confirmation_reply treats as the
# generic "edit" action (_CONFIRM_EDIT), which dispatcher.py resolves by
# moving the conversation to step="editing" — the platform's generic
# "Key = Value" field editor (agents/parser.parse_edit_instructions),
# which has zero knowledge of "Exclude 5"/"Remove 6,11"/pagination/etc.
# Once there, THIS hook (handle_confirming_reply) is never consulted
# again for the rest of the conversation — dispatcher.py only calls it
# while step=="confirming" — so a user who follows the card's own
# instructions gets permanently locked out of every recipient-editing
# command, stuck seeing "Tell me what to change. Example: Role = Casting
# Director" no matter what they type next. Confirmed via a full
# production-conversation repro (ambiguous template -> disambiguation ->
# confirmation card -> bare "2") and dispatcher.py's own new
# confirming_reply_fallthrough_to_generic_parser trace log.
#
# Fix: intercept the SAME bare "2"/"edit"/"change" set here, BEFORE it
# ever reaches parse_confirmation_reply, and redirect with a concrete
# example instead of letting dispatcher.py change the step out from under
# this hook. The conversation stays in "confirming" (this hook returning
# non-None never changes the step), so every command below keeps working
# on the very next reply. Approve ("1"/"approve"/"yes"/...) and Cancel
# ("3"/"cancel"/...) are deliberately NOT intercepted — those still fall
# through to the existing, unmodified generic handling exactly as before.
_BARE_EDIT_TOKENS = {"2", "edit", "change"}
_EDIT_REDIRECT_MESSAGE = (
    'To change something, just tell me directly — for example '
    '"Exclude 5", "Exclude Kripa", "Include 7", "Change template to Reminder", '
    'or "Preview". Reply 1 to Approve or 3 to Cancel.'
)


async def _handle_campaign_confirming_edit(text: str, collected: dict, ctx: ExecContext) -> Optional[str]:
    """The AgentDefinition.handle_confirming_reply hook — see its
    docstring in agents/models.py. Returns None for anything that isn't
    one of THIS agent's editing commands, letting dispatcher.py fall
    through to the existing, untouched approve ("1")/cancel ("3")
    handling (bare "2"/"edit"/"change" is handled directly below instead
    — see _BARE_EDIT_TOKENS' docstring)."""
    norm = (text or "").strip()
    low = norm.lower().rstrip("?.!")

    logger.info(
        "campaign_confirming_edit_entered phone=%s text=%r", ctx.sender_phone, text,
    )

    if low in _BARE_EDIT_TOKENS:
        logger.info(
            "campaign_confirming_edit_detected phone=%s command=bare_edit_redirect", ctx.sender_phone,
        )
        return _EDIT_REDIRECT_MESSAGE

    if low in _PREVIEW_TRIGGERS:
        logger.info("campaign_confirming_edit_detected phone=%s command=preview", ctx.sender_phone)
        return await _render_preview(collected)
    if low in _SUMMARY_TRIGGERS:
        logger.info("campaign_confirming_edit_detected phone=%s command=summary", ctx.sender_phone)
        return await _render_summary(collected)

    if low in _NEXT_PAGE_TRIGGERS:
        logger.info("campaign_confirming_edit_detected phone=%s command=next_page", ctx.sender_phone)
        return await _change_recipient_page(collected, ctx, delta=1)
    if low in _PREV_PAGE_TRIGGERS:
        logger.info("campaign_confirming_edit_detected phone=%s command=prev_page", ctx.sender_phone)
        return await _change_recipient_page(collected, ctx, delta=-1)
    page_m = _PAGE_N_RE.match(low)
    if page_m:
        logger.info("campaign_confirming_edit_detected phone=%s command=page_n page=%s", ctx.sender_phone, page_m.group(1))
        return await _set_recipient_page(collected, ctx, page=int(page_m.group(1)))
    if low in _SHOW_ALL_TRIGGERS:
        logger.info("campaign_confirming_edit_detected phone=%s command=show_all", ctx.sender_phone)
        return await _set_recipient_page(collected, ctx, page=1, show_all=True)
    if low in _SHOW_REMAINING_TRIGGERS:
        logger.info("campaign_confirming_edit_detected phone=%s command=show_remaining", ctx.sender_phone)
        return await _set_recipient_page(collected, ctx, page=int(collected.get("recipient_page") or "1"), show_remaining=True)

    excl_phrase = _strip_leading_phrase(norm, _EXCLUDE_TRIGGERS)
    if excl_phrase:
        logger.info("campaign_confirming_edit_detected phone=%s command=exclude phrase=%r", ctx.sender_phone, excl_phrase)
        return await _apply_recipient_edit(excl_phrase, collected, ctx, exclude=True)

    incl_phrase = _strip_leading_phrase(norm, _INCLUDE_TRIGGERS)
    if incl_phrase:
        logger.info("campaign_confirming_edit_detected phone=%s command=include phrase=%r", ctx.sender_phone, incl_phrase)
        return await _apply_recipient_edit(incl_phrase, collected, ctx, exclude=False)
    add_back_m = _ADD_BACK_RE.match(_LEADING_CONNECTOR_RE.sub("", norm))
    if add_back_m:
        logger.info("campaign_confirming_edit_detected phone=%s command=add_back phrase=%r", ctx.sender_phone, add_back_m.group(1))
        return await _apply_recipient_edit(add_back_m.group(1).strip(), collected, ctx, exclude=False)

    tmpl_m = _CHANGE_TEMPLATE_RE.match(norm)
    if tmpl_m:
        if collected.get("send_mode") in ("custom_message", "instagram"):
            # Changing the template doesn't apply to a literal custom
            # message or an Instagram-link send (both always use the one
            # seeded custom template) — without this gate, a stray "use
            # Reminder" could silently swap in a real template while
            # send_mode still points variable_data["message"] at the
            # literal text, corrupting the send.
            logger.info(
                "campaign_confirming_edit_detected phone=%s command=change_template_blocked send_mode=%s",
                ctx.sender_phone, collected.get("send_mode"),
            )
            return (
                "Changing the template doesn't apply to a custom message or "
                "Instagram send — cancel and resend to change what's being sent."
            )
        logger.info("campaign_confirming_edit_detected phone=%s command=change_template", ctx.sender_phone)
        return await _apply_template_change(tmpl_m.group(1).strip(" .!?"), collected, ctx)

    logger.info("campaign_confirming_edit_detected phone=%s command=none (falling through)", ctx.sender_phone)

    return None


SEND_REQUIREMENT_INTENT = IntentDefinition(
    intent_id="whatsapp_campaign.send_requirement",
    # Send/Share Semantic Router (Production fix, 2026-09-06) — no longer
    # directly triggerable at the top level; casting_pipeline.py's
    # SHARE_INTENT now owns every communication verb (send/share/forward/
    # deliver/message/broadcast/push/dispatch) and hands off to THIS
    # intent, unchanged, once content is classified as Instagram-sharing.
    # Still registered so registry.get_intent(agent, "whatsapp_campaign.
    # send_requirement") keeps resolving for that hand-off.
    triggers=[],
    fields=[
        SOURCE_QUERY_FIELD, RECIPIENT_QUERY_FIELD, STAGE_QUERY_FIELD, PROJECT_QUERY_FIELD,
        SEND_MODE_FIELD, AUTO_CONFIRM_FIELD, PLAN_FIELD, LEGACY_SYNTAX_FIELD,
    ],
    executor=_send_requirement_executor,
    extract_fields=extract_send_requirement_fields,
    build_confirmation=_build_send_requirement_confirmation,
    handle_confirming_reply=_handle_campaign_confirming_edit,
    try_auto_execute=_send_requirement_try_auto_execute,
    summary_title="You are about to send:",
)

UNAUTHORIZED_SENDER_MESSAGE = (
    "You're not authorized to use the WhatsApp Campaign Agent.\n\n"
    "Please contact an admin to be added."
)

# Static Help Command text (see parser.is_help_trigger / dispatcher.py).
# Hand-written, not generated — one intent (SEND_REQUIREMENT_INTENT) covers
# every trigger verb in SEND_TRIGGERS, so listing them all would just be
# noise; these examples cover the real recipient shapes the resolver
# actually supports (project/pipeline, named talent, phone number, CRM
# category, saved list) plus the real interactive-edit commands from
# _EDIT_REDIRECT_MESSAGE above. Update by hand if a new recipient shape or
# edit command is added.
# Talentgram Scouting Agent — master manual (Production fix, 2026-09-06,
# Consolidation + SEND/SHARE Semantic Model). Every command that used to
# be split across "Talentgram Casting Pipeline" and "Talentgram WhatsApp
# Agent" lives here now — this is the one place a Talentgram team member
# needs to remember. Every example is a real, tested command shape.
HELP_TEXT = (
    "TALENTGRAM SCOUTING AGENT\n"
    "QUICK MANUAL\n\n"
    "You can use this group for:\n\n"
    "1. CASTING PIPELINE — add/move talents\n"
    "2. SHARE — casting calls, templates, custom messages, Instagram\n"
    "3. SEND — audition/media files\n"
    "4. MEDIA — pull a talent's marked WhatsApp media into Talentgram\n"
    "5. SHOW / TESTED — look things up\n"
    "6. UNDO\n"
    "7. HELP\n\n"
    "Talk to it in plain English; the examples below are the reliable "
    "way to write each command.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "1. CASTING PIPELINE (ADD / MOVE)\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "ADD — adds talent(s) to a project's pipeline, at Ask To Test.\n\n"
    "Add Anusha Sharma to Hinge\n"
    "Add Anusha Sharma, Riya Sharma to Hinge\n"
    "Add Anusha Sharma to Hinge, L'Oreal\n\n"
    "MOVE — moves talent(s) already in a pipeline to a different stage "
    "(also understands shortlist, select, reject, hold, restore, not "
    "available, not interested, and every existing stage name).\n\n"
    "Move Anusha Sharma to Follow Up in Hinge\n\n"
    "ADD + MOVE, and ADD + MOVE + SHARE, in one message:\n\n"
    "Add Anusha Sharma to Hinge, move her to Follow Up\n"
    "Add Anusha Sharma to Hinge, move her to Follow Up, share the "
    "casting call with her\n\n"
    "IMPORTANT NOTES: only commas separate lists of talents/projects — "
    "a name that itself contains \"and\" is still treated as one. Before "
    "anything is added or moved, I show exactly what will happen and "
    "wait for your approval (reply 1, or \"and confirm\" to skip the "
    "approval step). Adding someone already in that pipeline reports it "
    "instead of creating a duplicate.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "2. SHARE\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "WHAT IT DOES: shares a saved template (like the casting call) or a "
    "custom message with one or more talents, or with everyone "
    "currently in a pipeline stage — for one project or several at "
    "once.\n\n"
    "HOW TO WRITE IT: Share <template or \"custom message\"> "
    "[for <project(s)>] to/with <talent(s) or a stage>. Comes together "
    "in whatever order reads naturally.\n\n"
    "Examples:\n\n"
    f"{SHARE_HELP_EXAMPLES}\n\n"
    "Also works with multiple projects and a custom message together:\n"
    "Share the template for Hinge, L'Oreal with Anusha Sharma, Riya Sharma\n\n"
    "To share a talent's own Instagram link instead of a template/"
    "message, see \"SHARE INSTAGRAM LINK\" below.\n\n"
    "IMPORTANT NOTES:\n"
    "• Commas separate multiple projects/talents — only a comma OUTSIDE "
    "a quoted message has that meaning.\n"
    "• Spacing is flexible and minor spelling mistakes are tolerated.\n"
    "• Everything between the opening and closing \" of a custom "
    "message is preserved exactly as written — commas, line breaks, "
    "hyphens, colons, asterisks, brackets, and every other character "
    "inside it are never treated as command structure.\n"
    "• An ambiguous talent or project name produces a numbered list "
    "to pick from — I never guess.\n"
    "• Pipeline-stage recipients are resolved and shown by name before "
    "you approve — never just \"everyone in Ask To Test\".\n"
    "• The confirmation shows the exact recipients and how each one "
    "will be reached (WhatsApp Group or Phone Number) before anything "
    "sends.\n"
    "• Before sending, I check that every named talent is actually in "
    "each named project's pipeline. If someone's missing, I show "
    "exactly which pairs and offer: 1) add them and move to Follow Up, "
    "then share, 2) share only where they're already in the pipeline, "
    "or 3) cancel.\n"
    "• Nothing is ever sent until you reply 1 to Approve. Reply 2 to "
    "Edit — say things like \"Remove 2\", \"Share only with 1,3\", "
    "\"Change project\", or \"Change message\", and I'll rebuild the "
    "preview and ask for approval again.\n"
    "• Once sent, I report back with the final result — how many "
    "actually went through successfully and which, if any, failed and "
    "why.\n"
    "• If your SHARE command is missing something or I can't quite "
    "parse it, I'll point you back to these same examples rather than "
    "just saying I didn't understand.\n\n"
    "SHARE INSTAGRAM LINK\n"
    "────────────────────\n\n"
    "WHAT IT DOES:\n"
    "Shares the Instagram link of one or more talents with one WhatsApp "
    "recipient.\n\n"
    "HOW TO WRITE IT:\n"
    "Share Instagram link of <talent(s)> to <recipient>\n\n"
    "Examples:\n\n"
    f"{SHARE_INSTAGRAM_HELP_EXAMPLES}\n\n"
    "Multiple talents are separated with commas.\n\n"
    "The recipient can be a WhatsApp name, WhatsApp group, "
    "project-associated WhatsApp group, or WhatsApp number.\n\n"
    "Nothing is sent until you approve the confirmation.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "3. SEND\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "Use SEND only when you want to forward audition/media files — "
    "templates and messages use SHARE instead.\n\n"
    "Send Anusha Sharma's audition video to Raj\n"
    "Send Anusha Sharma's audition material to the casting team\n\n"
    "IMPORTANT NOTES: ALWAYS shows the exact form first and needs an "
    "explicit approval — nothing is ever sent automatically. If it's "
    "genuinely unclear whether you mean a template/message or a media "
    "file, I'll ask which one rather than guessing.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "4. MEDIA (UPLOAD)\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "WHAT IT DOES: pulls a talent's @Gunwanti-marked WhatsApp media "
    "(takes/intro/photos) into their Talentgram submission, for the "
    "app's own review pages. Different from SEND — this never touches "
    "WhatsApp, and SEND never touches this.\n\n"
    "Upload Anusha Sharma's marked media for Hinge\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "5. SHOW / TESTED\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "SHOW — looks up ongoing projects, a project's pipeline, or which "
    "projects a talent is in.\n\n"
    "Show ongoing projects\n"
    "Show projects of Anusha Sharma\n"
    "Show the Follow Up pipeline of Hinge\n\n"
    "TESTED — checks a talent's current pipeline stage for a project "
    "(answers with the ACTUAL stage, not just yes/no).\n\n"
    "Tested Anusha Sharma for Hinge\n"
    "Has Anusha Sharma tested for Hinge\n\n"
    "Both run immediately — no approval needed, since they don't change "
    "anything.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "6. UNDO\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "Reverses the last pipeline move, within 5 minutes — including the "
    "move half of a combined Add+Move. Never undoes a SEND or SHARE — "
    "those are outbound WhatsApp messages, not pipeline changes.\n\n"
    "Undo\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "7. HELP\n\n"
    "WHAT IT DOES: shows this manual.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "MULTIPLE TALENTS / PROJECTS\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "Comma-separate multiple talents (Anusha Sharma,Riya Sharma) or "
    "multiple projects (Hinge,L'Oreal) — I understand them as lists. "
    "Naming both means every combination (never duplicated).\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "MULTIPLE COMMANDS IN ONE MESSAGE\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "ADD, MOVE, SHARE, and SEND can be chained in one message, or put "
    "each command on its own line — add \"and confirm\" once at the very "
    "end to skip the approval step on everything except SEND, which "
    "always keeps its own separate approval:\n\n"
    "Add Anusha Sharma to Hinge, move her to Follow Up, share the "
    "casting call with her and confirm\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "CONFIRMATION\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "Before anything changes or sends, I show exactly what will happen "
    "and wait:\n\n"
    "Reply:\n"
    "1 → Approve\n"
    "2 → Edit\n"
    "3 → Cancel\n\n"
    "On Edit, I'll ask specifically what you want to change about THAT "
    "pending action — no need to repeat the whole command.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "WHEN SOMETHING IS AMBIGUOUS\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "If a talent, project, or template name matches more than one real "
    "record, I'll show numbered options and ask — I never guess on a "
    "close match. Reply with the number and the original command "
    "continues, it doesn't need to be retyped.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "GENERAL\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "• Spaces around commas are ignored\n"
    "• Minor spelling mistakes are tolerated, including in command "
    "words (e.g. \"mover\" for \"move\")\n"
    "• Nothing is ever added, moved, or sent without your approval"
)

CAMPAIGN_AGENT = AgentDefinition(
    agent_id=AGENT_ID,
    name="WhatsApp Campaign Agent",
    module="whatsapp_campaign_agent",
    # SHARE_INTENT (Production fix, 2026-09-05) — the SAME IntentDefinition
    # object casting_pipeline.py builds and used to register on the
    # Casting Pipeline group; standalone SHARE now runs here instead. Its
    # own internal session_context/conversation calls already key off
    # ctx.agent_id (not a hardcoded constant), so reusing it unchanged
    # under THIS agent's id is correct, not merely convenient.
    intents=[
        SEND_REQUIREMENT_INTENT, SHARE_INTENT, SEND_INTENT,
        QUERY_INTENT, MOVE_INTENT, ADD_INTENT, UPLOAD_INTENT, UNDO_INTENT,
    ],
    # Talentgram Scouting Agent consolidation (Production fix, 2026-09-06)
    # — casting.send now runs exclusively through this agent (it's no
    # longer independently triggerable on casting-agent, which is
    # redirect-only), and casting.send relies on the Concurrent Task
    # Engine so an abandoned "confirming" SEND survives an unrelated
    # fresh command from the same phone instead of being silently
    # overwritten by the single conversation.py slot — the same
    # protection casting-agent always gave it. Purely additive per the
    # dispatcher's own design (a task record is created ALONGSIDE, never
    # instead of, the conversation.py record), so SHARE/ADD/MOVE/QUERY/
    # UPLOAD/UNDO's existing conversation-based flows on this agent are
    # unaffected.
    supports_concurrent_tasks=True,
    unauthorized_sender_message=UNAUTHORIZED_SENDER_MESSAGE,
    help_text=HELP_TEXT,
    # Talentgram Scouting Agent consolidation (Production fix, 2026-09-06)
    # — casting-agent's own bare-number/verb-less-query resolver, now
    # needed here since QUERY_INTENT itself moved. Already fixed to read
    # session state via ctx.agent_id rather than a hardcoded constant.
    resolve_bare_reply=_resolve_bare_reply,
)


def register() -> None:
    register_agent(CAMPAIGN_AGENT)
