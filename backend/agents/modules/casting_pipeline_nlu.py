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
import functools
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core import parse_height_to_inches


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


@functools.lru_cache(maxsize=8)
def _build_stage_registry_cached(stage_order_tuple: Tuple[str, ...]) -> Dict[str, str]:
    registry: Dict[str, str] = {}
    for stage in stage_order_tuple:
        registry[stage] = stage
        registry[stage_label(stage).lower()] = stage
    for alias, target in _STAGE_SHORTHAND.items():
        if target in stage_order_tuple:
            registry.setdefault(alias, target)
    return registry


def build_stage_registry(stage_order: List[str]) -> Dict[str, str]:
    """phrase -> canonical stage key, built from whatever is live right
    now. Includes each stage's own key, its auto-derived label, and any
    shorthand that still points at a live stage. Memoized on the stage
    order's contents (PIPELINE_STAGE_ORDER never changes at runtime, so
    this is a pure win, not a staleness risk) — called from several
    places per message (match_stage_phrase, extract_stage_phrase), so the
    same ~11-entry dict was being rebuilt repeatedly. Still takes
    `stage_order` as a parameter rather than importing PIPELINE_STAGE_ORDER
    directly — this module stays dynamic-stage-vocabulary-driven, never
    hardcoded, exactly as before."""
    return _build_stage_registry_cached(tuple(stage_order))


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


# ---------------------------------------------------------------------------
# Query-only "selected" support (UX polish, 2026-08-09). "Selected"/"select"/
# "selection" has no single, unambiguous meaning in this vocabulary —
# "Approved" and "Locked" are both plausible "chosen for the role" readings,
# and the sibling WhatsApp campaign agent's own stage matcher deliberately
# refuses to resolve "Selected" to any one stage for exactly this reason
# (see whatsapp_campaign_agent.py's _resolve_pipeline_stage). Rather than
# guess, this is wired through the SAME "ambiguous" shape a genuine fuzzy
# near-tie already produces (StageMatch.ambiguous / extract_stage_phrase's
# second return value) — so it flows through the one existing stage-
# ambiguous clarification path with zero new UI/plumbing.
#
# Deliberately NOT added to _STAGE_SHORTHAND/build_stage_registry: that
# registry is shared by casting.move's own stage resolution
# (extract_move_fields's tier-3 fallback, _validate_target_stage) — mixing
# an intentionally-ambiguous entry into it would change what "Mark Sarah
# Selected" does in a MOVE command, which this polish sprint must not touch.
# These wrappers are consulted ONLY from classify_query and this module's
# talent-query detection (i.e. only by casting.query) — extract_stage_phrase
# and match_stage_phrase themselves are completely unmodified, so
# extract_move_fields/_validate_target_stage behave byte-for-byte as before.
_QUERY_AMBIGUOUS_STAGE_WORDS: Dict[str, Tuple[str, ...]] = {
    "selected": ("approved", "locked"),
    "select": ("approved", "locked"),
    "selection": ("approved", "locked"),
}
_QUERY_AMBIGUOUS_STAGE_RE = re.compile(
    r"\b(?:" + "|".join(_QUERY_AMBIGUOUS_STAGE_WORDS.keys()) + r")\b", re.IGNORECASE
)


def _query_ambiguous_stage_lookup(word: str, stage_order: List[str]) -> Optional[StageMatch]:
    targets = _QUERY_AMBIGUOUS_STAGE_WORDS.get((word or "").strip().lower())
    if not targets:
        return None
    live = [t for t in targets if t in stage_order]
    if len(live) > 1:
        return StageMatch(ambiguous=[stage_label(t) for t in live])
    if len(live) == 1:
        return StageMatch(key=live[0])
    return None  # neither candidate stage is live anymore — fall through as usual


def match_stage_phrase_for_query(phrase: str, stage_order: List[str]) -> StageMatch:
    """Query-only counterpart of match_stage_phrase — tries the small
    deliberately-ambiguous word set above FIRST (an exact, case-insensitive
    whole-phrase check, not fuzzy matching — "selected" doesn't score high
    enough against any real stage to reach match_stage_phrase's own fuzzy
    tier anyway), then delegates to the real, unmodified match_stage_phrase
    for everything else."""
    hit = _query_ambiguous_stage_lookup(phrase, stage_order)
    if hit is not None:
        return hit
    return match_stage_phrase(phrase, stage_order)


def extract_stage_phrase_for_query(
    text: str, stage_order: List[str]
) -> "tuple[Optional[str], Optional[List[str]], str]":
    """Query-only counterpart of extract_stage_phrase — scans for the
    ambiguous word set as a whole word anywhere in the text first (cheap
    regex, no difflib involved), then delegates to the real, unmodified
    extract_stage_phrase for everything else, including every stage this
    system already understands unambiguously."""
    m = _QUERY_AMBIGUOUS_STAGE_RE.search(text or "")
    if m:
        hit = _query_ambiguous_stage_lookup(m.group(0), stage_order)
        if hit is not None:
            remaining = text[:m.start()] + " " + text[m.end():]
            return hit.key, hit.ambiguous, remaining
    return extract_stage_phrase(text, stage_order)


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
    # Phase 2 (Talent Selection & Add to Project, 2026-08-10) — set when the
    # raw selector text was literally "selected"/"the selection"/"my
    # selection", meaning "use the session's selection basket" rather than
    # a name to fuzzy-match. See casting_pipeline.py's _resolve_add_selection.
    use_basket: bool = False
    # Set when the raw selector text carried SELECTION_CMD_MARKER — a
    # Phase-2 "Select N"/"first 5"/... shorthand that arrived via
    # casting.move's own "select" trigger (see module-level docstring on
    # SELECTION_CMD_MARKER). Carries the raw command text (post-trigger)
    # for casting_pipeline.py to re-parse with extract_selection_command.
    raw_command: Optional[str] = None
    # UX polish (2026-08-10) — "Add 1,3,5 to X"/"Add first 5 to X" direct
    # shortcut. Set when the raw selector text carried
    # ADD_ORDINAL_SPEC_MARKER; carries the spec text ("1,3,5"/"first 5"/
    # "all"/...) for casting_pipeline.py to resolve via
    # resolve_selection_spec against the CURRENT talent-search
    # number_map — the exact same resolution Select/Remove use. Distinct
    # from use_basket: this never reads or writes session.selection_basket
    # at all, so it can never clobber an unrelated, real selection the
    # user might already be building.
    selection_spec: Optional[str] = None


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

# Phase 2 (Talent Selection & Add to Project, 2026-08-10) — "select" is
# already a live casting.move trigger ("Select Priya to Approved" is a real
# stage-move using "select" as a generic verb, same as "move"/"mark"). A
# Phase-2 shorthand ("Select 1,3,5") only ever reaches here because
# casting_pipeline.py's _extract_move_fields pre-checks the raw text BEFORE
# calling extract_move_fields at all — it recognizes a pure selection shape
# (no "to"/"into" stage connector) and hands it off encoded with this
# marker instead, so it sails through _validate_selector/parse_talent_selector
# unchanged rather than being (mis)parsed as a name or bare-ordinal move
# selector. See casting_pipeline.py's _handle_move_selection_shorthand.
SELECTION_CMD_MARKER = "__phase2_select__:"

# UX polish (2026-08-10) — "Add 1,3,5 to X"/"Add first 5 to X" direct
# shortcut. casting_pipeline.py's _extract_add_fields pre-checks the
# talent_selector text it already extracted (BEFORE this marker exists) —
# if it looks like a pure selection spec (digits/range/first-N/last-N/all,
# recognized via extract_selection_command), it's re-encoded with this
# marker so it passes _validate_selector/parse_talent_selector unchanged
# rather than being (mis)parsed as a name.
ADD_ORDINAL_SPEC_MARKER = "__add_ordinal_spec__:"

# "Add selected to X" / "Attach selected to X" — the literal word meaning
# "use the session's selection basket," never a talent's actual name.
_BASKET_SELECTOR_WORDS = {"selected", "the selection", "my selection"}

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

    if (raw or "").startswith(SELECTION_CMD_MARKER):
        return SelectorResult(ok=True, raw_command=raw[len(SELECTION_CMD_MARKER):])

    if (raw or "").startswith(ADD_ORDINAL_SPEC_MARKER):
        return SelectorResult(ok=True, selection_spec=raw[len(ADD_ORDINAL_SPEC_MARKER):])

    if (raw or "") == PRONOUN_LAST_MARKER:
        return SelectorResult(ok=True, name_query=PRONOUN_LAST_MARKER)

    if (raw or "").strip().lower() in _PRONOUN_WORDS:
        return SelectorResult(ok=True, name_query=PRONOUN_LAST_MARKER)

    if (raw or "").strip().lower() in _BASKET_SELECTOR_WORDS:
        return SelectorResult(ok=True, use_basket=True)

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
    # Local, request_scope-free sub-stage timing (2026-08-05 latency
    # sprint) — {"normalize", "exact_match", "token_match", "fuzzy_scoring",
    # "ranking"} in ms, plus "candidate_count". Kept as plain time.monotonic()
    # deltas (not agents.request_scope) so this module stays framework-
    # agnostic/pure and independently unit-testable; the caller
    # (casting_pipeline.py) folds these into the fine-grained op log.
    # Only populated on the single-name_query tier-escalation path (the one
    # this investigation is about) — the ordinal/everyone/resolved_id/
    # name_queries branches are index lookups or thin recursion, not real
    # comparison work.
    timing: Dict[str, float] = field(default_factory=dict)


_NAME_FUZZY_CUTOFF = 0.72          # worth suggesting at all
_NAME_AUTOCORRECT_CUTOFF = 0.85    # confident enough to auto-resolve without asking
_NAME_AMBIGUITY_MARGIN = 0.05      # top-2 candidates scoring this close => ask, never guess

_NAME_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
# A trailing possessive ("Ahaan's") must become its own clean token
# ("ahaan"), not merge into "ahaans" — deleting the apostrophe outright (the
# old behaviour) glues the "s" onto the base word, which then can never
# match a query token like "ahaan" under the token-subset tier below. Strip
# the possessive as a unit, before generic punctuation stripping ever runs.
_POSSESSIVE_RE = re.compile(r"'s\b")


def _normalize_name(s: str) -> str:
    """Lowercase, punctuation-stripped, whitespace-collapsed — absorbs the
    kind of noise a voice transcript or a typo introduces (stray periods,
    apostrophes, double spaces) without touching the underlying matching
    logic itself. A possessive "'s" is stripped as a unit (see
    _POSSESSIVE_RE) and a hyphen is treated as a word separator (space),
    not simply deleted — both matter for project names like "Tira -
    Ahaan's Film", which must tokenize as {tira, ahaan, film}, not
    {tira, ahaans, film} or a hyphen-glued mess."""
    s = (s or "").strip().lower()
    s = _POSSESSIVE_RE.sub("", s)
    s = s.replace("-", " ")
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
        # Local sub-stage timing (2026-08-05 latency sprint) — see
        # ResolvedTalents.timing's docstring for why this stays a plain
        # dict of time.monotonic() deltas instead of using request_scope.
        timing: Dict[str, float] = {"candidate_count": float(len(candidates))}

        def _finish(result: ResolvedTalents) -> ResolvedTalents:
            result.timing = timing
            return result

        t0 = time.monotonic()
        raw_q = selector.name_query.strip()
        timing["normalize"] = round((time.monotonic() - t0) * 1000, 3)
        if not raw_q:
            return _finish(ResolvedTalents(ok=False, error="I didn't catch who to move."))

        # Tier 1+2: exact, case-sensitive then case-insensitive — the
        # single highest-confidence signal, tried before anything
        # normalises casing away.
        t0 = time.monotonic()
        result = _pick_unique_or_ambiguous(
            [c for c in candidates if c.label.strip() == raw_q], raw_q
        )
        if result is None:
            q_lower = raw_q.lower()
            result = _pick_unique_or_ambiguous(
                [c for c in candidates if c.label.strip().lower() == q_lower], raw_q
            )
        timing["exact_match"] = round((time.monotonic() - t0) * 1000, 3)
        if result is not None:
            return _finish(result)

        # Tier 3: normalized substring (handles a short first name like
        # "Sneh" or "Aahana" against a full "First Last" label, and
        # absorbs punctuation/whitespace noise from a voice transcript).
        t0 = time.monotonic()
        q_norm = _normalize_name(raw_q)
        result = _pick_unique_or_ambiguous(
            [c for c in candidates if q_norm and q_norm in _normalize_name(c.label)], raw_q
        )
        timing["token_match"] = round((time.monotonic() - t0) * 1000, 3)
        if result is not None:
            return _finish(result)

        # Tier 4: fuzzy — whole-string or best-token similarity (see
        # _name_similarity for why both are tried). Auto-resolves ONLY
        # when the top candidate is both confident (>= autocorrect cutoff)
        # AND clearly ahead of the next-best one; two candidates scoring
        # within the ambiguity margin of each other are never silently
        # collapsed into a guess, regardless of how high either scores.
        t0 = time.monotonic()
        scored = sorted(
            ((c, _name_similarity(raw_q, c.label)) for c in candidates),
            key=lambda pair: pair[1], reverse=True,
        )
        timing["fuzzy_scoring"] = round((time.monotonic() - t0) * 1000, 3)
        t0 = time.monotonic()
        scored = [(c, s) for c, s in scored if s >= _NAME_FUZZY_CUTOFF]
        if not scored:
            timing["ranking"] = round((time.monotonic() - t0) * 1000, 3)
            return _finish(ResolvedTalents(ok=False, error="No matching talent."))

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
            timing["ranking"] = round((time.monotonic() - t0) * 1000, 3)
            return _finish(ResolvedTalents(
                ok=True, talent_ids=[top_c.id], talent_labels=[top_c.label],
                resolved_project_id=top_c.project_id, resolved_project_label=top_c.project_label,
            ))
        second_s = scored[1][1]
        if top_s >= _NAME_AUTOCORRECT_CUTOFF and (top_s - second_s) > _NAME_AMBIGUITY_MARGIN:
            timing["ranking"] = round((time.monotonic() - t0) * 1000, 3)
            return _finish(ResolvedTalents(
                ok=True, talent_ids=[top_c.id], talent_labels=[top_c.label],
                resolved_project_id=top_c.project_id, resolved_project_label=top_c.project_label,
            ))
        close_enough = [c for c, s in scored if (top_s - s) <= _NAME_AMBIGUITY_MARGIN][:8]
        timing["ranking"] = round((time.monotonic() - t0) * 1000, 3)
        return _finish(ResolvedTalents(
            ok=False,
            error=_format_ambiguous_matches(close_enough, raw_q),
            ambiguous_candidates=close_enough,
        ))

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
# Multi-action / compact-format preprocessing — shared by extract_move_fields
# and extract_add_fields, applied BEFORE either does its own single-action
# parsing. Turns slash-delimited and "and confirm"-suffixed input into the
# same normalized shape a single clean natural-language command already
# produces, and splits a multi-action message into one raw-text chunk per
# independent/chained action. A single-action message is unaffected: it
# comes back out as a 1-item chunk list with nothing stripped except the
# (rare) "and confirm"/trailing "Confirm" marker.
# ---------------------------------------------------------------------------
_AND_CONFIRM_RE = re.compile(r"\s+and\s+confirm\.?\s*$", re.IGNORECASE)
_CONFIRM_LINE_RE = re.compile(r"^\s*confirm\.?\s*$", re.IGNORECASE)


def normalize_compact_text(text: str) -> str:
    """Slash-delimited compact commands ("add / ahana / toyota") are
    treated exactly like the newline-delimited compact form — normalize
    once, up front, so every downstream parser (single-action extraction,
    multi-action splitting, "and confirm" recognition) is slash-aware for
    free. "/" is never a plausible fragment of a talent/project name in
    this system's existing conventions (same assumption already made for
    "and"/"talent" as noise words)."""
    return (text or "").replace("/", "\n")


def strip_and_confirm(text: str) -> "Tuple[str, bool]":
    """Recognizes the "and confirm" shortcut (agents/models.py's
    IntentDefinition.try_auto_execute — see casting_pipeline.py) — either
    trailing inline ("... and confirm") or as its own trailing line in
    the compact/line-based format ("...\\nConfirm"), one recognition
    point for both. Returns (text_with_the_modifier_removed, auto_confirm)."""
    working = text or ""
    m = _AND_CONFIRM_RE.search(working)
    if m:
        return working[:m.start()], True
    lines = working.split("\n")
    if lines and _CONFIRM_LINE_RE.match(lines[-1]):
        return "\n".join(lines[:-1]), True
    return working, False


def _starts_with_any_trigger(line: str, triggers: List[str]) -> bool:
    low = line.strip().lower()
    return any(
        low == t or low.startswith(t + " ") or low.startswith(t + ":")
        for t in triggers
    )


def classify_chunk_intent(chunk_text: str) -> Optional[str]:
    """Which intent a split_actions chunk belongs to, based on which
    trigger vocabulary its leading verb matches — "casting.move" or
    "casting.add", or None if it starts with neither (defensive; not
    reachable via split_actions's own chunking, which only ever starts a
    new chunk at a recognized trigger)."""
    first_line = (chunk_text or "").strip().split("\n", 1)[0]
    if _starts_with_any_trigger(first_line, [t.lower() for t in ADD_TRIGGERS]):
        return "casting.add"
    if _starts_with_any_trigger(first_line, [t.lower() for t in MOVE_TRIGGERS]):
        return "casting.move"
    return None


def split_actions(text: str) -> List[str]:
    """Splits a (possibly multi-action) message into one raw-text chunk
    per independent/chained action — a single-action message returns
    [text] unchanged, so this is strictly additive on top of the existing
    single-action extraction path, never a replacement for it. Two rules:

      1. Newline-separated segments where a LATER line independently
         starts with a recognized MOVE/ADD trigger word ("Move 2,4,6 to
         Approved in Toyota Glanza\\n\\nMove 10,11 to Hold in Nykaa") ->
         independent chunks. A blank line always forces a new chunk too.
         The compact one-name-per-line form ("Add\\nName\\nProject") never
         repeats a trigger word on a later line, so it always stays one
         chunk under this rule.
      2. If rule 1 found only one chunk, " and <verb>" chaining within
         that single chunk ("Add X to Y and move to Approved") -> a
         chained chunk. The new chunk's own talent selector is left
         implicit (whatever's left after its own verb) — the existing
         PRONOUN_LAST_MARKER/session.last_talent_id fallback already
         resolves "whoever we just discussed" for exactly this shape, so
         no new resolution logic is needed for the implicit continuation.
    """
    all_triggers = sorted({t.lower() for t in (MOVE_TRIGGERS + ADD_TRIGGERS)}, key=len, reverse=True)
    lines = (text or "").split("\n")

    chunks: List[List[str]] = [[]]
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if chunks[-1]:
                chunks.append([])
            continue
        if chunks[-1] and _starts_with_any_trigger(line, all_triggers):
            chunks.append([line])
        else:
            chunks[-1].append(line)
    chunks = [c for c in chunks if c]

    if len(chunks) > 1:
        return ["\n".join(c) for c in chunks]

    whole = chunks[0][0] if chunks and len(chunks[0]) == 1 else (text or "").strip()
    and_chained = _split_and_chained(whole, all_triggers)
    return and_chained if and_chained else [text or ""]


def _split_and_chained(text: str, all_triggers: List[str]) -> List[str]:
    pattern = re.compile(
        r"\s+and\s+(?=(?:" + "|".join(re.escape(t) for t in all_triggers) + r")\b)",
        re.IGNORECASE,
    )
    parts = [p.strip() for p in pattern.split(text) if p.strip()]
    return parts


def preprocess_command(text: str) -> "Tuple[List[str], bool]":
    """The one entry point extract_move_fields/extract_add_fields both
    call first (via casting_pipeline.py's shared wrapper): normalizes
    slashes to newlines, recognizes+strips the "and confirm" shortcut,
    then splits into one or more action chunks. Returns
    (chunks, auto_confirm)."""
    normalized = normalize_compact_text(text)
    stripped, auto_confirm = strip_and_confirm(normalized)
    chunks = split_actions(stripped)
    return chunks, auto_confirm


def split_multi_names(text: str) -> List[str]:
    """Splits an "X, Y and Z" list into ["X", "Y", "Z"] — the same comma/
    "and"-separated grammar parse_talent_selector's name_queries branch
    uses for talent names, applied here to PROJECT references too ("to
    Toyota Glanza and Nykaa") so a chunk naming multiple talents AND
    multiple projects can be cross-product-expanded in casting_pipeline.py.
    A single name returns a 1-item list unchanged."""
    cleaned = _AND_SEP_RE.sub(",", text or "")
    return [p.strip() for p in cleaned.split(",") if p.strip()]


# Bulk multi-mapping ("Add A and B to Project A, C and D to Project B, E to
# Project C and D") — every comma-separated SEGMENT names its own
# project(s), unlike a plain multi-name list sharing one trailing project
# ("Add A, B, C to Project A", where only the LAST segment carries a
# connector). Requiring every segment to carry its own to/in/for is what
# tells the two shapes apart without any new fuzzy matching — it's a
# structural check on the raw text, done before name/project resolution
# ever runs.
_SEGMENT_CONNECTOR_RE = re.compile(r"\b(?:to|in|for)\b", re.IGNORECASE)


def split_multi_segment_pairs(chunk: str, triggers: List[str]) -> Optional[List[str]]:
    """Splits ONE Add/Move raw-text chunk into one independent raw-text
    segment per talent-group -> project-group mapping, when 2+ comma-
    separated segments are present and EVERY one of them carries its own
    to/in/for connector (each names its own project). Returns None
    (unchanged) for the ordinary shapes — a single segment, or a plain
    comma/"and"-separated name list sharing one trailing project — so
    callers fall back to their existing single-project-or-cross-product
    handling untouched.

    Each returned segment is a complete, independently parseable chunk:
    the first segment keeps whatever trigger word the original chunk
    started with; every later segment gets that SAME trigger word
    reattached (not a generic "add"/"move" default) so verb-implied-stage
    triggers ("Approve A in X, B in Y") keep working per segment too."""
    verb, _ = _strip_leading_trigger(chunk, triggers)
    if verb is None:
        return None
    segments = [s.strip() for s in (chunk or "").split(",") if s.strip()]
    if len(segments) < 2:
        return None
    if not all(_SEGMENT_CONNECTOR_RE.search(s) for s in segments):
        return None
    return [seg if i == 0 else f"{verb} {seg}" for i, seg in enumerate(segments)]


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

    A NO-CONNECTOR positional compact form is also recognized — "Move\\n
    NAME\\nSTAGE\\nPROJECT" — tried first, only when the whole message has
    no "to"/"into"/"in"/"for" connector word anywhere and enough lines to
    be positionally unambiguous. A real natural-language command almost
    always has a connector; this exists purely for the compact operator
    shorthand where LINES are the grammar.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    has_connector = bool(re.search(r"\b(?:to|into|in|for)\b", " ".join(lines), re.IGNORECASE))
    if len(lines) >= 3 and not has_connector:
        verb0, first_remainder = _strip_leading_trigger(lines[0], MOVE_TRIGGERS)
        if verb0 is not None:
            positional = ([first_remainder] if first_remainder else []) + lines[1:]
            for i in range(1, len(positional)):
                stage_match = match_stage_phrase(positional[i], stage_order)
                if stage_match.key:
                    talent_part = ", ".join(positional[:i]).strip(" ,") or PRONOUN_LAST_MARKER
                    project_part = ", ".join(positional[i + 1:]).strip(" ,")
                    positional_out: Dict[str, str] = {
                        "talent_selector": talent_part, "target_stage": stage_match.key,
                    }
                    if project_part:
                        positional_out["project_query"] = project_part
                    return positional_out

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

    # Hotfix (2026-08-05, project-matching-only): tier 1 below used to take
    # WHATEVER follows "to"/"into" as target_stage unconditionally — so
    # "Move Ahana Pocha to Ahaan Film" (an implied-stage command naming
    # only the PROJECT) misfired into target_stage="Ahaan Film", which
    # then failed stage validation and asked "which pipeline is 'Ahaan
    # Film'?" instead of ever treating it as a project reference. Now the
    # tier-1 capture is validated against the real stage vocabulary before
    # being committed; on a miss it's held aside (not discarded) so tiers
    # 2/3 still get their normal chance to find a real stage elsewhere in
    # the remainder, and only if THEY also find nothing does the held
    # phrase get reinterpreted as project_query.
    unvalidated_stage_candidate: Optional["tuple[str, str]"] = None
    m = _TO_STAGE_RE.match(remainder)
    if m:
        selector_text = m.group(1).strip(" ,")
        stage_text = m.group(2).strip(" .!?,")
        stage_match = match_stage_phrase(stage_text, stage_order)
        if stage_match.key:
            out["talent_selector"] = _selector_or_pronoun(selector_text)
            out["target_stage"] = stage_match.key
            return out
        unvalidated_stage_candidate = (selector_text, stage_text)

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

    if unvalidated_stage_candidate:
        selector_text, stage_text = unvalidated_stage_candidate
        if "project_query" not in out:
            # No project was named anywhere else in the sentence — X is far
            # more likely the intended PROJECT (implied-stage phrasing)
            # than a mistyped stage name, so reinterpret it as such.
            out["talent_selector"] = _selector_or_pronoun(selector_text)
            out["project_query"] = stage_text
            return out
        # A project WAS already named explicitly (via "... in <project>"),
        # so X is unambiguously meant to be the stage — preserve the
        # original behaviour of passing it through as-is, so an invalid
        # value still surfaces the specific "I couldn't find a pipeline
        # named X" validation error downstream, not a vague generic
        # question that discards what the user actually typed.
        out["talent_selector"] = _selector_or_pronoun(selector_text)
        out["target_stage"] = stage_text
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
ADD_TRIGGERS = ["add", "attach"]  # "attach" added for Phase 2's "Attach selected to X" synonym.

_ADD_TRIGGER_RE = re.compile(r"^\s*(?:add|attach)\b[\s:]*", re.IGNORECASE)
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
    elif len(lines) >= 2:
        # No-connector positional compact form: "Add\nNAME\nPROJECT" — the
        # LAST line is the project, everything before it is the name(s).
        # Only tried when there's no "to"/"in"/"for" connector at all
        # (checked above) and more than one data line, so a genuine
        # single-name-no-project message ("Add Prajal Tushir") is
        # untouched — still correctly asks "which project?".
        out["project_query"] = lines[-1]
        remainder = ", ".join(lines[:-1])

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
    # Conversational Casting Insights (2026-08-09) — talent-centric and
    # stage-specific natural-language questions open with one of these
    # instead of an imperative verb ("Which projects is Ahana working on?",
    # "Is Ahana testing for Toyota Glanza?", "Has Ahana been approved for
    # Dove?"). Purely additive: none of these collide with MOVE_TRIGGERS,
    # ADD_TRIGGERS, or UNDO's triggers, so no other intent's routing changes.
    "which", "is", "are", "was", "were", "has", "have", "did", "does",
    # Bare stage-first phrasing with no leading verb at all ("Follow Up
    # pipeline", "Testing list") needs the stage word itself recognized as
    # a trigger — detect_trigger only ever looks at the FIRST word(s) of
    # the message. Deliberately NOT every stage synonym: only the ones
    # that cannot collide with MOVE_TRIGGERS/ADD_TRIGGERS/UNDO's own
    # triggers (checked against agents/modules/casting_pipeline.py's
    # MOVE_TRIGGERS below) — "hold"/"lock"/"approve"/"reject"/"shortlist"/
    # "not available"/"not interested" are already MOVE triggers, and
    # reusing any of those here would silently misroute an existing,
    # tested bare-stage MOVE command ("Hold Sarah") into casting.query
    # instead (detect_trigger picks whichever equal-length trigger it
    # finds first when two intents both match) — see
    # test_stage_first_commands. "testing"/"follow up" have no such
    # collision.
    "testing", "follow up", "followup",
    # Conversational Talent Search (Phase 1, 2026-08-10) — "find"/"search"
    # cover natural variants of the roster-search examples that don't
    # start with "show" ("Find actors in Mumbai"). Checked against
    # MOVE_TRIGGERS/ADD_TRIGGERS/undo's triggers: no collision. Deliberately
    # NOT adding bare nouns ("girls", "models", "available") as triggers —
    # detect_trigger only inspects the message's leading words, and those
    # are too generic (risk of misrouting ordinary group chatter that
    # happens to start with one of them).
    "find", "search",
    # Talent Selection & Add to Project (Phase 2, 2026-08-10) — "remove"/
    # "clear"/"unselect"/"deselect" have zero collisions against
    # MOVE_TRIGGERS/ADD_TRIGGERS/undo's triggers (confirmed by grep before
    # adding). "select" itself is deliberately NOT added here — it's
    # already a MOVE_TRIGGERS entry ("Select Priya to Approved" is a real
    # stage-move); a Phase-2 "Select 1,3,5" reaches casting.query's
    # selection handling via a different path — see
    # casting_pipeline.py's _extract_move_fields pre-check and
    # SELECTION_CMD_MARKER above.
    "remove", "clear", "unselect", "deselect",
    # UX polish (2026-08-10) — bare "Selected"/"Selection"/"My selection"
    # must show the basket without requiring a "Show" prefix. Confirmed
    # (empirically, via agents.parser.detect_trigger) these do NOT collide
    # with MOVE_TRIGGERS' "select" entry: detect_trigger requires an exact
    # word match ("select" alone, "select ", "select:", or "select" glued
    # to a digit) — "selected"/"selection" are different tokens entirely
    # and never satisfy any of those conditions against the "select"
    # trigger. "who" (for "Who have I selected") and "current" (for
    # "Current selection") are ALREADY triggers — no new word needed there.
    "selected", "selection", "my selection",
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
    kind: str  # "list_projects" | "project_detail" | "pipeline" | "talent_projects" | "talent_stage_query" | "talent_search" | "talent_search_page" | "replay" | "unrecognized"
    project_ordinal: Optional[int] = None
    project_name_query: Optional[str] = None
    stage_key: Optional[str] = None
    stage_ambiguous: Optional[List[str]] = None
    count_only: bool = False
    # Only set for "talent_projects"/"talent_stage_query" — the raw talent
    # name span extracted from the message (see _extract_talent_stage_query).
    talent_query: Optional[str] = None
    # Only set for "talent_search" — a partial filter dict using the SAME
    # key names as routers.talents._build_talent_query's own params
    # (gender, location, age_min/age_max, height_min/height_max,
    # interested_in), so casting_pipeline.py's executor can pass it
    # straight through with no parallel filter vocabulary.
    search_filters: Optional[Dict[str, Any]] = None
    # Relative terms with no resolvable number ("tall", "young") — must
    # clarify, never guess a default threshold (see classify_query's talent
    # search section below).
    search_vague_terms: Optional[List[str]] = None
    # Criteria with no backing Talent-schema field at all ("language",
    # "availability") — must clarify, never silently drop or guess.
    search_unsupported: Optional[List[str]] = None
    # Only set for "talent_search_page" — "next" | "previous" | "all".
    search_page_action: Optional[str] = None


# ---------------------------------------------------------------------------
# Talent-centric query detection (Conversational Casting Insights, 2026-08-09)
# — "Show Ahana's projects" / "Is Ahana approved for Dove?" style questions.
# Hand-curated phrasing patterns, same rule-based approach as every other
# matcher in this module (see module docstring) — NOT a new stage/name
# synonym table: stage words still resolve through match_stage_phrase /
# extract_stage_phrase (the one shared registry), and talent/project
# IDENTITY resolution (fuzzy matching, disambiguation) still happens
# entirely in resolve_against_candidates / resolve_project_by_name over
# in casting_pipeline.py. This layer only decides WHICH SPAN of the
# sentence is the talent name/stage/project reference.
# ---------------------------------------------------------------------------
# Alphanumeric (not letters-only) — a real name/slug can contain a digit
# (test/seed data commonly does, "Ahaan 2" is a plausible display-name
# suffix too), and a letters-only span would simply never match those,
# falling through to a worse (or wrong) classification instead.
_NAME_SPAN = r"([A-Za-z0-9][A-Za-z0-9.'’\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9.'’\-]*)*)"

# Multi-talent variant of _NAME_SPAN — same shape, plus a tolerated comma so
# "Ahana, Priya and Zara" (already normalized to a comma-list by the time
# any consumer downstream calls parse_talent_selector/split_multi_names on
# it) can be captured as ONE span here, instead of failing the whole
# pattern match at the first comma. Scoped to _TALENT_PROJECTS_PATTERNS
# only (not the single-talent boolean/stage patterns below) — a real
# person's name essentially never contains a comma, so this is purely
# additive for the "multiple talents in one query" shape.
_MULTI_NAME_SPAN = r"([A-Za-z0-9][A-Za-z0-9.,'’\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9.,'’\-]*)*)"

_TALENT_PROJECTS_PATTERNS = [
    # "Show me all pending projects of Ahana Pocha" / "Show active projects
    # of Ahana" / "Show pending projects for Ahana, Priya and Zara".
    re.compile(
        r"^\s*show\s+(?:me\s+)?(?:all\s+)?(?:the\s+)?(?:pending|active|ongoing|current)?\s*projects?\s+(?:of|for)\s+"
        + _MULTI_NAME_SPAN + r"\s*[.?!]*\s*$",
        re.IGNORECASE,
    ),
    # "Show Ahana's ongoing projects" / "Show Ahana Pocha's active projects"
    re.compile(
        r"^\s*show\s+" + _MULTI_NAME_SPAN + r"['’]s\s+(?:ongoing|active|current|pending)\s+projects?\s*[.?!]*\s*$",
        re.IGNORECASE,
    ),
    # "What projects is Ahana Pocha part of?" / "Which projects is Ahana
    # working on?" / "Which projects is Ahana testing for?" (the last one
    # implies a stage — classify_query re-checks the whole matched text for
    # an embedded stage phrase separately, below). "is/are" already agrees
    # with a plural multi-name span too.
    re.compile(
        r"^\s*(?:what|which)\s+(?:casting\s+)?projects?\s+(?:is|are)\s+" + _MULTI_NAME_SPAN
        + r"\s+(?:part\s+of|working\s+on|involved\s+in|testing\s+for)\s*[.?!]*\s*$",
        re.IGNORECASE,
    ),
    # "Which casting projects involve Ahana?"
    re.compile(
        r"^\s*(?:what|which)\s+(?:casting\s+)?projects?\s+involve\s+" + _MULTI_NAME_SPAN + r"\s*[.?!]*\s*$",
        re.IGNORECASE,
    ),
]

# "What tests are pending for Ahana?" — a stage word up front ("tests"),
# talent named at the end via "for <name>".
_STAGE_PENDING_FOR_TALENT_RE = re.compile(
    r"^\s*what\s+(.+?)\s+(?:are|is)\s+pending\s+for\s+" + _NAME_SPAN + r"\s*[.?!]*\s*$",
    re.IGNORECASE,
)

# Boolean/status lead-in — "Is/Are/Was/Were/Has/Have/Did/Does <name> ...".
_TALENT_STAGE_LEAD_RE = re.compile(
    r"^\s*(?:is|are|was|were|has|have|did|does)\s+(.+?)\s*[.?!]*\s*$", re.IGNORECASE
)
_TRAILING_FOR_PROJECT_RE = re.compile(r"\bfor\s+(.+?)\s*$", re.IGNORECASE)
# Connector words a talent-stage question can leave behind once the stage
# phrase and project reference are removed ("Ahana been", "Ahana get",
# "Ahana in") — none of these is ever plausibly part of a real person's
# name in this system, so (like _NOISE_WORD_RE's "talent(s)" removal
# above) they're stripped as whole words wherever they land, not just at
# the edges.
_TALENT_LEFTOVER_FILLER_RE = re.compile(r"\b(?:been|get|got|in|the)\b", re.IGNORECASE)


def _clean_talent_leftover(s: str) -> str:
    s = _TALENT_LEFTOVER_FILLER_RE.sub(" ", s or "")
    return re.sub(r"\s+", " ", s).strip(" ?.!,")


def _extract_talent_stage_query(stripped: str, stage_order: List[str]) -> Optional[QueryIntent]:
    """Recognizes the two "stage-specific question" shapes and the
    "talent's projects" shape from the Conversational Casting Insights
    sprint. Returns None (never a partial/garbage classification) unless a
    plausible talent name AND at least a stage or project signal were both
    found — an unrelated "Is/Are/Was" message a group member sends for some
    other reason falls through to whatever classify_query's existing logic
    (or "unrecognized") would have done anyway, rather than triggering a
    pointless talent lookup."""
    m = _STAGE_PENDING_FOR_TALENT_RE.match(stripped)
    if m:
        stage_phrase, name = m.group(1), m.group(2)
        stage_match = match_stage_phrase_for_query(stage_phrase, stage_order)
        name = _clean_talent_leftover(name)
        if name and (stage_match.key or stage_match.ambiguous):
            return QueryIntent(
                kind="talent_stage_query", talent_query=name,
                stage_key=stage_match.key, stage_ambiguous=stage_match.ambiguous,
            )

    for pattern in _TALENT_PROJECTS_PATTERNS:
        m = pattern.match(stripped)
        if not m:
            continue
        name = _clean_talent_leftover(m.group(1))
        if not name:
            continue
        stage_key, ambiguous, _rest = extract_stage_phrase_for_query(stripped, stage_order)
        if stage_key or ambiguous:
            return QueryIntent(
                kind="talent_stage_query", talent_query=name,
                stage_key=stage_key, stage_ambiguous=ambiguous,
            )
        return QueryIntent(kind="talent_projects", talent_query=name)

    m = _TALENT_STAGE_LEAD_RE.match(stripped)
    if m:
        rest = m.group(1)
        project_name_query = None
        pm = _TRAILING_FOR_PROJECT_RE.search(rest)
        if pm:
            candidate = pm.group(1).strip(" ?.!")
            if candidate:
                project_name_query = candidate
                rest = rest[:pm.start()].strip()
        stage_key, ambiguous, rest_after_stage = extract_stage_phrase_for_query(rest, stage_order)
        name = _clean_talent_leftover(rest_after_stage)
        if name and (stage_key or ambiguous or project_name_query):
            return QueryIntent(
                kind="talent_stage_query", talent_query=name,
                stage_key=stage_key, stage_ambiguous=ambiguous,
                project_name_query=project_name_query,
            )

    return None


# Fallback for the connector-less "<Project> <Stage>" phrasing ("Show
# Toyota Follow Up", "Toyota Follow Up list") — every known trigger/noise
# word stripped from what's left after the stage phrase itself is removed;
# if 1-4 words of real content remain, that's treated as an implicit
# project reference. Deliberately errs conservative (a broad noise list
# covering every existing QUERY_TRIGGERS word plus the interrogative words
# a stage-only question is built from — "how many talents in hold?" must
# reduce to nothing) since a false positive here would misroute an
# existing, already-working stage-only query into an unwanted project
# lookup.
_POSITIONAL_PROJECT_NOISE_WORDS = {
    "show", "open", "list", "pipeline", "stage", "view", "current", "who",
    "what", "which", "is", "are", "the", "a", "an", "again", "repeat",
    "how", "many", "talents", "talent", "in", "for", "of", "count",
    "summary", "project", "projects", "p",
}


def _extract_positional_project(rest: str) -> Optional[str]:
    # Alphanumeric tokens (not letters-only) — a real project name can
    # contain digits ("Google - Film 1 & 3"), and a letters-only pattern
    # would shred something like "Glanza2026" or a hex-suffixed test/slug
    # name into single-character fragments, blowing past the word cap
    # below for no reason.
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", rest or "")
    content = [w for w in words if w.lower() not in _POSITIONAL_PROJECT_NOISE_WORDS]
    if 1 <= len(content) <= 6:
        return " ".join(content)
    return None


# ---------------------------------------------------------------------------
# Verb-less pipeline queries (UX polish, 2026-08-09) — "Toyota Follow Up",
# "Toyota Selected", with no leading "Show"/trigger word at all. Every
# intent on this platform requires the message to START WITH a recognized
# trigger (agents/parser.detect_trigger) — this can never change that
# (out of scope, shared by every agent) — but casting-agent's own
# `resolve_bare_reply` hook (see casting_pipeline.py) already gets one
# last look at a message that matched no trigger and has no active
# conversation, and can claim it. This function is the PURE, DB-free half
# of that decision: does the message even LOOK like a "<Project> <Stage>"
# reference at all? It reuses extract_stage_phrase_for_query (which
# already covers "Selected" per the section above, plus everything
# extract_stage_phrase itself understands) and the exact same
# _extract_positional_project heuristic classify_query's own "pipeline"
# kind already relies on — no new stage/project matching logic.
# resolve_bare_reply then does the other, DB-aware half itself (verifying
# the candidate against the REAL live project list via
# resolve_project_by_name, the one shared project matcher) before
# claiming the message — this function alone is deliberately not enough
# to claim it, since without that verification step, ordinary group
# chatter that happens to contain a real stage word ("put him on hold for
# now") would otherwise get silently intercepted.
# ---------------------------------------------------------------------------
def extract_bare_pipeline_candidate(
    text: str, stage_order: List[str]
) -> Optional["Tuple[Optional[str], Optional[List[str]], str]"]:
    """Returns (stage_key, stage_ambiguous, project_name_candidate) when the
    text looks like a verb-less "<Project> <Stage>" reference, else None."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    stage_key, ambiguous, rest = extract_stage_phrase_for_query(stripped, stage_order)
    if not (stage_key or ambiguous):
        return None
    project = _extract_positional_project(rest)
    if not project:
        return None
    return stage_key, ambiguous, project


def classify_query(text: str, stage_order: List[str]) -> QueryIntent:
    stripped = (text or "").strip()
    if _REPLAY_RE.match(stripped):
        return QueryIntent(kind="replay")

    talent_intent = _extract_talent_stage_query(stripped, stage_order)
    if talent_intent is not None:
        return talent_intent

    project_ref = None
    m = _PROJECT_REF_RE.search(text)
    if m:
        project_ref = int(m.group(1))
        text = text[:m.start()] + " " + text[m.end():]

    stage_key, ambiguous, rest = extract_stage_phrase_for_query(text, stage_order)

    project_name_query = None
    name_m = _FOR_PROJECT_RE.search(rest)
    if name_m:
        candidate = name_m.group(1).strip(" ?.!\n")
        if candidate:
            project_name_query = candidate

    if (stage_key or ambiguous) and project_name_query is None and project_ref is None:
        # No "for"/"of <project>" connector — try the connector-less
        # "<Project> <Stage>" positional shape ("Show Toyota Follow Up",
        # "Toyota Follow Up list"). See _extract_positional_project's
        # docstring for why this is safe against existing stage-only
        # queries like "How many talents in hold?".
        project_name_query = _extract_positional_project(rest)

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

    page = extract_talent_search_pagination(stripped)
    if page is not None:
        return QueryIntent(kind="talent_search_page", search_page_action=page["action"])

    if _SEARCH_TRIGGER_RE.search(stripped):
        parsed = extract_talent_search_filters(stripped)
        return QueryIntent(
            kind="talent_search",
            search_filters=parsed["filters"] or None,
            search_vague_terms=parsed["vague_terms"] or None,
            search_unsupported=parsed["unsupported"] or None,
        )

    return QueryIntent(kind="unrecognized")


# ---------------------------------------------------------------------------
# Conversational Talent Search (Phase 1, 2026-08-10) — roster search by
# gender/category/city/age/height. Entirely separate from the talent-centric
# helpers above (those answer questions about ONE NAMED talent's project
# history/stage; this searches the whole roster by criteria). Filter keys
# mirror routers.talents._build_talent_query's own parameter names exactly,
# so casting_pipeline.py's executor can pass the parsed dict straight into
# that shared query builder — no parallel filter vocabulary, no new
# filtering system, per the feature spec.
# ---------------------------------------------------------------------------

_GENDER_WORD_MAP: Dict[str, str] = {
    "female": "female", "females": "female", "woman": "female", "women": "female",
    "girl": "female", "girls": "female",
    "male": "male", "males": "male", "man": "male", "men": "male",
    "boy": "male", "boys": "male",
}
# non_binary/prefer_not_say deliberately have no NL synonyms here — no
# plausible casual phrasing exists for them (same "hand-curate only what
# people actually type" philosophy as _STAGE_SHORTHAND above).
_GENDER_RE = re.compile(
    r"\b(" + "|".join(sorted(_GENDER_WORD_MAP, key=len, reverse=True)) + r")\b", re.IGNORECASE
)

_CATEGORY_WORD_MAP: Dict[str, str] = {
    "actor": "Acting", "actors": "Acting", "actress": "Acting", "actresses": "Acting",
    "acting": "Acting",
    "model": "Modeling", "models": "Modeling", "modeling": "Modeling", "modelling": "Modeling",
    "influencer": "Influencer Campaigns", "influencers": "Influencer Campaigns",
    "creator": "Influencer Campaigns", "creators": "Influencer Campaigns",
}
_CATEGORY_RE = re.compile(
    r"\b(" + "|".join(sorted(_CATEGORY_WORD_MAP, key=len, reverse=True)) + r")\b", re.IGNORECASE
)

# A talent-search-shaped message must mention at least one of these roster
# nouns — gates classify_query's talent_search fallthrough so an unrelated
# unrecognized message never gets misclassified as a roster search just
# because it fell through every other check.
_SEARCH_TRIGGER_RE = re.compile(
    r"\b(talent|talents|actor|actors|actress|actresses|model|models|modeling|modelling|"
    r"influencer|influencers|female|females|male|males|woman|women|man|men|girl|girls|boy|boys)\b",
    re.IGNORECASE,
)

# City/country span after from/in/at — no canonicalization against a known
# location list, matching _build_talent_query's own plain-string $in match
# (routers/talents.py). Capped at 2 words (covers "Mumbai", "New Delhi")
# and stops before a short list of connector words a trailing clause might
# start with, so "from Mumbai who speak Gujarati" doesn't swallow "who
# speak" into the captured city.
_LOCATION_STOPWORDS = ("who", "that", "and", "speak", "speaks", "speaking", "available", "with", "having")
_LOCATION_RE = re.compile(
    r"\b(?:from|in|at)\s+((?:(?!\b(?:" + "|".join(_LOCATION_STOPWORDS) + r")\b)[A-Za-z]+\s*){1,2})",
    re.IGNORECASE,
)

# Height tokens — same free-text shapes core.parse_height_to_inches already
# understands ("5'10\"", "5 ft 10", "172cm"); only the SPAN is matched here,
# the actual inches conversion is always delegated to that one function.
_HEIGHT_TOKEN_RE = r"\d+\s*(?:'|’|ft|feet)\s*\d*\"?|\d+\s*cm\b"
_HEIGHT_BETWEEN_RE = re.compile(
    r"\bbetween\s+(" + _HEIGHT_TOKEN_RE + r")\s+and\s+(" + _HEIGHT_TOKEN_RE + r")", re.IGNORECASE
)
_HEIGHT_ABOVE_RE = re.compile(
    r"\b(?:above|over|taller than)\s+(" + _HEIGHT_TOKEN_RE + r")", re.IGNORECASE
)
_HEIGHT_BELOW_RE = re.compile(
    r"\b(?:below|under|shorter than)\s+(" + _HEIGHT_TOKEN_RE + r")", re.IGNORECASE
)

_AGE_BETWEEN_RE = re.compile(r"\bbetween\s+(\d{1,3})\s+and\s+(\d{1,3})\b", re.IGNORECASE)
_AGE_RANGE_DASH_RE = re.compile(r"\bage[sd]?\s+(\d{1,3})\s*(?:-|to)\s*(\d{1,3})\b", re.IGNORECASE)
_AGE_ABOVE_RE = re.compile(r"\b(?:above|over|older than)\s+(\d{1,3})\b", re.IGNORECASE)
_AGE_BELOW_RE = re.compile(r"\b(?:below|under|younger than)\s+(\d{1,3})\b", re.IGNORECASE)

# Vague relative terms with NO resolvable number anywhere in the message —
# "Show tall girls" must ask "What minimum height should I use?" (the
# spec's own example), never guess a default threshold. Maps each word to
# the search_filters key a clarification answer will fill in.
VAGUE_TERM_FIELD: Dict[str, str] = {
    "tall": "height_min", "taller": "height_min",
    "short": "height_max", "shorter": "height_max",
    "old": "age_min", "older": "age_min",
    "young": "age_max", "younger": "age_max",
}

VAGUE_CLARIFY_QUESTIONS: Dict[str, str] = {
    "height_min": "What minimum height should I use?",
    "height_max": "What maximum height should I use?",
    "age_min": "What minimum age should I use?",
    "age_max": "What maximum age should I use?",
}

# Criteria with NO backing Talent-schema field at all (languages/
# availability only exist on per-project Submissions, a different
# collection) — must clarify, never silently drop or guess.
_LANGUAGE_HINT_RE = re.compile(r"\b(speak|speaks|speaking|language|languages|fluent in)\b", re.IGNORECASE)
_AVAILABILITY_HINT_RE = re.compile(r"\b(availab\w*|free on|free from|open on|open for)\b", re.IGNORECASE)


def _extract_height_range(text: str) -> Tuple[Optional[float], Optional[float], str]:
    """(height_min, height_max, text_with_matched_span_removed) — removal
    keeps a later age/location pass from tripping over height's own digits
    ("between 5'6\" and 5'10\"" must never be read as an age range)."""
    m = _HEIGHT_BETWEEN_RE.search(text)
    if m:
        a, b = parse_height_to_inches(m.group(1)), parse_height_to_inches(m.group(2))
        if a is not None and b is not None and a > b:
            a, b = b, a
        return a, b, text[:m.start()] + " " + text[m.end():]

    height_min = height_max = None
    m = _HEIGHT_ABOVE_RE.search(text)
    if m:
        height_min = parse_height_to_inches(m.group(1))
        text = text[:m.start()] + " " + text[m.end():]
    m = _HEIGHT_BELOW_RE.search(text)
    if m:
        height_max = parse_height_to_inches(m.group(1))
        text = text[:m.start()] + " " + text[m.end():]
    return height_min, height_max, text


def _extract_age_range(text: str) -> Tuple[Optional[int], Optional[int], str]:
    """(age_min, age_max, text_with_matched_span_removed). Height's own
    extraction always runs FIRST and strips its span (see
    extract_talent_search_filters/extract_talent_search_refinement) so a
    bare "between 18 and 22" reaching here is unambiguously an age range,
    never a leftover height clause."""
    m = _AGE_BETWEEN_RE.search(text) or _AGE_RANGE_DASH_RE.search(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        lo, hi = (a, b) if a <= b else (b, a)
        return lo, hi, text[:m.start()] + " " + text[m.end():]

    age_min = age_max = None
    m = _AGE_ABOVE_RE.search(text)
    if m:
        age_min = int(m.group(1))
        text = text[:m.start()] + " " + text[m.end():]
    m = _AGE_BELOW_RE.search(text)
    if m:
        age_max = int(m.group(1))
        text = text[:m.start()] + " " + text[m.end():]
    return age_min, age_max, text


def extract_talent_search_filters(text: str) -> Dict[str, Any]:
    """Parses a free-text roster-search request (a fresh, trigger-anchored
    message — "Show female models from Mumbai") into
    {"filters": {...partial dict...}, "vague_terms": [...], "unsupported": [...]}.
    Never guesses: a vague relative term with no number, or a criterion
    with no backing schema field, is surfaced separately for the caller to
    turn into a clarification question — it is NEVER folded into `filters`."""
    original = text or ""
    working = original

    filters: Dict[str, Any] = {}

    height_min, height_max, working = _extract_height_range(working)
    if height_min is not None:
        filters["height_min"] = height_min
    if height_max is not None:
        filters["height_max"] = height_max

    age_min, age_max, working = _extract_age_range(working)
    if age_min is not None:
        filters["age_min"] = age_min
    if age_max is not None:
        filters["age_max"] = age_max

    categories: List[str] = []
    for m in _CATEGORY_RE.finditer(working):
        mapped = _CATEGORY_WORD_MAP[m.group(1).lower()]
        if mapped not in categories:
            categories.append(mapped)
    if categories:
        filters["interested_in"] = categories
    working = _CATEGORY_RE.sub(" ", working)

    gender_m = _GENDER_RE.search(working)
    if gender_m:
        filters["gender"] = _GENDER_WORD_MAP[gender_m.group(1).lower()]
    working = _GENDER_RE.sub(" ", working)

    loc_m = _LOCATION_RE.search(working)
    if loc_m:
        city = " ".join(loc_m.group(1).split()).strip()
        if city:
            filters["location"] = [city.title()]

    vague_terms: List[str] = []
    if height_min is None and height_max is None:
        for word in ("tall", "taller", "short", "shorter"):
            if re.search(r"\b" + word + r"\b", original, re.IGNORECASE):
                vague_terms.append(word)
                break
    if age_min is None and age_max is None:
        for word in ("old", "older", "young", "younger"):
            if re.search(r"\b" + word + r"\b", original, re.IGNORECASE):
                vague_terms.append(word)
                break

    unsupported: List[str] = []
    if _LANGUAGE_HINT_RE.search(original):
        unsupported.append("language")
    if _AVAILABILITY_HINT_RE.search(original):
        unsupported.append("availability")

    return {"filters": filters, "vague_terms": vague_terms, "unsupported": unsupported}


# Narrower grammar for an UNTRIGGERED follow-up ("Only Mumbai", "Above
# 5'7\"", "Age under 22") — casting_pipeline.py's _resolve_bare_reply only
# ever calls this when a talent_search is already active in session (the
# primary safety net against swallowing unrelated chatter); this function
# is the secondary one, requiring an explicit filter-shaped clause so a
# bare "ok thanks" returns None regardless of session state.
_REFINE_ONLY_RE = re.compile(r"^\s*(?:only|just)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\s*[.!?]*\s*$", re.IGNORECASE)
_REFINE_BARE_GENDER_RE = re.compile(r"^\s*(?:only\s+)?(female|male|women|men|girls|boys)\s*[.!?]*\s*$", re.IGNORECASE)


def extract_talent_search_refinement(text: str) -> Optional[Dict[str, Any]]:
    stripped = (text or "").strip()
    if not stripped:
        return None

    filters: Dict[str, Any] = {}
    height_min, height_max, remainder = _extract_height_range(stripped)
    if height_min is not None:
        filters["height_min"] = height_min
    if height_max is not None:
        filters["height_max"] = height_max

    age_min, age_max, remainder = _extract_age_range(remainder)
    if age_min is not None:
        filters["age_min"] = age_min
    if age_max is not None:
        filters["age_max"] = age_max
    if filters:
        return filters

    m = _REFINE_BARE_GENDER_RE.match(stripped)
    if m:
        return {"gender": _GENDER_WORD_MAP[m.group(1).lower()]}

    m = _REFINE_ONLY_RE.match(stripped)
    if m:
        candidate = m.group(1).strip()
        mapped_gender = _GENDER_WORD_MAP.get(candidate.lower())
        if mapped_gender:
            return {"gender": mapped_gender}
        return {"location": [candidate.title()]}

    return None


# Pagination phrasing — anchored to the WHOLE stripped message (never
# mid-sentence), so "all good" or "let's do this next week" can never be
# misread as a page-navigation command.
_PAGE_NEXT_RE = re.compile(r"^\s*(?:show\s+)?(?:next|more)(?:\s+\d+)?\s*[.!?]*\s*$", re.IGNORECASE)
_PAGE_PREV_RE = re.compile(r"^\s*(?:show\s+)?(?:previous|prev)(?:\s+\d+)?\s*[.!?]*\s*$", re.IGNORECASE)
_PAGE_ALL_RE = re.compile(r"^\s*(?:show\s+)?all\s*[.!?]*\s*$", re.IGNORECASE)


def extract_talent_search_pagination(text: str) -> Optional[Dict[str, Any]]:
    stripped = (text or "").strip()
    if not stripped:
        return None
    if _PAGE_NEXT_RE.match(stripped):
        return {"action": "next"}
    if _PAGE_PREV_RE.match(stripped):
        return {"action": "previous"}
    if _PAGE_ALL_RE.match(stripped):
        return {"action": "all"}
    return None


# ---------------------------------------------------------------------------
# Talent Selection & Add to Project (Phase 2, 2026-08-10) — "Select
# 1,3,5"/"Remove 2"/"Clear selection"/... against the CURRENT talent-search
# number_map. Pure grammar only — spec resolution against actual ordinals
# (and the "not part of the current search" validation) is
# resolve_selection_spec below; the session-aware basket mutation itself
# lives in casting_pipeline.py's _handle_selection_command.
# ---------------------------------------------------------------------------
_SELECT_SPEC = r"(all|first\s+\d+|last\s+\d+|[\d,\-\s]+)"
_SELECTION_SELECT_RE = re.compile(r"^\s*select\s+" + _SELECT_SPEC + r"\s*[.!?]*\s*$", re.IGNORECASE)
_SELECTION_REMOVE_RE = re.compile(
    r"^\s*(?:remove|unselect|deselect)\s+" + _SELECT_SPEC + r"\s*[.!?]*\s*$", re.IGNORECASE
)
_SELECTION_REMOVE_ALL_RE = re.compile(r"^\s*(?:remove|unselect|deselect)\s+all\s*[.!?]*\s*$", re.IGNORECASE)
_SELECTION_CLEAR_RE = re.compile(r"^\s*clear(?:\s+selection)?\s*[.!?]*\s*$", re.IGNORECASE)

# "Show selected" / "Selected" / "Selection" / "Selected talents" /
# "Current selection" / "My selection" / "Who have I selected" — matched
# ONLY as a candidate; the caller (casting_pipeline.py's _query_executor)
# additionally requires a non-empty basket before treating it as Phase 2's
# "show basket" rather than the pre-existing ambiguous Approved/Locked
# stage query these same phrases already resolve to
# (_QUERY_AMBIGUOUS_STAGE_WORDS) — see that module's docstring on the
# "select" vocabulary collision.
SHOW_SELECTED_RE = re.compile(
    r"^\s*(?:show\s+)?(?:who\s+have\s+i\s+selected|my\s+selection|the\s+selection|"
    r"current\s+selection|selection|selected(?:\s+talents)?)\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def extract_selection_command(text: str) -> Optional[Dict[str, Any]]:
    """Recognizes a selection-basket command shape and nothing else — a
    talent NAME ("Select Priya"), a real move ("Select Priya to Approved"),
    or unrelated chatter all return None. Deliberately narrow (same
    "never guess" philosophy as every other parser in this module)."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    if _SELECTION_CLEAR_RE.match(stripped):
        return {"action": "clear"}
    m = _SELECTION_SELECT_RE.match(stripped)
    if m:
        return {"action": "select", "spec": m.group(1).strip()}
    if _SELECTION_REMOVE_ALL_RE.match(stripped):
        return {"action": "remove", "spec": "all"}
    m = _SELECTION_REMOVE_RE.match(stripped)
    if m:
        return {"action": "remove", "spec": m.group(1).strip()}
    return None


_FIRST_N_RE = re.compile(r"^first\s+(\d+)$", re.IGNORECASE)
_LAST_N_RE = re.compile(r"^last\s+(\d+)$", re.IGNORECASE)


def resolve_selection_spec(
    spec: str, current_ordinals: List[int]
) -> Tuple[Optional[List[int]], Optional[str]]:
    """(ordinals, error) — resolves "1,3,5"/"2-6"/"first 5"/"last 3"/"all"
    against the CURRENT page's actual ordinals (never the total match
    count — the number_map only ever holds what's actually displayed,
    same as Phase 1's pagination). ANY out-of-range ordinal rejects the
    WHOLE spec (never a partial selection) — "never guess" applies here
    exactly as everywhere else in this module."""
    ordered = sorted(current_ordinals)
    if not ordered:
        return None, "No talents are currently shown to select from."

    spec = (spec or "").strip().lower()
    if spec == "all":
        return list(ordered), None

    m = _FIRST_N_RE.match(spec)
    if m:
        return ordered[: int(m.group(1))], None
    m = _LAST_N_RE.match(spec)
    if m:
        n = int(m.group(1))
        return ordered[-n:] if n > 0 else [], None

    selector = parse_talent_selector(spec)
    if not selector.ok or not selector.ordinals:
        return None, f'"{spec}" isn\'t a valid selection.'

    valid = set(ordered)
    for o in selector.ordinals:
        if o not in valid:
            return None, f"Talent {o} is not part of the current search."
    return sorted(selector.ordinals), None


# ---------------------------------------------------------------------------
# Hotfix (2026-08-05): project-matching-only forgiveness layer. Talent
# matching (_normalize_name, _name_similarity, _NAME_AUTOCORRECT_CUTOFF,
# _NAME_AMBIGUITY_MARGIN, _token_subset_matches) is untouched — every
# helper below is a SEPARATE, project-only function, used only inside
# resolve_project_by_name.
# ---------------------------------------------------------------------------
# Deliberately short and casting-context-specific, not a general stopword
# list: common words a project name may or may not include, which
# shouldn't cost a match either way once removed symmetrically from both
# the query and every candidate label before comparing.
_PROJECT_FILLER_WORDS = {"the", "a", "an", "project", "film", "movie", "show", "series", "shoot", "shoots"}

# Looser than talent matching's shared cutoff/margin (0.85 / 0.05) — a
# project reference is usually the ONLY thing being named in that part of
# a sentence (unlike a talent name, which sits among other words), so a
# solid fuzzy ratio is already a confident signal on its own, and the
# real production evidence motivating this hotfix showed unwanted "did you
# mean" prompts even at high top scores. Still conservative: 0.78 is well
# above the 0.6 "worth suggesting at all" floor, and a 0.03 margin still
# catches a genuine near-tie between two real candidates.
_PROJECT_AUTOCORRECT_CUTOFF = 0.78
_PROJECT_AMBIGUITY_MARGIN = 0.03


def _project_match_tokens(s: str) -> set:
    """Tokenizes a project name/query for forgiving comparison: same
    normalization _normalize_project_label already uses, plus filler-word
    removal and simple plural/singular folding (a trailing 's' on a token
    longer than 3 characters is dropped, e.g. "Films"/"Film" and the
    common typo pattern "Ahaans"/"Ahaan" both fold to the same token).
    Project-matching only — talent matching's own tokenization in
    _name_similarity is untouched."""
    out = set()
    for t in _normalize_project_label(s).split():
        if t in _PROJECT_FILLER_WORDS:
            continue
        if len(t) > 3 and t.endswith("s"):
            t = t[:-1]
        if t:
            out.add(t)
    return out


def _project_token_subset_matches(query: str, labels: List[str]) -> List[int]:
    """Project-only counterpart of _token_subset_matches, using
    _project_match_tokens' filler-word/plural forgiveness — e.g. "Ahaan
    Movie" now matches a label containing "Ahaan Film" (both "movie" and
    "film" are filler words here), and "Tira Films" matches a label
    containing singular "Film". A separate function (not a modification of
    _token_subset_matches) so _match_label_tiers's shared disambiguation-
    reply resolution — used by both talent and project continuation — is
    completely unaffected."""
    q_tokens = _project_match_tokens(query)
    if not q_tokens:
        return []
    return [i for i, l in enumerate(labels) if q_tokens <= _project_match_tokens(l)]


def _project_name_similarity(query: str, label: str) -> float:
    """Project-only fuzzy similarity, computed over filler-word/plural-
    normalized tokens (order-independent — token-subset above already
    handles reordering, so this tier's job is purely typo tolerance).
    Combined with (never replacing) the shared _name_similarity via max()
    at the call site, so this can only ever IMPROVE a project match
    relative to today's behaviour, never regress one."""
    q_tokens = sorted(_project_match_tokens(query))
    lab_tokens = sorted(_project_match_tokens(label))
    if not q_tokens or not lab_tokens:
        return 0.0
    whole = difflib.SequenceMatcher(None, " ".join(q_tokens), " ".join(lab_tokens)).ratio()
    best_token = max(
        (difflib.SequenceMatcher(None, qt, lt).ratio() for qt in q_tokens for lt in lab_tokens),
        default=0.0,
    )
    return max(whole, best_token)


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
    # Multiple real (exact/substring/token-subset) matches tied, OR (Tier 5)
    # multiple fuzzy candidates too close to call — each {"id", "label"},
    # not bare strings, so a caller can fetch per-candidate detail (status/
    # talent count/last updated) for a richer disambiguation card without a
    # second name-to-id lookup.
    ambiguous: Optional[List[Dict[str, str]]] = None
    # No real/tied match, but fuzzy found close candidates below the
    # auto-resolve bar — same {"id", "label"} shape as `ambiguous`.
    suggestions: Optional[List[Dict[str, str]]] = None
    error: Optional[str] = None


_PROJECT_NAME_FUZZY_CUTOFF = 0.6


def resolve_project_by_name(name_query: str, projects: List[Dict[str, str]]) -> ProjectNameMatch:
    q_raw = (name_query or "").strip()
    if not q_raw or not projects:
        return ProjectNameMatch(error=f'I couldn\'t find a project matching "{name_query}".')

    labels = [(p.get("label") or "") for p in projects]
    q_lower = q_raw.lower()

    def _as_dicts(idxs: List[int]) -> List[Dict[str, str]]:
        return [{"id": projects[i]["id"], "label": labels[i]} for i in idxs]

    # Tier 1: exact, case-insensitive.
    exact = [i for i, l in enumerate(labels) if l.strip().lower() == q_lower]
    if len(exact) == 1:
        return ProjectNameMatch(project=projects[exact[0]])
    if len(exact) > 1:
        return ProjectNameMatch(ambiguous=_as_dicts(exact[:8]))

    # Tier 2: normalized exact — case/punctuation/hyphen/whitespace-
    # insensitive ("Bajaj Pulsar Main Guy" == "Bajaj Pulsar - Main Guy").
    q_norm = _normalize_project_label(q_raw)
    normalized_labels = [_normalize_project_label(l) for l in labels]
    nexact = [i for i, l in enumerate(normalized_labels) if q_norm and l == q_norm]
    if len(nexact) == 1:
        return ProjectNameMatch(project=projects[nexact[0]])
    if len(nexact) > 1:
        return ProjectNameMatch(ambiguous=_as_dicts(nexact[:8]))

    # Tier 3: normalized substring (either direction).
    contains = [
        i for i, l in enumerate(normalized_labels) if q_norm and (q_norm in l or l in q_norm)
    ]
    if len(contains) == 1:
        return ProjectNameMatch(project=projects[contains[0]])
    if len(contains) > 1:
        return ProjectNameMatch(ambiguous=_as_dicts(contains[:8]))

    # Tier 4: order-independent token-subset — every normalized query token
    # present somewhere in the candidate's tokens, regardless of order or
    # what's interposed between them ("Pulsar Guy" / "Bajaj Main Guy" both
    # match "Bajaj Pulsar - Main Guy"). Precise (not edit-distance), so
    # it's safe to auto-resolve on when unique — same safety bar as the
    # substring tier above, just reorder-tolerant. Hotfix (2026-08-05):
    # uses the project-only forgiving tokenizer (filler words + plural
    # folding, e.g. "Tira Films" / "Ahaan Movie" both still match) instead
    # of the strict shared _token_subset_matches — this can only find MORE
    # unique matches than the strict version, never fewer.
    subset_idxs = _project_token_subset_matches(q_raw, labels)
    if len(subset_idxs) == 1:
        return ProjectNameMatch(project=projects[subset_idxs[0]])
    if len(subset_idxs) > 1:
        return ProjectNameMatch(ambiguous=_as_dicts(subset_idxs[:8]))

    # Tier 5: character-level fuzzy / partial token overlap. Started as a
    # mirror of talent matching's own fuzzy tier; hotfix (2026-08-05) adds
    # a project-only scoring layer (_project_name_similarity, filler-word/
    # plural-normalized) taken via max() alongside the original shared
    # _name_similarity — so a project's score is never LOWER than it was
    # before, only ever equal or higher — plus project-specific (not
    # talent-shared) auto-resolve thresholds: a project reference is
    # usually the only thing named in that part of a sentence, so a solid
    # fuzzy ratio is already a confident signal on its own, and real
    # production evidence showed the shared, stricter talent thresholds
    # triggering unwanted "did you mean" prompts even at high top scores.
    scored = sorted(
        (
            (i, max(_name_similarity(q_raw, labels[i]), _project_name_similarity(q_raw, labels[i])))
            for i in range(len(labels))
        ),
        key=lambda pair: pair[1], reverse=True,
    )
    scored = [(i, s) for i, s in scored if s >= _NAME_FUZZY_CUTOFF]
    if not scored:
        return ProjectNameMatch(error=f'I couldn\'t find a project matching "{name_query}".')

    top_i, top_s = scored[0]
    if len(scored) == 1:
        # Nothing else cleared even the base "worth suggesting" cutoff —
        # there's nothing for it to be ambiguous WITH (same reasoning as
        # talent matching's single-candidate case).
        return ProjectNameMatch(project=projects[top_i])
    second_s = scored[1][1]
    if top_s >= _PROJECT_AUTOCORRECT_CUTOFF and (top_s - second_s) > _PROJECT_AMBIGUITY_MARGIN:
        return ProjectNameMatch(project=projects[top_i])

    close_enough = [i for i, s in scored if (top_s - s) <= _PROJECT_AMBIGUITY_MARGIN][:8]
    return ProjectNameMatch(suggestions=_as_dicts(close_enough))
