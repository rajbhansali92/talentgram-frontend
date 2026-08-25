"""Dual command syntax for upload/send (2026-08-25): both the original
hyphen grammar ("upload - Talent - Project") and plain space-separated
text ("upload Talent Project") must work. The space-separated form has
no delimiter between talent and project, so the boundary is resolved via
real database matching (_resolve_freeform_talent_project), never a fixed
word count or a guess.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, _now  # noqa: E402
from agents import modules as agent_modules  # noqa: E402
from agents import request_scope  # noqa: E402
from agents.modules import casting_pipeline as cp  # noqa: E402

agent_modules.register_all()

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def _seed_project(brand_name: str) -> str:
    pid = f"test-ff-proj-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({
        "id": pid, "brand_name": brand_name, "status": "ongoing", "slug": pid,
        "materials": [], "created_at": _now(),
    })
    return pid


async def _seed_talent(name: str) -> str:
    tid = f"test-ff-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": tid, "name": name, "tags": [], "notes": "",
        "phone": None, "whatsapp_group_name": "", "email": None, "normalized_email": None,
    })
    return tid


async def _cleanup(*, talent_ids=(), project_ids=()):
    await db.talents.delete_many({"id": {"$in": list(talent_ids)}})
    await db.projects.delete_many({"id": {"$in": list(project_ids)}})


# --- pure extraction (no DB) -------------------------------------------
def test_extract_upload_fields_hyphen_syntax_unchanged():
    fields = cp._extract_upload_fields("upload - Ahana Pocha - Google Test")
    assert fields == {"talent_selector": "Ahana Pocha", "project_query": "Google Test"}


def test_extract_upload_fields_space_syntax_marks_both_fields_identically():
    fields = cp._extract_upload_fields("upload Ahana Pocha Google Test")
    assert fields["talent_selector"] == fields["project_query"] == "Ahana Pocha Google Test"


def test_extract_send_fields_hyphen_syntax_unchanged():
    fields = cp._extract_send_fields("send - Ahana Pocha - Google Test")
    assert fields == {"talent_selector": "Ahana Pocha", "project_query": "Google Test"}


def test_extract_send_fields_space_syntax_marks_both_fields_identically():
    fields = cp._extract_send_fields("send Ahana Pocha Google Test")
    assert fields["talent_selector"] == fields["project_query"] == "Ahana Pocha Google Test"


def test_extract_fields_empty_remainder_returns_nothing():
    assert cp._extract_upload_fields("upload") == {}
    assert cp._extract_send_fields("send   ") == {}


# --- freeform DB-backed boundary resolution -----------------------------
async def test_freeform_resolves_unique_talent_project_split():
    talent_id = await _seed_talent("Ahana Pocha Freeform")
    project_id = await _seed_project("Google Freeform Test")
    request_scope.reset()
    try:
        talent_text, project_text, err = await cp._resolve_freeform_talent_project(
            "Ahana Pocha Freeform Google Freeform Test"
        )
        assert err is None, err
        assert talent_text == "Ahana Pocha Freeform"
        assert project_text == "Google Freeform Test"
    finally:
        await _cleanup(talent_ids=[talent_id], project_ids=[project_id])


async def test_freeform_fails_cleanly_when_no_split_resolves():
    request_scope.reset()
    talent_text, project_text, err = await cp._resolve_freeform_talent_project(
        "Totally Unknown Person Nonexistent Project Xyz123"
    )
    assert talent_text is None and project_text is None
    assert err is not None and err.ok is False
    assert err.error == "freeform_unresolved"


async def test_freeform_stops_on_genuine_ambiguity_never_guesses():
    """Two different real talents both named such that multiple split
    points each independently resolve to a valid (talent, project) pair —
    must STOP, never silently pick one."""
    # "Ahana" alone AND "Ahana Ambig" alone are both real talents; "Test
    # Freeform Ambig" and "Freeform Ambig" are both real projects — so
    # both "Ahana | Test Freeform Ambig" and "Ahana Ambig | Freeform
    # Ambig" independently resolve.
    t1 = await _seed_talent("Ahana FFAmbig")
    t2 = await _seed_talent("Ahana FFAmbig Extra")
    p1 = await _seed_project("Extra Project FFAmbig")
    p2 = await _seed_project("Project FFAmbig")
    request_scope.reset()
    try:
        talent_text, project_text, err = await cp._resolve_freeform_talent_project(
            "Ahana FFAmbig Extra Project FFAmbig"
        )
        assert talent_text is None and project_text is None
        assert err is not None and err.ok is False
        assert err.error == "freeform_ambiguous"
    finally:
        await _cleanup(talent_ids=[t1, t2], project_ids=[p1, p2])


async def test_freeform_single_word_fails_cleanly():
    request_scope.reset()
    talent_text, project_text, err = await cp._resolve_freeform_talent_project("Ahana")
    assert talent_text is None and project_text is None
    assert err is not None and err.error == "freeform_too_short"
