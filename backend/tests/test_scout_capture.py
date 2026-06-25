"""Unit tests for the FREE (EasyOCR + heuristics) AI Scout Capture engine.

EasyOCR itself is not exercised here (heavy torch dependency); instead the
deterministic regex/heuristic parser is tested on realistic OCR-text fixtures —
exactly the strings EasyOCR would emit for profile / DM / combined screenshots —
plus normalization and the duplicate-detection PRIORITY.
"""
import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "dummy")
os.environ.setdefault("CLOUDINARY_API_KEY", "dummy")
os.environ.setdefault("CLOUDINARY_API_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "dummy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scout_capture as sc  # noqa: E402


# ---------------------------------------------------------------------------
# OCR-text fixtures (what EasyOCR would emit, top-to-bottom)
# ---------------------------------------------------------------------------
PROFILE_TEXT = """anjalii_ee
Anjali
Fashion Model
22 posts
626 followers
0 following
Follow
Message
"""

DM_TEXT = """Anjali
11:01 AM
Sure . 7895189770
You can call anytime btw 12-1 pm
"""

MANAGER_TEXT = """Riya Sharma
Model
Manager: Rahul +91 9876543210
"""


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("7895189770", "+917895189770"),
        ("+91 7895189770", "+917895189770"),
        ("078951 89770", "+917895189770"),
        ("+1 (415) 555-0142", "+14155550142"),
        ("12345", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_phone(raw, expected):
    assert sc.normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("626", 626),
        ("626 followers", 626),
        ("25.4K", 25400),
        ("1.2M", 1_200_000),
        ("12,345", 12345),
        ("", None),
        ("n/a", None),
    ],
)
def test_parse_followers(raw, expected):
    assert sc.parse_followers(raw) == expected


def test_confidence_band():
    assert sc.confidence_band(98) == "high"
    assert sc.confidence_band(75) == "medium"
    assert sc.confidence_band(40) == "low"
    assert sc.confidence_band(None) == "low"


def test_normalize_extraction_cross_fills_instagram():
    raw = {
        "full_name": {"value": "Anjali", "confidence": 82},
        "instagram_username": {"value": "anjalii_ee", "confidence": 80},
        "instagram_url": {"value": "", "confidence": 0},
        "phone_number": {"value": "7895189770", "confidence": 90},
        "manager_name": {"value": "", "confidence": 0},
        "manager_phone": {"value": "", "confidence": 0},
        "followers_count": {"value": "626", "confidence": 92},
        "category": {"value": "Fashion Model", "confidence": 85},
        "location": {"value": "", "confidence": 0},
        "scouting_notes": {"value": "Fashion Model", "confidence": 80},
    }
    n = sc.normalize_extraction(raw)
    assert n["full_name"] == "Anjali"
    assert n["instagram_username"] == "anjalii_ee"
    assert n["instagram_url"] == "https://www.instagram.com/anjalii_ee"
    assert n["phone_number"] == "+917895189770"
    assert n["followers_count"] == 626


def test_normalize_extraction_derives_username_from_url():
    raw = {f: {"value": "", "confidence": 0} for f in sc.EXTRACTION_FIELDS}
    raw["instagram_url"] = {"value": "https://instagram.com/priyajainofficial/", "confidence": 90}
    n = sc.normalize_extraction(raw)
    assert n["instagram_username"] == "priyajainofficial"
    assert n["instagram_url"] == "https://www.instagram.com/priyajainofficial"


# ---------------------------------------------------------------------------
# Heuristic extraction — profile / DM / manager
# ---------------------------------------------------------------------------
def test_extract_profile_only():
    f = sc.extract_fields_from_text(PROFILE_TEXT)
    assert f["instagram_username"]["value"] == "anjalii_ee"
    assert f["full_name"]["value"] == "Anjali"
    assert sc.parse_followers(f["followers_count"]["value"]) == 626
    assert f["category"]["value"].lower() == "fashion model"
    # No phone present in a profile -> must NOT be fabricated.
    assert f["phone_number"]["value"] == ""


def test_extract_dm_only():
    f = sc.extract_fields_from_text(DM_TEXT)
    assert sc.normalize_phone(f["phone_number"]["value"]) == "+917895189770"
    # No profile fields hallucinated from a DM.
    assert f["instagram_username"]["value"] == ""
    assert f["followers_count"]["value"] == ""
    # Availability note captured.
    assert "pm" in (f["scouting_notes"]["value"] or "").lower()


def test_extract_manager_details():
    f = sc.extract_fields_from_text(MANAGER_TEXT)
    assert f["manager_name"]["value"] == "Rahul"
    assert sc.normalize_phone(f["manager_phone"]["value"]) == "+919876543210"
    # The manager's number must NOT be recorded as the talent's own phone.
    assert f["phone_number"]["value"] == ""


def test_no_phone_false_positive_from_followers():
    # A bare follower count must never be read as a phone number.
    f = sc.extract_fields_from_text("someuser\n1234567 followers\n")
    assert f["phone_number"]["value"] == ""


# ---------------------------------------------------------------------------
# Multi-screenshot merge — name from profile, phone from DM
# ---------------------------------------------------------------------------
def test_merge_profile_and_dm():
    prof = sc.extract_fields_from_text(PROFILE_TEXT)
    dm = sc.extract_fields_from_text(DM_TEXT)
    merged = sc.merge_extractions([prof, dm])
    n = sc.normalize_extraction(merged)
    assert n["full_name"] == "Anjali"                    # from profile
    assert n["instagram_username"] == "anjalii_ee"       # from profile
    assert n["phone_number"] == "+917895189770"          # from DM
    assert n["followers_count"] == 626                   # from profile


# ---------------------------------------------------------------------------
# Duplicate detection — priority order
# ---------------------------------------------------------------------------
def _mock_db(talents_hit=None, scouts_hit=None):
    db = MagicMock()
    db.talents.find_one = AsyncMock(return_value=talents_hit)
    db.workflow_scouts.find_one = AsyncMock(return_value=scouts_hit)
    return db


@pytest.mark.asyncio
async def test_duplicate_prefers_instagram_username_over_phone(monkeypatch):
    talent = {
        "id": "t1", "name": "Anjali", "instagram_handle": "anjalii_ee",
        "phone": "+917895189770", "created_at": "2026-05-12",
    }
    monkeypatch.setattr(sc, "db", _mock_db(talents_hit=talent))
    norm = {
        "instagram_username": "anjalii_ee",
        "instagram_url": "https://www.instagram.com/anjalii_ee",
        "phone_number": "+917895189770", "manager_phone": None,
    }
    dup = await sc.find_duplicate(norm)
    assert dup and dup["source"] == "talent" and dup["matched_on"] == "instagram_username"


@pytest.mark.asyncio
async def test_duplicate_falls_through_to_phone(monkeypatch):
    db = MagicMock()
    talent = {"id": "t9", "name": "Riya", "phone": "+917895189770", "created_at": "2026-01-01"}

    async def talents_find_one(query, *a, **k):
        return talent if "phone" in query else None

    async def scouts_find_one(query, *a, **k):
        return None

    db.talents.find_one = AsyncMock(side_effect=talents_find_one)
    db.workflow_scouts.find_one = AsyncMock(side_effect=scouts_find_one)
    monkeypatch.setattr(sc, "db", db)

    norm = {
        "instagram_username": "someone_else",
        "instagram_url": "https://www.instagram.com/someone_else",
        "phone_number": "+917895189770", "manager_phone": None,
    }
    dup = await sc.find_duplicate(norm)
    assert dup and dup["matched_on"] == "phone_number" and dup["id"] == "t9"


@pytest.mark.asyncio
async def test_no_duplicate_returns_none(monkeypatch):
    monkeypatch.setattr(sc, "db", _mock_db())
    norm = {
        "instagram_username": "brand_new",
        "instagram_url": "https://www.instagram.com/brand_new",
        "phone_number": "+919999999999", "manager_phone": None,
    }
    assert await sc.find_duplicate(norm) is None


@pytest.mark.asyncio
async def test_duplicate_never_matches_on_name_only(monkeypatch):
    monkeypatch.setattr(sc, "db", _mock_db())  # all find_one -> None
    norm = {
        "full_name": "Anjali",
        "instagram_username": "anjali_unique_123",
        "instagram_url": "https://www.instagram.com/anjali_unique_123",
        "phone_number": "+918888888888", "manager_phone": None,
    }
    assert await sc.find_duplicate(norm) is None
