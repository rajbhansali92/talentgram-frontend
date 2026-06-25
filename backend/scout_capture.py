"""AI Scout Capture Engine — FREE, fully self-hosted (EasyOCR + heuristics).

Turns one or more screenshots (Instagram profile / DM, WhatsApp, Facebook,
casting groups, talent conversations) into structured scouting fields:

    Screenshot -> EasyOCR -> raw text -> regex + heuristics -> field mapping
                -> multi-screenshot merge -> duplicate detection -> review modal

No paid API and no external AI service. OCR runs locally via EasyOCR (English),
and every field is derived with deterministic regex/heuristics, so the engine is
free to run and produces a heuristic per-field confidence score.

The output contract (per-field {value, confidence}, normalized values, duplicate
match) is unchanged from the previous implementation, so the existing review
modal and API endpoint keep working without modification.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from core import db, _now, cloudinary_upload, normalize_instagram_handle, APP_NAME

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB per screenshot

# Production hardening knobs (env-overridable).
# Per-image OCR wall-clock ceiling so a request never hangs the UI forever.
OCR_TIMEOUT_SEC = float(os.environ.get("SCOUT_OCR_TIMEOUT_SEC", "20"))
# Max OCR jobs running at once. EasyOCR/torch CPU inference is memory- and
# CPU-heavy; serialising to a small number prevents memory spikes under load.
OCR_CONCURRENCY = max(1, int(os.environ.get("SCOUT_OCR_CONCURRENCY", "2")))

_ACCEPTED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp"}
_EXT_TO_MEDIA = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

# Canonical extraction fields (the modal + normalize_extraction depend on these).
EXTRACTION_FIELDS = [
    "full_name",
    "instagram_username",
    "instagram_url",
    "phone_number",
    "manager_name",
    "manager_phone",
    "followers_count",
    "category",
    "location",
    "scouting_notes",
]

# Phone validation — E.164-ish: optional leading +, 7-15 digits.
_PHONE_RE = re.compile(r"^\+?\d{7,15}$")


class ScoutCaptureError(Exception):
    """Raised for caller-fixable problems (bad input, OCR engine missing)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# EasyOCR reader (lazy singleton) + readiness + concurrency guard
# ---------------------------------------------------------------------------
_reader = None
_ready = False              # True once the model is loaded and warmed
_ready_error: Optional[str] = None
_warmup_started = False
_semaphore: Optional[asyncio.Semaphore] = None


def _get_reader():
    """Lazily build the EasyOCR English reader. Heavy (loads torch + models),
    so it is created once on first use and reused for the process lifetime."""
    global _reader
    if _reader is not None:
        return _reader
    try:
        import easyocr  # lazy — keeps module import light and test-friendly
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ScoutCaptureError(
            503, "OCR engine 'easyocr' is not installed on the backend."
        ) from exc
    # gpu=False — runs on CPU. English only per spec.
    _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _reader


def _get_semaphore() -> asyncio.Semaphore:
    """Lazily create the OCR concurrency limiter (bound to the running loop)."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(OCR_CONCURRENCY)
    return _semaphore


def _warmup_sync() -> None:
    """Force EasyOCR to build the reader and load detection+recognition models
    by running one tiny OCR. Runs in a worker thread (never on the event loop)."""
    import numpy as np

    reader = _get_reader()
    blank = np.full((32, 128, 3), 255, dtype=np.uint8)
    reader.readtext(blank, detail=0)  # triggers model load; returns []


async def warmup() -> None:
    """Initialise EasyOCR at backend startup so the first user request does not
    pay model download/load latency. Idempotent and best-effort — failures flip
    readiness to False but never crash the app (the capture endpoint still
    lazy-loads on demand)."""
    global _ready, _ready_error, _warmup_started
    if _warmup_started:
        return
    _warmup_started = True
    try:
        await asyncio.to_thread(_warmup_sync)
        _ready, _ready_error = True, None
        logger.info("scout_capture: EasyOCR warmed up and ready")
    except Exception as exc:  # noqa: BLE001 — readiness records, never raises
        _ready, _ready_error = False, str(exc)
        logger.warning("scout_capture: OCR warmup failed (will lazy-load): %s", exc)


def ocr_readiness() -> Dict[str, Any]:
    """Internal readiness state for health checks."""
    return {
        "engine": "easyocr",
        "ready": _ready,
        "model_loaded": _reader is not None,
        "warmup_started": _warmup_started,
        "concurrency_limit": OCR_CONCURRENCY,
        "timeout_sec": OCR_TIMEOUT_SEC,
        "error": _ready_error,
    }


# ---------------------------------------------------------------------------
# Input validation / encoding
# ---------------------------------------------------------------------------
def prepare_images(files: List[Tuple[str, str, bytes]]) -> List[Dict[str, Any]]:
    """Validate uploaded screenshots. `files` is (filename, content_type, data).
    Returns image metadata dicts. Raises ScoutCaptureError on bad input."""
    if not files:
        raise ScoutCaptureError(400, "At least one screenshot is required.")
    if len(files) > MAX_IMAGES:
        raise ScoutCaptureError(400, f"At most {MAX_IMAGES} screenshots per capture.")

    out: List[Dict[str, Any]] = []
    for filename, content_type, data in files:
        if not data:
            raise ScoutCaptureError(400, f"Empty file: {filename or 'screenshot'}")
        if len(data) > MAX_IMAGE_BYTES:
            raise ScoutCaptureError(
                400, f"{filename or 'Screenshot'} exceeds the 10 MB limit."
            )
        media_type = _detect_media_type(content_type, filename, data)
        if media_type not in _ACCEPTED_MEDIA_TYPES:
            raise ScoutCaptureError(
                400, "Only PNG, JPG, JPEG, and WEBP screenshots are supported."
            )
        out.append(
            {
                "media_type": media_type,
                "data_b64": base64.standard_b64encode(data).decode("ascii"),
                "raw": data,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "filename": filename or "screenshot",
                "content_type": content_type or media_type,
            }
        )
    return out


def _detect_media_type(content_type: Optional[str], filename: str, data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and b"WEBP" in data[8:15]:
        return "image/webp"
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct in _ACCEPTED_MEDIA_TYPES:
        return ct
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    return _EXT_TO_MEDIA.get(ext, "")


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
def ocr_image(data: bytes) -> Tuple[List[Dict[str, Any]], int]:
    """Run EasyOCR on raw image bytes.

    Returns (lines, image_height) where each line is
    {text, conf, y, x} sorted top-to-bottom, left-to-right. The y coordinate +
    image height drive the "top profile area" name heuristic.
    """
    reader = _get_reader()
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise ScoutCaptureError(503, "OCR image deps (numpy/Pillow) missing.") from exc

    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as exc:
        raise ScoutCaptureError(400, "Could not decode image.") from exc
    height = img.height
    arr = np.array(img)

    results = reader.readtext(arr, detail=1, paragraph=False)
    lines: List[Dict[str, Any]] = []
    for bbox, text, conf in results:
        ys = [p[1] for p in bbox]
        xs = [p[0] for p in bbox]
        lines.append(
            {
                "text": (text or "").strip(),
                "conf": float(conf),
                "y": float(min(ys)),
                "x": float(min(xs)),
            }
        )
    lines = [l for l in lines if l["text"]]
    lines.sort(key=lambda l: (l["y"], l["x"]))
    return lines, height


# ---------------------------------------------------------------------------
# Heuristic field extraction (pure / deterministic / unit-testable)
# ---------------------------------------------------------------------------
# Instagram handle: standalone @name (not an email — preceded by start/space/colon).
_HANDLE_RE = re.compile(r"(?:^|[\s:|·•])@([A-Za-z0-9._]{2,30})")
_IG_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{2,30})", re.I
)
# Permissive phone candidate: + and digits with spaces/dashes/parens.
_PHONE_FINDER = re.compile(r"(\+?\d[\d\s\-()]{6,18}\d)")
_FOLLOWERS_RE = re.compile(r"([\d][\d.,]*\s*[KMkm]?)\s*follower", re.I)
# Manager indicators: strong role words anywhere, OR "contact"/"booking" only in
# label form ("Contact:") so a talent's own "contact me on <number>" in a DM is
# NOT misread as a manager line.
_MANAGER_RE = re.compile(
    r"\b(managed\s+by|manager|mgmt|management|agent)\b|\b(contact|booking)\s*:",
    re.I,
)
_LOC_EMOJI_RE = re.compile(r"📍\s*([A-Za-z][A-Za-z .,'-]{1,30})")

# UI chrome / non-name tokens to skip when guessing the display name.
_NAME_NOISE = {
    "followers", "following", "posts", "post", "message", "follow", "following",
    "edit profile", "professional dashboard", "verified", "story", "stories",
    "highlights", "reels", "tagged", "more", "see translation", "active now",
    "online", "typing", "instagram", "facebook", "whatsapp", "sponsored",
    "suggested", "for you", "view profile", "send message",
}

# Common Indian cities (+ a few metros) for location heuristics.
_CITIES = [
    "mumbai", "delhi", "new delhi", "bangalore", "bengaluru", "hyderabad",
    "chennai", "kolkata", "pune", "ahmedabad", "jaipur", "surat", "lucknow",
    "kanpur", "nagpur", "indore", "thane", "bhopal", "visakhapatnam", "patna",
    "vadodara", "ghaziabad", "ludhiana", "agra", "nashik", "goa", "chandigarh",
    "kochi", "cochin", "noida", "gurgaon", "gurugram", "coimbatore", "mysore",
    "mysuru", "guwahati", "dehradun", "udaipur", "rishikesh", "shimla",
]

# Profession / category keywords (longer phrases first so they win).
_CATEGORY_KEYWORDS = [
    "fashion model", "content creator", "social media influencer",
    "digital creator", "ugc creator", "make up artist", "makeup artist",
    "fitness model", "actor", "actress", "model", "influencer", "creator",
    "artist", "dancer", "singer", "musician", "photographer", "blogger",
    "vlogger", "youtuber", "anchor", "host", "stylist", "designer",
]
# Lines that carry useful availability/contact context for scouting notes.
_NOTE_HINT_RE = re.compile(
    r"\b(available|availability|dm|call|whatsapp|contact|between|book|"
    r"\d{1,2}\s*(?:am|pm)|am|pm|free|busy|shoot|collab)\b",
    re.I,
)


def _lines_from_text(text: str) -> List[Dict[str, Any]]:
    """Build pseudo-OCR lines from a plain string (used by tests and as a
    fallback). Assigns increasing y so top-area heuristics still work."""
    out = []
    for i, raw in enumerate((text or "").splitlines()):
        t = raw.strip()
        if t:
            out.append({"text": t, "conf": 0.9, "y": float(i * 10), "x": 0.0})
    return out


def _is_name_like(text: str) -> bool:
    if not text or "@" in text or "http" in text.lower() or "." in text and "instagram" in text.lower():
        return False
    if any(ch.isdigit() for ch in text):
        return False
    low = text.lower().strip()
    if low in _NAME_NOISE:
        return False
    if any(noise in low for noise in ("follow", "message", "profile", "posts", "verified")):
        return False
    tokens = text.split()
    if not (1 <= len(tokens) <= 4):
        return False
    letters = sum(c.isalpha() for c in text)
    return letters >= 2


def _extract_phones(text: str) -> List[str]:
    """Return normalized phone numbers found in a string (order preserved)."""
    found: List[str] = []
    for m in _PHONE_FINDER.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        # Require a leading + (intl) or a plausible local length to avoid
        # catching follower counts / dates / ids.
        if not (m.group(1).strip().startswith("+") or 10 <= len(digits) <= 13):
            continue
        norm = normalize_phone(m.group(1))
        if norm and norm not in found:
            found.append(norm)
    return found


def _field(value: Any, confidence: int) -> Dict[str, Any]:
    return {"value": value if value is not None else "", "confidence": confidence}


def extract_fields(lines: List[Dict[str, Any]], img_height: Optional[float] = None) -> Dict[str, Any]:
    """Heuristic field extraction from OCR lines (one screenshot).

    Returns a dict of EXTRACTION_FIELDS -> {value, confidence}. Empty value +
    confidence 0 when a field is not present (never fabricates).
    """
    texts = [l["text"] for l in lines]
    joined = "\n".join(texts)
    lower = joined.lower()
    if img_height is None:
        img_height = max((l.get("y", 0) for l in lines), default=0) + 10

    out = {f: _field("", 0) for f in EXTRACTION_FIELDS}

    # -- Instagram username + URL -----------------------------------------
    username, uname_conf = "", 0
    m = _IG_URL_RE.search(joined)
    if m:
        username, uname_conf = m.group(1), 96
    else:
        hm = _HANDLE_RE.search(joined)
        if hm:
            username, uname_conf = hm.group(1), 94
    if not username:
        # IG profile header shows the handle (no @) on the very top line, e.g.
        # "anjalii_ee" / "priyajainofficial". Accept a top-area, single-token,
        # all-lowercase handle pattern. A separator (./_/digit) raises confidence.
        for l in lines[:3]:
            t = l["text"].strip()
            if re.fullmatch(r"[a-z0-9](?:[a-z0-9._]{1,28})[a-z0-9_]", t):
                has_sep = bool(re.search(r"[._0-9]", t))
                username, uname_conf = t, (80 if has_sep else 72)
                break
    if username:
        out["instagram_username"] = _field(username, uname_conf)
        out["instagram_url"] = _field(
            f"https://www.instagram.com/{username.lower()}", uname_conf
        )

    # -- Manager (keyword-anchored) ---------------------------------------
    manager_line_idx = set()
    for i, line in enumerate(texts):
        mm = _MANAGER_RE.search(line)
        if not mm:
            continue
        manager_line_idx.add(i)
        # name = leading alpha words after the keyword (before any phone digits),
        # else a name-like next line.
        segment = line[mm.end():].strip(" :-—|").strip()
        nm = re.match(r"[A-Za-z][A-Za-z .]{1,40}", segment)
        cand = nm.group(0).strip() if nm else ""
        if not _is_name_like(cand):
            cand = ""
        if not cand and i + 1 < len(texts) and _is_name_like(texts[i + 1]):
            cand = texts[i + 1].strip()
        if cand and not out["manager_name"]["value"]:
            out["manager_name"] = _field(cand, 85)

    # -- Phones (talent vs manager) ---------------------------------------
    talent_phone, manager_phone = "", ""
    for i, line in enumerate(texts):
        phones = _extract_phones(line)
        if not phones:
            continue
        is_mgr_ctx = i in manager_line_idx or (i - 1) in manager_line_idx
        for p in phones:
            if is_mgr_ctx and not manager_phone:
                manager_phone = p
            elif not is_mgr_ctx and not talent_phone:
                talent_phone = p
    if talent_phone:
        conf = 90 if re.match(r"^\+91[6-9]\d{9}$", talent_phone) else 78
        out["phone_number"] = _field(talent_phone, conf)
    if manager_phone:
        out["manager_phone"] = _field(manager_phone, 85)

    # -- Followers ---------------------------------------------------------
    fm = _FOLLOWERS_RE.search(joined)
    if fm and parse_followers(fm.group(1)) is not None:
        out["followers_count"] = _field(fm.group(1).strip(), 92)
    else:
        # Fallback: a K/M number on a line adjacent to the word "followers".
        for i, line in enumerate(texts):
            if "follower" in line.lower():
                for j in (i, i - 1, i + 1):
                    if 0 <= j < len(texts):
                        km = re.search(r"([\d][\d.,]*\s*[KMkm]?)", texts[j])
                        if km and parse_followers(km.group(1)) is not None:
                            out["followers_count"] = _field(km.group(1).strip(), 70)
                            break
            if out["followers_count"]["value"]:
                break

    # -- Location ----------------------------------------------------------
    loc = ""
    em = _LOC_EMOJI_RE.search(joined)
    if em:
        loc = em.group(1).strip()
        out["location"] = _field(loc, 78)
    if not loc:
        for city in _CITIES:
            if re.search(r"\b" + re.escape(city) + r"\b", lower):
                out["location"] = _field(city.title(), 80)
                break

    # -- Category + scouting notes ----------------------------------------
    category = ""
    for kw in _CATEGORY_KEYWORDS:
        if kw in lower:
            category = kw.title()
            break
    if category:
        out["category"] = _field(category, 85)

    note_parts: List[str] = []
    if category:
        note_parts.append(category)
    if out["followers_count"]["value"]:
        note_parts.append(f"{out['followers_count']['value']} followers")
    for line in texts:
        if _NOTE_HINT_RE.search(line) and len(line) <= 80:
            cleaned = line.strip()
            if cleaned and cleaned not in note_parts:
                note_parts.append(cleaned)
    if note_parts:
        out["scouting_notes"] = _field("\n".join(note_parts[:8]), 80 if category else 55)

    # -- Name (top profile area heuristic) --------------------------------
    name_val, name_conf = "", 0
    for l in lines:  # already sorted top->bottom
        t = l["text"]
        if t.lower() == username.lower():
            continue
        if not _is_name_like(t):
            continue
        y = l.get("y", 0)
        top_frac = (y / img_height) if img_height else 1.0
        if top_frac > 0.45:  # only trust names in the top ~45% of the image
            continue
        name_conf = 82 if (top_frac <= 0.25 and t == t.title()) else 66
        name_val = t
        break
    if name_val:
        out["full_name"] = _field(name_val, name_conf)

    return out


def extract_fields_from_text(text: str) -> Dict[str, Any]:
    """Convenience wrapper: extract from a raw OCR string (no bounding boxes)."""
    lines = _lines_from_text(text)
    return extract_fields(lines, img_height=max((l["y"] for l in lines), default=0) + 10)


# ---------------------------------------------------------------------------
# Multi-screenshot merge
# ---------------------------------------------------------------------------
def merge_extractions(per_image: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge per-screenshot extractions into one. For each field, the highest-
    confidence non-empty value wins (so a profile screenshot supplies the name
    and a DM screenshot supplies the phone). Scouting notes are concatenated."""
    merged = {f: _field("", 0) for f in EXTRACTION_FIELDS}
    note_lines: List[str] = []
    note_conf = 0

    for extraction in per_image:
        for f in EXTRACTION_FIELDS:
            fv = extraction.get(f) or _field("", 0)
            if f == "scouting_notes":
                if fv["value"]:
                    for ln in str(fv["value"]).splitlines():
                        ln = ln.strip()
                        if ln and ln not in note_lines:
                            note_lines.append(ln)
                    note_conf = max(note_conf, fv["confidence"])
                continue
            if fv["value"] and fv["confidence"] > merged[f]["confidence"]:
                merged[f] = fv
            elif fv["value"] and not merged[f]["value"]:
                merged[f] = fv

    if note_lines:
        merged["scouting_notes"] = _field("\n".join(note_lines[:10]), note_conf)
    return merged


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Strip formatting, keep a single leading +. Returns an E.164-ish string
    (e.g. '+917895189770') or None if it cannot be a valid number."""
    if not raw:
        return None
    s = str(raw).strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if not plus:
        if digits.startswith("0"):
            digits = digits.lstrip("0")
        # Indian 10-digit mobile with no country code -> assume +91.
        if len(digits) == 10:
            digits = "91" + digits
    cand = "+" + digits
    return cand if _PHONE_RE.match(cand) else None


def parse_followers(raw: Optional[str]) -> Optional[int]:
    """'626' -> 626, '25.4K' -> 25400, '1.2M' -> 1200000."""
    if not raw:
        return None
    s = str(raw).strip().lower().replace(",", "").replace("followers", "").strip()
    m = re.match(r"^([\d.]+)\s*([km])?$", s)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    mult = {"k": 1_000, "m": 1_000_000}.get(m.group(2), 1)
    return int(num * mult)


def _username_from_url(url: Optional[str]) -> Optional[str]:
    return normalize_instagram_handle(url) if url else None


def normalize_extraction(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Derive cross-filled, normalised values from the merged extraction."""
    def _v(key: str) -> str:
        return (raw.get(key, {}) or {}).get("value", "") or ""

    username = normalize_instagram_handle(_v("instagram_username")) or _username_from_url(
        _v("instagram_url")
    )
    url = _v("instagram_url").strip()
    if username:
        url = f"https://www.instagram.com/{username}"
    elif not url:
        url = ""

    return {
        "full_name": _v("full_name").strip(),
        "instagram_username": (username or "").lower() or None,
        "instagram_url": url or None,
        "phone_number": normalize_phone(_v("phone_number")),
        "manager_name": _v("manager_name").strip() or None,
        "manager_phone": normalize_phone(_v("manager_phone")),
        "followers_count_raw": _v("followers_count").strip() or None,
        "followers_count": parse_followers(_v("followers_count")),
        "category": _v("category").strip() or None,
        "location": _v("location").strip() or None,
        "scouting_notes": _v("scouting_notes").strip() or None,
    }


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
# Priority: 1. Instagram URL  2. Instagram username  3. Phone  4. Manager phone
# NEVER name-based (Talentgram will have thousands of "Anjali"/"Riya"/"Neha").
async def find_duplicate(norm: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    username = norm.get("instagram_username")
    url = norm.get("instagram_url")
    phone = norm.get("phone_number")
    manager_phone = norm.get("manager_phone")

    if username:
        hit = await db.talents.find_one(
            {"instagram_handle": {"$regex": f"^{re.escape(username)}$", "$options": "i"}},
            {"_id": 0, "id": 1, "name": 1, "instagram_handle": 1, "phone": 1, "created_at": 1},
        )
        if hit:
            return _match(hit, "talent", "instagram_username", username)
        hit = await db.workflow_scouts.find_one(
            {"instagram_username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}},
            {"_id": 0},
        )
        if hit:
            return _match(hit, "scout", "instagram_username", username)

    if url:
        hit = await db.workflow_scouts.find_one(
            {"instagram_link": {"$regex": re.escape(url.rstrip("/")), "$options": "i"}},
            {"_id": 0},
        )
        if hit:
            return _match(hit, "scout", "instagram_url", url)

    if phone:
        digits = phone.lstrip("+")
        phone_rgx = {"$regex": re.escape(digits) + "$"}
        hit = await db.talents.find_one(
            {"phone": phone_rgx},
            {"_id": 0, "id": 1, "name": 1, "instagram_handle": 1, "phone": 1, "created_at": 1},
        )
        if hit:
            return _match(hit, "talent", "phone_number", phone)
        hit = await db.workflow_scouts.find_one({"phone": phone_rgx}, {"_id": 0})
        if hit:
            return _match(hit, "scout", "phone_number", phone)

    if manager_phone:
        digits = manager_phone.lstrip("+")
        hit = await db.workflow_scouts.find_one(
            {"manager_phone": {"$regex": re.escape(digits) + "$"}}, {"_id": 0}
        )
        if hit:
            return _match(hit, "scout", "manager_phone", manager_phone)

    return None


def _match(doc: Dict[str, Any], source: str, matched_on: str, value: str) -> Dict[str, Any]:
    return {
        "source": source,
        "matched_on": matched_on,
        "matched_value": value,
        "id": doc.get("id"),
        "name": doc.get("name"),
        "instagram_username": doc.get("instagram_username") or doc.get("instagram_handle"),
        "instagram_url": doc.get("instagram_link"),
        "phone": doc.get("phone"),
        "created_at": doc.get("created_at"),
    }


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
def _image_meta(img: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "filename": img["filename"],
        "sha256": img["sha256"],
        "size": img["size"],
        "content_type": img["content_type"],
        "url": None,
        "public_id": None,
    }


async def _archive_screenshots(audit_id: str, images: List[Dict[str, Any]]) -> None:
    """Background task: upload screenshots to Cloudinary and patch the audit doc.
    Best-effort — failures are logged, never surfaced, never block the request."""
    refs: List[Dict[str, Any]] = []
    for idx, img in enumerate(images):
        ref = _image_meta(img)
        try:
            res = cloudinary_upload(
                img["raw"],
                folder=f"{APP_NAME}/scout_capture/{audit_id}",
                public_id=f"shot_{idx}",
                resource_type="image",
                content_type=img["content_type"],
            )
            ref["url"] = res.get("secure_url")
            ref["public_id"] = res.get("public_id")
        except Exception as exc:  # noqa: BLE001
            logger.warning("scout_capture: screenshot audit upload failed: %s", exc)
        refs.append(ref)
    try:
        await db.scout_capture_audit.update_one(
            {"id": audit_id},
            {"$set": {"screenshots": refs, "screenshots_status": "stored"}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("scout_capture: audit screenshot patch failed: %s", exc)


async def write_audit(
    *,
    user_id: Optional[str],
    images: List[Dict[str, Any]],
    extraction: Dict[str, Any],
    normalized: Dict[str, Any],
    duplicate: Optional[Dict[str, Any]],
    raw_text: str,
    processing_ms: int,
) -> str:
    audit_id = str(uuid.uuid4())
    doc = {
        "id": audit_id,
        "user_id": user_id,
        "image_count": len(images),
        "screenshots": [_image_meta(img) for img in images],
        "screenshots_status": "pending",
        "raw_ocr_text": (raw_text or "")[:20000],
        "extraction": extraction,        # raw per-field {value, confidence}
        "normalized": normalized,        # canonical values
        "duplicate": duplicate,
        "processing_ms": processing_ms,
        "engine": "easyocr",
        "created_at": _now(),
    }
    try:
        await db.scout_capture_audit.insert_one(doc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scout_capture: audit insert failed: %s", exc)
    try:
        asyncio.create_task(_archive_screenshots(audit_id, images))
    except RuntimeError:  # no running loop (e.g. unit test) — skip archival
        pass
    return audit_id


def confidence_band(score: Any) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "low"
    if s >= 90:
        return "high"
    if s >= 70:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def run_capture(
    files: List[Tuple[str, str, bytes]], user_id: Optional[str]
) -> Dict[str, Any]:
    """End-to-end: validate -> OCR -> extract -> merge -> normalise -> dedupe
    -> audit. OCR is CPU-bound (blocking), so it runs in a thread to keep the
    event loop responsive. Returns the payload the review modal consumes."""
    started = time.monotonic()
    images = prepare_images(files)

    per_image: List[Dict[str, Any]] = []
    raw_texts: List[str] = []
    sem = _get_semaphore()
    for img in images:
        # Concurrency-limited + timeout-protected OCR. The limiter prevents
        # memory spikes when many scouts upload at once; the timeout guarantees
        # the request never hangs the UI indefinitely.
        try:
            async with sem:
                lines, height = await asyncio.wait_for(
                    asyncio.to_thread(ocr_image, img["raw"]),
                    timeout=OCR_TIMEOUT_SEC,
                )
        except asyncio.TimeoutError:
            raise ScoutCaptureError(
                504,
                "OCR timed out — please retry with a smaller or clearer screenshot.",
            )
        raw_texts.append("\n".join(l["text"] for l in lines))
        per_image.append(extract_fields(lines, height))

    extraction = merge_extractions(per_image)
    normalized = normalize_extraction(extraction)
    duplicate = await find_duplicate(normalized)
    processing_ms = int((time.monotonic() - started) * 1000)

    audit_id = await write_audit(
        user_id=user_id,
        images=images,
        extraction=extraction,
        normalized=normalized,
        duplicate=duplicate,
        raw_text="\n---\n".join(raw_texts),
        processing_ms=processing_ms,
    )

    fields = {
        key: {
            "value": (val or {}).get("value", ""),
            "confidence": (val or {}).get("confidence", 0),
            "band": confidence_band((val or {}).get("confidence", 0)),
        }
        for key, val in extraction.items()
    }

    return {
        "audit_id": audit_id,
        "fields": fields,
        "normalized": normalized,
        "duplicate": duplicate,
        "processing_ms": processing_ms,
    }
