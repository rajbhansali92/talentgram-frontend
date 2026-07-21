"""CRM Agent — natural-language understanding layer (2026-07-21).

Replaces position/line-based field extraction with entity recognition: the
message can be multi-line, single-line, or a natural-language sentence, in
any field order, and this module pulls out whichever CRM entities it can
find with reasonable confidence — never rejecting a message just because it
doesn't match one rigid template.

This is deliberately NOT the conversation engine, the confirmation flow, or
the transport — those are untouched. This module only replaces *how raw
text becomes a {field: value} dict*, via the IntentDefinition.extract_fields
/ parse_edits hooks (see agents/models.py). Everything downstream (missing-
field prompts, the confirm/edit/cancel loop, audit logging) is the same
generic engine as before.

Design: rule-based entity recognition, not statistical NLP. Each entity
type has a recognizer that scans whatever text remains after
higher-confidence entities have already claimed their spans, so "Raj Mehta,
Brand Manager, 9876543210" and "He's a Brand Manager, his number is
9876543210" both resolve the same way without one entity's match stealing
another's text.
"""
from __future__ import annotations

import difflib
import re
from typing import Dict, List, Optional, Tuple

from agents.models import FieldSpec

# ---------------------------------------------------------------------------
# Trigger stripping — mirrors parser.detect_trigger's trigger list but strips
# a leading trigger PHRASE (possibly multi-word, possibly with filler like
# "please" or "can you") from free text instead of requiring it to be the
# entire first line.
# ---------------------------------------------------------------------------
_TRIGGER_PREFIXES = [
    "please create a new contact",
    "can you save",
    "please save",
    "create a new contact",
    "create contact",
    "new contact",
    "save",
    "add",
]


def _strip_leading_trigger(text: str) -> str:
    working = text.strip()
    lowered = working.lower()
    for trig in _TRIGGER_PREFIXES:
        if lowered.startswith(trig):
            rest = working[len(trig):]
            if not rest or rest[0] in " \n:.,-":
                return rest.lstrip(" \n:.,-")
    return working


# ---------------------------------------------------------------------------
# Regex-based entity recognizers — highest confidence, extracted first so
# their matched spans never get mis-claimed by a lower-confidence recognizer.
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")
_INSTAGRAM_RE = re.compile(
    r"(?:instagram\.com/|(?<!\w)@)([A-Za-z0-9_.]{2,30})\b"
)
_PHONE_SCAN_RE = re.compile(r"(\+?\d[\d\s\-]{6,17}\d)")
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]+"
)


def _remove_span(text: str, start: int, end: int) -> str:
    return text[:start] + " " + text[end:]


def _extract_email(text: str) -> Tuple[Optional[str], str]:
    m = _EMAIL_RE.search(text)
    if not m:
        return None, text
    return m.group(0), _remove_span(text, m.start(), m.end())


def _extract_instagram(text: str) -> Tuple[Optional[str], str]:
    m = _INSTAGRAM_RE.search(text)
    if not m:
        return None, text
    return m.group(1), _remove_span(text, m.start(), m.end())


def _extract_phone(text: str) -> Tuple[Optional[str], str]:
    m = _PHONE_SCAN_RE.search(text)
    if not m:
        return None, text
    return m.group(1), _remove_span(text, m.start(), m.end())


# ---------------------------------------------------------------------------
# Company — best-effort suffix heuristic. Real company-name extraction
# without an entity model is inherently approximate; this only claims text
# when a recognizable business-entity keyword is present, so it stays silent
# (rather than guessing wrong) for anything else.
# ---------------------------------------------------------------------------
_COMPANY_SUFFIX_RE = re.compile(
    r"\b([A-Z][A-Za-z&.'\- ]{1,60}?\s(?:Studios?|Productions?|Films?|Media|"
    r"Entertainment|Agency|Agencies|Compan(?:y|ies)|Enterprises?|LLC|Inc\.?|"
    r"Pvt\.?\s?Ltd\.?|Ltd\.?|Corp\.?|Group))\b"
)


def _extract_company(text: str) -> Tuple[Optional[str], str]:
    m = _COMPANY_SUFFIX_RE.search(text)
    if not m:
        return None, text
    return m.group(1).strip(), _remove_span(text, m.start(), m.end())


# ---------------------------------------------------------------------------
# Location — a best-effort gazetteer of common industry hubs. Deliberately
# not exhaustive: the CRM schema has no dedicated city/country field (see
# crm.py's ROLE_LABEL_TO_SLUG-style comment), so a hit here becomes a tag,
# never a required or silently-invented value.
# ---------------------------------------------------------------------------
_LOCATIONS: Dict[str, Tuple[Optional[str], Optional[str]]] = {
    "mumbai": ("Mumbai", "India"), "delhi": ("Delhi", "India"),
    "new delhi": ("New Delhi", "India"), "bangalore": ("Bangalore", "India"),
    "bengaluru": ("Bengaluru", "India"), "hyderabad": ("Hyderabad", "India"),
    "chennai": ("Chennai", "India"), "kolkata": ("Kolkata", "India"),
    "pune": ("Pune", "India"), "ahmedabad": ("Ahmedabad", "India"),
    "goa": ("Goa", "India"),
    "dubai": ("Dubai", "UAE"), "abu dhabi": ("Abu Dhabi", "UAE"),
    "sharjah": ("Sharjah", "UAE"),
    "london": ("London", "UK"), "new york": ("New York", "USA"),
    "los angeles": ("Los Angeles", "USA"), "singapore": (None, "Singapore"),
    "india": (None, "India"), "uae": (None, "UAE"), "usa": (None, "USA"),
    "u.s.a": (None, "USA"), "uk": (None, "UK"),
}
# Longest keys first so "new delhi" matches before the substring "delhi".
_LOCATION_KEYS_BY_LENGTH = sorted(_LOCATIONS.keys(), key=len, reverse=True)


def _extract_location(text: str) -> Tuple[Optional[str], Optional[str], str]:
    lowered = text.lower()
    for key in _LOCATION_KEYS_BY_LENGTH:
        pattern = r"(?<!\w)" + re.escape(key) + r"(?!\w)"
        m = re.search(pattern, lowered)
        if m:
            city, country = _LOCATIONS[key]
            return city, country, _remove_span(text, m.start(), m.end())
    return None, None, text


# ---------------------------------------------------------------------------
# Name — whatever plausible-looking proper-noun text remains once every
# other entity has claimed its span. "Plausible" = has at least one
# alphabetic word, isn't just leftover punctuation/filler.
# ---------------------------------------------------------------------------
_FILLER_WORDS = {
    "he's", "she's", "is", "a", "an", "the", "his", "her", "him", "and",
    "from", "based", "in", "at", "role", "designation", "number", "phone",
    "contact", "for", "name", "of", "as", "please", "add", "save", "new",
    "named", "reach", "create", "can", "you",
}


def _clean_candidate(chunk: str) -> str:
    chunk = re.sub(r"[\n,.;:!?]+", " ", chunk)
    chunk = re.sub(r"\s+", " ", chunk).strip(" -")
    return chunk


def _extract_name(text: str) -> Tuple[Optional[str], str]:
    for raw_line in text.split("\n"):
        candidate = _clean_candidate(raw_line)
        if not candidate:
            continue
        words = [w for w in candidate.split(" ") if w]
        content_words = [w for w in words if w.lower() not in _FILLER_WORDS]
        if not content_words:
            continue
        if not re.search(r"[A-Za-z]", candidate):
            continue
        name = " ".join(content_words)
        if len(name) < 2:
            continue
        start = text.find(raw_line)
        remaining = text[:start] + text[start + len(raw_line):] if start >= 0 else text
        return name, remaining
    return None, text


_NAME_CORRECTION_RE = re.compile(
    r"(?:name\s+(?:is|should be)|change\s+name\s+to|correct\s+name\s+is)\s+"
    r"(?:actually\s+)?([A-Za-z][A-Za-z '\-]{1,60})",
    re.IGNORECASE,
)


def _extract_explicit_name_correction(text: str) -> Optional[str]:
    """Only used during natural-language EDIT parsing (see
    parse_edits_for_intent) — deliberately much stricter than
    _extract_name's "whatever's left over" fallback, which is only safe
    for a fresh message where some leftover text usually IS the name."""
    m = _NAME_CORRECTION_RE.search(text)
    if not m:
        return None
    candidate = _clean_candidate(m.group(1))
    return candidate or None


# ---------------------------------------------------------------------------
# Role — reuses whatever synonym/fuzzy registry the domain module provides
# (crm.py's SUPPORTED_ROLES + ROLE_SYNONYMS), so this stays a generic
# "find a phrase that matches one of these labels" scanner rather than
# hardcoding CRM role names itself. Scans word n-grams (longest first) so a
# multi-word role embedded anywhere in a sentence is found, not just a
# whole-line exact match.
# ---------------------------------------------------------------------------
_ROLE_AUTOCORRECT_CUTOFF = 0.85
_ROLE_FUZZY_CUTOFF = 0.72


def extract_role(text: str, registry: Dict[str, str]) -> Tuple[Optional[str], Optional[List[str]], str]:
    """Returns (matched_label, ambiguous_option_labels, remaining_text).
    Exactly one of matched_label / ambiguous_option_labels is set when a
    plausible role phrase was found; both are None if nothing matched at
    all (message simply doesn't mention a role)."""
    words = re.findall(r"[A-Za-z][A-Za-z']*", text)
    if not words:
        return None, None, text
    keys = list(registry.keys())
    best: Optional[Tuple[float, List[str], List[str]]] = None
    for n in (4, 3, 2, 1):
        for i in range(len(words) - n + 1):
            ngram_words = words[i:i + n]
            ngram = " ".join(ngram_words).lower()
            if ngram in registry:
                remaining = _remove_phrase(text, ngram_words)
                return registry[ngram], None, remaining
            close = difflib.get_close_matches(ngram, keys, n=3, cutoff=_ROLE_FUZZY_CUTOFF)
            if close:
                ratio = difflib.SequenceMatcher(None, ngram, close[0]).ratio()
                if best is None or ratio > best[0]:
                    best = (ratio, ngram_words, close)
    if best is None:
        return None, None, text
    ratio, ngram_words, close = best
    if ratio >= _ROLE_AUTOCORRECT_CUTOFF:
        remaining = _remove_phrase(text, ngram_words)
        return registry[close[0]], None, remaining
    options = [registry[k] for k in close]
    return None, options, text


def _remove_phrase(text: str, words: List[str]) -> str:
    phrase = r"\s+".join(re.escape(w) for w in words)
    m = re.search(phrase, text, re.IGNORECASE)
    if not m:
        return text
    return _remove_span(text, m.start(), m.end())


# ---------------------------------------------------------------------------
# Main orchestration — order matters: highest-confidence / least-ambiguous
# entities are extracted (and their span removed from the working text)
# first, so a lower-confidence recognizer like name/company never
# accidentally swallows a phone number or role phrase.
# ---------------------------------------------------------------------------
def extract_entities(text: str, role_registry: Dict[str, str]) -> Dict[str, object]:
    """Returns a dict that may contain any of: name, phone, role, email,
    instagram, company, city, country (all optional-presence, str values),
    plus `role_ambiguous` (list[str]) if a role phrase was found but
    matched multiple plausible labels too closely to auto-pick one."""
    working = _EMOJI_RE.sub(" ", text)
    working = _strip_leading_trigger(working)

    result: Dict[str, object] = {}

    email, working = _extract_email(working)
    if email:
        result["email"] = email

    phone, working = _extract_phone(working)
    if phone:
        result["phone"] = phone

    instagram, working = _extract_instagram(working)
    if instagram:
        result["instagram"] = instagram

    # Role before company: several canonical role labels ("Casting Company",
    # "Talent Agency", "Modeling Agency") share exact words with the
    # company-suffix heuristic ("Company", "Agency") — checking the
    # registry's exact/fuzzy matches first means a message that says
    # "Casting Company" resolves to the role, not a fabricated company name;
    # whatever role-shaped phrase gets removed here so the company
    # heuristic below only ever sees genuinely-remaining text.
    role, role_ambiguous, working = extract_role(working, role_registry)
    if role:
        result["role"] = role
    elif role_ambiguous:
        result["role_ambiguous"] = role_ambiguous

    company, working = _extract_company(working)
    if company:
        result["company"] = company

    city, country, working = _extract_location(working)
    if city:
        result["city"] = city
    if country:
        result["country"] = country

    name, working = _extract_name(working)
    if name:
        result["name"] = name

    return result


def extract_fields_for_intent(text: str, role_registry: Dict[str, str]) -> Dict[str, str]:
    """IntentDefinition.extract_fields-compatible: {field_key: raw_value}.
    Ambiguous role matches are surfaced as a specially-formatted "role" raw
    value so the existing FieldSpec.validate error path can turn it into a
    clarifying question — no conversation-engine changes needed."""
    entities = extract_entities(text, role_registry)
    out: Dict[str, str] = {}
    for key in ("name", "phone", "role", "email", "company", "city", "country", "instagram"):
        val = entities.get(key)
        if val:
            out[key] = str(val)
    if "role" not in out and entities.get("role_ambiguous"):
        # Encode the ambiguous options so _validate_role (crm.py) can
        # recognize this shape and ask "did you mean X or Y" instead of
        # treating it as a plain invalid string.
        out["role"] = "__ambiguous__:" + "|".join(entities["role_ambiguous"])
    return out


def parse_edits_for_intent(text: str, fields: List[FieldSpec], role_registry: Dict[str, str]) -> Dict[str, str]:
    """IntentDefinition.parse_edits-compatible natural-language edit parser.
    Falls back to the generic "Key = value" syntax first (still works,
    zero regression), then tries entity extraction on the whole message —
    whichever entities are found update ONLY those fields, leaving every
    other already-collected value untouched (the dispatcher only applies
    keys present in the returned dict).

    The name field is handled separately and more conservatively than a
    fresh message's extraction: a short correction utterance like "Actually
    he's from Dubai" or "No, change the role to Producer" has no name in
    it at all, but the generic name fallback (built for a FRESH message,
    where SOME leftover text is usually the name) would claim a stray
    filler word ("Actually", "No change to") and silently overwrite an
    already-confirmed correct name. Only an explicit correction cue
    ("name is X", "change name to X") updates it here."""
    from agents.parser import parse_edit_instructions

    explicit = parse_edit_instructions(text, fields)
    if explicit:
        return explicit

    field_keys = {f.key for f in fields}
    out: Dict[str, str] = {}

    if "name" in field_keys:
        explicit_name = _extract_explicit_name_correction(text)
        if explicit_name:
            out["name"] = explicit_name

    entities = extract_entities(text, role_registry)
    for key in ("phone", "role", "email", "company", "city", "country", "instagram"):
        if key in field_keys and entities.get(key):
            out[key] = str(entities[key])
    return out
