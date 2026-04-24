"""Regression tests for the casting review architecture:
- Renamable audition takes (category="take" + label)
- Client shape ordering (takes → intro → images)
- competitive_brand + custom_answers visibility gating (bool + per-question dict)
- Legacy take_1/2/3 auto-labelling
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import (  # noqa: E402
    CLIENT_ALLOWED_FIELDS,
    DEFAULT_FIELD_VISIBILITY,
    DEFAULT_VISIBILITY,
    _filter_talent_for_client,
    _submission_to_client_shape,
)


def _base_submission(media=None, form_data=None, fv=None):
    return {
        "id": "sub-1",
        "project_id": "p1",
        "talent_name": "Test Talent",
        "talent_email": "t@ex.com",
        "form_data": form_data or {
            "first_name": "Test", "last_name": "Talent",
            "age": 28, "height": "5'8\"", "location": "Mumbai",
            "availability": {"status": "yes"},
            "budget": {"status": "accept"},
        },
        "field_visibility": fv if fv is not None else {**DEFAULT_FIELD_VISIBILITY},
        "media": media or [],
    }


def _take(mid, cat="take", label=None, ts="t"):
    return {
        "id": mid, "category": cat, "label": label,
        "storage_path": f"{mid}.mp4", "content_type": "video/mp4",
        "size": 100, "created_at": ts,
    }


# --------------------------------------------------------------------------
# Renamable takes
# --------------------------------------------------------------------------
def test_new_take_category_carries_label():
    sub = _base_submission(media=[
        _take("t1", "take", "Scene 1", "1"),
        _take("t2", "take", "Emotional closeup", "2"),
    ])
    shape = _submission_to_client_shape(sub)
    takes = [m for m in shape["media"] if m["category"] == "take"]
    assert [m["label"] for m in takes] == ["Scene 1", "Emotional closeup"]


def test_new_take_blank_label_is_preserved_if_given():
    # Empty-string labels fall through to the fallback ("Take") — backend
    # client shape should never emit an empty label.
    sub = _base_submission(media=[
        _take("t1", "take", "", "1"),
    ])
    shape = _submission_to_client_shape(sub)
    takes = [m for m in shape["media"] if m["category"] == "take"]
    assert takes[0]["label"] == "Take"


def test_legacy_takes_auto_label():
    sub = _base_submission(media=[
        _take("t1", "take_1", None, "1"),
        _take("t3", "take_3", None, "3"),
        _take("t2", "take_2", None, "2"),
    ])
    shape = _submission_to_client_shape(sub)
    labels = [m["label"] for m in shape["media"] if m["category"] == "take"]
    # Must be in legacy order regardless of input order
    assert labels == ["Take 1", "Take 2", "Take 3"]


def test_legacy_and_new_takes_coexist_legacy_first():
    sub = _base_submission(media=[
        _take("n1", "take", "Custom A", "100"),
        _take("l1", "take_1", None, "50"),
        _take("n2", "take", "Custom B", "200"),
    ])
    shape = _submission_to_client_shape(sub)
    labels = [m["label"] for m in shape["media"] if m["category"] == "take"]
    assert labels == ["Take 1", "Custom A", "Custom B"]


def test_label_passes_through_filter_talent_for_client():
    sub = _base_submission(media=[_take("t1", "take", "Rooftop monologue", "1")])
    shape = _submission_to_client_shape(sub)
    filt = _filter_talent_for_client(shape, {**DEFAULT_VISIBILITY, "takes": True})
    kept = [m for m in filt["media"] if m["category"] == "take"]
    assert kept and kept[0]["label"] == "Rooftop monologue"


# --------------------------------------------------------------------------
# Client shape ordering: takes → intro → images
# --------------------------------------------------------------------------
def test_order_takes_then_intro_then_images():
    sub = _base_submission(media=[
        {"id": "i1", "category": "image", "storage_path": "i1.jpg", "content_type": "image/jpeg", "size": 1, "created_at": "a"},
        {"id": "v1", "category": "intro_video", "storage_path": "v.mp4", "content_type": "video/mp4", "size": 1, "created_at": "b"},
        _take("k1", "take", "S1", "c"),
    ])
    shape = _submission_to_client_shape(sub)
    cats = [m["category"] for m in shape["media"]]
    assert cats == ["take", "video", "portfolio"]


# --------------------------------------------------------------------------
# competitive_brand
# --------------------------------------------------------------------------
def test_competitive_brand_gated_off_by_default():
    sub = _base_submission(form_data={
        "first_name": "X", "last_name": "Y", "height": "5'10\"",
        "location": "Delhi", "availability": {"status": "yes"},
        "budget": {"status": "accept"}, "competitive_brand": "ACME",
    })
    shape = _submission_to_client_shape(sub)
    assert "competitive_brand" not in shape


def test_competitive_brand_gated_on_passes_through():
    fv = {**DEFAULT_FIELD_VISIBILITY, "competitive_brand": True}
    sub = _base_submission(
        form_data={
            "first_name": "X", "last_name": "Y", "height": "5'10\"",
            "location": "Delhi", "availability": {"status": "yes"},
            "budget": {"status": "accept"}, "competitive_brand": "ACME",
        },
        fv=fv,
    )
    shape = _submission_to_client_shape(sub)
    assert shape["competitive_brand"] == "ACME"
    # Must survive link-level filter
    filt = _filter_talent_for_client(shape, DEFAULT_VISIBILITY)
    assert filt["competitive_brand"] == "ACME"


# --------------------------------------------------------------------------
# custom_answers — bool and per-question dict visibility
# --------------------------------------------------------------------------
def test_custom_answers_all_or_nothing_bool():
    fv = {**DEFAULT_FIELD_VISIBILITY, "custom_answers": True}
    sub = _base_submission(
        form_data={
            "first_name": "X", "last_name": "Y", "height": "5'10\"",
            "location": "Delhi", "availability": {"status": "yes"},
            "budget": {"status": "accept"},
            "custom_answers": {"Passport?": "Yes", "Bike?": "No", "Swim?": ""},
        },
        fv=fv,
    )
    shape = _submission_to_client_shape(sub)
    qs = {q["question"]: q["answer"] for q in shape["custom_answers"]}
    assert qs == {"Passport?": "Yes", "Bike?": "No"}  # empty answer stripped


def test_custom_answers_per_question_dict_filter():
    fv = {
        **DEFAULT_FIELD_VISIBILITY,
        "custom_answers": {"Passport?": False, "Bike?": True},
    }
    sub = _base_submission(
        form_data={
            "first_name": "X", "last_name": "Y", "height": "5'10\"",
            "location": "Delhi", "availability": {"status": "yes"},
            "budget": {"status": "accept"},
            "custom_answers": {"Passport?": "Yes", "Bike?": "No", "Swim?": "Yes"},
        },
        fv=fv,
    )
    shape = _submission_to_client_shape(sub)
    qs = [q["question"] for q in shape["custom_answers"]]
    assert qs == ["Bike?"]  # only the whitelisted one


def test_custom_answers_all_hidden_removes_key():
    fv = {**DEFAULT_FIELD_VISIBILITY, "custom_answers": False}
    sub = _base_submission(
        form_data={
            "first_name": "X", "last_name": "Y", "height": "5'10\"",
            "location": "Delhi", "availability": {"status": "yes"},
            "budget": {"status": "accept"},
            "custom_answers": {"Q1": "A1"},
        },
        fv=fv,
    )
    shape = _submission_to_client_shape(sub)
    assert "custom_answers" not in shape


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------
def test_allowlist_contains_new_fields():
    assert "competitive_brand" in CLIENT_ALLOWED_FIELDS
    assert "custom_answers" in CLIENT_ALLOWED_FIELDS
