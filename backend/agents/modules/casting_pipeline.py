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
from agents import session_context, undo_store
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


async def _fetch_project_candidates(project_id: str) -> List[nlu.Candidate]:
    """Every pipeline row for the project, any stage — used for name-based
    move resolution, which isn't scoped to whatever stage was last shown."""
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
        out.append(nlu.Candidate(id=tid, label=names.get(tid, "Unknown"), stage=stage))
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
    ordinal: Optional[int], session: Optional[dict]
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Resolve a "Project N" reference (or, if none was given in this
    message, fall back to the session's current project). Returns
    (project{id,label}, error)."""
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
    project, err = await _resolve_project_ref(classification.project_ordinal, session)
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

    project, err = await _resolve_project_ref(classification.project_ordinal, session)
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
        return ExecResult(ok=True, message=f"{nlu.stage_label(stage)}: {len(candidates)} talent(s).")

    items = [{"ordinal": i + 1, "id": c.id, "label": c.label} for i, c in enumerate(candidates)]
    await session_context.update_session(
        AGENT_ID, ctx.sender_phone,
        current_project_id=project["id"], current_project_label=project["label"],
        current_stage=stage,
        number_map={"type": "talents", "items": items},
    )
    if not candidates:
        return ExecResult(ok=True, message=f"{nlu.stage_label(stage)} — no talents.")
    lines = [f'{it["ordinal"]}. {it["label"]}' for it in items]
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
        numbered = "\n".join(f"{i}. {o}" for i, o in enumerate(options, start=1))
        return ValidationResult(ok=False, error=f"Which pipeline did you mean?\n{numbered}\n\nPlease send the correct one.")

    match = nlu.match_stage_phrase(raw, list(PIPELINE_STAGE_ORDER))
    if match.key:
        return ValidationResult(ok=True, value=match.key)
    if match.ambiguous:
        numbered = "\n".join(f"{i}. {o}" for i, o in enumerate(match.ambiguous, start=1))
        return ValidationResult(ok=False, error=f"Which pipeline did you mean?\n{numbered}\n\nPlease send the correct one.")
    supported = ", ".join(nlu.stage_label(s) for s in PIPELINE_STAGE_ORDER)
    return ValidationResult(ok=False, error=f'"{raw.strip()}" isn\'t a known pipeline.\nSupported pipelines: {supported}')


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
) -> Tuple[Optional[ResolvedMove], Optional[str]]:
    """Shared by build_confirmation (read-only, for display) and the
    executor (for the actual write).

    Ordinal/"everyone" selection resolves against the session's STORED
    number_map — the exact listing the user was shown ("these numbers
    exist only for the current conversation") — not a fresh live query:
    if a talent has since moved out of that stage (by this same move
    sequence, or by someone else via the web UI), the remaining talents
    must NOT silently renumber out from under an in-flight "2,5,9-20"
    selector. Name-based selection has no prior listing to snapshot, so it
    always searches the live pipeline. Either way, the actual WRITE below
    (via _split_by_current_stage in the executor) re-checks each talent's
    CURRENT stage fresh at write time — stale identity is never trusted,
    only stale "still needs moving" status is re-verified.
    """
    target_stage = collected.get("target_stage") or ""
    if target_stage not in PIPELINE_STAGES:
        return None, "Pipeline not found."

    selector_text = collected.get("talent_selector") or ""
    selector = nlu.parse_talent_selector(selector_text)
    if not selector.ok:
        return None, selector.error

    project_id = (session or {}).get("current_project_id")
    project_label = (session or {}).get("current_project_label") or ""

    if selector.ordinals or selector.everyone:
        current_stage = (session or {}).get("current_stage")
        number_map = (session or {}).get("number_map") or {}
        if not project_id or not current_stage or number_map.get("type") != "talents":
            return None, 'I don\'t have a pipeline open. Send "Project N" then "Show <Pipeline>" first.'
        candidates = [
            nlu.Candidate(id=it["id"], label=it["label"], stage=current_stage)
            for it in (number_map.get("items") or [])
        ]
    else:
        if not project_id:
            return None, 'I don\'t know which project. Send "Project N" first.'
        candidates = await _fetch_project_candidates(project_id)

    if not await _project_exists(project_id):
        return None, "Project doesn't exist."

    resolved = nlu.resolve_against_candidates(selector, candidates)
    if not resolved.ok:
        return None, resolved.error

    return ResolvedMove(
        project_id=project_id,
        project_label=project_label,
        target_stage=target_stage,
        talent_ids=resolved.talent_ids,
        talent_labels=resolved.talent_labels,
    ), None


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
    resolved, err = await _resolve_move_selection(collected, session)
    if err:
        # The generic engine has already committed to "confirming" for this
        # turn by the time this hook runs — there is nothing to confirm, so
        # the resolution error IS the reply (approving it just re-runs the
        # same resolution in the executor and surfaces the same message).
        return err

    split = await _split_by_current_stage(resolved)
    if not split.actionable_ids:
        if len(resolved.talent_ids) == 1:
            return f"{resolved.talent_labels[0]} is already in {nlu.stage_label(resolved.target_stage)}."
        return "Nothing to move."

    lines = ["You are about to move:", ""]
    lines.extend(f"- {name}" for name in split.actionable_labels)
    if split.already_labels:
        lines.append("")
        lines.append(f"(already in {nlu.stage_label(resolved.target_stage)}, skipped: {', '.join(split.already_labels)})")
    lines.append("")
    lines.append(f"To: {nlu.stage_label(resolved.target_stage)}")
    lines.append("")
    lines.append("Reply:")
    lines.append("1 → Approve")
    lines.append("2 → Edit")
    lines.append("3 → Cancel")
    return "\n".join(lines)


async def _move_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    resolved, err = await _resolve_move_selection(collected, session)
    if err:
        return ExecResult(ok=False, error="move_resolution_failed", message=err)

    # Re-checked fresh here (not trusting whatever _build_move_confirmation
    # saw) — the only thing that's allowed to have changed between confirm
    # and approve is each talent's current stage, and that's exactly what
    # this re-derives.
    split = await _split_by_current_stage(resolved)

    if not split.actionable_ids:
        if len(resolved.talent_ids) == 1:
            return ExecResult(
                ok=False, error="already_in_stage",
                message=f"{resolved.talent_labels[0]} is already in {nlu.stage_label(resolved.target_stage)}.",
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
    from_label = ", ".join(nlu.stage_label(s) for s in split.from_stages) or "—"
    lines = [
        "Done.",
        "",
        f"Project: {resolved.project_label}",
        f"From: {from_label}",
        f"To: {nlu.stage_label(resolved.target_stage)}",
        f"Moved {moved} talent{'' if moved == 1 else 's'}.",
    ]
    if split.already_labels:
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


MOVE_INTENT = IntentDefinition(
    intent_id="casting.move",
    triggers=nlu.MOVE_TRIGGERS,
    fields=[TALENT_SELECTOR_FIELD, TARGET_STAGE_FIELD],
    executor=_move_executor,
    extract_fields=_extract_move_fields,
    build_confirmation=_build_move_confirmation,
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
    for prev_stage, ids in restorable.items():
        res = await bulk_move_by_talent_ids(project_id, ids, prev_stage)
        total_restored += res["moved"]
    after_counts = await get_stage_counts(project_id)

    lines = ["Undo complete.", f"Restored {total_restored} talent{'' if total_restored == 1 else 's'}."]
    if skipped:
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
