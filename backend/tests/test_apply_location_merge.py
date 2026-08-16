import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock

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

sys.path.insert(0, os.path.abspath("backend"))

import core
mock_db = MagicMock()
core.db = mock_db

from routers.applications import _promote_application_to_talent


def _existing_talent():
    return {
        "id": "talent-apply-1",
        "name": "Priya Shah",
        "email": "priya@example.com",
        "normalized_email": "priya@example.com",
        "location": [{"city": "Mumbai", "country": "India"}],
        # No `updated_at` — merge_talent_profile treats a talent with no
        # canonical clock as always-fresh, so the merge isn't skipped by
        # the (unrelated) freshness gate in this test.
    }


def _app_doc(location):
    return {
        "id": "app-1",
        "talent_email": "priya@example.com",
        "talent_name": "Priya Shah",
        "media": [],
        "form_data": {
            "first_name": "Priya",
            "last_name": "Shah",
            "location": location,
        },
    }


@pytest.mark.asyncio
async def test_apply_finalize_does_not_overwrite_existing_talent_location():
    """An existing talent applying via /apply with a different application
    location must NOT have their global db.talents.location overwritten —
    same rule /submit's finalize already enforces. Regression test for the
    bug where applications.py's merge (unlike submissions.py's) never
    popped `location` before calling merge_talent_profile()."""
    mock_db.talents.find_one = AsyncMock(return_value=_existing_talent())
    mock_db.talents.update_one = AsyncMock()
    mock_db.profile_audits.insert_one = AsyncMock()

    app_doc = _app_doc(location=[{"city": "Dubai", "country": "United Arab Emirates"}])

    talent_id, merged = await _promote_application_to_talent(app_doc, admin_id=None, source="application")

    assert talent_id == "talent-apply-1"
    assert merged is True

    # Find the update_one call that actually carries a $set patch (cover
    # cache's own update_one, if invoked, has a different shape).
    location_touching_calls = [
        call for call in mock_db.talents.update_one.call_args_list
        if "location" in (call.args[1].get("$set") or {})
    ]
    assert location_touching_calls == [], (
        f"expected no update_one call to touch `location`, got: {location_touching_calls}"
    )


@pytest.mark.asyncio
async def test_apply_finalize_seeds_location_for_brand_new_talent():
    """A brand-new talent (no existing db.talents record) applying via
    /apply SHOULD have their initial location seed the global profile —
    the location pop fix must only apply to the existing-talent merge
    branch, not new-talent creation."""
    mock_db.talents.find_one = AsyncMock(return_value=None)
    mock_db.talents.insert_one = AsyncMock()
    mock_db.talents.update_one = AsyncMock()

    app_doc = _app_doc(location=[{"city": "Dubai", "country": "United Arab Emirates"}])
    app_doc["talent_email"] = "brandnew@example.com"
    app_doc["form_data"]["email"] = "brandnew@example.com"

    talent_id, merged = await _promote_application_to_talent(app_doc, admin_id=None, source="application")

    assert merged is False
    assert mock_db.talents.insert_one.called
    inserted_doc = mock_db.talents.insert_one.call_args[0][0]
    assert inserted_doc.get("location") == [{"city": "Dubai", "country": "United Arab Emirates"}]
