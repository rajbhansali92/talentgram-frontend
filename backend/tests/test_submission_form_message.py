"""Unit tests for the canonical submission-form formatter
(routers/submissions.py: _submission_form_lines / _build_submission_form_messages),
the shared logic behind the WhatsApp "completed form" message that now
follows every NEW SUBMISSION / SUBMISSION UPDATED notification.

Pure-function tests — no DB, no HTTP, no WhatsApp worker involved. The
end-to-end enqueue behavior (two messages, correct order, correct
destination) is covered separately in test_whatsapp_notifications.py.
"""
import os
os.environ["MONGO_URL"] = "mongodb://localhost:27017"
os.environ["DB_NAME"] = "test"
os.environ["JWT_SECRET"] = "dummy"
os.environ["RESEND_API_KEY"] = "dummy"
os.environ["SENDGRID_API_KEY"] = "dummy"
os.environ["CLOUDINARY_CLOUD_NAME"] = "dummy"
os.environ["CLOUDINARY_API_KEY"] = "dummy"
os.environ["CLOUDINARY_API_SECRET"] = "dummy"
os.environ["ADMIN_EMAIL"] = "admin@talentgram.co"
os.environ["ADMIN_PASSWORD"] = "dummy"

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from routers.submissions import _submission_form_lines, _build_submission_form_messages

PROJECT = {"id": "proj-1", "brand_name": "Loreal Campaign", "custom_questions": []}


def _sub(form_data, effective_age=None, original_form_data=None):
    doc = {"id": "sub-1", "form_data": form_data}
    if effective_age is not None:
        doc["effective_age"] = effective_age
    if original_form_data is not None:
        doc["original_form_data"] = original_form_data
    return doc


# TEST 1 — Basic new submission: standard fields populated, all appear.
def test_basic_submission_standard_fields_present():
    form = {
        "first_name": "Dia",
        "last_name": "Chordia",
        "height": "5'7\"",
        "location": "Mumbai, India",
        "availability": {"status": "yes", "note": ""},
        "budget": {"status": "accept", "value": ""},
        "instagram_handle": "dia_chordia",
    }
    lines = _submission_form_lines(_sub(form, effective_age=22), PROJECT)
    text = "\n".join(lines)
    assert text.startswith("Talentgram x Loreal Campaign - Form")
    assert "Dia - C" in lines
    assert "Age - 22" in lines
    assert "Height - 5'7\"" in lines
    assert "Current Location - Mumbai, India" in lines
    assert "Availability - Available" in lines
    assert "Budget - Accepts Day Rate" in lines
    assert "Instagram link - https://www.instagram.com/dia_chordia/" in lines


# TEST 2 — Empty fields: unfilled standard fields are omitted entirely,
# never rendered as "Field: -" / "Not provided" / "N/A".
def test_empty_fields_are_omitted_not_placeholder():
    form = {"first_name": "Solo", "last_name": ""}
    lines = _submission_form_lines(_sub(form), PROJECT)
    text = "\n".join(lines)
    assert "Solo" in lines  # no last name -> no " - X" suffix
    for forbidden in ("Height -", "Current Location -", "Availability -", "Budget -",
                       "Age -", "Instagram link -", "Competitive Brand -"):
        assert forbidden not in text
    for placeholder in ("Not provided", "N/A", " - -", ": -"):
        assert placeholder not in text


# TEST 3 — Custom questions: multiple, all answered, all appear correctly.
def test_custom_questions_all_answered():
    project = {
        "id": "proj-2", "brand_name": "Snapdragon Film 2",
        "custom_questions": [
            {"id": "q1", "question": "Do you have a passport?"},
            {"id": "q2", "question": "Can you swim?"},
        ],
    }
    form = {
        "first_name": "Tanvi",
        "custom_answers": {"q1": "Yes, valid until 2030", "q2": "Yes"},
    }
    lines = _submission_form_lines(_sub(form), project)
    assert "Do you have a passport? - Yes, valid until 2030" in lines
    assert "Can you swim? - Yes" in lines


# TEST 4 — Partially answered custom questions: only answered ones appear.
def test_custom_questions_partially_answered():
    project = {
        "id": "proj-3", "brand_name": "X",
        "custom_questions": [
            {"id": "q1", "question": "Answered question"},
            {"id": "q2", "question": "Unanswered question"},
            {"id": "q3", "question": "Blank-string question"},
        ],
    }
    form = {
        "first_name": "P",
        "custom_answers": {"q1": "Yep", "q3": "   "},  # q2 missing key entirely
    }
    lines = _submission_form_lines(_sub(form), project)
    text = "\n".join(lines)
    assert "Answered question - Yep" in lines
    assert "Unanswered question" not in text
    assert "Blank-string question" not in text


# TEST 5 — Competitive Brand = YES: selected answer + details appear.
def test_competitive_brand_yes_shows_details():
    form = {
        "first_name": "P",
        "has_competitive_brand_experience": True,
        "competitive_brand": "Nike (2024 campaign)",
    }
    lines = _submission_form_lines(_sub(form), PROJECT)
    assert "Competitive Brand - Yes — Nike (2024 campaign)" in lines


def test_competitive_brand_yes_without_details_still_shows_yes():
    # Edge case: answered YES but no free-text details supplied — must
    # still show "Yes", not be silently omitted (this was the reported bug).
    form = {"first_name": "P", "has_competitive_brand_experience": True, "competitive_brand": ""}
    lines = _submission_form_lines(_sub(form), PROJECT)
    assert "Competitive Brand - Yes" in lines


# TEST 6 — Competitive Brand = NONE: correctly represented (was previously
# omitted entirely, indistinguishable from "not asked" — the core bug).
def test_competitive_brand_none_shows_correctly():
    form = {"first_name": "P", "has_competitive_brand_experience": False, "competitive_brand": ""}
    lines = _submission_form_lines(_sub(form), PROJECT)
    assert "Competitive Brand - None" in lines


def test_competitive_brand_not_asked_is_omitted():
    # Field genuinely not part of this project / never answered — must be
    # omitted, not shown as None (None is a real, different answer).
    form = {"first_name": "P"}
    lines = _submission_form_lines(_sub(form), PROJECT)
    assert not any(l.startswith("Competitive Brand") for l in lines)


# TEST 7 — Updated submission: the formatter always reads whatever dict
# it's given, so passing the freshly-fetched submission naturally reflects
# the latest saved state — verified here by feeding it "changed" data.
def test_reflects_latest_submitted_value():
    form_v1 = {"first_name": "P", "height": "5'6\""}
    form_v2 = {"first_name": "P", "height": "5'9\""}  # updated answer
    assert "Height - 5'6\"" in _submission_form_lines(_sub(form_v1), PROJECT)
    lines_v2 = _submission_form_lines(_sub(form_v2), PROJECT)
    assert "Height - 5'9\"" in lines_v2
    assert "Height - 5'6\"" not in lines_v2


# TEST 8 — Cleared answer: a previously-answered field that is now empty
# disappears from the new form.
def test_cleared_answer_disappears():
    form_with_value = {"first_name": "P", "custom_answers": {"q1": "Yes"}}
    project = {"id": "p", "brand_name": "X", "custom_questions": [{"id": "q1", "question": "Q1"}]}
    assert "Q1 - Yes" in _submission_form_lines(_sub(form_with_value), project)

    form_cleared = {"first_name": "P", "custom_answers": {"q1": ""}}
    lines_cleared = _submission_form_lines(_sub(form_cleared), project)
    assert not any(l.startswith("Q1") for l in lines_cleared)


# original_form_data precedence: once an admin override snapshot exists,
# the ORIGINAL talent-submitted values are shown (mirrors Copy Form's own
# `original_form_data ?? form_data` precedence), not the admin's edits.
def test_prefers_original_form_data_over_form_data():
    original = {"first_name": "Talent", "height": "5'5\""}
    edited = {"first_name": "Talent", "height": "5'11\""}  # admin override
    lines = _submission_form_lines(_sub(edited, original_form_data=original), PROJECT)
    assert "Height - 5'5\"" in lines
    assert "Height - 5'11\"" not in lines


# Location shapes: string, {city, country} dict, and list of either.
def test_location_shapes():
    assert "Current Location - Mumbai" in _submission_form_lines(
        _sub({"first_name": "P", "location": "Mumbai"}), PROJECT)
    assert "Current Location - Mumbai, India" in _submission_form_lines(
        _sub({"first_name": "P", "location": {"city": "Mumbai", "country": "India"}}), PROJECT)
    assert "Current Location - Mumbai, India; Delhi, India" in _submission_form_lines(
        _sub({"first_name": "P", "location": [
            {"city": "Mumbai", "country": "India"}, {"city": "Delhi", "country": "India"},
        ]}), PROJECT)


# Message splitting: an oversized form (many custom questions) splits into
# numbered PART messages rather than being truncated, and every answer
# survives across the parts.
def test_splits_when_form_exceeds_length_ceiling():
    many_questions = [{"id": f"q{i}", "question": f"Question number {i}"} for i in range(3000)]
    project = {"id": "p", "brand_name": "Big Project", "custom_questions": many_questions}
    answers = {f"q{i}": f"Answer {i}" * 3 for i in range(3000)}
    form = {"first_name": "P", "custom_answers": answers}

    messages = _build_submission_form_messages(_sub(form), project)
    assert len(messages) > 1
    for i, msg in enumerate(messages, 1):
        assert f"PART {i}/{len(messages)}" in msg
    # No answer lost across the split.
    combined = "\n".join(messages)
    assert "Question number 0 - Answer 0Answer 0Answer 0" in combined
    assert "Question number 2999 - Answer 2999Answer 2999Answer 2999" in combined


def test_does_not_split_ordinary_form():
    form = {"first_name": "Dia", "last_name": "Chordia", "height": "5'7\""}
    messages = _build_submission_form_messages(_sub(form), PROJECT)
    assert len(messages) == 1
    assert "PART" not in messages[0]
