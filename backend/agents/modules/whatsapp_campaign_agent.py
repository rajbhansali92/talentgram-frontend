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
PROJECT / MANUAL / SAVED_LISTS sources — the only new code is the free-text
routing logic that decides WHICH existing source_type+params a recipient
phrase means: a phone number, one or more named talents (reusing
casting-agent's own hardened talent-selector/fuzzy-match machinery), an
entire project's pipeline (reusing resolve_project_by_name, same as
before), or a saved contact/group list (new tiny matchers in
agents/modules/name_match.py, the SAME shared 4-tier shape templates
already used, not a new algorithm).

Attribution: every campaign launched via WhatsApp is created_by the single
seeded ADMIN_EMAIL account (there is no phone-to-user mapping in this
codebase) — the real WhatsApp sender is still fully traceable via
agents.audit.log_turn, which dispatcher.py already writes for every turn.

Ambiguity handling stays v1-simple: an unresolvable/ambiguous source or
recipient ends that attempt with a clear message and clears the pending
conversation, rather than opening a full disambiguation sub-conversation
(casting-agent's own pending_disambiguation continuation system took many
dedicated sprints to harden — replicating it here is still out of scope).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

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
from agents import conversation
from agents.modules import casting_pipeline_nlu as nlu
from agents.modules import name_match
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


def extract_send_requirement_fields(text: str) -> Dict[str, str]:
    _, remainder = nlu._strip_leading_trigger(text or "", SEND_TRIGGERS)
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
# ---------------------------------------------------------------------------
_SOURCE_FILLER_WORDS = {
    "requirement", "requirements", "brief", "briefs", "message", "template",
    "the", "this", "that", "content", "info", "information",
}


def _strip_source_filler(q: str) -> str:
    tokens = [t for t in q.split() if t.lower() not in _SOURCE_FILLER_WORDS]
    return " ".join(tokens).strip()


async def _resolve_source(source_query: str) -> TemplateMatch:
    q = (source_query or "").strip()
    if not q:
        return TemplateMatch(error="What should I send? (a project name or a template name)")
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


_PHONE_RE = re.compile(r"^[\d\s\+\-\(\)]{7,}$")


async def _resolve_recipient(recipient_query: str, stage_query: str) -> _RecipientTarget:
    q = (recipient_query or "").strip()
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
    if proj_match.project:
        stage_list = [stage_query] if stage_query else list(PIPELINE_STAGE_ORDER)
        stage_label = nlu.stage_label(stage_query) if stage_query else "All stages"
        return _RecipientTarget(
            ok=True, source_type="PROJECT",
            source_params=SourceParams(project_id=proj_match.project["id"], pipeline_stages=stage_list),
            display_label=f'{proj_match.project["label"]} — {stage_label}',
        )

    # Tier 3: one or more named talents — global pool (same candidate set
    # ADD_INTENT already searches; a talent being messaged directly needn't
    # be in any particular project's pipeline).
    selector = nlu.parse_talent_selector(q)
    if selector.ok and not selector.everyone and not selector.ordinals:
        candidates = await _fetch_all_talent_candidates()
        resolved = nlu.resolve_against_candidates(selector, candidates)
        if resolved.ok and resolved.talent_ids:
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
            options = "\n".join(f"- {c.label}" for c in resolved.ambiguous_candidates)
            return _RecipientTarget(
                ok=False,
                error=f'Multiple talents match "{q}":\n\n{options}\n\nPlease try again with the full name.',
            )

    # Tier 4: a saved contact list.
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

    # Tier 5: a saved WhatsApp group list.
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


async def _resolve_send_target(collected: Dict[str, str]) -> _SendTarget:
    source_query = (collected.get("source_query") or "").strip()
    recipient_query = (collected.get("recipient_query") or "").strip()
    stage_query = (collected.get("stage_query") or "").strip()

    tmpl_match = await _resolve_source(source_query)
    if tmpl_match.error:
        return _SendTarget(ok=False, error=tmpl_match.error)
    if tmpl_match.ambiguous:
        options = "\n".join(f"- {c['label']}" for c in tmpl_match.ambiguous)
        return _SendTarget(
            ok=False,
            error=f'Multiple templates match "{source_query}":\n\n{options}\n\nPlease try again with the exact template name.',
        )

    recipient = await _resolve_recipient(recipient_query, stage_query)
    if not recipient.ok:
        return _SendTarget(ok=False, error=recipient.error)

    return _SendTarget(
        ok=True, source_type=recipient.source_type, source_params=recipient.source_params,
        recipient_label=recipient.display_label, template=tmpl_match.template,
    )


def _truncate(s: str, limit: int = 300) -> str:
    s = s or ""
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


def _format_recipients_and_delivery(jobs: List[dict], skipped: List[dict]) -> "tuple[str, str]":
    """Builds the RECIPIENTS list ("✓ Name → destination") and DELIVERY
    summary line from create_batch's own dry-run job list — no separate
    recipient-formatting logic, this just renders what the real engine
    already resolved."""
    lines = []
    shown = jobs[:10]
    for j in shown:
        lines.append(f"✓ {j['talent_name']} → {j['destination']}")
    if len(jobs) > len(shown):
        lines.append(f"…and {len(jobs) - len(shown)} more")
    if skipped:
        lines.append(f"⚠ {len(skipped)} skipped (no phone/group on file)")
    recipients_block = "\n".join(lines) if lines else "(no recipients resolved)"

    groups = sum(1 for j in jobs if j.get("destination_type") == "group")
    numbers = sum(1 for j in jobs if j.get("destination_type") == "number")
    delivery_parts = []
    if groups:
        delivery_parts.append(f"{groups} WhatsApp Group{'s' if groups != 1 else ''}")
    if numbers:
        delivery_parts.append(f"{numbers} Phone Number{'s' if numbers != 1 else ''}")
    delivery = ", ".join(delivery_parts) if delivery_parts else "(none)"
    return recipients_block, delivery


async def _build_send_requirement_confirmation(collected: dict, ctx: ExecContext) -> str:
    target = await _resolve_send_target(collected)
    if not target.ok:
        # v1 scope (see module docstring): end this attempt cleanly rather
        # than opening a disambiguation sub-conversation.
        await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
        return target.error

    try:
        preview = await create_batch(
            BatchIn(
                source_type=target.source_type,
                source_params=target.source_params,
                template_id=target.template["id"],
                is_dry_run=True,
            ),
            admin=await _service_admin(),
        )
    except HTTPException as exc:
        await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
        return f"Couldn't prepare that: {exc.detail}"

    jobs = preview["jobs"]
    skipped = preview["skipped"]
    template_label = target.template.get("name") or target.template.get("slug") or ""
    recipients_block, delivery = _format_recipients_and_delivery(jobs, skipped)

    lines = [
        "ACTION",
        "Send Requirement",
        "",
        "MESSAGE SOURCE",
        template_label,
        "",
        "RECIPIENTS",
        recipients_block,
        "",
        "DELIVERY",
        delivery,
    ]
    if jobs:
        lines += ["", "Sample message", "", _truncate(jobs[0]["message_body"])]
    lines += ["", "Reply", "1 Approve", "2 Edit", "3 Cancel"]
    return "\n".join(lines)


async def _send_requirement_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    target = await _resolve_send_target(collected)
    if not target.ok:
        return ExecResult(ok=False, error="send_requirement_resolution_failed", message=target.error)

    try:
        result = await create_batch(
            BatchIn(
                source_type=target.source_type,
                source_params=target.source_params,
                template_id=target.template["id"],
                is_dry_run=False,
            ),
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


SEND_REQUIREMENT_INTENT = IntentDefinition(
    intent_id="whatsapp_campaign.send_requirement",
    triggers=SEND_TRIGGERS,
    fields=[SOURCE_QUERY_FIELD, RECIPIENT_QUERY_FIELD, STAGE_QUERY_FIELD],
    executor=_send_requirement_executor,
    extract_fields=extract_send_requirement_fields,
    build_confirmation=_build_send_requirement_confirmation,
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
