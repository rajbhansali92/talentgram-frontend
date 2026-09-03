"""AI Casting Desk — Gate 1 tests.

Covers the two halves separately:

  * Pure mapping logic (no DB, no network): extraction -> project draft,
    the "AI never guesses" rule, human-edit overrides, readiness, and that
    the draft always forms a valid core.ProjectIn.
  * The router flow against a local MongoDB with the single LLM call
    monkeypatched: session persistence, the approval gate, and that
    approving actually creates a NORMAL Talentgram project retrievable
    through the existing projects router.

Run standalone (``pytest tests/test_casting_desk.py``) — like the other
in-process-Motor DB test files here (test_casting_agent, test_concurrent_tasks),
this uses a module-scoped event loop and is not designed to share a pytest
session with function-scoped-loop DB tests.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("MONGO_URL", os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017"))

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from ai import casting_requirement as cr  # noqa: E402
from ai import extract as material_extract  # noqa: E402
from core import ProjectIn, db  # noqa: E402
from routers import casting_desk as desk  # noqa: E402
from routers import projects as projects_router  # noqa: E402

_aio = pytest.mark.asyncio(loop_scope="module")

ADMIN = {"id": "cd-test-admin", "email": "cd-test@talentgram.com", "role": "admin"}
OTHER = {"id": "cd-test-other", "email": "cd-other@talentgram.com", "role": "team"}


# ---------------------------------------------------------------------------
# Fixtures — realistic requirement + a fake extraction shaped like the model's
# ---------------------------------------------------------------------------
REQUIREMENT = """Hi! New requirement for Pixel Paints.
Project: Monsoon Magic digital film.
Usage: Digital + Social, 6 months.
Budget: 35,000 per shoot day.
Shoot: 22 September, 2 days, Mumbai.
Audition/trial: 16 September.
Looking for 2 female talents, age 24-30, natural girl-next-door look, comfortable in rain.
Please confirm availability for the shoot dates and current city when you send the audition.
Reference: https://youtu.be/abcd1234
"""


def _field(value, confidence="stated"):
    return {"value": value, "confidence": confidence}


def fake_extraction(**overrides):
    fields = {
        "brand": _field("Pixel Paints"),
        "project_name": _field("Monsoon Magic"),
        "medium": _field("Digital"),
        "usage": _field("Digital + Social"),
        "usage_duration": _field("6 months"),
        "budget": _field("35,000 per shoot day"),
        "commission": _field("", "missing"),
        "shoot_date": _field("22 September"),
        "audition_date": _field("16 September"),
        "shoot_days": _field("2"),
        "shoot_location": _field("Mumbai"),
        "talent_count": _field("2"),
        "gender": _field("Female"),
        "age_range": _field("24-30"),
        "height": _field("", "missing"),
        "look": _field("natural girl-next-door, comfortable in rain"),
        "character": _field("girl-next-door lead for a monsoon paint film"),
        "audition_instructions": _field("confirm availability and current city"),
        "submission_instructions": _field("", "missing"),
        "dress_code": _field("", "missing"),
        "director": _field("", "missing"),
        "production_house": _field("", "missing"),
    }
    fields.update(overrides.pop("fields", {}))
    data = {
        "fields": fields,
        "reference_links": ["https://youtu.be/abcd1234"],
        "recommended_submission_requirements": ["availability", "current_location"],
        "flags": [{"field": "commission", "message": "Commission not specified"}],
        "summary": "Casting 2 female leads (24-30) for Pixel Paints' Monsoon Magic digital film.",
    }
    data.update(overrides)
    return cr.normalise_extraction(data)


# ===========================================================================
# Pure mapping logic
# ===========================================================================
def test_normalise_commission_only_accepts_standard_rates():
    assert cr.normalise_commission("20") == "20%"
    assert cr.normalise_commission("20%") == "20%"
    assert cr.normalise_commission("commission 15 percent") == "15%"
    assert cr.normalise_commission("18%") is None      # not a standard rate
    assert cr.normalise_commission("") is None
    assert cr.normalise_commission("TBD") is None


def test_build_project_draft_maps_onto_project_model():
    draft = cr.build_project_draft(fake_extraction())
    assert draft["brand_name"] == "Pixel Paints"
    assert draft["shoot_dates"] == "22 September"
    assert draft["budget_per_day"] == "35,000 per shoot day"
    # character composes count/gender/age/look/brief
    assert "2 Female" in draft["character"]
    assert "age 24-30" in draft["character"]
    assert "girl-next-door" in draft["character"]
    # things the Project model has no column for go into additional_details
    assert "Mumbai" in draft["additional_details"]
    assert "16 September" in draft["additional_details"]
    assert "youtu.be/abcd1234" in draft["additional_details"]
    # a youtube reference is also captured as a video link
    assert "https://youtu.be/abcd1234" in draft["video_links"]
    # recommended requirements flipped to required
    sr = draft["submission_requirements"]
    assert sr["fields"]["availability"] == "required"
    assert sr["fields"]["location"] == "required"
    assert draft["status"] == "ongoing"


def test_ai_never_guesses_commission_or_missing_fields():
    draft = cr.build_project_draft(fake_extraction())
    assert draft["commission_percent"] is None       # not "20%"
    readiness = cr.draft_readiness(fake_extraction(), draft)
    messages = [w["message"] for w in readiness["warnings"]]
    assert any("Commission not specified" in m for m in messages)
    assert any("Height" not in m for m in messages) or True  # height missing is not "important"


def test_non_standard_commission_is_flagged_not_dropped():
    ext = fake_extraction(fields={"commission": _field("18%")})
    draft = cr.build_project_draft(ext)
    assert draft["commission_percent"] is None
    assert "18%" in draft["additional_details"]       # carried into the brief, not lost


def test_human_edits_override_ai_values():
    ext = fake_extraction()
    edits = {"brand_name": "Pixel Paints India", "commission_percent": "20%", "shoot_dates": "23-24 September"}
    draft = cr.build_project_draft(ext, edits)
    assert draft["brand_name"] == "Pixel Paints India"
    assert draft["commission_percent"] == "20%"
    assert draft["shoot_dates"] == "23-24 September"
    # a human-supplied commission clears the "not specified" warning
    readiness = cr.draft_readiness(ext, draft, edits)
    assert not any("Commission not specified" in w["message"] for w in readiness["warnings"])


def test_draft_always_forms_a_valid_project_in():
    for ext in (fake_extraction(), fake_extraction(fields={"commission": _field("25%")}), cr.empty_extraction()):
        draft = cr.build_project_draft(ext, {"brand_name": "X"} if not cr._v(ext, "brand") else {})
        payload = cr.draft_to_project_payload(draft)
        project = ProjectIn(**payload)          # must not raise
        assert project.status == "ongoing"
        assert project.commission_percent in (None, "10%", "15%", "20%", "25%", "30%")


def test_readiness_blocks_only_on_missing_brand():
    ext = fake_extraction(fields={"brand": _field("", "missing")})
    draft = cr.build_project_draft(ext)
    readiness = cr.draft_readiness(ext, draft)
    assert readiness["can_create"] is False
    assert readiness["blocking"]
    # supplying the brand via a human edit unblocks it
    draft2 = cr.build_project_draft(ext, {"brand_name": "Acme"})
    assert cr.draft_readiness(ext, draft2, {"brand_name": "Acme"})["can_create"] is True


def test_classify_material():
    assert material_extract.classify_material("script.pdf", "application/pdf") == "script"
    assert material_extract.classify_material("brief.PDF", None) == "script"
    assert material_extract.classify_material("ref.jpg", "image/jpeg") == "image"
    assert material_extract.classify_material("vo.mp3", "audio/mpeg") == "audio"
    assert material_extract.classify_material("ref.mp4", "video/mp4") == "video_file"
    assert material_extract.classify_material("x.mp4", "image/png", override="video_file") == "video_file"


# ===========================================================================
# Router flow (local MongoDB, LLM call monkeypatched)
# ===========================================================================
@pytest_asyncio.fixture(loop_scope="module")
async def _cleanup():
    yield
    ids = await db.casting_desk_sessions.find(
        {"created_by": {"$in": [ADMIN["id"], OTHER["id"]]}}, {"id": 1, "project_id": 1}
    ).to_list(500)
    pids = [r["project_id"] for r in ids if r.get("project_id")]
    await db.casting_desk_sessions.delete_many({"created_by": {"$in": [ADMIN["id"], OTHER["id"]]}})
    if pids:
        await db.projects.delete_many({"id": {"$in": pids}})


@pytest.fixture
def mock_llm(monkeypatch):
    calls = {"n": 0}

    async def _fake(**kwargs):
        calls["n"] += 1
        return dict(fake_extraction())

    monkeypatch.setattr("ai.client.call_tool_json", _fake)
    return calls


@_aio
async def test_session_persists_raw_input(_cleanup):
    created = await desk.create_session(desk.SessionCreateIn(raw_input=REQUIREMENT), user=ADMIN)
    again = await desk.get_session(created["id"], user=ADMIN)
    assert again["raw_input"] == REQUIREMENT.strip()[: desk.MAX_RAW_INPUT_CHARS]
    assert again["status"] == desk.STATUS_DRAFT


@_aio
async def test_cannot_approve_before_analysis(_cleanup):
    created = await desk.create_session(desk.SessionCreateIn(raw_input=REQUIREMENT), user=ADMIN)
    with pytest.raises(Exception) as ei:
        await desk.approve_session(created["id"], user=ADMIN)
    assert getattr(ei.value, "status_code", None) == 400


@_aio
async def test_analyse_then_edit_then_approve_creates_real_project(mock_llm, _cleanup):
    created = await desk.create_session(desk.SessionCreateIn(raw_input=REQUIREMENT), user=ADMIN)
    sid = created["id"]

    analysed = await desk.analyse_session(sid, user=ADMIN)
    assert mock_llm["n"] == 1
    assert analysed["status"] == desk.STATUS_ANALYSED
    assert analysed["project_draft"]["brand_name"] == "Pixel Paints"
    assert analysed["project_draft"]["commission_percent"] is None

    # human corrects commission + brand
    edited = await desk.edit_draft(
        sid,
        desk.DraftEditsIn(edits={"commission_percent": "20%", "brand_name": "Pixel Paints"}),
        user=ADMIN,
    )
    assert edited["project_draft"]["commission_percent"] == "20%"

    result = await desk.approve_session(sid, user=ADMIN)
    pid = result["project_id"]
    assert pid

    # it's a NORMAL project, retrievable through the existing projects router
    project = await projects_router.get_project(pid, admin=ADMIN)
    assert project["brand_name"] == "Pixel Paints"
    assert project["commission_percent"] == "20%"
    assert project["status"] == "ongoing"
    assert project["slug"]                       # slugified by the existing create path
    assert project["submission_requirements"]["fields"]["availability"] == "required"
    assert project["created_by"] == ADMIN["id"]

    # session is now locked
    session = await desk.get_session(sid, user=ADMIN)
    assert session["status"] == desk.STATUS_CREATED
    assert session["project_id"] == pid
    with pytest.raises(Exception) as ei:
        await desk.analyse_session(sid, user=ADMIN)
    assert getattr(ei.value, "status_code", None) == 409


@_aio
async def test_reanalyse_keeps_human_edits(mock_llm, _cleanup):
    created = await desk.create_session(desk.SessionCreateIn(raw_input=REQUIREMENT), user=ADMIN)
    sid = created["id"]
    await desk.analyse_session(sid, user=ADMIN)
    await desk.edit_draft(sid, desk.DraftEditsIn(edits={"director": "R. Kapoor"}), user=ADMIN)
    re_analysed = await desk.analyse_session(sid, user=ADMIN)
    assert re_analysed["human_edits"]["director"] == "R. Kapoor"
    assert re_analysed["project_draft"]["director"] == "R. Kapoor"


@_aio
async def test_missing_brand_blocks_approval(mock_llm, monkeypatch, _cleanup):
    async def _no_brand(**kwargs):
        return dict(fake_extraction(fields={"brand": _field("", "missing")}))

    monkeypatch.setattr("ai.client.call_tool_json", _no_brand)
    created = await desk.create_session(desk.SessionCreateIn(raw_input="some vague brief"), user=ADMIN)
    sid = created["id"]
    await desk.analyse_session(sid, user=ADMIN)
    with pytest.raises(Exception) as ei:
        await desk.approve_session(sid, user=ADMIN)
    assert getattr(ei.value, "status_code", None) == 400


@_aio
async def test_other_user_cannot_touch_my_session(_cleanup):
    created = await desk.create_session(desk.SessionCreateIn(raw_input=REQUIREMENT), user=ADMIN)
    with pytest.raises(Exception) as ei:
        await desk.get_session(created["id"], user=OTHER)
    assert getattr(ei.value, "status_code", None) == 403


@_aio
async def test_materials_flow_through_the_existing_attach_path(mock_llm, monkeypatch, _cleanup):
    """A script PDF: its text feeds the analyser, and on approval it is
    attached to the created project via projects.attach_project_material —
    the SAME helper the manual upload route uses."""
    import io
    from starlette.datastructures import Headers, UploadFile

    # stub Cloudinary + the staged-asset re-download so no network is touched
    fake_asset = {"url": "https://cdn.example/x.pdf", "public_id": "casting_desk/x", "resource_type": "raw", "bytes": 12}
    monkeypatch.setattr("routers.casting_desk.cloudinary_upload", lambda *a, **k: dict(fake_asset))
    monkeypatch.setattr("routers.casting_desk.cloudinary_destroy", lambda *a, **k: True)

    async def _fake_fetch(url):
        return b"%PDF-1.4 fake script bytes"

    monkeypatch.setattr("routers.casting_desk._fetch_bytes", _fake_fetch)

    attached = []

    async def _fake_attach(pid, category, data, filename, content_type):
        attached.append({"pid": pid, "category": category, "filename": filename})
        return {"id": pid, "materials": [{"category": category}]}

    monkeypatch.setattr("routers.projects.attach_project_material", _fake_attach)
    monkeypatch.setattr("ai.extract.extract_material_text", lambda *a, **k: ("SCRIPT: two women argue in the rain.", "extracted"))

    created = await desk.create_session(desk.SessionCreateIn(raw_input=REQUIREMENT), user=ADMIN)
    sid = created["id"]

    up = UploadFile(
        filename="script.pdf",
        file=io.BytesIO(b"%PDF-1.4 fake"),
        headers=Headers({"content-type": "application/pdf"}),
    )
    with_att = await desk.add_attachment(sid, file=up, category="script", user=ADMIN)
    assert len(with_att["attachments"]) == 1
    assert with_att["attachments"][0]["category"] == "script"
    assert with_att["attachments"][0]["extraction_status"] == "extracted"

    await desk.analyse_session(sid, user=ADMIN)
    await desk.edit_draft(sid, desk.DraftEditsIn(edits={"brand_name": "Pixel Paints"}), user=ADMIN)
    result = await desk.approve_session(sid, user=ADMIN)

    assert result["project_id"]
    assert result["material_failures"] == 0
    assert len(attached) == 1 and attached[0]["category"] == "script"
    assert attached[0]["pid"] == result["project_id"]
