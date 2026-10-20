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
    ("media", "video", r"intro(?:duction|ductory)?(?:\s+videos?)?|videos?"),
    ("media", "indian", r"indian\s+(?:(?:look|portfolio)\s+)?(?:pictures?|photos?|images?)|indian\s+(?:looks?|portfolio)"),
    ("media", "western", r"western\s+(?:(?:look|portfolio)\s+)?(?:pictures?|photos?|images?)|western\s+(?:looks?|portfolio)"),
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


_SPLIT_NAMES = re.compile(r"\s*(?:,|\band\b|&)\s*", re.I)
_LEAD = re.compile(r"^\s*(?:please\s+)?update\s+", re.I)             # only "update": "change X to Approved" stays a pipeline move
_NUM_WORD = (r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
             r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)")
_VALUE_MARK = re.compile(r"^\s*(?:(?:to|as|is|=|:|should\s+be|with)\s+|(?=\d|" + _NUM_WORD + r"\b))", re.I)
_CONNECT_ONLY = re.compile(r"^[\s,&.!?]*(?:\b(?:and|also|then|plus)\b[\s,&.!?]*)*$", re.I)
_POLITE = re.compile(r"\b(?:please|thanks|thank\s+you|for\s+me|now)\b", re.I)
_NAMEISH = re.compile(r"^[A-Za-z][A-Za-z.\-']*(?:\s+[A-Za-z][A-Za-z.\-']*){0,7}$")


def is_update(text: str) -> bool:
    return bool(_LEAD.match(text or ""))


def _clean_names(raw: str) -> List[str]:
    return [n.strip(" .,") for n in _SPLIT_NAMES.split(raw or "") if n.strip(" .,")]


def _name_phrase(gap: str) -> Optional[str]:
    """The text between two items read as TALENT NAMES: connectors / "the" / a trailing possessive "'s" removed. → the cleaned phrase,
    "" when there is nothing, None when it does not look like names (digits, symbols, too long) — the caller then fails closed."""
    g = re.sub(r"(?:'|\u2019)s\b", "", gap or "")
    g = re.sub(r"^[\s,&]*(?:(?:and|also|then|plus)\b[\s,&]*)*", "", g, flags=re.I)
    g = re.sub(r"^(?:the\s+)", "", g.strip(" ,.&"), flags=re.I)
    g = re.sub(r"[\s,&]*(?:\b(?:and|also|then|plus)\b[\s,&]*)*$", "", g, flags=re.I).strip(" ,.&")
    if not g:
        return ""
    return g if _NAMEISH.match(g.replace(" and ", " ").replace(",", " ")) and len(g.split()) <= 10 else None


def _unquote(v: str) -> str:
    v = v.strip()
    for a, b in (('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019")):
        if len(v) >= 2 and v[0] == a and v[-1] == b:
            return v[1:-1]
    return v


def parse_update(text: str) -> List[Dict[str, Any]]:
    """→ groups [{"names": [..], "ref": None|"previous", "items": [{kind,key,raw,said}]}] for one `update …` clause, or [] when it cannot be
    read COMPLETELY (a clause is never half-understood: leftover words that are neither names, items, values nor politeness make it [] and the
    assistant says it did not understand).

    One left-to-right scan, no possessive or word order required:
        "Kashvi Kalani's introduction video"   "Kashvi Kalani introduction video"   "Kashvi intro"   "the introduction video of Kashvi"
        "Angela and Priya's locations to Mumbai"   "Angela's age to 24 and location to Mumbai"   "Angela intro video and Priya Western photos"
    The words BEFORE the first item are the talent names — an item word is never part of a name. Text between two items is either the previous
    field's value (after to / as / is / =, or starting with a digit) or the NEXT talents' names. Groups apply their items to each of their talents.
    The WhatsApp group name takes its value verbatim (to the closing quote or the end) because a name may contain words like "and" or "age"."""
    t = (text or "").strip()
    m = _LEAD.match(t)
    if not m:
        return []
    body = t[m.end():].strip()
    items = find_items(body)
    if not items:
        return []
    pm = re.match(r"^(?:her|his|their)\s+", body, re.I)               # "update her age to 24" — the talent just discussed
    if pm:
        its, _ = _scan_items(body[pm.end():])
        return [{"names": [], "ref": "previous", "items": its}] if its else []
    head = body[: items[0][0]]
    if not re.sub(r"^the\b\s*", "", head.strip(), flags=re.I):          # items first: "the introduction video of Angela [to …]"
        fm = re.match(r"^(?P<items>.+?)\s+(?:of|for)\s+(?P<names>[A-Za-z][^,]*?)(?:\s+(?P<tail>(?:to|as|is)\s+.+))?$", body, re.I)
        nm = _name_phrase(fm.group("names")) if fm else None
        if not fm or not nm or not find_items(fm.group("items")):
            return []
        its, _ = _scan_items(fm.group("items") + (" " + fm.group("tail") if fm.group("tail") else ""))
        return [{"names": _clean_names(nm), "ref": None, "items": its}] if its else []
    first = _name_phrase(head)
    if not first:
        return []
    groups: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {"names": _clean_names(first), "ref": None, "items": []}
    skip_until = 0
    for i, (s_, e_, kind, key) in enumerate(items):
        if s_ < skip_until:                                  # inside a verbatim group name
            continue
        nxt = next((x[0] for x in items[i + 1:] if x[0] >= max(skip_until, e_)), len(body))
        gap = body[e_:nxt]
        item = {"kind": kind, "key": key, "raw": None, "said": body[s_:e_].strip()}
        residue = gap                                         # what is left after this item (and its value) up to the next item
        vm = _VALUE_MARK.match(body[e_:]) if kind in ("field", "unsupported") else None      # an unsupported item's value is read too, so it is reported, not mis-read as names
        if key == "whatsapp_group_name" and vm:
            v = body[e_ + vm.end():].lstrip()
            q = {'"': '"', "\u201c": "\u201d", "'": "'", "\u2018": "\u2019"}.get(v[:1])
            close = v.find(q, 1) if q else -1
            if q and close > 0:                               # a quoted name is taken exactly, and more items may follow it
                item["raw"], vend = v[1:close], e_ + (len(body[e_:]) - len(v)) + close + 1
            else:                                             # unquoted: everything to the end; only a sentence-final "." goes
                item["raw"], vend = re.sub(r"(?<=[^.\s])\.\s*$", "", v.rstrip()), len(body)
            skip_until = vend
            nxt = next((x[0] for x in items if x[0] >= vend), len(body))
            residue = body[vend:nxt]
        elif vm:
            val = gap[vm.end():] if _VALUE_MARK.match(gap) else ""
            nxt_is_last = not any(x[0] >= nxt and x[0] > e_ for x in items[i + 1:])
            # "… to Mumbai and Priya Sharma's age" — a possessive name phrase at the end of the value opens the next group
            pm2 = None if nxt_is_last else re.search(r"(?:\s+and\s+|\s*,\s*)(?P<nm>[A-Za-z][A-Za-z.\-]*(?:\s+[A-Za-z][A-Za-z.\-]*){0,4})(?:'|\u2019)s\s*$", val, re.I)
            new_names = pm2.group("nm") if pm2 else None
            if pm2:
                val = val[: pm2.start()]
            val = re.sub(r"\s*(?:,|\band\b)\s*$", "", val, flags=re.I).strip()
            item["raw"] = re.sub(r"\s*[.!?]+$", "", val) or None
            cur["items"].append(item)
            if new_names:
                groups.append(cur)
                cur = {"names": _clean_names(new_names), "ref": None, "items": []}
            continue
        cur["items"].append(item)
        residue = _POLITE.sub("", residue)
        if _CONNECT_ONLY.match(residue) or not residue.strip(" ,.&!?"):
            continue
        if nxt >= len(body):
            return []                                         # trailing words that are not names, an item or a value: not understood
        nm2 = _name_phrase(residue)
        if not nm2:
            return []
        groups.append(cur)
        cur = {"names": _clean_names(nm2), "ref": None, "items": []}
    groups.append(cur)
    return groups if all(g["names"] and g["items"] for g in groups) else []


def _scan_items(text: str):
    """Items (with values) of a tail where the names are already known (the "of Angela" and "her …" forms). → (items, residue) or None."""
    its = find_items(text)
    out: List[Dict[str, Any]] = []
    for k, (s_, e_, kind, key) in enumerate(its):
        nxt = its[k + 1][0] if k + 1 < len(its) else len(text)
        gap = text[e_:nxt]
        item = {"kind": kind, "key": key, "raw": None, "said": text[s_:e_].strip()}
        if kind in ("field", "unsupported"):
            vm = _VALUE_MARK.match(gap)
            if vm:
                val = gap[vm.end():] if key == "whatsapp_group_name" else re.sub(r"\s*[.!?]+$", "", re.sub(r"\s*(?:,|\band\b)\s*$", "", gap[vm.end():], flags=re.I).strip())
                item["raw"] = (_unquote(val.strip()) if key == "whatsapp_group_name" else val) or None
                out.append(item)
                if key == "whatsapp_group_name":
                    break
                continue
        out.append(item)
    return out, ""


# ------------------------------------------------------------------------------------------------------------- values
class BadValue(ValueError):
    pass


_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}


def spoken_to_digits(raw: str) -> str:
    """Spoken numbers as digits so a speech engine that writes words is understood: "five feet seven" → "5 feet 7", "twenty four" → "24",
    "one hundred seventy" → "170". Only whole number words are read; anything else is left exactly as it was (and then checked as usual)."""
    toks = re.findall(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]", (raw or "").replace("-", " "))
    out: List[str] = []
    i = 0
    while i < len(toks):
        w = toks[i].lower()
        if w in _ONES or w in _TENS or (w == "hundred"):
            n, j, seen = 0, i, False
            while j < len(toks):
                x = toks[j].lower()
                if x in _TENS and not seen or (x in _TENS and n % 100 == 0 and n):
                    n += _TENS[x]
                elif x in _ONES and _ONES[x] < 10 and n % 10 == 0 and not (n % 100 and n % 100 < 10):
                    n += _ONES[x]
                elif x in _ONES and not n:
                    n += _ONES[x]
                elif x == "hundred" and 0 < n < 10:
                    n *= 100
                elif x == "and" and n >= 100:
                    j += 1
                    continue
                else:
                    break
                seen = True
                j += 1
            if seen:
                out.append(str(n))
                i = j
                continue
        out.append(toks[i])
        i += 1
    return " ".join(out)


def parse_height(raw: str) -> Tuple[str, str]:
    """→ (stored value in the profile's own format 5'7", how it was understood). "5 feet 7 inches", "5ft 7", "5'7", "170 cm", "five feet seven"."""
    raw0 = raw
    if re.search(r"[A-Za-z]", raw or ""):
        spoken = spoken_to_digits(raw)
        m2 = re.fullmatch(r"([3-7])\s+(\d{1,2})", spoken.strip())                  # "five seven" — feet then inches, said without the units
        raw = f"{m2.group(1)}'{m2.group(2)}" if m2 else spoken
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
    raise BadValue(f"I couldn't read “{raw0}” as a height. Say it like “5 feet 7 inches”, “5'7” or “170 cm”.")


def parse_age(raw: str) -> int:
    if re.search(r"[A-Za-z]{3,}", raw or "") and re.match(r"^\s*" + _NUM_WORD, raw or "", re.I):
        raw = re.sub(r"\s*(?:years?(?:\s+old)?)?\s*$", "", spoken_to_digits(raw), flags=re.I)
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
