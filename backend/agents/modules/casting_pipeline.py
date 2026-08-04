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
from agents import conversation, session_context, undo_store
from agents.parser import parse_confirmation_reply, parse_edit_instructions
from agents.modules import casting_pipeline_nlu as nlu

AGENT_ID = "casting-agent"
UNDO_WINDOW_MINUTES = 5


# ---------------------------------------------------------------------------
# DB helpers — the only place in this module that touches Mongo directly.
# ---------------------------------------------------------------------------
async def _fetch_ongoing_projects() -> List[Dict[str, str]]:
    cursor = db.projects.find(
        {"status": "ongoing"}, {"_id": 0, "id": 1, "brand_name": 1}
    ).sort("brand_name", 1)
    docs = await cursor.to_list(2000)
    return [{"id": d["id"], "label": d.get("brand_name") or "(untitled project)"} for d in docs]


async def _project_exists(project_id: str) -> bool:
    return bool(await db.projects.find_one({"id": project_id}, {"_id": 0, "id": 1}))


async def _fetch_last_updated(project_id: str) -> Optional[Any]:
    """Most recent `updated_at` across the project's pipeline rows — when
    the pipeline itself was last touched, not when the project doc was
    created. `casting_pipeline` rows store this via core._now(), which is
    an ISO string, not a native datetime — ISO-8601's fixed-width format
    still sorts lexicographically == chronologically, so the Mongo sort
    below is correct regardless; _format_last_updated is what handles the
    str-vs-datetime distinction on the way out."""
    rows = await db.casting_pipeline.find(
        {"project_id": project_id}, {"_id": 0, "updated_at": 1}
    ).sort("updated_at", -1).limit(1).to_list(1)
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
    cursor = db.talents.find({"id": {"$in": talent_ids}}, {"_id": 0, "id": 1, "name": 1})
    return {t["id"]: (t.get("name") or "Unknown") async for t in cursor}


async def _fetch_stage_candidates(project_id: str, stage: str) -> List[nlu.Candidate]:
    rows = await db.casting_pipeline.find(
        {"project_id": project_id, "stage": {"$in": _stage_query_values(stage)}},
        {"_id": 0, "talent_id": 1},
    ).sort("created_at", 1).to_list(5000)
    talent_ids = [r["talent_id"] for r in rows if r.get("talent_id")]
    names = await _hydrate_names(talent_ids)
    return [nlu.Candidate(id=tid, label=names.get(tid, "Unknown"), stage=stage) for tid in talent_ids]


async def _fetch_project_candidates(project_id: str, project_label: str = "") -> List[nlu.Candidate]:
    """Every pipeline row for the project, any stage — used for name-based
    move resolution, which isn't scoped to whatever stage was last shown.
    project_label is passed in (the caller already has it) purely for
    display in a disambiguation list — no extra DB round trip to fetch it
    again."""
    rows = await db.casting_pipeline.find(
        {"project_id": project_id}, {"_id": 0, "talent_id": 1, "stage": 1}
    ).sort("created_at", 1).to_list(5000)
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
    rows = await db.casting_pipeline.find(
        {"project_id": {"$in": project_ids}},
        {"_id": 0, "talent_id": 1, "stage": 1, "project_id": 1},
    ).sort("created_at", 1).to_list(20000)
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
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
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
    """
    if name_query:
        projects = await _fetch_ongoing_projects()
        match = nlu.resolve_project_by_name(name_query, projects)
        if match.project:
            return match.project, None
        if match.ambiguous:
            numbered = "\n".join(f"- {o}" for o in match.ambiguous)
            return None, f"Which project did you mean?\n{numbered}"
        return None, match.error or f'I couldn\'t find a project matching "{name_query}".'

    if ordinal is not None:
        number_map = (session or {}).get("number_map") or {}
        if number_map.get("type") != "projects":
            return None, 'I don\'t have a project list open. Send "Show ongoing projects" first.'
        items = number_map.get("items") or []
        match = next((it for it in items if it.get("ordinal") == ordinal), None)
        if not match:
            return None, "Project doesn't exist."
        return {"id": match["id"], "label": match["label"]}, None

    current_id = (session or {}).get("current_project_id")
    current_label = (session or {}).get("current_project_label")
    if not current_id:
        return None, 'I don\'t know which project. Send "Project N" or "Show ongoing projects" first.'
    return {"id": current_id, "label": current_label or ""}, None


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
    lines = [f'{it["ordinal"]}. {it["label"]}' for it in items]
    return ExecResult(ok=True, message="\n".join(lines))


async def _handle_project_detail(
    ctx: ExecContext, session: Optional[dict], classification: nlu.QueryIntent
) -> ExecResult:
    project, err = await _resolve_project_ref(
        classification.project_ordinal, classification.project_name_query, session
    )
    if err:
        return ExecResult(ok=False, error="project_not_found", message=err)
    if not await _project_exists(project["id"]):
        return ExecResult(ok=False, error="project_not_found", message="Project doesn't exist.")

    counts = await get_stage_counts(project["id"])
    total_talents = sum(counts.values())
    last_updated = await _fetch_last_updated(project["id"])

    lines = ["Project", "", project["label"], "", f"Total Talents: {total_talents}", "", "Pipelines", ""]
    for i, stage in enumerate(PIPELINE_STAGE_ORDER, start=1):
        lines.append(f"{i}. {nlu.stage_label(stage)} ({counts.get(stage, 0)})")
    lines.append("")
    lines.append("Last Updated:")
    lines.append(_format_last_updated(last_updated))

    await session_context.update_session(
        AGENT_ID, ctx.sender_phone,
        current_project_id=project["id"], current_project_label=project["label"],
        current_stage=None,
        number_map={"type": None, "items": []},
    )
    return ExecResult(ok=True, message="\n".join(lines))


async def _handle_pipeline_query(
    ctx: ExecContext, session: Optional[dict], classification: nlu.QueryIntent
) -> ExecResult:
    if classification.stage_ambiguous:
        options = "\n".join(f"- {o}" for o in classification.stage_ambiguous)
        return ExecResult(ok=False, error="ambiguous_stage", message=f"Which pipeline did you mean?\n{options}")

    project, err = await _resolve_project_ref(
        classification.project_ordinal, classification.project_name_query, session
    )
    if err:
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
    sorted_candidates = sorted(candidates, key=lambda c: (c.label or "").strip().lower())
    items = [{"ordinal": i + 1, "id": c.id, "label": c.label} for i, c in enumerate(sorted_candidates)]
    await session_context.update_session(
        AGENT_ID, ctx.sender_phone,
        current_project_id=project["id"], current_project_label=project["label"],
        current_stage=stage,
        number_map={"type": "talents", "items": items},
    )

    header = [
        "Project",
        project["label"],
        "",
        "Pipeline",
        nlu.stage_label(stage),
        "",
        f"Total Talents: {len(items)}",
        "",
        "━━━━━━━━━━━━━━",
        "",
    ]
    if not items:
        return ExecResult(ok=True, message="\n".join(header) + "No talents in this pipeline.")
    lines = header + [f'{it["ordinal"]}. {it["label"]}' for it in items]
    return ExecResult(ok=True, message="\n".join(lines))


_IMPLICIT_COUNT_RE = re.compile(r"\b(how many|left|remaining)\b", re.IGNORECASE)


async def _query_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    raw_text = collected.get("query_text", "")
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
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
    return ExecResult(
        ok=False,
        error="unrecognized_query",
        message='I didn\'t understand that.\nTry "Show ongoing projects", "Project 3", or "Show Approved".',
    )


QUERY_INTENT = IntentDefinition(
    intent_id="casting.query",
    triggers=nlu.QUERY_TRIGGERS,
    fields=[QUERY_TEXT_FIELD],
    executor=_query_executor,
    extract_fields=_extract_query_fields,
    auto_confirm=True,
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
    return nlu.extract_move_fields(text, list(PIPELINE_STAGE_ORDER))


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

    project_query = (collected.get("project_query") or "").strip()
    force_global = project_query == nlu.FORCE_GLOBAL_MARKER
    if force_global:
        project_query = ""

    if force_global:
        project_id, project_label = None, ""
    elif project_query:
        projects = await _fetch_ongoing_projects()
        match = nlu.resolve_project_by_name(project_query, projects)
        if match.project:
            project_id = match.project["id"]
            project_label = match.project["label"]
        elif match.ambiguous:
            options = [{"label": o, "value": o} for o in match.ambiguous]
            msg = nlu.format_numbered_options("I found multiple projects.", [[o] for o in match.ambiguous])
            return None, msg, {"kind": "project", "field_key": "project_query", "options": options}
        elif match.suggestions:
            bullets = "\n".join(f"• {s}" for s in match.suggestions)
            return None, f"I couldn't find a project matching:\n\n{project_query}\n\nDid you mean:\n\n{bullets}", None
        else:
            return None, match.error or f'I couldn\'t find a project matching "{project_query}".', None
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
        candidates = await _fetch_project_candidates(project_id, project_label)
        if not await _project_exists(project_id):
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
            ), None
        if not selector.name_query and not selector.resolved_id:
            return None, 'I don\'t know which project. Send "Project N" first, or name the project.', None
        candidates = await _fetch_global_candidates()
        if not candidates:
            return None, "No active projects have any talents in their pipeline yet.", None

    resolved = nlu.resolve_against_candidates(selector, candidates)

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
            global_hit = nlu.resolve_against_candidates(
                nlu.SelectorResult(ok=True, name_query=selector.name_query), global_candidates
            )
            if global_hit.ok:
                found_name = global_hit.talent_labels[0]
                return None, (
                    f"I found {found_name}, but they aren't part of {project_label}.\n\n"
                    f"Would you like me to search all active projects instead?"
                ), {"kind": "retry_global", "field_key": "project_query", "options": []}

        if not project_id and selector.name_query and resolved.error == "No matching talent.":
            return None, f'"{selector.name_query}" wasn\'t found in any active project.', None

        return None, resolved.error, None

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
    rows = await db.casting_pipeline.find(
        {"project_id": resolved.project_id, "talent_id": {"$in": resolved.talent_ids}},
        {"_id": 0, "talent_id": 1, "stage": 1},
    ).to_list(len(resolved.talent_ids))
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


async def _build_move_confirmation(collected: dict, ctx: ExecContext) -> str:
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

    split = await _split_by_current_stage(resolved)
    if not split.actionable_ids:
        if len(resolved.talent_ids) == 1:
            return f"{resolved.talent_labels[0]} is already in {nlu.stage_label(resolved.target_stage)}.\n\nNo changes were made."
        return "Nothing to move."

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
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    resolved, err, _disambiguation = await _resolve_move_selection(collected, session)
    if err:
        # By the time a real confirmation was shown and approved, any
        # ambiguity should already have been resolved — if resolution
        # somehow still fails here (e.g. underlying data changed in the
        # last few seconds), fail cleanly rather than trying to restart a
        # clarification sub-flow mid-approval.
        return ExecResult(ok=False, error="move_resolution_failed", message=err)

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

    before_counts = await get_stage_counts(resolved.project_id)
    write_result = await bulk_move_by_talent_ids(resolved.project_id, split.actionable_ids, resolved.target_stage)
    after_counts = await get_stage_counts(resolved.project_id)

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
        elif stripped.isdigit():
            n = int(stripped)
            if 1 <= n <= len(options) and field_key:
                await session_context.update_session(AGENT_ID, ctx.sender_phone, pending_disambiguation=None)
                return {field_key: options[n - 1]["value"]}

    explicit = parse_edit_instructions(text, fields)
    if explicit:
        return explicit

    if pending and pending.get("field_key") and pending.get("kind") != "retry_global" and stripped:
        return {pending["field_key"]: stripped}

    return {}


MOVE_INTENT = IntentDefinition(
    intent_id="casting.move",
    triggers=nlu.MOVE_TRIGGERS,
    fields=[TALENT_SELECTOR_FIELD, TARGET_STAGE_FIELD, PROJECT_QUERY_FIELD],
    executor=_move_executor,
    extract_fields=_extract_move_fields,
    build_confirmation=_build_move_confirmation,
    parse_edits_async=_move_parse_edits_async,
    summary_title="You are about to move:",
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
    live_rows = await db.casting_pipeline.find(
        {"project_id": project_id, "talent_id": {"$in": list(previous_stage_by_id.keys())}},
        {"_id": 0, "talent_id": 1, "stage": 1},
    ).to_list(len(previous_stage_by_id) or 1)
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

    before_counts = await get_stage_counts(project_id)
    total_restored = 0
    restored_ids: List[str] = []
    for prev_stage, ids in restorable.items():
        res = await bulk_move_by_talent_ids(project_id, ids, prev_stage)
        total_restored += res["moved"]
        restored_ids.extend(ids)
    after_counts = await get_stage_counts(project_id)

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
    triggers=["undo"],
    fields=[],
    executor=_undo_executor,
    auto_confirm=True,
)


CASTING_AGENT = AgentDefinition(
    agent_id=AGENT_ID,
    name="Talentgram Casting Pipeline",
    module="casting_pipeline",
    intents=[QUERY_INTENT, MOVE_INTENT, UNDO_INTENT],
)


def register() -> None:
    register_agent(CASTING_AGENT)
