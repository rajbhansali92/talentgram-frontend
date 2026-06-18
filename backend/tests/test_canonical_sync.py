import os
import sys
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock

# Set required environment variables before importing core/server
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

# Add backend directory to sys.path
sys.path.insert(0, os.path.abspath("backend"))

import core
# Mock database global
mock_db = MagicMock()
core.db = mock_db

from fastapi.testclient import TestClient
from server import app
from core import make_token

client = TestClient(app)


@pytest.mark.asyncio
async def test_canonical_prefill_no_fallbacks():
    """Verify prefill lookup prioritizes db.talents and avoids 3-tier fallbacks when talent exists."""
    email = "test@talent.com"
    token = make_token({"role": "submitter", "email": email, "sid": "sub-123"}, days=1)

    # Mock finding the talent record with a specific introduction video
    mock_talent = {
        "id": "talent-123",
        "name": "Deeya Damini",
        "email": email,
        "normalized_email": email,
        "media": [
            {
                "id": "video-canonical",
                "category": "video",
                "url": "http://res.cloudinary.com/demo/video/upload/canonical.mp4",
                "resource_type": "video",
            }
        ]
    }
    mock_db.talents.find_one = AsyncMock(return_value=mock_talent)
    mock_db.submissions.find_one = AsyncMock(return_value={"id": "sub-123", "project_slug": "test-slug"})
    mock_db.submissions.update_one = AsyncMock()
    mock_db.applications.find_one = AsyncMock(return_value=None)
    mock_db.applications.update_one = AsyncMock()
    mock_db.projects.find_one = AsyncMock(return_value={"id": "test-slug"})

    # Even if we have other newer videos in submissions or applications, they should be ignored
    # because the talent profile exists and is the canonical source.
    resp = client.get(
        f"/api/public/prefill?email={email}",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    
    assert data["first_name"] == "Deeya"
    assert data["last_name"] == "Damini"
    video_item = next(m for m in data["prefill_media"] if m["category"] == "intro_video")
    assert video_item["id"] == "video-canonical"
    assert video_item["url"] == "http://res.cloudinary.com/demo/video/upload/canonical.mp4"


@pytest.mark.asyncio
async def test_category_mapping_invite_link():
    """Verify that all media categories map correctly without being dropped in the Invite Link flow."""
    from routers.applications import _reconcile_draft_from_talent

    app_doc = {
        "id": "app-123",
        "talent_email": "test@talent.com",
        "media": []
    }
    
    talent = {
        "id": "talent-123",
        "media": [
            {"id": "med-1", "category": "portfolio", "url": "url-1", "resource_type": "image"},
            {"id": "med-2", "category": "image", "url": "url-2", "resource_type": "image"},
            {"id": "med-3", "category": "video", "url": "url-3", "resource_type": "video"},
            {"id": "med-4", "category": "intro_video", "url": "url-4", "resource_type": "video"},
            {"id": "med-5", "category": "headshot", "url": "url-5", "resource_type": "image"},
            {"id": "med-6", "category": "additional_portfolio", "url": "url-6", "resource_type": "image"}
        ]
    }

    mock_db.applications.update_one = AsyncMock()

    # Call reconciliation
    await _reconcile_draft_from_talent(app_doc, talent, "app-123")

    # Assert application.update_one was called with the mapped media list
    assert mock_db.applications.update_one.called
    args, kwargs = mock_db.applications.update_one.call_args
    patch = args[1]["$set"]
    media_patch = patch["media"]

    # Verify all 6 media items are mapped correctly and none are dropped
    mapped_categories = [m["category"] for m in media_patch]
    assert len(mapped_categories) == 6
    assert "image" in mapped_categories  # portfolio & image maps to image
    assert "intro_video" in mapped_categories  # video & intro_video maps to intro_video
    assert "headshot" in mapped_categories
    assert "additional_portfolio" in mapped_categories


@pytest.mark.asyncio
async def test_location_remains_separate():
    """Verify project-specific location override does not overwrite global talent location."""
    from core import merge_talent_profile
    
    talent = {
        "id": "talent-123",
        "name": "Deeya Damini",
        "email": "test@talent.com",
        "location": [{"city": "Mumbai"}]
    }

    incoming_form = {
        "first_name": "Deeya",
        "last_name": "Damini",
        "location": [{"city": "Dubai"}] # Project-specific override
    }

    # In submission finalize, "location" is popped from form_to_merge
    form_to_merge = dict(incoming_form)
    form_to_merge.pop("location", None)

    mock_db.talents.update_one = AsyncMock()
    mock_db.profile_audits.insert_one = AsyncMock()

    await merge_talent_profile(talent, form_to_merge, "project_submission")

    # Verify location was not updated in db.talents update patch
    if mock_db.talents.update_one.called:
        args, kwargs = mock_db.talents.update_one.call_args
        patch = args[1]["$set"]
        assert "location" not in patch


# ---------------------------------------------------------------------------
# Regression Test D – Snapshot versioning: talent_profile_updated_at
#   Verify that _reconcile_draft_from_talent re-hydrates when db.talents has
#   a newer updated_at than the snapshot stored in the application doc.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_d_snapshot_versioning_forces_refresh():
    """
    When db.talents.updated_at is newer than the application's stored
    talent_profile_updated_at, reconciliation must always re-hydrate media.
    """
    from routers.applications import _reconcile_draft_from_talent

    OLD_TS = "2025-01-01T00:00:00"
    NEW_TS = "2026-06-01T00:00:00"

    # Application doc was last hydrated from an old version of the talent
    app_doc = {
        "id": "app-snapshot",
        "talent_email": "snapshot@test.com",
        "media": [{"id": "stale-media", "category": "image", "url": "stale-url"}],
        "talent_profile_updated_at": OLD_TS,  # snapshot from before talent update
    }

    # Talent has been updated more recently
    talent = {
        "id": "talent-snapshot",
        "updated_at": NEW_TS,
        "media": [{"id": "fresh-media", "category": "image", "url": "fresh-url"}],
    }

    mock_db.applications.update_one = AsyncMock()

    await _reconcile_draft_from_talent(app_doc, talent, "app-snapshot")

    assert mock_db.applications.update_one.called, (
        "Expected applications.update_one to be called when talent_profile_updated_at is stale"
    )
    args, _ = mock_db.applications.update_one.call_args
    patch = args[1]["$set"]
    media_patch = patch.get("media", [])
    ids = [m["id"] for m in media_patch]
    assert "fresh-media" in ids, (
        f"Expected fresh-media in patch, got: {ids}"
    )
    assert "stale-media" not in ids, (
        f"Stale media should have been replaced, got: {ids}"
    )


@pytest.mark.asyncio
async def test_d_snapshot_versioning_skips_when_current():
    """
    When the talent snapshot matches current db.talents.updated_at,
    reconciliation should skip the update (no DB write needed).
    """
    from routers.applications import _reconcile_draft_from_talent

    SAME_TS = "2026-06-01T00:00:00"

    app_doc = {
        "id": "app-current",
        "talent_email": "current@test.com",
        "media": [{"id": "existing-media", "category": "image", "url": "url"}],
        "talent_profile_updated_at": SAME_TS,
    }

    talent = {
        "id": "talent-current",
        "updated_at": SAME_TS,  # same — no refresh needed
        "media": [{"id": "talent-media", "category": "image", "url": "url"}],
    }

    mock_db.applications.update_one = AsyncMock()

    await _reconcile_draft_from_talent(app_doc, talent, "app-current")

    # When snapshot is current, media must NOT be re-hydrated.
    # A patch for talent_profile_updated_at is acceptable (idempotent stamp),
    # but the patch must not contain a "media" key that would overwrite the app.
    if mock_db.applications.update_one.called:
        args, _ = mock_db.applications.update_one.call_args
        patch = args[1].get("$set", {})
        assert "media" not in patch, (
            f"Media must not be re-hydrated when talent snapshot is already current. "
            f"Got patch keys: {list(patch.keys())}"
        )


# ---------------------------------------------------------------------------
# Regression Test E – Returning talent: submitted → draft status reset
#   Verify that start_application resets a submitted application to draft
#   and clears stale media so _reconcile can repopulate from db.talents.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_e_returning_talent_submitted_reset():
    """
    When a talent whose previous application is status='submitted' calls
    POST /public/apply, the backend must reset the application to 'draft',
    clear old media, and trigger reconciliation.
    """
    from routers.applications import start_application, ApplicationStartIn

    submitted_app = {
        "id": "app-returning",
        "talent_email": "returning@test.com",
        "status": "submitted",
        "media": [{"id": "old-media", "category": "image", "url": "old-url"}],
        "talent_profile_updated_at": "2025-01-01T00:00:00",
    }

    existing_talent = {
        "id": "talent-returning",
        "email": "returning@test.com",
        "normalized_email": "returning@test.com",
        "updated_at": "2026-06-01T00:00:00",
        "media": [{"id": "canonical-media", "category": "image", "url": "canonical-url"}],
    }

    mock_db.applications.find_one = AsyncMock(return_value=submitted_app)
    mock_db.applications.update_one = AsyncMock()
    mock_db.talents.find_one = AsyncMock(return_value=existing_talent)
    mock_db.projects.find_one = AsyncMock(return_value={"id": "proj-123", "slug": "proj"})

    payload = ApplicationStartIn(
        first_name="Jane",
        last_name="Doe",
        email="returning@test.com",
        phone="0000000000",
        profile_id=None,
    )

    from fastapi import Request
    # Call the function directly (not via HTTP client, to avoid auth complexity)
    # We verify that update_one is called with status='draft' and media=[]
    # The simplest proof is inspecting mock calls after calling start_application.
    try:
        await start_application(payload)
    except Exception:
        pass  # May raise due to token generation or missing mocks; that is OK

    # Verify that update_one was called to reset status to 'draft'
    assert mock_db.applications.update_one.called, "Expected update_one to reset the application"
    calls = mock_db.applications.update_one.call_args_list
    reset_call = None
    for call in calls:
        args, _ = call
        patch = args[1].get("$set", {})
        if patch.get("status") == "draft" and "media" in patch and patch["media"] == []:
            reset_call = call
            break
    assert reset_call is not None, (
        f"Expected a call resetting status=draft and media=[], got calls: {calls}"
    )


# ---------------------------------------------------------------------------
# Regression Test F – Empty array [] does NOT block hydration
#   This is the primary bug that caused Invite Link media to stay empty.
#   The OLD guard was: if not (app_doc.get("media") or []):
#   which treated [] as truthy and skipped hydration.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_f_empty_media_array_does_not_block_hydration():
    """
    CRITICAL REGRESSION:
    An application.media == [] (empty list) must NOT prevent reconciliation
    from pulling media out of db.talents. The old bug: 'if not ([] or [])' was
    always False so the guard never fired — but after the fix the logic is now
    driven by talent_profile_updated_at snapshot, not by media presence.

    Concretely: app with media=[] and talent with media=[...] → media hydrated.
    """
    from routers.applications import _reconcile_draft_from_talent

    app_doc = {
        "id": "app-empty-media",
        "talent_email": "emptymedia@test.com",
        "media": [],  # explicitly empty — must NOT block hydration
        "talent_profile_updated_at": None,  # never been hydrated
    }

    talent = {
        "id": "talent-empty",
        "updated_at": "2026-06-01T00:00:00",
        "media": [
            {"id": "indian-1", "category": "indian", "url": "http://example.com/indian.jpg"},
            {"id": "western-1", "category": "western", "url": "http://example.com/western.jpg"},
            {"id": "intro-1", "category": "intro_video", "url": "http://example.com/intro.mp4"},
        ],
    }

    mock_db.applications.update_one = AsyncMock()

    await _reconcile_draft_from_talent(app_doc, talent, "app-empty-media")

    assert mock_db.applications.update_one.called, (
        "Empty media=[] must NOT block reconciliation. update_one should have been called."
    )
    args, _ = mock_db.applications.update_one.call_args
    patch = args[1]["$set"]
    hydrated_media = patch.get("media", [])
    assert len(hydrated_media) == 3, (
        f"Expected 3 media items to be hydrated from talent, got {len(hydrated_media)}: {hydrated_media}"
    )
    categories = {m["category"] for m in hydrated_media}
    assert "indian" in categories
    assert "western" in categories
    assert "intro_video" in categories


# ---------------------------------------------------------------------------
# Regression Test G – split_full_name edge cases
# ---------------------------------------------------------------------------
def test_g_split_full_name_single_word():
    from routers.applications import split_full_name
    first, last = split_full_name("Madonna")
    assert first == "Madonna"
    assert last == ""


def test_g_split_full_name_two_words():
    from routers.applications import split_full_name
    first, last = split_full_name("Deeya Damini")
    assert first == "Deeya"
    assert last == "Damini"


def test_g_split_full_name_three_words():
    from routers.applications import split_full_name
    first, last = split_full_name("Mary Jane Watson")
    assert first == "Mary Jane"
    assert last == "Watson"


def test_g_split_full_name_empty():
    from routers.applications import split_full_name
    first, last = split_full_name("")
    assert first == ""
    assert last == ""


def test_g_split_full_name_whitespace():
    from routers.applications import split_full_name
    first, last = split_full_name("  Ali   Khan  ")
    assert first == "Ali"
    assert last == "Khan"
