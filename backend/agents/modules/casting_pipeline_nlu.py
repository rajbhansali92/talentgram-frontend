"""Casting Pipeline Agent — natural-language understanding layer.

Pure, DB-free, dynamic-stage-aware parsing shared by casting_pipeline.py's
two intents. Nothing here talks to Mongo — it operates on plain data
(the live PIPELINE_STAGE_ORDER list, and in-memory "candidate" lists the
caller already fetched) so it stays independently testable and so the
same resolution logic can run twice per move (once read-only for the
confirmation message, once for-real in the executor) without duplicating
it.

Design mirrors agents/modules/crm_nlu.py: rule-based entity recognition
(not statistical NLP), longest-match-first, with a "did you mean" escape
hatch (`StageMatch.ambiguous`) rather than silently guessing on a close
call — same shape as crm.py's ROLE_REGISTRY fuzzy matching.

Stage vocabulary is intentionally NEVER hardcoded here: every function
that needs to know what stages exist takes `stage_order` (the live
`PIPELINE_STAGE_ORDER` from routers/casting_pipeline.py) as a parameter,
so a stage added there later is understood immediately, with zero changes
in this file.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Stage matching
# ---------------------------------------------------------------------------
def stage_label(stage: str) -> str:
    """Canonical snake_case key -> human display label, purely mechanical
    (e.g. "ask_to_test" -> "Ask To Test") — this is what makes a brand new
    future stage (e.g. "on_hold_by_client") understood/displayed correctly
    with zero code changes here."""
    return (stage or "").replace("_", " ").strip().title()


# Common shorthand for the CURRENT stage vocabulary — a convenience
# accelerator, not the source of truth. Filtered against the live
# stage_order at match time, so a shorthand pointing at a stage that no
# longer exists is simply ignored (falls through to fuzzy matching against
# whatever's actually live) rather than erroring.
_STAGE_SHORTHAND: Dict[str, str] = {
    "test": "ask_to_test",
    "ask": "ask_to_test",
    "testing": "ask_to_test",
    "approve": "approved",
    "waiting": "hold",
    "shortlist": "shortlisted",
    "tested": "already_tested",
    "lock": "locked",
    "reject": "rejected",
    "na": "not_available",
    "not avail": "not_available",
    "ni": "not_interested",
    "followup": "follow_up",
    "follow up": "follow_up",
    "reached out": "follow_up",
}

_STAGE_FUZZY_CUTOFF = 0.72
_STAGE_AUTOCORRECT_CUTOFF = 0.85


def build_stage_registry(stage_order: List[str]) -> Dict[str, str]:
    """phrase -> canonical stage key, built fresh from whatever is live
    right now. Includes each stage's own key, its auto-derived label, and
    any shorthand that still points at a live stage."""
    registry: Dict[str, str] = {}
    for stage in stage_order:
        registry[stage] = stage
        registry[stage_label(stage).lower()] = stage
    for alias, target in _STAGE_SHORTHAND.items():
        if target in stage_order:
            registry.setdefault(alias, target)
    return registry


def format_numbered_options(header: str, options: List[List[str]]) -> str:
    """The one disambiguation-list style used identically for pipelines,
    projects, and talents — each option is a list of detail lines (a
    talent shows name/project/stage; a project or pipeline shows just its
    name), rendered numbered with a blank line between every line so a
    WhatsApp reader can scan it at a glance."""
    lines = [header, ""]
    for i, detail_lines in enumerate(options, start=1):
        lines.append(f"{i}.")
        lines.append("")
        for detail in detail_lines:
            lines.append(detail)
            lines.append("")
    lines.append("Reply with the number.")
    return "\n".join(lines).rstrip()


@dataclass
class StageMatch:
    key: Optional[str] = None
    ambiguous: Optional[List[str]] = None  # display labels, when > 1 close match


def match_stage_phrase(phrase: str, stage_order: List[str]) -> StageMatch:
    """Resolve one isolated phrase (already assumed to BE a stage name,
    not a whole sentence) against the live stage vocabulary."""
    registry = build_stage_registry(stage_order)
    key = re.sub(r"\s+", " ", (phrase or "").strip().lower()).replace("_", " ")
    if not key:
        return StageMatch()
    if key in registry:
        return StageMatch(key=registry[key])
    close = difflib.get_close_matches(key, registry.keys(), n=3, cutoff=_STAGE_FUZZY_CUTOFF)
    if close:
        ratio = difflib.SequenceMatcher(None, key, close[0]).ratio()
        if ratio >= _STAGE_AUTOCORRECT_CUTOFF:
            return StageMatch(key=registry[close[0]])
        options = sorted({registry[c] for c in close}, key=stage_label)
        return StageMatch(ambiguous=[stage_label(o) for o in options])
    return StageMatch()


def extract_stage_phrase(text: str, stage_order: List[str]) -> "tuple[Optional[str], Optional[List[str]], str]":
    """Scan free text for a stage phrase anywhere in it (word n-grams,
    longest first — mirrors crm_nlu.extract_role), returning
    (matched_key, ambiguous_labels, remaining_text_with_span_removed).
    Used for query classification and for pulling an implicit stage out of
    a move command that has no explicit "to <stage>" (e.g. "Mark Angela
    Kumar Approved")."""
    registry = build_stage_registry(stage_order)
    words = re.findall(r"[A-Za-z][A-Za-z']*", text)
    if not words:
        return None, None, text
    keys = list(registry.keys())
    best = None  # (ratio, ngram_words, close_matches)
    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            ngram_words = words[i:i + n]
            ngram = " ".join(ngram_words).lower()
            if ngram in registry:
                remaining = _remove_phrase(text, ngram_words)
                return registry[ngram], None, remaining
            close = difflib.get_close_matches(ngram, keys, n=3, cutoff=_STAGE_FUZZY_CUTOFF)
            if close:
                ratio = difflib.SequenceMatcher(None, ngram, close[0]).ratio()
                if best is None or ratio > best[0]:
                    best = (ratio, ngram_words, close)
    if best is None:
        return None, None, text
    ratio, ngram_words, close = best
    if ratio >= _STAGE_AUTOCORRECT_CUTOFF:
        remaining = _remove_phrase(text, ngram_words)
        return registry[close[0]], None, remaining
    return None, None, text  # low-confidence stray n-gram — not worth surfacing as ambiguous mid-sentence


def _remove_phrase(text: str, words: List[str]) -> str:
    phrase = r"\s+".join(re.escape(w) for w in words)
    m = re.search(phrase, text, re.IGNORECASE)
    if not m:
        return text
    return text[:m.start()] + " " + text[m.end():]


# Verbs that unambiguously imply their own target stage (only applied when
# the implied stage is actually live). Move/Mark/Shift/Transfer/Select/
# Restore are deliberately excluded — those always require an explicit
# stage, per the "no implicit carry-over" decision. Hand-curated rather
# than auto-derived from every live stage: several canonical stages
# (ask_to_test, already_tested) have no natural single-verb form ("Ask To
# Test Sarah" isn't a command anyone would type), so this stays an
# explicit, readable list — same shape as the original approve/reject/
# shortlist/not-available/not-interested entries.
IMPLIED_STAGE_BY_VERB: Dict[str, str] = {
    "approve": "approved",
    "reject": "rejected",
    "shortlist": "shortlisted",
    "not available": "not_available",
    "not interested": "not_interested",
    "hold": "hold",
    "lock": "locked",
}


# ---------------------------------------------------------------------------
# Talent selector grammar: ordinals / ranges / mixed lists / everyone / name
# ---------------------------------------------------------------------------
@dataclass
class SelectorResult:
    ok: bool
    ordinals: Optional[List[int]] = None
    everyone: bool = False
    name_query: Optional[str] = None
    name_queries: Optional[List[str]] = None  # "Aahana and Sneha" -> ["Aahana", "Sneha"]
    # Set only when the selector text is a disambiguation-pick marker (see
    # RESOLVED_TALENT_MARKER) — a specific talent already identified by ID
    # a moment ago (e.g. the user replied "2" to a disambiguation list).
    # Bypasses name matching entirely: re-matching by name a second time
    # could reproduce the exact same ambiguity, or fail on normalisation
    # quirks, whereas the ID is unambiguous by construction.
    resolved_id: Optional[str] = None
    resolved_label: Optional[str] = None
    error: Optional[str] = None


_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_ORDINAL_RE = re.compile(r"^\d+$")
_EVERYONE_WORDS = {"everyone", "all", "everybody"}
_MAX_RANGE_SPAN = 5000  # sanity cap so a typo'd "1-999999999" errors instead of allocating a giant set

# Encodes "the user just picked THIS exact talent from a disambiguation
# list" as a talent_selector value — see SelectorResult.resolved_id.
RESOLVED_TALENT_MARKER = "__resolved_talent__:"

# Encodes "ignore any project constraint and search every active project"
# as a project_query value — used when the user accepts a "would you like
# me to search all active projects instead?" offer.
FORCE_GLOBAL_MARKER = "__force_global__"

# Encodes "whoever we were just discussing" — a talent_selector value
# produced either by the user typing a pronoun ("Move him to Approved") or
# by extract_move_fields when a bare-VERB command (an EXISTING trigger —
# "Hold", "Approve him", "Lock") leaves no selector text at all.
# Deliberately NOT extended to bare stage NOUNS with no verb ("Approved",
# "Already Tested" typed alone) — those would collide with a message
# answering a pending "which pipeline?" question in an already-open move
# (detect_trigger's "a fresh trigger always restarts" rule would hijack
# the answer into starting an unrelated new move). Resolved against
# session.last_talent_id (see casting_pipeline.py's _resolve_move_selection)
# — the same "whoever was most recently and unambiguously discussed"
# referent, for both mid-clarification corrections
# and cross-command follow-ups.
PRONOUN_LAST_MARKER = "__last_referenced_talent__"
_PRONOUN_WORDS = {"him", "her", "them", "this one", "that one", "this", "that"}

# "Talent" is a noise word ("Move Talent 3 and 7 to Approved") and "and" is
# a valid list separator alongside commas ("Talent 3 and 7") — stripped/
# normalised before grammar matching. Harmless for real names: neither the
# bare word "talent" nor a standalone "and" is a plausible fragment of an
# actual person's name. Honorifics ("Mr Prajal") are stripped the same
# way — "Mr"/"Mrs"/"Ms"/"Mister"/"Miss" is never itself part of a name a
# talent would actually be searched by.
_NOISE_WORD_RE = re.compile(r"\btalents?\b", re.IGNORECASE)
_HONORIFIC_RE = re.compile(r"\b(?:mr|mrs|ms|mister|miss)\.?\s+", re.IGNORECASE)
_AND_SEP_RE = re.compile(r"\band\b", re.IGNORECASE)
# Voice-transcript filler ("hey", "please", "um") and repeated-word ("move
# move X") cleanup lives in agents/parser.clean_voice_transcript — it's
# generic (no casting-domain vocabulary) and needs to run BEFORE trigger
# detection (dispatcher.py), not just before field extraction, or a
# message like "hey move Sarah..." would never even be recognized as
# opening a move in the first place. See dispatcher.handle_inbound_message.


def _clean_selector_text(raw: str) -> str:
    text = _NOISE_WORD_RE.sub(" ", raw or "")
    text = _HONORIFIC_RE.sub(" ", text)
    text = _AND_SEP_RE.sub(",", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


def parse_talent_selector(raw: str) -> SelectorResult:
    """Syntax-only — has no idea how many talents actually exist. Handles:
    a bare ordinal ("7"), a comma list ("2,4,5,8"), a range ("1-25"), a
    mixed list ("2,5,9-20"), "Talent 3 and 7", "everyone"/"all", a
    disambiguation pick (RESOLVED_TALENT_MARKER), a pronoun referring to
    whoever was last discussed (PRONOUN_LAST_MARKER), or (falling through)
    a bare name to resolve by talent name."""
    if (raw or "").startswith(RESOLVED_TALENT_MARKER):
        payload = raw[len(RESOLVED_TALENT_MARKER):]
        tid, _, label = payload.partition("|")
        if tid and label:
            return SelectorResult(ok=True, resolved_id=tid, resolved_label=label)

    if (raw or "") == PRONOUN_LAST_MARKER:
        return SelectorResult(ok=True, name_query=PRONOUN_LAST_MARKER)

    if (raw or "").strip().lower() in _PRONOUN_WORDS:
        return SelectorResult(ok=True, name_query=PRONOUN_LAST_MARKER)

    text = _clean_selector_text(raw)
    if not text:
        return SelectorResult(ok=False, error="I didn't catch who to move.")

    lowered = text.lower()
    if lowered in _EVERYONE_WORDS:
        return SelectorResult(ok=True, everyone=True)

    if re.fullmatch(r"[\d,\-\s]+", text):
        ordinals: set = set()
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            m = _RANGE_RE.match(part)
            if m:
                start, end = int(m.group(1)), int(m.group(2))
                if start < 1 or end < start:
                    return SelectorResult(ok=False, error=f'"{part}" isn\'t a valid range.')
                if end - start > _MAX_RANGE_SPAN:
                    return SelectorResult(ok=False, error=f'"{part}" is too large a range.')
                ordinals.update(range(start, end + 1))
            elif _ORDINAL_RE.match(part):
                n = int(part)
                if n < 1:
                    return SelectorResult(ok=False, error=f'"{part}" isn\'t a valid number.')
                ordinals.add(n)
            else:
                return SelectorResult(ok=False, error=f'I couldn\'t understand "{part}".')
        if not ordinals:
            return SelectorResult(ok=False, error="I didn't catch who to move.")
        return SelectorResult(ok=True, ordinals=sorted(ordinals))

    # Not a numeric shape — one name, or several comma-separated names
    # ("Aahana and Sneha" already became "Aahana, Sneha" above). Each part
    # is resolved independently later (see resolve_against_candidates),
    # so an ambiguous/unmatched name is reported specifically rather than
    # the whole selector failing with a generic error — but a bad name
    # still blocks the move entirely, never silently drops just that one
    # person and moves the rest.
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) > 1:
        return SelectorResult(ok=True, name_queries=parts)
    return SelectorResult(ok=True, name_query=text)


@dataclass
class Candidate:
    id: str
    label: str
    stage: Optional[str] = None  # only populated when candidates span multiple stages (name search)
    # Populated only for a GLOBAL (cross-project) name search — lets an
    # ambiguous match be reported grouped by project instead of a flat,
    # unhelpfully identical-looking list of names.
    project_id: Optional[str] = None
    project_label: Optional[str] = None


@dataclass
class ResolvedTalents:
    ok: bool
    talent_ids: List[str] = field(default_factory=list)
    talent_labels: List[str] = field(default_factory=list)
    # Set alongside a single successful match found via a GLOBAL (cross-
    # project) search, so the caller knows WHICH project it came from —
    # a global search has no project_id to fall back on otherwise.
    resolved_project_id: Optional[str] = None
    resolved_project_label: Optional[str] = None
    # Set alongside an ambiguous-match error — the actual candidates that
    # tied, so the caller (casting_pipeline.py) can offer a numbered,
    # stateful "reply with the number" continuation instead of just
    # showing text the user would have to re-type a fix for.
    ambiguous_candidates: Optional[List[Candidate]] = None
    error: Optional[str] = None


_NAME_FUZZY_CUTOFF = 0.72          # worth suggesting at all
_NAME_AUTOCORRECT_CUTOFF = 0.85    # confident enough to auto-resolve without asking
_NAME_AMBIGUITY_MARGIN = 0.05      # top-2 candidates scoring this close => ask, never guess

_NAME_PUNCT_RE = re.compile(r"[^a-z0-9\s]")


def _normalize_name(s: str) -> str:
    """Lowercase, punctuation-stripped, whitespace-collapsed — absorbs the
    kind of noise a voice transcript or a typo introduces (stray periods,
    apostrophes, double spaces) without touching the underlying matching
    logic itself."""
    s = (s or "").strip().lower()
    s = _NAME_PUNCT_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _name_similarity(query: str, label: str) -> float:
    """Best-effort similarity: whole-string ratio, or (if higher) the best
    ratio between any token of the query and any token of the label. A
    short/typo'd first-name-only query ("Ahna") fuzzy-matches poorly
    against a full "First Last" label as a whole string (the unmatched
    surname dilutes the ratio) but matches its first-name token well; a
    query with an unrelated SECOND word ("Prajal Kumar" against "Prajal
    Tushir") needs the query itself tokenized too, or its one genuinely
    exact token ("Prajal") never gets to shine against the label's
    matching token — comparing token-by-token (not just whole-query-
    against-each-label-token) and taking the best score across all three
    comparisons handles every shape of query correctly."""
    q = _normalize_name(query)
    lab = _normalize_name(label)
    if not q or not lab:
        return 0.0
    whole = difflib.SequenceMatcher(None, q, lab).ratio()
    q_tokens = q.split()
    lab_tokens = lab.split()
    best_token = max(
        (difflib.SequenceMatcher(None, qt, lt).ratio() for qt in q_tokens for lt in lab_tokens),
        default=0.0,
    )
    return max(whole, best_token)


def _pick_unique_or_ambiguous(matches: List[Candidate], query: str) -> Optional[ResolvedTalents]:
    """None means "no matches at this tier, try the next one". A single
    match resolves. Multiple matches — even at a tier that used to assume
    uniqueness, like an exact match — are never silently collapsed to the
    first one; a global (cross-project) search can plausibly have two
    different people with the identical name, which a single-project
    search essentially never could, so every tier checks this the same
    way regardless of which scope called it."""
    if not matches:
        return None
    if len(matches) == 1:
        c = matches[0]
        return ResolvedTalents(
            ok=True, talent_ids=[c.id], talent_labels=[c.label],
            resolved_project_id=c.project_id, resolved_project_label=c.project_label,
        )
    return ResolvedTalents(
        ok=False, error=_format_ambiguous_matches(matches, query), ambiguous_candidates=matches,
    )


def resolve_against_candidates(selector: SelectorResult, candidates: List[Candidate]) -> ResolvedTalents:
    """Pure resolution against an already-fetched candidate list — the
    caller decides what that list is scoped to (current stage for
    ordinals/everyone, whole-project or global for a name search). Bounds-
    checks ordinals against the REAL candidate count, so an out-of-range
    selection ("Move 500" when only 40 are listed) errors clearly instead
    of silently no-op'ing."""
    if not candidates:
        return ResolvedTalents(ok=False, error="Nothing to move.")

    if selector.resolved_id:
        # A disambiguation pick from a moment ago — bypass name matching
        # entirely (re-matching by name could reproduce the exact same
        # ambiguity). If this exact candidate isn't in the current list
        # (scope narrowed since the pick was made), trust the identity
        # directly; the executor re-verifies current stage before writing
        # regardless of how the talent_id was arrived at.
        match = next((c for c in candidates if c.id == selector.resolved_id), None)
        if match:
            return ResolvedTalents(
                ok=True, talent_ids=[match.id], talent_labels=[match.label],
                resolved_project_id=match.project_id, resolved_project_label=match.project_label,
            )
        return ResolvedTalents(ok=True, talent_ids=[selector.resolved_id], talent_labels=[selector.resolved_label or "talent"])

    if selector.everyone:
        return ResolvedTalents(
            ok=True,
            talent_ids=[c.id for c in candidates],
            talent_labels=[c.label for c in candidates],
        )

    if selector.ordinals:
        max_ord = len(candidates)
        out_of_range = [n for n in selector.ordinals if n > max_ord]
        if out_of_range:
            return ResolvedTalents(
                ok=False,
                error=f"Only {max_ord} talent(s) are listed — #{out_of_range[0]} is out of range.",
            )
        ids, labels = [], []
        for n in selector.ordinals:
            c = candidates[n - 1]
            ids.append(c.id)
            labels.append(c.label)
        return ResolvedTalents(ok=True, talent_ids=ids, talent_labels=labels)

    if selector.name_queries:
        # Multiple names in one command ("Aahana and Sneha to Approved") —
        # resolve each independently (reusing the single-name branch below
        # via recursion, rather than duplicating its exact/contains/fuzzy
        # escalation) and combine. Any one name failing to resolve
        # uniquely blocks the WHOLE move — never silently proceeds with a
        # partial set, and reports specifically which name was the problem.
        ids: List[str] = []
        labels: List[str] = []
        seen_ids: set = set()
        errors: List[str] = []
        for q in selector.name_queries:
            one = resolve_against_candidates(SelectorResult(ok=True, name_query=q), candidates)
            if not one.ok:
                errors.append(one.error or f'No matching talent for "{q}".')
                continue
            for tid, label in zip(one.talent_ids, one.talent_labels):
                if tid not in seen_ids:
                    seen_ids.add(tid)
                    ids.append(tid)
                    labels.append(label)
        if errors:
            return ResolvedTalents(ok=False, error=" | ".join(errors))
        return ResolvedTalents(ok=True, talent_ids=ids, talent_labels=labels)

    if selector.name_query:
        raw_q = selector.name_query.strip()
        if not raw_q:
            return ResolvedTalents(ok=False, error="I didn't catch who to move.")

        # Tier 1: exact, case-SENSITIVE — the single highest-confidence
        # signal, tried before anything normalises casing away.
        result = _pick_unique_or_ambiguous(
            [c for c in candidates if c.label.strip() == raw_q], raw_q
        )
        if result is not None:
            return result

        # Tier 2: exact, case-insensitive.
        q_lower = raw_q.lower()
        result = _pick_unique_or_ambiguous(
            [c for c in candidates if c.label.strip().lower() == q_lower], raw_q
        )
        if result is not None:
            return result

        # Tier 3: normalized substring (handles a short first name like
        # "Sneh" or "Aahana" against a full "First Last" label, and
        # absorbs punctuation/whitespace noise from a voice transcript).
        q_norm = _normalize_name(raw_q)
        result = _pick_unique_or_ambiguous(
            [c for c in candidates if q_norm and q_norm in _normalize_name(c.label)], raw_q
        )
        if result is not None:
            return result

        # Tier 4: fuzzy — whole-string or best-token similarity (see
        # _name_similarity for why both are tried). Auto-resolves ONLY
        # when the top candidate is both confident (>= autocorrect cutoff)
        # AND clearly ahead of the next-best one; two candidates scoring
        # within the ambiguity margin of each other are never silently
        # collapsed into a guess, regardless of how high either scores.
        scored = sorted(
            ((c, _name_similarity(raw_q, c.label)) for c in candidates),
            key=lambda pair: pair[1], reverse=True,
        )
        scored = [(c, s) for c, s in scored if s >= _NAME_FUZZY_CUTOFF]
        if not scored:
            return ResolvedTalents(ok=False, error="No matching talent.")

        top_c, top_s = scored[0]
        if len(scored) == 1:
            # Only one candidate cleared even the base "worth suggesting"
            # cutoff — there is nothing for it to be AMBIGUOUS with, so
            # the higher autocorrect+margin bar (which exists purely to
            # protect against two confusable near-ties) doesn't apply.
            # Resolving here isn't "guessing": every move/add still shows
            # a confirmation with this exact name before anything is
            # written, which is the real safety net — the alternative
            # (an "I found multiple matching talents" list with a single
            # option) is strictly worse UX for zero extra safety.
            return ResolvedTalents(
                ok=True, talent_ids=[top_c.id], talent_labels=[top_c.label],
                resolved_project_id=top_c.project_id, resolved_project_label=top_c.project_label,
            )
        second_s = scored[1][1]
        if top_s >= _NAME_AUTOCORRECT_CUTOFF and (top_s - second_s) > _NAME_AMBIGUITY_MARGIN:
            return ResolvedTalents(
                ok=True, talent_ids=[top_c.id], talent_labels=[top_c.label],
                resolved_project_id=top_c.project_id, resolved_project_label=top_c.project_label,
            )
        close_enough = [c for c, s in scored if (top_s - s) <= _NAME_AMBIGUITY_MARGIN][:8]
        return ResolvedTalents(
            ok=False,
            error=_format_ambiguous_matches(close_enough, raw_q),
            ambiguous_candidates=close_enough,
        )

    return ResolvedTalents(ok=False, error="I didn't catch who to move.")


def _format_ambiguous_matches(matches: List[Candidate], query: str) -> str:
    """Uniform numbered disambiguation (see format_numbered_options) —
    each option shows the talent's name, their project (only when matches
    span more than one — redundant to repeat an identical project on
    every line otherwise), and their current stage."""
    multi_project = len({c.project_id for c in matches if c.project_id}) > 1
    options: List[List[str]] = []
    for c in matches[:8]:
        details = [c.label]
        if multi_project and c.project_label:
            details.append(c.project_label)
        if c.stage:
            details.append(stage_label(c.stage))
        options.append(details)
    return format_numbered_options("I found multiple matching talents.", options)


# ---------------------------------------------------------------------------
# Disambiguation-reply resolution — the ONE way a numbered clarification
# list (talents, projects, or pipelines — see format_numbered_options) is
# answered: a digit, an ordinal word, or a free-text label match. Shared by
# casting.move and casting.query so "2" / "the third one" / "Main Guy" all
# resolve identically regardless of which kind of ambiguity is pending.
# ---------------------------------------------------------------------------
_ORDINAL_WORD_TO_INDEX: Dict[str, int] = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
    "sixth": 6, "6th": 6,
    "seventh": 7, "7th": 7,
    "eighth": 8, "8th": 8,
    "ninth": 9, "9th": 9,
    "tenth": 10, "10th": 10,
    "top": 1,  # "top one"
    "last": -1, "bottom": -1,  # "-1" == the last option, resolved below
}

# Tolerates "the second one", "option 2", "#2", "number 2" wrapping around
# either a digit or an ordinal word — the digit branch in
# resolve_option_reply handles a bare number itself, so this only needs to
# recognize the ORDINAL WORD forms (plus a wrapped bare digit, since
# "option 2" isn't already caught by `stripped.isdigit()`).
_ORDINAL_WRAPPED_RE = re.compile(
    r"^\s*(?:the\s+)?(?:number\s+|option\s+|#\s*)?"
    r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"1st|2nd|3rd|4th|5th|6th|7th|8th|9th|10th|top|last|bottom|\d+)"
    r"(?:\s+(?:one|option|choice))?\s*$",
    re.IGNORECASE,
)


def resolve_option_reply(reply: str, options: List[Dict[str, str]]) -> Optional[int]:
    """1-based index into `options` the reply refers to, or None if it
    can't be determined without guessing (caller should re-prompt, never
    silently pick). Tries, in order: a bare digit, an ordinal word/phrase
    (tolerant of "the"/"option"/"#"/"number" wrapping), then a token-based
    fuzzy match of the reply against each option's own `label` — the same
    escalation and same never-guess-on-a-close-call rule as talent/project
    name resolution."""
    n = len(options)
    if n == 0:
        return None
    stripped = (reply or "").strip()
    if not stripped:
        return None

    if stripped.isdigit():
        idx = int(stripped)
        return idx if 1 <= idx <= n else None

    m = _ORDINAL_WRAPPED_RE.match(stripped)
    if m:
        word = m.group(1).lower()
        idx = int(word) if word.isdigit() else _ORDINAL_WORD_TO_INDEX.get(word)
        if idx is not None:
            resolved = idx if idx > 0 else n + idx + 1  # -1 -> n ("last")
            if 1 <= resolved <= n:
                return resolved

    labels = [opt.get("label") or "" for opt in options]
    idx0 = _match_label_tiers(stripped, labels)
    return (idx0 + 1) if idx0 is not None else None


def _normalize_project_label(s: str) -> str:
    return _normalize_name(s)  # same rules: lowercase, punctuation/hyphen-stripped, whitespace-collapsed


def _token_subset_matches(query: str, labels: List[str]) -> List[int]:
    """Indices of every label whose normalized tokens are a SUPERSET of the
    normalized query's tokens — order-independent, punctuation/hyphen/case-
    insensitive ("Pulsar Guy" / "Bajaj Main Guy" both match "Bajaj Pulsar -
    Main Guy"). Precise (not edit-distance), so unlike character-level
    fuzzy this is safe to auto-resolve on when unique."""
    q_tokens = set(_normalize_project_label(query).split())
    if not q_tokens:
        return []
    return [i for i, l in enumerate(labels) if q_tokens <= set(_normalize_project_label(l).split())]


def _match_label_tiers(query: str, labels: List[str]) -> Optional[int]:
    """0-based index of the single unambiguous match among `labels`, using
    the same exact -> normalized-exact -> normalized-substring ->
    token-subset -> fuzzy escalation as project-name resolution. Returns
    None on no match OR an ambiguous tie (never guesses) — used for
    resolving a free-text reply against a short list of disambiguation
    options, where returning the wrong index would apply the wrong choice."""
    q = (query or "").strip()
    if not q or not labels:
        return None

    q_lower = q.lower()
    exact = [i for i, l in enumerate(labels) if l.strip().lower() == q_lower]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None

    q_norm = _normalize_project_label(q)
    normalized = [_normalize_project_label(l) for l in labels]
    nexact = [i for i, l in enumerate(normalized) if q_norm and l == q_norm]
    if len(nexact) == 1:
        return nexact[0]
    if len(nexact) > 1:
        return None

    contains = [i for i, l in enumerate(normalized) if q_norm and (q_norm in l or l in q_norm)]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        return None

    subset = _token_subset_matches(q, labels)
    if len(subset) == 1:
        return subset[0]
    if len(subset) > 1:
        return None

    scored = sorted(
        ((i, _name_similarity(q, labels[i])) for i in range(len(labels))),
        key=lambda pair: pair[1], reverse=True,
    )
    scored = [(i, s) for i, s in scored if s >= _NAME_AUTOCORRECT_CUTOFF]
    if not scored:
        return None
    top_i, top_s = scored[0]
    second_s = scored[1][1] if len(scored) > 1 else 0.0
    if (top_s - second_s) > _NAME_AMBIGUITY_MARGIN:
        return top_i
    return None


# ---------------------------------------------------------------------------
# Move command extraction — trigger verb + selector text + stage text
# ---------------------------------------------------------------------------
MOVE_TRIGGERS = [
    "move", "mark", "shift", "transfer", "approve", "reject",
    "select", "shortlist", "restore", "not available", "not interested",
    "put",  # "Put Arya into Approved for Pantaloons"
    "hold", "lock",  # "Hold Sarah" / "Lock Rahul" — stage-first shorthand
]

# "to" or "into" both introduce the destination stage ("Move X to Approved",
# "Put X into Approved"). Non-greedy prefix so the FIRST such word found is
# treated as the stage connector, not a later coincidental "to"/"into".
_TO_STAGE_RE = re.compile(r"^(.*?)\b(?:to|into)\b\s+(.+)$", re.IGNORECASE | re.DOTALL)

# An explicit project reference inside a MOVE command — "... in Toyota
# Glanza", "... for Pantaloons". Deliberately extracted BEFORE stage/
# selector parsing (see extract_move_fields) so it can never be swallowed
# into either, and so a name/stage phrase containing "in"/"for" only as
# part of a longer word (never as its own word) is left untouched — \b
# enforces whole-word matches only, so "into" is never mistaken for "in".
_PROJECT_IN_MOVE_RE = re.compile(r"\b(?:in|for)\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _strip_leading_trigger(text: str, triggers: List[str]) -> "tuple[Optional[str], str]":
    working = " ".join(text.strip().split())
    lowered = working.lower()
    best = None  # (len, trigger, remainder)
    for trig in triggers:
        t = trig.lower()
        if lowered == t:
            cand = (len(t), t, "")
        elif lowered.startswith(t + " ") or lowered.startswith(t + ":"):
            cand = (len(t), t, working[len(trig):].lstrip(" :"))
        else:
            continue
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:
        return None, working
    return best[1], best[2]


def extract_move_fields(text: str, stage_order: List[str]) -> Dict[str, str]:
    """IntentDefinition.extract_fields-compatible: {field_key: raw_value}
    for casting.move. Pulls the trigger verb off the front, then:
      0. an explicit "... in <project>" / "... for <project>" tail, if
         present, extracted FIRST and removed — so it can't end up
         swallowed into the stage or the talent selector below (see
         _PROJECT_IN_MOVE_RE's docstring for why this is safe to do
         unconditionally, even when no project is actually mentioned).
      1. an explicit "... to <stage>" / "... into <stage>" tail, if
         present — remainder is the selector;
      2. else, if the verb itself implies a stage (Approve/Reject/...),
         use that — remainder (minus the verb) is the whole selector;
      3. else, scan the remainder for a bare trailing stage phrase
         ("Mark Angela Kumar Approved") — if found, remove it and the
         remainder is the selector;
      4. else, no stage found at all — target_stage stays unset and the
         generic engine will ask for it.

    Talent name(s) always end up as whatever's left after the project and
    stage references are removed — which is exactly how "explicit talent
    name" ends up taking effective priority over everything else: it's
    never partially consumed by the (separately, unambiguously anchored)
    project/stage extraction.
    """
    verb, remainder = _strip_leading_trigger(text, MOVE_TRIGGERS)
    out: Dict[str, str] = {}

    proj_m = _PROJECT_IN_MOVE_RE.search(remainder)
    if proj_m:
        project_candidate = proj_m.group(1).strip(" .!?")
        if project_candidate:
            out["project_query"] = project_candidate
            remainder = remainder[:proj_m.start()].strip()

    def _selector_or_pronoun(s: str) -> str:
        # A stage-only or bare-verb command ("Already Tested", "Hold",
        # "Approve him") leaves no talent named at all — rather than
        # asking "who?" every time, fall back to "whoever we were just
        # discussing" (see PRONOUN_LAST_MARKER), resolved against
        # session.last_talent_id in casting_pipeline.py.
        s = s.strip(" .!?,")
        return s if s else PRONOUN_LAST_MARKER

    m = _TO_STAGE_RE.match(remainder)
    if m:
        selector_text = m.group(1).strip(" ,")
        stage_text = m.group(2).strip(" .!?,")
        out["talent_selector"] = _selector_or_pronoun(selector_text)
        out["target_stage"] = stage_text
        return out

    implied = IMPLIED_STAGE_BY_VERB.get(verb or "")
    if implied and implied in stage_order:
        out["talent_selector"] = _selector_or_pronoun(remainder)
        out["target_stage"] = implied
        return out

    stage_key, ambiguous, rest = extract_stage_phrase(remainder, stage_order)
    if stage_key:
        out["talent_selector"] = _selector_or_pronoun(rest)
        out["target_stage"] = stage_key
        return out

    out["talent_selector"] = remainder.strip(" .!?")
    return out


# ---------------------------------------------------------------------------
# Add-to-pipeline command extraction — "Add <name(s)> to <project>" always
# targets Ask To Test (no stage field at all — casting_pipeline.py's
# ADD_INTENT hardcodes it, never asks). Supports both the single-line form
# ("Add Prajal, Arya to Toyota Glanza") and the one-name-per-line form:
#   Add
#   Prajal
#   Angela Kumar
#   Arya
#   to
#   Toyota Glanza
# ---------------------------------------------------------------------------
ADD_TRIGGERS = ["add"]

_ADD_TRIGGER_RE = re.compile(r"^\s*add\b[\s:]*", re.IGNORECASE)
# Tolerant of a comma (from the newline-join below) OR plain whitespace on
# either side of the connector, so both message shapes above resolve to
# the same project tail.
_PROJECT_IN_ADD_RE = re.compile(r"[,\s]*\b(?:to|in|for)\b[,\s]*(.+)$", re.IGNORECASE | re.DOTALL)


def extract_add_fields(text: str) -> Dict[str, str]:
    """IntentDefinition.extract_fields-compatible: {field_key: raw_value}
    for casting.add. Strips the "add" trigger from the first non-blank
    line, joins any remaining lines with ", " — turning the one-name-per-
    line form into the exact same comma-separated shape
    "Add Prajal, Arya to Toyota Glanza" already has — then extracts the
    trailing "to"/"in"/"for" project reference, leaving whatever's left as
    the talent selector text (fed to parse_talent_selector unchanged,
    which already understands comma/"and"-separated multi-name lists)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return {}
    lines[0] = _ADD_TRIGGER_RE.sub("", lines[0], count=1).strip()
    if not lines[0]:
        lines = lines[1:]
    remainder = ", ".join(lines)

    out: Dict[str, str] = {}
    proj_m = _PROJECT_IN_ADD_RE.search(remainder)
    if proj_m:
        project_candidate = proj_m.group(1).strip(" .!?,")
        if project_candidate:
            out["project_query"] = project_candidate
            remainder = remainder[:proj_m.start()]

    out["talent_selector"] = remainder.strip(" .!?,")
    return out


# ---------------------------------------------------------------------------
# Query classification — everything read-only routes through ONE intent;
# this decides which flavour of read the message actually is.
# ---------------------------------------------------------------------------
QUERY_TRIGGERS = [
    "show", "current", "open", "list", "view", "what",
    "how many", "who", "pending", "project", "projects",
    "summary", "p",  # "Summary Project 5" / bare "P5" (see parser.detect_trigger's glued-digit rule)
    # "Show again"/"repeat" — replay the last listing without repeating
    # the project/pipeline.
    "again", "repeat",
]

# Matches "Project 5", "project #5", and the "P5" / "P 5" shorthand alike.
_PROJECT_REF_RE = re.compile(r"\b(?:project|p)\s*#?\s*(\d+)\b", re.IGNORECASE)
_COUNT_HINT_RE = re.compile(r"\bhow many\b|\bcount\b", re.IGNORECASE)
_PROJECTS_LIST_HINT_RE = re.compile(
    r"\b(current|open|ongoing|active)\b.*\bprojects?\b|\bprojects?\b.*\b(active|ongoing|open|current)\b"
    r"|^projects?$|\bhow many projects\b",
    re.IGNORECASE,
)

# An explicit project NAME reference — "... for Google - Film 1 & 3", "... of
# Toyota Glanza". Applied to the text AFTER stage-phrase removal (see
# classify_query) so a stage phrase appearing before "for"/"of" is never
# swallowed into the captured name.
_FOR_PROJECT_RE = re.compile(r"\b(?:for|of)\s+(.+)$", re.IGNORECASE | re.DOTALL)

# "Show again" / "again" / "repeat" / "open it" / "show it" — replay
# whatever the last successful listing was (a fresh live query against
# current session context, not a cached echo — see casting_pipeline.py's
# _handle_replay).
_REPLAY_RE = re.compile(
    r"^\s*(?:show\s+(?:it\s+)?again|open\s+it|show\s+it|again|repeat)[\s.!?]*$", re.IGNORECASE
)


@dataclass
class QueryIntent:
    kind: str  # "list_projects" | "project_detail" | "pipeline" | "replay" | "unrecognized"
    project_ordinal: Optional[int] = None
    project_name_query: Optional[str] = None
    stage_key: Optional[str] = None
    stage_ambiguous: Optional[List[str]] = None
    count_only: bool = False


def classify_query(text: str, stage_order: List[str]) -> QueryIntent:
    stripped = (text or "").strip()
    if _REPLAY_RE.match(stripped):
        return QueryIntent(kind="replay")

    project_ref = None
    m = _PROJECT_REF_RE.search(text)
    if m:
        project_ref = int(m.group(1))
        text = text[:m.start()] + " " + text[m.end():]

    stage_key, ambiguous, rest = extract_stage_phrase(text, stage_order)

    project_name_query = None
    name_m = _FOR_PROJECT_RE.search(rest)
    if name_m:
        candidate = name_m.group(1).strip(" ?.!\n")
        if candidate:
            project_name_query = candidate

    if stage_key or ambiguous:
        return QueryIntent(
            kind="pipeline",
            project_ordinal=project_ref,
            project_name_query=project_name_query,
            stage_key=stage_key,
            stage_ambiguous=ambiguous,
            count_only=bool(_COUNT_HINT_RE.search(text)),
        )

    if project_ref is not None or project_name_query:
        return QueryIntent(kind="project_detail", project_ordinal=project_ref, project_name_query=project_name_query)

    if _PROJECTS_LIST_HINT_RE.search(text):
        return QueryIntent(kind="list_projects", count_only=bool(_COUNT_HINT_RE.search(text)))

    return QueryIntent(kind="unrecognized")


# ---------------------------------------------------------------------------
# Project name resolution — mirrors resolve_against_candidates' name-lookup
# escalation (exact -> unique substring -> fuzzy) but for project brand
# names rather than talent names; a separate function because the shapes
# differ (no ordinals/ranges/"everyone" for projects) and mixing them would
# make both harder to follow.
# ---------------------------------------------------------------------------
@dataclass
class ProjectNameMatch:
    project: Optional[Dict[str, str]] = None  # {"id": ..., "label": ...}
    ambiguous: Optional[List[str]] = None      # multiple real (exact/substring) matches tied
    suggestions: Optional[List[str]] = None    # no real match, but fuzzy found close candidates
    error: Optional[str] = None


_PROJECT_NAME_FUZZY_CUTOFF = 0.6


def resolve_project_by_name(name_query: str, projects: List[Dict[str, str]]) -> ProjectNameMatch:
    q_raw = (name_query or "").strip()
    if not q_raw or not projects:
        return ProjectNameMatch(error=f'I couldn\'t find a project matching "{name_query}".')

    labels = [(p.get("label") or "") for p in projects]
    q_lower = q_raw.lower()

    # Tier 1: exact, case-insensitive.
    exact = [i for i, l in enumerate(labels) if l.strip().lower() == q_lower]
    if len(exact) == 1:
        return ProjectNameMatch(project=projects[exact[0]])
    if len(exact) > 1:
        return ProjectNameMatch(ambiguous=[labels[i] for i in exact[:8]])

    # Tier 2: normalized exact — case/punctuation/hyphen/whitespace-
    # insensitive ("Bajaj Pulsar Main Guy" == "Bajaj Pulsar - Main Guy").
    q_norm = _normalize_project_label(q_raw)
    normalized_labels = [_normalize_project_label(l) for l in labels]
    nexact = [i for i, l in enumerate(normalized_labels) if q_norm and l == q_norm]
    if len(nexact) == 1:
        return ProjectNameMatch(project=projects[nexact[0]])
    if len(nexact) > 1:
        return ProjectNameMatch(ambiguous=[labels[i] for i in nexact[:8]])

    # Tier 3: normalized substring (either direction).
    contains = [
        i for i, l in enumerate(normalized_labels) if q_norm and (q_norm in l or l in q_norm)
    ]
    if len(contains) == 1:
        return ProjectNameMatch(project=projects[contains[0]])
    if len(contains) > 1:
        return ProjectNameMatch(ambiguous=[labels[i] for i in contains[:8]])

    # Tier 4: order-independent token-subset — every normalized query token
    # present somewhere in the candidate's tokens, regardless of order or
    # what's interposed between them ("Pulsar Guy" / "Bajaj Main Guy" both
    # match "Bajaj Pulsar - Main Guy"). Precise (not edit-distance), so
    # it's safe to auto-resolve on when unique — same safety bar as the
    # substring tier above, just reorder-tolerant.
    subset_idxs = _token_subset_matches(q_raw, labels)
    if len(subset_idxs) == 1:
        return ProjectNameMatch(project=projects[subset_idxs[0]])
    if len(subset_idxs) > 1:
        return ProjectNameMatch(ambiguous=[labels[i] for i in subset_idxs[:8]])

    # Tier 5: character-level fuzzy / partial token overlap. Deliberately
    # never auto-resolves here, however close — moving someone into the
    # WRONG client's project is a higher-stakes mistake than a talent-name
    # typo, so a close fuzzy hit is always offered as a suggestion to
    # confirm, never silently applied.
    labels_lower = [l.strip().lower() for l in labels]
    close = difflib.get_close_matches(q_lower, labels_lower, n=3, cutoff=_PROJECT_NAME_FUZZY_CUTOFF)
    if close:
        matched = [labels[i] for i, l in enumerate(labels_lower) if l in close]
        return ProjectNameMatch(suggestions=matched)

    return ProjectNameMatch(error=f'I couldn\'t find a project matching "{name_query}".')
