"""Personal Assistant — the `update` command's vocabulary: which profile items it can change, how a typed or spoken command is split into
per-talent groups, and how a value is read, checked and shown. Pure functions, no I/O.

Only what the Global Talent page really stores and the existing PUT /talents/{id} + POST /talents/{id}/media already accept is supported:
  fields : age, height, location, WhatsApp group name, Instagram handle, Instagram followers
  media  : introduction video ("video"), Indian look ("indian"), Western look ("western"), additional portfolio ("portfolio")
Anything else ("update her email / phone / name / date of birth / bio …") is reported as unsupported — never guessed into another field.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ------------------------------------------------------------------------------------------------------------ vocabulary
MEDIA = {
    "video": "Introduction video",
    "indian": "Indian look pictures",
    "western": "Western look pictures",
    "portfolio": "Additional portfolio",
}
FIELDS = {
    "age": "Age",
    "height": "Height",
    "location": "Location",
    "whatsapp_group_name": "WhatsApp group name",
    "instagram_handle": "Instagram handle",
    "instagram_followers": "Instagram followers",
}
# Existing admin-page rules (TalentEdit.jsx / core.py): the SAME limits and formats, so what passes here never fails server-side.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/jpg", "image/heic", "image/heif")
VIDEO_TYPES = ("video/mp4", "video/quicktime")
_EXT_OK = {"video": r"\.(mp4|mov)$", "image": r"\.(jpe?g|png|webp|heic|heif)$"}

_ITEM_PATTERNS: List[Tuple[str, str, str]] = [        # (kind, key, regex) — longest / most specific first
    ("media", "video", r"intro(?:duction)?\s+video|video"),
    ("media", "indian", r"indian\s+(?:look\s+)?(?:pictures?|photos?|images?|looks?|portfolio)|indian\s+look"),
    ("media", "western", r"western\s+(?:look\s+)?(?:pictures?|photos?|images?|looks?|portfolio)|western\s+look"),
    ("media", "portfolio", r"(?:additional|extra)\s+(?:portfolio|pictures?|photos?|images?)(?:\s+(?:pictures?|photos?|images?))?|portfolio(?:\s+(?:pictures?|photos?|images?))?"),
    ("field", "whatsapp_group_name", r"whats\s*app\s+group(?:\s+name)?|group\s+name"),
    ("field", "instagram_followers", r"insta(?:gram)?\s+followers|followers"),
    ("field", "instagram_handle", r"insta(?:gram)?(?:\s+(?:handle|id|username|account))?"),
    ("field", "height", r"height"),
    ("field", "age", r"age"),
    ("field", "location", r"locations?|city|cities"),
    ("unsupported", "other", r"e-?mail(?:\s+(?:address|id))?|phone(?:\s+number)?|mobile(?:\s+number)?|contact\s+number|number|name|date\s+of\s+birth|d\.?o\.?b\.?|birthday|bio|status|tags?|notes?|skills?|gender|ethnicity|cover(?:\s+(?:picture|photo|image))?|pictures?|photos?|images?"),
]
_ITEM_RE = re.compile(r"(?<![\w'])(?:" + "|".join(f"(?P<g{i}>{rx})" for i, (_k, _n, rx) in enumerate(_ITEM_PATTERNS)) + r")(?![\w])", re.I)


def find_items(text: str) -> List[Tuple[int, int, str, str]]:
    """→ [(start, end, kind, key)] — every item keyword in `text`, left to right (one match never overlaps the next)."""
    out = []
    for m in _ITEM_RE.finditer(text or ""):
        for i, (kind, key, _rx) in enumerate(_ITEM_PATTERNS):
            if m.group(f"g{i}") is not None:
                out.append((m.start(), m.end(), kind, key))
                break
    return out


_NAME_WORDS = r"[A-Za-z][A-Za-z.\-]*"
_POSSESSIVE = re.compile(rf"(?P<n>{_NAME_WORDS}(?:\s+(?:and\s+|,\s*)?{_NAME_WORDS}){{0,7}})\s*(?:'|’)s\b", re.I)
_SPLIT_NAMES = re.compile(r"\s*(?:,|\band\b|&)\s*", re.I)
_LEAD = re.compile(r"^\s*(?:please\s+)?update\s+", re.I)             # only "update": "change X to Approved" stays a pipeline move


def is_update(text: str) -> bool:
    return bool(_LEAD.match(text or ""))


def _clean_names(raw: str) -> List[str]:
    return [n.strip(" .,") for n in _SPLIT_NAMES.split(raw or "") if n.strip(" .,")]


def _items_with_values(tail: str) -> List[Dict[str, Any]]:
    """Items in one talent's tail text ("age to 24 and location to Mumbai", "introduction video and age"), each with its raw value.
    A value runs from after "to/as/is/=" to the start of the NEXT item keyword that follows an "and" / comma. The WhatsApp group name is
    different: it takes EVERYTHING up to the end (or the closing quote) because a group name may contain words like "and" or "age"."""
    items = find_items(tail)
    out: List[Dict[str, Any]] = []
    i = 0
    while i < len(items):
        s, e, kind, key = items[i]
        nxt = items[i + 1][0] if i + 1 < len(items) else len(tail)
        seg = tail[e:nxt]
        raw = None
        m = re.match(r"\s*(?:to|as|is|=|:|should\s+be|with)\s+(?P<v>.+)$", seg, re.S | re.I)
        if key == "whatsapp_group_name":
            m = re.match(r"\s*(?:to|as|is|=|:|should\s+be|with)\s+(?P<v>.+)$", tail[e:], re.S | re.I)
            if m:
                v = m.group("v").lstrip()
                q = {'"': '"', "\u201c": "\u201d", "'": "'", "\u2018": "\u2019"}.get(v[:1])
                close = v.find(q, 1) if q else -1
                if q and close > 0:                       # a quoted name is taken exactly, and more items may follow it
                    raw = v[1:close]
                    nxt = e + (len(tail[e:]) - len(v)) + close + 1
                else:                                     # unquoted: everything to the end (a name may contain "and", "age" …); only a sentence-final "." goes
                    raw = re.sub(r"(?<=[^.\s])\.\s*$", "", v.rstrip())
                    nxt = len(tail)
        elif m:
            raw = re.sub(r"\s*(?:,|\band\b)\s*$", "", m.group("v"), flags=re.I).strip()
            raw = re.sub(r"\s*[.!?]+$", "", raw)           # the sentence's own full stop is not part of a value
        out.append({"kind": kind, "key": key, "raw": raw if raw not in (None, "") else None, "said": tail[s:e].strip()})
        if key == "whatsapp_group_name" and raw is not None and nxt >= len(tail):
            break
        i += 1
        while i < len(items) and items[i][0] < nxt:
            i += 1
    # drop duplicates (the same item said twice keeps the last value)
    dedup: Dict[str, Dict[str, Any]] = {}
    for it in out:
        dedup[f"{it['kind']}:{it['key']}"] = it
    return list(dedup.values())


def _unquote(v: str) -> str:
    v = v.strip()
    for a, b in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
        if len(v) >= 2 and v[0] == a and v[-1] == b:
            return v[1:-1]
    return v


def parse_update(text: str) -> List[Dict[str, Any]]:
    """→ groups [{"names": [..], "ref": None|"previous", "items": [{kind,key,raw,said}]}] for one `update …` clause, or [] when it is not one.
    One group per talent-list; a group applies the SAME items to each of its talents ("Angela and Priya's locations to Mumbai")."""
    t = (text or "").strip()
    m = _LEAD.match(t)
    if not m:
        return []
    body = t[m.end():].strip()
    groups: List[Dict[str, Any]] = []
    # form 2:  "the introduction video of Angela Kumar", "update age for Angela to 24"
    f2 = re.match(r"^(?:the\s+)?(?P<items>.+?)\s+(?:of|for)\s+(?P<names>[A-Za-z][^,]*?)(?:\s+(?:to|as|is)\s+(?P<val>.+))?$", body, re.I)
    poss = list(_POSSESSIVE.finditer(body))
    if not poss and f2 and find_items(f2.group("items")):
        items = _items_with_values(f2.group("items") + (f" to {f2.group('val')}" if f2.group("val") else ""))
        names = _clean_names(f2.group("names"))
        return [{"names": names, "ref": None, "items": items}] if names and items else []
    # pronoun form: "update her age to 24"
    pm = re.match(r"^(?:her|his|their)\s+(?P<tail>.+)$", body, re.I)
    if pm:
        return [{"names": [], "ref": "previous", "items": _items_with_values(pm.group("tail"))}]
    if not poss:
        return []
    # form 1: "<names>'s <items> [and <names>'s <items>]…"
    cuts = []                                         # (names_text, tail_start)
    cursor = 0
    for k, pm_ in enumerate(poss):
        names_end = pm_.end("n")
        region = body[cursor:names_end]
        its = find_items(region)
        if its:                                       # item words before this name belong to the previous group
            names_start = cursor + its[-1][1]
            names_text = body[names_start:names_end]
            names_text = re.sub(r"^\s*(?:and|,|&)\s+", "", names_text, flags=re.I)
        else:
            names_start, names_text = cursor, body[cursor:names_end]
        cuts.append((names_text.strip(), names_start, pm_.end()))
        cursor = pm_.end()
    for k, (names_text, names_start, tail_start) in enumerate(cuts):
        tail_end = cuts[k + 1][1] if k + 1 < len(cuts) else len(body)
        items = _items_with_values(body[tail_start:tail_end])
        names = _clean_names(names_text)
        if names and items:
            groups.append({"names": names, "ref": None, "items": items})
    return groups


# ------------------------------------------------------------------------------------------------------------- values
class BadValue(ValueError):
    pass


def parse_height(raw: str) -> Tuple[str, str]:
    """→ (stored value in the profile's own format 5'7", how it was understood). "5 feet 7 inches", "5ft 7", "5'7", "170 cm"."""
    s = (raw or "").strip().lower().replace("’", "'").replace("”", '"').replace("“", '"')
    m = re.match(r"^(\d)\s*(?:'|ft|feet|foot|f)\s*(\d{1,2})?\s*(?:\"|in|inch|inches)?\.?$", s)
    if m:
        ft, inch = int(m.group(1)), int(m.group(2) or 0)
        if not (3 <= ft <= 7) or inch > 11:
            raise BadValue(f"“{raw}” isn't a realistic height (feet 3–7, inches 0–11).")
        return f'{ft}\'{inch}"', f'{ft} ft {inch} in'
    m = re.match(r"^(\d{3}(?:\.\d+)?)\s*(?:cm|centimet(?:er|re)s?)?$", s)
    if m:
        cm = float(m.group(1))
        if not (90 <= cm <= 240):
            raise BadValue(f"“{raw}” isn't a realistic height in cm.")
        total = round(cm / 2.54)
        return f"{total // 12}'{total % 12}\"", f"{m.group(1)} cm ≈ {total // 12} ft {total % 12} in (rounded)"
    raise BadValue(f"I couldn't read “{raw}” as a height. Say it like “5 feet 7 inches”, “5'7” or “170 cm”.")


def parse_age(raw: str) -> int:
    m = re.match(r"^\s*(\d{1,3})\s*(?:years?(?:\s+old)?|yrs?|y\.?o\.?)?\s*\.?$", raw or "", re.I)
    if not m:
        raise BadValue(f"I couldn't read “{raw}” as an age. Give a number, e.g. 24.")
    age = int(m.group(1))
    if not (0 <= age <= 120):
        raise BadValue("Age must be between 0 and 120.")
    return age


def parse_location(raw: str) -> List[Dict[str, str]]:
    """City[, Country] — several places separated by "and", ";" or "/". A city without a country is stored with India (the profile page's
    own default), and the approval shows exactly that."""
    parts = [p.strip(" .") for p in re.split(r"\s*(?:;|/|\band\b|&)\s*", raw or "", flags=re.I) if p.strip(" .")]
    out = []
    for p in parts:
        if "," in p:
            bits = [b.strip() for b in p.split(",") if b.strip()]
            out.append({"city": bits[0], "country": bits[-1] if len(bits) > 1 else "India"})
        else:
            out.append({"city": p, "country": "India"})
    if not out:
        raise BadValue("Which city?")
    return out


def location_text(loc: List[Dict[str, str]]) -> str:
    return "; ".join(f"{x.get('city')}, {x.get('country')}" for x in loc) or "—"


def parse_value(key: str, raw: str) -> Tuple[Any, str, Optional[str]]:
    """→ (value to store, text shown at approval, note about how it was interpreted or None). Raises BadValue with a user-facing reason."""
    raw = raw if raw is not None else ""
    if key == "age":
        a = parse_age(raw)
        return a, str(a), None
    if key == "height":
        v, how = parse_height(raw)
        return v, v, f"Understood as {how}" if how.replace(" ft ", "'").replace(" in", '"') != v else None
    if key == "location":
        loc = parse_location(raw)
        note = "Country not given — India assumed" if any("," not in p for p in re.split(r"\s*(?:;|/|\band\b|&)\s*", raw, flags=re.I) if p.strip()) else None
        return loc, location_text(loc), note
    if key == "whatsapp_group_name":
        v = raw  # EXACT: no trimming of inner spacing, no case change, no autocorrect (only the surrounding whitespace / quotes go)
        v = _unquote(v.strip("\n\r\t"))
        if not v.strip():
            raise BadValue("What is the exact WhatsApp group name?")
        return v, v, None
    if key == "instagram_handle":
        from core import normalize_instagram_handle
        h = normalize_instagram_handle(raw)
        if not h or not re.match(r"^[A-Za-z0-9._]{1,30}$", h):
            raise BadValue(f"“{raw}” doesn't look like an Instagram handle.")
        return h, "@" + h, None if h == raw.strip() else f"Read as @{h}"
    if key == "instagram_followers":
        v = raw.strip()
        if not v or len(v) > 20 or not re.match(r"^[0-9][0-9.,]*\s*[kKmM]?$", v):
            raise BadValue("Give a follower count like 12000 or 12.5k.")
        return v, v, None
    raise BadValue("That isn't something I can update.")


# ---------------------------------------------------------------------------------------------------------- file checks
def check_file(category: str, name: str, size: int, ctype: str) -> Optional[str]:
    """None if the file is acceptable for this media category under the admin page's own rules, else the reason."""
    video = category == "video"
    cap = MAX_VIDEO_BYTES if video else MAX_IMAGE_BYTES
    if size <= 0:
        return f"{name} is empty."
    if size > cap:
        return f"{name} is too large — the limit is {cap // (1024 * 1024)} MB."
    allowed = VIDEO_TYPES if video else IMAGE_TYPES
    ext_ok = bool(re.search(_EXT_OK["video" if video else "image"], name or "", re.I))
    if (ctype or "") not in allowed and not (not ctype and ext_ok):
        return f"{name} isn't an accepted format — use {'MP4 or MOV' if video else 'JPEG, PNG, WEBP or HEIC'}."
    return None


def sniff(category: str, head: bytes) -> bool:
    """Magic-byte check of the real bytes (a renamed file must not pass). Images: JPEG / PNG / WEBP / HEIC-HEIF. Video: MP4 / MOV (ftyp or moov/mdat)."""
    if category == "video":
        return head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip")
    return (head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" or (head[:4] == b"RIFF" and head[8:12] == b"WEBP") or head[4:8] == b"ftyp")
