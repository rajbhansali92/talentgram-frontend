"""`PUT /talents/{tid}` (admin full-form save) must NOT rewrite the media array.

The admin edit page submits the whole talent object, including `media` loaded
from a GET. `TalentUpdateIn.media` is `List[MediaItem]` (9 fields), so Pydantic
drops every extended media field on parse — `poster_url`, `thumbnail_url`,
`talent_id`, `source_*`, and the P3 `ownership` sub-document. `$set`-ing that
back erased all of it on every routine edit. `update_talent` now pops `media`
from the update entirely (media is owned by the dedicated /media endpoints).
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath("backend"))

import core
core.db = MagicMock()

from routers import talents as talents_router  # noqa: E402
from core import TalentUpdateIn  # noqa: E402


@pytest.mark.asyncio
async def test_update_talent_never_sets_media():
    existing = {
        "id": "t1", "name": "Old Name", "email": "t@example.com", "updated_at": "2026-08-01T00:00:00Z",
        "media": [
            {"id": "m1", "category": "portfolio", "url": "https://res/x.jpg", "public_id": "talentgram/talents/t1/portfolio/m1",
             "resource_type": "image", "size": 100, "created_at": "2026-07-01T00:00:00Z",
             "poster_url": None, "talent_id": "t1", "source_submission_id": "s9",
             "ownership": {"owner_type": "talent", "owner_id": "t1", "migration_version": "p3-v1"}},
        ],
    }

    captured = {}

    talents_router.db = MagicMock()
    talents_router.db.talents = MagicMock()
    talents_router.db.talents.find_one = AsyncMock(side_effect=[existing, None, {"id": "t1", "media": existing["media"]}])
    async def _update_one(q, u, **k):
        captured["set"] = u.get("$set", {})
        return MagicMock(matched_count=1, modified_count=1)
    talents_router.db.talents.update_one = AsyncMock(side_effect=_update_one)
    talents_router.db.storage_audit_log = MagicMock(insert_one=AsyncMock())
    talents_router.db.talent_audit_log = MagicMock(insert_one=AsyncMock())

    # payload mimics the admin edit page: whole talent object incl. media
    payload = TalentUpdateIn(
        name="New Name", email="t@example.com",
        media=[{"id": "m1", "category": "portfolio", "url": "https://res/x.jpg",
                "public_id": "talentgram/talents/t1/portfolio/m1", "resource_type": "image", "size": 100,
                "created_at": "2026-07-01T00:00:00Z"}],
    )

    try:
        await talents_router.update_talent("t1", payload, admin={"id": "a1", "role": "admin", "email": "a@x.com"})
    except Exception:
        # downstream enrich/response shaping may need more mocks; we only care
        # about what was written.
        pass

    assert "set" in captured, "update_one was not called"
    assert "media" not in captured["set"], (
        f"$set must not contain 'media' — it would clobber the stored array. Got keys: {sorted(captured['set'])}"
    )
    # the real field being edited is still written
    assert captured["set"].get("name") == "New Name"
