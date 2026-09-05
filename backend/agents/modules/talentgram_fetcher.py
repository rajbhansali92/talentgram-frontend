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

from core import db, _filter_talent_for_client, DEFAULT_VISIBILITY
from agents.models import AgentDefinition, IntentDefinition, FieldSpec, ExecContext, ExecResult, ValidationResult
from agents.registry import register_agent
from agents import session_context, name_match
from agents.modules import casting_pipeline_nlu as nlu
from agents.modules import media_assignment
from agents.modules.casting_pipeline import (
    _fetch_ongoing_projects,
    _fetch_all_talent_candidates,
    _resolve_talent_query_target_by_name,
)
# Existing DB-side talent filter/query builder (gender/age/height/location),
# already reused cross-module by casting_pipeline.py's own talent_search
# feature — the SAME reuse pattern, not a new import direction.
from routers.talents import _build_talent_query

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

    # Production fix — Competitive Brand and admin-defined project
    # questions must NEVER be silently dropped just because the answer is
    # blank (real bug: Ameya Saawant / Mivi Phones has field_visibility.
    # competitive_brand=True and submission_requirements.fields.
    # competitive_brand="required", but her actual value is "" — Copy
    # Form's own `if (od.competitive_brand)` check, which this used to
    # port verbatim, would omit the line entirely, hiding a required-but-
    # unanswered field from whoever reads the form). The canonical
    # "is this even part of THIS project's form" signal is
    # project.submission_requirements.fields.competitive_brand (audited
    # via a real submission) — present at all means the project's form
    # asks for it; absent means this project never did, so the line is
    # correctly never invented for it.
    requirements_fields = ((project or {}).get("submission_requirements") or {}).get("fields") or {}
    if "competitive_brand" in requirements_fields:
        competitive_brand = (od.get("competitive_brand") or "").strip() if isinstance(od.get("competitive_brand"), str) else od.get("competitive_brand")
        lines.append(f"Competitive Brand - {competitive_brand}" if competitive_brand else "Competitive Brand - [blank]")

    # Every admin-defined project question ALWAYS appears — an unanswered
    # question is real, useful information (the client/reviewer needs to
    # know it was asked and not yet answered), never silently dropped the
    # way Copy Form's own blank-answer check currently does.
    for q in (project or {}).get("custom_questions") or []:
        qid = q.get("id")
        question = (q.get("question") or "").strip()
        if not question:
            continue
        answer = (od.get("custom_answers") or {}).get(qid)
        answer_text = str(answer).strip() if answer is not None else ""
        lines.append(f"{question} - {answer_text}" if answer_text else f"{question} - [blank]")

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
# SHOW ME PROFILE — reuses the EXISTING generated-link system (audited
# first, before writing any of this): a talent's "portfolio link" is a
# `db.links` doc created by an admin via ForwardToLinkModal.jsx
# ("client-ready portfolio link from APPROVED submissions") — there is no
# separate per-talent auto-generated link anywhere in this codebase, so a
# single-talent link (talent_ids == [this one talent], is_public=True) IS
# the closest existing concept of "this talent's portfolio link". Fetcher
# only ever READS this collection — it never creates a link, matching the
# explicit "do not generate a new link" requirement. The public URL format
# (https://links.talentgramagency.com/l/{slug}) is exactly what
# frontend/src/middleware.ts rewrites for the `links` subdomain.
#
# The no-link fallback reuses core._filter_talent_for_client — the SAME
# strict client-facing whitelist every OTHER public surface (Client Link,
# slideshow, download bundle, PDF) already renders through — with
# DEFAULT_VISIBILITY (core.py) as the visibility map, since a bare talent
# record has no link-level visibility overrides to consult. This is a
# read-only reuse, not a new admin/private-field exposure path.
# ---------------------------------------------------------------------------

_LINKS_BASE_URL = "https://links.talentgramagency.com"

_LINK_TITLE_PREFIX_RE = re.compile(r"(?i)^\s*talentgram\s*[x×]\s*")


def _title_matches_talent_name(title: Optional[str], talent_label: str) -> bool:
    """A real-data audit (Angela Kumar) found she has TWO links that both
    pass the exact talent_ids==[id] match — "Talentgram x Angela" and
    "Talentgram x Kay Beauty" — structurally IDENTICAL in every other
    field (both manual "Individual Talent Share" links per LinkGenerator.
    jsx's own inferMode: submission_ids=[], auto_pull=False). Nothing in
    the schema marks one as "the general profile" vs "a one-off brand
    pitch" — the admin-typed title is the only signal, and a link titled
    after the talent's OWN name (not a brand) is what a human would
    recognize as her actual profile link. "Most recent" alone (the prior
    behaviour) picked "Kay Beauty" here purely because it happened to be
    created a day later — the real bug this fixes.

    Token-subset match, not exact equality — a real-data check against
    Angela Kumar showed her own name-titled link is literally "Talentgram
    x Angela" (first name only, not "Angela Kumar"), so exact equality
    against the full talent_label missed it entirely. Every word in the
    (prefix-stripped) title must appear among the talent's own name
    words — "angela" ⊆ {"angela","kumar"} matches; "kay beauty" ⊆
    {"angela","kumar"} does not."""
    t = _LINK_TITLE_PREFIX_RE.sub("", title or "").strip().lower()
    n = (talent_label or "").strip().lower()
    if not t or not n:
        return False
    t_tokens = set(t.split())
    n_tokens = set(n.split())
    return bool(t_tokens) and t_tokens.issubset(n_tokens)


async def _find_talent_portfolio_link(talent_id: str, talent_label: str) -> Optional[str]:
    candidates = await db.links.find(
        {"talent_ids": [talent_id], "is_public": True},
        {"_id": 0, "slug": 1, "title": 1},
    ).sort([("created_at", -1)]).to_list(50)
    if not candidates:
        return None
    name_matches = [c for c in candidates if _title_matches_talent_name(c.get("title"), talent_label)]
    chosen = name_matches[0] if name_matches else candidates[0]
    if not chosen.get("slug"):
        return None
    return f"{_LINKS_BASE_URL}/l/{chosen['slug']}"


def _render_work_link_line(stored: str) -> str:
    """Ported from WorkLinksDisplay.jsx's parseStoredWorkLink — same
    "Label || https://..." / bare-URL stored shape, rendered as a single
    plain text line (no icon/domain subtitle, which is presentation-only
    for the web UI, not needed for a WhatsApp text reply)."""
    s = (stored or "").strip()
    if not s:
        return ""
    if " || " in s:
        idx = s.index(" || ")
        label, url = s[:idx].strip(), s[idx + 4:].strip()
        return f"{label}: {url}" if label else url
    return s


def _render_talent_profile_fallback(talent_doc: Dict[str, Any]) -> str:
    shaped = _filter_talent_for_client(talent_doc, DEFAULT_VISIBILITY)
    name = shaped.get("name") or "Unnamed"
    lines = [f"Talentgram X {name}", ""]

    if shaped.get("age") is not None:
        lines.append(f"Age: {shaped['age']}")
    if shaped.get("height"):
        lines.append(f"Height: {shaped['height']}")
    location_text = _format_location_copy_form(shaped.get("location"))
    if location_text:
        lines.append(f"Location: {location_text}")
    ig_url = _instagram_profile_url_copy_form(shaped.get("instagram_handle"))
    if ig_url:
        lines.append(f"Instagram: {ig_url}")
    skills = shaped.get("skills") or []
    if skills:
        lines.append(f"Skills: {', '.join(skills)}")

    work_links = [
        _render_work_link_line(wl) for wl in (shaped.get("work_links") or [])
    ]
    work_links = [wl for wl in work_links if wl]
    if work_links:
        lines.append("")
        lines.append("Work Links:")
        lines.extend(work_links)

    return "\n".join(lines)


async def _render_talent_profile(
    talent_id: str, talent_label: str, talent_doc: Optional[Dict[str, Any]] = None,
) -> str:
    link_url = await _find_talent_portfolio_link(talent_id, talent_label)
    if link_url:
        return f"Talentgram X {talent_label}\n\nClick to view the portfolio:\n\n{link_url}"
    doc = talent_doc if talent_doc is not None else await db.talents.find_one({"id": talent_id}, {"_id": 0})
    if not doc:
        return f"Talentgram X {talent_label}\n\nNo profile information available."
    return _render_talent_profile_fallback(doc)


def _talent_not_found_profile_message(name_q: str) -> str:
    return f"I couldn't find {name_q} in Talentgram."


_PROFILE_WORD_RE = re.compile(r"(?i)\bprofiles?\b")
_LEADING_OF_FOR_RE = re.compile(r"(?i)^\s*(?:of|for)\s+")
_TRAILING_OF_FOR_RE = re.compile(r"(?i)\s+(?:of|for)\s*$")


async def _handle_profile_names(ctx: ExecContext, names: List[str]) -> ExecResult:
    if len(names) == 1:
        return await _resolve_single_profile(ctx, names[0])

    # Multiple talents -> mirrors casting_pipeline.py's own
    # _handle_talent_projects_multi precedent: a read has no wrong-record
    # risk, so one ambiguous/unresolved name is reported inline for THAT
    # name only, never blocking the others (no interactive resume here —
    # there is no project to tie-break an ambiguous name against anyway,
    # unlike FORM).
    candidates = await _fetch_all_talent_candidates()
    blocks: List[str] = []
    for name_q in names:
        talent_id, talent_label, err, ambiguous = await _resolve_talent_query_target_by_name(name_q, candidates)
        if ambiguous:
            blocks.append(f'"{name_q}" — multiple matching talents found. Ask about them one at a time to pick.')
            continue
        if not talent_id:
            blocks.append(f'"{name_q}" — {_talent_not_found_profile_message(name_q)}')
            continue
        blocks.append(await _render_talent_profile(talent_id, talent_label))
    return ExecResult(ok=True, message="\n\n---\n\n".join(blocks))


async def _resolve_single_profile(ctx: ExecContext, name_q: str) -> ExecResult:
    candidates = await _fetch_all_talent_candidates()
    talent_id, talent_label, err, ambiguous = await _resolve_talent_query_target_by_name(name_q, candidates)
    if ambiguous:
        # No project context exists for a profile request, so (unlike
        # FORM) there is no authoritative-submission tie-break available —
        # a genuine same-name duplicate always asks.
        return await _ask_show_me_talent_clarification(ctx, name_q, ambiguous, {"request_type": "profile"})
    if not talent_id:
        return ExecResult(ok=False, error="talent_not_found", message=_talent_not_found_profile_message(name_q))
    return ExecResult(ok=True, message=await _render_talent_profile(talent_id, talent_label))


async def _try_handle_profile_request(ctx: ExecContext, chunk: str) -> Optional[ExecResult]:
    """Recognizes "profile of X"/"profiles of X, Y"/"X, Y profiles" —
    tolerant of the anchor word appearing before OR after the talent list
    (both are shown as valid phrasing in the spec). Returns None (never a
    real error) when the chunk isn't a profile request at all, so the
    caller falls through to trying FILTERED_TALENTS, then FORM."""
    if not _PROFILE_WORD_RE.search(chunk):
        return None
    # Strip a leading "the" BEFORE removing the profile word itself —
    # "the profile of X" would otherwise leave a stray "the  of X" that
    # _LEADING_OF_FOR_RE (anchored on "of"/"for" only) can't clean up.
    talent_part = re.sub(r"(?i)^\s*the\s+", "", chunk)
    talent_part = _PROFILE_WORD_RE.sub("", talent_part)
    talent_part = _LEADING_OF_FOR_RE.sub("", talent_part)
    talent_part = _TRAILING_OF_FOR_RE.sub("", talent_part)
    talent_part = talent_part.strip(" ,")
    names = [t.strip() for t in talent_part.split(",") if t.strip()]
    if not names:
        return None
    return await _handle_profile_names(ctx, names)


# ---------------------------------------------------------------------------
# SHOW ME FILTERED TALENTS — reuses routers/talents.py's own
# _build_talent_query (the SAME DB-side gender/age/height/location query
# builder casting_pipeline.py's own talent_search feature already calls)
# and core.parse_height_to_inches indirectly via the SAME height-range
# normalization rules that write path already relies on for height_inches.
# No new search engine, no AI, no application-side filtering of the full
# talent collection — every filter becomes part of the Mongo query itself.
# ---------------------------------------------------------------------------

_TALENTS_WORD_RE = re.compile(r"(?i)\btalents?\b")
_GENDER_RE = re.compile(r"(?i)\b(female|males?|non[\s-]?binary)\b")
_GENDER_MAP = {
    "female": "female", "male": "male", "males": "male",
    "non binary": "non_binary", "non-binary": "non_binary", "nonbinary": "non_binary",
}

# Apostrophe/quote height range ("5'4 to 5'8", "5'4\"-5'8\"", curly quotes
# tolerated) — unambiguous on its own, no "height" keyword required.
_HEIGHT_APOSTROPHE_RANGE_RE = re.compile(
    r"(\d)\s*[’'’]\s*(\d{1,2})\s*[\"”]?\s*(?:-|to|through|and)\s*"
    r"(\d)\s*[’'’]\s*(\d{1,2})\s*[\"”]?",
    re.IGNORECASE,
)
# Dotted-decimal shorthand ("height 5.4 to 5.8") — the "height" keyword is
# REQUIRED here (unlike the apostrophe form) since a bare number range on
# its own would otherwise be indistinguishable from an age range.
_HEIGHT_DECIMAL_RANGE_RE = re.compile(
    r"height\s+(\d)(?:\.(\d{1,2}))?\s*(?:-|to|through|and)\s*(\d)(?:\.(\d{1,2}))?",
    re.IGNORECASE,
)
_AGE_RANGE_RE = re.compile(
    r"\b(?:age[s]?\s+)?(?:between\s+)?(\d{1,3})\s*(?:-|to|through|and)\s*(\d{1,3})\b"
    r"(?:\s*years?(?:\s*old)?)?",
    re.IGNORECASE,
)
# Whatever's left after stripping every recognized filter token/filler word
# is treated as the location term — a simple, robust "subtract the known
# vocabulary" heuristic rather than a location-specific grammar.
_FILTER_FILLER_RE = re.compile(
    r"(?i)\b(show|me|all|talents?|between|and|through|to|in|years?|old|"
    r"age|ages|height|female|males?|non[\s-]?binary|nonbinary)\b"
)


def _normalize_gender_word(raw: str) -> str:
    w = re.sub(r"[\s-]+", " ", raw.lower().strip())
    return _GENDER_MAP.get(w, w)


def _parse_filter_criteria(text: str) -> Dict[str, Any]:
    remaining = text

    gender = None
    m = _GENDER_RE.search(remaining)
    if m:
        gender = _normalize_gender_word(m.group(1))
        remaining = remaining[: m.start()] + " " + remaining[m.end():]

    height_min = height_max = None
    m = _HEIGHT_APOSTROPHE_RANGE_RE.search(remaining)
    if m:
        f1, i1, f2, i2 = m.groups()
        height_min = float(int(f1) * 12 + int(i1))
        height_max = float(int(f2) * 12 + int(i2))
        remaining = remaining[: m.start()] + " " + remaining[m.end():]
    else:
        m = _HEIGHT_DECIMAL_RANGE_RE.search(remaining)
        if m:
            f1, i1, f2, i2 = m.groups()
            height_min = float(int(f1) * 12 + int(i1 or 0))
            height_max = float(int(f2) * 12 + int(i2 or 0))
            remaining = remaining[: m.start()] + " " + remaining[m.end():]

    if height_min is not None and height_max is not None and height_min > height_max:
        height_min, height_max = height_max, height_min

    age_min = age_max = None
    m = _AGE_RANGE_RE.search(remaining)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        age_min, age_max = min(a, b), max(a, b)
        remaining = remaining[: m.start()] + " " + remaining[m.end():]

    location_text = _FILTER_FILLER_RE.sub(" ", remaining)
    location_text = re.sub(r"[,\s]+", " ", location_text).strip()

    return {
        "gender": gender, "age_min": age_min, "age_max": age_max,
        "height_min": height_min, "height_max": height_max,
        "location": location_text or None,
    }


async def _resolve_location_terms(raw_location: Optional[str]) -> List[str]:
    """Fuzzy-corrects a typed location term against the DISTINCT city/
    country values actually present in the talents collection (the same
    values routers/talents.py's own /talents/filter-options endpoint
    surfaces) — reuses the SAME shared tiered matcher every other name
    lookup on this platform uses, never a separate fuzzy algorithm. Falls
    back to the raw term when nothing confidently matches (an honest zero-
    result search, never a silently-wrong substitution)."""
    if not raw_location:
        return []
    cities = await db.talents.distinct("location.city", {"location.city": {"$nin": [None, ""]}})
    countries = await db.talents.distinct("location.country", {"location.country": {"$nin": [None, ""]}})
    candidates = sorted({c for c in cities if c} | {c for c in countries if c})
    if not candidates:
        return [raw_location]
    match = name_match.tiered_name_match(raw_location, candidates, lambda s: s)
    if match.item is not None:
        return [match.item]
    return [raw_location]


def _render_talent_filter_card(talent_doc: Dict[str, Any]) -> str:
    """Filtered-search result card — deliberately narrower than
    _render_talent_profile_fallback: Name/Age/Height/Location/Instagram
    ONLY (no Work Links, no Skills, no portfolio link). This is a
    talent-OPTIONS list for a client to browse, not an individual profile
    request — Production fix: the previous version reused
    _render_talent_profile (PROFILE's own richer format) for every filter
    result, which is why Work Links were leaking into filter output."""
    shaped = _filter_talent_for_client(talent_doc, DEFAULT_VISIBILITY)
    name = shaped.get("name") or "Unnamed"
    lines = [f"Talentgram X {name}", ""]
    if shaped.get("age") is not None:
        lines.append(f"Age: {shaped['age']}")
    if shaped.get("height"):
        lines.append(f"Height: {shaped['height']}")
    location_text = _format_location_copy_form(shaped.get("location"))
    if location_text:
        lines.append(f"Location: {location_text}")
    ig_url = _instagram_profile_url_copy_form(shaped.get("instagram_handle"))
    if ig_url:
        lines.append(f"Instagram: {ig_url}")
    return "\n".join(lines)


# WhatsApp's own actual documented text-message limit is 65,536 characters
# (audited from the transport side: whatsapp-worker/sender.py's text-send
# path just types the string via the keyboard with no length check or
# chunking of its own — there is no SMALLER limit anywhere else in this
# pipeline). This is that real constraint, not an invented page size —
# left with headroom for the header/footer text around it. Production fix:
# the previous version capped at an arbitrary 20 results / 6000 chars and
# silently dropped the remainder into a "showing N of M" footer — this
# reflects the platform's own established "no truncation, no paging,
# regardless of size" convention (casting_pipeline.py's _render_talent_list)
# instead, up to WhatsApp's real limit.
_WHATSAPP_SAFE_MESSAGE_CHAR_LIMIT = 60000


async def _run_filtered_talent_search(filters: Dict[str, Any]) -> ExecResult:
    location_list = await _resolve_location_terms(filters.get("location"))
    query = _build_talent_query(
        q=None, status=None,
        gender=filters.get("gender"), ethnicity=None,
        location=location_list,
        age_min=filters.get("age_min"), age_max=filters.get("age_max"),
        height_min=filters.get("height_min"), height_max=filters.get("height_max"),
        followers_min=None,
        interested_in=[], interested_in_mode="any",
        skills=[], skills_mode="any", tags=[], tags_mode="any",
    )
    total = await db.talents.count_documents(query)
    if total == 0:
        return ExecResult(ok=True, message=(
            "No talents matched these criteria.\n\n"
            "You can try broadening the age, height, gender, or location criteria."
        ))

    docs = await db.talents.find(query, {"_id": 0}) \
        .sort([("name", 1), ("id", 1)]).to_list(2000)

    blocks = [_render_talent_filter_card(doc) for doc in docs]
    header = f"Found {total} talent{'s' if total != 1 else ''} matching your criteria."
    body = "\n\n---\n\n".join(blocks)
    message = f"{header}\n\n{body}"

    if len(message) > _WHATSAPP_SAFE_MESSAGE_CHAR_LIMIT:
        # ONE filter request must produce ONE WhatsApp message — never a
        # partial list split across replies. When the complete list
        # genuinely can't fit, say so honestly instead of truncating.
        return ExecResult(ok=True, message=(
            f"Found {total} talents. Please narrow your criteria to get the "
            "complete list in one message.\n\n"
            "Try narrowing by age, height, gender, or location."
        ))

    return ExecResult(ok=True, message=message)


async def _try_handle_filtered_talents_request(ctx: ExecContext, chunk: str) -> Optional[ExecResult]:
    """Returns None (never a real error) when the chunk doesn't look like a
    filter query at all, so the caller falls through to FORM's existing
    parsing — this never intercepts "<talent> form for <project>" since
    that grammar has no bare "talent(s)" word and _parse_filter_criteria
    would find zero real filters either way."""
    if not _TALENTS_WORD_RE.search(chunk):
        return None
    filters = _parse_filter_criteria(chunk)
    if not any([
        filters["gender"], filters["age_min"] is not None, filters["age_max"] is not None,
        filters["height_min"] is not None, filters["height_max"] is not None, filters["location"],
    ]):
        return None
    return await _run_filtered_talent_search(filters)


# ---------------------------------------------------------------------------
# SHOW ME FORM — natural-language parsing (comma is the ONLY structural
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
    ctx: ExecContext, name_query: str, ambiguous: List["nlu.Candidate"], resume: Dict[str, Any],
) -> ExecResult:
    """`resume` carries a `request_type` ("form" | "profile") plus whatever
    type-specific context is needed to finish that request once resolved
    (FORM's `project_query`; PROFILE needs nothing extra) — see
    _resume_show_me's _KIND_TALENT branch, which dispatches on it."""
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
            "resume": resume,
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
            return await _ask_show_me_talent_clarification(
                ctx, talent_q, ambiguous, {"request_type": "form", "project_query": project_q},
            )
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
        selector = nlu.parse_talent_selector(resolved_value or "")
        talent_id, talent_label = selector.resolved_id, selector.resolved_label
        if not talent_id:
            return ExecResult(ok=False, error="expired", message="That selection has expired — please send your command again.")
        if resume.get("request_type") == "profile":
            return ExecResult(ok=True, message=await _render_talent_profile(talent_id, talent_label))
        project_q = resume.get("project_query") or ""
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
                return await _ask_show_me_talent_clarification(
                    ctx, talent_q, ambiguous, {"request_type": "form", "project_query": project_label},
                )
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

    # PROFILE and FILTERED_TALENTS are checked against the FIRST chunk only
    # — neither command uses the "multiple independent 'show me' commands"
    # grammar FORM supports (their own examples never chain that way), and
    # both return None (never a hard error) when the chunk isn't actually
    # theirs, so FORM's existing per-chunk parsing below is completely
    # unaffected for every message that doesn't match either new shape.
    profile_result = await _try_handle_profile_request(ctx, chunks[0])
    if profile_result is not None:
        return profile_result
    filtered_result = await _try_handle_filtered_talents_request(ctx, chunks[0])
    if filtered_result is not None:
        return filtered_result

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
    "Fetches talent information from Talentgram and sends it here.\n\n"
    "COMMANDS\n\n"
    "1. SHOW ME FORM\n\n"
    "Show me Angela's form for Hinge\n\n"
    "2. SHOW ME PROFILE\n\n"
    "Show me the profile of Angela Sharma\n\n"
    "Show me the profiles of Angela, Priya, Riya\n\n"
    "3. SHOW ME TALENTS BY CRITERIA\n\n"
    "Show me all female talents between 18 and 25 in Mumbai\n\n"
    "Show me female talents height 5'4 to 5'8 in Mumbai\n\n"
    "IMPORTANT\n\n"
    "- Spelling and spacing can be approximate.\n"
    "- Commas separate multiple talents or criteria.\n"
    "- If more than one talent matches, the agent will ask you to choose.\n"
    "- Profile links are returned when available.\n"
    "- If a profile link is unavailable, the agent sends a written profile instead."
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
