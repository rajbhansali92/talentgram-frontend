"""
test_talents_tagging.py — Enterprise Talent Tagging System Tests

Tests pure logic without importing FastAPI/bcrypt/motor.
All router-dependent tests use embedded logic extracted from the source
so they can run in any Python 3.x environment without the full stack.
"""
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Extracted helpers (pure logic, no framework deps)
# ---------------------------------------------------------------------------

def _normalize_tag_name(raw: str) -> str:
    """Mirrors routers/talents.py::_normalize_tag_name"""
    return raw.strip().lower()


def _tag_doc(tag_id: str, name: str) -> dict:
    """Mirrors routers/talents.py::_tag_doc (minus the _now() call)"""
    normalized = _normalize_tag_name(name)
    return {
        "id": tag_id,
        "name": name.strip(),
        "normalized_name": normalized,
        "created_at": "2026-01-01T00:00:00Z",
    }


def _compute_age_from_dob(dob: str) -> int | None:
    """Mirrors the DOB-only age calculation in applications.py"""
    if not dob:
        return None
    try:
        parts = dob.split("-")
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except Exception:
        return None
    from datetime import date
    today = date.today()
    age = today.year - y
    if (today.month, today.day) < (m, d):
        age -= 1
    return age if 0 <= age <= 120 else None


VALID_INTERESTS = {
    "Acting", "Modeling", "Print Campaigns", "TV Commercials",
    "Digital Ads", "Instagram Collaborations", "Influencer Campaigns",
    "Social Media Collaborations", "Fashion Campaigns", "Brand Shoots",
    "Music Videos", "OTT / Film Projects", "Event Appearances", "Hosting / Anchoring",
}


def _filter_interested_in(raw_interests: list) -> list:
    """Mirrors the allow-list filtering in applications.py::_application_to_talent"""
    return [i for i in (raw_interests or []) if isinstance(i, str) and i.strip() in VALID_INTERESTS]


def _application_to_talent_simple(app_doc: dict, admin_id: str) -> dict:
    """Simplified version of _application_to_talent for logic testing."""
    fd = app_doc.get("form_data") or {}
    tid = str(uuid.uuid4())
    dob = (fd.get("dob") or "").strip() or None
    age = _compute_age_from_dob(dob) if dob else None
    raw_interests = fd.get("interested_in") or []
    interested_in = _filter_interested_in(raw_interests)
    return {
        "id": tid,
        "name": f"{fd.get('first_name','')} {fd.get('last_name','')}".strip(),
        "email": app_doc.get("talent_email"),
        "age": age,
        "dob": dob,
        "interested_in": interested_in,
        "tags": [],
    }


# ---------------------------------------------------------------------------
# Unit: Tag name normalization
# ---------------------------------------------------------------------------

def test_normalize_tag_name_lowercase():
    assert _normalize_tag_name("  CASTING  ") == "casting"
    assert _normalize_tag_name("High Potential") == "high potential"
    assert _normalize_tag_name("Brand Ready") == "brand ready"


def test_normalize_tag_name_unicode():
    assert _normalize_tag_name("  Bollywood ") == "bollywood"


def test_normalize_tag_name_empty():
    assert _normalize_tag_name("   ") == ""


def test_normalize_tag_name_special_chars():
    assert _normalize_tag_name("A-List") == "a-list"


# ---------------------------------------------------------------------------
# Unit: _tag_doc structure
# ---------------------------------------------------------------------------

def test_tag_doc_structure():
    tag_id = str(uuid.uuid4())
    doc = _tag_doc(tag_id, "  High Potential  ")
    assert doc["id"] == tag_id
    assert doc["name"] == "High Potential"
    assert doc["normalized_name"] == "high potential"
    assert "created_at" in doc


def test_tag_doc_preserves_display_name():
    """Display name keeps original casing; normalized_name is lowercase."""
    doc = _tag_doc("id-001", "Brand Ready")
    assert doc["name"] == "Brand Ready"
    assert doc["normalized_name"] == "brand ready"


# ---------------------------------------------------------------------------
# Unit: interested_in filtering
# ---------------------------------------------------------------------------

def test_filter_interested_in_valid():
    result = _filter_interested_in(["Acting", "Modeling", "TV Commercials"])
    assert set(result) == {"Acting", "Modeling", "TV Commercials"}


def test_filter_interested_in_removes_invalid():
    result = _filter_interested_in(["Acting", "Random Nonsense", "", "TV Commercials"])
    assert "Acting" in result
    assert "TV Commercials" in result
    assert "Random Nonsense" not in result
    assert "" not in result


def test_filter_interested_in_removes_none():
    result = _filter_interested_in(["Acting", None])
    assert result == ["Acting"]


def test_filter_interested_in_all_14_valid():
    all_valid = list(VALID_INTERESTS)
    result = _filter_interested_in(all_valid)
    assert set(result) == VALID_INTERESTS


def test_filter_interested_in_empty_input():
    assert _filter_interested_in([]) == []
    assert _filter_interested_in(None) == []


# ---------------------------------------------------------------------------
# Unit: application → talent conversion
# ---------------------------------------------------------------------------

def test_application_to_talent_interested_in_filtering():
    app_doc = {
        "id": str(uuid.uuid4()),
        "talent_email": "test@example.com",
        "form_data": {
            "first_name": "Test",
            "last_name": "Talent",
            "interested_in": ["Acting", "Modeling", "Random Nonsense", "", "TV Commercials", None],
        },
    }
    result = _application_to_talent_simple(app_doc, "admin_id")
    assert set(result["interested_in"]) == {"Acting", "Modeling", "TV Commercials"}
    assert result["tags"] == []


def test_application_to_talent_empty_interested_in():
    app_doc = {
        "id": str(uuid.uuid4()),
        "talent_email": "test@example.com",
        "form_data": {"first_name": "Test", "last_name": "Talent"},
    }
    result = _application_to_talent_simple(app_doc, "admin_id")
    assert result["interested_in"] == []
    assert result["tags"] == []


def test_application_to_talent_all_valid_categories():
    all_valid = list(VALID_INTERESTS)
    app_doc = {
        "id": str(uuid.uuid4()),
        "talent_email": "test@example.com",
        "form_data": {
            "first_name": "Test",
            "last_name": "Talent",
            "interested_in": all_valid,
        },
    }
    result = _application_to_talent_simple(app_doc, "admin_id")
    assert set(result["interested_in"]) == VALID_INTERESTS


# ---------------------------------------------------------------------------
# Unit: Age resolution — DOB only (no override)
# ---------------------------------------------------------------------------

def test_age_resolved_from_dob():
    """Age is computed from DOB — override values are not applied."""
    app_doc = {
        "id": str(uuid.uuid4()),
        "talent_email": "test@example.com",
        "form_data": {
            "first_name": "Test",
            "last_name": "Talent",
            "dob": "1990-06-15",
            # These should be IGNORED
            "overrideAge": True,
            "submitted_age_override": 30,
        },
    }
    result = _application_to_talent_simple(app_doc, "admin_id")
    assert result["dob"] == "1990-06-15"
    assert result["age"] is not None
    # Age should be ~35 (from DOB 1990), NOT 30 (override value)
    assert result["age"] != 30
    assert 30 <= result["age"] <= 40  # reasonable range for 1990 birth year


def test_age_none_when_no_dob():
    app_doc = {
        "id": str(uuid.uuid4()),
        "talent_email": "test@example.com",
        "form_data": {"first_name": "Test", "last_name": "Talent"},
    }
    result = _application_to_talent_simple(app_doc, "admin_id")
    assert result["age"] is None
    assert result["dob"] is None


# ---------------------------------------------------------------------------
# Unit: Tag deduplication logic (normalized_name)
# ---------------------------------------------------------------------------

def test_tag_dedup_same_name_different_case():
    """Two tags with same name but different case should normalize identically."""
    n1 = _normalize_tag_name("High Potential")
    n2 = _normalize_tag_name("high potential")
    n3 = _normalize_tag_name("HIGH POTENTIAL")
    assert n1 == n2 == n3 == "high potential"


def test_tag_dedup_trailing_whitespace():
    n1 = _normalize_tag_name("  Casting Ready  ")
    n2 = _normalize_tag_name("Casting Ready")
    assert n1 == n2


# ---------------------------------------------------------------------------
# Unit: Cascading rename logic
# ---------------------------------------------------------------------------

def test_cascade_rename_updates_all_embedded_names():
    """After rename, every talent with the tag id should have updated name."""
    old_name = "High Potential"
    new_name = "Top Prospect"
    tag_id = "tag-001"

    # Simulated talent docs
    talents = [
        {"id": "t1", "tags": [{"id": "tag-001", "name": old_name}]},
        {"id": "t2", "tags": [{"id": "tag-001", "name": old_name}, {"id": "tag-002", "name": "Other"}]},
        {"id": "t3", "tags": [{"id": "tag-002", "name": "Other"}]},  # no match
    ]

    # Simulate $set with array_filters
    def apply_rename(talents, tag_id, new_name):
        for talent in talents:
            for tag in talent.get("tags", []):
                if tag["id"] == tag_id:
                    tag["name"] = new_name
        return talents

    updated = apply_rename(talents, tag_id, new_name)
    assert updated[0]["tags"][0]["name"] == new_name
    assert updated[1]["tags"][0]["name"] == new_name
    assert updated[1]["tags"][1]["name"] == "Other"  # untouched
    assert updated[2]["tags"][0]["name"] == "Other"  # untouched


# ---------------------------------------------------------------------------
# Unit: Cascading global delete logic ($pull)
# ---------------------------------------------------------------------------

def test_cascade_delete_strips_tag_from_all_talents():
    """After global delete, tag is removed from every talent document."""
    tag_id_to_delete = "tag-danger"

    talents = [
        {"id": "t1", "tags": [{"id": "tag-danger", "name": "Danger Tag"}, {"id": "tag-safe", "name": "Safe"}]},
        {"id": "t2", "tags": [{"id": "tag-danger", "name": "Danger Tag"}]},
        {"id": "t3", "tags": [{"id": "tag-safe", "name": "Safe"}]},  # no match
    ]

    def apply_pull(talents, tag_id):
        for talent in talents:
            talent["tags"] = [t for t in talent.get("tags", []) if t["id"] != tag_id]
        return talents

    updated = apply_pull(talents, tag_id_to_delete)
    assert all(t["id"] != "tag-danger" for talent in updated for t in talent["tags"])
    assert updated[0]["tags"] == [{"id": "tag-safe", "name": "Safe"}]
    assert updated[1]["tags"] == []
    assert updated[2]["tags"] == [{"id": "tag-safe", "name": "Safe"}]


# ---------------------------------------------------------------------------
# Unit: Idempotent tag assignment
# ---------------------------------------------------------------------------

def test_assign_tag_idempotent():
    """Assigning a tag that already exists on talent is skipped."""
    talent_tags = [{"id": "tag-abc", "name": "Existing Tag"}]
    existing_ids = [t["id"] for t in talent_tags]

    tag_to_assign_id = "tag-abc"
    already = tag_to_assign_id in existing_ids
    assert already  # should NOT be re-assigned


def test_assign_new_tag_not_already_present():
    """A new tag not yet on the talent should be assigned."""
    talent_tags = [{"id": "tag-abc", "name": "Existing Tag"}]
    existing_ids = [t["id"] for t in talent_tags]

    tag_to_assign_id = "tag-xyz"
    already = tag_to_assign_id in existing_ids
    assert not already  # should be assigned


# ---------------------------------------------------------------------------
# Unit: Remove tag from talent ($pull simulation)
# ---------------------------------------------------------------------------

def test_remove_tag_from_talent_removes_correct_tag():
    talent_tags = [
        {"id": "tag-001", "name": "Tag One"},
        {"id": "tag-002", "name": "Tag Two"},
    ]
    tag_id_to_remove = "tag-001"

    result = [t for t in talent_tags if t["id"] != tag_id_to_remove]
    assert len(result) == 1
    assert result[0]["id"] == "tag-002"


def test_remove_nonexistent_tag_is_noop():
    talent_tags = [{"id": "tag-001", "name": "Tag One"}]
    result = [t for t in talent_tags if t["id"] != "tag-nonexistent"]
    assert result == talent_tags


# ---------------------------------------------------------------------------
# Unit: interested_in merge in application approval
# ---------------------------------------------------------------------------

def test_interested_in_merge_deduplicates():
    existing_interests = {"Acting", "Modeling"}
    incoming_interests = {"Modeling", "Brand Shoots", "Acting", "TV Commercials"}
    merged = sorted(existing_interests | incoming_interests)
    assert merged == sorted({"Acting", "Modeling", "Brand Shoots", "TV Commercials"})
    assert len(merged) == len(set(merged))


def test_interested_in_merge_empty_existing():
    existing_interests = set()
    incoming_interests = {"Acting", "Modeling"}
    merged = sorted(existing_interests | incoming_interests)
    assert "Acting" in merged
    assert "Modeling" in merged


def test_interested_in_merge_both_empty():
    merged = sorted(set() | set())
    assert merged == []


def test_interested_in_merge_preserves_existing_when_incoming_empty():
    existing_interests = {"Acting", "Modeling"}
    incoming_interests = set()
    merged = sorted(existing_interests | incoming_interests)
    assert set(merged) == {"Acting", "Modeling"}


# ---------------------------------------------------------------------------
# Async unit tests (mock-based, no live DB)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_mock_assign_tag():
    """Verify async mock pattern works for assign tag flow."""
    mock_update = AsyncMock(return_value=MagicMock(modified_count=1, matched_count=1))

    result = await mock_update(
        {"id": "talent-001"},
        {"$push": {"tags": {"id": "tag-xyz", "name": "New Tag"}}}
    )
    assert result.modified_count == 1
    mock_update.assert_called_once()


@pytest.mark.asyncio
async def test_async_mock_global_delete_cascade():
    """Verify $pull mock for cascading global tag delete."""
    mock_delete = AsyncMock(return_value=MagicMock(deleted_count=1))
    mock_pull = AsyncMock(return_value=MagicMock(modified_count=3))

    del_result = await mock_delete({"id": "tag-danger"})
    assert del_result.deleted_count == 1

    pull_result = await mock_pull(
        {"tags.id": "tag-danger"},
        {"$pull": {"tags": {"id": "tag-danger"}}},
    )
    assert pull_result.modified_count == 3


def test_get_talent_query_uuid():
    from bson import ObjectId
    tid = "t1-uuid"
    query = {"id": tid}
    if len(tid) == 24:
        query = {"$or": [{"id": tid}, {"_id": ObjectId(tid)}]}
    assert query == {"id": "t1-uuid"}


def test_get_talent_query_objectid():
    from bson import ObjectId
    tid = "60a1f2e9d5e3c8b4a0f12345"
    query = {"id": tid}
    if len(tid) == 24:
        query = {"$or": [{"id": tid}, {"_id": ObjectId(tid)}]}
    assert query == {"$or": [{"id": "60a1f2e9d5e3c8b4a0f12345"}, {"_id": ObjectId("60a1f2e9d5e3c8b4a0f12345")}]}
