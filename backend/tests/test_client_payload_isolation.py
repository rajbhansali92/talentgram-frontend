"""Regression test: ensure admin-internal submission data NEVER leaks into the client payload.

Validates:
- `_filter_talent_for_client` returns only keys in CLIENT_ALLOWED_FIELDS
- `_submission_to_client_shape` never carries admin form_data keys (availability/budget/etc.)
- `_public_media` drops scope metadata

Run: cd /app/backend && python -m pytest tests/test_client_payload_isolation.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import (  # noqa: E402
    CLIENT_ALLOWED_FIELDS,
    DEFAULT_VISIBILITY,
    _filter_talent_for_client,
    _public_media,
    _submission_to_client_shape,
)


FORBIDDEN_KEYS = {
    "custom_answers", "competitive_brand",
    "form_data", "field_visibility",
    "dob", "gender", "bio", "source",
    "notes", "password", "created_by",
    "email", "phone", "talent_email", "talent_phone",
    "project_id", "submission_id", "scope",
}


def _submission_with_admin_data():
    """A realistic submission that includes every admin-internal field."""
    return {
        "id": "sub-1",
        "project_id": "proj-1",
        "talent_email": "tester@example.com",
        "talent_phone": "+15551234567",
        "talent_name": "Jane Tester",
        "status": "submitted",
        "decision": "approved",
        "form_data": {
            "first_name": "Jane",
            "last_name": "Tester",
            "age": "28",
            "height": "5'6\"",
            "location": "Mumbai",
            "competitive_brand": "some secret brand",
            "availability": {"status": "yes", "note": "free all month"},
            "budget": {"status": "custom", "value": "INR 25,000/day"},
            "custom_answers": {"q1": "answer1"},
        },
        "field_visibility": {
            "first_name": True, "last_name": True,
            "age": True, "height": True, "location": True,
            "competitive_brand": False, "availability": False,
            "budget": False, "custom_answers": False,
        },
        "media": [
            {"id": "m1", "category": "image", "storage_path": "p1.jpg",
             "content_type": "image/jpeg", "size": 100, "created_at": "t",
             "scope": "submission", "project_id": "proj-1", "submission_id": "sub-1"},
            {"id": "m2", "category": "intro_video", "storage_path": "v1.mp4",
             "content_type": "video/mp4", "size": 100, "created_at": "t",
             "scope": "submission", "project_id": "proj-1", "submission_id": "sub-1"},
            {"id": "m3", "category": "take_1", "storage_path": "t1.mp4",
             "content_type": "video/mp4", "size": 100, "created_at": "t",
             "scope": "submission", "project_id": "proj-1", "submission_id": "sub-1"},
        ],
    }


def test_availability_and_budget_flow_to_client_when_enabled():
    sub = _submission_with_admin_data()
    # Admin has toggled availability + budget visible
    sub["field_visibility"] = {**sub["field_visibility"], "availability": True, "budget": True}
    shape = _submission_to_client_shape(sub)
    assert shape["availability"] == {"status": "yes", "note": "free all month"}
    assert shape["budget"] == {"status": "custom", "value": "INR 25,000/day"}

    vis_on = {**DEFAULT_VISIBILITY, "availability": True, "budget": True}
    filtered = _filter_talent_for_client(shape, vis_on)
    assert filtered["availability"]["status"] == "yes"
    assert filtered["availability"]["note"] == "free all month"
    assert filtered["budget"]["status"] == "custom"
    assert filtered["budget"]["value"] == "INR 25,000/day"

    # Turning off link-level visibility removes the fields entirely
    vis_off = {**DEFAULT_VISIBILITY, "availability": False, "budget": False}
    filtered2 = _filter_talent_for_client(shape, vis_off)
    assert "availability" not in filtered2
    assert "budget" not in filtered2


def test_submission_shape_excludes_admin_form_data():
    shape = _submission_to_client_shape(_submission_with_admin_data())
    leaks = set(shape.keys()) & FORBIDDEN_KEYS
    assert not leaks, f"Admin-internal keys leaked: {leaks}"
    # All takes normalised to category="take"; legacy take_N auto-labelled "Take N"
    cats = [m.get("category") for m in shape["media"]]
    assert "take" in cats, "at least one take should be carried through the shape"
    takes = [m for m in shape["media"] if m.get("category") == "take"]
    assert takes[0]["label"] == "Take 1"


def test_takes_are_ordered_and_gated_by_visibility():
    sub = _submission_with_admin_data()
    # Add take_2 + take_3 out of order to verify ordering
    sub["media"].insert(0, {"id": "m4", "category": "take_3", "storage_path": "t3.mp4",
                             "content_type": "video/mp4", "size": 100, "created_at": "t"})
    sub["media"].insert(0, {"id": "m5", "category": "take_2", "storage_path": "t2.mp4",
                             "content_type": "video/mp4", "size": 100, "created_at": "t"})
    shape = _submission_to_client_shape(sub)
    # New contract: ORDER = takes → intro → images; legacy takes labelled Take 1/2/3
    cats = [m["category"] for m in shape["media"]]
    take_cats = [c for c in cats if c == "take"]
    assert len(take_cats) == 3, f"expected 3 takes, got {cats}"
    # Takes must appear before intro (category="video") and portfolio
    first_intro = next((i for i, c in enumerate(cats) if c == "video"), -1)
    first_take = next((i for i, c in enumerate(cats) if c == "take"), -1)
    assert first_take < first_intro, f"takes must come before intro: {cats}"
    labels = [m["label"] for m in shape["media"] if m["category"] == "take"]
    assert labels == ["Take 1", "Take 2", "Take 3"]

    # With takes off, all takes dropped
    vis_off = {**DEFAULT_VISIBILITY, "takes": False}
    filtered = _filter_talent_for_client(shape, vis_off)
    assert not any(m["category"] == "take" for m in filtered["media"])

    # With takes on, all present
    vis_on = {**DEFAULT_VISIBILITY, "takes": True}
    filtered = _filter_talent_for_client(shape, vis_on)
    kept = [m for m in filtered["media"] if m["category"] == "take"]
    assert [m["label"] for m in kept] == ["Take 1", "Take 2", "Take 3"]


def test_final_payload_is_allowlist_only():
    shape = _submission_to_client_shape(_submission_with_admin_data())
    filtered = _filter_talent_for_client(shape, DEFAULT_VISIBILITY)
    extra = set(filtered.keys()) - CLIENT_ALLOWED_FIELDS
    assert not extra, f"Keys outside allowlist: {extra}"
    leaks = set(filtered.keys()) & FORBIDDEN_KEYS
    assert not leaks, f"Admin-internal keys present: {leaks}"


def test_public_media_strips_scope_markers():
    internal = {
        "id": "m1", "category": "image", "storage_path": "p1.jpg",
        "content_type": "image/jpeg", "size": 100, "created_at": "t",
        "scope": "submission", "project_id": "proj-1", "submission_id": "sub-1",
    }
    cleaned = _public_media(internal)
    for key in ("scope", "project_id", "submission_id", "talent_id"):
        assert key not in cleaned


def test_hidden_field_visibility_drops_values():
    sub = _submission_with_admin_data()
    # Hide everything at the link level
    visibility = {k: False for k in DEFAULT_VISIBILITY}
    visibility["portfolio"] = True  # still show images
    shape = _submission_to_client_shape(sub)
    filtered = _filter_talent_for_client(shape, visibility)
    # Only id, name, media, cover_media_id should remain
    assert "age" not in filtered
    assert "height" not in filtered
    assert "location" not in filtered
    assert filtered["id"] == "sub-1"
    assert filtered["name"] == "Jane Tester"
    # intro_video is off — no video media
    cats = [m["category"] for m in filtered["media"]]
    assert "video" not in cats
