"""Talentgram Casting Pipeline Agent — the second registered WhatsApp
agent, following crm.py's exact shape (field validators + executor(s) +
one register() call). Scoped to exactly one backend module: the Casting
Pipeline (backend/routers/casting_pipeline.py, db.casting_pipeline +
db.projects). This file owns all DB access for the agent; the pure
parsing/matching logic lives in casting_pipeline_nlu.py so it stays
independently testable.

Two intents:
  casting.query  — every read-only ask (project list, project detail,
                    pipeline listing/counts, plus Conversational Talent
                    Search — roster search by gender/category/city/age/
                    height, reusing routers.talents._build_talent_query,
                    the same filter engine Global Talent's page uses).
                    auto_confirm=True: nothing to approve, replies
                    immediately.
  casting.move   — every pipeline mutation (move/mark/approve/reject/...).
                    Always confirms first via a custom `build_confirmation`
                    hook (the generic confirmation.py can't resolve a raw
                    talent selector into real names — see agents/models.py
                    and agents/dispatcher.py for why that hook exists).

Stage vocabulary is never hardcoded here — PIPELINE_STAGE_ORDER is
imported live from routers/casting_pipeline.py (the app's one real source
of truth), so a stage added there later works in this agent immediately.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from core import db, parse_height_to_inches, _submission_to_client_shape

from routers.casting_pipeline import (
    LEGACY_STAGE_ALIASES,
    PIPELINE_STAGE_ORDER,
    PIPELINE_STAGES,
    _normalise_stage,
    add_talents_to_pipeline,
    bulk_move_by_talent_ids,
    get_stage_counts,
)

from routers.talents import _build_talent_query, _LIST_PROJECTION, _enrich_list

# Combined Casting Pipeline + WhatsApp Automation (2026-08-19) — the
# "add,move,send" family reuses the WhatsApp Engine's OWN, unmodified
# batch-creation/rendering path (create_batch already renders {{project_
# name}}/{{shoot_dates}}/{{budget}}/{{submission_link}} etc. correctly for
# a source_type="PROJECT" send — see routers/whatsapp.py). Safe to import
# at module level: routers/whatsapp.py has no dependency back on this
# module. The template-name resolver (agents.modules.whatsapp_campaign_
# agent._resolve_source) is NOT imported here at module level, though —
# that module already imports FROM this one (_fetch_ongoing_projects etc.),
# so importing it back here would be circular; it's imported locally,
# inside the one function that needs it, instead (see _flush_group_send).
from routers.whatsapp import BatchIn, SourceParams, create_batch

from agents.models import (
    AgentDefinition,
    ExecContext,
    ExecResult,
    FieldSpec,
    IntentDefinition,
    ValidationResult,
)
from agents.registry import register_agent
from agents import conversation, request_scope, session_context, undo_store
from agents.parser import parse_confirmation_reply, parse_edit_instructions
from agents.modules import casting_pipeline_nlu as nlu
from agents.modules import media_assignment
from agents.modules import media_send

AGENT_ID = "casting-agent"
UNDO_WINDOW_MINUTES = 5

logger = logging.getLogger(__name__)

# Placeholder value for a normally-required field (talent_selector,
# target_stage, project_query) when this turn is actually a multi-action
# plan (see collected["_plan"]) — exists purely so the generic engine's
# next_missing_field check sees "something provided" and doesn't ask a
# question the plan path will never read the answer to.
_PLAN_PLACEHOLDER = "__plan__"


def _validate_hidden(raw: str) -> ValidationResult:
    """Trivial always-ok validator for the internal, never-user-facing
    fields below (_auto_confirm, _plan) — they carry raw signal/data
    between extract_fields and build_confirmation/executor/
    try_auto_execute, never a value a user typed or edits directly."""
    return ValidationResult(ok=True, value=raw)


AUTO_CONFIRM_FIELD = FieldSpec(
    key="_auto_confirm", label="AutoConfirm", question="",
    validate=_validate_hidden, required=False,
)

PLAN_FIELD = FieldSpec(
    key="_plan", label="Plan", question="",
    validate=_validate_hidden, required=False,
)


def _log_talent_resolve_timing(resolved: "nlu.ResolvedTalents") -> None:
    """Folds nlu.resolve_against_candidates's local sub-stage timing dict
    (normalize/exact_match/token_match/fuzzy_scoring/ranking) into the
    fine-grained op log under the "talent_lookup" stage bucket, and logs
    the candidate count directly — so the Talent Lookup investigation's
    per-substage breakdown shows up in the real op trace, not just the
    coarse total."""
    timing = getattr(resolved, "timing", None) or {}
    if not timing:
        return
    for key, value in timing.items():
        if key == "candidate_count":
            continue
        request_scope.record(f"talent_match_{key}", "talent_lookup", elapsed_ms=value)
    logger.info(
        "talent_lookup_detail request_id=%s candidate_count=%d timing=%s",
        request_scope.get_request_id(), int(timing.get("candidate_count", 0)), timing,
    )


# ---------------------------------------------------------------------------
# DB helpers — the only place in this module that touches Mongo directly.
# ---------------------------------------------------------------------------
async def _timed_project_lookup(awaitable, collection: str = "projects", name: str = "project_lookup"):
    """Wraps a single Mongo read (or an asyncio.gather of several) in the
    request_scope "project_lookup" timing bucket AND the fine-grained op
    log (Mongo Summary) — one call-site-level wrap instead of editing
    every helper function's body, so latency instrumentation stays
    low-risk to add and easy to audit. Reserved for reads that resolve
    project IDENTITY (which project(s) exist/match), as distinct from
    `_timed_talent_lookup`'s pipeline-membership reads — the split the
    latency investigation asked for instead of one catch-all "mongo"
    bucket. `collection`/`name` are overridable per call site so the
    Mongo Summary shows the REAL collection touched, not always
    "projects" (e.g. `_fetch_last_updated` actually reads
    casting_pipeline)."""
    with request_scope.op(name, "project_lookup", collection=collection, cache="miss"):
        return await awaitable


async def _timed_talent_lookup(awaitable, collection: str = "talents", name: str = "talent_lookup"):
    """Same as `_timed_project_lookup`, for reads that resolve TALENT
    identity/candidates or pipeline-membership rows (casting_pipeline
    rows, talent name hydration)."""
    with request_scope.op(name, "talent_lookup", collection=collection, cache="miss"):
        return await awaitable


async def _timed_write(awaitable, collection: str = "casting_pipeline", name: str = "db_write"):
    with request_scope.op(name, "db_write", collection=collection, cache=None):
        return await awaitable


async def _timed_aggregation(awaitable, collection: str = "casting_pipeline", name: str = "aggregation"):
    with request_scope.op(name, "aggregation", collection=collection, cache="miss"):
        return await awaitable


_PROJECTS_CACHE_KEY = ("ongoing_projects",)


async def _fetch_ongoing_projects() -> List[Dict[str, str]]:
    # Per-turn only (request_scope resets every dispatch) — safe: never
    # serves data from a previous message, only avoids a genuinely
    # redundant re-fetch when more than one resolution branch needs the
    # live project list in the SAME turn (e.g. the "search everywhere?"
    # retry path after an initial project-scoped lookup).
    found, cached = request_scope.cache_get(_PROJECTS_CACHE_KEY)
    if found:
        with request_scope.op("fetch_ongoing_projects", "project_lookup", collection="projects", cache="hit"):
            pass
        return cached
    cursor = db.projects.find(
        {"status": "ongoing"}, {"_id": 0, "id": 1, "brand_name": 1}
    ).sort("brand_name", 1)
    docs = await _timed_project_lookup(cursor.to_list(2000), name="fetch_ongoing_projects")
    projects = [{"id": d["id"], "label": d.get("brand_name") or "(untitled project)"} for d in docs]
    request_scope.cache_set(_PROJECTS_CACHE_KEY, projects)
    return projects


_AND_OR_COMMA_RE = re.compile(r",|\band\b", re.IGNORECASE)


async def _resolve_project_query_names(project_query: Optional[str]) -> List[str]:
    """Returns the project name fragment(s) `project_query` actually refers
    to — the one place ADD/MOVE (compound-plan segments) and SHARE decide
    whether a project reference names ONE project or SEVERAL.

    nlu.split_multi_names blindly treats every bare "and"/comma in the text
    as a list separator — correct for "Toyota Glanza and Nykaa" (two real,
    distinct projects), but wrong whenever a SINGLE project's own name
    happens to contain "and" ("Vaseline (Film 1 and Film 4)"): it cuts that
    one name into two meaningless fragments which then each independently
    fuzzy-resolve straight back to the SAME real project, producing a
    duplicate ADD/MOVE/SHARE step referencing the identical project twice
    (2026-08-29 compound-command regression — a talent added/shared twice
    from one instruction typed once).

    Resolved generically, with no project name ever hardcoded, by
    preferring the real project catalog over blind text-splitting: if the
    WHOLE, unsplit query already resolves unambiguously to exactly one
    real ongoing project, that's authoritative — split fragments could
    only ever produce a WORSE (ambiguous, duplicate, or wrong) answer than
    the one the full text already gives, so it's returned untouched as a
    single-item list. Splitting into several names is used only as the
    fallback, when the whole string does NOT resolve to an existing
    project — the actual "these are two different named things" case
    ("Toyota Glanza and Nykaa" doesn't itself name any one real project,
    so it falls through to being split into ["Toyota Glanza", "Nykaa"]
    exactly as before). The DB round-trip only ever runs when the text
    contains an "and"/comma at all — the overwhelmingly common single-
    project, no-separator case never pays for it."""
    text = (project_query or "").strip()
    if not text or not _AND_OR_COMMA_RE.search(text):
        return [text] if text else [None]
    projects_list = await _fetch_ongoing_projects()
    whole_match = nlu.resolve_project_by_name(text, projects_list)
    if whole_match.project is not None:
        return [text]
    return nlu.split_multi_names(text) or [text]


async def _project_exists(project_id: str) -> bool:
    cache_key = ("project_exists", project_id)
    found, cached = request_scope.cache_get(cache_key)
    if found:
        with request_scope.op("project_exists", "project_lookup", collection="projects", cache="hit"):
            pass
        return cached
    result = bool(await _timed_project_lookup(
        db.projects.find_one({"id": project_id}, {"_id": 0, "id": 1}), name="project_exists"
    ))
    request_scope.cache_set(cache_key, result)
    return result


async def _project_exists_batch(project_ids: List[str]) -> None:
    """Batch-checks existence for several project ids in ONE $in query and
    seeds the per-turn cache for each — so individual `_project_exists`
    calls that follow (one per cross-product pair, potentially against
    several DIFFERENT projects) become cache hits instead of one query
    each. Only called from the plan engine's multi-project pre-fetch
    (_resolve_one_plan_step); the single-action path never needs it (it
    only ever resolves one project)."""
    unique_ids = list(dict.fromkeys(pid for pid in project_ids if pid))
    if not unique_ids:
        return
    rows = await _timed_project_lookup(
        db.projects.find({"id": {"$in": unique_ids}}, {"_id": 0, "id": 1}).to_list(len(unique_ids)),
        name="project_exists_batch",
    )
    found_ids = {r["id"] for r in rows}
    for pid in unique_ids:
        request_scope.cache_set(("project_exists", pid), pid in found_ids)


async def _fetch_last_updated(project_id: str) -> Optional[Any]:
    """Most recent `updated_at` across the project's pipeline rows — when
    the pipeline itself was last touched, not when the project doc was
    created. `casting_pipeline` rows store this via core._now(), which is
    an ISO string, not a native datetime — ISO-8601's fixed-width format
    still sorts lexicographically == chronologically, so the Mongo sort
    below is correct regardless; _format_last_updated is what handles the
    str-vs-datetime distinction on the way out."""
    rows = await _timed_project_lookup(
        db.casting_pipeline.find(
            {"project_id": project_id}, {"_id": 0, "updated_at": 1}
        ).sort("updated_at", -1).limit(1).to_list(1),
        collection="casting_pipeline", name="fetch_last_updated",
    )
    return rows[0]["updated_at"] if rows else None


def _format_last_updated(raw: Optional[Any]) -> str:
    if not raw:
        return "—"
    dt = raw
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    time_str = dt.strftime("%I:%M %p").lstrip("0")
    if dt.date() == now.date():
        return f"Today {time_str}"
    if dt.date() == (now - timedelta(days=1)).date():
        return f"Yesterday {time_str}"
    return dt.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")


def _stage_query_values(stage: str) -> List[str]:
    """Canonical stage key -> every raw value that should match it,
    folding in legacy aliases (e.g. querying "approved" must also match
    rows still stored as the deprecated "sent")."""
    values = [stage]
    values.extend(legacy for legacy, target in LEGACY_STAGE_ALIASES.items() if target == stage)
    return values


async def _hydrate_names(talent_ids: List[str]) -> Dict[str, str]:
    if not talent_ids:
        return {}
    with request_scope.op("hydrate_names", "talent_lookup", collection="talents", cache="miss"):
        cursor = db.talents.find({"id": {"$in": talent_ids}}, {"_id": 0, "id": 1, "name": 1})
        return {t["id"]: (t.get("name") or "Unknown") async for t in cursor}


async def _fetch_stage_candidates(project_id: str, stage: str) -> List[nlu.Candidate]:
    rows = await _timed_talent_lookup(
        db.casting_pipeline.find(
            {"project_id": project_id, "stage": {"$in": _stage_query_values(stage)}},
            {"_id": 0, "talent_id": 1},
        ).sort("created_at", 1).to_list(5000),
        collection="casting_pipeline", name="fetch_stage_candidates",
    )
    talent_ids = [r["talent_id"] for r in rows if r.get("talent_id")]
    names = await _hydrate_names(talent_ids)
    return [nlu.Candidate(id=tid, label=names.get(tid, "Unknown"), stage=stage) for tid in talent_ids]


async def _fetch_project_candidates(project_id: str, project_label: str = "") -> List[nlu.Candidate]:
    """Every pipeline row for the project, any stage — used for name-based
    move resolution, which isn't scoped to whatever stage was last shown.
    project_label is passed in (the caller already has it) purely for
    display in a disambiguation list — no extra DB round trip to fetch it
    again."""
    rows = await _timed_talent_lookup(
        db.casting_pipeline.find(
            {"project_id": project_id}, {"_id": 0, "talent_id": 1, "stage": 1}
        ).sort("created_at", 1).to_list(5000),
        collection="casting_pipeline", name="fetch_project_candidates",
    )
    talent_ids = [r["talent_id"] for r in rows if r.get("talent_id")]
    names = await _hydrate_names(talent_ids)
    out: List[nlu.Candidate] = []
    for r in rows:
        tid = r.get("talent_id")
        if not tid:
            continue
        stage = _normalise_stage(r.get("stage")) or r.get("stage")
        out.append(nlu.Candidate(
            id=tid, label=names.get(tid, "Unknown"), stage=stage,
            project_id=project_id, project_label=project_label,
        ))
    return out


async def _fetch_global_candidates() -> List[nlu.Candidate]:
    """Every pipeline row across every ONGOING project — the fallback used
    only when a move names a talent but no project at all (neither
    explicit in the message nor active in stored session context). Scoped
    to ongoing projects, same as every other "which projects are in play"
    read in this module. Each candidate carries its project_id/label so an
    ambiguous match can be reported grouped by project rather than as an
    unhelpfully identical-looking flat list."""
    projects = await _fetch_ongoing_projects()
    if not projects:
        return []
    project_ids = [p["id"] for p in projects]
    project_label_by_id = {p["id"]: p["label"] for p in projects}
    rows = await _timed_talent_lookup(
        collection="casting_pipeline", name="fetch_global_candidates",
        awaitable=db.casting_pipeline.find(
            {"project_id": {"$in": project_ids}},
            {"_id": 0, "talent_id": 1, "stage": 1, "project_id": 1},
        ).sort("created_at", 1).to_list(20000)
    )
    talent_ids = list({r["talent_id"] for r in rows if r.get("talent_id")})
    names = await _hydrate_names(talent_ids)
    out: List[nlu.Candidate] = []
    for r in rows:
        tid = r.get("talent_id")
        if not tid:
            continue
        pid = r.get("project_id")
        stage = _normalise_stage(r.get("stage")) or r.get("stage")
        out.append(nlu.Candidate(
            id=tid, label=names.get(tid, "Unknown"), stage=stage,
            project_id=pid, project_label=project_label_by_id.get(pid),
        ))
    return out


# ---------------------------------------------------------------------------
# casting.query — list projects / project detail / pipeline listing+counts
# ---------------------------------------------------------------------------
def _validate_query_text(raw: str) -> ValidationResult:
    text = (raw or "").strip()
    if not text:
        return ValidationResult(ok=False, error="I didn't catch that.")
    return ValidationResult(ok=True, value=text)


QUERY_TEXT_FIELD = FieldSpec(
    key="query_text",
    label="Query",
    question="What would you like to know?",
    validate=_validate_query_text,
)


def _extract_query_fields(text: str) -> Dict[str, str]:
    # No sub-parsing here — the executor classifies the full raw text
    # itself (stage names are dynamic, so this can't be a fixed set of
    # per-field patterns the generic engine could pre-split for us).
    return {"query_text": text}


async def _resolve_project_ref(
    ordinal: Optional[int], name_query: Optional[str], session: Optional[dict]
) -> Tuple[Optional[Dict[str, str]], Optional[str], Optional[List[str]]]:
    """Resolve which project a message is about. Precedence, strictly:

      1. An explicit project NAME in this message ("... for Google - Film
         1 & 3") — always wins, even mid-conversation with a different
         project already active. If a name was given but doesn't resolve
         to anything, that's an error, NOT a silent fall-through to the
         next tier — the whole point is an explicit reference must never
         be quietly ignored in favour of stale context.
      2. An explicit project NUMBER in this message ("Project 3"),
         resolved against the session's last-shown project listing.
      3. The session's stored current project, only when the message
         named nothing explicit at all.

    Returns (project, error_message, ambiguous_labels) — ambiguous_labels
    is set only when error_message is itself a "which one?" clarification
    the caller should let the user resolve with a short, stateful reply
    (see _query_parse_edits_async) instead of repeating the whole command.
    """
    if name_query:
        projects = await _fetch_ongoing_projects()
        with request_scope.stage("fuzzy"):
            match = nlu.resolve_project_by_name(name_query, projects)
        if match.project:
            return match.project, None, None
        if match.ambiguous:
            labels = [o["label"] for o in match.ambiguous]
            numbered = "\n".join(f"- {label}" for label in labels)
            return None, f"Which project did you mean?\n{numbered}", labels
        if match.suggestions:
            bullets = "\n".join(f"• {o['label']}" for o in match.suggestions)
            return None, (
                f"I couldn't find a project matching:\n\n{name_query}\n\nDid you mean:\n\n{bullets}"
            ), None
        return None, match.error or f'I couldn\'t find a project matching "{name_query}".', None

    if ordinal is not None:
        number_map = (session or {}).get("number_map") or {}
        if number_map.get("type") != "projects":
            return None, 'I don\'t have a project list open. Send "Show ongoing projects" first.', None
        items = number_map.get("items") or []
        match = next((it for it in items if it.get("ordinal") == ordinal), None)
        if not match:
            return None, "Project doesn't exist.", None
        return {"id": match["id"], "label": match["label"]}, None, None

    current_id = (session or {}).get("current_project_id")
    current_label = (session or {}).get("current_project_label")
    if current_id:
        return {"id": current_id, "label": current_label or ""}, None, None

    # Nothing named/numbered in the message and no active project in
    # stored context — offer every currently-ongoing project as a numbered
    # pick, reusing the EXACT same (project, error, ambiguous_labels) shape
    # an ambiguous NAME match already returns (see the `if name_query:`
    # branch above), so the caller's existing _ask_project_clarification
    # flow handles it identically, with zero new plumbing. Auto-picks
    # silently only when there is exactly one ongoing project — no real
    # choice to offer.
    projects = await _fetch_ongoing_projects()
    if len(projects) == 1:
        return {"id": projects[0]["id"], "label": projects[0]["label"]}, None, None
    if len(projects) > 1:
        labels = [p["label"] for p in projects]
        numbered = "\n".join(f"- {label}" for label in labels)
        return None, f"Which project did you mean?\n{numbered}", labels
    return None, 'I don\'t know which project. Send "Project N" or "Show ongoing projects" first.', None


async def _handle_list_projects(ctx: ExecContext, classification: nlu.QueryIntent) -> ExecResult:
    projects = await _fetch_ongoing_projects()

    if classification.count_only:
        n = len(projects)
        return ExecResult(ok=True, message=f"There {'is' if n == 1 else 'are'} {n} ongoing project{'' if n == 1 else 's'}.")

    if not projects:
        return ExecResult(ok=True, message="No ongoing projects right now.")

    items = [{"ordinal": i + 1, "id": p["id"], "label": p["label"]} for i, p in enumerate(projects)]
    await session_context.update_session(
        AGENT_ID, ctx.sender_phone,
        current_project_id=None, current_project_label=None, current_stage=None,
        number_map={"type": "projects", "items": items},
        talent_search=None,
        selection_basket=None,
    )
    with request_scope.stage("response_formatting"):
        lines = [f'{it["ordinal"]}. {it["label"]}' for it in items]
        rendered = "\n".join(lines)
    return ExecResult(ok=True, message=rendered)


async def _ask_project_clarification(
    ctx: ExecContext, err: str, ambiguous: List[str], resume: Dict[str, Any]
) -> ExecResult:
    """Shared by every query handler that hits an ambiguous project name —
    stores a numbered disambiguation + enough of the original query
    (`resume`) to finish it once the reply resolves which project was
    meant (see _query_parse_edits_async / _query_executor's resume-marker
    handling), instead of dead-ending or making the user repeat the whole
    command."""
    options = [{"label": o, "value": o} for o in ambiguous]
    await session_context.update_session(
        AGENT_ID, ctx.sender_phone,
        pending_disambiguation={
            "kind": "project", "field_key": "query_text", "options": options, "resume": resume,
        },
    )
    return ExecResult(ok=False, error="ambiguous_project", message=err, needs_clarification=True)


async def _handle_project_detail(
    ctx: ExecContext, session: Optional[dict], classification: nlu.QueryIntent
) -> ExecResult:
    project, err, ambiguous = await _resolve_project_ref(
        classification.project_ordinal, classification.project_name_query, session
    )
    if err:
        if ambiguous:
            return await _ask_project_clarification(ctx, err, ambiguous, {"query_kind": "project_detail"})
        return ExecResult(ok=False, error="project_not_found", message=err)
    # Three independent reads — run concurrently instead of as three
    # sequential round trips. All are read-only, so overlapping them (and
    # discarding counts/last_updated's results in the rare case the
    # project turns out not to exist) is safe.
    project_exists, counts, last_updated = await asyncio.gather(
        _project_exists(project["id"]),
        _timed_aggregation(get_stage_counts(project["id"])),
        _fetch_last_updated(project["id"]),
    )
    if not project_exists:
        return ExecResult(ok=False, error="project_not_found", message="Project doesn't exist.")
    total_talents = sum(counts.values())

    with request_scope.stage("response_formatting"):
        lines = ["Project", "", project["label"], "", f"Total Talents: {total_talents}", "", "Pipelines", ""]
        for i, stage in enumerate(PIPELINE_STAGE_ORDER, start=1):
            lines.append(f"{i}. {nlu.stage_label(stage)} ({counts.get(stage, 0)})")
        lines.append("")
        lines.append("Last Updated:")
        lines.append(_format_last_updated(last_updated))
        rendered = "\n".join(lines)

    await session_context.update_session(
        AGENT_ID, ctx.sender_phone,
        current_project_id=project["id"], current_project_label=project["label"],
        current_stage=None,
        number_map={"type": None, "items": []},
        talent_search=None,
        selection_basket=None,
    )
    return ExecResult(ok=True, message=rendered)


def _render_talent_list(project_label: str, stage: str, items: List[Dict[str, Any]]) -> str:
    """Renders the FULL, stable, alphabetically-numbered talent list in one
    message — no truncation, no paging, regardless of size (WhatsApp
    handles long messages fine)."""
    total = len(items)
    header = [
        "Project", project_label, "", "Pipeline", nlu.stage_label(stage), "",
        f"Total Talents: {total}", "", "━━━━━━━━━━━━━━", "",
    ]
    if not items:
        return "\n".join(header) + "No talents in this pipeline."
    lines = header + [f'{it["ordinal"]}. {it["label"]}' for it in items]
    return "\n".join(lines)


async def _handle_replay(ctx: ExecContext, session: Optional[dict]) -> ExecResult:
    """"Show again" / "again" / "repeat" / "open it" / "show it" — replays
    whatever the session currently holds via a FRESH live query (not a
    cached echo), so ordinals/counts stay correct even after an
    intervening move."""
    current_stage = (session or {}).get("current_stage")
    current_project_id = (session or {}).get("current_project_id")
    if current_stage:
        return await _handle_pipeline_query(ctx, session, nlu.QueryIntent(kind="pipeline", stage_key=current_stage))
    if current_project_id:
        return await _handle_project_detail(ctx, session, nlu.QueryIntent(kind="project_detail"))
    return await _handle_list_projects(ctx, nlu.QueryIntent(kind="list_projects"))


async def _handle_pipeline_multi(ctx: ExecContext, raw_project_ref: str, raw_stage_ref: str) -> ExecResult:
    """"show - Project(s) - Pipeline(s)" (Simplified Command Language,
    2026-08-17) — one-or-many projects x one-or-many stages, one grouped
    listing block per (project, stage) pair. Reuses the exact candidate
    fetch (_fetch_stage_candidates) and project/stage matchers
    _handle_pipeline_query's own single-project/single-stage path already
    uses — this only adds the loop over multiple resolved projects/stages,
    never a parallel listing implementation."""
    project_queries = nlu.split_multi_names(raw_project_ref) or [(raw_project_ref or "").strip()]
    stage_queries = nlu.split_multi_names(raw_stage_ref) or [(raw_stage_ref or "").strip()]

    projects = await _fetch_ongoing_projects()
    resolved_projects: List[Tuple[str, str]] = []
    errors: List[str] = []
    for pq in project_queries:
        with request_scope.stage("fuzzy"):
            match = nlu.resolve_project_by_name(pq, projects)
        if match.project:
            resolved_projects.append((match.project["id"], match.project["label"]))
        elif match.ambiguous:
            errors.append(f'"{pq}" — multiple matching projects found.')
        else:
            errors.append(f'"{pq}" — no matching project.')

    resolved_stages: List[str] = []
    for sq in stage_queries:
        stage_match = nlu.match_stage_phrase(sq, list(PIPELINE_STAGE_ORDER))
        if stage_match.key:
            if stage_match.key not in resolved_stages:
                resolved_stages.append(stage_match.key)
        elif stage_match.ambiguous:
            errors.append(f'"{sq}" — multiple matching pipelines: {", ".join(stage_match.ambiguous)}.')
        else:
            errors.append(f'"{sq}" — unrecognized pipeline.')

    if not resolved_projects or not resolved_stages:
        return ExecResult(
            ok=False, error="pipeline_multi_unresolved", message="\n".join(errors) or "Nothing to show.",
        )

    blocks: List[str] = []
    for pid, plabel in resolved_projects:
        for stage in resolved_stages:
            candidates = await _fetch_stage_candidates(pid, stage)
            with request_scope.stage("response_formatting"):
                sorted_c = sorted(candidates, key=lambda c: (c.label or "").strip().lower())
            header = f"{plabel} — {nlu.stage_label(stage)}"
            if not sorted_c:
                blocks.append(f"{header}\n(none)")
                continue
            lines = [header] + [f"{i + 1}. {c.label}" for i, c in enumerate(sorted_c)]
            blocks.append("\n".join(lines))
    if errors:
        blocks.append("\n".join(errors))
    return ExecResult(ok=True, message="\n\n".join(blocks))


async def _handle_pipeline_query(
    ctx: ExecContext, session: Optional[dict], classification: nlu.QueryIntent
) -> ExecResult:
    if classification.stage_ambiguous:
        options = [{"label": o, "value": o} for o in classification.stage_ambiguous]
        await session_context.update_session(
            AGENT_ID, ctx.sender_phone,
            pending_disambiguation={
                "kind": "stage", "field_key": "query_text", "options": options,
                "resume": {
                    "query_kind": "pipeline_stage_pending",
                    "project_ordinal": classification.project_ordinal,
                    "project_name_query": classification.project_name_query,
                    "count_only": classification.count_only,
                },
            },
        )
        bullets = "\n".join(f"- {o}" for o in classification.stage_ambiguous)
        return ExecResult(
            ok=False, error="ambiguous_stage",
            message=f"Which pipeline did you mean?\n{bullets}", needs_clarification=True,
        )

    project, err, ambiguous = await _resolve_project_ref(
        classification.project_ordinal, classification.project_name_query, session
    )
    if err:
        if ambiguous:
            resume = {
                "query_kind": "pipeline",
                "stage_key": classification.stage_key,
                "count_only": classification.count_only,
            }
            return await _ask_project_clarification(ctx, err, ambiguous, resume)
        return ExecResult(ok=False, error="project_not_found", message=err)
    if not await _project_exists(project["id"]):
        return ExecResult(ok=False, error="project_not_found", message="Project doesn't exist.")

    stage = classification.stage_key
    if not stage or stage not in PIPELINE_STAGES:
        return ExecResult(ok=False, error="pipeline_not_found", message="Pipeline not found.")

    candidates = await _fetch_stage_candidates(project["id"], stage)

    if classification.count_only:
        await session_context.update_session(
            AGENT_ID, ctx.sender_phone,
            current_project_id=project["id"], current_project_label=project["label"],
            current_stage=stage,
            talent_search=None,
            selection_basket=None,
        )
        return ExecResult(
            ok=True,
            message=f"{project['label']} — {nlu.stage_label(stage)}: {len(candidates)} talent(s).",
        )

    # Presentation-only sort — alphabetical, case-insensitive. The DB query
    # itself (created_at) is untouched; this is purely how the list is
    # displayed and numbered. Ordinals are assigned AFTER sorting and that
    # sorted order is exactly what's persisted into number_map, so a later
    # "Move 15" indexes into the same order the user was actually shown —
    # never the original DB order.
    with request_scope.stage("response_formatting"):
        sorted_candidates = sorted(candidates, key=lambda c: (c.label or "").strip().lower())
        items = [{"ordinal": i + 1, "id": c.id, "label": c.label} for i, c in enumerate(sorted_candidates)]
        rendered = _render_talent_list(project["label"], stage, items)
    await session_context.update_session(
        AGENT_ID, ctx.sender_phone,
        current_project_id=project["id"], current_project_label=project["label"],
        current_stage=stage,
        number_map={"type": "talents", "items": items},
        talent_search=None,
        selection_basket=None,
    )
    return ExecResult(ok=True, message=rendered)


# ---------------------------------------------------------------------------
# Talent-centric + stage-specific queries (Conversational Casting Insights,
# 2026-08-09) — "Show Ahana's projects", "Is Ahana approved for Dove?".
# Read-only, exactly like the rest of casting.query: no pipeline row is
# ever written here. Talent identity resolution reuses the SAME candidate
# fetch + fuzzy matcher Add already uses (_fetch_all_talent_candidates +
# nlu.resolve_against_candidates) rather than anything new, and ambiguity
# is offered via the SAME session_context.pending_disambiguation +
# _query_parse_edits_async plumbing project/stage ambiguity already use.
# ---------------------------------------------------------------------------
async def _resolve_talent_query_target_by_name(
    name_query: str, candidates: List[nlu.Candidate]
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[List[nlu.Candidate]]]:
    """Single-name resolution against an ALREADY-FETCHED candidate list —
    factored out of _resolve_talent_query_target so a multi-talent query
    (_handle_talent_projects_multi) can resolve each name independently
    without re-fetching the whole talent roster once per name. Same
    (talent_id, talent_label, error, ambiguous_candidates) shape."""
    if not name_query:
        return None, None, "I didn't catch who you meant.", None
    with request_scope.stage("fuzzy"):
        resolved = nlu.resolve_against_candidates(nlu.SelectorResult(ok=True, name_query=name_query), candidates)
    _log_talent_resolve_timing(resolved)
    if not resolved.ok:
        return None, None, resolved.error, resolved.ambiguous_candidates
    return resolved.talent_ids[0], resolved.talent_labels[0], None, None


async def _resolve_talent_query_target(
    raw: str
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[List[nlu.Candidate]]]:
    """Resolve a talent-query subject to (talent_id, talent_label, error,
    ambiguous_candidates). Reuses parse_talent_selector so a
    disambiguation-pick marker (RESOLVED_TALENT_MARKER, set only by
    _ask_talent_clarification's resume) bypasses name matching entirely —
    exactly like casting.move's own selection resolution already does for
    a talent picked from a numbered list a moment ago."""
    selector = nlu.parse_talent_selector(raw)
    if not selector.ok:
        return None, None, selector.error, None
    if selector.resolved_id:
        return selector.resolved_id, selector.resolved_label, None, None

    name_query = selector.name_query or (raw or "").strip()
    if not name_query:
        return None, None, "I didn't catch who you meant.", None
    candidates = await _fetch_all_talent_candidates()
    if not candidates:
        return None, None, "No talents found.", None
    return await _resolve_talent_query_target_by_name(name_query, candidates)


async def _ask_talent_clarification(
    ctx: ExecContext, err: str, candidates: List[nlu.Candidate], resume: Dict[str, Any]
) -> ExecResult:
    """Talent-side counterpart of _ask_project_clarification — same
    session_context.pending_disambiguation + resume shape, just with
    options value-encoded via RESOLVED_TALENT_MARKER (id+label) instead of
    a bare label, identical to how casting.move already disambiguates an
    ambiguous talent match."""
    options = [
        {"id": c.id, "label": c.label, "value": f"{nlu.RESOLVED_TALENT_MARKER}{c.id}|{c.label}"}
        for c in candidates
    ]
    await session_context.update_session(
        AGENT_ID, ctx.sender_phone,
        pending_disambiguation={"kind": "talent", "field_key": "query_text", "options": options, "resume": resume},
    )
    return ExecResult(ok=False, error="ambiguous_talent", message=err, needs_clarification=True)


async def _fetch_talent_active_memberships(talent_id: str) -> List[Dict[str, Any]]:
    """Every one of this talent's pipeline rows across currently-ONGOING
    projects — same "ongoing only" scope every other query already uses.
    Returns [{"project_id", "project_label", "stage"}], ordered oldest-
    membership-first."""
    projects = await _fetch_ongoing_projects()
    if not projects:
        return []
    project_ids = [p["id"] for p in projects]
    label_by_id = {p["id"]: p["label"] for p in projects}
    rows = await _timed_talent_lookup(
        db.casting_pipeline.find(
            {"talent_id": talent_id, "project_id": {"$in": project_ids}},
            {"_id": 0, "project_id": 1, "stage": 1},
        ).sort("created_at", 1).to_list(2000),
        collection="casting_pipeline", name="fetch_talent_memberships",
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        pid = r.get("project_id")
        if pid not in label_by_id:
            continue
        stage = _normalise_stage(r.get("stage")) or r.get("stage")
        out.append({"project_id": pid, "project_label": label_by_id[pid], "stage": stage})
    return out


def _render_talent_projects(talent_label: str, memberships: List[Dict[str, Any]]) -> str:
    if not memberships:
        return f"{talent_label} is currently not part of any active casting pipeline."
    lines = [talent_label, ""]
    for m in memberships:
        lines.append(m["project_label"])
        lines.append(f"• {nlu.stage_label(m['stage'])}")
        lines.append("")
    lines.append(f"Total Active Projects: {len(memberships)}")
    return "\n".join(lines)


async def _handle_talent_projects_multi(ctx: ExecContext, name_queries: List[str]) -> ExecResult:
    """"Show pending projects for A, B and C" — resolves each name
    INDEPENDENTLY (same fuzzy matcher, same candidate roster, one fetch
    shared across all of them) and renders one grouped block per talent,
    reusing _render_talent_projects verbatim so the per-talent formatting
    stays identical to the single-talent query. A name that's ambiguous or
    unmatched is reported inline rather than blocking the whole query —
    unlike a write, a read has no wrong-record risk, so there's no reason
    to make the user resolve every ambiguity before seeing the other
    talents' results; they can re-ask about just that one name to
    disambiguate it via the normal single-talent flow."""
    candidates = await _fetch_all_talent_candidates()
    if not candidates:
        return ExecResult(ok=False, error="no_talents", message="No talents found.")

    blocks: List[str] = []
    any_found = False
    for name_query in name_queries:
        talent_id, talent_label, err, ambiguous = await _resolve_talent_query_target_by_name(
            name_query, candidates
        )
        if err:
            if ambiguous:
                blocks.append(f'"{name_query}" — multiple matching talents found. Ask about them one at a time to pick.')
            else:
                blocks.append(f'"{name_query}" — no matching talent.')
            continue
        any_found = True
        memberships = await _fetch_talent_active_memberships(talent_id)
        with request_scope.stage("response_formatting"):
            blocks.append(_render_talent_projects(talent_label, memberships))
    return ExecResult(ok=any_found, message="\n\n".join(blocks))


def _render_pending_tests(talent_label: str, pending_projects: List[str]) -> str:
    n = len(pending_projects)
    if n == 0:
        return f"{talent_label} — No pending tests"
    lines = [f"{talent_label} — {n} pending test{'' if n == 1 else 's'}"]
    lines += [f"• {p}" for p in pending_projects]
    return "\n".join(lines)


async def _handle_pending_tests(ctx: ExecContext, raw_talent_ref: str) -> ExecResult:
    """"pending test - Talent(s)" (Simplified Command Language, 2026-08-17)
    — one-or-many talents, reusing the exact resolution/candidate-fetch
    _handle_talent_projects_multi already uses, filtered to the "ask_to_
    test" stage specifically (the pipeline's own "hasn't tested yet"
    signal — see _fetch_talent_active_memberships) rather than every
    active membership regardless of stage. Same per-name independent
    resolution / inline-ambiguity-note pattern as every other bulk
    talent-name query on this platform — a name that's ambiguous or
    unmatched is reported alongside the others, never blocking them."""
    selector = nlu.parse_talent_selector(raw_talent_ref)
    name_queries = selector.name_queries if (selector.ok and selector.name_queries) else (
        [selector.name_query] if (selector.ok and selector.name_query) else [(raw_talent_ref or "").strip()]
    )
    candidates = await _fetch_all_talent_candidates()
    if not candidates:
        return ExecResult(ok=False, error="no_talents", message="No talents found.")

    blocks: List[str] = []
    any_found = False
    for name_query in name_queries:
        talent_id, talent_label, err, ambiguous = await _resolve_talent_query_target_by_name(
            name_query, candidates
        )
        if err:
            if ambiguous:
                blocks.append(f'"{name_query}" — multiple matching talents found. Ask about them one at a time to pick.')
            else:
                blocks.append(f'"{name_query}" — no matching talent.')
            continue
        any_found = True
        memberships = await _fetch_talent_active_memberships(talent_id)
        pending = [m["project_label"] for m in memberships if m["stage"] == "ask_to_test"]
        with request_scope.stage("response_formatting"):
            blocks.append(_render_pending_tests(talent_label, pending))
    return ExecResult(ok=any_found, message="\n\n".join(blocks))


async def _handle_talent_projects(ctx: ExecContext, raw_talent_ref: str) -> ExecResult:
    selector = nlu.parse_talent_selector(raw_talent_ref)
    if selector.ok and selector.name_queries:
        return await _handle_talent_projects_multi(ctx, selector.name_queries)

    talent_id, talent_label, err, ambiguous = await _resolve_talent_query_target(raw_talent_ref)
    if err:
        if ambiguous:
            return await _ask_talent_clarification(ctx, err, ambiguous, {"query_kind": "talent_projects"})
        return ExecResult(ok=False, error="talent_not_found", message=err)
    memberships = await _fetch_talent_active_memberships(talent_id)
    with request_scope.stage("response_formatting"):
        rendered = _render_talent_projects(talent_label, memberships)
    return ExecResult(ok=True, message=rendered)


def _render_talent_stage_filtered(talent_label: str, stage: str, matching: List[Dict[str, Any]]) -> str:
    if not matching:
        return f"{talent_label} is not currently in {nlu.stage_label(stage)} for any active project."
    lines = [talent_label, "", nlu.stage_label(stage), ""]
    for m in matching:
        lines.append(f'• {m["project_label"]}')
    lines.append("")
    lines.append(f"Total: {len(matching)}")
    return "\n".join(lines)


async def _render_talent_stage_boolean(
    talent_id: str, talent_label: str, stage: str, project: Dict[str, str]
) -> ExecResult:
    row = await _timed_talent_lookup(
        db.casting_pipeline.find_one(
            {"talent_id": talent_id, "project_id": project["id"]}, {"_id": 0, "stage": 1}
        ),
        collection="casting_pipeline", name="talent_stage_lookup",
    )
    current_stage = _normalise_stage((row or {}).get("stage")) if row else None
    if row and current_stage == stage:
        message = f"Yes.\n\n{talent_label} is currently in {nlu.stage_label(stage)} for {project['label']}."
    elif row:
        message = f"No.\n\n{talent_label} is currently in {nlu.stage_label(current_stage)} for {project['label']}."
    else:
        message = f"No.\n\n{talent_label} is not part of {project['label']}."
    return ExecResult(ok=True, message=message)


def _render_testing_check(talent_label: str, project_statuses: List[Tuple[str, str]]) -> str:
    lines = [talent_label]
    lines += [f"• {p} — {s}" for p, s in project_statuses]
    return "\n".join(lines)


async def _handle_testing_check(ctx: ExecContext, raw_talent_ref: str, raw_project_ref: str) -> ExecResult:
    """"testing? - Talent(s) - Project(s)" (Simplified Command Language,
    2026-08-17) — one-or-many talents x one-or-many projects, grouped per
    talent. Reuses the existing status model directly from
    casting_pipeline (stage == "ask_to_test" -> a test was requested but
    not yet submitted; "already_tested" -> submitted; anything else that
    still has a real pipeline row -> a test WAS submitted at some point
    and the pipeline has since progressed past it, reusing that stage's
    own existing label rather than inventing a parallel status
    vocabulary; no row at all -> never tested for that project) — no new
    testing-status system, per spec."""
    talent_selector = nlu.parse_talent_selector(raw_talent_ref)
    talent_queries = talent_selector.name_queries if (talent_selector.ok and talent_selector.name_queries) else (
        [talent_selector.name_query] if (talent_selector.ok and talent_selector.name_query)
        else [(raw_talent_ref or "").strip()]
    )
    project_queries = nlu.split_multi_names(raw_project_ref) or [(raw_project_ref or "").strip()]

    candidates = await _fetch_all_talent_candidates()
    if not candidates:
        return ExecResult(ok=False, error="no_talents", message="No talents found.")
    projects = await _fetch_ongoing_projects()

    resolved_projects: List[Tuple[str, str]] = []
    project_errors: List[str] = []
    for pq in project_queries:
        with request_scope.stage("fuzzy"):
            match = nlu.resolve_project_by_name(pq, projects)
        if match.project:
            resolved_projects.append((match.project["id"], match.project["label"]))
        elif match.ambiguous:
            project_errors.append(f'"{pq}" — multiple matching projects found. Ask about it separately to pick.')
        else:
            project_errors.append(f'"{pq}" — no matching project.')

    if not resolved_projects:
        return ExecResult(
            ok=False, error="no_projects", message="\n".join(project_errors) or "No matching projects.",
        )

    project_ids = [pid for pid, _label in resolved_projects]
    blocks: List[str] = []
    any_found = False
    for tq in talent_queries:
        talent_id, talent_label, err, ambiguous = await _resolve_talent_query_target_by_name(tq, candidates)
        if err:
            if ambiguous:
                blocks.append(f'"{tq}" — multiple matching talents found. Ask about them one at a time to pick.')
            else:
                blocks.append(f'"{tq}" — no matching talent.')
            continue
        any_found = True
        rows = await _timed_talent_lookup(
            db.casting_pipeline.find(
                {"talent_id": talent_id, "project_id": {"$in": project_ids}},
                {"_id": 0, "project_id": 1, "stage": 1},
            ).to_list(len(project_ids)),
            collection="casting_pipeline", name="testing_check_lookup",
        )
        stage_by_project = {r["project_id"]: (_normalise_stage(r.get("stage")) or r.get("stage")) for r in rows}
        statuses: List[Tuple[str, str]] = []
        for pid, plabel in resolved_projects:
            stage = stage_by_project.get(pid)
            if stage is None:
                statuses.append((plabel, "Not tested"))
            elif stage == "already_tested":
                statuses.append((plabel, "Tested"))
            elif stage == "ask_to_test":
                statuses.append((plabel, "Test pending"))
            else:
                statuses.append((plabel, f"Tested — {nlu.stage_label(stage)}"))
        with request_scope.stage("response_formatting"):
            blocks.append(_render_testing_check(talent_label, statuses))
    if project_errors:
        blocks.append("\n".join(project_errors))
    return ExecResult(ok=any_found, message="\n\n".join(blocks))


async def _handle_talent_stage_query(
    ctx: ExecContext, session: Optional[dict], classification: nlu.QueryIntent, raw_talent_ref: str
) -> ExecResult:
    if classification.stage_ambiguous:
        options = [{"label": o, "value": o} for o in classification.stage_ambiguous]
        await session_context.update_session(
            AGENT_ID, ctx.sender_phone,
            pending_disambiguation={
                "kind": "stage", "field_key": "query_text", "options": options,
                "resume": {
                    "query_kind": "talent_stage_pending",
                    "talent_ref": raw_talent_ref,
                    "project_name_query": classification.project_name_query,
                },
            },
        )
        bullets = "\n".join(f"- {o}" for o in classification.stage_ambiguous)
        return ExecResult(
            ok=False, error="ambiguous_stage",
            message=f"Which pipeline did you mean?\n{bullets}", needs_clarification=True,
        )

    # Natural-language bulk HAS TESTED (2026-08-27, Command Specification
    # V1 Phase 3A) — "has A,B tested for X,Y" previously only ever
    # resolved a single talent (a comma-joined string handed whole to the
    # single-name resolver below, which correctly refused to guess but
    # also never fanned it out). Reuses _handle_testing_check UNCHANGED —
    # the exact same one-or-many-talents x one-or-many-projects grouping
    # "testing? - A,B - X,Y" already provides — rather than teaching this
    # function a second bulk implementation. Scoped narrowly to the
    # "tested" stage family (ask_to_test/already_tested — the only stage
    # words this specific natural phrasing ever maps to; see
    # _STAGE_SHORTHAND) so a genuinely single-item, non-"tested" stage
    # question ("Is X approved for Y?") is completely untouched, even if
    # it happens to mention multiple names for some other reason.
    if classification.stage_key in ("ask_to_test", "already_tested") and (
        "," in raw_talent_ref or "," in (classification.project_name_query or "")
    ):
        return await _handle_testing_check(
            ctx, raw_talent_ref, classification.project_name_query or "",
        )

    talent_id, talent_label, err, ambiguous = await _resolve_talent_query_target(raw_talent_ref)
    if err:
        if ambiguous:
            resume = {
                "query_kind": "talent_stage_query",
                "stage_key": classification.stage_key,
                "project_name_query": classification.project_name_query,
            }
            return await _ask_talent_clarification(ctx, err, ambiguous, resume)
        return ExecResult(ok=False, error="talent_not_found", message=err)

    stage = classification.stage_key
    if not stage or stage not in PIPELINE_STAGES:
        bullets = "\n".join(f"• {nlu.stage_label(s)}" for s in PIPELINE_STAGE_ORDER)
        return ExecResult(
            ok=False, error="pipeline_not_found",
            message=f"I couldn't tell which pipeline you meant.\n\nAvailable pipelines:\n\n{bullets}",
        )

    project_name_query = classification.project_name_query
    if project_name_query:
        project, perr, pambiguous = await _resolve_project_ref(None, project_name_query, session)
        if perr:
            if pambiguous:
                resume = {
                    "query_kind": "talent_stage_query_project_pending",
                    "talent_id": talent_id, "talent_label": talent_label, "stage_key": stage,
                }
                return await _ask_project_clarification(ctx, perr, pambiguous, resume)
            return ExecResult(ok=False, error="project_not_found", message=perr)
        with request_scope.stage("response_formatting"):
            return await _render_talent_stage_boolean(talent_id, talent_label, stage, project)

    # No project named — filtered listing across every active membership.
    memberships = await _fetch_talent_active_memberships(talent_id)
    matching = [m for m in memberships if m["stage"] == stage]
    with request_scope.stage("response_formatting"):
        rendered = _render_talent_stage_filtered(talent_label, stage, matching)
    return ExecResult(ok=True, message=rendered)


_IMPLICIT_COUNT_RE = re.compile(r"\b(how many|left|remaining)\b", re.IGNORECASE)

# Set by _query_parse_edits_async when a pending project/stage
# disambiguation (see _ask_project_clarification / the stage_ambiguous
# branch of _handle_pipeline_query) has just been resolved — tells
# _query_executor to bypass classify_query entirely and resume the
# ORIGINAL query (stored in session.pending_disambiguation["resume"])
# with the now-resolved value, rather than re-parsing free text.
_QUERY_RESUME_MARKER = "__query_resume__"


async def _resume_pending_query(session: Optional[dict], ctx: ExecContext) -> ExecResult:
    pending = (session or {}).get("pending_disambiguation") or {}
    resume = pending.get("resume") or {}
    resolved_value = pending.get("resolved_value")
    await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)

    query_kind = resume.get("query_kind")
    if query_kind == "project_detail":
        classification = nlu.QueryIntent(kind="project_detail", project_name_query=resolved_value)
        return await _handle_project_detail(ctx, session, classification)
    if query_kind == "pipeline":
        classification = nlu.QueryIntent(
            kind="pipeline", project_name_query=resolved_value,
            stage_key=resume.get("stage_key"), count_only=bool(resume.get("count_only")),
        )
        return await _handle_pipeline_query(ctx, session, classification)
    if query_kind == "pipeline_stage_pending":
        stage_match = nlu.match_stage_phrase(resolved_value or "", list(PIPELINE_STAGE_ORDER))
        classification = nlu.QueryIntent(
            kind="pipeline",
            project_ordinal=resume.get("project_ordinal"),
            project_name_query=resume.get("project_name_query"),
            stage_key=stage_match.key,
            count_only=bool(resume.get("count_only")),
        )
        return await _handle_pipeline_query(ctx, session, classification)

    if query_kind == "talent_projects":
        # resolved_value is a RESOLVED_TALENT_MARKER-encoded pick (from an
        # ambiguous-talent clarification) — _handle_talent_projects hands
        # it straight to _resolve_talent_query_target, which bypasses name
        # matching entirely for that shape (see parse_talent_selector).
        return await _handle_talent_projects(ctx, resolved_value or "")

    if query_kind == "talent_stage_query":
        # Talent name was ambiguous; now resolved. Stage/project were
        # already known before the clarification, carried via `resume`.
        classification = nlu.QueryIntent(
            kind="talent_stage_query",
            stage_key=resume.get("stage_key"),
            project_name_query=resume.get("project_name_query"),
        )
        return await _handle_talent_stage_query(ctx, session, classification, resolved_value or "")

    if query_kind == "talent_stage_pending":
        # Stage phrase was ambiguous; now resolved to one of the offered
        # stage labels.
        stage_match = nlu.match_stage_phrase(resolved_value or "", list(PIPELINE_STAGE_ORDER))
        classification = nlu.QueryIntent(
            kind="talent_stage_query",
            stage_key=stage_match.key,
            project_name_query=resume.get("project_name_query"),
        )
        return await _handle_talent_stage_query(ctx, session, classification, resume.get("talent_ref") or "")

    if query_kind == "talent_stage_query_project_pending":
        # Talent + stage were already known; the PROJECT name was
        # ambiguous and is now resolved to an exact label.
        talent_id = resume.get("talent_id")
        talent_label = resume.get("talent_label") or "talent"
        stage = resume.get("stage_key")
        project, perr, _pambiguous = await _resolve_project_ref(None, resolved_value, session)
        if perr:
            return ExecResult(ok=False, error="project_not_found", message=perr)
        return await _render_talent_stage_boolean(talent_id, talent_label, stage, project)

    return ExecResult(
        ok=False, error="unrecognized_query",
        message='I didn\'t understand that.\nTry "Show ongoing projects", "Project 3", or "Show Approved".',
    )


# ---------------------------------------------------------------------------
# Conversational Talent Search (Phase 1, 2026-08-10) — roster search by
# gender/category/city/age/height, scoped to "Talentgram Casting Pipeline"
# only (enforced generically by registry.resolve_agent_for_group). Reuses
# routers.talents._build_talent_query/_LIST_PROJECTION/_enrich_list — the
# EXACT filter engine Global Talent's own page uses — no parallel filtering
# system. Out of scope: recommendations, ranking, bulk actions, shortlisting,
# opening a talent's profile, WhatsApp/PDF export — this only ever renders a
# read-only text listing.
# ---------------------------------------------------------------------------
_TALENT_SEARCH_PAGE_SIZE = 20
_TALENT_SEARCH_ALL_CAP = 500

# Bare-reply markers, same idiom as _QUERY_RESUME_MARKER above.
_TALENT_SEARCH_PAGE_MARKER = "__talent_search_page__:"
_TALENT_SEARCH_REFINE_MARKER = "__talent_search_refine__:"
# Set by _query_parse_edits_async once a vague-term or unsupported-criteria
# clarification reply has been resolved — tells _query_executor to run the
# search directly off session.talent_search_pending_filters rather than
# re-parsing free text.
_TALENT_SEARCH_RESUME_MARKER = "__talent_search_resume__"
_TALENT_SEARCH_CANCELLED_MARKER = "__talent_search_cancelled__"


def _gender_display(gender: str) -> str:
    return (gender or "").replace("_", " ").title()


def _inches_to_height_str(inches: float) -> str:
    total = int(round(inches))
    feet, remainder = divmod(total, 12)
    return f'{feet}\'{remainder}"'


def _format_active_filters(filters: Dict[str, Any]) -> Optional[str]:
    """Labeled, human-readable summary of the currently active filters —
    shown above every result set (and in the zero-results message) so the
    conversation's search context is always visible, never implicit."""
    parts = []
    if filters.get("gender"):
        parts.append(f'Gender: {_gender_display(filters["gender"])}')
    if filters.get("interested_in"):
        parts.append(f'Category: {"/".join(filters["interested_in"])}')
    if filters.get("location"):
        parts.append(f'City: {"/".join(filters["location"])}')

    age_min, age_max = filters.get("age_min"), filters.get("age_max")
    if age_min is not None and age_max is not None:
        parts.append(f"Age: {age_min}–{age_max}")
    elif age_min is not None:
        parts.append(f"Age: {age_min}+")
    elif age_max is not None:
        parts.append(f"Age: up to {age_max}")

    height_min, height_max = filters.get("height_min"), filters.get("height_max")
    if height_min is not None and height_max is not None:
        parts.append(f"Height: {_inches_to_height_str(height_min)}–{_inches_to_height_str(height_max)}")
    elif height_min is not None:
        parts.append(f"Height: {_inches_to_height_str(height_min)}+")
    elif height_max is not None:
        parts.append(f"Height: up to {_inches_to_height_str(height_max)}")

    return ", ".join(parts) if parts else None


def _active_filter_labels(filters: Dict[str, Any]) -> List[str]:
    """Just the field NAMES currently active (e.g. ["Gender", "City"]) —
    used by the zero-results message to suggest which filter to relax,
    without re-parsing _format_active_filters' display string."""
    labels = []
    if filters.get("gender"):
        labels.append("Gender")
    if filters.get("interested_in"):
        labels.append("Category")
    if filters.get("location"):
        labels.append("City")
    if filters.get("age_min") is not None or filters.get("age_max") is not None:
        labels.append("Age")
    if filters.get("height_min") is not None or filters.get("height_max") is not None:
        labels.append("Height")
    return labels


_INSTAGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_INSTAGRAM_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?instagram\.com/(.+)$", re.IGNORECASE)


def _format_instagram_link(raw: Optional[str]) -> Optional[str]:
    """Presentation-only normalization to a clickable https://instagram.com/
    URL — never touches the stored instagram_handle field. Already-correct
    URLs round-trip unchanged; a URL variant (http, www, trailing slash) is
    normalized to the canonical form; "@handle" or a bare username is
    turned into a full URL; empty/invalid input is omitted entirely
    (returns None) rather than rendered as a broken or partial line."""
    value = (raw or "").strip()
    if not value:
        return None

    m = _INSTAGRAM_URL_RE.match(value)
    if m:
        path = m.group(1).strip("/")
        return f"https://instagram.com/{path}" if path else None

    username = value[1:] if value.startswith("@") else value
    if not _INSTAGRAM_USERNAME_RE.match(username):
        return None
    return f"https://instagram.com/{username}"


# Metadata lines under a numbered talent entry are indented, never
# numbered — only the talent's own line carries the persistent ordinal
# (Phase 2 selection/bulk actions will resolve against THAT number only).
_CARD_INDENT = "   "


def _format_talent_card(ordinal: int, t: Dict[str, Any]) -> str:
    lines = [f'{ordinal}. {t.get("name") or "Unnamed"}']

    # location entries are normally {"city","country"} dicts, but this is a
    # shared dev DB with some legacy/malformed docs (plain strings) — skip
    # those defensively rather than crash the whole card render over one
    # bad talent in an otherwise-valid result page.
    locations = t.get("location") or []
    city = "/".join([l["city"] for l in locations if isinstance(l, dict) and l.get("city")]) or None

    if t.get("age") is not None:
        lines.append(f'{_CARD_INDENT}Age: {t["age"]}')
    if t.get("height"):
        lines.append(f'{_CARD_INDENT}Height: {t["height"]}')
    if city:
        lines.append(f'{_CARD_INDENT}City: {city}')
    if t.get("gender"):
        lines.append(f'{_CARD_INDENT}Gender: {_gender_display(t["gender"])}')
    categories = t.get("interested_in") or []
    if categories:
        lines.append(f'{_CARD_INDENT}Category: {"/".join(categories)}')

    instagram_url = _format_instagram_link(t.get("instagram_handle"))
    if instagram_url:
        lines.append(f'{_CARD_INDENT}Instagram:')
        lines.append(f'{_CARD_INDENT}{instagram_url}')

    return "\n".join(lines)


def _render_talent_search_results(
    talents: List[Dict[str, Any]], total: int, skip: int, page_size: int,
    filters: Dict[str, Any], *, is_all: bool = False,
) -> str:
    filters_line = _format_active_filters(filters)

    if total == 0:
        lines = ["Found 0 talents matching that."]
        if filters_line:
            lines += ["", f"Filters: {filters_line}"]
        labels = _active_filter_labels(filters)
        if labels:
            lines += ["", f"Try removing one or more filters (e.g. {', '.join(labels)}) and search again."]
        return "\n".join(lines)

    lines = [f"Found {total} talents."]
    if filters_line:
        lines.append(f"Filters: {filters_line}")
    lines += ["", "━━━━━━━━━━━━━━", ""]
    for i, t in enumerate(talents):
        lines.append(_format_talent_card(skip + i + 1, t))
        lines.append("")
        lines.append("━━━━━━━━━━━━━━")
        lines.append("")
    shown = len(talents)

    if is_all:
        footer = f"Showing all {shown} of {total} results."
        if shown < total:
            footer = f"Showing all {shown} of {total} results (capped)."
    else:
        footer = f"Showing {shown} of {total} results."
        hints = []
        if skip + shown < total:
            hints.append('Reply "Show next 20" for more.')
        if skip > 0:
            hints.append('Reply "Show previous 20" to go back.')
        if hints:
            footer += "\n" + " ".join(hints)
    lines.append(footer)
    return "\n".join(lines)


async def _run_talent_search(
    ctx: ExecContext, filters: Dict[str, Any], *, skip: int = 0,
    limit: Optional[int] = None, is_all: bool = False,
) -> ExecResult:
    query = _build_talent_query(
        q=None, status=None,
        gender=filters.get("gender"), ethnicity=None,
        location=filters.get("location") or [],
        age_min=filters.get("age_min"), age_max=filters.get("age_max"),
        height_min=filters.get("height_min"), height_max=filters.get("height_max"),
        followers_min=None,
        interested_in=filters.get("interested_in") or [], interested_in_mode="any",
        skills=[], skills_mode="any", tags=[], tags_mode="any",
    )
    page_size = limit or _TALENT_SEARCH_PAGE_SIZE
    with request_scope.stage("talent_search_query"):
        total, docs = await asyncio.gather(
            db.talents.count_documents(query),
            db.talents.find(query, _LIST_PROJECTION)
                .sort([("name", 1), ("id", 1)]).skip(skip).limit(page_size).to_list(page_size),
        )
        talents = [_enrich_list(t) for t in docs]

    # Temporary result index (Phase 1 UX polish, 2026-08-10) — same
    # ordinal->id number_map pattern _handle_pipeline_query already uses
    # for talent-list replies, so a future "Select 3"/"Shortlist 1,2,5"
    # (Phase 2) has a real id to resolve against, not just a displayed
    # number. Ordinals are skip+i+1 (continue across pages, never reset),
    # so the SAME talent keeps the SAME number across "next"/"previous".
    items = [
        {"ordinal": skip + i + 1, "id": t.get("id"), "label": t.get("name") or "Unnamed"}
        for i, t in enumerate(talents)
    ]
    await session_context.update_session(
        AGENT_ID, ctx.sender_phone,
        talent_search={"filters": filters, "skip": skip, "page_size": page_size, "total": total},
        talent_search_pending_vague=None,
        talent_search_pending_unsupported=None,
        talent_search_pending_filters=None,
        talent_search_pending_is_refinement=None,
        number_map={"type": "talent_search", "items": items},
    )
    with request_scope.stage("response_formatting"):
        message = _render_talent_search_results(talents, total, skip, page_size, filters, is_all=is_all)
    return ExecResult(ok=True, message=message)


async def _handle_talent_search(
    ctx: ExecContext, session: Optional[dict], classification: nlu.QueryIntent, *, is_refinement: bool
) -> ExecResult:
    if classification.search_unsupported:
        kinds = " and ".join(classification.search_unsupported)
        await session_context.update_session(
            AGENT_ID, ctx.sender_phone,
            talent_search_pending_unsupported={
                "filters": classification.search_filters or {},
                "is_refinement": is_refinement,
            },
        )
        return ExecResult(
            ok=False, needs_clarification=True,
            message=f"I can't filter by {kinds} yet — want results without that filter?",
        )

    if classification.search_vague_terms:
        term = classification.search_vague_terms[0]
        field_key = nlu.VAGUE_TERM_FIELD.get(term)
        question = nlu.VAGUE_CLARIFY_QUESTIONS.get(field_key, "Could you be more specific?")
        await session_context.update_session(
            AGENT_ID, ctx.sender_phone,
            talent_search_pending_vague={"field": field_key},
            talent_search_pending_filters=classification.search_filters or {},
            talent_search_pending_is_refinement=is_refinement,
        )
        return ExecResult(ok=False, needs_clarification=True, message=question)

    new_filters = classification.search_filters or {}
    if is_refinement:
        existing = (session or {}).get("talent_search") or {}
        merged = {**(existing.get("filters") or {}), **new_filters}
    else:
        merged = new_filters
    return await _run_talent_search(ctx, merged)


async def _handle_talent_search_page(
    ctx: ExecContext, session: Optional[dict], action: Optional[str]
) -> ExecResult:
    active = (session or {}).get("talent_search")
    if not active:
        return ExecResult(
            ok=False, error="no_active_search",
            message='No active talent search yet. Try "Show female models from Mumbai" first.',
        )
    filters = active.get("filters") or {}
    skip = active.get("skip", 0)
    page_size = active.get("page_size", _TALENT_SEARCH_PAGE_SIZE)
    total = active.get("total", 0)

    if action == "next":
        new_skip = skip + page_size
        if new_skip >= total:
            return ExecResult(ok=True, message=f"That's all {total} results — nothing more to show.")
        return await _run_talent_search(ctx, filters, skip=new_skip, limit=page_size)

    if action == "previous":
        if skip <= 0:
            return ExecResult(ok=True, message="You're already at the first page.")
        return await _run_talent_search(ctx, filters, skip=max(0, skip - page_size), limit=page_size)

    if action == "all":
        return await _run_talent_search(ctx, filters, skip=0, limit=_TALENT_SEARCH_ALL_CAP, is_all=True)

    return ExecResult(ok=False, error="unrecognized_page_action", message="I didn't understand that.")


def _parse_clarification_value(text: str, field_key: Optional[str]):
    stripped = (text or "").strip()
    if field_key in ("height_min", "height_max"):
        return parse_height_to_inches(stripped)
    if field_key in ("age_min", "age_max"):
        m = re.match(r"^\s*(\d{1,3})\s*$", stripped)
        return int(m.group(1)) if m else None
    return None


async def _handle_talent_search_pending_resume(ctx: ExecContext, session: Optional[dict]) -> ExecResult:
    filters = (session or {}).get("talent_search_pending_filters") or {}
    is_refinement = (session or {}).get("talent_search_pending_is_refinement", False)
    if is_refinement:
        existing = (session or {}).get("talent_search") or {}
        filters = {**(existing.get("filters") or {}), **filters}
    return await _run_talent_search(ctx, filters)


# ---------------------------------------------------------------------------
# Talent Selection & Add to Project (Phase 2, 2026-08-10) — "Select
# 1,3,5"/"Remove 2"/"Clear selection"/"Show selected" against the CURRENT
# talent-search number_map, and "Add/Attach selected to <project>" reusing
# casting.add's existing project-resolution/confirmation machinery
# unchanged (see _resolve_add_selection below). Session-only until an add
# actually runs — nothing here writes to db.casting_pipeline.
# ---------------------------------------------------------------------------
def _render_selection_status(count: int) -> str:
    return (
        f"Selected: {count} talent{'' if count == 1 else 's'}\n\n"
        "Available commands:\n\n"
        "• Show selected\n"
        "• Add selected to a project\n"
        "• Remove 3\n"
        "• Clear selection"
    )


def _render_selection_action_result(verb: str, affected_labels: List[str], count: int) -> str:
    """UX polish (2026-08-10): shows only the talents affected by THIS
    action (never the whole basket — that's what "Show selected" is for),
    then the same compact status block. affected_labels is empty when the
    action was a no-op (e.g. "Select 1" for an already-selected talent) —
    in that case the checkmark block is skipped entirely rather than
    printing an empty one."""
    lines = []
    if affected_labels:
        lines.append(f"✓ {verb}")
        lines.append("")
        lines.extend(f"• {name}" for name in affected_labels)
        lines.append("")
    lines.append(_render_selection_status(count))
    return "\n".join(lines)


def _render_selection_basket(session: Optional[dict]) -> ExecResult:
    items = ((session or {}).get("selection_basket") or {}).get("items") or []
    if not items:
        return ExecResult(
            ok=True,
            message="No talents are currently selected.\n\nSearch and select talents first.",
        )
    lines = [f"Currently selected ({len(items)})", ""]
    lines += [f'{i}. {it["label"]}' for i, it in enumerate(items, start=1)]
    return ExecResult(ok=True, message="\n".join(lines))


async def _handle_selection_command(
    ctx: ExecContext, session: Optional[dict], action: str, spec: Optional[str]
) -> ExecResult:
    if action == "clear":
        await session_context.update_session(AGENT_ID, ctx.sender_phone, selection_basket=None)
        return ExecResult(ok=True, message="Selection cleared.")

    number_map = (session or {}).get("number_map") or {}
    if number_map.get("type") != "talent_search":
        return ExecResult(
            ok=False, error="no_active_search",
            message='No active talent search. Try "Show female models from Mumbai" first.',
        )

    items_by_ordinal = {it["ordinal"]: it for it in number_map.get("items") or []}
    ordinals, err = nlu.resolve_selection_spec(spec or "", list(items_by_ordinal.keys()))
    if err:
        return ExecResult(ok=False, error="invalid_selection", message=err)

    basket_items = list(((session or {}).get("selection_basket") or {}).get("items") or [])
    ids_in_basket = {it["id"] for it in basket_items}
    affected_labels: List[str] = []

    if action == "select":
        for o in ordinals:
            entry = items_by_ordinal[o]
            if entry["id"] not in ids_in_basket:
                basket_items.append({"id": entry["id"], "label": entry["label"]})
                ids_in_basket.add(entry["id"])
                affected_labels.append(entry["label"])
    else:  # "remove" — a talent not currently in the basket is a silent
        # no-op for that one, matching this codebase's "never error on an
        # already-satisfied state" convention (e.g. _split_by_existing_membership).
        remove_ids = {items_by_ordinal[o]["id"] for o in ordinals}
        affected_labels = [it["label"] for it in basket_items if it["id"] in remove_ids]
        basket_items = [it for it in basket_items if it["id"] not in remove_ids]

    await session_context.update_session(
        AGENT_ID, ctx.sender_phone, selection_basket={"items": basket_items},
    )
    if not basket_items:
        return ExecResult(ok=True, message="Selection cleared.")
    verb = "Selected" if action == "select" else "Removed"
    return ExecResult(ok=True, message=_render_selection_action_result(verb, affected_labels, len(basket_items)))


async def _handle_move_selection_shorthand(collected: dict, ctx: ExecContext) -> ExecResult:
    """"Select 1,3,5" arriving via casting.move's own "select" trigger (see
    SELECTION_CMD_MARKER / _extract_move_fields's pre-check) — a
    session-only basket mutation, never a real pipeline write."""
    raw = collected.get("talent_selector") or ""
    remainder = raw[len(nlu.SELECTION_CMD_MARKER):]
    command = nlu.extract_selection_command("select " + remainder)
    if not command:
        return ExecResult(ok=False, error="unrecognized_selection", message="I didn't understand that.")
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    return await _handle_selection_command(ctx, session, command["action"], command.get("spec"))


async def _query_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    raw_text = collected.get("query_text", "")
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)

    if raw_text == _QUERY_RESUME_MARKER:
        return await _resume_pending_query(session, ctx)

    if raw_text == _TALENT_SEARCH_RESUME_MARKER:
        return await _handle_talent_search_pending_resume(ctx, session)
    if raw_text == _TALENT_SEARCH_CANCELLED_MARKER:
        return ExecResult(ok=True, message="Okay, cancelled.")
    if raw_text.startswith(_TALENT_SEARCH_PAGE_MARKER):
        action = raw_text[len(_TALENT_SEARCH_PAGE_MARKER):]
        return await _handle_talent_search_page(ctx, session, action)
    if raw_text.startswith(_TALENT_SEARCH_REFINE_MARKER):
        refine_text = raw_text[len(_TALENT_SEARCH_REFINE_MARKER):]
        refinement = nlu.extract_talent_search_refinement(refine_text) or {}
        classification = nlu.QueryIntent(kind="talent_search", search_filters=refinement)
        return await _handle_talent_search(ctx, session, classification, is_refinement=True)

    # Phase 2 — "Show selected" etc. only wins over the pre-existing
    # ambiguous Approved/Locked stage query when a basket actually has
    # something in it (see module-level docstring on the "select"
    # vocabulary collision); an empty basket falls through unchanged.
    if nlu.SHOW_SELECTED_RE.match(raw_text) and ((session or {}).get("selection_basket") or {}).get("items"):
        return _render_selection_basket(session)

    selection_cmd = nlu.extract_selection_command(raw_text)
    if selection_cmd and selection_cmd["action"] != "select":
        # "select" itself only ever reaches casting.query via the MOVE-side
        # interception (_extract_move_fields) — see the collision note above.
        return await _handle_selection_command(ctx, session, selection_cmd["action"], selection_cmd.get("spec"))

    with request_scope.stage("nlu"):
        classification = nlu.classify_query(raw_text, list(PIPELINE_STAGE_ORDER))

    if (
        classification.kind == "unrecognized"
        and _IMPLICIT_COUNT_RE.search(raw_text)
        and (session or {}).get("current_stage")
    ):
        # "How many talents left?" names no stage at all — it implicitly
        # means "in whatever pipeline we were just looking at".
        classification = nlu.QueryIntent(
            kind="pipeline", stage_key=session["current_stage"], count_only=True
        )

    if classification.kind == "list_projects":
        return await _handle_list_projects(ctx, classification)
    if classification.kind == "project_detail":
        return await _handle_project_detail(ctx, session, classification)
    if classification.kind == "pipeline":
        return await _handle_pipeline_query(ctx, session, classification)
    if classification.kind == "talent_projects":
        return await _handle_talent_projects(ctx, classification.talent_query or "")
    if classification.kind == "pending_tests":
        return await _handle_pending_tests(ctx, classification.talent_query or "")
    if classification.kind == "testing_check":
        return await _handle_testing_check(
            ctx, classification.talent_query or "", classification.project_name_query or "",
        )
    if classification.kind == "pipeline_multi":
        return await _handle_pipeline_multi(
            ctx, classification.project_name_query or "", classification.stage_key_multi or "",
        )
    if classification.kind == "talent_stage_query":
        return await _handle_talent_stage_query(ctx, session, classification, classification.talent_query or "")
    if classification.kind == "talent_search":
        return await _handle_talent_search(ctx, session, classification, is_refinement=False)
    if classification.kind == "talent_search_page":
        return await _handle_talent_search_page(ctx, session, classification.search_page_action)
    if classification.kind == "replay":
        return await _handle_replay(ctx, session)
    return ExecResult(
        ok=False,
        error="unrecognized_query",
        message='I didn\'t understand that.\nTry "Show ongoing projects", "Project 3", or "Show Approved".',
    )


async def _query_parse_edits_async(
    text: str, collected: Dict[str, str], fields: List[FieldSpec], ctx: ExecContext
) -> Dict[str, str]:
    """Interprets an "editing"-step reply while a query's project/stage
    ambiguity is pending (see _ask_project_clarification and
    _handle_pipeline_query's stage_ambiguous branch) — a number, ordinal
    word, or free-text label match against the pending options resumes the
    original query via _QUERY_RESUME_MARKER (see _resume_pending_query),
    without the user repeating the whole command."""
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    stripped = (text or "").strip()

    pending_unsupported = (session or {}).get("talent_search_pending_unsupported")
    if pending_unsupported:
        decision = parse_confirmation_reply(stripped)
        if decision == "approve":
            await session_context.update_session(
                AGENT_ID, ctx.sender_phone,
                talent_search_pending_unsupported=None,
                talent_search_pending_filters=pending_unsupported.get("filters") or {},
                talent_search_pending_is_refinement=pending_unsupported.get("is_refinement", False),
            )
            return {"query_text": _TALENT_SEARCH_RESUME_MARKER}
        await session_context.update_session(
            AGENT_ID, ctx.sender_phone, talent_search_pending_unsupported=None,
        )
        return {"query_text": _TALENT_SEARCH_CANCELLED_MARKER}

    pending_vague = (session or {}).get("talent_search_pending_vague")
    if pending_vague:
        value = _parse_clarification_value(stripped, pending_vague.get("field"))
        if value is None:
            return {}
        staged = (session or {}).get("talent_search_pending_filters") or {}
        filters = {**staged, pending_vague["field"]: value}
        await session_context.update_session(
            AGENT_ID, ctx.sender_phone,
            talent_search_pending_vague=None,
            talent_search_pending_filters=filters,
        )
        return {"query_text": _TALENT_SEARCH_RESUME_MARKER}

    pending = (session or {}).get("pending_disambiguation")
    if not pending:
        return {}

    options = pending.get("options") or []
    idx = nlu.resolve_option_reply(stripped, options)
    if idx is None:
        return {}
    resolved_value = options[idx - 1]["value"]
    await session_context.update_session(
        AGENT_ID, ctx.sender_phone,
        pending_disambiguation={**pending, "resolved_value": resolved_value},
    )
    return {"query_text": _QUERY_RESUME_MARKER}


QUERY_INTENT = IntentDefinition(
    intent_id="casting.query",
    triggers=nlu.QUERY_TRIGGERS,
    fields=[QUERY_TEXT_FIELD],
    executor=_query_executor,
    extract_fields=_extract_query_fields,
    auto_confirm=True,
    parse_edits_async=_query_parse_edits_async,
)


# ---------------------------------------------------------------------------
# casting.move — move/mark/approve/reject/... (always confirmed)
# ---------------------------------------------------------------------------
def _validate_selector(raw: str) -> ValidationResult:
    result = nlu.parse_talent_selector(raw)
    if not result.ok:
        return ValidationResult(ok=False, error=result.error)
    return ValidationResult(ok=True, value=raw.strip())


def _validate_target_stage(raw: str) -> ValidationResult:
    raw = raw or ""
    if raw == _PLAN_PLACEHOLDER:
        # A multi-action plan carries its own per-step stage inside
        # collected["_plan"] — this placeholder only exists to satisfy
        # next_missing_field's required-field check; _build_move_confirmation
        # / _move_executor / _move_try_auto_execute all check `_plan`
        # FIRST and never read target_stage's resolved value when it's set.
        return ValidationResult(ok=True, value=raw)
    if raw.startswith("__ambiguous__:"):
        options = [o for o in raw[len("__ambiguous__:"):].split("|") if o]
        msg = nlu.format_numbered_options("I found multiple pipelines.", [[o] for o in options])
        return ValidationResult(ok=False, error=msg)

    match = nlu.match_stage_phrase(raw, list(PIPELINE_STAGE_ORDER))
    if match.key:
        return ValidationResult(ok=True, value=match.key)
    if match.ambiguous:
        msg = nlu.format_numbered_options("I found multiple pipelines.", [[o] for o in match.ambiguous])
        return ValidationResult(ok=False, error=msg)
    bullets = "\n".join(f"• {nlu.stage_label(s)}" for s in PIPELINE_STAGE_ORDER)
    return ValidationResult(
        ok=False,
        error=f'I couldn\'t find a pipeline named "{raw.strip()}".\n\nAvailable pipelines:\n\n{bullets}',
    )


TALENT_SELECTOR_FIELD = FieldSpec(
    key="talent_selector",
    label="Talent(s)",
    question='Who should I move? (a name, a number, numbers like 2,5,9-20, or "everyone")',
    validate=_validate_selector,
    aliases=["talent", "talents", "who"],
)

TARGET_STAGE_FIELD = FieldSpec(
    key="target_stage",
    label="To",
    question=(
        "MOVE needs a pipeline stage.\n\n"
        "Example:\n"
        "Move Ayra Krishna to Shortlisted\n\n"
        "Nothing has been moved yet."
    ),
    validate=_validate_target_stage,
    aliases=["stage", "pipeline", "to"],
)


def _validate_project_query(raw: str) -> ValidationResult:
    # Syntax-only, like every other field's validate — no DB access here.
    # Actual resolution (exact/substring/fuzzy match against live projects,
    # with a clear error or "which one?" on failure) happens once, in
    # _resolve_move_selection, shared by both confirmation and execution.
    return ValidationResult(ok=True, value=(raw or "").strip())


PROJECT_QUERY_FIELD = FieldSpec(
    key="project_query",
    label="Project",
    question="Which project?",  # never asked — required=False, only ever pre-filled from the message
    validate=_validate_project_query,
    aliases=["project", "for", "in"],
    required=False,
)


_SELECT_TRIGGER_RE = re.compile(r"^\s*select\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _strip_send_template_markers(
    chunks: "List[Tuple[int, str]]",
) -> "Tuple[List[Tuple[int, str]], Dict[int, str]]":
    """Combined Casting Pipeline + WhatsApp Automation (2026-08-19) — pulls
    nlu.SEND_TEMPLATE_MARKER out of each chunk (see that constant's own
    comment for why it's embedded in the translated text rather than
    threaded separately), returning the CLEANED chunks — safe to feed into
    nlu.extract_move_fields/extract_add_fields, neither of which has any
    idea this marker exists — plus a group_idx -> template_query lookup.

    A marker attached to only ONE sub-chunk of a multi-sub-chunk group
    (e.g. the "move to X" half of an "Add ... and move to X" chain — see
    translate_simple_command_to_natural_language) still applies to the
    WHOLE group: _execute_plan/_build_plan_confirmation only ever read
    this dict at the group-boundary flush point, never per individual
    step, so it doesn't matter which specific chunk within a group
    happened to carry the literal marker text."""
    group_send_template: Dict[int, str] = {}
    cleaned: List[Tuple[int, str]] = []
    for g, c in chunks:
        if nlu.SEND_TEMPLATE_MARKER in c:
            core, _, tmpl = c.partition(nlu.SEND_TEMPLATE_MARKER)
            core = core.rstrip()
            tmpl = tmpl.strip()
            if tmpl:
                group_send_template[g] = tmpl
            cleaned.append((g, core))
        else:
            cleaned.append((g, c))
    return cleaned, group_send_template


def _extract_move_fields(text: str) -> Dict[str, str]:
    # Whole-Stage Move (2026-08-20) — "move - Project - StageFrom to
    # StageTo" (no talent named — moves EVERYONE currently in StageFrom).
    # Checked FIRST, on the whole (and-confirm-stripped) message: this
    # shape has no natural-language sentence to translate into (see
    # nlu.parse_simple_stage_move_command's module comment) — it's
    # resolved via its own dedicated STAGE_MOVE_MARKER path instead.
    # Scoped to a single, standalone command for now (not mixed into a
    # multi-command/blank-line-separated message) — a line that isn't
    # this exact shape returns None and falls straight through to
    # everything below, unaffected.
    stripped_for_stage_move, stage_move_auto_confirm = nlu.strip_and_confirm(text or "")
    stage_move = nlu.parse_simple_stage_move_command(
        stripped_for_stage_move.strip(), list(PIPELINE_STAGE_ORDER),
    )
    if stage_move is not None:
        payload = json.dumps({"from_stage": stage_move["from_stage"], "excluded_ids": []})
        out = {
            "talent_selector": nlu.STAGE_MOVE_MARKER + payload,
            "target_stage": nlu.stage_label(stage_move["to_stage"]),
            "project_query": stage_move["project_part"],
        }
        if stage_move_auto_confirm:
            out[AUTO_CONFIRM_FIELD.key] = "1"
        return out

    # Simplified Command Language (2026-08-17) — translates every
    # "Action - Talent(s) - Project(s) - Pipeline" line into its natural-
    # language equivalent BEFORE any of the existing extraction below
    # runs; a line that doesn't match the grammar (including any genuine
    # natural-language command, and the pure "select" shorthand checked
    # right below) passes through byte-for-byte unchanged.
    text = nlu.translate_simple_commands_in_text(text, list(PIPELINE_STAGE_ORDER))

    # Phase 2 (Talent Selection & Add to Project, 2026-08-10) — "select" is
    # already a live casting.move trigger word, so "Select 1,3,5" reaches
    # HERE via the normal move-trigger path. Intercept ONLY the pure
    # selection shape (no "to"/"into" stage connector at all —
    # extract_selection_command itself won't match "select Priya to
    # Approved" or a bare name like "select Priya"): everything else falls
    # through to the untouched move-extraction logic below, byte-for-byte.
    stripped = (text or "").strip()
    m = _SELECT_TRIGGER_RE.match(stripped)
    if m:
        remainder = m.group(1).strip()
        if nlu.extract_selection_command("select " + remainder):
            return {
                "talent_selector": nlu.SELECTION_CMD_MARKER + remainder,
                "target_stage": _PLAN_PLACEHOLDER,
            }

    with request_scope.stage("nlu"):
        chunks, auto_confirm = nlu.preprocess_command_grouped(text)
        chunks, group_send_template = _strip_send_template_markers(chunks)
        out: Dict[str, str] = {}

        if len(chunks) == 1 and not group_send_template:
            group0, raw0 = chunks[0]
            fields = nlu.extract_move_fields(raw0, list(PIPELINE_STAGE_ORDER))
            project_names = nlu.split_multi_names(fields.get("project_query") or "") if fields.get("project_query") else []
            if len(project_names) <= 1:
                # The overwhelmingly common case — a normal single-action
                # command (a multi-name TALENT selector into one project is
                # already handled by the existing bulk-move machinery below,
                # untouched). Nothing about this path changed this sprint.
                out.update(fields)
                if auto_confirm:
                    out[AUTO_CONFIRM_FIELD.key] = "1"
                return out
            # "... in Toyota Glanza and ABC Project" — a multi-project
            # reference has no existing single-operation concept (a move only
            # ever targets one project_id), so it always becomes a 1-step
            # plan; _resolve_one_plan_step cross-product-expands it against
            # however many talent names were also given.
            chunks = [(group0, raw0)]

        # Combined Casting Pipeline + WhatsApp Automation (2026-08-19) —
        # a pending send_template (present whenever group_send_template
        # has an entry for that step's group) is carried on EVERY step
        # dict, redundantly repeated across a group's sub-chunks — harmless,
        # since it's read only once per group, at _execute_plan/_build_
        # plan_confirmation's group-boundary flush point.
        steps = [
            {
                "intent_id": nlu.classify_chunk_intent(c) or "casting.move", "raw_text": c, "group": g,
                "send_template": group_send_template.get(g),
            }
            for g, c in chunks
        ]
        out[PLAN_FIELD.key] = json.dumps(steps)
        out["talent_selector"] = _PLAN_PLACEHOLDER
        out["target_stage"] = _PLAN_PLACEHOLDER
        out["project_query"] = _PLAN_PLACEHOLDER
        if auto_confirm:
            out[AUTO_CONFIRM_FIELD.key] = "1"
        return out


@dataclass
class ResolvedMove:
    project_id: str
    project_label: str
    target_stage: str
    talent_ids: List[str]
    talent_labels: List[str]


async def _resolve_stage_move_selection(
    collected: dict, session: Optional[dict]
) -> Tuple[Optional[ResolvedMove], Optional[str], Optional[Dict[str, Any]]]:
    """Whole-Stage Move (2026-08-20) resolution — "move - Project -
    StageFrom to StageTo". Builds a ResolvedMove from EVERY talent
    currently in StageFrom for the resolved project (minus whatever's
    been excluded via an "Exclude X" reply on the confirmation card — see
    _stage_move_handle_confirming_reply below), reusing the exact same
    project resolution (nlu.resolve_project_by_name) and candidate fetch
    (_fetch_stage_candidates) every other stage-scoped query in this file
    already uses. Once a ResolvedMove comes back, EVERYTHING downstream
    (confirmation-card text, _split_by_current_stage, the actual write,
    undo) is the exact same, completely unmodified named-talent-move
    machinery — only how the talent_ids/talent_labels were gathered
    differs."""
    target_stage = collected.get("target_stage") or ""
    if target_stage not in PIPELINE_STAGES:
        return None, "Pipeline not found.", None

    raw = collected.get("talent_selector") or ""
    payload_raw = raw[len(nlu.STAGE_MOVE_MARKER):]
    try:
        payload = json.loads(payload_raw) if payload_raw else {}
    except (ValueError, TypeError):
        payload = {}
    from_stage = payload.get("from_stage") or ""
    if from_stage not in PIPELINE_STAGES:
        return None, "Pipeline not found.", None
    excluded_ids = set(payload.get("excluded_ids") or [])

    project_query = (collected.get("project_query") or "").strip()
    if not project_query:
        return None, 'I don\'t know which project. Send "Project N" first, or name the project.', None

    projects = await _fetch_ongoing_projects()
    with request_scope.stage("fuzzy"):
        match = nlu.resolve_project_by_name(project_query, projects)
    if match.ambiguous:
        options = [{"label": o["label"], "value": o["label"]} for o in match.ambiguous]
        msg = nlu.format_numbered_options("I found multiple projects.", [[o["label"]] for o in match.ambiguous])
        return None, msg, {"kind": "project", "field_key": "project_query", "options": options}
    if not match.project:
        if match.suggestions:
            bullets = "\n".join(f"• {o['label']}" for o in match.suggestions)
            return None, (
                f"I couldn't find a project matching:\n\n{project_query}\n\nDid you mean:\n\n{bullets}"
            ), {"kind": "free_text_retry", "field_key": "project_query", "options": []}
        return None, (
            match.error or f'I couldn\'t find a project matching "{project_query}".'
        ), {"kind": "free_text_retry", "field_key": "project_query", "options": []}

    project = match.project
    if not await _project_exists(project["id"]):
        return None, "Project doesn't exist.", None

    candidates = await _fetch_stage_candidates(project["id"], from_stage)
    with request_scope.stage("response_formatting"):
        sorted_c = sorted(candidates, key=lambda c: (c.label or "").strip().lower())
    talent_ids = [c.id for c in sorted_c if c.id not in excluded_ids]
    talent_labels = [c.label for c in sorted_c if c.id not in excluded_ids]
    if not talent_ids:
        excluded_note = " (all excluded)" if excluded_ids else ""
        return None, (
            f"No one is currently in {nlu.stage_label(from_stage)} for {project['label']}.{excluded_note}"
        ), None

    resolved = ResolvedMove(
        project_id=project["id"], project_label=project["label"], target_stage=target_stage,
        talent_ids=talent_ids, talent_labels=talent_labels,
    )
    return resolved, None, None


async def _resolve_move_selection(
    collected: dict, session: Optional[dict]
) -> Tuple[Optional[ResolvedMove], Optional[str], Optional[Dict[str, Any]]]:
    """Shared by build_confirmation (read-only, for display) and the
    executor (for the actual write). Returns (resolved, error_message,
    disambiguation) — disambiguation is set only when `error_message` is
    itself a clarification (talent/project ambiguity, or a "not part of
    this project — search everywhere?" offer) that the caller should let
    the user resolve with a short, stateful reply rather than repeating
    the whole command (see _build_move_confirmation / _move_parse_edits_async).

    Project resolution: an explicit project reference IN THIS MESSAGE
    ("... in Toyota Glanza", "... for Pantaloons") always wins over
    whatever project is active in stored context — and, per the same
    "never silently ignore an explicit reference" rule as the query path
    (_resolve_project_ref), an explicit reference that doesn't resolve to
    a real project is a clear error, not a silent fall-through to stale
    context. FORCE_GLOBAL_MARKER (set only by accepting a "search
    everywhere?" offer) explicitly discards BOTH the message's own
    project reference and stored context for this one resolution.

    Ordinal/"everyone" selection resolves against the session's STORED
    number_map — the exact listing the user was shown ("these numbers
    exist only for the current conversation") — not a fresh live query:
    if a talent has since moved out of that stage (by this same move
    sequence, or by someone else via the web UI), the remaining talents
    must NOT silently renumber out from under an in-flight "2,5,9-20"
    selector. Because that stored map is intrinsically scoped to whatever
    project was active when it was generated, an ordinal/"everyone"
    selector combined with an explicit project reference to a DIFFERENT
    project is rejected rather than guessing which one was meant. Name-
    based selection has no prior listing to snapshot, so it always
    searches the live pipeline of whichever project was resolved. Either
    way, the actual WRITE below (via _split_by_current_stage in the
    executor) re-checks each talent's CURRENT stage fresh at write time —
    stale identity is never trusted, only stale "still needs moving"
    status is re-verified.
    """
    if (collected.get("talent_selector") or "").startswith(nlu.STAGE_MOVE_MARKER):
        # Whole-Stage Move (2026-08-20) — delegates entirely to its own
        # resolver, which builds a ResolvedMove from "everyone currently
        # in the named FROM stage" instead of a named selector. Every
        # caller of _resolve_move_selection (confirmation building, the
        # executor, auto-confirm, undo) is unaffected — they all already
        # operate generically on whatever ResolvedMove comes back.
        return await _resolve_stage_move_selection(collected, session)

    target_stage = collected.get("target_stage") or ""
    if target_stage not in PIPELINE_STAGES:
        return None, "Pipeline not found.", None

    selector_text = collected.get("talent_selector") or ""
    selector = nlu.parse_talent_selector(selector_text)
    if not selector.ok:
        return None, selector.error, None

    # Pronoun ("him"/"her"/"this one", or a bare-stage command like
    # "Already Tested" with nobody named) — resolves against whoever was
    # most recently and unambiguously discussed, in THEIR project (the
    # pronoun already fully identifies both; project_query/session context
    # is not consulted for this resolution, same as a disambiguation pick).
    pronoun_project_id: Optional[str] = None
    pronoun_project_label: str = ""
    if selector.name_query == nlu.PRONOUN_LAST_MARKER:
        last_id = (session or {}).get("last_talent_id")
        last_label = (session or {}).get("last_talent_label") or "them"
        last_project_id = (session or {}).get("last_talent_project_id")
        last_project_label = (session or {}).get("last_talent_project_label") or ""
        if not last_id or not last_project_id:
            return None, 'I\'m not sure who you mean — try naming them, e.g. "Move Sarah to Hold".', None
        selector = nlu.SelectorResult(ok=True, resolved_id=last_id, resolved_label=last_label)
        pronoun_project_id, pronoun_project_label = last_project_id, last_project_label

    project_query = (collected.get("project_query") or "").strip()
    force_global = project_query == nlu.FORCE_GLOBAL_MARKER
    if force_global:
        project_query = ""

    if pronoun_project_id:
        project_id, project_label = pronoun_project_id, pronoun_project_label
    elif force_global:
        project_id, project_label = None, ""
    elif project_query:
        projects = await _fetch_ongoing_projects()
        with request_scope.stage("fuzzy"):
            match = nlu.resolve_project_by_name(project_query, projects)
        if match.project:
            project_id = match.project["id"]
            project_label = match.project["label"]
        elif match.ambiguous:
            options = [{"label": o["label"], "value": o["label"]} for o in match.ambiguous]
            msg = nlu.format_numbered_options("I found multiple projects.", [[o["label"]] for o in match.ambiguous])
            return None, msg, {"kind": "project", "field_key": "project_query", "options": options}
        elif match.suggestions:
            bullets = "\n".join(f"• {o['label']}" for o in match.suggestions)
            return None, (
                f"I couldn't find a project matching:\n\n{project_query}\n\nDid you mean:\n\n{bullets}"
            ), {"kind": "free_text_retry", "field_key": "project_query", "options": []}
        else:
            return None, (
                match.error or f'I couldn\'t find a project matching "{project_query}".'
            ), {"kind": "free_text_retry", "field_key": "project_query", "options": []}
    else:
        project_id = (session or {}).get("current_project_id")
        project_label = (session or {}).get("current_project_label") or ""

    if not force_global and (selector.ordinals or selector.everyone):
        current_stage = (session or {}).get("current_stage")
        number_map = (session or {}).get("number_map") or {}
        if not project_id or not current_stage or number_map.get("type") != "talents":
            return None, 'I don\'t have a pipeline open. Send "Project N" then "Show <Pipeline>" first.', None
        if project_query and project_id != (session or {}).get("current_project_id"):
            return None, (
                f'The pipeline list you last showed isn\'t for "{project_label}". '
                f'Send "Show <Pipeline> for {project_label}" first, then use its numbers.'
            ), None
        candidates = [
            nlu.Candidate(id=it["id"], label=it["label"], stage=current_stage)
            for it in (number_map.get("items") or [])
        ]
    elif project_id:
        # Independent reads — run concurrently rather than as two
        # sequential round trips.
        candidates, project_ok = await asyncio.gather(
            _fetch_project_candidates(project_id, project_label),
            _project_exists(project_id),
        )
        if not project_ok:
            return None, "Project doesn't exist.", None
    else:
        # No explicit project in this message and none active in stored
        # context (or the user explicitly asked to search everywhere). A
        # single name is searched across every ongoing project rather
        # than erroring immediately — but NOT multiple names in one
        # command: a move writes into one project's pipeline in a single
        # call, and guessing which of several differently-named people
        # belongs to which project risks a silent wrong-project write, so
        # that case still asks for an explicit project instead of guessing.
        if selector.name_queries:
            return None, (
                "I need to know the project for a multi-talent move — "
                'please include it, e.g. "Move X and Y to Approved in PROJECT".'
            ), {"kind": "free_text_retry", "field_key": "project_query", "options": []}
        if not selector.name_query and not selector.resolved_id:
            return None, 'I don\'t know which project. Send "Project N" first, or name the project.', None
        candidates = await _fetch_global_candidates()
        if not candidates:
            return None, "No active projects have any talents in their pipeline yet.", None

    with request_scope.stage("fuzzy"):
        resolved = nlu.resolve_against_candidates(selector, candidates)
    _log_talent_resolve_timing(resolved)

    if not resolved.ok:
        if resolved.ambiguous_candidates:
            options = [
                {"id": c.id, "label": c.label, "value": f"{nlu.RESOLVED_TALENT_MARKER}{c.id}|{c.label}"}
                for c in resolved.ambiguous_candidates
            ]
            return None, resolved.error, {"kind": "talent", "field_key": "talent_selector", "options": options}

        # A project-scoped search found NOBODY at all (not ambiguous, just
        # empty) — check whether this talent exists somewhere else active
        # before giving up, and if so, offer to widen the search rather
        # than a flat dead end.
        if project_id and selector.name_query and resolved.error == "No matching talent.":
            global_candidates = await _fetch_global_candidates()
            with request_scope.stage("fuzzy"):
                global_hit = nlu.resolve_against_candidates(
                    nlu.SelectorResult(ok=True, name_query=selector.name_query), global_candidates
                )
            _log_talent_resolve_timing(global_hit)
            if global_hit.ok:
                found_name = global_hit.talent_labels[0]
                return None, (
                    f"I found {found_name}, but they aren't part of {project_label}.\n\n"
                    f"Would you like me to search all active projects instead?"
                ), {"kind": "retry_global", "field_key": "project_query", "options": []}

        if not project_id and selector.name_query and resolved.error == "No matching talent.":
            return None, (
                f'"{selector.name_query}" wasn\'t found in any active project.'
            ), {"kind": "free_text_retry", "field_key": "talent_selector", "options": []}

        # Every other resolution failure (whatever led here) stays
        # continuable on the talent field too — a garbage retry doesn't
        # discard the pending move, it just asks again (see
        # _move_parse_edits_async's free_text_retry fallback).
        return None, resolved.error, {"kind": "free_text_retry", "field_key": "talent_selector", "options": []}

    if not project_id:
        # Resolved via the global fallback — the match itself tells us
        # which project, since none was known ahead of time. Guaranteed
        # to be a real, currently-ongoing project (it came straight from
        # _fetch_ongoing_projects via _fetch_global_candidates).
        project_id = resolved.resolved_project_id
        project_label = resolved.resolved_project_label or project_label

    return ResolvedMove(
        project_id=project_id,
        project_label=project_label,
        target_stage=target_stage,
        talent_ids=resolved.talent_ids,
        talent_labels=resolved.talent_labels,
    ), None, None


@dataclass
class SplitMove:
    actionable_ids: List[str]
    actionable_labels: List[str]
    already_labels: List[str]
    from_stages: List[str]
    # Per-actionable-talent previous stage — the data an UNDO needs to
    # restore each talent to ITS OWN prior stage (a single move can pull
    # talents out of several different stages at once, e.g. a name-based
    # move over a whole project).
    previous_stage_by_id: Dict[str, str] = dataclass_field(default_factory=dict)


async def _split_by_current_stage(resolved: ResolvedMove) -> SplitMove:
    """Live check, at whatever moment this is called (confirmation-render
    time AND, separately, approve time): which of the resolved talents are
    already in the target stage right now? Talents already there are
    excluded from the write — matches "Talent already in Approved." — and
    "from_stages" (for the success message's per-stage before/after
    counts) reflects only the talents actually being moved."""
    rows = await _timed_talent_lookup(
        db.casting_pipeline.find(
            {"project_id": resolved.project_id, "talent_id": {"$in": resolved.talent_ids}},
            {"_id": 0, "talent_id": 1, "stage": 1},
        ).to_list(len(resolved.talent_ids)),
        collection="casting_pipeline", name="split_by_current_stage",
    )
    current_stage_by_id = {
        r["talent_id"]: (_normalise_stage(r.get("stage")) or r.get("stage")) for r in rows
    }
    actionable_ids: List[str] = []
    actionable_labels: List[str] = []
    already_labels: List[str] = []
    previous_stage_by_id: Dict[str, str] = {}
    for tid, label in zip(resolved.talent_ids, resolved.talent_labels):
        if current_stage_by_id.get(tid) == resolved.target_stage:
            already_labels.append(label)
        else:
            actionable_ids.append(tid)
            actionable_labels.append(label)
            previous_stage_by_id[tid] = current_stage_by_id.get(tid)
    from_stages = sorted({s for tid, s in current_stage_by_id.items() if tid in actionable_ids})
    return SplitMove(actionable_ids, actionable_labels, already_labels, from_stages, previous_stage_by_id)


async def _remember_last_talent(ctx: ExecContext, resolved: "ResolvedMove") -> None:
    """Tracks "whoever we're currently discussing" for pronoun resolution
    ("him"/"her"/"this one", or a bare-stage command like "Already
    Tested") — only meaningful for a SINGLE-talent resolution (a bulk move
    has no single referent). Called both while still confirming (so a
    pronoun works mid-clarification, referring to who's being discussed
    right now) and after a completed move (so it survives across separate
    commands too, e.g. "Move Sarah to Hold" then later "Approve her")."""
    if len(resolved.talent_ids) == 1:
        await session_context.update_session(
            AGENT_ID, ctx.sender_phone,
            last_talent_id=resolved.talent_ids[0],
            last_talent_label=resolved.talent_labels[0],
            last_talent_project_id=resolved.project_id,
            last_talent_project_label=resolved.project_label,
        )


# ---------------------------------------------------------------------------
# Multi-action plan engine — shared by casting.move AND casting.add, since
# a plan can chain both ("Add X to Y and move to Approved"). A plan is a
# JSON list of {"intent_id", "raw_text"} steps (collected["_plan"], set by
# _extract_move_fields/_extract_add_fields via nlu.split_actions). Every
# step is resolved through the EXACT SAME per-domain resolver a single
# action already uses (_resolve_move_selection / _resolve_add_selection),
# so fuzzy matching, disambiguation payloads, and the pronoun/last-talent
# continuation between chained steps all come for free — nothing here
# re-implements any of that.
# ---------------------------------------------------------------------------
def _deserialize_plan(raw: Optional[str]) -> List[Dict[str, str]]:
    try:
        steps = json.loads(raw or "[]")
        return steps if isinstance(steps, list) else []
    except (TypeError, ValueError):
        return []


async def _resolve_one_plan_step(
    step: Dict[str, str], ctx: ExecContext, touched_pairs: List[Dict[str, str]],
    plan_resolved_flag: Optional[List[bool]] = None,
) -> List[Dict[str, Any]]:
    """Resolves ONE plan step (one raw-text chunk) into one or more
    resolved sub-steps. A chunk naming several independent talent-group ->
    project-group MAPPINGS in one go ("A and B to X, C to Y, D to X and
    Z") is first split into one segment per mapping
    (nlu.split_multi_segment_pairs) — each segment is then resolved
    exactly like any other single-project-or-cross-product chunk always
    has been (see _resolve_one_plan_segment). `touched_pairs` is a plan-
    wide accumulator (fresh per _build_plan_confirmation/_execute_plan
    call, mutated in place) of every (talent, project) pair the plan has
    successfully resolved so far — see _resolve_one_plan_segment's
    docstring for how a later, fully-implicit chained step ("...and move
    to Follow Up") uses it to apply to the WHOLE set instead of only
    whoever was last discussed."""
    intent_id = step.get("intent_id") or "casting.move"
    raw_text = step.get("raw_text") or ""
    triggers = nlu.ADD_TRIGGERS if intent_id == "casting.add" else nlu.MOVE_TRIGGERS
    with request_scope.stage("nlu"):
        segments = nlu.split_multi_segment_pairs(raw_text, triggers) or [raw_text]

    out: List[Dict[str, Any]] = []
    for segment_text in segments:
        out.extend(await _resolve_one_plan_segment(intent_id, segment_text, ctx, touched_pairs, plan_resolved_flag))
    return out


def _touched_pairs_matching_talent(
    talent_raw: str, touched_pairs: List[Dict[str, str]]
) -> List[Dict[str, str]]:
    """Filters touched_pairs down to the pair(s) belonging to an
    EXPLICITLY-named talent (or comma/and-separated several — same
    multi-name grammar every other selector already accepts), for the
    "Move A to Follow Up" explicit-narrowing shape right after a
    multi-project bulk ADD. Matches against touched_pairs' own
    talent_label (this plan's own record of who it just resolved a
    moment ago), not a fresh fuzzy DB lookup — cheap, and correct: the
    user is referring back to someone this SAME instruction already
    named. Returns [] when nothing matches, so the caller falls through
    to the ordinary single/cross-product resolution path unchanged (e.g.
    a genuinely new name never touched by this plan)."""
    names = [n.strip().lower() for n in (nlu.split_multi_names(talent_raw) or [talent_raw]) if n.strip()]
    if not names:
        return []
    matched: List[Dict[str, str]] = []
    for pair in touched_pairs:
        label_lower = (pair.get("talent_label") or "").strip().lower()
        if not label_lower:
            continue
        if any(label_lower == n or label_lower.startswith(n + " ") or n in label_lower for n in names):
            matched.append(pair)
    return matched


async def _resolve_one_plan_segment(
    intent_id: str, raw_text: str, ctx: ExecContext, touched_pairs: List[Dict[str, str]],
    plan_resolved_flag: Optional[List[bool]] = None,
) -> List[Dict[str, Any]]:
    """Resolves ONE segment — a single "<talent(s)> to <project(s)>"
    mapping — into one or more resolved sub-steps, more than one only when
    IT ITSELF cross-product-expands (multiple talent names AND/OR multiple
    project names on the SAME segment, e.g. "Add Ahana and Prajal to
    Toyota and Nykaa" -> 4 sub-steps, or "Move 4 to Approved in Toyota and
    ABC" -> 2). Each result dict: {"intent_id", "raw_text", "label" (a
    project label once resolved, else the raw text), "resolved"
    (ResolvedMove/ResolvedAdd or None), "error" (str or None)}.

    A fully-implicit MOVE segment — no talent named (bare "...and move to
    Follow Up", where extract_move_fields itself defaults talent_selector
    to PRONOUN_LAST_MARKER) OR an explicit plural/last-referent pronoun
    ("her"/"him"/"them"/"both" — still just raw text at this point,
    nlu.parse_talent_selector is what actually recognizes a pronoun word)
    AND no project named — is the "...and move to Follow Up" trailing-
    action shape. Rather than resolving against session.last_talent_id
    (a single referent), it fans out across every (talent, project) pair
    `touched_pairs` has accumulated from EARLIER steps/segments in this
    SAME plan, grouped by project (one bulk write per project, matching
    the existing "one Mongo call per project" bulk-move convention rather
    than one call per talent). An explicitly-scoped trailing action
    (naming its own talent and/or project) never reaches this branch —
    it resolves through the normal single/cross-product path below,
    exactly as before. "all"/"everyone" is deliberately NOT treated as
    this kind of pronoun — it already has its own, different, broader
    meaning elsewhere (the whole current pipeline for a project, not just
    what this one plan touched) and changing that is out of scope here.
    """
    with request_scope.stage("nlu"):
        if intent_id == "casting.add":
            fields = nlu.extract_add_fields(raw_text)
        else:
            fields = nlu.extract_move_fields(raw_text, list(PIPELINE_STAGE_ORDER))
            # extract_move_fields returns the RAW stage phrase ("Approved") —
            # the single-action path normalizes it into the internal stage key
            # ("approved") via the generic engine's FieldSpec.validate at
            # initial-collection time; a plan step bypasses that machinery
            # entirely (it resolves straight from raw_text), so it must
            # normalize here itself or _resolve_move_selection's `target_stage
            # not in PIPELINE_STAGES` check always fails.
            stage_result = _validate_target_stage(fields.get("target_stage") or "")
            if stage_result.ok:
                fields["target_stage"] = stage_result.value

        talent_raw = fields.get("talent_selector") or ""
        project_raw = fields.get("project_query")

    # 2026-08-29 fix: this used to compare talent_raw directly against the
    # PRONOUN_LAST_MARKER sentinel — which only ever matches the bare
    # "nothing named at all" case (extract_move_fields' own fallback). A
    # literally-typed pronoun word ("both"/"her"/"him"/"them") stays raw
    # text here; only nlu.parse_talent_selector (called deeper inside
    # _resolve_move_selection, on the single-referent path below) actually
    # recognizes it — so this branch was silently unreachable for every
    # explicit pronoun, and "Move both to Follow Up" fell through to
    # resolving against session.last_talent_id (ONE stale referent) instead
    # of fanning out across every pair this plan just touched. Parsing the
    # selector here — same helper, same pronoun vocabulary
    # _share_recipient_is_implicit/_plan_selector_is_implicit already use
    # for SHARE/SEND's identical implicit-reference check — makes this
    # branch reachable for both shapes without inventing a second pronoun
    # concept.
    #
    # A NAMED (non-pronoun) talent with no project of their own — "Move A
    # to Follow Up" right after "Add A,B to X,Y" — is the RULE's explicit-
    # narrowing case: A's own touched pairs (both projects A was just
    # added to) still need the SAME multi-project fan-out, just filtered
    # down to A instead of everyone. Matched against touched_pairs' own
    # labels (this plan's own record of who it just resolved a moment
    # ago) rather than a fresh fuzzy DB lookup — cheaper, and exactly what
    # "A" refers back to here.
    _talent_selector = nlu.parse_talent_selector(talent_raw)
    _talent_is_implicit_pronoun = bool(
        _talent_selector.ok and _talent_selector.name_query == nlu.PRONOUN_LAST_MARKER
    )
    _fan_out_pairs: List[Dict[str, str]] = []
    if intent_id == "casting.move" and not project_raw and touched_pairs:
        if _talent_is_implicit_pronoun:
            _fan_out_pairs = touched_pairs
        elif talent_raw:
            _fan_out_pairs = _touched_pairs_matching_talent(talent_raw, touched_pairs)

    # Current-Command Context Only (2026-09-02) — a genuine production
    # regression: "Add Anusha Sharma, move her to Follow Up, share the
    # casting call with her" with no project on the ADD clause. ADD fails
    # to resolve (missing project), so touched_pairs stays EMPTY for this
    # plan/group. Before this fix, MOVE's implicit pronoun ("her") then
    # fell through to the single-pair path below, which calls
    # _resolve_move_selection — and THAT function's own pronoun handling
    # (line ~2168) resolves an implicit pronoun against
    # session.last_talent_id/last_talent_project_id, i.e. whoever was
    # last discussed in a COMPLETELY UNRELATED EARLIER COMMAND. In the
    # reported incident this fabricated "Move Vikram Sharma to Follow Up
    # in Hinge" — a talent never even mentioned in the current message.
    #
    # The fix: within a compound PLAN, an implicit last-referent pronoun
    # on a MOVE step may ONLY ever resolve against something THIS PLAN
    # has already successfully resolved (current-command context only,
    # per the "no stale context may ever create a new action" rule) —
    # never a stale session value left over from a genuinely separate,
    # earlier command. touched_pairs alone isn't quite the right signal
    # here, though: it's deliberately reset at every GROUP boundary
    # (Simplified Command Language, e.g. "Add ...\n\nMove\nApproved" —
    # two independent-looking groups within ONE message that are still
    # meant to chain, via _remember_last_talent/session.last_talent_id,
    # exactly as they always have). `plan_resolved_flag` is the broader,
    # never-reset-per-group signal: True once ANYTHING earlier in this
    # SAME plan-resolution pass (any group) has successfully resolved —
    # at that point session.last_talent_id is guaranteed to hold what
    # THIS plan itself just wrote, not stale cross-command data, so the
    # existing session fallback below is safe to use. Only when NEITHER
    # touched_pairs NOR any earlier part of this whole plan has resolved
    # anything does this step stay unresolved with a clear "depends on an
    # earlier step" message instead of guessing — atomic resolution: an
    # unresolved earlier step can never let a later step fabricate its
    # own referent from memory.
    _plan_has_resolved_anything = bool(plan_resolved_flag and plan_resolved_flag[0])
    if (
        intent_id == "casting.move" and not project_raw and _talent_is_implicit_pronoun
        and not touched_pairs and not _plan_has_resolved_anything
    ):
        return [{
            "intent_id": intent_id, "raw_text": raw_text, "label": raw_text, "resolved": None,
            "error": (
                "This step depends on an earlier step in this command that "
                "hasn't been resolved yet — nothing has been moved."
            ),
        }]

    if _fan_out_pairs:
        target_stage = fields.get("target_stage") or ""
        if target_stage not in PIPELINE_STAGES:
            return [{
                "intent_id": intent_id, "raw_text": raw_text,
                "label": raw_text, "resolved": None, "error": "Pipeline not found.",
            }]
        by_project: Dict[str, Dict[str, Any]] = {}
        for pair in _fan_out_pairs:
            bucket = by_project.setdefault(
                pair["project_id"],
                {"project_label": pair["project_label"], "talent_ids": [], "talent_labels": []},
            )
            if pair["talent_id"] not in bucket["talent_ids"]:
                bucket["talent_ids"].append(pair["talent_id"])
                bucket["talent_labels"].append(pair["talent_label"])
        out: List[Dict[str, Any]] = []
        for project_id, bucket in by_project.items():
            resolved = ResolvedMove(
                project_id=project_id, project_label=bucket["project_label"],
                target_stage=target_stage,
                talent_ids=bucket["talent_ids"], talent_labels=bucket["talent_labels"],
            )
            for tid, tl in zip(resolved.talent_ids, resolved.talent_labels):
                touched_pairs.append({
                    "talent_id": tid, "talent_label": tl,
                    "project_id": resolved.project_id, "project_label": resolved.project_label,
                })
            out.append({
                "intent_id": intent_id, "raw_text": raw_text,
                "label": resolved.project_label, "resolved": resolved, "error": None,
            })
        if out:
            await _remember_last_talent(ctx, out[-1]["resolved"])
            if plan_resolved_flag is not None:
                plan_resolved_flag[0] = True
        return out

    talent_names = nlu.split_multi_names(talent_raw) or [talent_raw]
    project_names = await _resolve_project_query_names(project_raw)

    if len(project_names) > 1:
        # Cross-product-expand ONLY on multi-project — a multi-name talent
        # selector against a SINGLE project is the pre-existing, already-
        # correct "bulk move/add several people at once" behaviour (one
        # write, one aggregate count), handled natively by
        # _resolve_move_selection/_resolve_add_selection's own multi-name
        # support; splitting it into one sub-step per name here would
        # silently turn "2 talents moved" into two separate "1 talent
        # moved" lines for every bulk step inside a plan.
        pairs = [(t, p) for t in talent_names for p in project_names]
        # Opt 3 (2026-08-05 latency sprint): batch-check existence for
        # every distinct project this step touches in ONE $in query,
        # instead of one query per pair inside the loop below — pure
        # in-memory name resolution (already cached via
        # _fetch_ongoing_projects), so pre-resolving names to ids here
        # costs no extra Mongo round trips.
        projects_list = await _fetch_ongoing_projects()
        pre_resolve_ids = []
        for pname in set(project_names):
            pmatch = nlu.resolve_project_by_name(pname, projects_list)
            if pmatch.project:
                pre_resolve_ids.append(pmatch.project["id"])
        if pre_resolve_ids:
            await _project_exists_batch(pre_resolve_ids)
    else:
        pairs = [(talent_raw, project_raw)]

    out = []
    # Opt 2 (2026-08-05 latency sprint): _remember_last_talent used to be
    # called once PER PAIR — for a multi-project cross-product step that's
    # N sequential write+read-back round trips to persist a value only the
    # LAST one ends up keeping (last-write-wins). Nothing within this same
    # step's pair loop ever reads session.last_talent_id back (only a
    # LATER, separate plan step does — see the docstring note above), so
    # it's safe to collect the last successful resolution and write it
    # once, after the loop, instead of once per pair.
    last_resolved_single = None
    for talent_text, project_text in pairs:
        sub_fields = dict(fields)
        sub_fields["talent_selector"] = talent_text
        if project_text is not None:
            sub_fields["project_query"] = project_text
        session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
        if intent_id == "casting.add":
            resolved, err, _dis = await _resolve_add_selection(sub_fields, session)
        else:
            resolved, err, _dis = await _resolve_move_selection(sub_fields, session)
        if resolved is not None:
            if len(resolved.talent_ids) == 1:
                last_resolved_single = resolved
            if plan_resolved_flag is not None:
                plan_resolved_flag[0] = True
            for tid, tl in zip(resolved.talent_ids, resolved.talent_labels):
                touched_pairs.append({
                    "talent_id": tid, "talent_label": tl,
                    "project_id": resolved.project_id, "project_label": resolved.project_label,
                })
        out.append({
            "intent_id": intent_id, "raw_text": raw_text,
            "label": (resolved.project_label if resolved else (project_text or raw_text)),
            "resolved": resolved, "error": err,
        })
    if last_resolved_single is not None:
        # Updates session.last_talent_id — a LATER step in the SAME plan
        # referring implicitly to "whoever we just discussed" (e.g. a
        # chained "move to Approved" with no name of its own) picks this
        # up via the existing PRONOUN_LAST_MARKER path. Written once, after
        # every pair in THIS step has resolved — identical end result to
        # the old per-pair writes (last-write-wins), just without the
        # discarded intermediate round trips.
        await _remember_last_talent(ctx, last_resolved_single)
    return out


@dataclass
class _GroupSendBucket:
    project_label: str
    talent_ids: List[str] = dataclass_field(default_factory=list)
    talent_labels: List[str] = dataclass_field(default_factory=list)


async def _flush_group_send(
    summary_lines: List[str], template_query: Optional[str],
    project_send_data: Dict[str, "_GroupSendBucket"], *, is_dry_run: bool,
) -> None:
    """Combined Casting Pipeline + WhatsApp Automation (2026-08-19) — the
    ONE place a pending group WhatsApp send actually happens (or, for
    is_dry_run, is just described for the preview card). Called at every
    group boundary in _execute_plan/_build_plan_confirmation, AFTER that
    group's add/move resolution has already been accounted for — the
    pipeline update always comes first, by construction (this is only
    ever invoked once a group's steps have all been processed).
    `project_send_data` maps project_id -> the talents that successfully
    ended this group's MOVE step at the target stage (see _execute_plan's
    accumulation, right below "Already in that stage." too — an already-
    satisfied move still counts as success for send purposes). A talent
    whose pipeline update FAILED never enters this dict at all, so a
    failed pipeline op can never trigger a send for that talent/project.

    Reuses the EXACT SAME create_batch (routers/whatsapp.py, unmodified)
    a single-project-narrowed-by-talent_ids WhatsApp Campaign Agent send
    already uses — same template resolution, same recipient resolution
    (talent phone/WhatsApp group), same rendering (including the
    per-project-context rendering fix), same job creation, same existing
    WhatsApp Worker pickup. No new sender, no new renderer, no new
    recipient-resolution logic."""
    if not template_query or not project_send_data:
        return
    # Local import — whatsapp_campaign_agent.py already imports FROM this
    # module at its own top level (_fetch_ongoing_projects etc.), so a
    # top-level import here would be circular. Deferred to this one call
    # site, which only ever runs at request time, well after both modules
    # have finished loading.
    from agents.modules import whatsapp_campaign_agent as wa

    tmpl_match = await wa._resolve_source(template_query)
    if not tmpl_match.template:
        reason = tmpl_match.error or f'Multiple templates match "{template_query}" — please use the exact name.'
        summary_lines += ["", f"✗ WhatsApp send — {reason}"]
        return
    template_label = tmpl_match.template.get("name") or tmpl_match.template.get("slug") or ""

    if is_dry_run:
        for bucket in project_send_data.values():
            names = ", ".join(bucket.talent_labels)
            summary_lines.append(f"→ then send {template_label} to {names} in {bucket.project_label}")
        return

    admin = await wa._service_admin()
    total_queued = 0
    for pid, bucket in project_send_data.items():
        summary_lines.append("")
        summary_lines.append(bucket.project_label)
        try:
            result = await create_batch(
                BatchIn(
                    source_type="PROJECT",
                    source_params=SourceParams(
                        project_id=pid, pipeline_stages=list(PIPELINE_STAGE_ORDER),
                        talent_ids=bucket.talent_ids,
                    ),
                    template_id=tmpl_match.template["id"], is_dry_run=False,
                ),
                admin=admin,
            )
        except HTTPException as exc:
            for label in bucket.talent_labels:
                summary_lines.append(f"• {label} — WhatsApp send failed ({exc.detail})")
            continue
        sent_ids = {j.get("talent_id") for j in result["jobs"]}
        for tid, label in zip(bucket.talent_ids, bucket.talent_labels):
            if tid in sent_ids:
                summary_lines.append(f"• {label} — {template_label} sent")
            else:
                summary_lines.append(f"• {label} — no phone/WhatsApp group on file, not sent")
        total_queued += len(result["jobs"])
    summary_lines.append("")
    summary_lines.append(f"{total_queued} WhatsApp message{'' if total_queued == 1 else 's'} queued.")


async def _resolve_plan_steps_for_display(
    collected: dict, ctx: ExecContext,
) -> "Tuple[List[Dict[str, Any]], List[str]]":
    """The read-only resolution half of _build_plan_confirmation, factored
    out (2026-08-28) so the Guided Edit Prompt for a compound plan
    (_build_plan_edit_prompt) can describe the SAME pending plan without
    a second resolution implementation. Returns (resolved_steps,
    preview_send_lines) — see _describe_plan_step_lines for turning
    resolved_steps into the numbered "1. Add X to Y" lines both callers
    render identically."""
    steps = _deserialize_plan(collected.get(PLAN_FIELD.key))
    resolved_steps: List[Dict[str, Any]] = []
    touched_pairs: List[Dict[str, str]] = []
    # Never reset at a group boundary (unlike touched_pairs) — see
    # _resolve_one_plan_segment's "Current-Command Context Only" docstring
    # for why this broader, whole-plan signal is needed alongside
    # touched_pairs for the Simplified Command Language's legitimate
    # cross-group chaining ("Add ...\n\nMove\nApproved").
    plan_resolved_flag: List[bool] = [False]
    last_group: Optional[Any] = None
    group_send_template: Optional[str] = None
    group_send_data: Dict[str, _GroupSendBucket] = {}
    preview_send_lines: List[str] = []
    for raw_step_index, step in enumerate(steps):
        group = step.get("group", 0)
        if last_group is not None and group != last_group:
            # A new independent, blank-line-separated command begins here
            # (Simplified Command Language, 2026-08-17) — the touched_pairs
            # fan-out below must never let one command's implicit trailing
            # action ("...and move to X") apply to an EARLIER, unrelated
            # command's talents just because they happen to share one plan.
            touched_pairs = []
            await _flush_group_send(preview_send_lines, group_send_template, group_send_data, is_dry_run=True)
            group_send_template, group_send_data = None, {}
        last_group = group
        if step.get("send_template"):
            group_send_template = step["send_template"]
        # Compound Actions (2026-08-27) — casting.share/casting.send steps
        # have a fundamentally different shape (no talent/project cross-
        # product the way ADD/MOVE's own _resolve_one_plan_step expects),
        # so they're handled here directly instead of being routed through
        # it. Both still read from — and casting.share also still adds
        # to — the SAME touched_pairs accumulator, so an implicit/pronoun
        # reference ("with her", "for TVS Jupiter" left unnamed) inherits
        # from whatever this SAME group's earlier ADD/MOVE steps touched.
        #
        # "_raw_step_index" (Guided Step-Specific Editing, 2026-09-02) —
        # which entry of the RAW `steps` list (the PLAN_FIELD JSON) this
        # displayed/resolved line came from, so a later "edit step N" reply
        # can locate and rewrite exactly that one raw step. Purely
        # additive bookkeeping — every existing reader of resolved_steps
        # dicts (_describe_plan_step_lines, _execute_plan, etc.) ignores
        # unknown extra keys, so this changes no existing behaviour.
        if step.get("intent_id") == "casting.share":
            share_res = await _resolve_share_step_for_plan(step.get("raw_text") or "", touched_pairs)
            resolved_steps.append({
                "intent_id": "casting.share", "raw_text": step.get("raw_text") or "",
                "label": None, "resolved": None, "error": None, "share_resolution": share_res,
                "_raw_step_index": raw_step_index,
            })
            continue
        if step.get("intent_id") == "casting.send":
            send_fields = _resolve_send_step_for_plan(step.get("raw_text") or "", touched_pairs)
            resolved_steps.append({
                "intent_id": "casting.send", "raw_text": step.get("raw_text") or "",
                "label": None, "resolved": None, "error": None, "send_fields": send_fields,
                "_raw_step_index": raw_step_index,
            })
            continue
        resolved = await _resolve_one_plan_step(step, ctx, touched_pairs, plan_resolved_flag)
        for rs in resolved:
            rs["_raw_step_index"] = raw_step_index
        resolved_steps.extend(resolved)
        if step.get("send_template"):
            for rs in resolved:
                r = rs["resolved"]
                if r is not None and rs["intent_id"] == "casting.move":
                    bucket = group_send_data.setdefault(
                        r.project_id, _GroupSendBucket(project_label=r.project_label)
                    )
                    for tid, tl in zip(r.talent_ids, r.talent_labels):
                        if tid not in bucket.talent_ids:
                            bucket.talent_ids.append(tid)
                            bucket.talent_labels.append(tl)
    await _flush_group_send(preview_send_lines, group_send_template, group_send_data, is_dry_run=True)
    return resolved_steps, preview_send_lines


def _describe_plan_step_lines(resolved_steps: "List[Dict[str, Any]]") -> List[str]:
    """Renders resolved_steps (from _resolve_plan_steps_for_display) into
    the numbered "1. Add X to Y" description lines — shared verbatim by
    the plan's confirmation card and its Guided Edit Prompt, so editing
    a plan always describes the exact same pending operation the
    confirmation card just showed."""
    lines: List[str] = []
    for i, rs in enumerate(resolved_steps, start=1):
        if rs["intent_id"] == "casting.share":
            sr = rs.get("share_resolution")
            if sr is not None and sr.ok:
                recipients_desc = "pipeline" if sr.is_pipeline_target else ", ".join(sr.talent_labels)
                projects_desc = ", ".join(sr.project_labels)
                lines.append(f"{i}. Share {sr.template_label} for {projects_desc} with {recipients_desc}")
            else:
                err = sr.error if sr is not None else "Could not resolve this step."
                lines.append(f"{i}. {rs['raw_text']} — {err}")
            continue
        if rs["intent_id"] == "casting.send":
            sf = rs.get("send_fields") or {}
            if sf.get("error"):
                lines.append(f"{i}. {rs['raw_text']} — {sf['error']}")
                continue
            talent_desc = sf.get("talent_selector") or "?"
            project_desc = sf.get("project_query") or "?"
            lines.append(
                f"{i}. Send {talent_desc}'s submission for {project_desc} "
                "— a separate approval will follow"
            )
            continue
        r = rs["resolved"]
        if r is not None:
            names = ", ".join(r.talent_labels)
            if rs["intent_id"] == "casting.add":
                lines.append(f"{i}. Add {names} to {r.project_label} (Ask To Test)")
            else:
                lines.append(f"{i}. Move {names} to {nlu.stage_label(r.target_stage)} in {r.project_label}")
        else:
            lines.append(f"{i}. {rs['raw_text']} — {rs['error']}")
    return lines


_ADD_MISSING_PROJECT_ERROR = 'Which project? e.g. "Add Prajal Tushir to Toyota Glanza".'

# Guided Project Selection (2026-09-03) — the missing-project clarification
# (and the "change the project" mid-edit clarification below) must search
# the COMPLETE ongoing-project catalogue every time — _fetch_ongoing_
# projects already returns every ongoing project (no arbitrary slice), so
# the fix is simply to stop truncating it before display. Only the
# DISPLAY is paged, via the same "next page" recognizer Conversational
# Talent Search already uses (nlu.extract_talent_search_pagination, which
# already accepts "more"/"next"/"show more") — reused, not reinvented, per
# "a simple, reliable existing mechanism is preferred."
_PROJECT_CHOICE_PAGE_SIZE = 8


def _format_project_choice(
    all_projects: List[Dict[str, str]], offset: int, header: str,
) -> Tuple[str, List[Dict[str, str]]]:
    """(text, options_for_this_page). `all_projects` is ALWAYS the full,
    freshly-fetched ongoing-project catalogue — nothing about which
    projects exist is ever guessed, cached-stale, or silently truncated
    before this point; only how many are SHOWN at once is paged."""
    total = len(all_projects)
    page = all_projects[offset:offset + _PROJECT_CHOICE_PAGE_SIZE]
    lines = [header, ""]
    if total > _PROJECT_CHOICE_PAGE_SIZE:
        if offset == 0:
            lines.append(f"I found {total} active projects. Here are the closest matches:")
        else:
            lines.append(f"Showing projects {offset + 1}-{offset + len(page)} of {total}:")
        lines.append("")
    options = []
    for i, p in enumerate(page, start=1):
        lines.append(f"{i} → {p['label']}")
        options.append({"label": p["label"], "value": p["label"]})
    lines.append("")
    if offset + _PROJECT_CHOICE_PAGE_SIZE < total:
        lines.append('Reply with the number, type the project name, or "MORE" to see more projects.')
    else:
        lines.append("Reply with the number or type the project name.")
    return "\n".join(lines), options


async def _build_plan_missing_project_clarification(
    resolved_steps: List[Dict[str, Any]], ctx: ExecContext, collected: dict,
) -> Optional[str]:
    """Guided ADD-Missing-Project Resume — "Add Anusha Sharma, move her to
    Follow Up, share the casting call with her" (no project on ADD) must
    ask ONLY for the missing project, searching the FULL ongoing-project
    catalogue (never an arbitrary first-N slice, never stale/previous-
    command context), with a clean numbered/paginated way to answer that
    resumes THIS SAME command — never a bare "Which project?" line buried
    inside a full plan card with no way to continue except retyping
    everything. Scoped narrowly to the FIRST step being casting.add with
    genuinely no project named at all (never ambiguous — that already has
    its own numbered-disambiguation flow via _resolve_add_project); every
    other kind of step failure still renders through the normal plan
    confirmation card unchanged. Returns None when this doesn't apply."""
    if not resolved_steps:
        return None
    rs = resolved_steps[0]
    if rs["intent_id"] != "casting.add" or rs.get("resolved") is not None:
        return None
    if (rs.get("error") or "").strip() != _ADD_MISSING_PROJECT_ERROR:
        return None

    projects = await _fetch_ongoing_projects()
    # raw_text still carries this step's own "Add" trigger word (plan
    # chunks are the un-stripped chunk text — see nlu.preprocess_command_
    # grouped) — strip it so the resend example doesn't double up on "Add".
    _, talent_hint = nlu._strip_leading_trigger((rs.get("raw_text") or "").strip(), nlu.ADD_TRIGGERS)
    talent_hint = talent_hint.strip()

    text, options = _format_project_choice(projects, 0, "ADD needs a project before I can continue.")
    text += f"\n\nOr send:\nAdd {talent_hint or '[Talent]'} to [Project Name]"
    text += "\n\nNothing has been added, moved, or shared yet."

    await session_context.update_session(
        AGENT_ID, ctx.sender_phone,
        pending_disambiguation={
            "kind": "plan_missing_project", "field_key": _PLAN_EDIT_STEP_KEY,
            "options": options, "offset": 0,
        },
    )
    new_collected = dict(collected)
    new_collected[_PLAN_EDIT_STEP_KEY] = "1"
    await conversation.update_conversation(
        ctx.agent_id, ctx.sender_phone, collected=new_collected, step="editing"
    )
    return text


async def _build_plan_confirmation(collected: dict, ctx: ExecContext) -> str:
    resolved_steps, preview_send_lines = await _resolve_plan_steps_for_display(collected, ctx)
    missing_project_clarification = await _build_plan_missing_project_clarification(resolved_steps, ctx, collected)
    if missing_project_clarification is not None:
        return missing_project_clarification
    lines = ["You are about to run this plan:", ""]
    lines.extend(_describe_plan_step_lines(resolved_steps))
    if preview_send_lines:
        lines.append("")
        lines.extend(preview_send_lines)
    lines.append("")
    lines.append("Reply:")
    lines.append("1 → Approve")
    lines.append("2 → Edit a step")
    lines.append("3 → Cancel")
    return "\n".join(lines)


async def _build_plan_edit_prompt(collected: dict, ctx: ExecContext) -> str:
    """Guided Step Selector (2026-09-03) — replying "2" (or the word
    "edit"/"change") to a multi-step plan's confirmation ALWAYS lands
    here first, asking WHICH step to edit (1/2/3 = this plan's own ADD/
    MOVE/SHARE order), before any step-specific instruction is possible.
    This is the fix for the exact reported ambiguity: from the
    CONFIRMATION card, 1/2/3 always mean Approve/Edit/Cancel (unchanged);
    it is only INSIDE this selector — a completely separate state — that
    a bare number is reinterpreted as a step choice, so "3" can never be
    misread as Cancel one screen after the confirmation card. The user's
    next reply (a step number, or CANCEL) is handled by
    _plan_aware_parse_edits_async's "selecting which step" branch, which
    persists the choice and shows that step's own "EDITING STEP N —
    KIND" prompt — never both in one round trip, so the user is never
    asked to already know which number means what."""
    resolved_steps, _preview_send_lines = await _resolve_plan_steps_for_display(collected, ctx)
    lines = ["EDITING YOUR PLAN", "", "Which step would you like to edit?", ""]
    for i, rs in enumerate(resolved_steps, start=1):
        kind_label = _PLAN_STEP_KIND_LABELS.get(rs["intent_id"], rs["intent_id"])
        lines.append(f"{i} → {kind_label}")
    example_n = 2 if len(resolved_steps) >= 2 else 1
    lines += [
        "", f'Reply with the step number, for example "{example_n}".',
        "", "Or type CANCEL to leave editing.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Guided Step-Specific Editing (2026-09-02, revised 2026-09-03) — TWO
# clearly separate "editing" sub-states, never sharing a number's meaning:
#
#   1. SELECTING WHICH STEP (_PLAN_EDIT_STEP_KEY unset) — reached right
#      after "2"/"edit" on the confirmation card (_build_plan_edit_prompt
#      shows "1 -> ADD / 2 -> MOVE / 3 -> SHARE"). A step number opens
#      that step's own editor; CANCEL cancels the whole plan.
#   2. EDITING ONE STEP (_PLAN_EDIT_STEP_KEY set) — "EDITING STEP N —
#      KIND" is shown, and a free-text instruction ("change the stage to
#      Shortlisted", "share it with X instead", ...) rewrites ONLY that
#      one raw plan step — every other step's raw_text passes through
#      unchanged, so "only that step changes" holds structurally, not
#      just by convention. CANCEL still cancels the whole plan here too.
#
# This is the fix for the reported ambiguity: from the CONFIRMATION card,
# "3" always means Cancel; a bare number is ONLY ever reinterpreted as a
# step choice inside the SELECTING-WHICH-STEP sub-state, a completely
# separate turn/state the confirmation card's own 1/2/3 never overlaps
# with.
# ---------------------------------------------------------------------------
_PLAN_EDIT_STEP_KEY = "_plan_edit_step"

_EDIT_CANCEL_RE = re.compile(
    r"^\s*(?:cancel(?:\s+editing)?|stop\s+editing)\s*[.!?]*\s*$", re.IGNORECASE
)
_EDITING_CANCELLED_MESSAGE = "Editing cancelled. Nothing has been executed."


async def _cancel_plan_edit(ctx: ExecContext) -> str:
    """Cancels the WHOLE pending plan from inside either editing sub-
    state — the only unambiguous meaning of CANCEL there (never "go back
    one level"), matching the master requirement that a bare step number
    can never double as a cancel word once the user is selecting/editing
    a step. Reuses the exact same conversation-clearing primitive the
    ordinary "3 -> Cancel" confirmation reply already uses; only the
    wording differs (confirming CANCEL was understood specifically as an
    edit-time cancel, not a generic "3")."""
    await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
    await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
    return _EDITING_CANCELLED_MESSAGE


def _validate_plan_step_edit_error(raw: str) -> ValidationResult:
    """Always-fails, echoing `raw` back as the error text. Exists purely
    to surface a step-specific "I didn't understand that change" message
    through the existing generic edit-reply pipeline (agents/dispatcher.
    py's _collect_or_advance: a validation failure's `error` becomes the
    turn's reply, and — critically — `collected`/the conversation step
    are left UNTOUCHED when validate fails, so the user stays in the
    exact same "editing step N" state and can just try again, without any
    new dispatcher-level state machine)."""
    return ValidationResult(ok=False, error=raw)


PLAN_STEP_EDIT_ERROR_FIELD = FieldSpec(
    key="_plan_step_edit_error", label="Plan Step Edit Error", question="",
    validate=_validate_plan_step_edit_error, required=False,
)

# Registered as a REAL field (not just a raw collected[] key someone reads
# directly) purely so a successful edit can CLEAR it via the normal edits-
# dict merge in agents/dispatcher.py's _collect_or_advance — that merge
# only ever applies keys present in intent.fields, silently dropping
# anything else (see PLAN_STEP_EDIT_ERROR_FIELD's own docstring on this
# same mechanism). Without this, a resolved step index would leak into
# the NEXT "confirming" -> "editing" cycle: the very next "2" reply
# (meant to pick a fresh step from the "which step" selector) would be
# misread as a free-text instruction for the STALE step instead.
PLAN_EDIT_STEP_FIELD = FieldSpec(
    key=_PLAN_EDIT_STEP_KEY, label="Plan Edit Step", question="",
    validate=_validate_hidden, required=False,
)

_PLAN_STEP_KIND_LABELS = {
    "casting.add": "ADD", "casting.move": "MOVE", "casting.share": "SHARE", "casting.send": "SEND",
}


def _describe_plan_step_for_edit(rs: Dict[str, Any]) -> Tuple[str, List[str]]:
    """(current_description, example_instructions) for ONE resolved plan
    step — reuses the exact same resolved data _describe_plan_step_lines
    renders (never a second, possibly-diverging description)."""
    intent_id = rs["intent_id"]
    if intent_id == "casting.add":
        r = rs.get("resolved")
        if r is None:
            return rs.get("raw_text") or "", []
        current = f"Add {', '.join(r.talent_labels)} to {r.project_label}."
        examples = ["Change the project to [Project]", "Change ADD to [Talent(s)]"]
        if len(r.talent_labels) > 1:
            examples.append(f"Remove {r.talent_labels[-1]}")
        examples.append("Remove this step")
        return current, examples
    if intent_id == "casting.move":
        r = rs.get("resolved")
        if r is None:
            return rs.get("raw_text") or "", []
        current = f"Move {', '.join(r.talent_labels)} to {nlu.stage_label(r.target_stage)} in {r.project_label}."
        examples = ["Change the stage to Shortlisted", "Change the project to [Project]"]
        if len(r.talent_labels) > 1:
            examples.append(f"Remove {r.talent_labels[-1]}")
            examples.append(f"Move only {r.talent_labels[0]}")
        examples.append("Remove this step")
        return current, examples
    if intent_id == "casting.share":
        sr = rs.get("share_resolution")
        if sr is None or not sr.ok:
            return rs.get("raw_text") or "", []
        recipients_desc = "everyone in the pipeline" if sr.is_pipeline_target else ", ".join(sr.talent_labels)
        current = f"Share the casting call with {recipients_desc} for {', '.join(sr.project_labels)}."
        examples = []
        if not sr.is_pipeline_target:
            examples.append("Share it with [Talent] instead")
            examples.append("Share it with both")
        examples.append("Change the project")
        examples.append("Remove this step")
        return current, examples
    if intent_id == "casting.send":
        sf = rs.get("send_fields") or {}
        talent_desc = sf.get("talent_selector") or "?"
        project_desc = sf.get("project_query") or "?"
        current = f"Send {talent_desc}'s submission for {project_desc}."
        return current, ["SEND has its own separate approval after this plan runs — nothing to change here."]
    return rs.get("raw_text") or "", []


def _build_plan_step_edit_prompt_from_resolved(resolved_steps: List[Dict[str, Any]], step_index: int) -> str:
    rs = resolved_steps[step_index - 1]
    kind_label = _PLAN_STEP_KIND_LABELS.get(rs["intent_id"], rs["intent_id"])
    current, examples = _describe_plan_step_for_edit(rs)
    lines = [f"EDITING STEP {step_index} — {kind_label}", "", "Current:", current, "",
             "What would you like to change?"]
    if examples:
        lines += ["", "You can say:"]
        lines += [f"• {e}" for e in examples]
    lines += ["", "Nothing will execute until you confirm."]
    return "\n".join(lines)


_EDIT_STAGE_RE = re.compile(r"^\s*change\s+(?:the\s+)?stage\s+to\s+(.+?)\s*$", re.IGNORECASE)
_EDIT_PROJECT_RE = re.compile(r"^\s*change\s+(?:the\s+)?project\s+to\s+(.+?)\s*$", re.IGNORECASE)
# "Change the project" / "Change project" with NOTHING after it — a clear
# intent missing only the destination; see the guided clarification this
# triggers in _apply_plan_step_edit_instruction. Must be checked BEFORE
# _EDIT_PROJECT_RE only in the sense that the "to ..." form should win
# when present — matched separately (mutually exclusive patterns; a "to"
# clause always fails this one) so ordering between them doesn't matter.
_EDIT_PROJECT_BARE_RE = re.compile(r"^\s*change\s+(?:the\s+)?project\s*[.!?]*\s*$", re.IGNORECASE)
_EDIT_REMOVE_STEP_RE = re.compile(r"^\s*(?:remove|delete)\s+(?:this\s+step|step)\s*$", re.IGNORECASE)
_EDIT_REMOVE_NAME_RE = re.compile(r"^\s*remove\s+(.+?)\s*$", re.IGNORECASE)
_EDIT_MOVE_ONLY_RE = re.compile(r"^\s*(?:move|keep)\s+only\s+(.+?)\s*$", re.IGNORECASE)
_EDIT_SHARE_WITH_RE = re.compile(r"^\s*share\s+(?:it\s+)?with\s+(.+?)(?:\s+instead)?\s*$", re.IGNORECASE)
# "Change ADD to Anusha and Nikita" — replaces WHO an ADD step names
# entirely (never partial — "remove X"/narrowing already cover the
# incremental cases). ADD-specific per the explicit example; MOVE keeps
# its own "remove X"/"move only X" narrowing vocabulary unchanged.
_EDIT_CHANGE_ADD_TALENTS_RE = re.compile(r"^\s*change\s+add\s+to\s+(.+?)\s*$", re.IGNORECASE)


def _rebuild_add_raw_text(talent_labels: List[str], project_text: str) -> str:
    return f"Add {', '.join(talent_labels)} to {project_text}"


def _rebuild_move_raw_text(
    talent_labels: List[str], stage_label: str, project_text: Optional[str] = None,
) -> str:
    """project_text=None deliberately omits the "in <project>" clause —
    see _plan_step_project_is_chained's docstring for why a stage-only/
    narrowing edit on a step chained from an earlier not-yet-executed ADD
    must stay implicit (naming only the talent(s) and stage) rather than
    naming an explicit project, so it keeps resolving through the plan's
    own touched_pairs fan-out instead of a live DB lookup that would find
    nothing yet."""
    base = f"Move {', '.join(talent_labels)} to {stage_label}"
    return f"{base} in {project_text}" if project_text else base


def _rebuild_share_raw_text(project_text: str, recipient_text: str) -> str:
    return f"Share the casting call for {project_text} with {recipient_text}"


def _plan_step_project_is_chained(
    raw_i: int, project_id: str, talent_ids: List[str], resolved_steps: List[Dict[str, Any]],
) -> bool:
    """True when an EARLIER step (smaller _raw_step_index) in this SAME
    plan already resolved at least one of `talent_ids` into `project_id`
    — meaning this step's project traces back to that earlier step
    (typically a chained ADD that hasn't been written to the database
    yet by the time a later step is being edited/previewed), not an
    independent, already-existing DB fact. See _rebuild_move_raw_text."""
    talent_id_set = set(talent_ids)
    for other in resolved_steps:
        if other.get("_raw_step_index", raw_i) >= raw_i:
            continue
        other_r = other.get("resolved")
        if other_r is None:
            continue
        if getattr(other_r, "project_id", None) != project_id:
            continue
        if talent_id_set & set(getattr(other_r, "talent_ids", None) or []):
            return True
    return False


async def _apply_plan_step_edit_instruction(
    collected: dict, ctx: ExecContext, step_index: int, instruction: str,
    resolved_steps: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Applies ONE free-text edit instruction to step `step_index` (1-
    based, matching the number the confirmation card displayed) of a
    compound plan. Returns (new_plan_json, None) on success, or
    (None, error_message) — the error is shown VERBATIM to the user while
    the conversation stays in the exact same "editing step N" state (see
    PLAN_STEP_EDIT_ERROR_FIELD), never silently discarding the pending
    edit or the rest of the plan. Only ever rewrites raw step
    `steps[raw_step_index]` (or removes that one entry) — every other
    step's raw_text is passed through byte-for-byte unchanged."""
    steps = _deserialize_plan(collected.get(PLAN_FIELD.key))
    if resolved_steps is None:
        resolved_steps, _ = await _resolve_plan_steps_for_display(collected, ctx)
    if step_index < 1 or step_index > len(resolved_steps):
        return None, "That step number doesn't exist in this plan anymore."
    rs = resolved_steps[step_index - 1]
    raw_i = rs.get("_raw_step_index")
    if raw_i is None or raw_i >= len(steps):
        return None, "I couldn't locate that step to edit — try re-sending the whole command."

    intent_id = rs["intent_id"]
    if intent_id == "casting.send":
        return None, "SEND has its own separate approval after this plan runs — there's nothing to edit here."

    stripped = (instruction or "").strip()

    if _EDIT_REMOVE_STEP_RE.match(stripped):
        if len(steps) <= 1:
            return None, "Can't remove the only step in this plan — type CANCEL to cancel the whole thing instead."
        new_steps = steps[:raw_i] + steps[raw_i + 1:]
        return json.dumps(new_steps), None

    sibling_count = sum(1 for other in resolved_steps if other.get("_raw_step_index") == raw_i)
    if sibling_count > 1:
        return None, (
            "This step represents multiple talents/projects combined and can't be "
            'edited individually yet — cancel and re-send the command with your '
            'correction, or say "remove this step".'
        )

    new_raw_text: Optional[str] = None

    if intent_id in ("casting.add", "casting.move"):
        # Guided ambiguous-instruction clarification (Part 2, 2026-09-03)
        # — "change the project" with NO destination named is a CLEAR
        # intent missing only one piece of information; ask a specific,
        # numbered question (reusing the exact same full-catalogue/
        # pagination mechanism the initial missing-project clarification
        # uses) instead of a generic "I didn't understand that change"
        # parser error. Checked before resolution-state matters — even an
        # unresolved (missing-project) ADD step can answer "change the
        # project" this way, landing on the identical clarification its
        # own auto-shown prompt already offers.
        if _EDIT_PROJECT_BARE_RE.match(stripped):
            projects = await _fetch_ongoing_projects()
            header = f"I can change the project for Step {step_index}.\n\nWhich project should I use?"
            text_out, options = _format_project_choice(projects, 0, header)
            await session_context.update_session(
                AGENT_ID, ctx.sender_phone,
                pending_disambiguation={
                    "kind": "plan_missing_project", "field_key": _PLAN_EDIT_STEP_KEY,
                    "options": options, "offset": 0,
                },
            )
            return None, text_out

        r = rs.get("resolved")
        if r is None:
            # The ONE edit ever allowed against an unresolved step: filling
            # in an ADD step's genuinely missing project (Guided ADD-
            # Missing-Project Resume, 2026-09-02) — reached via
            # _plan_aware_parse_edits_async wrapping the user's numbered/
            # free-text reply into this exact instruction shape. Every
            # other unresolved-step case (ambiguous talent, talent not
            # found, etc.) has its own existing clarification flow and
            # still can't be edited this way.
            m = _EDIT_PROJECT_RE.match(stripped)
            if intent_id == "casting.add" and m and rs.get("error") == _ADD_MISSING_PROJECT_ERROR:
                original_raw = (steps[raw_i].get("raw_text") or "").strip()
                new_raw_text = f"{original_raw} to {m.group(1).strip()}"
            else:
                return None, rs.get("error") or "This step hasn't resolved yet — nothing to edit."

        # Preserving-the-project edits (stage-only, narrowing) must stay
        # implicit ("Move X to Y", no "in <project>") when this step's
        # project traces back to an EARLIER step in this same plan — see
        # _plan_step_project_is_chained/_rebuild_move_raw_text. Only the
        # explicit "change the project" edit below ever names a project
        # outright, since that's the one case actually changing it.
        move_project_text = (
            None
            if r is not None and intent_id == "casting.move"
            and _plan_step_project_is_chained(raw_i, r.project_id, r.talent_ids, resolved_steps)
            else (r.project_label if r is not None else None)
        )

        m = _EDIT_STAGE_RE.match(stripped) if (r is not None and intent_id == "casting.move") else None
        if m:
            stage_result = _validate_target_stage(m.group(1))
            if not stage_result.ok:
                return None, stage_result.error
            new_raw_text = _rebuild_move_raw_text(
                r.talent_labels, nlu.stage_label(stage_result.value), move_project_text,
            )

        if new_raw_text is None:
            m = _EDIT_PROJECT_RE.match(stripped)
            if m:
                new_project_text = m.group(1).strip()
                if intent_id == "casting.move":
                    new_raw_text = _rebuild_move_raw_text(
                        r.talent_labels, nlu.stage_label(r.target_stage), new_project_text,
                    )
                else:
                    new_raw_text = _rebuild_add_raw_text(r.talent_labels, new_project_text)

        if new_raw_text is None and intent_id == "casting.add":
            m = _EDIT_CHANGE_ADD_TALENTS_RE.match(stripped)
            if m:
                new_names = [n.strip() for n in (nlu.split_multi_names(m.group(1)) or [m.group(1)]) if n.strip()]
                if not new_names:
                    return None, f"Couldn't understand \"{m.group(1).strip()}\" as talent name(s)."
                new_raw_text = _rebuild_add_raw_text(new_names, r.project_label)

        if new_raw_text is None and intent_id == "casting.move":
            m = _EDIT_MOVE_ONLY_RE.match(stripped)
            if m:
                keep_names = [n.strip().lower() for n in (nlu.split_multi_names(m.group(1)) or [m.group(1)]) if n.strip()]
                kept = [t for t in r.talent_labels if any(k in t.lower() for k in keep_names)]
                if not kept:
                    return None, f"Couldn't find {m.group(1).strip()} among this step's talents."
                new_raw_text = _rebuild_move_raw_text(kept, nlu.stage_label(r.target_stage), move_project_text)

        if new_raw_text is None:
            m = _EDIT_REMOVE_NAME_RE.match(stripped)
            if m:
                remove_name = m.group(1).strip().lower()
                remaining = [t for t in r.talent_labels if remove_name not in t.lower()]
                if len(remaining) == len(r.talent_labels):
                    return None, f"Couldn't find {m.group(1).strip()} among this step's talents."
                if not remaining:
                    if len(steps) <= 1:
                        return None, "Can't remove the only talent and leave an empty step — type CANCEL instead."
                    new_steps = steps[:raw_i] + steps[raw_i + 1:]
                    return json.dumps(new_steps), None
                if intent_id == "casting.move":
                    new_raw_text = _rebuild_move_raw_text(remaining, nlu.stage_label(r.target_stage), move_project_text)
                else:
                    new_raw_text = _rebuild_add_raw_text(remaining, r.project_label)

    elif intent_id == "casting.share":
        if _EDIT_PROJECT_BARE_RE.match(stripped):
            projects = await _fetch_ongoing_projects()
            header = f"I can change the project for Step {step_index}.\n\nWhich project should I use?"
            text_out, options = _format_project_choice(projects, 0, header)
            await session_context.update_session(
                AGENT_ID, ctx.sender_phone,
                pending_disambiguation={
                    "kind": "plan_missing_project", "field_key": _PLAN_EDIT_STEP_KEY,
                    "options": options, "offset": 0,
                },
            )
            return None, text_out

        sr = rs.get("share_resolution")
        if sr is None or not sr.ok:
            return None, (sr.error if sr is not None else None) or "This step hasn't resolved yet — nothing to edit."

        m = _EDIT_PROJECT_RE.match(stripped)
        if m:
            recipient_text = "pipeline" if sr.is_pipeline_target else ",".join(sr.talent_labels)
            new_raw_text = _rebuild_share_raw_text(m.group(1).strip(), recipient_text)

        if new_raw_text is None:
            m = _EDIT_SHARE_WITH_RE.match(stripped)
            if m:
                new_raw_text = _rebuild_share_raw_text(",".join(sr.project_labels), m.group(1).strip())

        if new_raw_text is None and not sr.is_pipeline_target:
            m = _EDIT_REMOVE_NAME_RE.match(stripped)
            if m:
                remove_name = m.group(1).strip().lower()
                remaining = [t for t in sr.talent_labels if remove_name not in t.lower()]
                if len(remaining) == len(sr.talent_labels):
                    return None, f"Couldn't find {m.group(1).strip()} among this step's recipients."
                if not remaining:
                    if len(steps) <= 1:
                        return None, "Can't remove the only recipient and leave an empty step — type CANCEL instead."
                    new_steps = steps[:raw_i] + steps[raw_i + 1:]
                    return json.dumps(new_steps), None
                new_raw_text = _rebuild_share_raw_text(",".join(sr.project_labels), ",".join(remaining))

    if new_raw_text is None:
        return None, (
            'I didn\'t understand that change.\n\n'
            'Try: "change the stage to Shortlisted", "change the project to [Project]", '
            '"remove [Name]", or "remove this step".\n\n'
            'Or type CANCEL to leave editing.'
        )

    new_steps = list(steps)
    new_steps[raw_i] = dict(new_steps[raw_i])
    new_steps[raw_i]["raw_text"] = new_raw_text
    return json.dumps(new_steps), None


_PLAN_STEP_NUMBER_RE = re.compile(r"^\s*(\d+)\s*$")


async def _plan_aware_parse_edits_async(
    text: str, collected: Dict[str, str], fields: List[FieldSpec], ctx: ExecContext,
) -> Dict[str, str]:
    """Wraps _move_parse_edits_async (shared by ADD_INTENT/MOVE_INTENT).
    Handles BOTH "editing" sub-states for a compound plan — see the
    "Guided Step-Specific Editing" block comment above for what each
    means and why they're kept structurally separate. Every other case —
    a plain (non-plan) ADD/MOVE edit, or no PLAN_FIELD at all — falls
    straight through to the existing, unmodified _move_parse_edits_async,
    completely unaffected."""
    if not (collected.get(PLAN_FIELD.key) or "").strip():
        return await _move_parse_edits_async(text, collected, fields, ctx)

    stripped = (text or "").strip()

    # CANCEL (and "cancel editing"/"stop editing") is unambiguous in
    # EITHER sub-state — it never means "step 3" or anything else, only
    # ever "cancel the whole pending plan". Checked before either
    # sub-state's own number/instruction parsing so it can never be
    # shadowed by them.
    if _EDIT_CANCEL_RE.match(stripped):
        message = await _cancel_plan_edit(ctx)
        return {PLAN_STEP_EDIT_ERROR_FIELD.key: message}

    step_index_raw = (collected.get(_PLAN_EDIT_STEP_KEY) or "").strip()

    if not step_index_raw:
        # Sub-state 1: SELECTING WHICH STEP (just saw "EDITING YOUR PLAN
        # — which step would you like to edit? 1 -> ADD / 2 -> MOVE / 3
        # -> SHARE"). A bare number here — and ONLY here — is
        # reinterpreted as a step choice; this state is never reachable
        # from the confirmation card's own "3 -> Cancel" reply (CANCEL is
        # handled above, and the confirmation card's 1/2/3 are parsed by
        # dispatcher.py's ordinary approve/edit/cancel logic BEFORE this
        # function is ever called for a plan at all).
        resolved_steps, _ = await _resolve_plan_steps_for_display(collected, ctx)
        m = _PLAN_STEP_NUMBER_RE.match(stripped)
        n = int(m.group(1)) if m else 0
        if not m or n < 1 or n > len(resolved_steps):
            return {PLAN_STEP_EDIT_ERROR_FIELD.key: (
                f"I didn't understand that.\n\n"
                f"Reply with a step number from 1 to {len(resolved_steps)}, "
                f"or type CANCEL to leave editing."
            )}
        new_collected = dict(collected)
        new_collected[_PLAN_EDIT_STEP_KEY] = str(n)
        await conversation.update_conversation(
            ctx.agent_id, ctx.sender_phone, collected=new_collected, step="editing"
        )
        prompt = _build_plan_step_edit_prompt_from_resolved(resolved_steps, n)
        return {PLAN_STEP_EDIT_ERROR_FIELD.key: prompt}

    # Sub-state 2: EDITING ONE STEP ("EDITING STEP N — KIND" was just
    # shown). The free-text reply is an instruction for THAT step only.
    try:
        step_index = int(step_index_raw)
    except ValueError:
        step_index = 0
    if step_index < 1:
        return {PLAN_STEP_EDIT_ERROR_FIELD.key: "Something went wrong — type CANCEL and try again."}

    instruction = text
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    pending = (session or {}).get("pending_disambiguation")
    if pending and pending.get("kind") == "plan_missing_project":
        # Guided Project Selection (2026-09-03) — a pending
        # "plan_missing_project" disambiguation means we're SPECIFICALLY
        # waiting for a project (either the ADD step's own missing
        # project, or an in-progress "change the project" edit on any
        # step): "MORE" pages through the FULL catalogue (reusing the
        # same "next page" recognizer Conversational Talent Search
        # already uses), a bare number picks from the shown page,
        # otherwise the raw reply IS the project name/query itself —
        # never a generic "change X to Y" instruction, since nothing
        # about this specific field has resolved yet for the user to be
        # referring back to.
        page = nlu.extract_talent_search_pagination(stripped)
        if page and page.get("action") == "next":
            projects = await _fetch_ongoing_projects()
            offset = int(pending.get("offset") or 0) + _PROJECT_CHOICE_PAGE_SIZE
            if offset >= len(projects):
                offset = 0  # wrap — "MORE" can never dead-end the conversation
            header = pending.get("header") or "ADD needs a project before I can continue."
            text_out, options = _format_project_choice(projects, offset, header)
            await session_context.update_session(
                AGENT_ID, ctx.sender_phone,
                pending_disambiguation={
                    "kind": "plan_missing_project", "field_key": _PLAN_EDIT_STEP_KEY,
                    "options": options, "offset": offset, "header": header,
                },
            )
            return {PLAN_STEP_EDIT_ERROR_FIELD.key: text_out}

        options = pending.get("options") or []
        idx = nlu.resolve_option_reply(stripped, options) if options else None
        project_text = options[idx - 1]["value"] if idx is not None else stripped
        instruction = f"change the project to {project_text}"
        await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)

    new_plan_json, err = await _apply_plan_step_edit_instruction(collected, ctx, step_index, instruction)
    if err:
        return {PLAN_STEP_EDIT_ERROR_FIELD.key: err}
    # Clear _PLAN_EDIT_STEP_KEY now that this step's edit succeeded and
    # we're heading back to "confirming" — see PLAN_EDIT_STEP_FIELD's
    # docstring for why leaving it set would misroute the NEXT "2" reply.
    return {PLAN_FIELD.key: new_plan_json, _PLAN_EDIT_STEP_KEY: ""}


async def _plan_step_editing_claims_reply(
    text: str, collected: Dict[str, str], ctx: ExecContext,
) -> bool:
    """IntentDefinition.claims_editing_reply hook (see agents/models.py) —
    shared by ADD_INTENT/MOVE_INTENT. Called ONLY when a conversation is
    mid- "edit ONE specific plan step" (_PLAN_EDIT_STEP_KEY set — the
    "selecting which step" sub-state never needs this: its only valid
    replies are bare numbers/CANCEL, none of which collide with a
    trigger word) AND the incoming message would otherwise be treated as
    a fresh trigger. Pure, side-effect-free pattern matching against the
    SAME instruction shapes _apply_plan_step_edit_instruction actually
    understands — deliberately NOT a blanket "any editing turn is
    immune" grant: a message that doesn't match any recognized edit
    instruction (e.g. a genuinely new, unrelated compound command)
    returns False, so dispatcher.py's normal "a fresh trigger always
    restarts" rule still applies to it exactly as before this feature
    existed."""
    if not (collected.get(_PLAN_EDIT_STEP_KEY) or "").strip():
        return False
    if not (collected.get(PLAN_FIELD.key) or "").strip():
        return False
    stripped = (text or "").strip()
    if not stripped:
        return False
    # Guided Project Selection — while a project clarification is
    # pending, ANY non-empty reply is meaningful (a number, "MORE", or
    # the project's own name/query, which could itself start with a
    # trigger word by pure coincidence).
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    pending = (session or {}).get("pending_disambiguation")
    if pending and pending.get("kind") == "plan_missing_project":
        return True
    if _PLAN_STEP_NUMBER_RE.match(stripped) or _EDIT_CANCEL_RE.match(stripped):
        return True
    return bool(
        _EDIT_STAGE_RE.match(stripped) or _EDIT_PROJECT_RE.match(stripped)
        or _EDIT_PROJECT_BARE_RE.match(stripped) or _EDIT_CHANGE_ADD_TALENTS_RE.match(stripped)
        or _EDIT_REMOVE_STEP_RE.match(stripped) or _EDIT_REMOVE_NAME_RE.match(stripped)
        or _EDIT_MOVE_ONLY_RE.match(stripped) or _EDIT_SHARE_WITH_RE.match(stripped)
    )


async def _execute_plan(collected: dict, ctx: ExecContext) -> ExecResult:
    """Runs every step SEQUENTIALLY — each wrapped in its own try/except
    so one failing/ambiguous step never aborts the rest — and returns one
    combined summary in the exact ✓/✗ format specified.

    Combined Casting Pipeline + WhatsApp Automation (2026-08-19) — a step
    carrying send_template (see _strip_send_template_markers) accumulates
    its successful casting.move outcomes into group_send_data as it goes;
    at each group boundary (and once more after the loop, for the final
    group) that group's accumulated data is flushed to an actual WhatsApp
    send via _flush_group_send — always AFTER every add/move write for
    that group has already been attempted, per the required ordering. A
    talent/project pair whose move step errored or whose add/move
    resolution failed is never added to group_send_data, so it can never
    receive the WhatsApp message for that failed operation (see
    _flush_group_send's own docstring)."""
    steps = _deserialize_plan(collected.get(PLAN_FIELD.key))
    summary_lines = ["Completed", ""]
    any_success = False
    touched_pairs: List[Dict[str, str]] = []
    # Never reset at a group boundary — see _resolve_plan_steps_for_
    # display's identical flag and _resolve_one_plan_segment's docstring.
    plan_resolved_flag: List[bool] = [False]
    last_group: Optional[Any] = None
    group_send_template: Optional[str] = None
    group_send_data: Dict[str, _GroupSendBucket] = {}
    pending_send_fields: Optional[Dict[str, str]] = None

    for step in steps:
        group = step.get("group", 0)
        if last_group is not None and group != last_group:
            # See _build_plan_confirmation's identical reset — must stay
            # in sync with it (same grouping, same fan-out boundary).
            touched_pairs = []
            await _flush_group_send(summary_lines, group_send_template, group_send_data, is_dry_run=False)
            group_send_template, group_send_data = None, {}
        last_group = group
        if step.get("send_template"):
            group_send_template = step["send_template"]

        # Compound Actions (2026-08-27) — see _build_plan_confirmation's
        # identical branch.
        if step.get("intent_id") == "casting.share":
            try:
                share_res = await _resolve_share_step_for_plan(step.get("raw_text") or "", touched_pairs)
            except Exception:
                logger.exception("plan share step resolution failed raw_text=%r", step.get("raw_text"))
                summary_lines += [f"✗ {step.get('raw_text', 'share')}", "", "Something went wrong resolving this step.", ""]
                continue
            if not share_res.ok:
                summary_lines += [f"✗ Share — {step.get('raw_text', '')}", "", share_res.error or "Could not resolve this step.", ""]
                continue
            try:
                body_lines, total_queued = await _run_share_sends(share_res)
            except Exception:
                logger.exception("plan share step send failed raw_text=%r", step.get("raw_text"))
                summary_lines += [f"✗ Share {share_res.template_label}", "", "Something went wrong sending this.", ""]
                continue
            summary_lines += [
                f"✓ Share {share_res.template_label}",
            ] + body_lines + [
                "", f"{total_queued} WhatsApp message{'' if total_queued == 1 else 's'} queued.", "",
            ]
            any_success = any_success or total_queued > 0
            continue

        if step.get("intent_id") == "casting.send":
            # Deliberately NOT executed here — casting.send has its own
            # multi-step, admin-approval-gated workflow (build form → show
            # → allow edits → explicit approval → freeze → send) that must
            # never be bypassed just because it arrived as the tail of a
            # compound command (see SEND_INTENT/_build_send_confirmation/
            # _send_executor, all unchanged). The LAST such step in the
            # plan wins (there is normally only one); handed off to a
            # fresh casting.send conversation once every other step has
            # run — see the hand-off after this loop.
            pending_send_fields = _resolve_send_step_for_plan(step.get("raw_text") or "", touched_pairs)
            continue

        try:
            sub_steps = await _resolve_one_plan_step(step, ctx, touched_pairs, plan_resolved_flag)
        except Exception:
            logger.exception("plan step resolution failed raw_text=%r", step.get("raw_text"))
            summary_lines += [f"✗ {step.get('raw_text', 'step')}", "", "Something went wrong resolving this step.", ""]
            continue

        for rs in sub_steps:
            label = rs["label"] or rs["raw_text"]
            if rs["resolved"] is None:
                summary_lines += [f"✗ {label}", "", rs["error"] or "Could not resolve this step.", ""]
                continue
            r = rs["resolved"]
            try:
                if rs["intent_id"] == "casting.add":
                    split = await _split_by_existing_membership(r)
                    if not split.actionable_ids:
                        summary_lines += [f"✗ {label}", "", "Already in this pipeline.", ""]
                        continue
                    result = await _timed_write(
                        add_talents_to_pipeline(r.project_id, split.actionable_ids, "ask_to_test")
                    )
                    added = result["added"]
                    summary_lines += [
                        f"✓ {label}", "", f"{added} talent{'' if added == 1 else 's'} added", "",
                    ]
                    any_success = True
                else:
                    split = await _split_by_current_stage(r)
                    if not split.actionable_ids:
                        summary_lines += [f"✗ {label}", "", "Already in that stage.", ""]
                        if step.get("send_template"):
                            # Already at the target stage — the pipeline
                            # objective IS satisfied, just not by a fresh
                            # write this turn — still send-eligible.
                            bucket = group_send_data.setdefault(
                                r.project_id, _GroupSendBucket(project_label=r.project_label)
                            )
                            for tid, tl in zip(r.talent_ids, r.talent_labels):
                                if tid not in bucket.talent_ids:
                                    bucket.talent_ids.append(tid)
                                    bucket.talent_labels.append(tl)
                        continue
                    write_result = await _timed_write(
                        bulk_move_by_talent_ids(r.project_id, split.actionable_ids, r.target_stage)
                    )
                    moved = write_result["moved"]
                    # Compound-plan UNDO (2026-08-27) — a MOVE step inside a
                    # plan (Add,Move / Add,Move,Send) is the exact same
                    # pipeline write _move_executor makes standalone, so it
                    # records undo the exact same way: same undo_store, same
                    # dict shape, same 5-minute TTL. Only ever called here
                    # once the write above has actually succeeded (moved
                    # talent_ids is non-empty) — a step that errored or found
                    # "already in that stage" never reaches this line, so no
                    # misleading undo record is ever created for a partial or
                    # no-op step. The compound ADD half of this same plan is
                    # deliberately NOT given its own undo record — reverting
                    # "added to the pipeline" (removing the row entirely) is
                    # a materially different operation from "reverting a
                    # stage", which is all undo_store/casting.undo currently
                    # model; scope stays exactly what the existing MOVE undo
                    # already covers. A plan's own optional "send" leg (a
                    # WhatsApp campaign message) has no undo concept in this
                    # system at all and is untouched by this change.
                    plan_operation_id = str(uuid.uuid4())
                    await undo_store.store_undo(
                        AGENT_ID, ctx.sender_phone,
                        {
                            "operation_id": plan_operation_id,
                            "project_id": r.project_id,
                            "project_label": r.project_label,
                            "new_stage": r.target_stage,
                            "previous_stage_by_id": split.previous_stage_by_id,
                            "approved_by": ctx.sender_phone,
                            "approved_by_name": ctx.sender_name,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                        ttl_minutes=UNDO_WINDOW_MINUTES,
                    )
                    summary_lines += [
                        f"✓ {label}", "", f"{moved} talent{'' if moved == 1 else 's'} moved", "",
                        f"Reply UNDO within {UNDO_WINDOW_MINUTES} minutes to restore the previous stage.", "",
                    ]
                    any_success = True
                    if step.get("send_template"):
                        bucket = group_send_data.setdefault(
                            r.project_id, _GroupSendBucket(project_label=r.project_label)
                        )
                        for tid, tl in zip(r.talent_ids, r.talent_labels):
                            if tid not in bucket.talent_ids:
                                bucket.talent_ids.append(tid)
                                bucket.talent_labels.append(tl)
            except Exception:
                logger.exception("plan step execution failed label=%r", label)
                summary_lines += [f"✗ {label}", "", "Something went wrong executing this step.", ""]

    await _flush_group_send(summary_lines, group_send_template, group_send_data, is_dry_run=False)

    if pending_send_fields is None:
        return ExecResult(ok=any_success, message="\n".join(summary_lines).rstrip())

    if pending_send_fields.get("error"):
        # P0 fix (2026-08-30) — a singular pronoun ("her"/"him") that
        # can't be safely resolved to exactly one touched talent STOPS
        # here and asks, rather than silently guessing/merging (see
        # _resolve_plan_pronoun_talents). Every other step in the plan
        # has still already run — only the SEND leg is withheld.
        summary_lines += ["", f"✗ Send — {pending_send_fields['error']}"]
        return ExecResult(ok=any_success, message="\n".join(summary_lines).rstrip())

    # Compound Actions SEND hand-off (2026-08-27) — everything else in
    # this plan has run; now hand off into casting.send's OWN, completely
    # unmodified conversation flow (build form → show → allow edits →
    # explicit approval → freeze → send), by rendering its confirmation
    # card directly and asking dispatcher.py to open a fresh casting.send
    # conversation instead of clearing this one (see ExecResult.
    # next_conversation / agents/dispatcher.py's _clear_or_handoff — the
    # exact same reply the user would get from a fresh "send - Talent -
    # Project" command). Never auto-approved: the next "1" the admin
    # sends is a genuine, distinct approval of THIS card, not inherited
    # from the plan's own "1".
    send_collected = dict(pending_send_fields)
    try:
        send_card = await _build_send_confirmation(send_collected, ctx)
    except Exception:
        logger.exception("plan send hand-off build_confirmation failed fields=%r", pending_send_fields)
        summary_lines += ["", "✗ Send — something went wrong preparing the send form."]
        return ExecResult(ok=any_success, message="\n".join(summary_lines).rstrip())

    summary_lines += ["", send_card]
    return ExecResult(
        ok=any_success, message="\n".join(summary_lines).rstrip(),
        next_conversation={"intent_id": "casting.send", "collected": send_collected},
    )


async def _move_try_auto_execute(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    if (collected.get("talent_selector") or "").startswith(nlu.SELECTION_CMD_MARKER):
        # Phase 2 selection shorthand ("Select 1,3,5") arriving via
        # casting.move's own "select" trigger — a session-only basket
        # mutation, never a real pipeline write, so it always fires here
        # immediately regardless of _auto_confirm (unlike every other
        # branch in this function, which only auto-executes when the
        # user explicitly chained "and confirm").
        return await _handle_move_selection_shorthand(collected, ctx)
    if collected.get(PLAN_FIELD.key):
        if not collected.get(AUTO_CONFIRM_FIELD.key):
            return None
        return await _execute_plan(collected, ctx)
    if not collected.get(AUTO_CONFIRM_FIELD.key):
        return None
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    _resolved, err, _dis = await _resolve_move_selection(collected, session)
    if err:
        # Still ambiguous/erroring — fall through to the normal
        # confirmation flow, which sets up pending_disambiguation +
        # "editing"-step continuation as usual. Since _auto_confirm stays
        # in the persisted `collected` across that continuation, THIS
        # check re-fires on the next turn once the ambiguity resolves,
        # and auto-executes then — no extra state needed.
        return None
    return await _move_executor(collected, ctx)


async def _build_move_confirmation(collected: dict, ctx: ExecContext) -> str:
    if (collected.get("talent_selector") or "").startswith(nlu.SELECTION_CMD_MARKER):
        # Defensive — _move_try_auto_execute always intercepts this
        # sentinel first via the primary dispatch path, so this branch is
        # normally unreachable; kept as a safety net against the
        # Concurrent Task Engine's independent dispatch path.
        result = await _handle_move_selection_shorthand(collected, ctx)
        return result.message
    if collected.get(PLAN_FIELD.key):
        return await _build_plan_confirmation(collected, ctx)

    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    resolved, err, disambiguation = await _resolve_move_selection(collected, session)

    if err:
        if disambiguation:
            # A talent/project name, or "isn't part of this project",
            # needs a short clarifying reply — store what we're waiting to
            # hear back and switch this conversation into the "editing"
            # step (see _move_parse_edits_async) so the user's VERY NEXT
            # reply (a bare number, or "yes") resolves it and the move
            # continues, without repeating the whole command. This
            # overrides the "confirming" step the generic engine has
            # already committed to for this turn — there is nothing to
            # approve/edit/cancel yet, so 1/2/3 semantics would be wrong.
            await session_context.update_session(
                AGENT_ID, ctx.sender_phone, pending_disambiguation=disambiguation
            )
            await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, step="editing")
        else:
            await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
        return err

    # Resolved cleanly — clear any stale pending clarification so a LATER,
    # unrelated "editing" turn (for a totally different reason) can't be
    # misread as answering an already-settled disambiguation.
    await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
    await _remember_last_talent(ctx, resolved)

    split = await _split_by_current_stage(resolved)
    if not split.actionable_ids:
        if len(resolved.talent_ids) == 1:
            return f"{resolved.talent_labels[0]} is already in {nlu.stage_label(resolved.target_stage)}.\n\nNo changes were made."
        return "Nothing to move."

    with request_scope.stage("response_formatting"):
        from_label = ", ".join(nlu.stage_label(s) for s in split.from_stages) or "—"
        lines = [
            "Project",
            resolved.project_label,
            "",
            "Pipeline",
            from_label,
            "",
            "You are about to move",
            "",
        ]
        lines.extend(f"• {name}" for name in split.actionable_labels)
        if split.already_labels:
            lines.append("")
            lines.append(f"(already in {nlu.stage_label(resolved.target_stage)}, skipped: {', '.join(split.already_labels)})")
        lines.append("")
        lines.append("To")
        lines.append("")
        lines.append(nlu.stage_label(resolved.target_stage))
        lines.append("")
        lines.append("Reply:")
        lines.append("1 → Approve")
        lines.append("2 → Edit")
        lines.append("3 → Cancel")
        return "\n".join(lines)


async def _move_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    if (collected.get("talent_selector") or "").startswith(nlu.SELECTION_CMD_MARKER):
        # Defensive — same safety net as _build_move_confirmation above.
        return await _handle_move_selection_shorthand(collected, ctx)
    if collected.get(PLAN_FIELD.key):
        return await _execute_plan(collected, ctx)

    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    resolved, err, _disambiguation = await _resolve_move_selection(collected, session)
    if err:
        # By the time a real confirmation was shown and approved, any
        # ambiguity should already have been resolved — if resolution
        # somehow still fails here (e.g. underlying data changed in the
        # last few seconds), fail cleanly rather than trying to restart a
        # clarification sub-flow mid-approval.
        return ExecResult(ok=False, error="move_resolution_failed", message=err)

    await _remember_last_talent(ctx, resolved)

    # A real move (not the Phase 2 selection shorthand, intercepted above)
    # is an unrelated workflow — PART 10's session-reset rule, same as
    # every other query/move/add success path in this file.
    await session_context.update_session(AGENT_ID, ctx.sender_phone, selection_basket=None)

    if collected.get("project_query"):
        # An explicit project named in a natural-language move ("... in
        # Toyota Glanza") becomes the active project for whatever comes
        # next, same as naming one in a query would — a partial $set, so
        # this does NOT touch number_map/current_stage (the previous
        # sprint's stability guarantee: only "Show <Pipeline>" is allowed
        # to regenerate the displayed-list mapping).
        await session_context.update_session(
            AGENT_ID, ctx.sender_phone,
            current_project_id=resolved.project_id,
            current_project_label=resolved.project_label,
        )

    # Re-checked fresh here (not trusting whatever _build_move_confirmation
    # saw) — the only thing that's allowed to have changed between confirm
    # and approve is each talent's current stage, and that's exactly what
    # this re-derives.
    split = await _split_by_current_stage(resolved)

    if not split.actionable_ids:
        if len(resolved.talent_ids) == 1:
            return ExecResult(
                ok=False, error="already_in_stage",
                message=f"{resolved.talent_labels[0]} is already in {nlu.stage_label(resolved.target_stage)}.\n\nNo changes were made.",
            )
        return ExecResult(ok=False, error="nothing_to_move", message="Nothing to move.")

    before_counts = await _timed_aggregation(get_stage_counts(resolved.project_id))
    write_result = await _timed_write(
        bulk_move_by_talent_ids(resolved.project_id, split.actionable_ids, resolved.target_stage)
    )
    after_counts = await _timed_aggregation(get_stage_counts(resolved.project_id))

    operation_id = str(uuid.uuid4())
    await undo_store.store_undo(
        AGENT_ID, ctx.sender_phone,
        {
            "operation_id": operation_id,
            "project_id": resolved.project_id,
            "project_label": resolved.project_label,
            "new_stage": resolved.target_stage,
            # {talent_id: previous_stage} — a single move can pull talents
            # out of several different stages at once; undo restores each
            # one to ITS OWN previous stage, not one shared value.
            "previous_stage_by_id": split.previous_stage_by_id,
            "approved_by": ctx.sender_phone,
            "approved_by_name": ctx.sender_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        ttl_minutes=UNDO_WINDOW_MINUTES,
    )

    with request_scope.stage("response_formatting"):
        moved = write_result["moved"]
        lines = [
            "Done.",
            "",
            "Project",
            resolved.project_label,
            "",
            f"Moved {moved} talent{'' if moved == 1 else 's'}.",
            "",
        ]
        lines.extend(f"• {name}" for name in split.actionable_labels)
        if split.already_labels:
            lines.append("")
            lines.append(f"({len(split.already_labels)} already in {nlu.stage_label(resolved.target_stage)} — skipped)")
        lines.append("")
        for stage in split.from_stages:
            lines.append(nlu.stage_label(stage))
            lines.append(f"{before_counts.get(stage, 0)} → {after_counts.get(stage, 0)}")
            lines.append("")
        lines.append(nlu.stage_label(resolved.target_stage))
        lines.append(f"{before_counts.get(resolved.target_stage, 0)} → {after_counts.get(resolved.target_stage, 0)}")
        lines.append("")
        lines.append(f"Operation ID: {operation_id}")
        lines.append("")
        lines.append(f"Reply UNDO within {UNDO_WINDOW_MINUTES} minutes to restore the previous state.")

    # Audit-only enrichment: `collected` is the SAME dict object the
    # dispatcher logs as `parsed_fields` for this exact turn right after
    # this executor returns (see agents/dispatcher.py's approve branch) —
    # mutating it in place is the only way to get resolved project/talent/
    # stage detail into the audit trail without changing audit.py's fixed
    # schema or the generic engine's domain-agnostic approve branch.
    collected["project"] = resolved.project_label
    collected["target_stage_label"] = nlu.stage_label(resolved.target_stage)
    collected["from_stages"] = [nlu.stage_label(s) for s in split.from_stages]
    collected["talents_moved"] = split.actionable_labels[:50]
    collected["operation_id"] = operation_id

    return ExecResult(
        ok=True,
        message="\n".join(lines).rstrip(),
        data={
            "project_id": resolved.project_id,
            "target_stage": resolved.target_stage,
            "talent_ids": split.actionable_ids,
            "from_stages": split.from_stages,
            "moved": moved,
            "operation_id": operation_id,
        },
    )


async def _move_parse_edits_async(
    text: str, collected: Dict[str, str], fields: List[FieldSpec], ctx: ExecContext
) -> Dict[str, str]:
    """Interprets an "editing"-step reply while a clarification is
    pending (see _build_move_confirmation): a bare number picks that
    option from the stored disambiguation list, and "yes"/"go ahead"/etc.
    accepts a "search all active projects instead?" offer — either way,
    the move continues from where it left off, without the user repeating
    the original command. Falls back to the generic "Key = value" syntax,
    and finally — if a clarification is pending but the reply matched
    neither of the above — treats the raw reply as a more specific retry
    of whichever field was unclear (e.g. retyping a fuller name)."""
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    pending = (session or {}).get("pending_disambiguation")
    stripped = (text or "").strip()

    if pending:
        kind = pending.get("kind")
        field_key = pending.get("field_key")
        options = pending.get("options") or []

        if kind == "retry_global":
            if parse_confirmation_reply(stripped) == "approve":
                await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
                return {"project_query": nlu.FORCE_GLOBAL_MARKER}
        elif field_key and options:
            # A number, an ordinal word ("the third one", "last"), or a
            # free-text match against the option's own label ("Main Guy",
            # "Bajaj Pulsar - Main Guy") — see resolve_option_reply for the
            # full escalation. Returns None (never guesses) rather than
            # picking a close-but-not-clearly-unique option.
            idx = nlu.resolve_option_reply(stripped, options)
            if idx is not None:
                await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
                return {field_key: options[idx - 1]["value"]}
            if kind == "talent":
                # Multi-pick fallback ("1 and 3", "Thakur and Singh") —
                # ONLY for a talent-selector field (the only field shape
                # that can legitimately hold more than one resolved value)
                # and only tried after the single-pick resolver above
                # already failed, so every existing single-pick reply is
                # unaffected. Substitutes the picked options' plain LABEL
                # text (not their encoded RESOLVED_TALENT_MARKER "value")
                # joined by ", " — parse_talent_selector already splits a
                # comma-separated list into independent name_queries and
                # exact-matches each on the next pass, so no changes to
                # the selector grammar itself are needed.
                multi_idx = nlu.resolve_option_reply_multi(stripped, options)
                if multi_idx and len(multi_idx) > 1:
                    await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
                    labels = ", ".join(options[i - 1]["label"] for i in multi_idx)
                    return {field_key: labels}

    explicit = parse_edit_instructions(text, fields)
    if explicit:
        return explicit

    if pending and pending.get("field_key") and pending.get("kind") != "retry_global" and stripped:
        return {pending["field_key"]: stripped}

    return {}


_STAGE_MOVE_EXCLUDE_RE = re.compile(r"^\s*(?:exclude|skip|remove)\s+(.+)$", re.IGNORECASE)


async def _stage_move_handle_confirming_reply(
    raw_text: str, collected: Dict[str, str], ctx: ExecContext,
) -> Optional[str]:
    """Whole-Stage Move (2026-08-20) — "Exclude X"/"Skip X" typed directly
    on top of an already-shown stage-move confirmation card excludes that
    talent (or comma/"and"-separated list of talents) from the bulk set
    and re-shows the (now smaller) card, without leaving the "confirming"
    step — mirrors the WhatsApp Campaign Agent's own Interactive Campaign
    Editing pattern (Exclude/Include on top of a send preview). Returns
    None (falls through to the generic "1 Approve / 2 Edit / 3 Cancel"
    handling) for every other turn, including every ordinary named-talent
    move confirmation — this only ever fires when a stage-move is the one
    currently pending."""
    selector = collected.get("talent_selector") or ""
    if not selector.startswith(nlu.STAGE_MOVE_MARKER):
        return None
    m = _STAGE_MOVE_EXCLUDE_RE.match(raw_text or "")
    if not m:
        return None

    payload_raw = selector[len(nlu.STAGE_MOVE_MARKER):]
    try:
        payload = json.loads(payload_raw) if payload_raw else {}
    except (ValueError, TypeError):
        payload = {}
    from_stage = payload.get("from_stage") or ""
    already_excluded = set(payload.get("excluded_ids") or [])

    project_query = (collected.get("project_query") or "").strip()
    projects = await _fetch_ongoing_projects()
    with request_scope.stage("fuzzy"):
        pmatch = nlu.resolve_project_by_name(project_query, projects)
    if not pmatch.project:
        return None  # shouldn't happen — the card wouldn't have rendered otherwise

    candidates = await _fetch_stage_candidates(pmatch.project["id"], from_stage)
    live_candidates = [c for c in candidates if c.id not in already_excluded]

    names = nlu.split_multi_names(m.group(1)) or [m.group(1)]
    newly_excluded_ids: List[str] = []
    not_found: List[str] = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        with request_scope.stage("fuzzy"):
            resolved = nlu.resolve_against_candidates(
                nlu.SelectorResult(ok=True, name_query=name), live_candidates,
            )
        if resolved.ok and len(resolved.talent_ids) == 1:
            newly_excluded_ids.append(resolved.talent_ids[0])
        else:
            not_found.append(name)

    if not newly_excluded_ids:
        return f"Couldn't find {', '.join(not_found)} in this list — reply with the exact name, or 1/2/3."

    payload["excluded_ids"] = list(already_excluded | set(newly_excluded_ids))
    new_collected = dict(collected)
    new_collected["talent_selector"] = nlu.STAGE_MOVE_MARKER + json.dumps(payload)
    await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, collected=new_collected)
    card = await _build_move_confirmation(new_collected, ctx)
    if not_found:
        card = f"Couldn't find: {', '.join(not_found)}\n\n{card}"
    return card


async def _build_move_edit_prompt(collected: dict, ctx: ExecContext) -> str:
    """Guided Edit Prompts (2026-08-28) — connects "2" to the SPECIFIC
    pending move instead of a generic "tell me what to change". Reads the
    same collected talent_selector/project_query/target_stage text
    _build_move_confirmation already resolved; no new resolution."""
    if collected.get(PLAN_FIELD.key):
        return await _build_plan_edit_prompt(collected, ctx)

    talent = (collected.get("talent_selector") or "").strip()
    project = (collected.get("project_query") or "").strip()
    stage_raw = (collected.get("target_stage") or "").strip()
    stage = nlu.stage_label(stage_raw) if stage_raw in PIPELINE_STAGES else stage_raw

    lines = ["EDITING MOVE", "", "Current:"]
    if talent:
        lines.append(f"Talent: {talent}")
    if project:
        lines.append(f"Project: {project}")
    if stage:
        lines.append(f"Stage: {stage}")
    lines += [
        "", "Tell me what you want to change.", "",
        "Examples:",
        "• Move both to Shortlisted",
        "• Remove one of the talents",
        "• Change project to XYZ",
        "", "Nothing will be executed until you confirm.",
    ]
    return "\n".join(lines)


MOVE_INTENT = IntentDefinition(
    intent_id="casting.move",
    triggers=nlu.MOVE_TRIGGERS,
    fields=[
        TALENT_SELECTOR_FIELD, TARGET_STAGE_FIELD, PROJECT_QUERY_FIELD, AUTO_CONFIRM_FIELD, PLAN_FIELD,
        PLAN_STEP_EDIT_ERROR_FIELD, PLAN_EDIT_STEP_FIELD,
    ],
    executor=_move_executor,
    extract_fields=_extract_move_fields,
    build_confirmation=_build_move_confirmation,
    build_edit_prompt=_build_move_edit_prompt,
    parse_edits_async=_plan_aware_parse_edits_async,
    try_auto_execute=_move_try_auto_execute,
    # Unchanged from before the plan-editing feature existed — "2"/"3" on
    # a plan's CONFIRMATION card are now always the ordinary Approve/
    # Edit/Cancel (dispatcher.py's generic parser); the step-selector/
    # step-editor sub-states live entirely inside "editing" via
    # parse_edits_async above, so no confirming-reply interception is
    # needed for plans at all — only Whole-Stage-Move's own "exclude X"
    # interception remains here.
    handle_confirming_reply=_stage_move_handle_confirming_reply,
    claims_editing_reply=_plan_step_editing_claims_reply,
    summary_title="You are about to move:",
)


# ---------------------------------------------------------------------------
# casting.add — "Add <name(s)> to <project>" creates a NEW pipeline entry
# (never moves an existing one) always in Ask To Test; never asks which
# pipeline. Talent resolution is GLOBAL (the whole talents database, not
# scoped to any project's existing pipeline — a brand new addition may
# have no pipeline row anywhere yet), everything else — project
# resolution, disambiguation continuation, fuzzy/typo tolerance — reuses
# the exact same building blocks casting.move already uses.
# ---------------------------------------------------------------------------
ADD_TALENT_SELECTOR_FIELD = FieldSpec(
    key="talent_selector",
    label="Talent(s)",
    question="Who should I add?",
    validate=_validate_selector,
    aliases=["talent", "talents", "who"],
)

ADD_PROJECT_QUERY_FIELD = FieldSpec(
    key="project_query",
    label="Project",
    question=(
        "ADD needs a project.\n\n"
        "Example:\n"
        "ADD Ayra Krishna to Score Condoms\n\n"
        "Nothing has been added yet."
    ),
    validate=_validate_project_query,
    aliases=["project", "for", "in", "to"],
    required=True,  # unlike MOVE's optional project_query — every Add spec
    # example names the project explicitly; a mistaken add is a real DB
    # write into someone's roster, so it's never defaulted from session
    # context, always asked if omitted.
)


def _extract_add_fields(text: str) -> Dict[str, str]:
    # Simplified Command Language (2026-08-17) — see _extract_move_fields'
    # identical comment; same translation, same "pass through unchanged
    # when it isn't the simple grammar" guarantee.
    text = nlu.translate_simple_commands_in_text(text, list(PIPELINE_STAGE_ORDER))

    with request_scope.stage("nlu"):
        chunks, auto_confirm = nlu.preprocess_command_grouped(text)
        chunks, group_send_template = _strip_send_template_markers(chunks)
        out: Dict[str, str] = {}

        if len(chunks) == 1 and not group_send_template:
            group0, raw0 = chunks[0]
            fields = nlu.extract_add_fields(raw0)
            project_names = nlu.split_multi_names(fields.get("project_query") or "") if fields.get("project_query") else []
            if len(project_names) <= 1:
                # UX polish (2026-08-10) — "Add 1,3,5 to X"/"Add first 5 to
                # X" direct shortcut: extract_add_fields already correctly
                # separated the talent_selector from the project tail; if
                # THAT selector looks like a pure selection spec (reusing
                # extract_selection_command's grammar via a synthetic
                # "select " prefix — the exact same shape Select/Remove
                # recognize), re-encode it so _resolve_add_selection
                # resolves it against the current talent-search
                # number_map instead of trying to fuzzy-match it as a name.
                selector_text = fields.get("talent_selector") or ""
                spec_cmd = nlu.extract_selection_command("select " + selector_text)
                if spec_cmd and spec_cmd["action"] == "select":
                    fields["talent_selector"] = nlu.ADD_ORDINAL_SPEC_MARKER + spec_cmd["spec"]
                out.update(fields)
                if auto_confirm:
                    out[AUTO_CONFIRM_FIELD.key] = "1"
                return out
            chunks = [(group0, raw0)]

        # See _extract_move_fields' identical comment on send_template.
        steps = [
            {
                "intent_id": nlu.classify_chunk_intent(c) or "casting.add", "raw_text": c, "group": g,
                "send_template": group_send_template.get(g),
            }
            for g, c in chunks
        ]
        out[PLAN_FIELD.key] = json.dumps(steps)
        out["talent_selector"] = _PLAN_PLACEHOLDER
        out["project_query"] = _PLAN_PLACEHOLDER
        if auto_confirm:
            out[AUTO_CONFIRM_FIELD.key] = "1"
        return out


_ALL_TALENTS_CACHE_KEY = ("all_talent_candidates",)


async def _fetch_all_talent_candidates() -> List[nlu.Candidate]:
    """Every talent in the database (id+name only) — the search space for
    Add, since a talent being added fresh may have no pipeline row in ANY
    project yet, unlike Move's candidates which are always scoped to an
    existing pipeline. Capped at 20000, same cap _fetch_global_candidates
    already uses for its own whole-database-ish scan.

    Request-scoped cache (2026-08-05 latency investigation finding): this
    is an unfiltered, uncached full-collection fetch — before this cache,
    a multi-project cross-product Add ("Add X to A and B") called this
    once PER project pair, re-downloading the identical, non-project-
    specific candidate list every time. Mirrors _fetch_ongoing_projects's
    existing cache pattern exactly: one fetch per turn, discarded at the
    next request_scope.reset(), never stale across turns."""
    found, cached = request_scope.cache_get(_ALL_TALENTS_CACHE_KEY)
    if found:
        with request_scope.op("fetch_all_talent_candidates", "talent_lookup", collection="talents", cache="hit"):
            pass
        return cached
    cursor = db.talents.find({}, {"_id": 0, "id": 1, "name": 1}).limit(20000)
    docs = await _timed_talent_lookup(
        cursor.to_list(20000), collection="talents", name="fetch_all_talent_candidates",
    )
    candidates = [nlu.Candidate(id=d["id"], label=d.get("name") or "Unknown") for d in docs]
    request_scope.cache_set(_ALL_TALENTS_CACHE_KEY, candidates)
    return candidates


@dataclass
class ResolvedAdd:
    project_id: str
    project_label: str
    talent_ids: List[str]
    talent_labels: List[str]
    # Phase 2 (2026-08-10) — set when talent_ids came from the session's
    # selection basket ("Add selected to X") rather than name resolution;
    # tells _add_executor to auto-clear the basket on success (PART 7).
    came_from_basket: bool = False
    # UX polish (2026-08-10) — set when talent_ids came from a direct
    # "Add 1,3,5 to X" ordinal shortcut, resolved fresh against the
    # current talent-search number_map. Deliberately a SEPARATE flag from
    # came_from_basket: this path never reads or writes
    # session.selection_basket at all, so _add_executor must NOT clear it
    # or announce "Selection cleared." — there is no real basket involved,
    # and doing so would silently wipe out an unrelated selection the user
    # might already be building.
    came_from_ordinal_shortcut: bool = False


async def _resolve_add_project(
    project_query: str,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """(project_id, project_label, error_message, disambiguation) — the
    project-resolution half of _resolve_add_selection, factored out so
    both the name-based path and Phase 2's basket path share the exact
    same resolution/ambiguity/suggestion handling (never duplicated)."""
    if not project_query:
        return None, None, 'Which project? e.g. "Add Prajal Tushir to Toyota Glanza".', None

    projects = await _fetch_ongoing_projects()
    with request_scope.stage("fuzzy"):
        match = nlu.resolve_project_by_name(project_query, projects)
    if match.project:
        return match.project["id"], match.project["label"], None, None
    if match.ambiguous:
        options = [{"label": o["label"], "value": o["label"]} for o in match.ambiguous]
        msg = nlu.format_numbered_options("I found multiple projects.", [[o["label"]] for o in match.ambiguous])
        return None, None, msg, {"kind": "project", "field_key": "project_query", "options": options}
    if match.suggestions:
        bullets = "\n".join(f"• {o['label']}" for o in match.suggestions)
        return None, None, (
            f"I couldn't find a project matching:\n\n{project_query}\n\nDid you mean:\n\n{bullets}"
        ), {"kind": "free_text_retry", "field_key": "project_query", "options": []}
    return None, None, (
        match.error or f'I couldn\'t find a project matching "{project_query}".'
    ), {"kind": "free_text_retry", "field_key": "project_query", "options": []}


async def _resolve_add_selection(
    collected: dict, session: Optional[dict]
) -> Tuple[Optional[ResolvedAdd], Optional[str], Optional[Dict[str, Any]]]:
    """Mirrors _resolve_move_selection's shape (project resolution incl.
    ambiguous/suggestion/free_text_retry payloads; talent resolution incl.
    ambiguous_candidates payload) but against the global talent candidate
    set instead of a project's pipeline rows, and with no stage/ordinal/
    number_map concept at all — Add never has a "displayed list" to index
    into (except Phase 2's selection basket, see selector.use_basket
    below, which arrives with already-known ids, no matching needed)."""
    selector_text = collected.get("talent_selector") or ""
    selector = nlu.parse_talent_selector(selector_text)
    if not selector.ok:
        return None, selector.error, None

    if selector.use_basket:
        items = ((session or {}).get("selection_basket") or {}).get("items") or []
        if not items:
            return None, "No talents are currently selected.\n\nSearch and select talents first.", None
        project_query = (collected.get("project_query") or "").strip()
        project_id, project_label, err, disambiguation = await _resolve_add_project(project_query)
        if err:
            return None, err, disambiguation
        if not await _project_exists(project_id):
            return None, "Project doesn't exist.", None
        return ResolvedAdd(
            project_id=project_id, project_label=project_label,
            talent_ids=[it["id"] for it in items], talent_labels=[it["label"] for it in items],
            came_from_basket=True,
        ), None, None

    if selector.selection_spec is not None:
        # "Add 1,3,5 to X" / "Add first 5 to X" — resolved against the
        # CURRENT talent-search number_map, the exact same resolution
        # Select/Remove use (nlu.resolve_selection_spec). Never touches
        # session.selection_basket.
        number_map = (session or {}).get("number_map") or {}
        if number_map.get("type") != "talent_search":
            return None, 'No active talent search. Try "Show female models from Mumbai" first.', None
        items_by_ordinal = {it["ordinal"]: it for it in number_map.get("items") or []}
        ordinals, err = nlu.resolve_selection_spec(selector.selection_spec, list(items_by_ordinal.keys()))
        if err:
            return None, err, None
        project_query = (collected.get("project_query") or "").strip()
        project_id, project_label, err, disambiguation = await _resolve_add_project(project_query)
        if err:
            return None, err, disambiguation
        if not await _project_exists(project_id):
            return None, "Project doesn't exist.", None
        return ResolvedAdd(
            project_id=project_id, project_label=project_label,
            talent_ids=[items_by_ordinal[o]["id"] for o in ordinals],
            talent_labels=[items_by_ordinal[o]["label"] for o in ordinals],
            came_from_ordinal_shortcut=True,
        ), None, None

    if selector.ordinals or selector.everyone:
        # No numbered listing exists for Add to index into — defensive,
        # not reachable via extract_add_fields's own grammar.
        return None, 'Please name who to add, e.g. "Add Prajal Tushir to Toyota Glanza".', None

    project_query = (collected.get("project_query") or "").strip()
    project_id, project_label, err, disambiguation = await _resolve_add_project(project_query)
    if err:
        return None, err, disambiguation

    candidates, project_ok = await asyncio.gather(
        _fetch_all_talent_candidates(),
        _project_exists(project_id),
    )
    if not project_ok:
        return None, "Project doesn't exist.", None

    with request_scope.stage("fuzzy"):
        resolved = nlu.resolve_against_candidates(selector, candidates)
    _log_talent_resolve_timing(resolved)
    if not resolved.ok:
        if resolved.ambiguous_candidates:
            options = [
                {"id": c.id, "label": c.label, "value": f"{nlu.RESOLVED_TALENT_MARKER}{c.id}|{c.label}"}
                for c in resolved.ambiguous_candidates
            ]
            return None, resolved.error, {"kind": "talent", "field_key": "talent_selector", "options": options}
        if resolved.error == "No matching talent.":
            return None, "No matching talent found.", {
                "kind": "free_text_retry", "field_key": "talent_selector", "options": [],
            }
        return None, resolved.error, {"kind": "free_text_retry", "field_key": "talent_selector", "options": []}

    return ResolvedAdd(
        project_id=project_id, project_label=project_label,
        talent_ids=resolved.talent_ids, talent_labels=resolved.talent_labels,
    ), None, None


@dataclass
class SplitAdd:
    actionable_ids: List[str]
    actionable_labels: List[str]
    already_labels: List[str]


async def _split_by_existing_membership(resolved: ResolvedAdd) -> SplitAdd:
    """Live check: which of the resolved talents already have a pipeline
    row (in ANY stage) for this project right now? Those are reported,
    never silently duplicated — mirrors MOVE's _split_by_current_stage."""
    rows = await _timed_talent_lookup(
        db.casting_pipeline.find(
            {"project_id": resolved.project_id, "talent_id": {"$in": resolved.talent_ids}},
            {"_id": 0, "talent_id": 1},
        ).to_list(len(resolved.talent_ids)),
        collection="casting_pipeline", name="split_by_existing_membership",
    )
    existing_ids = {r["talent_id"] for r in rows}
    actionable_ids: List[str] = []
    actionable_labels: List[str] = []
    already_labels: List[str] = []
    for tid, label in zip(resolved.talent_ids, resolved.talent_labels):
        if tid in existing_ids:
            already_labels.append(label)
        else:
            actionable_ids.append(tid)
            actionable_labels.append(label)
    return SplitAdd(actionable_ids, actionable_labels, already_labels)


async def _build_add_confirmation(collected: dict, ctx: ExecContext) -> str:
    if collected.get(PLAN_FIELD.key):
        return await _build_plan_confirmation(collected, ctx)

    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    resolved, err, disambiguation = await _resolve_add_selection(collected, session)

    if err:
        if disambiguation:
            await session_context.update_session(
                AGENT_ID, ctx.sender_phone, pending_disambiguation=disambiguation
            )
            await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, step="editing")
        else:
            await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
        return err

    await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
    await _remember_last_talent(ctx, resolved)

    split = await _split_by_existing_membership(resolved)
    if not split.actionable_ids:
        if len(resolved.talent_ids) == 1:
            return (
                f"{resolved.talent_labels[0]} is already in the {resolved.project_label} pipeline."
                "\n\nNo changes were made."
            )
        return "Everyone named is already in this pipeline.\n\nNo changes were made."

    lines = ["You are about to add", ""]
    if len(split.actionable_labels) == 1:
        lines.append(split.actionable_labels[0])
    else:
        lines.extend(f"• {name}" for name in split.actionable_labels)
    if split.already_labels:
        lines.append("")
        lines.append(f"(already in this pipeline, skipped: {', '.join(split.already_labels)})")
    lines += [
        "", "to", "", resolved.project_label, "",
        "Pipeline:", "Ask To Test", "",
        "Reply:", "1 → Approve", "2 → Edit", "3 → Cancel",
    ]
    return "\n".join(lines)


async def _add_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    if collected.get(PLAN_FIELD.key):
        return await _execute_plan(collected, ctx)

    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    resolved, err, _disambiguation = await _resolve_add_selection(collected, session)
    if err:
        return ExecResult(ok=False, error="add_resolution_failed", message=err)

    await _remember_last_talent(ctx, resolved)
    if not resolved.came_from_basket and not resolved.came_from_ordinal_shortcut:
        # A real, name-based add is an unrelated workflow — PART 10's
        # session-reset rule. The basket-add path clears it separately,
        # paired with the "Selection cleared." confirmation (PART 7). The
        # ordinal-shortcut path ("Add 1,3,5 to X") never touches
        # selection_basket in either direction — it must NOT be swept up
        # by this reset, or it would silently wipe out an unrelated, real
        # selection the user might already be building.
        await session_context.update_session(AGENT_ID, ctx.sender_phone, selection_basket=None)
    if collected.get("project_query"):
        await session_context.update_session(
            AGENT_ID, ctx.sender_phone,
            current_project_id=resolved.project_id, current_project_label=resolved.project_label,
        )

    split = await _split_by_existing_membership(resolved)
    if not split.actionable_ids:
        if len(resolved.talent_ids) == 1:
            return ExecResult(
                ok=False, error="already_in_pipeline",
                message=(
                    f"{resolved.talent_labels[0]} is already in the {resolved.project_label} pipeline."
                    "\n\nNo changes were made."
                ),
            )
        return ExecResult(ok=False, error="nothing_to_add", message="Everyone named is already in this pipeline.\n\nNo changes were made.")

    result = await _timed_write(
        add_talents_to_pipeline(resolved.project_id, split.actionable_ids, "ask_to_test")
    )
    added = result["added"]

    lines = [
        "Done.", "",
        "Project", resolved.project_label, "",
        f"Added {added} talent{'' if added == 1 else 's'} to Ask To Test.", "",
    ]
    lines.extend(f"• {name}" for name in split.actionable_labels)
    if split.already_labels:
        lines.append("")
        lines.append(f"({len(split.already_labels)} already in this pipeline — skipped)")

    if resolved.came_from_basket:
        # PART 7: auto-clear the selection basket after a successful
        # "Add/Attach selected to X" — the whole point of the basket was
        # this one action, and leaving it populated risks a later,
        # unrelated "Add selected to Y" silently re-adding the same people.
        await session_context.update_session(AGENT_ID, ctx.sender_phone, selection_basket=None)
        lines.append("")
        lines.append("Selection cleared.")

    collected["project"] = resolved.project_label
    collected["talents_added"] = split.actionable_labels[:50]

    return ExecResult(
        ok=True,
        message="\n".join(lines).rstrip(),
        data={
            "project_id": resolved.project_id,
            "talent_ids": split.actionable_ids,
            "added": added,
        },
    )


async def _add_try_auto_execute(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    if collected.get(PLAN_FIELD.key):
        if not collected.get(AUTO_CONFIRM_FIELD.key):
            return None
        return await _execute_plan(collected, ctx)
    if not collected.get(AUTO_CONFIRM_FIELD.key):
        return None
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    _resolved, err, _dis = await _resolve_add_selection(collected, session)
    if err:
        # Still ambiguous/erroring — fall through to the normal
        # confirmation flow; _auto_confirm persists in `collected` across
        # the "editing"-step continuation, so this check re-fires and
        # auto-executes once the ambiguity resolves.
        return None
    return await _add_executor(collected, ctx)


async def _build_add_edit_prompt(collected: dict, ctx: ExecContext) -> str:
    """Guided Edit Prompts (2026-08-28) — connects "2" to the SPECIFIC
    pending add instead of a generic "tell me what to change"."""
    if collected.get(PLAN_FIELD.key):
        return await _build_plan_edit_prompt(collected, ctx)

    talent = (collected.get("talent_selector") or "").strip()
    project = (collected.get("project_query") or "").strip()

    lines = ["EDITING ADD", "", "Current:"]
    if talent:
        lines.append(f"Talent: {talent}")
    if project:
        lines.append(f"Project: {project}")
    lines += [
        "", "Tell me what you want to change.", "",
        "Examples:",
        "• Add another talent",
        "• Remove one of the talents",
        "• Change project to XYZ",
        "", "Nothing will be executed until you confirm.",
    ]
    return "\n".join(lines)


ADD_INTENT = IntentDefinition(
    intent_id="casting.add",
    triggers=nlu.ADD_TRIGGERS,
    fields=[
        ADD_TALENT_SELECTOR_FIELD, ADD_PROJECT_QUERY_FIELD, AUTO_CONFIRM_FIELD, PLAN_FIELD,
        PLAN_STEP_EDIT_ERROR_FIELD, PLAN_EDIT_STEP_FIELD,
    ],
    executor=_add_executor,
    extract_fields=_extract_add_fields,
    build_confirmation=_build_add_confirmation,
    build_edit_prompt=_build_add_edit_prompt,
    try_auto_execute=_add_try_auto_execute,
    # Reused verbatim from MOVE (wrapped by _plan_aware_parse_edits_async
    # — see its docstring — for Guided Step-Specific Editing, 2026-09-02)
    # — it only ever reads session's pending_disambiguation (agent+phone
    # scoped, not intent-specific) and resolves a numbered/ordinal/label
    # reply or a free-text retry against whichever field_key is pending;
    # Add's disambiguation shapes ("kind": "project"/"talent"/
    # "free_text_retry") are the exact same ones MOVE produces, and Add
    # never produces a "retry_global" kind, so that branch simply never
    # fires here.
    parse_edits_async=_plan_aware_parse_edits_async,
    # No handle_confirming_reply — "2"/"3" on a plan's CONFIRMATION card
    # are always the ordinary Approve/Edit/Cancel (dispatcher.py's
    # generic parser); the step-selector/step-editor sub-states live
    # entirely inside "editing" via parse_edits_async above.
    claims_editing_reply=_plan_step_editing_claims_reply,
    summary_title="You are about to add:",
)


# ---------------------------------------------------------------------------
# casting.upload — "upload - Talent - Project" (Media-Assignment Phase 1,
# 2026-08-22). Resolves talent + project (STOP on ambiguity, same
# principle as MOVE/ADD — never guess), then hands off to the bounded,
# on-demand Media-Assignment mechanism (agents/modules/media_assignment.py
# + services/media_assignment_worker.py + whatsapp-worker/mark_scan.py).
#
# auto_confirm=True, but unlike QUERY this is NOT a read-only intent — the
# executor's return message is only an ACK ("Scanning..."), not the final
# result. The actual scan -> validate -> download -> upload sequence runs
# entirely outside this request/response cycle (it can take well over a
# minute for a real video download), driven by the backend orchestrator
# loop; the final UPLOAD COMPLETE/FAILED report is posted back into this
# same casting-agent group later via the normal create_batch() outbound
# path, not as a reply to this specific message. No confirmation gate is
# needed here because nothing is written to WhatsApp or to a submission
# until the orchestrator's own validation (identity, project match,
# ambiguity, resolution-failure checks) passes — see media_assignment.py.
#
# Talent ambiguity/project ambiguity here are a flat STOP with a clear
# message, not an interactive numbered-disambiguation resume (unlike MOVE/
# ADD) — a deliberate Phase 1 simplification: re-issuing "upload - <fuller
# name> - project" is trivial, and building the full disambiguation-resume
# plumbing for a command that's supposed to be rare/on-demand wasn't
# judged worth the complexity for this first cut.
# ---------------------------------------------------------------------------
UPLOAD_TALENT_FIELD = FieldSpec(
    key="talent_selector", label="Talent",
    question="Who should I upload media for?",
    validate=_validate_selector, aliases=["talent", "who"],
)

UPLOAD_PROJECT_FIELD = FieldSpec(
    key="project_query", label="Project",
    question="Which project?",
    validate=_validate_project_query, aliases=["project", "for"],
)


def _extract_upload_fields(text: str) -> Dict[str, str]:
    """"upload - Talent - Project" (hyphen) OR "upload Talent Project"
    (space-separated, 2026-08-25) — its own trigger ("upload"), needing
    zero changes to the add/move-only _ACTION_PREFIX_RE in
    casting_pipeline_nlu.py (confirmed during planning: that regex is
    private to parse_simple_add_move_command, not a generic hook).

    Space-separated form: this extractor is a pure, sync, DB-free
    function (see IntentDefinition.extract_fields's contract) so it
    CANNOT determine here where the talent name ends and the project
    name begins — that requires real database matching. Both fields are
    set to the SAME full remainder text as a marker; _upload_executor
    detects talent_selector == project_query and resolves the actual
    boundary via _resolve_freeform_talent_project (async, DB-aware)
    before doing anything else."""
    _, remainder = nlu._strip_leading_trigger(text or "", ["upload"])
    remainder = (remainder or "").strip()
    if not remainder:
        return {}
    fields = nlu._split_hyphen_fields(remainder, 2)
    if fields:
        talent_part, project_part = fields
        return {"talent_selector": talent_part, "project_query": project_part}
    return {"talent_selector": remainder, "project_query": remainder}


_UPLOAD_RESOLUTION_ERROR_MESSAGES = {
    "no_submission_found": "No {project_label} submission was found for {talent_label} — "
        "the mark-based upload workflow attaches media to an existing project submission, "
        "which doesn't exist yet for this talent/project.",
    "ambiguous_submission": "More than one talent record named {talent_label} has its own "
        "{project_label} submission. I can't tell which one this upload is for without a "
        "clearer reference — please check for duplicate talent records.",
    "no_email_on_submission": "{talent_label}'s {project_label} submission has no email on file — "
        "the upload destination can't be verified without one.",
    "email_maps_to_no_talent": "{talent_label}'s {project_label} submission email doesn't match "
        "any talent record — the upload destination can't be verified.",
    "email_maps_to_multiple_talents": "{talent_label}'s {project_label} submission email matches "
        "more than one talent record — the upload destination can't be verified. Please resolve "
        "the duplicate talent records first.",
    "submission_talent_mismatch": "{talent_label}'s {project_label} submission's talent_id doesn't "
        "match the talent its own submitted email resolves to — a data inconsistency. Please check "
        "this submission before retrying.",
    "email_resolved_to_unexpected_talent": "{talent_label}'s {project_label} submission's email "
        "resolves to a different talent record than the one requested — refusing to guess which "
        "one this upload is for.",
}


async def _upload_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    talent_selector = collected.get("talent_selector") or ""
    project_query = collected.get("project_query") or ""

    # Space-separated command (2026-08-25) — see _extract_upload_fields:
    # both fields hold the SAME full remainder text as a marker when no
    # hyphen was found; resolve the real talent/project boundary here,
    # against actual database records, before anything else runs.
    if talent_selector.strip() and talent_selector.strip() == project_query.strip():
        talent_selector, project_query, freeform_error = await _resolve_freeform_talent_project(talent_selector)
        if freeform_error is not None:
            return freeform_error

    # Step 1: resolve the project FIRST — the authoritative-talent lookup
    # below needs project_id to find "the project's submission for the
    # talent" (media_assignment.resolve_authoritative_talent_for_upload).
    projects = await _fetch_ongoing_projects()
    with request_scope.stage("fuzzy"):
        match = nlu.resolve_project_by_name(project_query, projects)
    if match.ambiguous:
        options = "\n".join(f"{i + 1}. {o['label']}" for i, o in enumerate(match.ambiguous))
        return ExecResult(
            ok=False, error="ambiguous_project",
            message=f"I found multiple projects.\n\n{options}\n\nPlease re-run with the exact project name.",
        )
    if not match.project:
        return ExecResult(
            ok=False, error="project_not_found",
            message=f'I couldn\'t find a project matching "{project_query}".',
        )
    project = match.project

    # Step 2: name resolution identifies the CANDIDATE SET this upload is
    # about — never the authoritative destination (see
    # resolve_authoritative_talent_for_upload's own module note: two
    # `talents` records can share an identical name during the admin-
    # manual-add -> talent-submits-their-own-email transition period).
    # Ambiguous name resolution is NOT an immediate stop here — the
    # project+email step below can often disambiguate it deterministically
    # by finding which single candidate actually has this project's
    # submission.
    talent_id, talent_label, err, ambiguous = await _resolve_talent_query_target(talent_selector)
    if ambiguous:
        candidate_ids = [c.id for c in ambiguous]
        candidate_label = ambiguous[0].label  # display only — all share the searched name
    elif talent_id:
        candidate_ids = [talent_id]
        candidate_label = talent_label
    else:
        return ExecResult(ok=False, error="talent_not_found", message=err or "No matching talent found.")

    # Step 3: the WhatsApp SOURCE group — resolved from whichever candidate
    # talent record(s) actually have one configured. This is deliberately
    # separate from the authoritative (email-verified) destination below:
    # the talent record that has interacted over WhatsApp and the talent
    # record tied to this project's submitted email are not guaranteed to
    # be the same document during the duplicate-record transition period.
    candidate_docs = await db.talents.find(
        {"id": {"$in": candidate_ids}}, {"_id": 0, "id": 1, "whatsapp_group_name": 1},
    ).to_list(20)
    group_names = {(d.get("whatsapp_group_name") or "").strip() for d in candidate_docs if (d.get("whatsapp_group_name") or "").strip()}
    if not group_names:
        return ExecResult(
            ok=False, error="no_whatsapp_group",
            message=f"{candidate_label} has no WhatsApp group configured — the mark-based "
                    "upload workflow requires one. Add it in Talentgram first.",
        )
    if len(group_names) > 1:
        return ExecResult(
            ok=False, error="ambiguous_whatsapp_group",
            message=f"Multiple different WhatsApp groups are configured across talent records named "
                    f"{candidate_label} — please resolve the duplicate talent records first.",
        )
    group_name = next(iter(group_names))

    # Step 4: the project's submission for this talent, re-derived from
    # the submission's OWN submitted email — the single source of truth
    # for upload destination. Never the name-resolved talent_id directly.
    auth = await media_assignment.resolve_authoritative_talent_for_upload(project["id"], candidate_ids)
    if not auth.ok:
        template = _UPLOAD_RESOLUTION_ERROR_MESSAGES.get(
            auth.error, "Could not verify the upload destination for {talent_label} / {project_label} ({error})."
        )
        message = template.format(talent_label=candidate_label, project_label=project["label"], error=auth.error)
        return ExecResult(ok=False, error=f"upload_target_unresolved:{auth.error}", message=message)

    authoritative_talent_id = auth.talent_id
    authoritative_talent_label = auth.talent_label or candidate_label

    identity = await media_assignment.get_gunwanti_identity()
    if not identity or not identity.get("lid"):
        return ExecResult(
            ok=False, error="identity_not_configured",
            message="The Gunwanti agent identity (WhatsApp LID) is not configured yet — "
                    "contact an admin before using upload.",
        )

    # The authoritative (email-verified) talent_id is what flows into the
    # scan/download/upload pipeline from here on — this is what makes
    # uploaded_media.talent_id == submission.talent_id hold, regardless of
    # which candidate the WhatsApp group itself came from.
    await media_assignment.create_scan_request(
        talent_id=authoritative_talent_id, talent_label=authoritative_talent_label,
        project_id=project["id"], project_label=project["label"],
        group_name=group_name,
    )
    return ExecResult(
        ok=True,
        message=f"Scanning {authoritative_talent_label}'s WhatsApp group for {project['label']} media…\n\n"
                "I'll report back here once it's done.",
    )


UPLOAD_INTENT = IntentDefinition(
    intent_id="casting.upload",
    triggers=["upload"],
    fields=[UPLOAD_TALENT_FIELD, UPLOAD_PROJECT_FIELD],
    executor=_upload_executor,
    extract_fields=_extract_upload_fields,
    auto_confirm=True,
)


# ---------------------------------------------------------------------------
# casting.share — Command Simplification + SHARE (2026-08-27). Shares a
# project's requirement/casting-call TEMPLATE with talent(s) or the whole
# pipeline — distinct from casting.send above, which forwards ONE talent's
# own marked audition media. Reuses, unmodified:
#   - the WhatsApp Campaign Agent's own template resolver
#     (agents.modules.whatsapp_campaign_agent._resolve_source /
#     _fetch_templates) — the exact same real, seeded "Casting Call"
#     template (routers/whatsapp.py's default-template seed) that
#     "Add,Move,Send - ... - Casting Call - ..." already sends via
#     _flush_group_send below;
#   - _resolve_add_project (project-name resolution, ambiguity/suggestion
#     handling) and _fetch_all_talent_candidates +
#     nlu.parse_talent_selector/resolve_against_candidates (global talent
#     resolution, independent of pipeline membership — a SHARE recipient
#     need not already be in the project) — both are ADD's own resolvers,
#     called here verbatim, not duplicated;
#   - create_batch (routers/whatsapp.py, unmodified) for the actual send,
#     the same call _flush_group_send already makes for a compound
#     Add,Move,Send tail step.
# Multiple projects fan out as independent send calls to the SAME resolved
# recipient set (the cross-product Part 19 describes); "to pipeline"
# recipients skip talent_ids entirely so create_batch's own recipient
# engine targets everyone currently in that project's pipeline, exactly
# like a WhatsApp Campaign Agent project-wide send already does — no new
# recipient-resolution logic for that case either.
# ---------------------------------------------------------------------------
SHARE_TRIGGERS = ["share"]

# Production Readiness (2026-09-03) — SHARE has exactly 3 purposes: a
# saved template (ANY saved template, resolved by name via the WhatsApp
# Campaign Agent's own wa._resolve_source/resolve_template_by_name fuzzy
# matcher — not hardcoded to "Casting Call") or a custom message (reusing
# the Campaign Agent's own quote-parsing primitives verbatim, see
# wa._find_quote_span/_fetch_custom_template — the SAME "Custom Message"
# mechanism its own send command already uses), sent to 1+ named talents
# or everyone currently in one specific pipeline stage, for 1+ projects.
# One canonical grammar; the legacy "share - Project - Talent" hyphen
# syntax is detected and redirected (_SHARE_LEGACY_MARKER below) rather
# than silently parsed, per "remove/restrict confusing legacy syntax."
_SHARE_CUSTOM_MESSAGE_RE = re.compile(r"^\s*custom\s+message\b", re.IGNORECASE)
_SHARE_TO_RECIPIENT_RE = re.compile(r"\b(?:to|with)\b\s+(.+)$", re.IGNORECASE | re.DOTALL)
_SHARE_FOR_PROJECT_RE = re.compile(r"\bfor\b\s+(.+)$", re.IGNORECASE | re.DOTALL)
_SHARE_LEADING_FILLER_RE = re.compile(r"^\s*(?:the|this|that|a|an|same)\s+", re.IGNORECASE)
_SHARE_TEMPLATE_WORD_ONLY_RE = re.compile(r"^\s*templates?\s*$", re.IGNORECASE)
_SHARE_PIPELINE_RECIPIENT_RE = re.compile(r"^\s*(?:the\s+)?pipelines?\s*$", re.IGNORECASE)
_SHARE_EVERYONE_IN_RE = re.compile(r"^\s*everyone\s+in\s+(?:the\s+)?(.+?)\s*$", re.IGNORECASE)
_SHARE_TRAILING_STAGE_WORD_RE = re.compile(r"\s+stage\s*$", re.IGNORECASE)
# Compound Actions (2026-08-27) — "SHARE the casting call with her" leaves
# a bare "the"/"casting call" behind; a project reference that reduces to
# nothing but filler means none was actually named (as opposed to a real
# name, which never reduces to just filler) — treated as empty so the
# compound-plan resolver knows to inherit the project from context
# instead of treating filler text as a literal (nonexistent) project name.
_SHARE_PROJECT_FILLER_ONLY_RE = re.compile(r"^\s*(?:the|this|that|a|an|same)\s*$", re.IGNORECASE)

# Sentinels — encoded into project_query/recipient_query so a single
# extra required-field check in _resolve_share can surface a SPECIFIC,
# guiding error via the exact same "resolved.ok=False -> show resolved.
# error" path every other SHARE failure already uses; no new dispatcher-
# level machinery, no new field.
_SHARE_LEGACY_MARKER = "__share_legacy_hyphen__"
_SHARE_MISSING_QUOTES_MARKER = "__share_missing_quotes__"
_SHARE_STAGE_MARKER_PREFIX = "__share_stage__:"
_SHARE_STAGE_UNKNOWN_PREFIX = "__share_stage_unknown__:"


def _share_split_for_clause(text: str) -> Tuple[str, str]:
    """(remaining_text, project_phrase) — the LAST " for <project>" clause
    anywhere in `text` is the project reference; "" for project_phrase
    when none is present. Deliberately independent of position: "Template
    for Project to Talent" (project before target) and "Template to
    everyone in Stage for Project" (project after target) both work by
    applying this to whichever side actually contains it."""
    m = _SHARE_FOR_PROJECT_RE.search(text)
    if not m:
        return text, ""
    return text[:m.start()].strip(), m.group(1).strip(" .!?")


def _share_split_target(before: str, after: str) -> Tuple[str, str]:
    """(remaining_head, target_raw) — finds the recipient/stage clause
    ("to"/"with" X), searching `after` first then `before` (mirrors the
    Campaign Agent's own custom-message search order: the quoted message
    can come before OR after the recipient clause). `before`/`after` are
    identical to the whole remainder itself when there's no quoted span
    (`after=""`), so this same helper serves saved-template mode too."""
    m = _SHARE_TO_RECIPIENT_RE.search(after)
    if m:
        return before, m.group(1).strip(" .!?")
    m = _SHARE_TO_RECIPIENT_RE.search(before)
    if m:
        return before[:m.start()].strip(), m.group(1).strip(" .!?")
    return before, ""


def _share_classify_target(target_raw: str) -> Dict[str, str]:
    """Encodes a resolved recipient_query value from the raw target
    phrase: pipeline (existing "(the) pipeline(s)" = every stage,
    unchanged), a specific stage (explicit "everyone in <stage>", or a
    bare phrase that itself names a real stage — checked via the SAME
    nlu.match_stage_phrase every MOVE command already resolves stage
    names through), or a talent list (falls through unchanged — a name
    that merely LOOKS like it could be a stage but doesn't cleanly match
    one is still just a name, never blocked)."""
    stripped = target_raw.strip()
    if not stripped:
        return {"recipient_query": ""}
    if _SHARE_PIPELINE_RECIPIENT_RE.match(stripped):
        return {"recipient_query": stripped}
    everyone_m = _SHARE_EVERYONE_IN_RE.match(stripped)
    candidate = _SHARE_TRAILING_STAGE_WORD_RE.sub("", everyone_m.group(1) if everyone_m else stripped).strip()
    stage_match = nlu.match_stage_phrase(candidate, list(PIPELINE_STAGE_ORDER))
    if stage_match.key:
        return {"recipient_query": _SHARE_STAGE_MARKER_PREFIX + stage_match.key}
    if everyone_m:
        # "everyone in X" unambiguously means a stage — X just isn't a
        # real one, so this must surface as "stage not found", never be
        # silently reinterpreted as a talent literally named "X".
        return {"recipient_query": _SHARE_STAGE_UNKNOWN_PREFIX + candidate}
    return {"recipient_query": stripped}


def _extract_share_fields(text: str) -> Dict[str, str]:
    """The one canonical SHARE grammar: "SHARE <template-or-'custom
    message \"...\"'> [for <project(s)>] (to|with) <talent(s)|stage>",
    tolerant of natural connector/filler variation and reasonable
    spacing. The "for <project>" clause may appear on either side of the
    recipient clause. A saved template is ANY name resolved later by
    _resolve_share via the Campaign Agent's own fuzzy template matcher —
    nothing here special-cases "Casting Call" beyond it being the most
    common real template name in this database, which the fuzzy matcher
    already handles like any other."""
    # "and confirm" — stripped FIRST, exactly like every other intent's
    # extraction (see nlu.strip_and_confirm's docstring), so it never ends
    # up glued onto the recipient clause below.
    raw_text, auto_confirm = nlu.strip_and_confirm(text or "")
    _, remainder = nlu._strip_leading_trigger(raw_text, SHARE_TRIGGERS)
    remainder = (remainder or "").strip()
    if not remainder:
        return {}

    out: Dict[str, str] = {}

    if _SHARE_CUSTOM_MESSAGE_RE.match(remainder):
        # Custom Message mode — reuses the Campaign Agent's own quote-span
        # finder VERBATIM: FIRST-to-LAST quote character, everything
        # between is one opaque payload (commas/hyphens/colons/newlines/
        # embedded quotes never tokenized), matching Part 3's exact
        # requirement. Local import — see the module-level comment above
        # _resolve_share on why this isn't a top-level import.
        from agents.modules import whatsapp_campaign_agent as wa

        after_phrase = _SHARE_CUSTOM_MESSAGE_RE.sub("", remainder, count=1)
        span = wa._find_quote_span(after_phrase)
        if not span:
            out = {"project_query": _SHARE_MISSING_QUOTES_MARKER, "recipient_query": _SHARE_MISSING_QUOTES_MARKER}
            if auto_confirm:
                out[AUTO_CONFIRM_FIELD.key] = "1"
            return out
        open_start, open_end, close_start, close_end = span
        message = after_phrase[open_end:close_start]
        before, after = after_phrase[:open_start], after_phrase[close_end:]
        _head, target_raw = _share_split_target(before, after)
        _head, project_from_head = _share_split_for_clause(_head)
        target_raw, project_from_target = _share_split_for_clause(target_raw)
        out = {
            "custom_message": message,
            "project_query": project_from_target or project_from_head,
        }
        out.update(_share_classify_target(target_raw))
        if auto_confirm:
            out[AUTO_CONFIRM_FIELD.key] = "1"
        return out

    parts = nlu._split_hyphen_fields(remainder, 2)
    if parts:
        # Legacy hyphen syntax — redirected, not parsed (Part 17). The
        # underlying send mechanism below is completely unaffected; only
        # this one old entry point is retired.
        out = {"project_query": _SHARE_LEGACY_MARKER, "recipient_query": _SHARE_LEGACY_MARKER}
        if auto_confirm:
            out[AUTO_CONFIRM_FIELD.key] = "1"
        return out

    head, target_raw = _share_split_target(remainder, "")
    head, project_from_head = _share_split_for_clause(head)
    target_raw, project_from_target = _share_split_for_clause(target_raw) if target_raw else ("", "")
    project_query = project_from_target or project_from_head
    if _SHARE_PROJECT_FILLER_ONLY_RE.match(project_query):
        project_query = ""

    template_phrase = _SHARE_LEADING_FILLER_RE.sub("", head.strip()).strip(" ,")
    out = {"project_query": project_query}
    out.update(_share_classify_target(target_raw))
    if template_phrase and not _SHARE_TEMPLATE_WORD_ONLY_RE.match(template_phrase):
        out["template_query"] = template_phrase

    if auto_confirm:
        out[AUTO_CONFIRM_FIELD.key] = "1"
    return out


def _validate_share_text(raw: str) -> ValidationResult:
    # Syntax-only, like every other field's validate — no DB access here.
    # recipient_query in particular can legitimately be the literal word
    # "pipeline" (not a talent selector shape at all), so this deliberately
    # does NOT reuse _validate_selector, which would reject it outright.
    return ValidationResult(ok=True, value=(raw or "").strip())


SHARE_PROJECT_FIELD = FieldSpec(
    key="project_query", label="Project(s)",
    question=(
        "SHARE needs a template and a talent or pipeline stage.\n\n"
        "Try:\nShare Casting Call with Anusha Sharma\n\n"
        "or:\nShare Casting Call for Hinge to Follow Up"
    ),
    # NOT required at the FieldSpec level (Part 1/3's own canonical
    # examples, "Share Casting Call with Anusha Sharma", never name a
    # project) — _resolve_share itself still demands one, but only when
    # it can't be inferred from a talent target's own pipeline
    # membership, or when the target is a stage/pipeline (which always
    # needs one explicit). See _resolve_share_project_ids /
    # _infer_share_projects_from_talents.
    validate=_validate_share_text, required=False, aliases=["project", "projects", "for"],
)
SHARE_RECIPIENT_FIELD = FieldSpec(
    key="recipient_query", label="Recipient(s)",
    question=(
        "SHARE needs a talent or pipeline stage.\n\n"
        "Try:\nShare Casting Call with Anusha Sharma\n\n"
        "or:\nShare Casting Call for Hinge to Follow Up"
    ),
    validate=_validate_share_text, aliases=["to", "recipient", "recipients", "talent"],
)
SHARE_TEMPLATE_FIELD = FieldSpec(
    key="template_query", label="Content", question="",
    validate=_validate_share_text, required=False, aliases=["template", "content"],
)


@dataclass
class _ShareResolution:
    ok: bool
    error: Optional[str] = None
    disambiguation: Optional[Dict[str, Any]] = None
    template: Optional[Dict[str, str]] = None
    template_label: str = ""
    project_ids: List[str] = dataclass_field(default_factory=list)
    project_labels: List[str] = dataclass_field(default_factory=list)
    is_pipeline_target: bool = False
    is_stage_target: bool = False
    target_stage: str = ""
    talent_ids: List[str] = dataclass_field(default_factory=list)
    talent_labels: List[str] = dataclass_field(default_factory=list)
    # Custom Message mode (Part 3/4, 2026-09-03) — set instead of
    # template/template_label when the SHARE step is a literal message
    # rather than a saved template; _run_share_sends passes this through
    # BatchIn.variable_data["message"], the SAME field the WhatsApp
    # Campaign Agent's own Custom Message mode already renders via.
    variable_data: Dict[str, str] = dataclass_field(default_factory=dict)
    # Pipeline-Check Option 2 (Production fix, 2026-09-04) — set ONLY when
    # collected[SHARE_PAIR_RESTRICTION_FIELD.key] narrows a named-talent
    # SHARE down to specific (project, talent) pairs already in that
    # project's pipeline (the admin chose "share only where already in
    # the pipeline" after a Pipeline Check). None means "no restriction",
    # i.e. every project sends to the full talent_ids list, exactly the
    # pre-existing behaviour. project_ids/project_labels are ALREADY
    # filtered down to only projects with at least one surviving pair
    # when this is set — see _resolve_share's talent-target tail.
    pair_talent_ids: Optional[Dict[str, List[str]]] = None


_SHARE_LEGACY_REDIRECT = (
    'Please use the new SHARE format.\n\n'
    'Example:\nShare Casting Call for Hinge with Anusha Sharma'
)
_SHARE_MISSING_QUOTES_ERROR = (
    "Please put the custom message inside quotation marks.\n\n"
    'Example:\nShare custom message "Please confirm your availability." with Anusha Sharma'
)


async def _infer_share_projects_from_talents(talent_ids: List[str]) -> List[Dict[str, str]]:
    """Part 1/3's own canonical examples ("Share Casting Call with Anusha
    Sharma") never name a project at all — rather than always demanding
    one, infer it from the requested talent(s)' OWN existing ongoing-
    pipeline membership when none was stated. Returns the DISTINCT set of
    ongoing projects (across all given talents) they're currently in,
    dedup'd by project id; empty when none of them are in any ongoing
    pipeline yet. Never guesses beyond what already exists — a talent with
    no pipeline row simply contributes nothing here, same as everywhere
    else in this module."""
    if not talent_ids:
        return []
    ongoing = {p["id"]: p["label"] for p in await _fetch_ongoing_projects()}
    cursor = db.casting_pipeline.find(
        {"talent_id": {"$in": talent_ids}}, {"_id": 0, "project_id": 1}
    )
    rows = await cursor.to_list(2000)
    seen: List[str] = []
    for row in rows:
        pid = row.get("project_id")
        if pid and pid in ongoing and pid not in seen:
            seen.append(pid)
    return [{"id": pid, "label": ongoing[pid]} for pid in seen]


async def _resolve_share_project_ids(
    project_query: str,
) -> Tuple[Optional[List[str]], Optional[List[str]], Optional["_ShareResolution"]]:
    """Resolves an EXPLICITLY-given project_query into (ids, labels, None),
    or (None, None, failure) to propagate straight back out of
    _resolve_share — factored out since both the stage/pipeline-target
    branch and the talent-target-with-an-explicit-project branch need the
    identical resolution."""
    names = await _resolve_project_query_names(project_query)
    ids: List[str] = []
    labels: List[str] = []
    for pname in names:
        pid, plabel, err, dis = await _resolve_add_project(pname)
        if err:
            return None, None, _ShareResolution(ok=False, error=err, disambiguation=dis)
        ids.append(pid)
        labels.append(plabel)
    return ids, labels, None


async def _resolve_share(collected: dict) -> _ShareResolution:
    # Local import — see the module-level comment on the routers.whatsapp
    # import above: whatsapp_campaign_agent.py already imports FROM this
    # module, so a top-level import here would be circular (identical
    # reasoning/pattern to _flush_group_send's own local import below).
    from agents.modules import whatsapp_campaign_agent as wa

    project_query = (collected.get("project_query") or "").strip()
    recipient_query = (collected.get("recipient_query") or "").strip()
    template_query = (collected.get("template_query") or "").strip()
    custom_message = collected.get("custom_message")

    if project_query == _SHARE_LEGACY_MARKER or recipient_query == _SHARE_LEGACY_MARKER:
        return _ShareResolution(ok=False, error=_SHARE_LEGACY_REDIRECT)
    if project_query == _SHARE_MISSING_QUOTES_MARKER or recipient_query == _SHARE_MISSING_QUOTES_MARKER:
        return _ShareResolution(ok=False, error=_SHARE_MISSING_QUOTES_ERROR)

    if not recipient_query:
        return _ShareResolution(
            ok=False, error=(
                "SHARE needs a talent or pipeline stage.\n\n"
                "Try:\nShare Casting Call with Anusha Sharma\n\n"
                "or:\nShare Casting Call for Hinge to Follow Up"
            ),
        )

    template: Optional[Dict[str, str]] = None
    template_label = ""
    variable_data: Dict[str, str] = {}
    if custom_message is not None:
        template = await wa._fetch_custom_template()
        if not template:
            return _ShareResolution(ok=False, error="No custom-message template is configured yet.")
        template_label = "Custom message"
        variable_data = {"message": custom_message}
    elif template_query:
        tmpl_match = await wa._resolve_source(template_query)
        if tmpl_match.error:
            return _ShareResolution(ok=False, error=tmpl_match.error)
        if tmpl_match.ambiguous:
            options = [{"label": o["label"], "value": o["label"]} for o in tmpl_match.ambiguous]
            msg = nlu.format_numbered_options(
                "I found multiple templates.", [[o["label"]] for o in tmpl_match.ambiguous],
            )
            return _ShareResolution(
                ok=False, error=msg,
                disambiguation={"kind": "free_text_retry", "field_key": "template_query", "options": options},
            )
        template = tmpl_match.template
        template_label = template.get("name") or template.get("slug") or ""
    else:
        # Bare "template" with no specific name — auto-pick only when
        # exactly one real template exists; otherwise ask (Part 9: never
        # guess which template).
        templates = await wa._fetch_templates()
        if not templates:
            return _ShareResolution(
                ok=False,
                error=(
                    "I couldn't find that template.\n\n"
                    "Please send the saved template name, for example:\n"
                    "Share Casting Call with Anusha Sharma"
                ),
            )
        if len(templates) == 1:
            template = templates[0]
            template_label = template.get("name") or template.get("slug") or ""
        else:
            options = [
                {"label": t.get("name") or t.get("slug") or "", "value": t.get("name") or t.get("slug") or ""}
                for t in templates
            ]
            msg = nlu.format_numbered_options(
                "Which template should I share?", [[o["label"]] for o in options],
            )
            return _ShareResolution(
                ok=False, error=msg,
                disambiguation={"kind": "free_text_retry", "field_key": "template_query", "options": options},
            )

    # Classify the recipient BEFORE deciding how to handle a missing
    # project — Part 1/3's own canonical examples ("Share Casting Call
    # with Anusha Sharma") never state one; a stage/pipeline target
    # (Part 2's examples always include "for <project>") still needs one
    # explicitly, since "everyone in Follow Up" names no talent to infer
    # a project from.
    is_pipeline_recipient = bool(_SHARE_PIPELINE_RECIPIENT_RE.match(recipient_query))
    is_stage_unknown = recipient_query.startswith(_SHARE_STAGE_UNKNOWN_PREFIX)
    is_stage_known = recipient_query.startswith(_SHARE_STAGE_MARKER_PREFIX)

    if not project_query and (is_pipeline_recipient or is_stage_unknown or is_stage_known):
        projects = await _fetch_ongoing_projects()
        text, options = _format_project_choice(projects, 0, "Which project should I use?")
        return _ShareResolution(
            ok=False, error=text,
            disambiguation={"kind": "project", "field_key": "project_query", "options": options},
        )

    if is_stage_unknown:
        stage_text = recipient_query[len(_SHARE_STAGE_UNKNOWN_PREFIX):]
        bullets = "\n".join(f"• {nlu.stage_label(s)}" for s in PIPELINE_STAGE_ORDER)
        return _ShareResolution(
            ok=False,
            error=f'I couldn\'t find a pipeline stage named "{stage_text}".\n\nAvailable stages:\n\n{bullets}',
        )

    if is_pipeline_recipient or is_stage_known:
        project_ids, project_labels, failure = await _resolve_share_project_ids(project_query)
        if failure:
            return failure
        if is_pipeline_recipient:
            return _ShareResolution(
                ok=True, template=template, template_label=template_label, variable_data=variable_data,
                project_ids=project_ids, project_labels=project_labels,
                is_pipeline_target=True,
            )
        target_stage = recipient_query[len(_SHARE_STAGE_MARKER_PREFIX):]
        return _ShareResolution(
            ok=True, template=template, template_label=template_label, variable_data=variable_data,
            project_ids=project_ids, project_labels=project_labels,
            is_stage_target=True, target_stage=target_stage,
        )

    # Talent target — resolve the talent(s) first; only then decide the
    # project, since an omitted project is inferred from THEIR OWN
    # existing ongoing-pipeline membership rather than always demanded
    # up front (see _infer_share_projects_from_talents).
    selector = nlu.parse_talent_selector(recipient_query)
    if not selector.ok:
        return _ShareResolution(ok=False, error=selector.error)
    candidates = await _fetch_all_talent_candidates()
    with request_scope.stage("fuzzy"):
        resolved = nlu.resolve_against_candidates(selector, candidates)
    if not resolved.ok:
        if resolved.ambiguous_candidates:
            options = [
                {"id": c.id, "label": c.label, "value": f"{nlu.RESOLVED_TALENT_MARKER}{c.id}|{c.label}"}
                for c in resolved.ambiguous_candidates
            ]
            msg = nlu.format_numbered_options(
                f'I found multiple talents matching "{selector.name_query or recipient_query}".\n'
                "Which one did you mean?",
                [[c.label] for c in resolved.ambiguous_candidates],
            )
            return _ShareResolution(
                ok=False, error=msg,
                disambiguation={"kind": "talent", "field_key": "recipient_query", "options": options},
            )
        return _ShareResolution(
            ok=False,
            error=resolved.error or "No matching talent found.",
            disambiguation={"kind": "free_text_retry", "field_key": "recipient_query", "options": []},
        )

    if project_query:
        project_ids, project_labels, failure = await _resolve_share_project_ids(project_query)
        if failure:
            return failure
    else:
        inferred = await _infer_share_projects_from_talents(resolved.talent_ids)
        if len(inferred) == 1:
            project_ids = [inferred[0]["id"]]
            project_labels = [inferred[0]["label"]]
        elif len(inferred) > 1:
            options = [{"label": p["label"], "value": p["label"]} for p in inferred]
            msg = nlu.format_numbered_options(
                "Which project should I use?", [[p["label"]] for p in inferred],
            )
            return _ShareResolution(
                ok=False, error=msg,
                disambiguation={"kind": "project", "field_key": "project_query", "options": options},
            )
        else:
            projects = await _fetch_ongoing_projects()
            text, options = _format_project_choice(projects, 0, "Which project should I use?")
            return _ShareResolution(
                ok=False, error=text,
                disambiguation={"kind": "project", "field_key": "project_query", "options": options},
            )

    # Pipeline-Check Option 2 (Production fix, 2026-09-04) — when set, an
    # earlier Pipeline Check turn narrowed this SHARE to specific (project,
    # talent) pairs already in that project's pipeline; apply it here so
    # EVERY downstream consumer (confirmation card, _run_share_sends) sees
    # the SAME narrowed picture from one place, never two. A project left
    # with zero surviving talents is dropped entirely rather than shown
    # with nobody to send to.
    pair_talent_ids: Optional[Dict[str, List[str]]] = None
    restriction_raw = (collected.get(SHARE_PAIR_RESTRICTION_FIELD.key) or "").strip()
    if restriction_raw:
        try:
            restriction = json.loads(restriction_raw)
        except (TypeError, ValueError):
            restriction = {}
        kept_project_ids: List[str] = []
        kept_project_labels: List[str] = []
        pair_map: Dict[str, List[str]] = {}
        for pid, plabel in zip(project_ids, project_labels):
            allowed = set(restriction.get(pid) or [])
            keep = [tid for tid in resolved.talent_ids if tid in allowed]
            if keep:
                kept_project_ids.append(pid)
                kept_project_labels.append(plabel)
                pair_map[pid] = keep
        project_ids, project_labels = kept_project_ids, kept_project_labels
        pair_talent_ids = pair_map

    return _ShareResolution(
        ok=True, template=template, template_label=template_label, variable_data=variable_data,
        project_ids=project_ids, project_labels=project_labels,
        talent_ids=resolved.talent_ids, talent_labels=resolved.talent_labels,
        pair_talent_ids=pair_talent_ids,
    )


def _share_recipient_is_implicit(recipient_raw: str) -> bool:
    """True when a SHARE step's own recipient clause names no one of its
    own — empty, or a pronoun ("her"/"him"/"them"/"both"/...) — reusing
    the SAME pronoun vocabulary MOVE's own implicit-continuation already
    recognizes (nlu.parse_talent_selector), not a second pronoun list.
    "pipeline" is a real, explicit SHARE target (Part 10/18), never
    implicit, so it's excluded here even though it isn't a name either."""
    if not recipient_raw:
        return True
    if _SHARE_PIPELINE_RECIPIENT_RE.match(recipient_raw):
        return False
    selector = nlu.parse_talent_selector(recipient_raw)
    return bool(selector.ok and selector.name_query == nlu.PRONOUN_LAST_MARKER)


async def _resolve_share_step_for_plan(
    raw_text: str, touched_pairs: List[Dict[str, str]]
) -> "_ShareResolution":
    """Resolves one 'casting.share' plan step (Compound Actions,
    2026-08-27) — reuses _extract_share_fields/_resolve_share UNCHANGED;
    the only new behaviour is inheriting an implicit/pronoun project or
    recipient from `touched_pairs` (the SAME plan-wide accumulator MOVE's
    own PRONOUN_LAST_MARKER fan-out already uses — see
    _resolve_one_plan_segment) before handing off to the ordinary
    resolver. An EXPLICIT project/recipient named on the SHARE clause
    itself (Part 4: "SHARE the casting call with Siddhi" naming someone
    OTHER than who was just added/moved) always wins — inheritance only
    ever fills in what the clause left unsaid."""
    fields = _extract_share_fields(raw_text)
    project_raw = (fields.get("project_query") or "").strip()
    recipient_raw = (fields.get("recipient_query") or "").strip()
    template_query = fields.get("template_query") or ""

    # Current-Command Context Only (2026-09-02) — see the identical guard
    # in _resolve_one_plan_segment. An implicit recipient ("her"/"both"/
    # unnamed) with NOTHING yet touched by this same plan must never be
    # handed to _resolve_share as literal text (which would try to fuzzy-
    # match the pronoun word itself against real talent names) — it
    # depends on an earlier step that hasn't resolved yet.
    if _share_recipient_is_implicit(recipient_raw) and not touched_pairs:
        return _ShareResolution(ok=False, error=(
            "This step depends on an earlier step in this command that "
            "hasn't been resolved yet — nothing has been shared."
        ))

    if _share_recipient_is_implicit(recipient_raw) and touched_pairs:
        labels, err = _resolve_plan_pronoun_talents(recipient_raw, touched_pairs)
        if err:
            return _ShareResolution(ok=False, error=err)
        recipient_raw = ",".join(labels)

    if not project_raw and touched_pairs:
        seen_p: List[str] = []
        for pair in touched_pairs:
            if pair["project_label"] not in seen_p:
                seen_p.append(pair["project_label"])
        project_raw = ",".join(seen_p)

    collected = {"project_query": project_raw, "recipient_query": recipient_raw}
    if template_query:
        collected["template_query"] = template_query
    return await _resolve_share(collected)


@dataclass
class _SharePairCheck:
    talent_id: str
    talent_label: str
    project_id: str
    project_label: str
    in_pipeline: bool


async def _share_pipeline_matrix(resolved: "_ShareResolution") -> List[_SharePairCheck]:
    """The FULL (project, talent) cross-product this named-talent SHARE
    would touch, each flagged with whether a casting_pipeline row already
    exists — checked BEFORE the confirmation is even shown, never
    discovered only after "Approve" (Part 10; extended to a true
    cross-product Pipeline Check, Production fix 2026-09-04). Only
    meaningful for a named-talent SHARE; a stage/whole-pipeline target is
    inherently scoped to EXISTING pipeline members already, so there's
    nothing to check. A template send needs a pipeline row to render
    {{project_name}}/{{shoot_dates}}/{{budget}} from
    (resolve_recipients_engine's PROJECT branch, routers/whatsapp.py) —
    confirmed by inspection, not assumed; there is no "share anyway"
    bypass in the underlying mechanism, so none is invented here either."""
    if resolved.is_pipeline_target or resolved.is_stage_target or not resolved.talent_ids:
        return []
    out: List[_SharePairCheck] = []
    for pid, plabel in zip(resolved.project_ids, resolved.project_labels):
        rows = await db.casting_pipeline.find(
            {"project_id": pid, "talent_id": {"$in": resolved.talent_ids}},
            {"_id": 0, "talent_id": 1},
        ).to_list(len(resolved.talent_ids))
        existing_ids = {r["talent_id"] for r in rows}
        for tid, tlabel in zip(resolved.talent_ids, resolved.talent_labels):
            out.append(_SharePairCheck(tid, tlabel, pid, plabel, tid in existing_ids))
    return out


def _format_share_pipeline_check(matrix: List[_SharePairCheck]) -> str:
    """The structured Pipeline Check message (Production fix, 2026-09-04)
    — replaces the old single-pair-only "X is not currently in the Y
    pipeline" message AND the old multi-pair "list + Reply CANCEL" dead
    end with ONE format that always shows the full cross-product picture
    and always offers a real, numbered decision (never just an error)."""
    missing = [r for r in matrix if not r.in_pipeline]
    lines = ["Pipeline check", ""]
    for row in matrix:
        mark = "✓" if row.in_pipeline else "❌"
        lines.append(f"{mark} {row.talent_label} — {row.project_label}")
    lines.append("")

    distinct_talents: List[str] = []
    distinct_projects: List[str] = []
    for row in missing:
        if row.talent_label not in distinct_talents:
            distinct_talents.append(row.talent_label)
        if row.project_label not in distinct_projects:
            distinct_projects.append(row.project_label)
    if len(distinct_talents) == 1:
        subject = distinct_talents[0]
        if len(distinct_projects) == 1:
            lines.append(f"{subject} is not currently in the {distinct_projects[0]} pipeline.")
        else:
            lines.append(f"{subject} is not currently in these project pipelines.")
    else:
        lines.append("Some of the named talent(s) are not currently in these project pipelines.")

    lines += [
        "", "What would you like to do?", "",
        "1 → Add the missing talent(s) to the project(s), move them to Follow Up, "
        "then share the casting call",
        "2 → Share only for project(s) where they are already in the pipeline",
        "3 → Cancel",
        "", "Nothing has been sent yet.",
    ]
    return "\n".join(lines)


def _build_share_gate_add_move_share_text(
    resolved: "_ShareResolution", missing: List[_SharePairCheck], collected: dict,
) -> str:
    """Builds ONE compound "Add ..., move ..., share ..." command text
    covering EXACTLY the missing (project, talent) pairs.

    The Add clause uses one explicit "talent to project" segment per
    missing pair, reusing nlu.split_multi_segment_pairs (the SAME "A to
    X, B to Y, C to X" multi-mapping grammar the compound-plan engine
    already understands, confirmed by direct testing) so an IRREGULAR
    missing set (e.g. talent A missing only from project 1, talent B
    missing only from project 2) is never over-added beyond what's
    actually missing.

    The Move clause deliberately does NOT name a project — it names each
    DISTINCT missing talent once ("move Nikita Tiwari to Follow Up") and
    relies on the compound-plan's own touched_pairs continuation
    (_touched_pairs_matching_talent — the SAME mechanism the proven
    single-pair handoff already used via "her") to move exactly the
    pair(s) THIS plan's own Add step just touched. Naming the project
    explicitly here would make Move re-check the LIVE pipeline at preview
    time (before Add has actually run) and report "nothing to move" —
    the real bug this construction avoids. It also means an
    already-Shortlisted talent is never silently demoted back to Follow
    Up just because a DIFFERENT pair happened to be missing (Production
    fix, 2026-09-04): touched_pairs only ever contains what THIS Add
    step just created, never a pre-existing valid pair.

    By the time this plan finishes, every pair the ORIGINAL SHARE request
    named is valid, so the Share step itself reuses the ORIGINAL, full
    talent/project lists unchanged — never just the newly-added ones."""
    add_segments = [f"{row.talent_label} to {row.project_label}" for row in missing]
    move_names: List[str] = []
    for row in missing:
        if row.talent_label not in move_names:
            move_names.append(row.talent_label)
    project_list = ", ".join(resolved.project_labels)
    talent_list = ", ".join(resolved.talent_labels)
    custom_message = collected.get("custom_message")
    if custom_message is not None:
        share_clause = f'share custom message "{custom_message}" for {project_list} with {talent_list}'
    else:
        share_clause = f"share {resolved.template_label} for {project_list} with {talent_list}"
    return (
        f"Add {', '.join(add_segments)}, "
        f"move {', '.join(move_names)} to Follow Up, "
        f"{share_clause}"
    )


async def _build_share_confirmation(collected: dict, ctx: ExecContext) -> str:
    resolved = await _resolve_share(collected)
    if not resolved.ok:
        if resolved.disambiguation:
            await session_context.update_session(
                AGENT_ID, ctx.sender_phone, pending_disambiguation=resolved.disambiguation
            )
            await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, step="editing")
        else:
            await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
        return resolved.error

    matrix = await _share_pipeline_matrix(resolved)
    if any(not row.in_pipeline for row in matrix):
        await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, step="confirming")
        await session_context.update_session(
            AGENT_ID, ctx.sender_phone,
            pending_disambiguation={"kind": "share_pipeline_check"},
        )
        return _format_share_pipeline_check(matrix)

    await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
    return _build_share_confirmation_preview(resolved)


def _build_share_confirmation_preview(resolved: "_ShareResolution") -> str:
    """The actual "You are about to SHARE:" preview for an already-
    resolved, already pipeline-checked SHARE — factored out of
    _build_share_confirmation so Pipeline Check Option 2 (Production fix,
    2026-09-04) can build the SAME preview for a resolution that's been
    narrowed to only the pairs already in a pipeline, without duplicating
    this formatting a second time.

    Confirmation Card Quality (Part 11) — the single-project shape stays
    compact (Content/Project/Recipients/Messages); 2+ projects switch to
    one block per project plus a running total, matching the exact
    formats specified. Never duplicated numbering/steps — this is a
    flat, single-operation preview, not a multi-step plan card."""
    lines = ["You are about to SHARE:", "", "Template:", resolved.template_label, ""]
    is_multi_project = len(resolved.project_ids) > 1
    if resolved.is_pipeline_target or resolved.is_stage_target:
        recipients_desc = (
            f"everyone in {nlu.stage_label(resolved.target_stage)}"
            if resolved.is_stage_target else "everyone currently in the pipeline"
        )
        if is_multi_project:
            for plabel in resolved.project_labels:
                lines += [f"Project: {plabel}", f"Recipients: {recipients_desc}", ""]
            lines += ["Reply:", "1 → Approve", "2 → Edit", "3 → Cancel"]
        else:
            lines += [
                "Project:", resolved.project_labels[0], "",
                # NOT str.capitalize() — that lowercases the rest of the
                # string too, turning "everyone in Follow Up" into
                # "Everyone in follow up" and mangling the stage's own
                # proper-cased label. Just upper-case the leading letter.
                "Recipients:", recipients_desc[:1].upper() + recipients_desc[1:], "",
                "Reply:", "1 → Approve", "2 → Edit", "3 → Cancel",
            ]
    else:
        # Pipeline-Check Option 2 — a restricted resolution can send a
        # DIFFERENT talent subset per project; every other SHARE keeps
        # showing the same full talent_labels list for every project,
        # exactly the pre-existing behaviour.
        labels_by_id = dict(zip(resolved.talent_ids, resolved.talent_labels))

        def _names_for(pid: str) -> List[str]:
            if resolved.pair_talent_ids is None:
                return resolved.talent_labels
            return [labels_by_id.get(tid, tid) for tid in resolved.pair_talent_ids.get(pid, [])]

        if is_multi_project:
            total = 0
            for pid, plabel in zip(resolved.project_ids, resolved.project_labels):
                names = _names_for(pid)
                lines += [f"Project: {plabel}", f"Recipients: {', '.join(names)}",
                          f"Messages: {len(names)}", ""]
                total += len(names)
            lines += ["Total:", f"{total} WhatsApp message{'' if total == 1 else 's'}", "",
                      "Reply:", "1 → Approve", "2 → Edit", "3 → Cancel"]
        else:
            names = _names_for(resolved.project_ids[0])
            lines += ["Project:", resolved.project_labels[0], "", "Recipients:"]
            lines += [f"{i}. {t}" for i, t in enumerate(names, start=1)]
            lines += ["", f"Messages:\n{len(names)}", "", "Reply:", "1 → Approve", "2 → Edit", "3 → Cancel"]
    return "\n".join(lines)


async def _run_share_sends(resolved: "_ShareResolution") -> Tuple[List[str], int]:
    """The actual WhatsApp send loop for an already-resolved SHARE —
    factored out of _share_executor so the Compound Actions plan engine's
    "casting.share" step (_execute_plan) can reuse the EXACT same send
    logic instead of a second copy. Returns (body_lines, total_queued);
    the caller wraps these in whatever header/footer its own context
    needs (a standalone "Shared." message vs. one line item inside a
    combined plan summary)."""
    from agents.modules import whatsapp_campaign_agent as wa
    admin = await wa._service_admin()

    body_lines: List[str] = []
    total_queued = 0
    stages = [resolved.target_stage] if resolved.is_stage_target else list(PIPELINE_STAGE_ORDER)
    for pid, plabel in zip(resolved.project_ids, resolved.project_labels):
        body_lines.append("")
        body_lines.append(plabel)
        # Pipeline-Check Option 2 — a restricted resolution narrows WHICH
        # talents this specific project actually targets; every other
        # SHARE keeps sending to the full talent_ids list, unchanged.
        project_talent_ids = (
            resolved.pair_talent_ids.get(pid, []) if resolved.pair_talent_ids is not None
            else resolved.talent_ids
        )
        if resolved.is_pipeline_target or resolved.is_stage_target:
            source_params = SourceParams(project_id=pid, pipeline_stages=stages)
        else:
            source_params = SourceParams(
                project_id=pid, pipeline_stages=stages,
                talent_ids=project_talent_ids,
            )
        try:
            result = await create_batch(
                BatchIn(
                    source_type="PROJECT", source_params=source_params,
                    template_id=resolved.template["id"], is_dry_run=False,
                    variable_data=resolved.variable_data,
                ),
                admin=admin,
            )
        except HTTPException as exc:
            body_lines.append(f"✗ WhatsApp send failed ({exc.detail})")
            continue
        jobs = result["jobs"]
        if resolved.is_pipeline_target or resolved.is_stage_target:
            body_lines.append(f"• {len(jobs)} message{'' if len(jobs) == 1 else 's'} queued")
        else:
            sent_ids = {j.get("talent_id") for j in jobs}
            labels_by_id = dict(zip(resolved.talent_ids, resolved.talent_labels))
            for tid in project_talent_ids:
                label = labels_by_id.get(tid, tid)
                if tid in sent_ids:
                    body_lines.append(f"• {label} — sent")
                else:
                    # Real reason is one of: not currently in THIS
                    # project's pipeline (a "Casting Call" send needs a
                    # pipeline row to render {{project_name}}/{{shoot_
                    # dates}}/{{budget}} from — see resolve_recipients_
                    # engine's PROJECT branch, routers/whatsapp.py), or no
                    # phone/WhatsApp group on file. create_batch's result
                    # doesn't distinguish the two, so both are named
                    # rather than guessing which one applies. The Part 10
                    # pipeline-membership pre-flight check above already
                    # catches the FIRST case before this point is ever
                    # reached — this line is the safety net for the
                    # second (no phone/group on file), not a duplicate of
                    # that gate.
                    body_lines.append(
                        f"• {label} — not currently in {plabel}'s pipeline, "
                        "or no phone/WhatsApp group on file — not sent"
                    )
        total_queued += len(jobs)
    return body_lines, total_queued


async def _share_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    resolved = await _resolve_share(collected)
    if not resolved.ok:
        return ExecResult(ok=False, error="share_resolution_failed", message=resolved.error)

    body_lines, total_queued = await _run_share_sends(resolved)

    header = ["Shared.", "", f"Content: {resolved.template_label}"]
    footer = ["", f"{total_queued} WhatsApp message{'' if total_queued == 1 else 's'} queued."]
    return ExecResult(
        ok=True, message="\n".join(header + body_lines + footer),
        data={"queued": total_queued},
    )


async def _share_try_auto_execute(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    if not collected.get(AUTO_CONFIRM_FIELD.key):
        return None
    resolved = await _resolve_share(collected)
    if not resolved.ok:
        # Still ambiguous/erroring — fall through to the normal
        # confirmation flow; _auto_confirm persists in `collected` across
        # the "editing"-step continuation, so this check re-fires and
        # auto-executes once the ambiguity resolves, same as ADD/MOVE.
        return None
    # "and confirm" never skips the Part 10 pipeline-membership gate —
    # that gate is a real, unresolved blocker, not an approval step to
    # bypass, so an auto-confirming SHARE that hits it still falls
    # through to the normal confirmation flow, exactly like an
    # unresolved ambiguity does above.
    matrix = await _share_pipeline_matrix(resolved)
    if any(not row.in_pipeline for row in matrix):
        return None
    return await _share_executor(collected, ctx)


async def _build_share_edit_prompt(collected: dict, ctx: ExecContext) -> str:
    """Guided Edit Prompts — connects "2" to the SPECIFIC pending share,
    including the RESOLVED template name (reusing _resolve_share, exactly
    what _build_share_confirmation already called) rather than just
    echoing back the raw hint text. Falls back to the raw collected text
    if resolution itself is what's currently failing (e.g. an ambiguous
    project) — still better than a blank/generic prompt."""
    resolved = await _resolve_share(collected)
    if resolved.ok:
        project_desc = ", ".join(resolved.project_labels)
        if resolved.is_stage_target:
            recipient_desc = f"everyone in {nlu.stage_label(resolved.target_stage)}"
        elif resolved.is_pipeline_target:
            recipient_desc = "everyone in the pipeline"
        else:
            recipient_desc = ", ".join(resolved.talent_labels)
        template_desc = resolved.template_label
    else:
        project_desc = (collected.get("project_query") or "").strip()
        recipient_desc = (collected.get("recipient_query") or "").strip()
        template_desc = (collected.get("template_query") or "").strip() or "the saved template"

    current = f'Share "{template_desc}" for {project_desc or "?"} with {recipient_desc or "?"}.'
    lines = [
        "EDITING SHARE", "", "Current:", current, "",
        "What would you like to change?", "",
        "You can say:",
        "• Change the template",
        "• Change the project",
        "• Change the talent",
        "• Add another talent",
        "• Remove a talent",
    ]
    if collected.get("custom_message") is not None:
        lines.append("• Change the message")
    lines += ["• Cancel", "", "Nothing will be sent until you confirm."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Guided SHARE Editing (Part 12, 2026-09-03) — natural-language edit
# instructions during "EDITING SHARE", each rewriting ONLY the one field
# it names; unrecognized text falls through to _move_parse_edits_async
# (the existing generic pending_disambiguation numbered-reply resume,
# unchanged — still how an ambiguous talent/project/template mid-edit
# gets resolved). Reuses the SAME CANCEL vocabulary/mechanism (_EDIT_
# CANCEL_RE, the always-fails-and-echoes PLAN_STEP_EDIT_ERROR_FIELD
# trick) the ADD/MOVE/SHARE compound-plan editor already established —
# not a second cancel implementation, and it directly resolves the "3
# means both Cancel and something else" ambiguity class of bug for
# SHARE too, since the literal word CANCEL never collides with a step
# number here in the first place.
# ---------------------------------------------------------------------------
_SHARE_EDIT_PROJECT_RE = re.compile(r"^\s*change\s+(?:the\s+)?project\s+to\s+(.+?)\s*$", re.IGNORECASE)
_SHARE_EDIT_TEMPLATE_RE = re.compile(r"^\s*change\s+(?:the\s+)?template\s+to\s+(.+?)\s*$", re.IGNORECASE)
_SHARE_EDIT_TALENT_TO_RE = re.compile(
    r"^\s*(?:change\s+(?:the\s+)?talents?\s+to|share\s+it\s+with)\s+(.+?)\s*$", re.IGNORECASE
)
_SHARE_EDIT_REMOVE_RE = re.compile(r"^\s*remove\s+(.+?)\s*$", re.IGNORECASE)
_SHARE_EDIT_ADD_RE = re.compile(
    r"^\s*add\s+(?:another\s+talent\s*[:\-]?\s*)?(.+?)\s*$", re.IGNORECASE
)
_SHARE_EDIT_MESSAGE_RE = re.compile(r"^\s*change\s+(?:the\s+)?message\s+to\s*[:\-]?\s*(.+)$", re.IGNORECASE | re.DOTALL)


async def _share_parse_edits_async(
    text: str, collected: Dict[str, str], fields: List[FieldSpec], ctx: ExecContext,
) -> Dict[str, str]:
    stripped = (text or "").strip()

    if _EDIT_CANCEL_RE.match(stripped):
        await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
        await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
        return {PLAN_STEP_EDIT_ERROR_FIELD.key: _EDITING_CANCELLED_MESSAGE}

    is_custom = collected.get("custom_message") is not None

    m = _SHARE_EDIT_MESSAGE_RE.match(stripped)
    if m and is_custom:
        return {"custom_message": m.group(1).strip()}

    m = _SHARE_EDIT_PROJECT_RE.match(stripped)
    if m:
        return {"project_query": m.group(1).strip()}

    m = _SHARE_EDIT_TEMPLATE_RE.match(stripped)
    if m and not is_custom:
        return {"template_query": m.group(1).strip()}

    m = _SHARE_EDIT_TALENT_TO_RE.match(stripped)
    if m:
        return {"recipient_query": m.group(1).strip()}

    m = _SHARE_EDIT_REMOVE_RE.match(stripped) or _SHARE_EDIT_ADD_RE.match(stripped)
    if m:
        resolved = await _resolve_share(collected)
        current_names = (
            list(resolved.talent_labels)
            if resolved.ok and not (resolved.is_pipeline_target or resolved.is_stage_target)
            else []
        )
        if current_names:
            remove_m = _SHARE_EDIT_REMOVE_RE.match(stripped)
            if remove_m:
                remove_name = remove_m.group(1).strip().lower()
                remaining = [t for t in current_names if remove_name not in t.lower()]
                if len(remaining) == len(current_names):
                    return {PLAN_STEP_EDIT_ERROR_FIELD.key: f"Couldn't find {remove_m.group(1).strip()} among the recipients."}
                if not remaining:
                    return {PLAN_STEP_EDIT_ERROR_FIELD.key: (
                        "Can't remove the only recipient — change the talent instead, or CANCEL."
                    )}
                return {"recipient_query": ",".join(remaining)}
            add_m = _SHARE_EDIT_ADD_RE.match(stripped)
            if add_m:
                return {"recipient_query": ",".join(current_names + [add_m.group(1).strip()])}

    return await _move_parse_edits_async(text, collected, fields, ctx)


async def _share_editing_claims_reply(
    text: str, collected: Dict[str, str], ctx: ExecContext,
) -> bool:
    """IntentDefinition.claims_editing_reply hook for standalone SHARE
    editing (Part 12) — the SAME trigger-word collision Task A's compound-
    plan editing already solved (see _plan_step_editing_claims_reply):
    SHARE's own suggested edit phrasing can start with a trigger word
    that belongs to SHARE itself ("Share it with X instead" starts with
    "share") or to a DIFFERENT intent ("Add another talent: X" starts
    with ADD's own trigger "add") — without this hook, dispatcher.py
    would treat either as a brand-new command instead of routing it into
    _share_parse_edits_async, discarding the edit in progress. Pure,
    side-effect-free pattern matching against the SAME instruction shapes
    _share_parse_edits_async actually understands — a message matching
    none of them returns False, so a genuinely unrelated fresh command
    still restarts normally, exactly as before this hook existed."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return bool(
        _EDIT_CANCEL_RE.match(stripped)
        or _SHARE_EDIT_MESSAGE_RE.match(stripped)
        or _SHARE_EDIT_PROJECT_RE.match(stripped)
        or _SHARE_EDIT_TEMPLATE_RE.match(stripped)
        or _SHARE_EDIT_TALENT_TO_RE.match(stripped)
        or _SHARE_EDIT_REMOVE_RE.match(stripped)
        or _SHARE_EDIT_ADD_RE.match(stripped)
    )


async def _share_handle_confirming_reply(
    text: str, collected: Dict[str, str], ctx: ExecContext,
) -> Optional[str]:
    """AgentDefinition.handle_confirming_reply hook — checked FIRST while
    a SHARE conversation sits in "confirming", but only ever ACTS when
    session.pending_disambiguation.kind == "share_pipeline_check" (set by
    _build_share_confirmation's Pipeline Check gate, Production fix
    2026-09-04). 1/2/3 there mean Add+Move+Share / Share-only-valid-pairs
    / Cancel — a completely different decision from the ordinary
    confirmation card's own "1 -> Approve" (approving THIS card would try
    to send pairs that aren't in the pipeline yet) — so it must be
    intercepted before the generic parser reaches it. Returns None for
    every other case (no pending gate at all), leaving the ordinary
    1/2/3 Approve/Edit/Cancel behaviour of every other SHARE confirmation
    completely unchanged."""
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    pending = (session or {}).get("pending_disambiguation")
    if not pending or pending.get("kind") != "share_pipeline_check":
        return None
    stripped = (text or "").strip()

    # Re-resolve fresh rather than trusting anything stashed on `pending`
    # — collected (the ORIGINAL SHARE request's own fields) is the single
    # source of truth here, exactly as everywhere else in this module, so
    # there's no separate copy of talent/project data to go stale.
    resolved = await _resolve_share(collected)
    if not resolved.ok:
        await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
        await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
        return resolved.error
    matrix = await _share_pipeline_matrix(resolved)
    missing = [row for row in matrix if not row.in_pipeline]
    valid = [row for row in matrix if row.in_pipeline]

    if stripped == "1":
        if not missing:
            return None  # nothing left to add/move — let the normal flow re-check.
        # Hand off to the EXISTING, already-production-verified ADD ->
        # MOVE -> SHARE compound-plan engine — never a second, invented
        # execution path. Built the exact same way a user would type it
        # themselves, then dispatched as a fresh casting.add trigger.
        compound_text = _build_share_gate_add_move_share_text(resolved, missing, collected)
        add_fields = _extract_add_fields(compound_text)
        await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
        await conversation.start_conversation(
            agent_id=ctx.agent_id, phone=ctx.sender_phone,
            group_name=ctx.group_name, intent_id="casting.add", collected=add_fields,
        )
        await conversation.update_conversation(ctx.agent_id, ctx.sender_phone, step="confirming")
        return await _build_add_confirmation(add_fields, ctx)

    if stripped == "2":
        await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
        if not valid:
            await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
            return (
                "None of the requested talent × project pairs are in a "
                "pipeline yet, so there's nothing to share.\n\n"
                "Nothing was sent."
            )
        restriction: Dict[str, List[str]] = {}
        for row in valid:
            restriction.setdefault(row.project_id, []).append(row.talent_id)
        new_collected = dict(collected)
        new_collected[SHARE_PAIR_RESTRICTION_FIELD.key] = json.dumps(restriction)
        await conversation.update_conversation(
            ctx.agent_id, ctx.sender_phone, step="confirming", collected=new_collected,
        )
        restricted = await _resolve_share(new_collected)
        if not restricted.ok:
            return restricted.error
        preview = _build_share_confirmation_preview(restricted)
        if missing:
            skip_lines = "\n".join(f"• {row.talent_label} — {row.project_label}" for row in missing)
            preview = f"Skipping (not in pipeline):\n{skip_lines}\n\n{preview}"
        return preview

    if stripped == "3" or parse_confirmation_reply(stripped) == "cancel":
        await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
        await conversation.clear_conversation(ctx.agent_id, ctx.sender_phone)
        return "CANCELLED\n\nNothing from the pending SHARE action was executed or sent."

    return None


SHARE_CUSTOM_MESSAGE_FIELD = FieldSpec(
    key="custom_message", label="Message", question="", validate=_validate_hidden, required=False,
)

# Pipeline-Check Option 2 (Production fix, 2026-09-04) — a hidden field,
# same pattern as SHARE_CUSTOM_MESSAGE_FIELD above: never surfaced to the
# admin, never asked for, just carries a JSON {project_id: [talent_id,...]}
# restriction across turns inside `collected` (the ONLY thing conversation
# state persists) after "2 -> Share only where already in the pipeline".
# See _resolve_share's own use of it and _share_handle_confirming_reply.
SHARE_PAIR_RESTRICTION_FIELD = FieldSpec(
    key="_share_pair_restriction", label="Pair restriction", question="",
    validate=_validate_hidden, required=False,
)


SHARE_INTENT = IntentDefinition(
    intent_id="casting.share",
    triggers=SHARE_TRIGGERS,
    fields=[
        SHARE_PROJECT_FIELD, SHARE_RECIPIENT_FIELD, SHARE_TEMPLATE_FIELD, SHARE_CUSTOM_MESSAGE_FIELD,
        SHARE_PAIR_RESTRICTION_FIELD, AUTO_CONFIRM_FIELD, PLAN_STEP_EDIT_ERROR_FIELD,
    ],
    executor=_share_executor,
    extract_fields=_extract_share_fields,
    build_confirmation=_build_share_confirmation,
    build_edit_prompt=_build_share_edit_prompt,
    try_auto_execute=_share_try_auto_execute,
    handle_confirming_reply=_share_handle_confirming_reply,
    # Production fix (2026-09-03) — standalone SHARE's own numbered
    # talent/project/template ambiguity clarification (_resolve_share's
    # disambiguation payloads, identical shape to ADD/MOVE's own) was
    # never actually resumable: with no parse_edits_async set, a reply
    # like "1" fell through to the fully generic "Key = value" parser and
    # produced "I couldn't understand that. Try: Role = Casting Director"
    # instead of continuing the pending SHARE. _share_parse_edits_async
    # (Part 12 natural-language editing) falls through to
    # _move_parse_edits_async — already fully generic (reads session.
    # pending_disambiguation, keyed only by field_key/options — never
    # anything MOVE-specific; its one MOVE-only branch, "retry_global", is
    # a kind SHARE never produces) — for that numbered-reply resume, so
    # nothing about it is duplicated, only extended.
    parse_edits_async=_share_parse_edits_async,
    # Production fix (2026-09-03) — without this, a guided edit reply
    # that happens to start with a trigger word ("Share it with X" starts
    # with SHARE's own "share"; "Add another talent: X" starts with ADD's
    # "add") restarted a brand-new command instead of routing into
    # _share_parse_edits_async, silently discarding the edit in progress.
    # See _share_editing_claims_reply's own docstring.
    claims_editing_reply=_share_editing_claims_reply,
    summary_title="You are about to share:",
)


# ---------------------------------------------------------------------------
# casting.send — "send - Talent - Project" (2026-08-24). An INDEPENDENT
# consumer of the same @Gunwanti + mark WhatsApp source media UPLOAD
# resolves — never requires submission.media[], media_assignments, or a
# prior upload. Talent identity resolution (name candidates -> source
# WhatsApp group -> email-authoritative talent via
# media_assignment.resolve_authoritative_talent_for_upload) is IDENTICAL
# to upload's, reused unchanged — only what happens after resolution
# differs: SEND creates a scan request marked workflow="send" with an
# explicit destination_group, which the backend orchestrator
# (services/media_assignment_worker.py) routes to SEND-specific
# post-scan handling instead of upload's Cloudinary/submission path.
# ---------------------------------------------------------------------------
SEND_TALENT_FIELD = FieldSpec(
    key="talent_selector", label="Talent",
    question="Who should I send media for?",
    validate=_validate_selector, aliases=["talent", "who"],
)

SEND_PROJECT_FIELD = FieldSpec(
    key="project_query", label="Project",
    question=(
        "SEND needs a talent and project.\n\n"
        "Example:\n"
        "SEND Ayra Krishna for Score Condoms\n\n"
        "Nothing has been sent."
    ),
    validate=_validate_project_query, aliases=["project", "for"],
)


def _validate_passthrough(v: str) -> ValidationResult:
    return ValidationResult(ok=True, value=v)


# A hidden, never-prompted-for (required=False) field (Phase 2, 2026-08-26)
# — the real state an outgoing-form edit changes (the admin's field
# overrides) lives in media_send.SEND_APPROVALS_COLLECTION, a durable
# record keyed on (talent, project, destination), NOT in this generic
# `collected` dict. But the dispatcher's own edit-loop
# (agents/dispatcher.py's _collect_or_advance/_advance_task) silently
# drops any key parse_edits_async returns that isn't a declared
# IntentDefinition.fields key, and treats a genuinely EMPTY edits dict as
# "I didn't understand that" — so _send_parse_edits_async returns this
# field's key (set to any non-empty sentinel) whenever it successfully
# applied a form-field edit, purely so the generic engine recognizes the
# turn as understood; nothing ever reads this key back out of `collected`.
SEND_FORM_EDIT_FIELD = FieldSpec(
    key="_send_form_edit_marker", label="Form Edit", question="",
    validate=_validate_passthrough, required=False,
)


# P0 fix (2026-08-30) — "send her THE CASTING CALL" / "send both THE
# CASTING CALLS" / "Send Kripa THE CASTING CALL for Project" is the
# canonical natural phrasing for SEND (Command Resolution spec, Parts 2/4/
# 5/9) — "the casting call"/"submission"/"media" here is a pure object
# noun describing WHAT gets sent, never part of a talent name. Left
# unstripped, it used to glue onto the pronoun/name ("her the casting
# call", "Kripa the casting call") before either extractor ever saw it:
# _plan_selector_is_implicit's exact "== pronoun word" check then failed
# (the raw text was never literally "her"), so the compound-plan inherit-
# from-touched_pairs branch never fired, and the whole garbled string got
# fuzzy-matched against the ENTIRE talent database instead — the exact
# root cause of a real production incident where "send her the casting
# call" resolved to a completely unrelated talent. Stripped ONCE, in one
# shared helper, before EITHER extractor (standalone or compound-plan)
# does anything else — "her"/"both"/"Kripa" is what's left, exactly the
# text _plan_selector_is_implicit/_resolve_talent_query_target already
# know how to handle correctly.
_SEND_OBJECT_FILLER_RE = re.compile(
    r"\b(?:the\s+)?casting\s+calls?\b",
    re.IGNORECASE,
)


def _strip_send_object_filler(text: str) -> str:
    cleaned = _SEND_OBJECT_FILLER_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_send_fields(text: str) -> Dict[str, str]:
    """"send - Talent - Project" (hyphen), "SEND Talent for Project" /
    "SEND Talent the casting call for Project" (canonical natural
    language — the explicit "for" connector checked first, same regex the
    compound-plan SEND step uses), or "send Talent Project" (space-
    separated, boundary resolved later via the DB-aware
    _resolve_freeform_talent_project) — own trigger ("send")."""
    _, remainder = nlu._strip_leading_trigger(text or "", ["send"])
    remainder = (remainder or "").strip()
    if not remainder:
        return {}
    fields = nlu._split_hyphen_fields(remainder, 2)
    if fields:
        talent_part, project_part = fields
        return {"talent_selector": talent_part, "project_query": project_part}
    remainder = _strip_send_object_filler(remainder)
    if not remainder:
        return {}
    m = _SEND_STEP_FOR_PROJECT_RE.match(remainder)
    if m:
        return {"talent_selector": m.group(1).strip(), "project_query": m.group(2).strip()}
    return {"talent_selector": remainder, "project_query": remainder}


# Compound Actions (2026-08-27) — "SEND her for TVS Jupiter" as a plan
# step's own raw_text. Deliberately separate from _extract_send_fields
# above (which handles the STANDALONE hyphen/space-separated forms, the
# latter resolved via the DB-querying _resolve_freeform_talent_project) —
# a compound-plan SEND clause always has an explicit "for" connector once
# translated to natural language, so a small dedicated regex is enough;
# no new DB-aware boundary-guessing needed.
_SEND_STEP_FOR_PROJECT_RE = re.compile(r"^(.*?)\s+for\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _extract_send_fields_for_plan(raw_text: str) -> Dict[str, str]:
    _, remainder = nlu._strip_leading_trigger(raw_text or "", ["send"])
    remainder = _strip_send_object_filler((remainder or "").strip())
    if not remainder:
        return {}
    m = _SEND_STEP_FOR_PROJECT_RE.match(remainder)
    if m:
        return {"talent_selector": m.group(1).strip(), "project_query": m.group(2).strip()}
    return {"talent_selector": remainder, "project_query": ""}


def _plan_selector_is_implicit(raw: str) -> bool:
    """True when a compound-plan clause's own talent/recipient reference
    names no one of its own — empty, or a pronoun ("her"/"them"/"both"/
    ...) — reusing the SAME pronoun vocabulary MOVE's own implicit-
    continuation already recognizes (nlu.parse_talent_selector), not a
    second pronoun list. Shared by the SHARE and SEND plan-step
    resolvers below."""
    if not raw:
        return True
    selector = nlu.parse_talent_selector(raw)
    return bool(selector.ok and selector.name_query == nlu.PRONOUN_LAST_MARKER)


# P0 fix (2026-08-30) — singular vs. plural pronoun inheritance. "both"/
# "them" (or the clause naming no one at all) genuinely mean "everyone
# this plan has touched so far" — but "her"/"him"/"this one" are SINGULAR:
# if the plan has touched more than one distinct talent, "her" does NOT
# unambiguously mean all of them, and silently joining every touched
# talent under a singular pronoun is exactly the class of bug that sent a
# casting call to a completely unrelated talent in production (a
# differently-shaped bug — an unstripped filler phrase defeated the
# pronoun check entirely — but the underlying principle is the same:
# a singular reference must resolve to exactly one talent or ask, never
# guess/merge). Scoped to singular pronouns ONLY; "both"/"them"/empty are
# unaffected and keep inheriting the full touched set exactly as before.
_SINGULAR_PRONOUN_WORDS = {"him", "her", "this one", "that one", "this", "that"}


def _resolve_plan_pronoun_talents(
    raw: str, touched_pairs: List[Dict[str, str]],
) -> "Tuple[List[str], Optional[str]]":
    """Resolves an implicit/pronoun talent reference against touched_pairs.
    Returns (talent_labels, error) — error is None on success. "both"/
    "them"/an unnamed reference inherit every distinct talent this plan
    has touched; a singular pronoun ("her"/"him"/"this"/"that") only
    resolves that way when touched_pairs names exactly ONE distinct
    talent — with more than one, "her" is genuinely ambiguous, so this
    returns a clarification message instead of picking or merging."""
    seen: List[str] = []
    for pair in touched_pairs:
        if pair["talent_label"] not in seen:
            seen.append(pair["talent_label"])
    if raw.strip().lower() in _SINGULAR_PRONOUN_WORDS and len(seen) > 1:
        options = "\n".join(f"{i + 1}. {name}" for i, name in enumerate(seen))
        return [], (
            f'"{raw.strip()}" could mean more than one talent from this plan:\n\n{options}\n\n'
            "Please name the talent explicitly instead (e.g. the exact name)."
        )
    return seen, None


def _resolve_send_step_for_plan(raw_text: str, touched_pairs: List[Dict[str, str]]) -> Dict[str, str]:
    """Resolves one 'casting.send' plan step's talent/project text —
    inheriting an implicit/pronoun reference from `touched_pairs` (the
    SAME plan-wide accumulator MOVE's own PRONOUN_LAST_MARKER fan-out and
    _resolve_share_step_for_plan both use), exactly like SHARE's plan-step
    resolver. Returns raw {"talent_selector", "project_query"} text —
    NOT yet resolved against the database; casting.send's own existing
    build_confirmation/executor do that unchanged, via the normal
    conversation hand-off (see _execute_plan's casting.send branch). A
    dict with an "error" key (and no "talent_selector") means the pronoun
    could not be safely resolved — see _resolve_plan_pronoun_talents."""
    fields = _extract_send_fields_for_plan(raw_text)
    talent_raw = (fields.get("talent_selector") or "").strip()
    project_raw = (fields.get("project_query") or "").strip()

    if _plan_selector_is_implicit(talent_raw) and touched_pairs:
        labels, err = _resolve_plan_pronoun_talents(talent_raw, touched_pairs)
        if err:
            return {"error": err}
        talent_raw = ",".join(labels)

    if not project_raw and touched_pairs:
        seen_p: List[str] = []
        for pair in touched_pairs:
            if pair["project_label"] not in seen_p:
                seen_p.append(pair["project_label"])
        project_raw = ",".join(seen_p)

    return {"talent_selector": talent_raw, "project_query": project_raw}


async def _resolve_freeform_talent_project(
    full_text: str,
) -> Tuple[Optional[str], Optional[str], Optional[ExecResult]]:
    """Space-separated "Talent Project" has no delimiter, so the boundary
    is determined by trying every word-split point against REAL talent
    and project records — never guessed, never a fixed word count.
    Returns (talent_text, project_text, None) once exactly one split
    point resolves BOTH sides unambiguously, or (None, None, ExecResult)
    to STOP the caller immediately on failure/ambiguity. The winning
    split's raw text fragments are returned (not the resolved ids) so
    the caller's existing, already-proven talent/project resolution code
    runs exactly once, the same way it does for the hyphen syntax —
    avoiding two different resolution code paths that could disagree."""
    words = (full_text or "").split()
    if len(words) < 2:
        return None, None, ExecResult(
            ok=False, error="freeform_too_short",
            message=f'I couldn\'t tell who and what project you meant in "{full_text}" — '
                    'try "Talent - Project" or make sure both a talent name and a project name are included.',
        )
    projects = await _fetch_ongoing_projects()
    # Keyed by the RESOLVED (talent_id, project_id) pair, not by which
    # split text produced it — several split points can independently
    # fuzzy-match down to the exact same talent/project (e.g. "Ahana",
    # "Ahana Pocha", and "Ahana Pocha Freeform" can all resolve to the
    # same talent record). That's the same answer found multiple ways,
    # never genuine ambiguity; only DISTINCT resolved pairs count as
    # competing candidates. Among splits sharing a resolved pair, the
    # one whose raw text is closest to the canonical labels wins — a
    # short prefix like "Ahana" can fuzzy-resolve to the same talent as
    # the full "Ahana Pocha Freeform", but the full text is the correct
    # fragment to hand back to the caller's own resolver.
    winners_by_id: Dict[Tuple[str, str], Tuple[str, str, str, str]] = {}
    scores_by_id: Dict[Tuple[str, str], int] = {}
    for split_at in range(1, len(words)):
        talent_text = " ".join(words[:split_at])
        project_text = " ".join(words[split_at:])
        talent_id, talent_label, talent_err, talent_ambiguous = await _resolve_talent_query_target(talent_text)
        if talent_err or talent_ambiguous or not talent_id:
            continue
        with request_scope.stage("fuzzy"):
            project_match = nlu.resolve_project_by_name(project_text, projects)
        if project_match.ambiguous or not project_match.project:
            continue
        key = (talent_id, project_match.project["id"])
        project_label = project_match.project["label"]
        score = (
            int(talent_text.strip().lower() == (talent_label or "").strip().lower())
            + int(project_text.strip().lower() == (project_label or "").strip().lower())
        )
        if key not in winners_by_id or score > scores_by_id[key]:
            winners_by_id[key] = (talent_text, project_text, talent_label or talent_text, project_label)
            scores_by_id[key] = score

    winners = list(winners_by_id.values())
    if len(winners) == 1:
        talent_text, project_text, _, _ = winners[0]
        return talent_text, project_text, None
    if not winners:
        return None, None, ExecResult(
            ok=False, error="freeform_unresolved",
            message=f'I couldn\'t determine the talent/project split in "{full_text}" — '
                    'no combination matched both a real talent and a real project. '
                    'Try "Talent - Project" to be explicit.',
        )
    # More than one split point independently resolves — genuinely
    # ambiguous, never auto-picked (e.g. a talent name that is itself a
    # prefix of a longer talent name, both with valid same-named projects).
    options = "\n".join(
        f"{i + 1}. {t_label} — {p_label}" for i, (_, _, t_label, p_label) in enumerate(winners)
    )
    return None, None, ExecResult(
        ok=False, error="freeform_ambiguous",
        message=f'"{full_text}" could mean more than one talent/project combination:\n\n{options}\n\n'
                'Please use "Talent - Project" to be explicit.',
    )


async def _resolve_send_target(
    collected: dict, *, destination_group_override: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[ExecResult]]:
    """Shared resolution steps for casting.send (2026-08-26 approval-flow
    refactor) — used identically by _build_send_confirmation (rendering/
    refreshing the outgoing-form preview), _send_parse_edits_async
    (validating an edit against the CURRENT resolved talent/project), and
    _send_executor (re-verified fresh at approval time — same reasoning as
    _move_executor re-checking _resolve_move_selection rather than
    trusting whatever the confirmation card last saw). Returns
    (resolved-context-dict, None) on success or (None, ExecResult) with the
    exact error to show, unchanged from the original single-function
    _send_executor this was extracted from.

    `destination_group_override` is a test-only seam (never set by the
    real chat-dispatch path) — lets a disposable E2E point SEND at a
    throwaway WhatsApp group without touching casting-agent's own
    production `group_names` config, mirroring how upload's own disposable
    tests call `_upload_executor` directly rather than through the full
    chat pipeline."""
    talent_selector = collected.get("talent_selector") or ""
    project_query = collected.get("project_query") or ""

    # Space-separated command (2026-08-25) — see _extract_send_fields.
    if talent_selector.strip() and talent_selector.strip() == project_query.strip():
        talent_selector, project_query, freeform_error = await _resolve_freeform_talent_project(talent_selector)
        if freeform_error is not None:
            return None, freeform_error

    # Step 1: resolve the project FIRST — identical reasoning to upload
    # (the authoritative-talent lookup needs project_id).
    projects = await _fetch_ongoing_projects()
    with request_scope.stage("fuzzy"):
        match = nlu.resolve_project_by_name(project_query, projects)
    if match.ambiguous:
        options = "\n".join(f"{i + 1}. {o['label']}" for i, o in enumerate(match.ambiguous))
        return None, ExecResult(
            ok=False, error="ambiguous_project",
            message=f"I found multiple projects.\n\n{options}\n\nPlease re-run with the exact project name.",
        )
    if not match.project:
        return None, ExecResult(
            ok=False, error="project_not_found",
            message=f'I couldn\'t find a project matching "{project_query}".',
        )
    project = match.project

    # Step 2: name resolution -> candidate set (never the authoritative
    # destination by itself) — identical to upload's own step 2.
    talent_id, talent_label, err, ambiguous = await _resolve_talent_query_target(talent_selector)
    if ambiguous:
        candidate_ids = [c.id for c in ambiguous]
        candidate_label = ambiguous[0].label
    elif talent_id:
        candidate_ids = [talent_id]
        candidate_label = talent_label
    else:
        return None, ExecResult(ok=False, error="talent_not_found", message=err or "No matching talent found.")

    # Step 3: the WhatsApp SOURCE group — identical to upload's step 3.
    candidate_docs = await db.talents.find(
        {"id": {"$in": candidate_ids}}, {"_id": 0, "id": 1, "whatsapp_group_name": 1},
    ).to_list(20)
    group_names = {(d.get("whatsapp_group_name") or "").strip() for d in candidate_docs if (d.get("whatsapp_group_name") or "").strip()}
    if not group_names:
        return None, ExecResult(
            ok=False, error="no_whatsapp_group",
            message=f"{candidate_label} has no WhatsApp group configured — the mark-based "
                    "send workflow requires one. Add it in Talentgram first.",
        )
    if len(group_names) > 1:
        return None, ExecResult(
            ok=False, error="ambiguous_whatsapp_group",
            message=f"Multiple different WhatsApp groups are configured across talent records named "
                    f"{candidate_label} — please resolve the duplicate talent records first.",
        )
    group_name = next(iter(group_names))

    # Step 4: email-authoritative talent resolution — the EXACT SAME
    # function upload uses, unchanged. SEND never resolves its source
    # solely from name when this relationship is available.
    auth = await media_assignment.resolve_authoritative_talent_for_upload(project["id"], candidate_ids)
    if not auth.ok:
        template = _UPLOAD_RESOLUTION_ERROR_MESSAGES.get(
            auth.error, "Could not verify the send source for {talent_label} / {project_label} ({error})."
        )
        message = template.format(talent_label=candidate_label, project_label=project["label"], error=auth.error)
        return None, ExecResult(ok=False, error=f"send_source_unresolved:{auth.error}", message=message)

    authoritative_talent_id = auth.talent_id
    authoritative_talent_label = auth.talent_label or candidate_label

    identity = await media_assignment.get_gunwanti_identity()
    if not identity or not identity.get("lid"):
        return None, ExecResult(
            ok=False, error="identity_not_configured",
            message="The Gunwanti agent identity (WhatsApp LID) is not configured yet — "
                    "contact an admin before using send.",
        )

    # Step 5: the DESTINATION — an explicit, project-level field, never
    # derived from casting-agent's own global `group_names` config (that
    # config drives a different, unrelated inbound-listening surface and
    # must stay untouched) and never string-appended from brand_name.
    # If the project has no destination group configured, SEND refuses
    # rather than falling back to anything guessed.
    project_doc = await db.projects.find_one({"id": project["id"]}, {"_id": 0})
    if destination_group_override:
        destination_group = destination_group_override
    else:
        destination_group = ((project_doc or {}).get("whatsapp_casting_group_name") or "").strip()
        if not destination_group:
            return None, ExecResult(
                ok=False, error="destination_not_configured",
                message=f"{project['label']} has no WhatsApp casting group configured — "
                        "add it to the project before using send.",
            )

    # Step 6 (2026-08-26 revision): SEND sends the talent's submission
    # details — the submission's OWN `decision` (pending/approved/etc.) is
    # NOT a prerequisite here. The two concepts are deliberately kept
    # separate: submission.decision reflects the recruiter's ordinary
    # review workflow, while SEND has its own explicit approval gate (the
    # admin approving the outgoing form/SEND operation itself — see
    # _build_send_confirmation/_send_parse_edits_async). A submission
    # must still EXIST (there is no data to build a form from otherwise),
    # but its decision can be "pending" — the admin approving the SEND
    # form is what makes this safe, not a prior submission-review verdict.
    # Once the full SEND operation succeeds, the submission's decision is
    # transitioned to "approved" via the real production mechanism (see
    # services/media_assignment_worker.py's SEND completion branch) —
    # never before, and never merely because form/media prep succeeded.
    submission = await db.submissions.find_one(
        {"project_id": project["id"], "talent_id": authoritative_talent_id},
        {"_id": 0}, sort=[("submitted_at", -1), ("created_at", -1)],
    )
    if not submission:
        return None, ExecResult(
            ok=False, error="no_submission",
            message=f"{authoritative_talent_label} has no submission for {project['label']} — "
                    "send requires a submission to build the outgoing form from.",
        )

    return {
        "project": project, "project_doc": project_doc,
        "authoritative_talent_id": authoritative_talent_id,
        "authoritative_talent_label": authoritative_talent_label,
        "group_name": group_name, "destination_group": destination_group,
        "submission": submission,
    }, None


def _send_approval_overrides(existing: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """A "completed" approval is a terminal record of a PAST SEND
    operation — a fresh "send" invocation for the same talent/project
    (e.g. new media marked afterward) must start from the submission's own
    values again, not silently inherit a previous operation's edits."""
    if not existing or existing.get("status") == media_send.SEND_APPROVAL_STATUS_COMPLETED:
        return {}
    return dict(existing.get("overrides") or {})


async def _build_bulk_send_confirmation(pairs: List[Tuple[str, str]], collected: dict) -> str:
    """Resolves every (talent, project) pair independently — a single
    ambiguous/unresolvable pair reports that exact problem and stops the
    ENTIRE bulk request (never dispatches the pairs that DID resolve while
    silently dropping the ones that didn't; never guesses). Only once
    every pair resolves cleanly does this show the combined preview."""
    resolved: List[Dict[str, Any]] = []
    for talent_sel, project_q in pairs:
        target, err = await _resolve_send_target({"talent_selector": talent_sel, "project_query": project_q})
        if err is not None:
            return (
                f'For "{talent_sel} / {project_q}":\n\n{err.message}\n\n'
                f"Nothing in this bulk send request has been previewed or sent yet — "
                f"fix this pair and re-run the full command."
            )
        resolved.append(target)

    lines = [
        "SEND FORM PREVIEW (BULK) — 🚫 Nothing Has Been Sent Yet", "",
        f"{len(resolved)} independent sends will be prepared, each using that talent's own "
        f"submission form. Nothing has gone out, and nothing will, until you explicitly "
        f"approve below.", "",
    ]
    for t in resolved:
        lines.append(f"• {t['authoritative_talent_label']} → {t['project']['label']} → {t['destination_group']}")
    lines += [
        "",
        "Editing an individual form isn't supported for a bulk send — cancel and re-run a "
        "single \"send - Talent - Project\" first if you need to edit one before sending.",
        "",
        "Reply:",
        "1 → Approve all (starts sending every pair above: Takes → Introduction → Form → Pictures → ☑️, one after another)",
        "3 → Cancel",
    ]
    return "\n".join(lines)


def _send_selector_pairs(collected: dict) -> List[Tuple[str, str]]:
    """Command Enhancement (2026-08-27) — bulk SEND: "send - Talent A,
    Talent B - Project A,Project B" (or the natural-language equivalent)
    fans out to one independent (talent, project) pair per cross-product
    combination, exactly the same comma-splitting nlu.split_multi_names
    already gives ADD/MOVE. Returns the SINGLE (talent_selector,
    project_query) pair UNCHANGED (no split at all) whenever neither field
    contains a comma — the space-separated freeform shape (talent_selector
    == project_query, resolved later by _resolve_freeform_talent_project)
    never contains one either, so it is also completely unaffected."""
    talent_selector = (collected.get("talent_selector") or "").strip()
    project_query = (collected.get("project_query") or "").strip()
    if "," not in talent_selector and "," not in project_query:
        return [(talent_selector, project_query)]
    talents = nlu.split_multi_names(talent_selector) or [talent_selector]
    projects = nlu.split_multi_names(project_query) or [project_query]
    return [(t, p) for t in talents for p in projects]


async def _build_send_confirmation(collected: dict, ctx: ExecContext) -> str:
    """Phase 2 (2026-08-26) — the required explicit approval step: renders
    the EXACT outgoing SEND form (Project Name/Name/Age/.../Budget, nothing
    else — see media_send.build_form_send_message) as the confirmation
    card itself, so "show the form to the admin" and "ask the admin to
    approve/edit/cancel" are the same generic confirm/edit/cancel gate
    every other intent already uses. Never sends anything — approval only
    happens in _send_executor, once the admin replies 1.

    Bulk (2026-08-27): 2+ (talent, project) pairs show one combined
    preview listing every pair that resolved cleanly — any single pair
    that's ambiguous or unresolvable STOPS the whole request and asks for
    clarification (never dispatches the pairs that DID resolve while
    silently dropping the ones that didn't)."""
    pairs = _send_selector_pairs(collected)
    if len(pairs) > 1:
        return await _build_bulk_send_confirmation(pairs, collected)

    target, err = await _resolve_send_target(collected)
    if err is not None:
        return err.message

    talent_id = target["authoritative_talent_id"]
    project_id = target["project"]["id"]
    destination_group = target["destination_group"]

    existing = await media_send.get_send_approval(talent_id, project_id, destination_group)
    if existing and existing.get("status") == media_send.SEND_APPROVAL_STATUS_APPROVED:
        # Already approved on an earlier turn (e.g. resuming after a
        # worker-side failure) — reuse the FROZEN message verbatim rather
        # than regenerating it, so a retry never shows/sends different
        # wording than what was actually approved.
        header = "SEND FORM — Already Approved (resuming — the form itself is not re-sent)"
        message = existing["message"]
    else:
        overrides = _send_approval_overrides(existing)
        built = media_send.build_form_send_message(
            target["submission"], target["project_doc"],
            target["authoritative_talent_label"], target["project"]["label"], overrides,
        )
        await media_send.save_send_approval_draft(
            talent_id=talent_id, project_id=project_id, destination_group=destination_group,
            submission_id=target["submission"]["id"], overrides=overrides,
            message=built["message"], content_hash=built["content_hash"],
        )
        header = "SEND FORM PREVIEW — 🚫 Nothing Has Been Sent Yet"
        message = built["message"]

    return (
        f"{header}\n\n"
        f"This is the EXACT form that will be sent. Nothing has gone out, and nothing "
        f"will, until you explicitly approve below.\n\n"
        f"Destination: {destination_group}\n\n"
        f"{message}\n\n"
        'Edit any field with e.g. "Age = 24" (one or more lines).\n\n'
        "Reply:\n"
        "1 → Approve (starts sending: Takes → Introduction → this form → Pictures → ☑️)\n"
        "2 → Edit\n"
        "3 → Cancel"
    )


_EDIT_LINE_RE = re.compile(r"^\s*(.+?)\s*[:=]\s*(.*)$")


async def _send_parse_edits_async(
    text: str, collected: Dict[str, str], fields: List[FieldSpec], ctx: ExecContext,
) -> Dict[str, str]:
    """Phase 2/4 (2026-08-26) — "Age = 24" (any number of lines, any of
    the outgoing form's editable fields or a project's own custom
    question text) rewrites the DRAFT approval snapshot in
    media_send.SEND_APPROVALS_COLLECTION directly; it never touches the
    underlying submission (Phase 4's explicit requirement). Falls back to
    the generic "Key = value" parser (e.g. "Talent = ...") for the
    intent's own declared fields when no form-field edit is recognized.

    Bulk (2026-08-27): per-field editing isn't supported across multiple
    pairs (which of N forms would "Age = 24" apply to?) — the bulk
    confirmation already tells the admin this and offers 1/3 only; this
    just falls through to the generic edit-instruction parser rather than
    silently editing the wrong (or every) pair's submission."""
    if len(_send_selector_pairs(collected)) > 1:
        return parse_edit_instructions(text, fields) or {}

    target, err = await _resolve_send_target(collected)
    if err is None:
        talent_id = target["authoritative_talent_id"]
        project_id = target["project"]["id"]
        destination_group = target["destination_group"]

        existing = await media_send.get_send_approval(talent_id, project_id, destination_group)
        overrides = _send_approval_overrides(existing)

        shape = _submission_to_client_shape(target["submission"], project=target["project_doc"])
        label_to_key: Dict[str, str] = {v.lower(): k for k, v in media_send.OVERRIDABLE_FIELD_LABELS.items()}
        for qa in (shape.get("custom_answers") or []):
            question = (qa.get("question") or "").strip()
            if question:
                label_to_key[question.lower()] = question

        applied = False
        for line in (text or "").splitlines():
            m = _EDIT_LINE_RE.match(line)
            if not m:
                continue
            override_key = label_to_key.get(m.group(1).strip().lower())
            if override_key is None:
                continue
            overrides[override_key] = m.group(2).strip()
            applied = True

        if applied:
            built = media_send.build_form_send_message(
                target["submission"], target["project_doc"],
                target["authoritative_talent_label"], target["project"]["label"], overrides,
            )
            await media_send.save_send_approval_draft(
                talent_id=talent_id, project_id=project_id, destination_group=destination_group,
                submission_id=target["submission"]["id"], overrides=overrides,
                message=built["message"], content_hash=built["content_hash"],
            )
            return {"_send_form_edit_marker": "1"}

    explicit = parse_edit_instructions(text, fields)
    if explicit:
        return explicit
    return {}


async def _send_executor(
    collected: dict, ctx: ExecContext, *, destination_group_override: Optional[str] = None,
) -> ExecResult:
    """Runs ONLY once the admin has explicitly approved the outgoing form
    (Phase 2) — never merely because the underlying submission itself is
    approved. Re-resolves talent/project/destination/submission fresh
    (same reasoning as _move_executor not trusting the confirmation
    card's own snapshot), then freezes whatever draft/overrides exist as
    the approved snapshot (Phase 4) before dispatching.

    Bulk (2026-08-27): 2+ (talent, project) pairs re-resolves (same
    "never trust the confirmation card's own snapshot" reasoning) and
    dispatches each pair as its OWN completely independent send operation
    — its own approval snapshot, its own scan_request, its own idempotency
    key (talent_id, project_id, destination_group) — via the exact same
    single-pair code every non-bulk send already uses, just called once
    per pair. One pair failing (e.g. no submission) never blocks or rolls
    back any other pair already dispatched."""
    pairs = _send_selector_pairs(collected)
    if len(pairs) > 1:
        summaries = []
        any_ok = False
        for talent_sel, project_q in pairs:
            result = await _send_one_pair(
                {"talent_selector": talent_sel, "project_query": project_q}, ctx,
                destination_group_override=destination_group_override,
            )
            any_ok = any_ok or result.ok
            mark = "✓" if result.ok else "✗"
            summaries.append(f"{mark} {talent_sel} → {project_q}\n{result.message}")
        header = f"✅ Approved by {ctx.sender_phone} — {len(pairs)} independent sends dispatched:\n\n"
        return ExecResult(ok=any_ok, message=header + "\n\n".join(summaries))

    return await _send_one_pair(collected, ctx, destination_group_override=destination_group_override)


async def _send_one_pair(
    collected: dict, ctx: ExecContext, *, destination_group_override: Optional[str] = None,
) -> ExecResult:
    """The exact, unchanged single-(talent, project) SEND dispatch —
    extracted verbatim from the original single-item _send_executor body
    so bulk and non-bulk sends run identical code, never two copies that
    could drift apart."""
    target, err = await _resolve_send_target(collected, destination_group_override=destination_group_override)
    if err is not None:
        return err

    talent_id = target["authoritative_talent_id"]
    talent_label = target["authoritative_talent_label"]
    project = target["project"]
    destination_group = target["destination_group"]
    submission = target["submission"]

    existing = await media_send.get_send_approval(talent_id, project["id"], destination_group)
    overrides = _send_approval_overrides(existing)
    form_built = media_send.build_form_send_message(
        submission, target["project_doc"], talent_label, project["label"], overrides,
    )
    await media_send.save_send_approval_draft(
        talent_id=talent_id, project_id=project["id"], destination_group=destination_group,
        submission_id=submission["id"], overrides=overrides,
        message=form_built["message"], content_hash=form_built["content_hash"],
    )
    await media_send.approve_send_form(talent_id, project["id"], destination_group, approved_by=ctx.sender_phone)

    form_message: Optional[str] = None
    already_form = await media_send.already_sent_form(
        talent_id, project["id"], destination_group, form_built["content_hash"],
    )
    if not already_form:
        await media_send.record_form_send(
            talent_id=talent_id, project_id=project["id"], destination_group=destination_group,
            submission_id=submission["id"], content_hash=form_built["content_hash"], created_by="whatsapp-agent",
        )
        form_message = form_built["message"]

    await media_send.create_send_scan_request(
        talent_id=talent_id, talent_label=talent_label,
        project_id=project["id"], project_label=project["label"],
        group_name=target["group_name"], destination_group=destination_group,
        form_message=form_message, submission_id=submission["id"], content_hash=form_built["content_hash"],
    )
    return ExecResult(
        ok=True,
        message=f"✅ Approved by {ctx.sender_phone} — now sending {talent_label}'s marked "
                f"{project['label']} media to {destination_group}\n\n"
                f"Order: Takes → Introduction → Form → Pictures → ☑️ (skipping any stage with nothing marked).\n\n"
                f"I'll report back here once it's done.",
    )


# Guided Edit Prompts (2026-08-28) — the fixed, client-facing field set
# the SEND form actually shows (see media_send.build_form_send_message,
# the single source of truth this mirrors) — deliberately never the raw
# database fields UPLOAD's Client View exposes (gender/ethnicity/
# followers/skills/...), matching that function's own "never the internal/
# raw fields" contract. Custom Questions are per-submission/dynamic (the
# form above already shows their real text), so they're named generically
# here rather than re-fetched and duplicated.
_SEND_FORM_EDITABLE_FIELDS = [
    "Project Name", "Name", "Age", "Height", "Current Location",
    "Availability", "Competitive Brand", "Instagram Link",
    "Custom Questions (if shown above)", "Budget",
]


async def _build_send_edit_prompt(collected: dict, ctx: ExecContext) -> str:
    """Guided Edit Prompts (2026-08-28) — SEND keeps its own separate
    form-approval flow untouched (media_send.py, _send_executor,
    _send_parse_edits_async — none of that changes here); this only makes
    the "2 → Edit" prompt name what's actually editable on the form
    instead of the generic Role=value example."""
    lines = [
        "EDITING SEND FORM", "",
        "You can change any field shown in the form above.", "",
        "Current fields:",
    ]
    lines += [f"• {label}" for label in _SEND_FORM_EDITABLE_FIELDS]
    lines += [
        "", "Tell me the field and new value.", "",
        "Examples:",
        "• Age = 24",
        "• Current Location = Mumbai",
        "• Availability = Available",
        "• Budget = ₹25,000",
        "", "Nothing will be sent until you approve the updated form.",
    ]
    return "\n".join(lines)


async def _build_send_cancel_message(collected: dict, ctx: ExecContext) -> str:
    """Guided Cancel Messages (2026-08-28) — "nothing saved" undersells a
    SEND cancel specifically: an approved form snapshot may have existed
    (media_send.SEND_APPROVALS_COLLECTION) and is what's actually being
    discarded here, never a real media send (that only ever happens from
    _send_executor, after a genuine "1")."""
    return (
        "SEND CANCELLED\n\n"
        "Nothing was sent.\n"
        "The form approval was discarded."
    )


SEND_INTENT = IntentDefinition(
    intent_id="casting.send",
    triggers=["send"],
    fields=[SEND_TALENT_FIELD, SEND_PROJECT_FIELD, SEND_FORM_EDIT_FIELD],
    executor=_send_executor,
    extract_fields=_extract_send_fields,
    build_confirmation=_build_send_confirmation,
    build_edit_prompt=_build_send_edit_prompt,
    build_cancel_message=_build_send_cancel_message,
    parse_edits_async=_send_parse_edits_async,
    auto_confirm=False,
)


# ---------------------------------------------------------------------------
# casting.undo — restore the last move within its window. auto_confirm:
# there's nothing to approve here (the approval already happened for the
# original move) — UNDO is itself the confirmed action, a fixed 5-minute-
# window "yes I meant to reverse that" reply.
# ---------------------------------------------------------------------------
async def _undo_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    doc = await undo_store.get_undo(AGENT_ID, ctx.sender_phone)
    if doc is None:
        return ExecResult(ok=False, error="no_undo_available", message="No recent operation available to undo.")
    if undo_store.is_expired(doc):
        await undo_store.clear_undo(AGENT_ID, ctx.sender_phone)
        return ExecResult(ok=False, error="undo_expired", message="Undo period has expired.")

    # An unrelated workflow — PART 10's session-reset rule.
    await session_context.update_session(AGENT_ID, ctx.sender_phone, selection_basket=None)

    operation = doc["operation"]
    # One-shot: clear BEFORE restoring so a duplicate/concurrent "UNDO"
    # reply can't double-apply the same restore (matches "expire
    # immediately after use").
    await undo_store.clear_undo(AGENT_ID, ctx.sender_phone)

    project_id = operation["project_id"]
    new_stage = operation["new_stage"]
    previous_stage_by_id: Dict[str, str] = operation.get("previous_stage_by_id") or {}

    if not await _project_exists(project_id):
        return ExecResult(ok=False, error="project_not_found", message="Project doesn't exist.")

    # Only restore talents STILL in the stage this operation moved them to
    # — if a talent has since been moved again (by another command, or via
    # the web UI), undo must not silently clobber that later, legitimate
    # change. Uses the shared bulk-move helper for the actual write, same
    # as every other mutation in this module.
    live_rows = await _timed_talent_lookup(
        db.casting_pipeline.find(
            {"project_id": project_id, "talent_id": {"$in": list(previous_stage_by_id.keys())}},
            {"_id": 0, "talent_id": 1, "stage": 1},
        ).to_list(len(previous_stage_by_id) or 1),
        collection="casting_pipeline", name="undo_live_rows",
    )
    live_stage_by_id = {
        r["talent_id"]: (_normalise_stage(r.get("stage")) or r.get("stage")) for r in live_rows
    }

    restorable: Dict[str, List[str]] = {}  # previous_stage -> [talent_ids]
    skipped = 0
    for tid, prev_stage in previous_stage_by_id.items():
        if live_stage_by_id.get(tid) == new_stage:
            restorable.setdefault(prev_stage, []).append(tid)
        else:
            skipped += 1

    if not restorable:
        return ExecResult(ok=False, error="nothing_to_undo", message="Nothing to move.")

    before_counts = await _timed_aggregation(get_stage_counts(project_id))
    total_restored = 0
    restored_ids: List[str] = []
    for prev_stage, ids in restorable.items():
        res = await _timed_write(bulk_move_by_talent_ids(project_id, ids, prev_stage))
        total_restored += res["moved"]
        restored_ids.extend(ids)
    after_counts = await _timed_aggregation(get_stage_counts(project_id))

    # Names are looked up fresh here purely for display — nothing about the
    # undo record's stored shape or the restore logic above depends on
    # them, so this adds no new persisted field.
    restored_names = await _hydrate_names(restored_ids)
    restored_labels = sorted(
        (restored_names.get(tid, "Unknown") for tid in restored_ids), key=str.lower
    )

    lines = [
        "Undo complete.",
        "",
        "Project",
        operation.get("project_label") or "",
        "",
        f"Restored {total_restored} talent{'' if total_restored == 1 else 's'}.",
        "",
    ]
    lines.extend(f"• {name}" for name in restored_labels)
    if skipped:
        lines.append("")
        lines.append(f"({skipped} skipped — already moved again since)")
    lines.append("")
    lines.append(nlu.stage_label(new_stage))
    lines.append(f"{before_counts.get(new_stage, 0)} → {after_counts.get(new_stage, 0)}")
    for prev_stage in sorted(restorable.keys()):
        lines.append("")
        lines.append(nlu.stage_label(prev_stage))
        lines.append(f"{before_counts.get(prev_stage, 0)} → {after_counts.get(prev_stage, 0)}")
    lines.append("")
    lines.append(f"Reverted Operation ID: {operation.get('operation_id')}")

    # Audit-only enrichment — same in-place-mutation pattern as the move
    # executor (see its comment above): this turn's audit row (logged by
    # the dispatcher's auto_confirm branch right after this returns) needs
    # to clearly show a revert happened, which operation it reverted, and
    # what was restored.
    collected["reverted"] = True
    collected["project"] = operation.get("project_label")
    collected["reverted_operation_id"] = operation.get("operation_id")
    collected["restored_stage"] = nlu.stage_label(new_stage)
    collected["restored_count"] = total_restored

    return ExecResult(
        ok=True,
        message="\n".join(lines).rstrip(),
        data={
            "project_id": project_id,
            "restored": total_restored,
            "skipped": skipped,
            "operation_id": operation.get("operation_id"),
        },
    )


UNDO_INTENT = IntentDefinition(
    intent_id="casting.undo",
    triggers=["undo", "undo that"],
    fields=[],
    executor=_undo_executor,
    auto_confirm=True,
)


async def _resolve_bare_reply(text: str, ctx: ExecContext) -> Optional[Tuple[IntentDefinition, Dict[str, str]]]:
    """A bare number with no active conversation, right after "Show ongoing
    projects" (or any other handler that populated a "projects" number_map)
    — the session already knows the mapping, so "14" should open Project 14
    exactly like "Project 14" would, without making the user repeat the
    word. Scoped to projects only (talent-list numbers already work today
    via an explicit "Move N ..." command, which has its own richer
    selector grammar this isn't trying to replace).

    Also handles verb-less pipeline queries (UX polish, 2026-08-09) —
    "Toyota Follow Up", "Toyota Selected" with no leading "Show"/trigger
    word at all. nlu.extract_bare_pipeline_candidate does the pure,
    DB-free half of recognizing the "<Project> <Stage>" shape; here we do
    the DB-aware half — verifying the candidate text actually resolves to
    a REAL ongoing project via the one shared project matcher
    (resolve_project_by_name) before claiming the message. That
    verification is the whole safety net: without it, ordinary group
    chatter that happens to contain a real stage word ("put him on hold
    for now") would otherwise get silently intercepted as a pipeline
    query. Only claims the message when a real project resolves (uniquely
    or ambiguously) — an unresolvable candidate is left alone, falling
    through to "unrelated chatter, ignore", exactly as before this
    existed.

    Also handles Conversational Talent Search refinements/pagination
    (Phase 1, 2026-08-10) — "Only Mumbai", "Above 5'7\"", "Age under 22",
    "Previous 20" arrive with no leading trigger word at all. Tried LAST,
    after the digit/number_map and verb-less-pipeline checks above, so
    existing precedence is unchanged. Primary safety net: only even
    attempted when session.talent_search is already active (an unrelated
    "Mumbai shoot confirmed" with no search in progress is never touched).
    Secondary safety net: nlu.extract_talent_search_refinement/_pagination
    require an explicit filter- or page-shaped clause — a bare "ok thanks"
    returns None regardless of session state."""
    stripped = (text or "").strip()
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)

    if stripped.isdigit():
        number_map = (session or {}).get("number_map") or {}
        if number_map.get("type") == "projects":
            return QUERY_INTENT, {"query_text": f"Project {stripped}"}
    else:
        candidate = nlu.extract_bare_pipeline_candidate(stripped, list(PIPELINE_STAGE_ORDER))
        if candidate is not None:
            _stage_key, _ambiguous, project_text = candidate
            projects = await _fetch_ongoing_projects()
            with request_scope.stage("fuzzy"):
                match = nlu.resolve_project_by_name(project_text, projects)
            if match.project or match.ambiguous:
                return QUERY_INTENT, {"query_text": stripped}

    if (session or {}).get("talent_search"):
        page = nlu.extract_talent_search_pagination(stripped)
        if page is not None:
            return QUERY_INTENT, {"query_text": _TALENT_SEARCH_PAGE_MARKER + page["action"]}
        if nlu.extract_talent_search_refinement(stripped):
            return QUERY_INTENT, {"query_text": _TALENT_SEARCH_REFINE_MARKER + stripped}

    return None


# Static Help Command text (see parser.is_help_trigger / dispatcher.py).
# Hand-written, not generated from QUERY_INTENT/MOVE_INTENT/ADD_INTENT/
# UNDO_INTENT's trigger lists — those are internal NLU vocabulary (~30
# query-trigger words alone), not user-facing command syntax. Every example
# below is a real, tested command shape (see tests/test_talent_search_agent.py
# and tests/test_casting_agent.py). Update this string by hand alongside any
# future intent/command change.
HELP_TEXT = (
    "TALENTGRAM CASTING PIPELINE\n"
    "QUICK MANUAL\n\n"
    "This group manages talents, projects, and the casting pipeline — "
    "adding talents, moving them through stages, sharing casting calls, "
    "sending submissions, and checking status. Talk to it in plain "
    "English; the examples below are the reliable way to write each "
    "command.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "1. ADD\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "WHAT IT DOES: adds talent(s) to a project's pipeline, at Ask To Test.\n\n"
    "BASIC FORMAT: Add [talent] to [project]\n"
    "Add Kripa Trivedi to Parachute Jasmine Oil\n\n"
    "MULTIPLE TALENTS (comma-separate):\n"
    "Add Kripa Trivedi,Siddhi Bankhele to Parachute Jasmine Oil\n\n"
    "MULTIPLE PROJECTS (comma-separate):\n"
    "Add Kripa Trivedi to Project A,Project B\n\n"
    "MULTIPLE TALENTS + MULTIPLE PROJECTS (every combination — Kripa→A, "
    "Kripa→B, Siddhi→A, Siddhi→B, never duplicated):\n"
    "Add Kripa Trivedi,Siddhi Bankhele to Project A,Project B\n\n"
    "ADD + MOVE in one message:\n"
    "Add Kripa Trivedi to Parachute Jasmine Oil, move her to Follow Up\n\n"
    "ADD + MOVE + SHARE in one message (SHARE reuses exactly who ADD/MOVE "
    "just touched):\n"
    "Add Kripa Trivedi to Parachute Jasmine Oil, move her to Follow Up, "
    "share the casting call with her\n\n"
    "IMPORTANT NOTES:\n"
    "• Only commas separate lists — the comma never splits a real name, "
    "and a project name that itself contains \"and\" (e.g. Vaseline (Film 1 "
    "and Film 4)) is still treated as one project.\n"
    "• Extra or uneven spacing, and small typos in the word \"add\" itself "
    "(addd, ad) or natural phrasing (add ... in/for/under ...) all work — "
    "no need to type it exactly.\n"
    "• In a chained command, her/him/both/them refers to whoever ADD just "
    "touched in THAT message — never a name from an earlier, unrelated "
    "command.\n"
    "• If a talent or project name matches more than one record, I list "
    "them numbered and ask which one you mean — reply with the number and "
    "the original command continues, it doesn't need to be retyped.\n"
    "• Before anything is added, I show exactly what will happen and wait "
    "for your approval — nothing is added until you reply 1 (or \"and "
    "confirm\" to skip the approval step).\n"
    "• Adding someone already in that pipeline reports it instead of "
    "creating a duplicate entry.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "2. MOVE\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "WHAT IT DOES: moves talent(s) already in a pipeline to a different "
    "stage.\n\n"
    "HOW TO WRITE IT: Move [talent] to [stage] in [project]\n\n"
    "EXAMPLES:\n"
    "Move Kripa Trivedi to Follow Up in Parachute Jasmine Oil\n"
    "Move Kripa Trivedi,Siddhi Bankhele to Shortlisted in Project A\n\n"
    "IMPORTANT NOTES: also understands shortlist, select, reject, hold, "
    "restore, not available, not interested, and every existing stage "
    "name. Shows a preview and waits for approval, same as ADD.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "3. SHARE\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "WHAT IT DOES: shares a saved template or a custom message with "
    "talent(s), or with everyone currently in a pipeline stage. This is "
    "for sharing INFORMATION — for sending a talent's own submission, "
    "use SEND instead.\n\n"
    "THREE WAYS TO USE IT:\n\n"
    "1. Saved template → talent(s):\n"
    "Share Casting Call with Kripa Trivedi\n"
    "Share Casting Call for Parachute Jasmine Oil with Kripa Trivedi,"
    "Siddhi Bankhele\n\n"
    "2. Saved template → everyone in a pipeline stage:\n"
    "Share Casting Call for Parachute Jasmine Oil to Follow Up\n"
    "Share Casting Call with everyone in the Shortlisted stage for "
    "Project A\n\n"
    "3. Custom message → talent(s) or a pipeline stage:\n"
    "Share custom message \"Please confirm your availability.\" with "
    "Kripa Trivedi\n"
    "Share custom message \"Please confirm your availability.\" to "
    "Follow Up for Project A\n\n"
    "IMPORTANT NOTES:\n"
    "• A custom message goes in quotation marks — everything inside "
    "them (commas, line breaks, punctuation) is sent exactly as "
    "written; only a comma OUTSIDE the quotes separates multiple "
    "talents.\n"
    "• If you don't name a project, I use the one the talent is "
    "already in — if that's ambiguous (more than one), I'll ask "
    "which.\n"
    "• If a talent isn't yet in that project's pipeline, I'll offer to "
    "add and move her first rather than sending to someone who isn't "
    "there.\n"
    "• Extra/uneven spacing and small typos are tolerated; if a "
    "template, project, or talent name is ambiguous, I list numbered "
    "options and ask rather than guess.\n"
    "• Shows a preview and waits for approval before anything sends.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "4. SEND\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "WHAT IT DOES: forwards a talent's own marked WhatsApp audition "
    "media (Takes → Introduction → Form → Pictures → ☑️) to a casting "
    "WhatsApp group.\n\n"
    "HOW TO WRITE IT: Send [talent] for [project]\n\n"
    "EXAMPLES:\n"
    "Send Kripa Trivedi for Parachute Jasmine Oil\n"
    "Send Kripa Trivedi,Siddhi Bankhele for Parachute Jasmine Oil\n\n"
    "IMPORTANT NOTES: ALWAYS shows the exact form first and needs an "
    "explicit approval — nothing is ever sent automatically. Reply:\n"
    "1 → Approve\n"
    "2 → Edit a field\n"
    "3 → Cancel\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "5. UPLOAD\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "WHAT IT DOES: pulls a talent's @Gunwanti-marked WhatsApp media "
    "(takes/intro/photos) into their Talentgram submission, for the "
    "app's own review pages. Different from SEND — this never touches "
    "WhatsApp, and SEND never touches this.\n\n"
    "HOW TO WRITE IT: Upload [talent]'s marked media for [project]\n\n"
    "EXAMPLES:\n"
    "Upload Kripa Trivedi's marked media for Parachute Jasmine Oil\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "6. TESTED\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "WHAT IT DOES: checks a talent's current pipeline stage for a "
    "project.\n\n"
    "HOW TO WRITE IT: Tested [talent] for [project]\n\n"
    "EXAMPLES:\n"
    "Tested Kripa Trivedi for Parachute Jasmine Oil\n"
    "Has Kripa Trivedi tested for Parachute Jasmine Oil\n"
    "Tested Kripa Trivedi,Siddhi Bankhele for Project A,Project B\n\n"
    "IMPORTANT NOTES: answers with the ACTUAL current stage (e.g. "
    "Shortlisted) — not just yes/no. Runs immediately, no approval "
    "needed (it doesn't change anything).\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "7. SHOW\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "WHAT IT DOES: looks up ongoing projects, a project's pipeline, or "
    "which projects a talent is in.\n\n"
    "HOW TO WRITE IT: Show [projects / a project's pipeline / talent's projects]\n\n"
    "EXAMPLES:\n"
    "Show ongoing projects\n"
    "Show projects of Kripa Trivedi\n"
    "Show projects of Kripa Trivedi,Siddhi Bankhele\n"
    "Show the Follow Up pipeline of Parachute Jasmine Oil\n\n"
    "IMPORTANT NOTES: runs immediately, no approval needed.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "8. UNDO\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "WHAT IT DOES: reverses the last pipeline move, within 5 minutes — "
    "including the move half of a combined Add+Move.\n\n"
    "HOW TO WRITE IT: Undo\n\n"
    "IMPORTANT NOTES: never undoes a SEND or SHARE — those are outbound "
    "WhatsApp messages, not pipeline changes.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "9. HELP\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "WHAT IT DOES: shows this manual.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "MULTIPLE TALENTS / MULTIPLE PROJECTS\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "Comma-separate multiple talents (Kripa Trivedi,Siddhi Bankhele) or "
    "multiple projects (Project A,Project B) — I understand them as "
    "lists. Naming both means every combination:\n\n"
    "Add Kripa Trivedi,Siddhi Bankhele to Project A,Project B\n\n"
    "means Kripa→A, Kripa→B, Siddhi→A, Siddhi→B — one talent is never "
    "added twice to the same project.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "MULTIPLE COMMANDS IN ONE MESSAGE\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "ADD, MOVE, SHARE, and SEND can be chained in one message — each "
    "runs in the order you wrote it:\n\n"
    "Add Kripa Trivedi to Parachute Jasmine Oil, move her to Follow Up, "
    "share the casting call with her\n\n"
    "Add A,B to Project 1,Project 2, move both to Follow Up, send both "
    "the casting calls\n\n"
    "SEND inside a combined command still shows its own separate form "
    "and needs its own explicit approval — never bundled into the rest. "
    "Finish with \"and confirm\" to skip the approval step on everything "
    "else (SEND's own approval is never skipped).\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "\"BOTH\" / \"HER\" / \"HIM\" / \"THEM\"\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "In a combined command, an unnamed talent (her/him/both/them) means "
    "whoever the earlier steps in THAT SAME message just touched. \"Both\"/"
    "\"them\" mean everyone touched so far; \"her\"/\"him\" mean exactly one "
    "— if more than one talent could be meant, I'll ask rather than guess.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "CONFIRMATION\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "Before anything changes, I show exactly what will happen and wait:\n\n"
    "Reply:\n"
    "1 → Approve\n"
    "2 → Edit\n"
    "3 → Cancel\n\n"
    "On Edit, I'll ask specifically what you want to change about THAT "
    "pending action — no need to repeat the whole command.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "WHEN SOMETHING IS AMBIGUOUS\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "If a talent or project name matches more than one real record, "
    "I'll show numbered options and ask you to pick — I never guess on "
    "a close spelling match. Once you answer, I continue the original "
    "command automatically.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "WHEN AN ERROR OCCURS\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "I'll tell you what I understood, what's missing or wrong, and "
    "exactly what to send next — nothing is ever changed or sent when "
    "something goes wrong.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "GENERAL\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "• Spaces around commas are ignored\n"
    "• Minor spelling mistakes are tolerated, including in command words "
    "(e.g. \"mover\" for \"move\")\n"
    "• Talent search (e.g. \"Find actors in Mumbai\") and picking from a "
    "list (Select 1,3,5) still work too"
)

CASTING_AGENT = AgentDefinition(
    agent_id=AGENT_ID,
    name="Talentgram Casting Pipeline",
    module="casting_pipeline",
    intents=[QUERY_INTENT, MOVE_INTENT, ADD_INTENT, UPLOAD_INTENT, SEND_INTENT, SHARE_INTENT, UNDO_INTENT],
    resolve_bare_reply=_resolve_bare_reply,
    # Concurrent Task Engine (2026-08-05) — casting-agent is the first (and
    # so far only) agent to opt into independently-addressable, concurrent
    # WhatsApp-reply-routable operations. CRM does not set this, so it is
    # completely unaffected.
    supports_concurrent_tasks=True,
    help_text=HELP_TEXT,
)


def register() -> None:
    register_agent(CASTING_AGENT)
