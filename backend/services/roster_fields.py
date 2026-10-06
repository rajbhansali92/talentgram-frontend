"""Roster field registry — which Global Talent fields a roster may show.

The Global Talent profile stays the single source of truth: this module only
decides (a) which EXISTING, public/professional-safe profile fields can be
offered, (b) their order and grouping, and (c) how a raw profile value is
rendered as roster text. Nothing here writes to a talent.

Never offered (private/internal): phone, email, notes, internal ids, tokens,
availability/budget (project/submission data), financial or operational fields.

Defaults: Name, Age, Height, Location, Instagram, Introduction Video are ON;
everything else is OFF. A roster stores its own complete {key: bool} map; a
roster created before this feature has none and gets exactly these defaults, so
it renders as it always did.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Languages are stored inside the talent's flat `skills` list (the Skills
# selector's "Languages" category). Mirrors frontend/src/components/
# SkillsSelector.jsx SKILLS_CATEGORIES.Languages — a test asserts they match.
LANGUAGE_SKILLS = [
    "English", "Hindi", "Spanish", "French", "Mandarin Chinese", "Japanese", "Russian",
    "German", "Arabic", "Marathi", "Gujarati", "Punjabi", "Tamil", "Telugu", "Kannada",
    "Malayalam", "Bengali", "Urdu", "Other",
]

# Mirrors frontend/src/lib/talentSchema.js ETHNICITY_OPTIONS / GENDER_OPTIONS.
ETHNICITY_LABELS = {
    "indian": "Indian", "south_asian": "South Asian", "east_asian": "East Asian", "caucasian": "Caucasian",
    "african": "African", "middle_eastern": "Middle Eastern", "latino": "Latino", "mixed": "Mixed", "other": "Other",
}
GENDER_LABELS = {"female": "Female", "male": "Male", "non_binary": "Non-binary"}  # prefer_not_say is never shown

# (key, label, group)
FIELDS: List[Dict[str, Any]] = [
    {"key": "name", "label": "Name", "group": "basic", "default": True},
    {"key": "age", "label": "Age", "group": "basic", "default": True},
    {"key": "height", "label": "Height", "group": "basic", "default": True},
    {"key": "location", "label": "Location", "group": "basic", "default": True},
    {"key": "gender", "label": "Gender", "group": "basic", "default": False},
    {"key": "ethnicity", "label": "Ethnicity", "group": "basic", "default": False},
    {"key": "instagram", "label": "Instagram", "group": "social", "default": True},
    {"key": "instagram_followers", "label": "Instagram Followers", "group": "social", "default": False},
    {"key": "skills", "label": "Skills & Special Abilities", "group": "professional", "default": False},
    {"key": "languages", "label": "Languages", "group": "professional", "default": False},
    {"key": "intro_video", "label": "Introduction Video", "group": "media", "default": True},
]
GROUPS = [
    {"key": "basic", "label": "Basic information"},
    {"key": "social", "label": "Social"},
    {"key": "professional", "label": "Professional"},
    {"key": "media", "label": "Media"},
]
FIELD_KEYS = [f["key"] for f in FIELDS]
DEFAULT_FIELDS: Dict[str, bool] = {f["key"]: f["default"] for f in FIELDS}

# Which raw Global Talent attributes each field is rendered from. The roster's Mongo
# projection is derived from THIS map, so a field can never be selectable yet unfetched.
SOURCE_KEYS: Dict[str, List[str]] = {
    "name": ["name"], "age": ["age", "dob"], "height": ["height"], "location": ["location"],
    "gender": ["gender"], "ethnicity": ["ethnicity"], "instagram": ["instagram_handle"],
    "instagram_followers": ["instagram_followers"], "skills": ["skills"], "languages": ["skills"],
    "intro_video": ["media"],
}


def projection() -> Dict[str, int]:
    keys = {"id", "status", "media"}  # always: identity, lifecycle guard, roster images
    for fk in FIELD_KEYS:
        keys.update(SOURCE_KEYS[fk])
    return {"_id": 0, **{k: 1 for k in sorted(keys)}}


# Display label used on the page (shorter than the picker label where helpful).
INFO_LABELS = {
    "age": "Age", "height": "Height", "location": "Location", "gender": "Gender", "ethnicity": "Ethnicity",
    "instagram": "Instagram", "instagram_followers": "Followers", "skills": "Skills", "languages": "Languages",
}
# Long free-text style values get their own full-width row.
BLOCK_KEYS = {"skills", "languages"}


def registry() -> Dict[str, Any]:
    """What the admin UI renders (single source of truth for it)."""
    return {
        "groups": [{**g, "fields": [{"key": f["key"], "label": f["label"], "default": f["default"]}
                                    for f in FIELDS if f["group"] == g["key"]]} for g in GROUPS],
        "defaults": dict(DEFAULT_FIELDS),
    }


def normalize_fields(raw: Optional[dict]) -> Dict[str, bool]:
    """Complete {key: bool} map. A missing/non-dict config -> the defaults; an
    unknown key is dropped; a known key with a non-bool value falls back to its
    default. Never raises."""
    out = dict(DEFAULT_FIELDS)
    if isinstance(raw, dict):
        for k in FIELD_KEYS:
            if isinstance(raw.get(k), bool):
                out[k] = raw[k]
    return out


def has_stored_fields(roster: Optional[dict]) -> bool:
    return isinstance(((roster or {}).get("fields")), dict)


def _clean(s: Any) -> str:
    return " ".join(str(s).split()) if s is not None else ""


def info_items(talent: dict, fields: Dict[str, bool], format_location, instagram_handle) -> List[dict]:
    """Ordered roster info items for ONE talent. A field that is enabled but has
    no value for this talent is simply omitted (no 'N/A', no empty label)."""
    items: List[dict] = []

    def add(key: str, value: str, href: Optional[str] = None) -> None:
        value = _clean(value)
        if value:
            it = {"key": key, "label": INFO_LABELS[key], "value": value}
            if href:
                it["href"] = href
            if key in BLOCK_KEYS:
                it["block"] = True
            items.append(it)

    if fields.get("age") and talent.get("age") not in (None, ""):
        add("age", talent["age"])
    if fields.get("height"):
        add("height", talent.get("height"))
    if fields.get("location") and talent.get("location"):
        loc = format_location(talent["location"])
        add("location", "" if loc == "-" else loc)
    if fields.get("gender"):
        add("gender", GENDER_LABELS.get(str(talent.get("gender") or "").lower(), ""))
    if fields.get("ethnicity"):
        e = str(talent.get("ethnicity") or "").strip()
        add("ethnicity", ETHNICITY_LABELS.get(e.lower(), e.replace("_", " ").title()) if e else "")
    if fields.get("instagram"):
        h = instagram_handle(talent.get("instagram_handle"))
        if h:
            add("instagram", f"@{h}", f"https://www.instagram.com/{h}/")
    if fields.get("instagram_followers"):
        add("instagram_followers", talent.get("instagram_followers"))
    skills = [s for s in (talent.get("skills") or []) if isinstance(s, str) and s.strip()]
    langs = set(LANGUAGE_SKILLS)
    if fields.get("skills"):
        add("skills", ", ".join(s for s in skills if s not in langs))
    if fields.get("languages"):
        add("languages", ", ".join(s for s in skills if s in langs))
    return items
