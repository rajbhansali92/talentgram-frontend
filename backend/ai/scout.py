"""AI Scout — Gate 2 brain.

Pipeline (all pure/deterministic except the two LLM calls):

    project (+ its Gate-1 session if any)
        -> resolve_criteria()          structured scouting criteria (Gate-1 session,
                                       else 1 small LLM call, else regex best-effort)
        -> build_candidate_query()     lenient Mongo query (missing != rejected)
        -> [db.talents.find]           bounded candidate pool
        -> deterministic sub-scores    requirement_fit / location_fit / profile_confidence
        -> rank_candidates()           ONE batched LLM call per ~25 candidates:
                                       character_fit / experience_fit / strengths /
                                       risks / reason — NOTHING factual
        -> assemble_result()           merge, compute overall, build field_verification
                                       + unknowns SERVER-SIDE (never from the model)
        -> tier()                      Top / Strong / Possible

Design choices that matter:
  * The model never scores gender/age/height/location fit and never states a
    factual attribute — those come from the DB. It only judges the subjective
    bits (does the bio/look read right for the character; is there relevant
    experience) and writes the explanation.
  * A missing talent field is "Unknown / Needs verification", never a rejection,
    unless the project requirement is a hard one the admin marked.
  * One filter query + a few batched LLM calls. No per-talent LLM call.

NOT the same feature as backend/scout_capture.py ("AI Scout Capture" =
screenshot -> new-talent fields). This ranks EXISTING talents for a project.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ai import client as llm

logger = logging.getLogger(__name__)

SCOUT_MODEL = os.environ.get("AI_SCOUT_MODEL") or llm.DEFAULT_MODEL
MAX_CANDIDATES = int(os.environ.get("AI_SCOUT_MAX_CANDIDATES", "300"))
BATCH_SIZE = int(os.environ.get("AI_SCOUT_BATCH_SIZE", "25"))

# interested_in vocabulary (frontend TalentEdit.jsx activeOptions).
TALENT_CATEGORIES = ["Acting", "Modeling", "Influencer Campaigns"]

# Fields whose presence defines "profile completeness" — mirrors
# routers.talents._COMPLETENESS_FIELDS (kept local so this module is DB-free).
_COMPLETENESS_FIELDS = [
    "cover_url", "height", "location", "gender", "dob", "ethnicity",
    "instagram_handle", "instagram_followers", "bio", "skills",
    "interested_in", "work_links",
]

# Weighting for the overall score — renormalised over whichever components
# are actually available (a None component is dropped, never scored as 0).
_WEIGHTS = {
    "requirement_fit": 0.35,
    "character_fit": 0.22,
    "location_fit": 0.18,
    "experience_fit": 0.13,
    "profile_confidence": 0.12,
}

# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------
EMPTY_CRITERIA: Dict[str, Any] = {
    "gender": "",
    "age_min": None,
    "age_max": None,
    "height_min": "",          # free text, e.g. "5'6\""
    "locations": [],           # city names
    "ethnicity": "",
    "categories": [],          # subset of TALENT_CATEGORIES
    "competitive_brands_note": "",
    "character_summary": "",
    "hard_filters": [],        # which of {gender,age,height,location} the admin made strict
}

_CRITERIA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "gender": {"type": "string", "description": "female | male | non_binary | '' if not specified"},
        "age_min": {"type": "integer", "description": "0 if not specified"},
        "age_max": {"type": "integer", "description": "0 if not specified"},
        "height_min": {"type": "string", "description": "e.g. \"5'6\\\"\" or '' — minimum height only"},
        "locations": {"type": "array", "items": {"type": "string"}, "description": "shoot / base cities"},
        "ethnicity": {"type": "string", "description": "'' unless the brief clearly specifies one"},
        "categories": {"type": "array", "items": {"type": "string", "enum": TALENT_CATEGORIES}},
        "competitive_brands_note": {"type": "string"},
        "character_summary": {"type": "string", "description": "1-2 sentences: who we are casting"},
    },
    "required": [
        "gender", "age_min", "age_max", "height_min", "locations", "ethnicity",
        "categories", "competitive_brands_note", "character_summary",
    ],
}

_CRITERIA_SYSTEM = """You extract SCOUTING criteria from a casting project so a \
talent database can be filtered. Only use what the project text states. If \
something is not stated, leave it empty / 0 — never guess a "typical" value. \
gender is one of female/male/non_binary or ''. age_min/age_max are integers (0 \
if not stated). height_min is a minimum only (or ''). locations are cities. categories is \
a subset of Acting/Modeling/Influencer Campaigns. competitive_brands_note copies \
any competitor / conflict restriction verbatim. character_summary is 1-2 plain \
sentences. Respond only via the tool."""


def _norm_gender(v: str) -> str:
    s = (v or "").strip().lower()
    if s in ("female", "f", "woman", "women"):
        return "female"
    if s in ("male", "m", "man", "men"):
        return "male"
    if s in ("non_binary", "non-binary", "nonbinary", "nb"):
        return "non_binary"
    return ""


def _parse_age_range(raw: str) -> Tuple[Optional[int], Optional[int]]:
    if not raw:
        return None, None
    nums = [int(n) for n in re.findall(r"\d{1,2}", raw)]
    nums = [n for n in nums if 5 <= n <= 90]
    if len(nums) >= 2:
        return min(nums[:2]), max(nums[:2])
    if len(nums) == 1:
        return nums[0], nums[0]
    return None, None


def criteria_from_gate1_extraction(ext: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic map from a Gate-1 casting_desk session's `extraction`."""
    f = ext.get("fields", {}) if isinstance(ext, dict) else {}

    def val(k: str) -> str:
        return ((f.get(k) or {}).get("value") or "").strip()

    age_min, age_max = _parse_age_range(val("age_range"))
    cats: List[str] = []
    look_blob = " ".join([val("look"), val("character"), ext.get("summary", "") or ""]).lower()
    if any(w in look_blob for w in ("influenc", "creator", "ugc")):
        cats.append("Influencer Campaigns")
    if any(w in look_blob for w in ("model", "runway", "print")):
        cats.append("Modeling")
    if any(w in look_blob for w in ("act", "actor", "actress", "scene", "dialogue", "character")):
        cats.append("Acting")

    character = val("character") or ext.get("summary", "") or ""
    if val("look"):
        character = (character + " Look: " + val("look")).strip()

    crit = dict(EMPTY_CRITERIA)
    crit.update({
        "gender": _norm_gender(val("gender")),
        "age_min": age_min,
        "age_max": age_max,
        "height_min": val("height"),
        "locations": [c.strip() for c in re.split(r"[,/&]| and ", val("shoot_location")) if c.strip()],
        "ethnicity": "",
        "categories": cats,
        "competitive_brands_note": val("submission_instructions") if "compet" in val("submission_instructions").lower() else "",
        "character_summary": character[:600],
    })
    return crit


async def extract_criteria_via_llm(project: Dict[str, Any]) -> Dict[str, Any]:
    text_parts = [
        f"Brand: {project.get('brand_name', '')}",
        f"Medium / usage: {project.get('medium_usage', '')}",
        f"Character / talent requirement:\n{project.get('character', '')}",
        f"Additional details:\n{project.get('additional_details', '')}",
    ]
    sr = (project.get("submission_requirements") or {}).get("fields", {})
    if sr.get("competitive_brand") == "required" or project.get("competitive_brand_enabled"):
        text_parts.append("Competitive-brand / conflict restriction applies to this project.")
    user = "\n\n".join(p for p in text_parts if p.strip())

    data = await llm.call_tool_json(
        system=_CRITERIA_SYSTEM,
        user=user,
        tool_name="emit_scouting_criteria",
        tool_description="Structured scouting criteria extracted from the project.",
        input_schema=_CRITERIA_SCHEMA,
        max_tokens=1200,
        model=SCOUT_MODEL,
    )
    return normalise_criteria(data)


def normalise_criteria(raw: Dict[str, Any]) -> Dict[str, Any]:
    crit = dict(EMPTY_CRITERIA)
    if not isinstance(raw, dict):
        return crit
    crit["gender"] = _norm_gender(str(raw.get("gender") or ""))
    for k in ("age_min", "age_max"):
        v = raw.get(k)
        try:
            iv = int(v) if v is not None and str(v).strip() != "" else 0
        except (TypeError, ValueError):
            iv = 0
        crit[k] = iv if iv and 5 <= iv <= 90 else None
    if crit["age_min"] and crit["age_max"] and crit["age_min"] > crit["age_max"]:
        crit["age_min"], crit["age_max"] = crit["age_max"], crit["age_min"]
    crit["height_min"] = str(raw.get("height_min") or "").strip()
    crit["locations"] = [str(x).strip() for x in (raw.get("locations") or []) if str(x).strip()][:6]
    crit["ethnicity"] = str(raw.get("ethnicity") or "").strip().lower()
    crit["categories"] = [c for c in (raw.get("categories") or []) if c in TALENT_CATEGORIES]
    crit["competitive_brands_note"] = str(raw.get("competitive_brands_note") or "").strip()
    crit["character_summary"] = str(raw.get("character_summary") or "").strip()[:800]
    hf = raw.get("hard_filters") or []
    crit["hard_filters"] = [h for h in hf if h in ("gender", "age", "height", "location")]
    return crit


# ---------------------------------------------------------------------------
# Deterministic sub-scoring
# ---------------------------------------------------------------------------
def _height_to_inches(raw: Optional[str]) -> Optional[float]:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(?:cm)?$", s)
    if m and float(m.group(1)) > 100:
        return round(float(m.group(1)) / 2.54, 1)
    m = re.search(r"(\d+)\s*(?:'|’|ft|feet)\s*(\d+)?", s)
    if m:
        return float(int(m.group(1)) * 12 + (int(m.group(2)) if m.group(2) else 0))
    return None


def talent_age(talent: Dict[str, Any]) -> Optional[int]:
    dob = talent.get("dob")
    if dob and isinstance(dob, str) and len(dob) >= 4:
        try:
            from datetime import date
            y, m, d = (int(x) for x in dob[:10].split("-"))
            today = date.today()
            return today.year - y - ((today.month, today.day) < (m, d))
        except Exception:
            pass
    a = talent.get("age")
    if isinstance(a, int) and 0 < a < 120:
        return a
    return None


def talent_height_inches(talent: Dict[str, Any]) -> Optional[float]:
    v = talent.get("height_inches")
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    return _height_to_inches(talent.get("height"))


def talent_cities(talent: Dict[str, Any]) -> List[str]:
    out = []
    for loc in talent.get("location") or []:
        if isinstance(loc, dict):
            if loc.get("city"):
                out.append(str(loc["city"]).strip().lower())
    return out


def profile_confidence(talent: Dict[str, Any]) -> int:
    present = 0
    for fld in _COMPLETENESS_FIELDS:
        v = talent.get(fld)
        if v not in (None, "", []) and not (isinstance(v, list) and len(v) == 0):
            present += 1
    return round(100 * present / len(_COMPLETENESS_FIELDS))


def requirement_fit(talent: Dict[str, Any], crit: Dict[str, Any]) -> Tuple[Optional[int], Dict[str, str]]:
    """Deterministic. Returns (score|None, per-dimension verdicts).
    A dimension the criteria doesn't ask for is 'n/a'; one it asks for but
    the talent lacks is 'unknown' (excluded from the average, never 0)."""
    dims: Dict[str, str] = {}
    scored: List[float] = []

    # gender
    if crit.get("gender"):
        tg = (talent.get("gender") or "").strip().lower()
        if not tg:
            dims["gender"] = "unknown"
        elif tg == crit["gender"]:
            dims["gender"] = "match"
            scored.append(1.0)
        else:
            dims["gender"] = "mismatch"
            scored.append(0.0)
    else:
        dims["gender"] = "n/a"

    # age
    if crit.get("age_min") is not None or crit.get("age_max") is not None:
        a = talent_age(talent)
        lo = crit.get("age_min") if crit.get("age_min") is not None else 0
        hi = crit.get("age_max") if crit.get("age_max") is not None else 200
        if a is None:
            dims["age"] = "unknown"
        elif lo <= a <= hi:
            dims["age"] = "match"
            scored.append(1.0)
        elif lo - 3 <= a <= hi + 3:
            dims["age"] = "near"
            scored.append(0.5)
        else:
            dims["age"] = "mismatch"
            scored.append(0.0)
    else:
        dims["age"] = "n/a"

    # height (minimum)
    if crit.get("height_min"):
        want = _height_to_inches(crit["height_min"])
        h = talent_height_inches(talent)
        if want is None:
            dims["height"] = "n/a"
        elif h is None:
            dims["height"] = "unknown"
        elif h >= want:
            dims["height"] = "match"
            scored.append(1.0)
        elif h >= want - 1:
            dims["height"] = "near"
            scored.append(0.6)
        else:
            dims["height"] = "mismatch"
            scored.append(0.2)
    else:
        dims["height"] = "n/a"

    # ethnicity (soft)
    if crit.get("ethnicity"):
        te = (talent.get("ethnicity") or "").strip().lower()
        if not te:
            dims["ethnicity"] = "unknown"
        elif te == crit["ethnicity"]:
            dims["ethnicity"] = "match"
            scored.append(1.0)
        else:
            dims["ethnicity"] = "mismatch"
            scored.append(0.3)
    else:
        dims["ethnicity"] = "n/a"

    score = round(100 * sum(scored) / len(scored)) if scored else None
    return score, dims


def location_fit(talent: Dict[str, Any], crit: Dict[str, Any]) -> Optional[int]:
    """100 = based in a wanted city, 40 = based elsewhere (would travel/relocate),
    None = no location on file (Unknown) OR not a criterion."""
    wants = [c.strip().lower() for c in (crit.get("locations") or []) if c.strip()]
    if not wants:
        return None
    cities = talent_cities(talent)
    if not cities:
        return None
    if any(c in wants for c in cities):
        return 100
    return 40


def category_hit(talent: Dict[str, Any], crit: Dict[str, Any]) -> Optional[bool]:
    wants = crit.get("categories") or []
    if not wants:
        return None
    have = set(x for x in (talent.get("interested_in") or []))
    if not have:
        return None
    return bool(have & set(wants))


# ---------------------------------------------------------------------------
# Candidate query (lenient — missing field never rejects unless hard-filtered)
# ---------------------------------------------------------------------------
def build_candidate_query(crit: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic candidate filter.

    * gender: ALWAYS filtered, leniently — a matching or an UNKNOWN gender is
      kept; only an explicit opposite gender is excluded (a male is not a
      candidate for a female role).
    * age / height / location: filtered ONLY when the admin ticks them as a
      hard requirement (``crit['hard_filters']``). A hard filter keeps
      matches AND talents whose field is missing (incomplete profile !=
      rejection, per spec) but drops explicit out-of-range values. When NOT
      ticked they are pure ranking signals — the whole gender-matching pool
      is scored and tiered, and out-of-spec talents fall to "Possible".
    * Soft-deleted / draft / merged talents are always excluded.

    ``routers.talents._age_range_to_dob_range`` is reused for the exact dob math.
    """
    from routers.talents import _age_range_to_dob_range

    hard = set(crit.get("hard_filters") or [])
    ands: List[Dict[str, Any]] = []

    def with_unknown(field: str, match_clause: Dict[str, Any]) -> Dict[str, Any]:
        return {"$or": [match_clause, {field: {"$in": [None, ""]}}, {field: {"$exists": False}}]}

    if crit.get("gender"):
        ands.append(with_unknown("gender", {"gender": crit["gender"]}))

    if "age" in hard and (crit.get("age_min") is not None or crit.get("age_max") is not None):
        lo = crit.get("age_min") if crit.get("age_min") is not None else 0
        hi = crit.get("age_max") if crit.get("age_max") is not None else 200
        # ±3yr band so a "near" match survives the filter and gets scored.
        dob_range = _age_range_to_dob_range(max(lo - 3, 0), hi + 3)
        age_clause = {"$gte": max(lo - 3, 0), "$lte": hi + 3}
        match = {"$or": [{"dob": dob_range}, {"age": age_clause}]}
        ands.append({"$or": [
            match,
            {"$and": [{"dob": {"$in": [None, ""]}}, {"age": {"$in": [None]}}]},
            {"$and": [{"dob": {"$exists": False}}, {"age": {"$exists": False}}]},
        ]})

    if "height" in hard and crit.get("height_min"):
        want = _height_to_inches(crit["height_min"])
        if want is not None:
            ands.append(with_unknown("height_inches", {"height_inches": {"$gte": want - 1}}))

    if "location" in hard and crit.get("locations"):
        rx = [{"location.city": {"$regex": f"^{re.escape(c)}$", "$options": "i"}} for c in crit["locations"]]
        ands.append({"$or": rx + [{"location": {"$size": 0}}, {"location": {"$exists": False}}]})

    query: Dict[str, Any] = {"status": {"$nin": ["DRAFT", "ARCHIVED", "MERGED"]}}
    if ands:
        query["$and"] = ands
    return query


CANDIDATE_PROJECTION = {
    "_id": 0, "id": 1, "name": 1, "gender": 1, "dob": 1, "age": 1, "height": 1,
    "height_inches": 1, "location": 1, "ethnicity": 1, "instagram_handle": 1,
    "instagram_followers": 1, "bio": 1, "interested_in": 1, "skills": 1, "tags": 1,
    "work_links": 1, "total_submissions": 1, "cover_url": 1, "cover_thumbnail_url": 1,
    "media_count": 1, "status": 1,
}


# ---------------------------------------------------------------------------
# LLM ranking (subjective only)
# ---------------------------------------------------------------------------
_RANK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "rankings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "talent_id": {"type": "string"},
                    "character_fit": {"type": "integer", "description": "0-100, or -1 if bio/photo give no signal"},
                    "experience_fit": {"type": "integer", "description": "0-100 from stored submissions/tags/followers/work links ONLY, or -1 if none present"},
                    "confidence": {"type": "number", "description": "0-1 — how sure you are given the data provided"},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["talent_id", "character_fit", "experience_fit", "confidence", "strengths", "risks", "reason"],
            },
        }
    },
    "required": ["rankings"],
}

_RANK_SYSTEM = """You are an experienced casting assistant at a talent agency. \
For each candidate you are given ONLY the data the agency has on file. Judge two \
things and write a short explanation:

  character_fit (0-100, or -1): does this person's bio / stated look / categories \
read as right for the character? Use -1 if there is no bio and no photo on file.
  experience_fit (0-100, or -1): is there RELEVANT experience — using only the \
provided submission count, tags, follower tier, work links, categories. Use -1 if \
none of that is present.

Hard rules:
- NEVER state or assume a fact that is not in the provided data: not age, not \
height, not location, not availability, not past brands, not appearance. If it is \
not given, it is unknown.
- strengths: each item must be grounded in the provided data (e.g. "Beauty tag on \
file", "Has 3 prior auditions", "Bio mentions premium lifestyle content"). No \
speculation.
- risks: real gaps a caster should verify (e.g. "No bio on file", "Follower tier \
unknown", "No prior work with this agency").
- reason: 1-2 sentences. Honest. Say "profile is thin" when it is.
- Do not rank gender/age/height/location fit — that is handled separately.

Return one entry per candidate via the tool."""


def _candidate_for_llm(t: Dict[str, Any]) -> Dict[str, Any]:
    """Only what's needed to judge character/experience. No email/phone/whatsapp."""
    return {
        "talent_id": t.get("id"),
        "categories_on_file": t.get("interested_in") or [],
        "skills_on_file": (t.get("skills") or [])[:20],
        "tags_on_file": [tg.get("name") for tg in (t.get("tags") or []) if isinstance(tg, dict) and tg.get("name")],
        "bio": (t.get("bio") or "")[:600] or None,
        "follower_tier": t.get("instagram_followers") or None,
        "has_instagram": bool(t.get("instagram_handle")),
        "work_link_count": len(t.get("work_links") or []),
        "prior_auditions_with_agency": t.get("total_submissions") or 0,
        "has_photo_on_file": bool(t.get("cover_url") or t.get("cover_thumbnail_url")),
    }


async def rank_candidates(project_ctx: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """One batched LLM call per BATCH_SIZE candidates. Returns {talent_id: ai_row}.
    Raises llm.LLMUnavailable / llm.LLMError."""
    out: Dict[str, Dict[str, Any]] = {}
    if not candidates:
        return out

    ctx_lines = [
        f"Project brand: {project_ctx.get('brand_name', '')}",
        f"Medium / usage: {project_ctx.get('medium_usage', '')}",
        f"Character we are casting: {project_ctx.get('character_summary', '')}",
    ]
    if project_ctx.get("categories"):
        ctx_lines.append(f"Desired talent categories: {', '.join(project_ctx['categories'])}")
    if project_ctx.get("competitive_brands_note"):
        ctx_lines.append(f"Competitive-brand restriction: {project_ctx['competitive_brands_note']}")
    ctx = "\n".join(ctx_lines)

    for i in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[i:i + BATCH_SIZE]
        import json
        user = (
            f"{ctx}\n\nCANDIDATES (data on file only):\n"
            + json.dumps([_candidate_for_llm(t) for t in batch], ensure_ascii=False, indent=1)
        )
        data = await llm.call_tool_json(
            system=_RANK_SYSTEM,
            user=user,
            tool_name="emit_rankings",
            tool_description="One judgement per candidate.",
            input_schema=_RANK_SCHEMA,
            max_tokens=8000,
            model=SCOUT_MODEL,
        )
        for row in (data.get("rankings") or []):
            tid = row.get("talent_id")
            if tid:
                out[tid] = row
    return out


# ---------------------------------------------------------------------------
# Assembly + verification + tiering
# ---------------------------------------------------------------------------
def _clamp(v: Any, lo: int = 0, hi: int = 100) -> Optional[int]:
    """-1 (or None / unparseable) means 'no signal' -> None (rendered 'Unknown')."""
    if v is None:
        return None
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    return max(lo, min(hi, n))


def field_verification(talent: Dict[str, Any], crit: Dict[str, Any], req_dims: Dict[str, str]) -> Dict[str, Any]:
    """DB-sourced factual panel. Never influenced by the model."""
    age = talent_age(talent)
    h = talent_height_inches(talent)
    cities = [l.get("city") for l in (talent.get("location") or []) if isinstance(l, dict) and l.get("city")]
    return {
        "gender": {
            "value": talent.get("gender") or None,
            "status": "on_file" if talent.get("gender") else "unknown",
            "verdict": req_dims.get("gender", "n/a"),
        },
        "age": {
            "value": age,
            "status": "on_file" if age is not None else "unknown",
            "verdict": req_dims.get("age", "n/a"),
        },
        "height": {
            "value": talent.get("height") or (f"{h}in" if h else None),
            "status": "on_file" if h is not None else "unknown",
            "verdict": req_dims.get("height", "n/a"),
        },
        "location": {
            "value": cities or None,
            "status": "on_file" if cities else "unknown",
            "verdict": (
                "n/a" if not crit.get("locations")
                else "unknown" if not cities
                else "match" if location_fit(talent, crit) == 100
                else "different_city"
            ),
        },
        "competitive_brand_history": {"value": None, "status": "not_tracked"},
        "availability": {"value": None, "status": "not_confirmed"},
    }


def _build_unknowns(fv: Dict[str, Any], char_fit: Optional[int], exp_fit: Optional[int], crit: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    if fv["gender"]["status"] == "unknown" and crit.get("gender"):
        out.append("Gender not on file")
    if fv["age"]["status"] == "unknown" and (crit.get("age_min") is not None or crit.get("age_max") is not None):
        out.append("Age / DOB not on file")
    if fv["height"]["status"] == "unknown" and crit.get("height_min"):
        out.append("Height not on file")
    if fv["location"]["status"] == "unknown" and crit.get("locations"):
        out.append("Location not on file")
    if crit.get("competitive_brands_note"):
        out.append("Competitive-brand history is not tracked in Talentgram — verify manually")
    out.append("Availability for the shoot dates is not confirmed")
    if char_fit is None:
        out.append("Character fit not assessed — no bio or photo on file")
    if exp_fit is None:
        out.append("No experience signal on file (auditions / tags / links)")
    return out


def assemble_result(talent: Dict[str, Any], crit: Dict[str, Any], ai_row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    ai_row = ai_row or {}
    req_score, req_dims = requirement_fit(talent, crit)
    loc_score = location_fit(talent, crit)
    conf_score = profile_confidence(talent)
    char_fit = _clamp(ai_row.get("character_fit"))
    exp_fit = _clamp(ai_row.get("experience_fit"))

    components = {
        "requirement_fit": req_score,
        "character_fit": char_fit,
        "location_fit": loc_score,
        "experience_fit": exp_fit,
        "profile_confidence": conf_score,
    }
    present = {k: v for k, v in components.items() if v is not None}
    if present:
        wsum = sum(_WEIGHTS[k] for k in present)
        base_overall = sum(v * _WEIGHTS[k] for k, v in present.items()) / wsum
    else:
        base_overall = None

    # ------------------------------------------------------------------
    # Evidence coverage — a talent scored off 1-2 known things must not
    # read like a fully-profiled 90+ match. `evidence` is a soft count of
    # how many of the four real fit signals we could actually evaluate
    # (a requirement_fit built off a single dimension only counts half);
    # profile_confidence adds at most 0.5 on its own. `overall` is the
    # weighted score scaled down by how thin that coverage is.
    # ------------------------------------------------------------------
    evaluated_req_dims = sum(1 for v in req_dims.values() if v in ("match", "near", "mismatch"))
    evidence = 0.0
    if req_score is not None:
        evidence += 1.0 if evaluated_req_dims >= 2 else 0.5
    evidence += 1.0 if char_fit is not None else 0.0
    evidence += 1.0 if loc_score is not None else 0.0
    evidence += 1.0 if exp_fit is not None else 0.0
    evidence += min(0.5, (conf_score or 0) / 200.0)

    if evidence >= 2.5:
        coverage = 1.0
    elif evidence >= 1.75:
        coverage = 0.80
    elif evidence >= 1.0:
        coverage = 0.62
    else:
        coverage = 0.42

    overall = round(base_overall * coverage) if base_overall is not None else None

    fv = field_verification(talent, crit, req_dims)
    unknowns = _build_unknowns(fv, char_fit, exp_fit, crit)

    # sanitise model strengths: drop any that assert a hard attribute the DB
    # doesn't have (defence in depth — the factual panel is the real guard).
    raw_strengths = [str(s).strip() for s in (ai_row.get("strengths") or []) if str(s).strip()]
    strengths: List[str] = []
    for s in raw_strengths[:6]:
        low = s.lower()
        if fv["age"]["status"] == "unknown" and re.search(r"\bage\b|\byears? old\b|\b\d{2}\s*(?:yo|y/o)\b", low):
            continue
        if fv["height"]["status"] == "unknown" and re.search(r"height|tall|\d\s*'|\bcm\b", low):
            continue
        if fv["location"]["status"] == "unknown" and crit.get("locations") and any(c.lower() in low for c in crit["locations"]):
            continue
        strengths.append(s)

    risks = [str(r).strip() for r in (ai_row.get("risks") or []) if str(r).strip()][:6]

    return {
        "talent_id": talent.get("id"),
        "name": talent.get("name"),
        "image_url": talent.get("cover_thumbnail_url") or talent.get("cover_url"),
        "instagram_handle": talent.get("instagram_handle"),
        "overall": overall,
        "base_overall": round(base_overall) if base_overall is not None else None,
        "evidence_count": round(evidence, 1),
        "evidence_coverage": coverage,
        "scores": {
            "requirement_fit": req_score,
            "character_fit": char_fit,
            "location_fit": loc_score,
            "experience_fit": exp_fit,
            "profile_confidence": conf_score,
        },
        "confidence": round(float(ai_row.get("confidence", 0.0) or 0.0), 2),
        "requirement_dimensions": req_dims,
        "field_verification": fv,
        "strengths": strengths,
        "risks": risks,
        "unknowns": unknowns,
        "reason": str(ai_row.get("reason") or "").strip() or "Not enough profile data to explain a strong recommendation.",
        "ai_ranked": bool(ai_row),
    }


def tier(result: Dict[str, Any]) -> str:
    """Top    — strong fit on solid evidence, no missing hard field.
    Strong — good fit, or fit dampened by travel / a near miss.
    Possible — an explicit off-spec value, a missing hard field, or too
               little verified data to stand behind a strong claim."""
    overall = result.get("overall")
    req = result["scores"]["requirement_fit"]
    dim_vals = set(result["requirement_dimensions"].values())
    loc_verdict = result["field_verification"]["location"]["verdict"]
    evidence = result.get("evidence_count", 0)

    hard_unknowns = any(
        u.startswith(("Gender not", "Age", "Height not", "Location not")) for u in result.get("unknowns", [])
    )
    if "mismatch" in dim_vals or hard_unknowns or evidence < 1.75:
        return "possible"

    dampened = "near" in dim_vals or loc_verdict == "different_city"
    if overall is not None and overall >= 80 and (req is None or req >= 75) and not dampened and evidence >= 2.5:
        return "top"
    if overall is not None and overall >= 62:
        return "strong"
    return "possible"
