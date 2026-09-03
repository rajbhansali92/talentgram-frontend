"""AI Casting Desk — Gate 1 requirement parser + project-draft mapping.

Pipeline (all pure functions except `analyse` itself):

    raw text + material text
        -> analyse()                -> ExtractedRequirement (per-field {value, confidence})
        -> build_project_draft()    -> a ProjectIn-SHAPED dict + submission_requirements
        -> draft_readiness()        -> what's missing / ambiguous (never blocks except no brand)
        -> draft_to_project_payload()-> kwargs for core.ProjectIn(**payload)

The schema below intentionally maps ONTO the existing Talentgram Project
model (core.ProjectIn) + the existing submission-requirements object
(core.default_submission_requirements()). Fields Talentgram's Project model
does not have (audition date, shoot location, shoot days, age/gender/height
of the character, dress code, ...) are composed into the model's existing
free-text fields (`character`, `additional_details`) exactly the way a
human fills that form today — NOT turned into new DB columns.

HARD RULE, enforced by the prompt and re-checked here: the model must not
invent a value that the requirement does not state. Unstated => value ""
and confidence "missing". Commission especially is never guessed.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ai import client as llm

# Confidence vocabulary surfaced to the UI. Deliberately 3 buckets, no maths.
CONF_STATED = "stated"      # explicitly in the requirement
CONF_INFERRED = "inferred"  # strongly implied but not spelled out
CONF_MISSING = "missing"    # not present at all -> value must be ""

_FIELD_KEYS = [
    "brand", "project_name", "medium", "usage", "usage_duration",
    "budget", "commission", "shoot_date", "audition_date", "shoot_days",
    "shoot_location", "talent_count", "gender", "age_range", "height",
    "look", "character", "audition_instructions", "submission_instructions",
    "dress_code", "director", "production_house",
]

# Submission-requirement toggles the AI is allowed to recommend. Each maps to
# a real key in core.default_submission_requirements()["fields"] (or the
# project-level competitive_brand flag).
_ALLOWED_REQUIREMENTS = [
    "current_location", "availability", "budget_expectation", "competitive_brand",
]
_REQUIREMENT_TO_FIELD = {
    "current_location": "location",
    "availability": "availability",
    "budget_expectation": "budget_expectation",
    # competitive_brand is handled via the project flag + the "competitive_brand" field
}

_ONE_FIELD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "value": {
            "type": "string",
            "description": "Verbatim or lightly-normalised value from the requirement. Empty string if not stated.",
        },
        "confidence": {"type": "string", "enum": [CONF_STATED, CONF_INFERRED, CONF_MISSING]},
    },
    "required": ["value", "confidence"],
}

REQUIREMENT_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "fields": {
            "type": "object",
            "additionalProperties": False,
            "properties": {k: _ONE_FIELD_SCHEMA for k in _FIELD_KEYS},
            "required": list(_FIELD_KEYS),
        },
        "reference_links": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Any URLs in the requirement (reference videos, decks, brand sites).",
        },
        "recommended_submission_requirements": {
            "type": "array",
            "items": {"type": "string", "enum": _ALLOWED_REQUIREMENTS},
            "description": "Which audition-form questions this brief implies should be REQUIRED.",
        },
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "field": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["field", "message"],
            },
            "description": "Anything missing, ambiguous, or contradictory a human must resolve.",
        },
        "summary": {
            "type": "string",
            "description": "One or two plain sentences describing the casting need.",
        },
    },
    "required": ["fields", "reference_links", "recommended_submission_requirements", "flags", "summary"],
}

_SYSTEM_PROMPT = """You are a casting coordinator's assistant at Talentgram, a talent \
agency. You read a raw casting requirement (usually a WhatsApp message from a casting \
director, plus any attached script / brief / audio-brief text) and turn it into a \
structured project brief.

Rules — follow them exactly:
1. Extract ONLY what the requirement states. Never invent, guess, or fill in a \
"typical" value. If something is not mentioned, set its value to "" and confidence \
to "missing".
2. confidence: "stated" = written explicitly; "inferred" = strongly implied by the \
text but not spelled out; "missing" = absent.
3. COMMISSION is never assumed. If the requirement does not mention a commission / \
agency fee, commission.value must be "" and confidence "missing".
4. Dates: keep them as written ("15 Sept", "next Monday"). Do not resolve to a \
calendar date.
5. budget: copy the figure as written, including currency and "per day" / "total" \
wording if present.
6. talent_count: a number as a string ("3"). gender / age_range / height / look \
describe the person being cast.
7. character: the role / brief / what the talent must portray.
8. Put every URL you see into reference_links.
9. recommended_submission_requirements: include an item only when the brief clearly \
implies that audition question matters (e.g. a tight shoot window -> "availability"; \
an explicit budget-confirmation ask -> "budget_expectation"; a competitor-brand \
conflict note -> "competitive_brand"; a location-specific shoot -> "current_location").
10. flags: list everything a human must decide — missing commission, vague location, \
unclear number of talents, contradictory dates, etc.

Output only by calling the emit_casting_requirement tool."""

_USER_TEMPLATE = """CASTING REQUIREMENT (raw):
\"\"\"
{raw}
\"\"\"
{materials}"""


def _blank_field() -> Dict[str, str]:
    return {"value": "", "confidence": CONF_MISSING}


def empty_extraction() -> Dict[str, Any]:
    return {
        "fields": {k: _blank_field() for k in _FIELD_KEYS},
        "reference_links": [],
        "recommended_submission_requirements": [],
        "flags": [],
        "summary": "",
    }


def normalise_extraction(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a model response into the exact shape the rest of the code
    relies on — tolerant of a missing key or a bare string value."""
    out = empty_extraction()
    fields = raw.get("fields") or {}
    for k in _FIELD_KEYS:
        fv = fields.get(k)
        if isinstance(fv, dict):
            value = str(fv.get("value") or "").strip()
            conf = fv.get("confidence")
            if conf not in (CONF_STATED, CONF_INFERRED, CONF_MISSING):
                conf = CONF_STATED if value else CONF_MISSING
        elif isinstance(fv, str):
            value = fv.strip()
            conf = CONF_STATED if value else CONF_MISSING
        else:
            value, conf = "", CONF_MISSING
        if not value:
            conf = CONF_MISSING
        out["fields"][k] = {"value": value, "confidence": conf}

    links = raw.get("reference_links")
    if isinstance(links, list):
        out["reference_links"] = [str(x).strip() for x in links if str(x).strip()]

    reqs = raw.get("recommended_submission_requirements")
    if isinstance(reqs, list):
        out["recommended_submission_requirements"] = [
            r for r in reqs if r in _ALLOWED_REQUIREMENTS
        ]

    flags = raw.get("flags")
    if isinstance(flags, list):
        clean_flags = []
        for f in flags:
            if isinstance(f, dict) and (f.get("message") or f.get("field")):
                clean_flags.append({
                    "field": str(f.get("field") or "").strip(),
                    "message": str(f.get("message") or "").strip(),
                })
            elif isinstance(f, str) and f.strip():
                clean_flags.append({"field": "", "message": f.strip()})
        out["flags"] = clean_flags

    if isinstance(raw.get("summary"), str):
        out["summary"] = raw["summary"].strip()
    return out


async def analyse(raw_text: str, materials_text: str = "") -> Dict[str, Any]:
    """Run the single Gate-1 LLM call. Raises ai.client.LLMUnavailable /
    LLMError — the router maps those to 503 / 502."""
    raw_text = (raw_text or "").strip()
    if not raw_text and not materials_text.strip():
        raise llm.LLMError("nothing to analyse — paste a requirement or add a material")

    materials_block = ""
    if materials_text.strip():
        # Guard prompt size — a long script only needs its first pages for the brief.
        clipped = materials_text.strip()[:20000]
        materials_block = f'\n\nATTACHED MATERIAL TEXT (script / brief / audio brief):\n"""\n{clipped}\n"""'

    user = _USER_TEMPLATE.format(raw=raw_text[:20000], materials=materials_block)
    data = await llm.call_tool_json(
        system=_SYSTEM_PROMPT,
        user=user,
        tool_name="emit_casting_requirement",
        tool_description="Return the structured casting brief extracted from the requirement.",
        input_schema=REQUIREMENT_TOOL_SCHEMA,
        max_tokens=4000,
    )
    return normalise_extraction(data)


# ---------------------------------------------------------------------------
# Mapping: ExtractedRequirement (+ human edits) -> ProjectIn-shaped draft
# ---------------------------------------------------------------------------

# core.COMMISSION_OPTIONS, imported lazily to keep this module DB-free for unit tests.
def _commission_options() -> List[str]:
    from core import COMMISSION_OPTIONS
    return list(COMMISSION_OPTIONS)


def normalise_commission(raw: str) -> Optional[str]:
    """'20' / '20%' / '20 percent' -> '20%' iff it's one of the allowed
    rates; anything else -> None (caller flags it for manual entry)."""
    if not raw:
        return None
    m = re.search(r"(\d{1,2})", raw)
    if not m:
        return None
    candidate = f"{int(m.group(1))}%"
    return candidate if candidate in _commission_options() else None


def _v(ext: Dict[str, Any], key: str) -> str:
    return (ext.get("fields", {}).get(key, {}) or {}).get("value", "") or ""


def _compose_character(ext: Dict[str, Any]) -> str:
    parts: List[str] = []
    count = _v(ext, "talent_count")
    gender = _v(ext, "gender")
    age = _v(ext, "age_range")
    head_bits = [b for b in [count, gender] if b]
    if head_bits:
        line = " ".join(head_bits)
        if age:
            line += f", age {age}"
        parts.append(line)
    elif age:
        parts.append(f"Age {age}")
    for label, key in (("Height", "height"), ("Look", "look"), ("Character / brief", "character")):
        val = _v(ext, key)
        if val:
            parts.append(f"{label}: {val}")
    return "\n".join(parts)


def _compose_medium_usage(ext: Dict[str, Any]) -> str:
    medium = _v(ext, "medium")
    usage = _v(ext, "usage")
    duration = _v(ext, "usage_duration")
    bits = []
    primary = " / ".join([b for b in [medium, usage] if b])
    if primary:
        bits.append(primary)
    if duration:
        bits.append(f"Usage duration: {duration}")
    return " — ".join(bits)


def _compose_additional_details(ext: Dict[str, Any]) -> str:
    lines: List[str] = []
    if ext.get("summary"):
        lines.append(ext["summary"])
        lines.append("")
    ordered = [
        ("Audition / trial date", "audition_date"),
        ("Shoot location", "shoot_location"),
        ("Shoot days", "shoot_days"),
        ("Dress code (audition)", "dress_code"),
        ("Audition instructions", "audition_instructions"),
        ("Submission instructions", "submission_instructions"),
    ]
    for label, key in ordered:
        val = _v(ext, key)
        if val:
            lines.append(f"{label}: {val}")
    links = ext.get("reference_links") or []
    if links:
        lines.append("")
        lines.append("Reference links:")
        lines.extend(f"- {u}" for u in links)
    return "\n".join(lines).strip()


_VIDEO_URL_RE = re.compile(r"(youtu\.be|youtube\.com|vimeo\.com|drive\.google\.com|\.mp4|\.mov)", re.I)


def build_project_draft(ext: Dict[str, Any], human_edits: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Deterministically map an extraction to a ProjectIn-shaped dict, then
    overlay any human field edits. `human_edits` keys are draft keys;
    scalar values replace, `submission_requirements` / `video_links` replace
    wholesale when present."""
    from core import default_submission_requirements

    human_edits = dict(human_edits or {})
    ext = normalise_extraction(ext)

    commission_raw = _v(ext, "commission")
    commission = normalise_commission(commission_raw)

    reqs = set(ext.get("recommended_submission_requirements") or [])
    sub_req = default_submission_requirements()
    for r in reqs:
        field = _REQUIREMENT_TO_FIELD.get(r)
        if field and field in sub_req["fields"]:
            sub_req["fields"][field] = "required"
    if "competitive_brand" in reqs:
        sub_req["fields"]["competitive_brand"] = "required"

    links = ext.get("reference_links") or []
    video_links = [u for u in links if _VIDEO_URL_RE.search(u or "")]

    draft: Dict[str, Any] = {
        "brand_name": _v(ext, "brand"),
        "project_label": _v(ext, "project_name"),  # display only; not a ProjectIn field
        "character": _compose_character(ext),
        "shoot_dates": _v(ext, "shoot_date"),
        "budget_per_day": _v(ext, "budget"),
        "commission_percent": commission,
        "medium_usage": _compose_medium_usage(ext),
        "director": _v(ext, "director"),
        "production_house": _v(ext, "production_house"),
        "additional_details": _compose_additional_details(ext),
        "video_links": video_links,
        "competitive_brand_enabled": "competitive_brand" in reqs,
        "status": "ongoing",
        "submission_requirements": sub_req,
    }

    # Overlay human edits.
    for k, val in human_edits.items():
        if k == "submission_requirements" and isinstance(val, dict):
            draft["submission_requirements"] = val
        elif k == "video_links" and isinstance(val, list):
            draft["video_links"] = [str(x).strip() for x in val if str(x).strip()]
        elif k == "commission_percent":
            draft["commission_percent"] = val or None
        elif k in draft:
            draft[k] = val

    # A commission the model reported but that isn't a standard rate: never
    # silently drop it — carry the raw text into the brief and flag it.
    if commission_raw and draft["commission_percent"] is None and "commission_percent" not in human_edits:
        note = f"Commission mentioned as \"{commission_raw}\" — not a standard rate (10/15/20/25/30%). Set manually."
        if note not in draft["additional_details"]:
            draft["additional_details"] = (draft["additional_details"] + "\n\n" + note).strip()

    return draft


_IMPORTANT_MISSING = {
    "brand": "Brand not specified",
    "shoot_date": "Shoot date not specified",
    "budget": "Budget not specified",
    "commission": "Commission not specified",
    "shoot_location": "Shoot location not specified",
    "talent_count": "Number of talents not specified",
    "medium": "Usage / medium not specified",
}

# extraction field key -> the project-draft key a human edit would live under
_FIELD_TO_DRAFT_KEY = {
    "brand": "brand_name",
    "shoot_date": "shoot_dates",
    "budget": "budget_per_day",
    "commission": "commission_percent",
    "medium": "medium_usage",
    "shoot_location": "additional_details",
    "talent_count": "character",
    "character": "character",
    "look": "character",
}


def _human_resolved(field_key: str, human_edits: Dict[str, Any]) -> bool:
    dk = _FIELD_TO_DRAFT_KEY.get(field_key)
    return bool(dk and str(human_edits.get(dk) or "").strip())


def draft_readiness(ext: Dict[str, Any], draft: Dict[str, Any], human_edits: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What the human should know before approving. Only a missing brand
    name BLOCKS creation — everything else is a warning the human overrides
    by clicking Approve (mirrors the existing project form, which only
    hard-requires brand_name). A warning disappears once the human supplies
    that value, whether it originated as a missing field or an AI flag."""
    human_edits = human_edits or {}
    ext = normalise_extraction(ext)
    blocking: List[str] = []
    warnings: List[Dict[str, str]] = []
    seen: set = set()

    if not (draft.get("brand_name") or "").strip():
        blocking.append("Brand name is required to create the project.")

    for key, label in _IMPORTANT_MISSING.items():
        field = ext["fields"].get(key, {})
        if field.get("confidence") == CONF_MISSING and not _human_resolved(key, human_edits):
            warnings.append({"field": key, "message": label})
            seen.add(label.lower())

    for f in ext.get("flags", []):
        fk = (f.get("field") or "").strip()
        msg = (f.get("message") or "").strip()
        if not msg or msg.lower() in seen:
            continue
        if fk and _human_resolved(fk, human_edits):
            continue
        warnings.append({"field": fk, "message": msg})
        seen.add(msg.lower())

    return {
        "can_create": not blocking,
        "blocking": blocking,
        "warnings": warnings,
    }


_PROJECT_IN_KEYS = [
    "brand_name", "brand_link", "character", "shoot_dates", "budget_per_day",
    "commission_percent", "medium_usage", "director", "production_house",
    "additional_details", "video_links", "competitive_brand_enabled",
    "status", "submission_requirements",
]


def draft_to_project_payload(draft: Dict[str, Any]) -> Dict[str, Any]:
    """Pick exactly the keys core.ProjectIn accepts. Empty strings become
    None where the model field is Optional. `commission_percent` is passed
    only when it's a valid rate (ProjectIn rejects anything else)."""
    payload: Dict[str, Any] = {}
    for k in _PROJECT_IN_KEYS:
        if k not in draft:
            continue
        val = draft[k]
        if k == "commission_percent":
            payload[k] = val if val in _commission_options() else None
        elif k == "brand_name":
            payload[k] = (val or "").strip()
        elif isinstance(val, str):
            payload[k] = val.strip() or None
        else:
            payload[k] = val
    return payload
