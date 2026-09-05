"""Talentgram Fetcher — a NEW, separate WhatsApp agent, scoped to its OWN
WhatsApp group ("Talentgram Fetcher Agent"), completely independent of
Talentgram Scouting Agent (agents/modules/whatsapp_campaign_agent.py) and
Talentgram Casting Pipeline (agents/modules/casting_pipeline.py). Group
routing itself needs no new code here at all — it's entirely data-driven
via `whatsapp_agent_config` (see agents/registry.py's resolve_agent_for_group
and agents/__init__.py's seed_agent_config call for AGENT_ID below); the
WhatsApp worker discovers this new group automatically via the existing
generic /known-groups endpoint the moment that config doc exists.

Fetcher's job, for now, is exactly ONE command: SHOW ME — "fetch an
existing submission's form for a talent+project and return it in
WhatsApp", using the CANONICAL (talent_id, project_id) pair, never a name/
email/latest-submission guess (same safety bar SEND's own submission
lookup uses — see casting_pipeline.py's _resolve_send_target Step 6).

Reused, not duplicated:
  - Talent/project resolution: _fetch_all_talent_candidates,
    _resolve_talent_query_target_by_name, _fetch_ongoing_projects (all
    from casting_pipeline.py) + casting_pipeline_nlu.resolve_project_by_name
    — the EXACT same fuzzy/typo-tolerant resolvers ADD/MOVE/UPLOAD/SEND
    already use. Nothing here re-implements name matching.
  - Duplicate-talent-record tie-break:
    media_assignment.resolve_authoritative_talent_for_upload — the same
    submission-ownership tie-break SEND uses (Production fix, 2026-09-03)
    to resolve a split admin-added/talent-submitted duplicate record pair
    to whichever one actually has THIS project's submission, without ever
    guessing by name alone.
  - Numbered ambiguity resolution: the SAME session_context.
    pending_disambiguation + resume-marker mechanism casting.query/casting.
    move already use for talent/project ambiguity (see casting_pipeline.py's
    _ask_talent_clarification/_ask_project_clarification/_resume_pending_query
    for the established pattern this module's own
    _ask_show_me_talent_clarification/_ask_show_me_project_clarification/
    _resume_show_me mirror) — RESOLVED_TALENT_MARKER-encoded so a genuine
    same-name duplicate-record pick is never lost across the round trip
    the way a bare label pick would be.
  - Multi-item read tolerance: mirrors casting_pipeline.py's own
    _handle_talent_projects_multi precedent — a read has no wrong-record
    risk the way a write does, so one ambiguous/unresolved pair in a
    multi-talent or multi-project SHOW ME is reported inline for that pair
    only, never blocking the other valid pairs.

The one piece with NO existing backend twin is the Submission Review
"Copy Form" formatter itself (frontend/src/pages-components/
SubmissionReviewCenter.jsx's handleCopyForm) — audited before writing any
code here: it is pure frontend JS with no backend equivalent at all.
media_send.py's build_form_send_message LOOKS similar (same rough field
set) but is a DIFFERENT formatter for a DIFFERENT purpose (the outgoing
SEND-to-brand form) and is NOT byte-identical to Copy Form — it reads
`form_data` (not `original_form_data ?? form_data`), applies field-
visibility gating Copy Form deliberately ignores, and formats budget/
Instagram differently (parentheses vs em-dash; instagram.com vs
www.instagram.com with a trailing slash). Reusing it would silently
produce WRONG field values for any submission with admin overrides in
original_form_data, and would drop fields Copy Form always shows. Since a
Python function cannot literally call a frontend JS function,
build_copy_form_message below is a deliberate, single, line-by-line port
of handleCopyForm's exact logic (field selection, order, and string
format) — the closest thing to "reuse" actually possible across the
language boundary. If handleCopyForm's own formatting ever changes, this
port must be updated by hand to match; there is no way to make that
automatic across Python/JS.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from core import db
from agents.models import AgentDefinition, IntentDefinition, FieldSpec, ExecContext, ExecResult, ValidationResult
from agents.registry import register_agent
from agents import session_context
from agents.modules import casting_pipeline_nlu as nlu
from agents.modules import media_assignment
from agents.modules.casting_pipeline import (
    _fetch_ongoing_projects,
    _fetch_all_talent_candidates,
    _resolve_talent_query_target_by_name,
)

AGENT_ID = "talentgram-fetcher-agent"

# ---------------------------------------------------------------------------
# Copy Form port (see module docstring — no existing backend equivalent).
# ---------------------------------------------------------------------------

_IG_URL_PREFIX_RE = re.compile(r"^https?://(www\.)?instagram\.com/", re.IGNORECASE)
_IG_BARE_PREFIX_RE = re.compile(r"^(www\.)?instagram\.com/", re.IGNORECASE)


def _normalize_instagram_handle(raw: Any) -> str:
    """Ported from frontend/src/lib/mediaUtils.js's normalizeInstagramHandle
    — strips a full URL prefix (with/without www / protocol), a leading
    "@", and any trailing slash/query string."""
    if not raw or not isinstance(raw, str):
        return ""
    s = raw.strip()
    s = _IG_URL_PREFIX_RE.sub("", s)
    s = _IG_BARE_PREFIX_RE.sub("", s)
    if s.startswith("@"):
        s = s[1:]
    s = s.split("?")[0].split("/")[0].strip()
    return s


def _instagram_profile_url_copy_form(handle: Any) -> str:
    """Ported from frontend/src/lib/mediaUtils.js's instagramProfileUrl —
    NOTE this deliberately differs from media_send.py's own
    _format_instagram_link (no "www.", no trailing slash): that's a
    DIFFERENT formatter for SEND's outgoing form, not Copy Form."""
    if not handle or not isinstance(handle, str):
        return ""
    raw = _normalize_instagram_handle(handle)
    return f"https://www.instagram.com/{raw}/" if raw else ""


def _format_location_copy_form(location: Any) -> str:
    """Ported from frontend/src/lib/sanitize.js's formatTalentLocation —
    tolerates a bare string, a list of locations, or a single
    {"city","country"} dict; returns "" for anything empty/unrecognised."""
    if not location:
        return ""
    if isinstance(location, str):
        return location.strip()
    if isinstance(location, list):
        parts = [_format_location_copy_form(loc) for loc in location]
        return "; ".join(p for p in parts if p)
    if isinstance(location, dict):
        city = (location.get("city") or "").strip()
        country = (location.get("country") or "").strip()
        return ", ".join(p for p in (city, country) if p)
    return ""


def build_copy_form_message(sub: Dict[str, Any], project: Optional[Dict[str, Any]]) -> str:
    """Line-by-line port of SubmissionReviewCenter.jsx's handleCopyForm.
    Reads original_form_data, falling back to form_data ONLY when
    original_form_data is genuinely absent (None) — mirrors JS's `??`
    exactly, which is NOT the same as `or` (an explicit empty {} on
    original_form_data must NOT fall through to form_data, exactly as
    Copy Form itself never does). Deliberately ignores field_visibility —
    Copy Form always shows every field that has a value, unlike the
    client-facing shape SEND's own form uses."""
    od = sub.get("original_form_data")
    if od is None:
        od = sub.get("form_data")
    if od is None:
        od = {}

    lines: List[str] = []
    brand = (project or {}).get("brand_name") or ""
    lines.append(f"Talentgram x {brand} - Form")
    lines.append("")

    first_name = (od.get("first_name") or "").strip()
    last_name = (od.get("last_name") or "").strip()
    last_initial = last_name[0] if last_name else ""
    lines.append(f"{first_name} - {last_initial}" if last_initial else first_name)

    age = sub.get("effective_age")
    if age is None:
        age = od.get("age")
    if age is not None and str(age).strip() != "":
        lines.append(f"Age - {age}")

    height = od.get("height")
    if height:
        lines.append(f"Height - {height}")

    location_text = _format_location_copy_form(od.get("location"))
    if location_text:
        lines.append(f"Current Location - {location_text}")

    availability = od.get("availability")
    if not isinstance(availability, dict):
        availability = {"status": "", "note": availability or ""}
    if availability.get("status"):
        avail_text = "Available" if availability.get("status") == "yes" else "Unavailable"
        note = (availability.get("note") or "").strip() if isinstance(availability.get("note"), str) else availability.get("note")
        if note:
            avail_text = f"{avail_text} — {note}"
        lines.append(f"Availability - {avail_text}")

    competitive_brand = od.get("competitive_brand")
    if competitive_brand:
        lines.append(f"Competitive Brand - {competitive_brand}")

    for q in (project or {}).get("custom_questions") or []:
        qid = q.get("id")
        question = (q.get("question") or "").strip()
        if not question:
            continue
        answer = (od.get("custom_answers") or {}).get(qid)
        if answer is not None and str(answer).strip() != "":
            lines.append(f"{question} - {answer}")

    ig_url = _instagram_profile_url_copy_form(od.get("instagram_handle"))
    if ig_url:
        lines.append(f"Instagram link - {ig_url}")

    budget = od.get("budget")
    if not isinstance(budget, dict):
        budget = {"status": "", "value": budget or ""}
    if budget.get("status"):
        budget_text = "Accepts Day Rate" if budget.get("status") == "accept" else "Expected Day Rate"
        value = budget.get("value")
        if isinstance(value, str):
            value = value.strip()
        if value:
            budget_text = f"{budget_text} — {value}"
        lines.append(f"Budget - {budget_text}")

    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# SHOW ME — natural-language parsing (comma is the ONLY structural
# delimiter for talents/projects/commands, per spec — no "and" handling
# here, unlike disambiguation-reply parsing elsewhere on this platform).
# ---------------------------------------------------------------------------

_SHOW_ME_SPLIT_RE = re.compile(r"(?i)\bshow\s+me\b")
_FORM_FOR_RE = re.compile(r"(?i)\bforms?\s+for\b")
_TRAILING_POSSESSIVE_RE = re.compile(r"[’']s\s*$")


def _split_into_command_chunks(text: str) -> List[str]:
    """Splits "Show me A form for X, Show me B form for Y" into
    independent chunks, one per "show me" occurrence, each with the
    trigger phrase itself stripped. A single-command message (the common
    case) yields exactly one chunk."""
    matches = list(_SHOW_ME_SPLIT_RE.finditer(text))
    if not matches:
        stripped = text.strip()
        return [stripped] if stripped else []
    chunks: List[str] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end].strip(" ,\t\n")
        if chunk:
            chunks.append(chunk)
    return chunks


def _parse_chunk(chunk: str) -> Tuple[List[str], List[str], Optional[str]]:
    """One chunk -> (talent_queries, project_queries, error). Grammar:
    "<talent-list> ['s]? form[s] for <project-list>", talents/projects
    each comma-separated. Tolerant of case, extra spaces, and apostrophe
    variants (both ' and the fancy unicode ’)."""
    m = _FORM_FOR_RE.search(chunk)
    if not m:
        return [], [], "unrecognized"
    talent_part = chunk[: m.start()].strip()
    project_part = chunk[m.end():].strip()
    talent_part = _TRAILING_POSSESSIVE_RE.sub("", talent_part).strip()
    talent_part = talent_part.rstrip("’'").strip()
    talents = [t.strip() for t in talent_part.split(",") if t.strip()]
    projects = [p.strip() for p in project_part.split(",") if p.strip()]
    if not talents or not projects:
        return [], [], "incomplete"
    return talents, projects, None


_UNRECOGNIZED_MESSAGE = (
    'I didn\'t understand that.\nTry "Show me Angela\'s form for Hinge".'
)


def _project_not_found_message(project_q: str) -> str:
    return f'I couldn\'t find the project {project_q}.\n\nPlease check the project name and try again.'


def _talent_not_found_message(talent_q: str, project_q: str) -> str:
    return (
        f"I couldn't find {talent_q} in Talentgram.\n\n"
        f"Try:\nShow me {talent_q}'s form for {project_q}"
    )


def _no_submission_message(talent_label: str, project_label: str) -> str:
    return (
        f"No {project_label} submission was found for {talent_label}.\n\n"
        "The form can only be fetched after a submission exists."
    )


# ---------------------------------------------------------------------------
# Canonical submission lookup — SAME query shape as SEND's own Step 6
# (casting_pipeline.py's _resolve_send_target): talent_id + project_id
# ONLY, never name/email/latest-project. Sorted so a genuine multi-row
# edge case still resolves deterministically, never by chance.
# ---------------------------------------------------------------------------
async def _fetch_submission(talent_id: str, project_id: str) -> Optional[Dict[str, Any]]:
    return await db.submissions.find_one(
        {"talent_id": talent_id, "project_id": project_id},
        {"_id": 0}, sort=[("submitted_at", -1), ("created_at", -1)],
    )


async def _render_single_pair(talent_id: str, talent_label: str, project: Dict[str, str]) -> ExecResult:
    submission = await _fetch_submission(talent_id, project["id"])
    if not submission:
        return ExecResult(
            ok=True, error="no_submission",
            message=_no_submission_message(talent_label, project["label"]),
        )
    project_doc = await db.projects.find_one({"id": project["id"]}, {"_id": 0})
    form_text = build_copy_form_message(submission, project_doc)
    return ExecResult(ok=True, message=form_text)


async def _identifier_for_talent(talent_id: str) -> str:
    """A short, human-distinguishing detail for a numbered disambiguation
    line — same real-world need the master spec's own example shows
    ("Angela Sharma — [identifier]"). Prefers phone, then WhatsApp group,
    then email — whichever is actually on file; a talent record with none
    of these still gets a clearly-labelled placeholder rather than an
    empty dash."""
    doc = await db.talents.find_one(
        {"id": talent_id},
        {"_id": 0, "phone": 1, "whatsapp_group_name": 1, "email": 1, "normalized_email": 1},
    ) or {}
    for key in ("phone", "whatsapp_group_name", "email", "normalized_email"):
        val = (doc.get(key) or "").strip()
        if val:
            return val
    return "no contact on file"


# ---------------------------------------------------------------------------
# Numbered ambiguity resolution — session_context.pending_disambiguation +
# resume-marker mechanism, mirroring casting_pipeline.py's
# _ask_talent_clarification / _ask_project_clarification /
# _resume_pending_query exactly (see module docstring). A dedicated
# (not shared) implementation here so Fetcher never touches
# casting_pipeline.py's working, production-frozen code.
# ---------------------------------------------------------------------------

_SHOW_ME_RESUME_MARKER = "\x00SHOW_ME_RESUME\x00"
_CANCEL_VALUE = "__show_me_cancel__"
_KIND_TALENT = "show_me_talent"
_KIND_PROJECT = "show_me_project"


async def _ask_show_me_talent_clarification(
    ctx: ExecContext, name_query: str, ambiguous: List["nlu.Candidate"], project_q: str,
) -> ExecResult:
    options = []
    for c in ambiguous:
        identifier = await _identifier_for_talent(c.id)
        options.append({
            "label": f"{c.label} — {identifier}",
            "value": f"{nlu.RESOLVED_TALENT_MARKER}{c.id}|{c.label}",
        })
    options.append({"label": "Cancel", "value": _CANCEL_VALUE})
    await session_context.update_session(
        ctx.agent_id, ctx.sender_phone,
        pending_disambiguation={
            "kind": _KIND_TALENT, "field_key": "raw_text", "options": options,
            "resume": {"project_query": project_q},
        },
    )
    lines = [f"Which {name_query} do you mean?", ""]
    for i, o in enumerate(options):
        lines.append(f"{i + 1} → {o['label']}")
    return ExecResult(ok=False, error="ambiguous_talent", message="\n".join(lines), needs_clarification=True)


async def _ask_show_me_project_clarification(
    ctx: ExecContext, name_query: str, ambiguous: List[Dict[str, str]], talent_q: str,
) -> ExecResult:
    options = [{"label": o["label"], "value": o["label"]} for o in ambiguous]
    options.append({"label": "Cancel", "value": _CANCEL_VALUE})
    await session_context.update_session(
        ctx.agent_id, ctx.sender_phone,
        pending_disambiguation={
            "kind": _KIND_PROJECT, "field_key": "raw_text", "options": options,
            "resume": {"talent_query": talent_q},
        },
    )
    lines = [f'Which project matching "{name_query}" do you mean?', ""]
    for i, o in enumerate(options):
        lines.append(f"{i + 1} → {o['label']}")
    return ExecResult(ok=False, error="ambiguous_project", message="\n".join(lines), needs_clarification=True)


async def _resolve_single_pair(ctx: ExecContext, talent_q: str, project_q: str) -> ExecResult:
    """Full resolution for the single-(talent, project) case — the ONLY
    case that gets the interactive, resumable numbered clarification (see
    module docstring: a multi-pair request instead reports an ambiguous
    pair inline and keeps going, matching _handle_talent_projects_multi's
    existing precedent)."""
    projects = await _fetch_ongoing_projects()
    pmatch = nlu.resolve_project_by_name(project_q, projects)
    if pmatch.ambiguous:
        return await _ask_show_me_project_clarification(ctx, project_q, pmatch.ambiguous, talent_q)
    if not pmatch.project:
        return ExecResult(ok=False, error="project_not_found", message=_project_not_found_message(project_q))
    project = pmatch.project

    candidates = await _fetch_all_talent_candidates()
    talent_id, talent_label, err, ambiguous = await _resolve_talent_query_target_by_name(talent_q, candidates)
    if ambiguous:
        candidate_ids = [c.id for c in ambiguous]
        auth = await media_assignment.resolve_authoritative_talent_for_upload(project["id"], candidate_ids)
        if auth.ok:
            talent_id, talent_label = auth.talent_id, auth.talent_label
        else:
            return await _ask_show_me_talent_clarification(ctx, talent_q, ambiguous, project_q)
    if not talent_id:
        return ExecResult(ok=False, error="talent_not_found", message=_talent_not_found_message(talent_q, project_q))

    return await _render_single_pair(talent_id, talent_label, project)


async def _resume_show_me(session: Optional[dict], ctx: ExecContext) -> ExecResult:
    pending = (session or {}).get("pending_disambiguation") or {}
    resume = pending.get("resume") or {}
    resolved_value = pending.get("resolved_value")
    await session_context.update_session(ctx.agent_id, ctx.sender_phone, pending_disambiguation=None)

    if not pending:
        return ExecResult(ok=False, error="expired", message="That selection has expired — please send your command again.")
    if resolved_value == _CANCEL_VALUE:
        return ExecResult(ok=True, message="Okay, cancelled.")

    kind = pending.get("kind")
    if kind == _KIND_TALENT:
        project_q = resume.get("project_query") or ""
        selector = nlu.parse_talent_selector(resolved_value or "")
        talent_id, talent_label = selector.resolved_id, selector.resolved_label
        if not talent_id:
            return ExecResult(ok=False, error="expired", message="That selection has expired — please send your command again.")
        projects = await _fetch_ongoing_projects()
        pmatch = nlu.resolve_project_by_name(project_q, projects)
        if not pmatch.project:
            return ExecResult(ok=False, error="project_not_found", message=_project_not_found_message(project_q))
        return await _render_single_pair(talent_id, talent_label, pmatch.project)

    if kind == _KIND_PROJECT:
        talent_q = resume.get("talent_query") or ""
        project_label = resolved_value or ""
        projects = await _fetch_ongoing_projects()
        pmatch = nlu.resolve_project_by_name(project_label, projects)
        if not pmatch.project:
            return ExecResult(ok=False, error="project_not_found", message=_project_not_found_message(project_label))
        project = pmatch.project
        candidates = await _fetch_all_talent_candidates()
        talent_id, talent_label, err, ambiguous = await _resolve_talent_query_target_by_name(talent_q, candidates)
        if ambiguous:
            candidate_ids = [c.id for c in ambiguous]
            auth = await media_assignment.resolve_authoritative_talent_for_upload(project["id"], candidate_ids)
            if auth.ok:
                talent_id, talent_label = auth.talent_id, auth.talent_label
            else:
                return await _ask_show_me_talent_clarification(ctx, talent_q, ambiguous, project_label)
        if not talent_id:
            return ExecResult(ok=False, error="talent_not_found", message=_talent_not_found_message(talent_q, project_label))
        return await _render_single_pair(talent_id, talent_label, project)

    return ExecResult(ok=False, error="expired", message="That selection has expired — please send your command again.")


# ---------------------------------------------------------------------------
# Multi-pair path — mirrors _handle_talent_projects_multi's existing
# precedent (casting_pipeline.py): a read has no wrong-record risk, so one
# ambiguous/unresolved pair is reported inline for THAT pair only, never
# blocking the others.
# ---------------------------------------------------------------------------
async def _resolve_multi_pairs(pairs: List[Tuple[str, str]]) -> ExecResult:
    projects_cache = await _fetch_ongoing_projects()
    candidates_cache = await _fetch_all_talent_candidates()

    resolved_project_labels: set = set()
    # Each entry: (talent_label_or_query, project_label_or_query, body_text)
    results: List[Tuple[str, str, str]] = []

    for talent_q, project_q in pairs:
        pmatch = nlu.resolve_project_by_name(project_q, projects_cache)
        if pmatch.ambiguous:
            options = "; ".join(o["label"] for o in pmatch.ambiguous)
            results.append((
                talent_q, project_q,
                f'"{project_q}" — multiple matching projects found ({options}). '
                f"Ask about this one individually with the exact project name.",
            ))
            continue
        if not pmatch.project:
            results.append((talent_q, project_q, f'"{project_q}" — no matching project found.'))
            continue
        project = pmatch.project
        resolved_project_labels.add(project["label"])

        talent_id, talent_label, err, ambiguous = await _resolve_talent_query_target_by_name(talent_q, candidates_cache)
        if ambiguous:
            candidate_ids = [c.id for c in ambiguous]
            auth = await media_assignment.resolve_authoritative_talent_for_upload(project["id"], candidate_ids)
            if auth.ok:
                talent_id, talent_label = auth.talent_id, auth.talent_label
            else:
                results.append((
                    talent_q, project["label"],
                    f'"{talent_q}" — multiple matching talents found. Ask about them one at a time to pick.',
                ))
                continue
        if not talent_id:
            results.append((talent_q, project["label"], f'"{talent_q}" — no matching talent found.'))
            continue

        submission = await _fetch_submission(talent_id, project["id"])
        if not submission:
            results.append((talent_label, project["label"], _no_submission_message(talent_label, project["label"])))
            continue
        project_doc = await db.projects.find_one({"id": project["id"]}, {"_id": 0})
        form_text = build_copy_form_message(submission, project_doc)
        results.append((talent_label, project["label"], form_text))

    show_project_header = len(resolved_project_labels) > 1
    blocks: List[str] = []
    for talent_label, project_label, body in results:
        if show_project_header:
            header = f"{talent_label} — {project_label}"
        else:
            header = talent_label
        blocks.append(f"{header}\n{body}" if header else body)
    return ExecResult(ok=True, message="\n\n".join(blocks))


# ---------------------------------------------------------------------------
# Intent wiring
# ---------------------------------------------------------------------------
def _validate_show_me_text(raw: str) -> ValidationResult:
    text = (raw or "").strip()
    if not text:
        return ValidationResult(ok=False, error="I didn't catch that.")
    return ValidationResult(ok=True, value=text)


SHOW_ME_TEXT_FIELD = FieldSpec(
    key="raw_text",
    label="Command",
    question="What would you like me to show?",
    validate=_validate_show_me_text,
)


def _extract_show_me_fields(text: str) -> Dict[str, str]:
    # No sub-parsing here — the executor parses the full raw text itself
    # (talent/project lists, multiple commands), exactly like casting.query
    # already does for its own free-form query_text field.
    return {"raw_text": text}


async def _show_me_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    raw = collected.get("raw_text", "")
    session = await session_context.get_session(ctx.agent_id, ctx.sender_phone)

    if raw == _SHOW_ME_RESUME_MARKER:
        return await _resume_show_me(session, ctx)

    chunks = _split_into_command_chunks(raw)
    if not chunks:
        return ExecResult(ok=False, error="unrecognized", message=_UNRECOGNIZED_MESSAGE)

    pairs: List[Tuple[str, str]] = []
    for chunk in chunks:
        talents, projects, err = _parse_chunk(chunk)
        if err:
            return ExecResult(ok=False, error="unrecognized", message=_UNRECOGNIZED_MESSAGE)
        for t in talents:
            for p in projects:
                pairs.append((t, p))

    if not pairs:
        return ExecResult(ok=False, error="unrecognized", message=_UNRECOGNIZED_MESSAGE)

    if len(pairs) == 1:
        talent_q, project_q = pairs[0]
        return await _resolve_single_pair(ctx, talent_q, project_q)

    return await _resolve_multi_pairs(pairs)


async def _show_me_parse_edits_async(
    text: str, collected: Dict[str, str], fields: List[FieldSpec], ctx: ExecContext,
) -> Dict[str, str]:
    """Interprets an "editing"-step reply while a SHOW ME talent/project
    ambiguity is pending — mirrors casting_pipeline.py's
    _query_parse_edits_async exactly (same pending_disambiguation +
    resume-marker shape, just this agent's own state)."""
    session = await session_context.get_session(ctx.agent_id, ctx.sender_phone)
    pending = (session or {}).get("pending_disambiguation")
    if not pending or pending.get("field_key") != "raw_text":
        return {}
    options = pending.get("options") or []
    idx = nlu.resolve_option_reply((text or "").strip(), options)
    if idx is None:
        return {}
    resolved_value = options[idx - 1]["value"]
    await session_context.update_session(
        ctx.agent_id, ctx.sender_phone,
        pending_disambiguation={**pending, "resolved_value": resolved_value},
    )
    return {"raw_text": _SHOW_ME_RESUME_MARKER}


HELP_TEXT = (
    "TALENTGRAM FETCHER\n\n"
    "WHAT IT DOES\n\n"
    "Fetches information from Talentgram and sends it here.\n\n"
    "CURRENT COMMAND\n\n"
    "SHOW ME\n\n"
    "Examples:\n\n"
    "Show me Angela's form for Hinge\n\n"
    "Show me Angela, Priya forms for Hinge\n\n"
    "Show me Angela's form for Hinge, Dove\n\n"
    "Show me Angela, Priya forms for Hinge, Dove\n\n"
    "IMPORTANT\n\n"
    "- Commas separate multiple talents or projects.\n"
    "- Spelling and spacing can be approximate.\n"
    "- If more than one match is possible, the agent will ask you to choose.\n"
    "- Only existing submissions can be fetched."
)

SHOW_ME_INTENT = IntentDefinition(
    intent_id="fetcher.show_me",
    # "show" alone (not just "show me") is the actual trigger match
    # detect_trigger uses — it anchors on `first.startswith(trigger + " ")`,
    # which is agnostic to how much whitespace follows; a literal two-word
    # "show me" trigger would NOT match "Show   me   Angela..." (multiple
    # internal spaces), since agents/parser.py's shared trigger/voice-
    # transcript cleanup only strips line ends, never collapses internal
    # runs of spaces (true for every OTHER multi-word trigger on this
    # platform too — a pre-existing platform gap, not something safe to
    # fix globally here). This module's own _split_into_command_chunks
    # already tolerates arbitrary spacing around "show...me" via \s+, so
    # anchoring the trigger on "show" alone and letting the executor's own
    # parser do the rest is the smallest fix that doesn't touch shared
    # agents/parser.py code at all.
    triggers=["show me", "show"],
    fields=[SHOW_ME_TEXT_FIELD],
    executor=_show_me_executor,
    extract_fields=_extract_show_me_fields,
    auto_confirm=True,
    parse_edits_async=_show_me_parse_edits_async,
)

FETCHER_AGENT = AgentDefinition(
    agent_id=AGENT_ID,
    name="Talentgram Fetcher",
    module="talentgram_fetcher",
    intents=[SHOW_ME_INTENT],
    help_text=HELP_TEXT,
)


def register() -> None:
    register_agent(FETCHER_AGENT)
