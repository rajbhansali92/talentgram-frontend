"""Talentgram Casting Pipeline Agent — the second registered WhatsApp
agent, following crm.py's exact shape (field validators + executor(s) +
one register() call). Scoped to exactly one backend module: the Casting
Pipeline (backend/routers/casting_pipeline.py, db.casting_pipeline +
db.projects). This file owns all DB access for the agent; the pure
parsing/matching logic lives in casting_pipeline_nlu.py so it stays
independently testable.

Two intents:
  casting.query  — every read-only ask (project list, project detail,
                    pipeline listing/counts). auto_confirm=True: nothing
                    to approve, replies immediately.
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

from core import db

from routers.casting_pipeline import (
    LEGACY_STAGE_ALIASES,
    PIPELINE_STAGE_ORDER,
    PIPELINE_STAGES,
    _normalise_stage,
    add_talents_to_pipeline,
    bulk_move_by_talent_ids,
    get_stage_counts,
)

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
    if not current_id:
        return None, 'I don\'t know which project. Send "Project N" or "Show ongoing projects" first.', None
    return {"id": current_id, "label": current_label or ""}, None, None


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
    )
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

    return ExecResult(
        ok=False, error="unrecognized_query",
        message='I didn\'t understand that.\nTry "Show ongoing projects", "Project 3", or "Show Approved".',
    )


async def _query_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    raw_text = collected.get("query_text", "")
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)

    if raw_text == _QUERY_RESUME_MARKER:
        return await _resume_pending_query(session, ctx)

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
    pending = (session or {}).get("pending_disambiguation")
    stripped = (text or "").strip()
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
    question="Which pipeline should they move to?",
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


def _extract_move_fields(text: str) -> Dict[str, str]:
    with request_scope.stage("nlu"):
        chunks, auto_confirm = nlu.preprocess_command(text)
        out: Dict[str, str] = {}

        if len(chunks) == 1:
            fields = nlu.extract_move_fields(chunks[0], list(PIPELINE_STAGE_ORDER))
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
            chunks = [chunks[0]]

        steps = [{"intent_id": nlu.classify_chunk_intent(c) or "casting.move", "raw_text": c} for c in chunks]
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


async def _resolve_one_plan_step(step: Dict[str, str], ctx: ExecContext) -> List[Dict[str, Any]]:
    """Resolves ONE plan step (one raw-text chunk) into one or more
    resolved sub-steps — more than one only when the chunk cross-product-
    expands (multiple talent names AND/OR multiple project names on the
    SAME chunk, e.g. "Add Ahana and Prajal to Toyota and Nykaa" -> 4
    sub-steps, or "Move 4 to Approved in Toyota and ABC" -> 2). Each
    result dict: {"intent_id", "raw_text", "label" (a project label once
    resolved, else the raw chunk text), "resolved" (ResolvedMove/
    ResolvedAdd or None), "error" (str or None)}."""
    intent_id = step.get("intent_id") or "casting.move"
    raw_text = step.get("raw_text") or ""
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
        talent_names = nlu.split_multi_names(talent_raw) or [talent_raw]
        project_raw = fields.get("project_query")
        project_names = nlu.split_multi_names(project_raw) if project_raw else [None]

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

    out: List[Dict[str, Any]] = []
    # Opt 2 (2026-08-05 latency sprint): _remember_last_talent used to be
    # called once PER PAIR — for a multi-project cross-product step that's
    # N sequential write+read-back round trips to persist a value only the
    # LAST one ends up keeping (last-write-wins). Nothing within this same
    # step's pair loop ever reads session.last_talent_id back (only a
    # LATER, separate plan step does — see the docstring note below), so
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
        if resolved is not None and len(resolved.talent_ids) == 1:
            last_resolved_single = resolved
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


async def _build_plan_confirmation(collected: dict, ctx: ExecContext) -> str:
    steps = _deserialize_plan(collected.get(PLAN_FIELD.key))
    resolved_steps: List[Dict[str, Any]] = []
    for step in steps:
        resolved_steps.extend(await _resolve_one_plan_step(step, ctx))

    lines = ["You are about to run this plan:", ""]
    for i, rs in enumerate(resolved_steps, start=1):
        r = rs["resolved"]
        if r is not None:
            names = ", ".join(r.talent_labels)
            if rs["intent_id"] == "casting.add":
                lines.append(f"{i}. Add {names} to {r.project_label} (Ask To Test)")
            else:
                lines.append(f"{i}. Move {names} to {nlu.stage_label(r.target_stage)} in {r.project_label}")
        else:
            lines.append(f"{i}. {rs['raw_text']} — {rs['error']}")
    lines.append("")
    lines.append("Reply:")
    lines.append("1 → Approve")
    lines.append("2 → Edit")
    lines.append("3 → Cancel")
    return "\n".join(lines)


async def _execute_plan(collected: dict, ctx: ExecContext) -> ExecResult:
    """Runs every step SEQUENTIALLY — each wrapped in its own try/except
    so one failing/ambiguous step never aborts the rest — and returns one
    combined summary in the exact ✓/✗ format specified."""
    steps = _deserialize_plan(collected.get(PLAN_FIELD.key))
    summary_lines = ["Completed", ""]
    any_success = False

    for step in steps:
        try:
            sub_steps = await _resolve_one_plan_step(step, ctx)
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
                        continue
                    write_result = await _timed_write(
                        bulk_move_by_talent_ids(r.project_id, split.actionable_ids, r.target_stage)
                    )
                    moved = write_result["moved"]
                    summary_lines += [
                        f"✓ {label}", "", f"{moved} talent{'' if moved == 1 else 's'} moved", "",
                    ]
                    any_success = True
            except Exception:
                logger.exception("plan step execution failed label=%r", label)
                summary_lines += [f"✗ {label}", "", "Something went wrong executing this step.", ""]

    return ExecResult(ok=any_success, message="\n".join(summary_lines).rstrip())


async def _move_try_auto_execute(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
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

    explicit = parse_edit_instructions(text, fields)
    if explicit:
        return explicit

    if pending and pending.get("field_key") and pending.get("kind") != "retry_global" and stripped:
        return {pending["field_key"]: stripped}

    return {}


MOVE_INTENT = IntentDefinition(
    intent_id="casting.move",
    triggers=nlu.MOVE_TRIGGERS,
    fields=[TALENT_SELECTOR_FIELD, TARGET_STAGE_FIELD, PROJECT_QUERY_FIELD, AUTO_CONFIRM_FIELD, PLAN_FIELD],
    executor=_move_executor,
    extract_fields=_extract_move_fields,
    build_confirmation=_build_move_confirmation,
    parse_edits_async=_move_parse_edits_async,
    try_auto_execute=_move_try_auto_execute,
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
    question="Which project should I add them to?",
    validate=_validate_project_query,
    aliases=["project", "for", "in", "to"],
    required=True,  # unlike MOVE's optional project_query — every Add spec
    # example names the project explicitly; a mistaken add is a real DB
    # write into someone's roster, so it's never defaulted from session
    # context, always asked if omitted.
)


def _extract_add_fields(text: str) -> Dict[str, str]:
    with request_scope.stage("nlu"):
        chunks, auto_confirm = nlu.preprocess_command(text)
        out: Dict[str, str] = {}

        if len(chunks) == 1:
            fields = nlu.extract_add_fields(chunks[0])
            project_names = nlu.split_multi_names(fields.get("project_query") or "") if fields.get("project_query") else []
            if len(project_names) <= 1:
                out.update(fields)
                if auto_confirm:
                    out[AUTO_CONFIRM_FIELD.key] = "1"
                return out
            chunks = [chunks[0]]

        steps = [{"intent_id": nlu.classify_chunk_intent(c) or "casting.add", "raw_text": c} for c in chunks]
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


async def _resolve_add_selection(
    collected: dict, session: Optional[dict]
) -> Tuple[Optional[ResolvedAdd], Optional[str], Optional[Dict[str, Any]]]:
    """Mirrors _resolve_move_selection's shape (project resolution incl.
    ambiguous/suggestion/free_text_retry payloads; talent resolution incl.
    ambiguous_candidates payload) but against the global talent candidate
    set instead of a project's pipeline rows, and with no stage/ordinal/
    number_map concept at all — Add never has a "displayed list" to index
    into."""
    selector_text = collected.get("talent_selector") or ""
    selector = nlu.parse_talent_selector(selector_text)
    if not selector.ok:
        return None, selector.error, None
    if selector.ordinals or selector.everyone:
        # No numbered listing exists for Add to index into — defensive,
        # not reachable via extract_add_fields's own grammar.
        return None, 'Please name who to add, e.g. "Add Prajal Tushir to Toyota Glanza".', None

    project_query = (collected.get("project_query") or "").strip()
    if not project_query:
        return None, 'Which project? e.g. "Add Prajal Tushir to Toyota Glanza".', None

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


ADD_INTENT = IntentDefinition(
    intent_id="casting.add",
    triggers=nlu.ADD_TRIGGERS,
    fields=[ADD_TALENT_SELECTOR_FIELD, ADD_PROJECT_QUERY_FIELD, AUTO_CONFIRM_FIELD, PLAN_FIELD],
    executor=_add_executor,
    extract_fields=_extract_add_fields,
    build_confirmation=_build_add_confirmation,
    try_auto_execute=_add_try_auto_execute,
    # Reused verbatim from MOVE — it only ever reads session's
    # pending_disambiguation (agent+phone scoped, not intent-specific) and
    # resolves a numbered/ordinal/label reply or a free-text retry against
    # whichever field_key is pending; Add's disambiguation shapes ("kind":
    # "project"/"talent"/"free_text_retry") are the exact same ones MOVE
    # produces, and Add never produces a "retry_global" kind, so that
    # branch simply never fires here.
    parse_edits_async=_move_parse_edits_async,
    summary_title="You are about to add:",
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
    selector grammar this isn't trying to replace)."""
    stripped = (text or "").strip()
    if not stripped.isdigit():
        return None
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    number_map = (session or {}).get("number_map") or {}
    if number_map.get("type") != "projects":
        return None
    return QUERY_INTENT, {"query_text": f"Project {stripped}"}


CASTING_AGENT = AgentDefinition(
    agent_id=AGENT_ID,
    name="Talentgram Casting Pipeline",
    module="casting_pipeline",
    intents=[QUERY_INTENT, MOVE_INTENT, ADD_INTENT, UNDO_INTENT],
    resolve_bare_reply=_resolve_bare_reply,
    # Concurrent Task Engine (2026-08-05) — casting-agent is the first (and
    # so far only) agent to opt into independently-addressable, concurrent
    # WhatsApp-reply-routable operations. CRM does not set this, so it is
    # completely unaffected.
    supports_concurrent_tasks=True,
)


def register() -> None:
    register_agent(CASTING_AGENT)
