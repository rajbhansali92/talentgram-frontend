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
from dataclasses import dataclass
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
from agents.modules.casting_pipeline import _fetch_ongoing_projects, _fetch_all_talent_candidates

AGENT_ID = "whatsapp-campaign-agent"

logger = logging.getLogger(__name__)

# Action-verb synonyms — ANY of these opens the intent (broadened trigger
# gate, not a single literal phrase). The old compound phrases are kept as
# explicit aliases so "Send campaign to X using Y" still opens the same
# intent it always did — they're longer, so parser.detect_trigger's
# longest-match tie-break naturally still prefers them when present, though
# it no longer matters which one wins since both route here now.
SEND_VERBS = [
    "send", "share", "forward", "deliver", "message",
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
    return await db.whatsapp_templates.find(
        {}, {"_id": 0, "id": 1, "name": 1, "slug": 1}
    ).sort("created_at", 1).to_list(200)


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
    return stripped[len(trig):].lstrip(" :\n\t")


def extract_send_requirement_fields(text: str) -> Dict[str, str]:
    remainder = _strip_leading_trigger_preserve_newlines(text or "", SEND_TRIGGERS)
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
            else:
                out["recipient_query"] = recipient_part
        if source_part:
            out["source_query"] = source_part

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
# "Pipeline = Approved" edit line against its label/aliases.
STAGE_QUERY_FIELD = FieldSpec(
    key="stage_query", label="Pipeline",
    question="Which pipeline stage?",
    validate=_validate_query_text, aliases=["stage", "pipeline"],
    required=False,
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


_PHONE_RE = re.compile(r"^[\d\s\+\-\(\)]{7,}$")
_NAME_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _fuzzy_match_is_safe(query_fragment: str, matched_label: str) -> bool:
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
    overlaps does not)."""
    q_tokens = set(_NAME_TOKEN_RE.findall((query_fragment or "").lower()))
    l_tokens = set(_NAME_TOKEN_RE.findall((matched_label or "").lower()))
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
    stage_match = nlu.match_stage_phrase(stage_query, PIPELINE_STAGE_ORDER)
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
        contacts.append(ManualContact(name=d.get("name") or "", phone=phone, whatsapp_group_name=group_name))
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
    if proj_match.project and _fuzzy_match_is_safe(q, proj_match.project["label"]):
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
    # be in any particular project's pipeline).
    selector = nlu.parse_talent_selector(q)
    if selector.ok and not selector.everyone and not selector.ordinals:
        # Multi Manual Recipients (2026-08-09) — 2+ comma/newline/"and"/"&"
        # separated names is unambiguously a manual recipient LIST (never a
        # project/CRM/saved-list name, which never legitimately contains a
        # separator like this), so it's handled entirely here rather than
        # falling through to nlu.parse_talent_selector's own (ambiguity-
        # losing, see the section above) multi-name grammar. A single name
        # — the overwhelmingly common case — falls through to the existing,
        # byte-for-byte-unchanged single-name resolution below.
        fragments = _split_recipient_names(q)
        if len(fragments) >= 2:
            multi = await _resolve_multi_recipient_names(fragments)
            if multi.not_found or multi.unsafe:
                problems = list(multi.not_found) + [frag for frag, _label in multi.unsafe]
                joined = "\n".join(problems)
                return _RecipientTarget(ok=False, error=f"Couldn't find:\n\n{joined}")
            if multi.ambiguous:
                frag, amb_candidates, idx = multi.ambiguous
                return _RecipientTarget(
                    ok=False,
                    ambiguous=AmbiguousEntity(
                        entity_type="talent", field_key=_PENDING_MULTI_RECIPIENT_PICK_KEY,
                        candidates=[disambiguation.Candidate(id=c.id, label=c.label) for c in amb_candidates],
                        extra_collected={
                            _PENDING_MULTI_RECIPIENT_FRAGMENTS_KEY: json.dumps(fragments),
                            _PENDING_MULTI_RECIPIENT_INDEX_KEY: str(idx),
                        },
                    ),
                )
            if not multi.resolved:
                return _RecipientTarget(ok=False, error="Who should this go to?")
            contacts, no_contact_info = await _build_manual_contacts(multi.resolved)
            if not contacts:
                return _RecipientTarget(
                    ok=False,
                    error=f'{" and ".join(no_contact_info)} — no phone number or WhatsApp group on file.',
                )
            labels = [label for _tid, label in multi.resolved]
            return _RecipientTarget(
                ok=True, source_type="MANUAL",
                source_params=SourceParams(contacts=contacts),
                display_label=", ".join(labels),
            )

        candidates = await _fetch_all_talent_candidates()
        resolved = nlu.resolve_against_candidates(selector, candidates)
        if resolved.ok and resolved.talent_ids:
            # Safety gate (see _fuzzy_match_is_safe docstring) — every
            # resolved label must genuinely contain what the user typed,
            # not just share one word (e.g. a surname) with it.
            fragments = nlu.split_multi_names(q)
            unsafe = [
                label for label in resolved.talent_labels
                if not any(_fuzzy_match_is_safe(frag, label) for frag in fragments)
            ]
            if unsafe:
                return _RecipientTarget(
                    ok=False,
                    error=(
                        f'"{q}" matched "{", ".join(unsafe)}" but that name doesn\'t look like '
                        f"a real match — please use the exact full name to avoid sending to the "
                        f"wrong person."
                    ),
                )
            docs = await db.talents.find(
                {"id": {"$in": resolved.talent_ids}},
                {"_id": 0, "id": 1, "name": 1, "phone": 1, "whatsapp_group_name": 1},
            ).to_list(len(resolved.talent_ids))
            tmap = {d["id"]: d for d in docs}
            contacts: List[ManualContact] = []
            for tid in resolved.talent_ids:
                d = tmap.get(tid)
                if not d:
                    continue
                group_name = (d.get("whatsapp_group_name") or "").strip()
                phone = (d.get("phone") or "").strip()
                if not group_name and not phone:
                    continue
                contacts.append(ManualContact(name=d.get("name") or "", phone=phone, whatsapp_group_name=group_name))
            if contacts:
                return _RecipientTarget(
                    ok=True, source_type="MANUAL",
                    source_params=SourceParams(contacts=contacts),
                    display_label=", ".join(resolved.talent_labels),
                )
            return _RecipientTarget(
                ok=False,
                error=f'{" and ".join(resolved.talent_labels)} — no phone number or WhatsApp group on file.',
            )
        if resolved.ambiguous_candidates:
            return _RecipientTarget(
                ok=False,
                ambiguous=AmbiguousEntity(
                    entity_type="talent", field_key="recipient_query",
                    candidates=[
                        disambiguation.Candidate(id=c.id, label=c.label)
                        for c in resolved.ambiguous_candidates
                    ],
                ),
            )

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


async def _resolve_send_target(collected: Dict[str, str]) -> _SendTarget:
    source_query = (collected.get("source_query") or "").strip()
    recipient_query = (collected.get("recipient_query") or "").strip()
    stage_query = (collected.get("stage_query") or "").strip()

    tmpl_match = await _resolve_source(source_query)
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
    recipient = await _resolve_recipient(recipient_query, stage_query)
    if not recipient.ok:
        if recipient.ambiguous:
            return _SendTarget(ok=False, ambiguous=recipient.ambiguous)
        return _SendTarget(ok=False, error=recipient.error)

    return _SendTarget(
        ok=True, source_type=recipient.source_type, source_params=recipient.source_params,
        recipient_label=recipient.display_label, template=tmpl_match.template,
        project_label=recipient.project_label, pipeline_stage_label=recipient.pipeline_stage_label,
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
    Editing needs zero new filtering logic of its own."""
    return BatchIn(
        source_type=target.source_type, source_params=target.source_params,
        excluded_recipient_ids=list(collected.get("excluded_ids") or []),
        template_id=target.template["id"], is_dry_run=is_dry_run,
    )


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


async def _build_send_requirement_confirmation(collected: dict, ctx: ExecContext) -> str:
    collected = await _resume_pending_recipient_edit(collected, ctx)
    collected = await _resume_pending_multi_recipient(collected, ctx)
    target = await _resolve_send_target(collected)
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
        preview = await create_batch(
            await _build_batch_in(target, collected, is_dry_run=True),
            admin=await _service_admin(),
        )
    except HTTPException as exc:
        await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
        return f"Couldn't prepare that: {exc.detail}"

    jobs = preview["jobs"]
    skipped = preview["skipped"]
    template_label = target.template.get("name") or target.template.get("slug") or ""

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
            "Template", template_label, "",
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
        if jobs:
            lines += ["", "Sample message", "", _truncate(jobs[0]["message_body"])]
        lines += ["", "Reply", "1 Approve", "2 Edit", "3 Cancel"]
        return "\n".join(lines)

    delivery = _delivery_summary(jobs)

    lines = [
        "ACTION",
        "Send Requirement",
        "",
        "MESSAGE SOURCE",
        template_label,
        "",
        f"RECIPIENTS ({total_active})",
        "",
    ]
    lines += recipient_lines
    lines += ["", "DELIVERY", delivery]
    lines += edit_lines
    if skipped:
        lines += ["", f"⚠ {len(skipped)} skipped (no phone/group on file)"]
    if jobs:
        lines += ["", "Sample message", "", _truncate(jobs[0]["message_body"])]
    lines += ["", "Reply", "1 Approve", "2 Edit", "3 Cancel"]
    return "\n".join(lines)


async def _send_requirement_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    target = await _resolve_send_target(collected)
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
        result = await create_batch(
            await _build_batch_in(target, collected, is_dry_run=False),
            admin=await _service_admin(),
        )
    except HTTPException as exc:
        return ExecResult(
            ok=False, error="send_requirement_launch_failed",
            message=f"Couldn't send that: {exc.detail}",
        )

    batch = result["batch"]
    queued = len(result["jobs"])
    template_label = target.template.get("name") or target.template.get("slug") or ""
    message = (
        "Sent.\n\n"
        f"Message Source\n{template_label}\n\n"
        f"Recipients\n{target.recipient_label}\n\n"
        f"Queued {queued} message(s) — delivery happens over the next few minutes.\n\n"
        f"Batch ID: {batch['id']}"
    )
    return ExecResult(ok=True, message=message, data={"batch_id": batch["id"], "queued": queued})


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
        preview = await create_batch(
            await _build_batch_in(target, lookup_collected, is_dry_run=True), admin=await _service_admin(),
        )
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
        logger.info("campaign_confirming_edit_detected phone=%s command=change_template", ctx.sender_phone)
        return await _apply_template_change(tmpl_m.group(1).strip(" .!?"), collected, ctx)

    logger.info("campaign_confirming_edit_detected phone=%s command=none (falling through)", ctx.sender_phone)

    return None


SEND_REQUIREMENT_INTENT = IntentDefinition(
    intent_id="whatsapp_campaign.send_requirement",
    triggers=SEND_TRIGGERS,
    fields=[SOURCE_QUERY_FIELD, RECIPIENT_QUERY_FIELD, STAGE_QUERY_FIELD],
    executor=_send_requirement_executor,
    extract_fields=extract_send_requirement_fields,
    build_confirmation=_build_send_requirement_confirmation,
    handle_confirming_reply=_handle_campaign_confirming_edit,
    summary_title="You are about to send:",
)

UNAUTHORIZED_SENDER_MESSAGE = (
    "You're not authorized to use the WhatsApp Campaign Agent.\n\n"
    "Please contact an admin to be added."
)

CAMPAIGN_AGENT = AgentDefinition(
    agent_id=AGENT_ID,
    name="WhatsApp Campaign Agent",
    module="whatsapp_campaign_agent",
    intents=[SEND_REQUIREMENT_INTENT],
    # Deliberately NOT opting into supports_concurrent_tasks — single-
    # conversation flow is sufficient for v1; this is an additive,
    # per-agent flag that can be turned on later with zero risk to any
    # other agent's code.
    unauthorized_sender_message=UNAUTHORIZED_SENDER_MESSAGE,
)


def register() -> None:
    register_agent(CAMPAIGN_AGENT)
