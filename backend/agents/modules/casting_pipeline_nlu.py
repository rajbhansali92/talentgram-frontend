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
# stage, per the "no implicit carry-over" decision.
IMPLIED_STAGE_BY_VERB: Dict[str, str] = {
    "approve": "approved",
    "reject": "rejected",
    "shortlist": "shortlisted",
    "not available": "not_available",
    "not interested": "not_interested",
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
    error: Optional[str] = None


_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_ORDINAL_RE = re.compile(r"^\d+$")
_EVERYONE_WORDS = {"everyone", "all", "everybody"}
_MAX_RANGE_SPAN = 5000  # sanity cap so a typo'd "1-999999999" errors instead of allocating a giant set

# "Talent" is a noise word ("Move Talent 3 and 7 to Approved") and "and" is
# a valid list separator alongside commas ("Talent 3 and 7") — stripped/
# normalised before grammar matching. Harmless for real names: neither the
# bare word "talent" nor a standalone "and" is a plausible fragment of an
# actual person's name.
_NOISE_WORD_RE = re.compile(r"\btalents?\b", re.IGNORECASE)
_AND_SEP_RE = re.compile(r"\band\b", re.IGNORECASE)


def _clean_selector_text(raw: str) -> str:
    text = _NOISE_WORD_RE.sub(" ", raw or "")
    text = _AND_SEP_RE.sub(",", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


def parse_talent_selector(raw: str) -> SelectorResult:
    """Syntax-only — has no idea how many talents actually exist. Handles:
    a bare ordinal ("7"), a comma list ("2,4,5,8"), a range ("1-25"), a
    mixed list ("2,5,9-20"), "Talent 3 and 7", "everyone"/"all", or
    (falling through) a bare name to resolve by talent name."""
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

    return SelectorResult(ok=True, name_query=text)


@dataclass
class Candidate:
    id: str
    label: str
    stage: Optional[str] = None  # only populated when candidates span multiple stages (name search)


@dataclass
class ResolvedTalents:
    ok: bool
    talent_ids: List[str] = field(default_factory=list)
    talent_labels: List[str] = field(default_factory=list)
    error: Optional[str] = None


_NAME_FUZZY_CUTOFF = 0.72


def resolve_against_candidates(selector: SelectorResult, candidates: List[Candidate]) -> ResolvedTalents:
    """Pure resolution against an already-fetched candidate list — the
    caller decides what that list is scoped to (current stage for
    ordinals/everyone, whole-project for a name search). Bounds-checks
    ordinals against the REAL candidate count, so an out-of-range
    selection ("Move 500" when only 40 are listed) errors clearly instead
    of silently no-op'ing."""
    if not candidates:
        return ResolvedTalents(ok=False, error="Nothing to move.")

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

    if selector.name_query:
        q = selector.name_query.strip().lower()
        exact = [c for c in candidates if c.label.strip().lower() == q]
        if exact:
            return ResolvedTalents(ok=True, talent_ids=[exact[0].id], talent_labels=[exact[0].label])
        contains = [c for c in candidates if q in c.label.strip().lower()]
        if len(contains) == 1:
            return ResolvedTalents(ok=True, talent_ids=[contains[0].id], talent_labels=[contains[0].label])
        if len(contains) > 1:
            names = ", ".join(c.label for c in contains[:8])
            return ResolvedTalents(
                ok=False,
                error=f'Multiple talents match "{selector.name_query}": {names}. '
                      f"Please be more specific, or use their number.",
            )
        close = difflib.get_close_matches(
            q, [c.label.lower() for c in candidates], n=3, cutoff=_NAME_FUZZY_CUTOFF
        )
        if close:
            names = ", ".join(c.label for c in candidates if c.label.lower() in close)
            return ResolvedTalents(ok=False, error=f'No matching talent. Did you mean: {names}?')
        return ResolvedTalents(ok=False, error="No matching talent.")

    return ResolvedTalents(ok=False, error="I didn't catch who to move.")


# ---------------------------------------------------------------------------
# Move command extraction — trigger verb + selector text + stage text
# ---------------------------------------------------------------------------
MOVE_TRIGGERS = [
    "move", "mark", "shift", "transfer", "approve", "reject",
    "select", "shortlist", "restore", "not available", "not interested",
]

_TO_STAGE_RE = re.compile(r"^(.*?)\bto\b\s+(.+)$", re.IGNORECASE | re.DOTALL)


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
      1. an explicit "... to <stage>" tail, if present — remainder is the
         selector;
      2. else, if the verb itself implies a stage (Approve/Reject/...),
         use that — remainder (minus the verb) is the whole selector;
      3. else, scan the remainder for a bare trailing stage phrase
         ("Mark Angela Kumar Approved") — if found, remove it and the
         remainder is the selector;
      4. else, no stage found at all — target_stage stays unset and the
         generic engine will ask for it.
    """
    verb, remainder = _strip_leading_trigger(text, MOVE_TRIGGERS)
    out: Dict[str, str] = {}

    m = _TO_STAGE_RE.match(remainder)
    if m:
        selector_text, stage_text = m.group(1).strip(" ,"), m.group(2).strip()
        out["talent_selector"] = selector_text
        out["target_stage"] = stage_text
        return out

    implied = IMPLIED_STAGE_BY_VERB.get(verb or "")
    if implied and implied in stage_order:
        out["talent_selector"] = remainder.strip()
        out["target_stage"] = implied
        return out

    stage_key, ambiguous, rest = extract_stage_phrase(remainder, stage_order)
    if stage_key:
        out["talent_selector"] = rest.strip()
        out["target_stage"] = stage_key
        return out

    out["talent_selector"] = remainder.strip()
    return out


# ---------------------------------------------------------------------------
# Query classification — everything read-only routes through ONE intent;
# this decides which flavour of read the message actually is.
# ---------------------------------------------------------------------------
QUERY_TRIGGERS = [
    "show", "current", "open", "list", "view", "what",
    "how many", "who", "pending", "project", "projects",
    "summary", "p",  # "Summary Project 5" / bare "P5" (see parser.detect_trigger's glued-digit rule)
]

# Matches "Project 5", "project #5", and the "P5" / "P 5" shorthand alike.
_PROJECT_REF_RE = re.compile(r"\b(?:project|p)\s*#?\s*(\d+)\b", re.IGNORECASE)
_COUNT_HINT_RE = re.compile(r"\bhow many\b|\bcount\b", re.IGNORECASE)
_PROJECTS_LIST_HINT_RE = re.compile(
    r"\b(current|open|ongoing|active)\b.*\bprojects?\b|\bprojects?\b.*\b(active|ongoing|open|current)\b"
    r"|^projects?$|\bhow many projects\b",
    re.IGNORECASE,
)


@dataclass
class QueryIntent:
    kind: str  # "list_projects" | "project_detail" | "pipeline" | "unrecognized"
    project_ordinal: Optional[int] = None
    stage_key: Optional[str] = None
    stage_ambiguous: Optional[List[str]] = None
    count_only: bool = False


def classify_query(text: str, stage_order: List[str]) -> QueryIntent:
    project_ref = None
    m = _PROJECT_REF_RE.search(text)
    if m:
        project_ref = int(m.group(1))
        text = text[:m.start()] + " " + text[m.end():]

    stage_key, ambiguous, _rest = extract_stage_phrase(text, stage_order)

    if stage_key or ambiguous:
        return QueryIntent(
            kind="pipeline",
            project_ordinal=project_ref,
            stage_key=stage_key,
            stage_ambiguous=ambiguous,
            count_only=bool(_COUNT_HINT_RE.search(text)),
        )

    if project_ref is not None:
        return QueryIntent(kind="project_detail", project_ordinal=project_ref)

    if _PROJECTS_LIST_HINT_RE.search(text):
        return QueryIntent(kind="list_projects", count_only=bool(_COUNT_HINT_RE.search(text)))

    return QueryIntent(kind="unrecognized")
