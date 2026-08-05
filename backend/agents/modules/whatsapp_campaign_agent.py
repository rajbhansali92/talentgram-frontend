"""WhatsApp Campaign Orchestration Agent — the third registered WhatsApp
agent. Translates a natural-language WhatsApp command into the SAME
`BatchIn` object the web app's campaign UI builds, and calls the EXISTING
compile-preview / launch function (`routers.whatsapp.create_batch`)
directly, in-process — zero duplicate recipient resolution, template
rendering, job queueing, or delivery tracking. `create_batch` already IS
both the preview and the launch call, distinguished only by
`is_dry_run` — there is no separate preview endpoint to reuse, so this
module calls the one function twice (dry-run for the confirmation card,
live on approval), exactly mirroring what the web app's own "Preview" then
"Send" buttons do.

Scoped to its own WhatsApp group ("Talentgram WhatsApp Agent"), completely
independent of casting-agent/crm-agent — see agents/__init__.py's
seed_agent_config call for the group binding.

v1 scope (per the approved plan): PROJECT recipient source only (via
casting_pipeline stage + project) — CRM/MANUAL/SAVED_LISTS sources are not
supported by this agent. Attribution: every campaign launched via WhatsApp
is created_by the single seeded ADMIN_EMAIL account (there is no phone-to-
user mapping in this codebase) — the real WhatsApp sender is still fully
traceable via agents.audit.log_turn, which dispatcher.py already writes
for every turn (sender_phone/sender_name/raw_message), cross-referenceable
to the resulting batch by the Batch ID included in both the confirmation
card and the launch success message.

Ambiguity handling is intentionally v1-simple: an unresolvable/ambiguous
project or template ends that attempt with a clear message and clears the
pending conversation, rather than opening a full disambiguation sub-
conversation (casting-agent's own pending_disambiguation continuation
system took many dedicated sprints to harden — replicating it here would
be exactly the kind of scope this agent is NOT meant to take on). The user
simply retries with a more specific command.

One intent:
  whatsapp_campaign.launch — "Send campaign to <project> [<stage>] using
  <template>". Always confirms first via build_confirmation (which itself
  is where the dry-run preview happens); only launches on explicit
  approval (1/2/3), via the standard confirm/edit/cancel gate the generic
  engine already provides.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from fastapi import HTTPException

from core import db, ADMIN_EMAIL

from routers.whatsapp import BatchIn, SourceParams, create_batch
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
from agents.modules.casting_pipeline import _fetch_ongoing_projects

AGENT_ID = "whatsapp-campaign-agent"

logger = logging.getLogger(__name__)

CAMPAIGN_TRIGGERS = [
    "send campaign", "launch campaign", "start campaign",
    "run campaign", "broadcast", "send broadcast",
]


# ---------------------------------------------------------------------------
# Template matching — no existing name->id resolver for templates exists in
# this codebase (only exact template_id lookups), so this small, local
# matcher IS the orchestration layer's own job: translating free text into
# an existing template_id, not a duplicate of the templates API or
# create_batch itself. Mirrors resolve_project_by_name's tier shape
# (exact -> normalized-exact -> substring -> fuzzy) but is fully
# independent — different collection, different fields, no shared state.
# ---------------------------------------------------------------------------
@dataclass
class TemplateMatch:
    template: Optional[Dict[str, str]] = None
    ambiguous: Optional[List[Dict[str, str]]] = None
    error: Optional[str] = None


_TEMPLATE_FUZZY_CUTOFF = 0.6
_TEMPLATE_AUTOCORRECT_CUTOFF = 0.8
_TEMPLATE_AMBIGUITY_MARGIN = 0.05
_TEMPLATE_PUNCT_RE = re.compile(r"[^a-z0-9\s]")


def _normalize_template_query(s: str) -> str:
    s = (s or "").strip().lower()
    s = _TEMPLATE_PUNCT_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def resolve_template_by_name(name_query: str, templates: List[Dict[str, str]]) -> TemplateMatch:
    q_raw = (name_query or "").strip()
    if not q_raw or not templates:
        return TemplateMatch(error=f'I couldn\'t find a template matching "{name_query}".')

    def _label(t: Dict[str, str]) -> str:
        return t.get("name") or t.get("slug") or ""

    labels = [_label(t) for t in templates]
    q_lower = q_raw.lower()

    def _as_dicts(idxs: List[int]) -> List[Dict[str, str]]:
        return [{"id": templates[i]["id"], "label": labels[i]} for i in idxs]

    # Tier 1: exact, case-insensitive.
    exact = [i for i, l in enumerate(labels) if l.strip().lower() == q_lower]
    if len(exact) == 1:
        return TemplateMatch(template=templates[exact[0]])
    if len(exact) > 1:
        return TemplateMatch(ambiguous=_as_dicts(exact[:8]))

    # Tier 2: normalized exact (case/punctuation/whitespace-insensitive).
    q_norm = _normalize_template_query(q_raw)
    norm_labels = [_normalize_template_query(l) for l in labels]
    nexact = [i for i, l in enumerate(norm_labels) if q_norm and l == q_norm]
    if len(nexact) == 1:
        return TemplateMatch(template=templates[nexact[0]])
    if len(nexact) > 1:
        return TemplateMatch(ambiguous=_as_dicts(nexact[:8]))

    # Tier 3: normalized substring (either direction).
    contains = [i for i, l in enumerate(norm_labels) if q_norm and (q_norm in l or l in q_norm)]
    if len(contains) == 1:
        return TemplateMatch(template=templates[contains[0]])
    if len(contains) > 1:
        return TemplateMatch(ambiguous=_as_dicts(contains[:8]))

    # Tier 4: character-level fuzzy, confident-and-unambiguous auto-resolve.
    scored = sorted(
        ((i, difflib.SequenceMatcher(None, q_norm, norm_labels[i]).ratio()) for i in range(len(labels))),
        key=lambda pair: pair[1], reverse=True,
    )
    scored = [(i, s) for i, s in scored if s >= _TEMPLATE_FUZZY_CUTOFF]
    if not scored:
        return TemplateMatch(error=f'I couldn\'t find a template matching "{name_query}".')
    top_i, top_s = scored[0]
    if len(scored) == 1:
        return TemplateMatch(template=templates[top_i])
    second_s = scored[1][1]
    if top_s >= _TEMPLATE_AUTOCORRECT_CUTOFF and (top_s - second_s) > _TEMPLATE_AMBIGUITY_MARGIN:
        return TemplateMatch(template=templates[top_i])
    close_enough = [i for i, s in scored if (top_s - s) <= _TEMPLATE_AMBIGUITY_MARGIN][:8]
    return TemplateMatch(ambiguous=_as_dicts(close_enough))


async def _fetch_templates() -> List[Dict[str, str]]:
    return await db.whatsapp_templates.find(
        {}, {"_id": 0, "id": 1, "name": 1, "slug": 1}
    ).sort("created_at", 1).to_list(200)


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
# Field extraction — "Send campaign to <project> [<stage>] using <template>
# [template]". v1-simple, fixed connector roles to stay unambiguous: stage
# is a bare vocabulary match (no connector word, removed first via the
# already-hardened extract_stage_phrase); template follows "using"/"with";
# project follows "to". A command that doesn't fit this shape just leaves
# that field unextracted — it falls through to the existing missing-field
# question flow (dispatcher.py), the same graceful degradation every other
# intent on this platform already has, not a hard failure.
# ---------------------------------------------------------------------------
_TEMPLATE_RE = re.compile(
    r"\b(?:using|with)\s+(?:the\s+)?(.+?)(?:\s+template\b)?\s*[.!?]*$",
    re.IGNORECASE | re.DOTALL,
)
_PROJECT_RE = re.compile(r"\bto\s+(.+)$", re.IGNORECASE | re.DOTALL)


def extract_campaign_fields(text: str) -> Dict[str, str]:
    _, remainder = nlu._strip_leading_trigger(text or "", CAMPAIGN_TRIGGERS)
    out: Dict[str, str] = {}

    # Template and project are extracted (and their matched spans removed)
    # FIRST, in that order — both anchored to the current end of the
    # remainder, mirroring casting_pipeline_nlu's own trailing-clause
    # technique (_PROJECT_IN_MOVE_RE). Stage is scanned LAST, only in
    # whatever text is left over — deliberately never over the project
    # name itself: extract_stage_phrase's shorthand table includes common
    # single words (e.g. "test" -> ask_to_test), so scanning the raw
    # project name risks a false positive whenever a real project's name
    # happens to contain one of those words (confirmed live: a project
    # named "...Test..." was misread as a stage-only command before this
    # ordering fix). A user who wants to name a stage explicitly puts it
    # BEFORE "to <project>", e.g. "Send campaign Approved to Toyota Glanza
    # using Diwali Promo" — that word ends up in the leftover text this
    # scans, never inside the captured project name.
    t_m = _TEMPLATE_RE.search(remainder)
    if t_m:
        template_query = t_m.group(1).strip(" .!?")
        if template_query:
            out["template_query"] = template_query
            remainder = remainder[:t_m.start()].strip()

    p_m = _PROJECT_RE.search(remainder)
    if p_m:
        project_candidate = p_m.group(1).strip(" .!?")
        if project_candidate:
            out["project_query"] = project_candidate
            remainder = remainder[:p_m.start()].strip()

    if remainder:
        stage_key, _ambig, _rest = nlu.extract_stage_phrase(remainder, PIPELINE_STAGE_ORDER)
        if stage_key:
            out["stage_query"] = stage_key

    return out


def _validate_query_text(raw: str) -> ValidationResult:
    v = (raw or "").strip()
    if not v:
        return ValidationResult(ok=False, error="I didn't catch that — please try again.")
    return ValidationResult(ok=True, value=v)


PROJECT_QUERY_FIELD = FieldSpec(
    key="project_query", label="Project",
    question="Which project should I send this campaign to?",
    validate=_validate_query_text, aliases=["project"],
)
TEMPLATE_QUERY_FIELD = FieldSpec(
    key="template_query", label="Template",
    question="Which template should I use?",
    validate=_validate_query_text, aliases=["template"],
)
# Optional — never blocks confirmation (next_missing_field skips
# required=False fields entirely). Absent means "every stage" (see
# _resolve_campaign_target). Kept as a real field anyway so the generic
# "Key = value" edit parser (parser.parse_edit_instructions) can match a
# "Pipeline = Approved" edit line against its label/aliases for free.
STAGE_QUERY_FIELD = FieldSpec(
    key="stage_query", label="Pipeline",
    question="Which pipeline stage?",
    validate=_validate_query_text, aliases=["stage", "pipeline"],
    required=False,
)


# ---------------------------------------------------------------------------
# Shared resolution — called from both the confirmation-card builder and
# the executor (re-resolved fresh at approval time, same pattern casting-
# agent's MOVE/ADD intents already use, rather than trusting whatever the
# card build resolved could still be valid moments later).
# ---------------------------------------------------------------------------
@dataclass
class _CampaignTarget:
    ok: bool
    project: Optional[Dict[str, str]] = None
    stage_list: Optional[List[str]] = None
    stage_label: Optional[str] = None
    template: Optional[Dict[str, str]] = None
    error: Optional[str] = None


async def _resolve_campaign_target(collected: Dict[str, str]) -> _CampaignTarget:
    project_query = (collected.get("project_query") or "").strip()
    template_query = (collected.get("template_query") or "").strip()
    stage_query = (collected.get("stage_query") or "").strip()

    projects = await _fetch_ongoing_projects()
    proj_match = nlu.resolve_project_by_name(project_query, projects)
    if proj_match.error:
        return _CampaignTarget(ok=False, error=proj_match.error)
    if proj_match.ambiguous:
        options = "\n".join(f"- {c['label']}" for c in proj_match.ambiguous)
        return _CampaignTarget(
            ok=False,
            error=f'Multiple projects match "{project_query}":\n\n{options}\n\nPlease try again with the exact project name.',
        )
    if proj_match.suggestions:
        options = "\n".join(f"- {c['label']}" for c in proj_match.suggestions)
        return _CampaignTarget(
            ok=False,
            error=f'I couldn\'t confidently match "{project_query}" to one project. Did you mean:\n\n{options}\n\nPlease try again with the exact project name.',
        )
    project = proj_match.project

    if stage_query:
        stage_list = [stage_query]
        stage_label = nlu.stage_label(stage_query)
    else:
        stage_list = list(PIPELINE_STAGE_ORDER)
        stage_label = "All stages"

    templates = await _fetch_templates()
    tmpl_match = resolve_template_by_name(template_query, templates)
    if tmpl_match.error:
        return _CampaignTarget(ok=False, error=tmpl_match.error)
    if tmpl_match.ambiguous:
        options = "\n".join(f"- {c['label']}" for c in tmpl_match.ambiguous)
        return _CampaignTarget(
            ok=False,
            error=f'Multiple templates match "{template_query}":\n\n{options}\n\nPlease try again with the exact template name.',
        )
    template = tmpl_match.template

    return _CampaignTarget(
        ok=True, project=project, stage_list=stage_list, stage_label=stage_label, template=template,
    )


def _truncate(s: str, limit: int = 300) -> str:
    s = s or ""
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


async def _build_campaign_confirmation(collected: dict, ctx: ExecContext) -> str:
    target = await _resolve_campaign_target(collected)
    if not target.ok:
        # v1 scope (see module docstring): end this attempt cleanly rather
        # than opening a disambiguation sub-conversation.
        await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
        return target.error

    try:
        preview = await create_batch(
            BatchIn(
                source_type="PROJECT",
                source_params=SourceParams(project_id=target.project["id"], pipeline_stages=target.stage_list),
                template_id=target.template["id"],
                is_dry_run=True,
            ),
            admin=await _service_admin(),
        )
    except HTTPException as exc:
        await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
        return f"Couldn't prepare that campaign: {exc.detail}"

    jobs = preview["jobs"]
    skipped = preview["skipped"]
    template_label = target.template.get("name") or target.template.get("slug") or ""

    lines = [
        "Campaign",
        template_label,
        "",
        "Project",
        target.project["label"],
        "",
        "Pipeline",
        target.stage_label,
        "",
        f"Recipients: {len(jobs)}",
    ]
    if skipped:
        lines.append(f"Skipped (unresolvable): {len(skipped)}")
    if jobs:
        lines += ["", "Sample message", "", _truncate(jobs[0]["message_body"])]
    lines += ["", "Reply:", "1 → Send", "2 → Edit", "3 → Cancel"]
    return "\n".join(lines)


async def _campaign_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    target = await _resolve_campaign_target(collected)
    if not target.ok:
        return ExecResult(ok=False, error="campaign_resolution_failed", message=target.error)

    try:
        result = await create_batch(
            BatchIn(
                source_type="PROJECT",
                source_params=SourceParams(project_id=target.project["id"], pipeline_stages=target.stage_list),
                template_id=target.template["id"],
                is_dry_run=False,
            ),
            admin=await _service_admin(),
        )
    except HTTPException as exc:
        return ExecResult(
            ok=False, error="campaign_launch_failed",
            message=f"Couldn't launch that campaign: {exc.detail}",
        )

    batch = result["batch"]
    queued = len(result["jobs"])
    template_label = target.template.get("name") or target.template.get("slug") or ""
    message = (
        "Campaign launched.\n\n"
        f"Project\n{target.project['label']}\n\n"
        f"Pipeline\n{target.stage_label}\n\n"
        f"Template\n{template_label}\n\n"
        f"Queued {queued} message(s) — delivery happens over the next few minutes.\n\n"
        f"Batch ID: {batch['id']}"
    )
    return ExecResult(ok=True, message=message, data={"batch_id": batch["id"], "queued": queued})


CAMPAIGN_INTENT = IntentDefinition(
    intent_id="whatsapp_campaign.launch",
    triggers=CAMPAIGN_TRIGGERS,
    fields=[PROJECT_QUERY_FIELD, TEMPLATE_QUERY_FIELD, STAGE_QUERY_FIELD],
    executor=_campaign_executor,
    extract_fields=extract_campaign_fields,
    build_confirmation=_build_campaign_confirmation,
    summary_title="You are about to launch:",
)

UNAUTHORIZED_SENDER_MESSAGE = (
    "You're not authorized to use the WhatsApp Campaign Agent.\n\n"
    "Please contact an admin to be added."
)

CAMPAIGN_AGENT = AgentDefinition(
    agent_id=AGENT_ID,
    name="WhatsApp Campaign Agent",
    module="whatsapp_campaign_agent",
    intents=[CAMPAIGN_INTENT],
    # Deliberately NOT opting into supports_concurrent_tasks — single-
    # conversation flow is sufficient for v1; this is an additive,
    # per-agent flag (see agents/tasks.py) that can be turned on later
    # with zero risk to any other agent's code.
    #
    # unauthorized_sender_message (2026-08-06): a launched campaign is a
    # real, mass-messaging action, so the allowlist itself stays fail-
    # closed exactly as before — this only replaces the silent
    # handled=False an unauthorized sender used to get with a clear
    # message, so a legitimate team member who hasn't been added yet
    # knows to ask, instead of the bot looking broken/unresponsive.
    unauthorized_sender_message=UNAUTHORIZED_SENDER_MESSAGE,
)


def register() -> None:
    register_agent(CAMPAIGN_AGENT)
