import os
import sys
import pytest
import cloudinary.exceptions
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath("backend"))

import core
mock_db = MagicMock()
core.db = mock_db

from routers.cloudinary_admin import (
    classify_media_item,
    compute_category_breakdown,
    aggregate_project_talent_totals,
    count_other_references,
    delete_one_media_item,
    resolve_full_public_id,
)


def _agg(items):
    """Build a mock .aggregate(pipeline) -> cursor whose .to_list(...) resolves to items."""
    return MagicMock(return_value=AsyncMock(to_list=AsyncMock(return_value=items)))


class TestResolveFullPublicId:
    def test_full_public_id_passes_through(self):
        item = {"public_id": "talentgram/submissions/s1/leaf", "url": "https://res.cloudinary.com/x/video/upload/v1/talentgram/submissions/s1/leaf.mp4"}
        assert resolve_full_public_id(item) == "talentgram/submissions/s1/leaf"

    def test_bare_public_id_recovered_from_url(self):
        """The actual bug found via live testing: submission_sign_upload
        returns a bare public_id (no folder), and submission_complete_upload
        stores it verbatim — but the real Cloudinary asset lives at
        {folder}/{public_id}. The `url` field always has the true path, so
        it must be used to recover the real public_id before any destroy()
        call, or the destroy silently no-ops (Cloudinary returns "not
        found" rather than raising)."""
        item = {
            "public_id": "efeb1ca8-4838-4a20-810a-517566ae844c",
            "url": "https://res.cloudinary.com/talentgram/video/upload/w_1280,h_720,c_limit,q_auto,vc_auto,f_mp4/v1787073379/talentgram/submissions/7b303a1e-ec05-439f-98de-34c7fd501da4/efeb1ca8-4838-4a20-810a-517566ae844c.mp4",
        }
        resolved = resolve_full_public_id(item)
        assert resolved == "talentgram/submissions/7b303a1e-ec05-439f-98de-34c7fd501da4/efeb1ca8-4838-4a20-810a-517566ae844c"

    def test_bare_public_id_no_transformation_segment(self):
        item = {
            "public_id": "leaf123",
            "url": "https://res.cloudinary.com/talentgram/image/upload/v1787073384/talentgram/submissions/sid/leaf123.jpg",
        }
        assert resolve_full_public_id(item) == "talentgram/submissions/sid/leaf123"

    def test_no_url_falls_back_to_stored_value(self):
        item = {"public_id": "leaf123", "url": None}
        assert resolve_full_public_id(item) == "leaf123"

    def test_no_public_id_returns_none(self):
        assert resolve_full_public_id({"url": "https://x"}) is None


class TestClassifyMediaItem:
    def test_admin_added_always_wins(self):
        assert classify_media_item({"scope": "admin_added", "category": "take"}) == "admin_uploads"
        assert classify_media_item({"admin_added": True, "category": "indian"}) == "admin_uploads"

    def test_audition_categories(self):
        assert classify_media_item({"category": "take"}) == "audition_videos"
        assert classify_media_item({"category": "take_1"}) == "audition_videos"
        assert classify_media_item({"category": "take_2"}) == "audition_videos"

    def test_intro_video(self):
        assert classify_media_item({"category": "intro_video"}) == "intro_videos"

    def test_indian_western(self):
        assert classify_media_item({"category": "indian"}) == "indian_look_images"
        assert classify_media_item({"category": "western"}) == "western_look_images"

    def test_generic_image_falls_back_to_portfolio(self):
        assert classify_media_item({"category": "image"}) == "portfolio_images"
        assert classify_media_item({"category": "additional_portfolio"}) == "portfolio_images"
        assert classify_media_item({"category": None}) == "portfolio_images"


@pytest.mark.asyncio
class TestComputeCategoryBreakdown:
    async def test_sizes_come_from_media_size_not_asset_metadata(self):
        """The core Phase 5 regression guard: category totals must be summed
        from submissions/applications media[].size, never asset_metadata."""
        mock_db.submissions.aggregate = _agg([
            {"category": "take", "size": 1000, "scope": "submission"},
            {"category": "intro_video", "size": 2000, "scope": "submission"},
        ])
        mock_db.applications.aggregate = _agg([])
        mock_db.asset_metadata.aggregate = _agg([])
        mock_db.feedback.count_documents = AsyncMock(return_value=0)

        result = await compute_category_breakdown()

        assert result["audition_videos"]["size"] == 1000
        assert result["audition_videos"]["count"] == 1
        assert result["intro_videos"]["size"] == 2000
        assert result["intro_videos"]["count"] == 1
        # asset_metadata.aggregate was never consulted for these two buckets
        # (only voice_notes reads it) — confirmed by it returning [] above
        # yet audition/intro still resolved non-zero.

    async def test_zero_size_items_dont_crash_and_still_count(self):
        mock_db.submissions.aggregate = _agg([{"category": "take", "size": 0, "scope": "submission"}])
        mock_db.applications.aggregate = _agg([])
        mock_db.asset_metadata.aggregate = _agg([])
        mock_db.feedback.count_documents = AsyncMock(return_value=0)

        result = await compute_category_breakdown()
        assert result["audition_videos"]["size"] == 0
        assert result["audition_videos"]["count"] == 1

    async def test_voice_notes_still_sourced_from_asset_metadata(self):
        mock_db.submissions.aggregate = _agg([])
        mock_db.applications.aggregate = _agg([])
        mock_db.asset_metadata.aggregate = _agg([{"_id": None, "total_size": 5000, "count": 2}])
        mock_db.feedback.count_documents = AsyncMock(return_value=2)

        result = await compute_category_breakdown()
        assert result["voice_notes"]["size"] == 5000
        assert result["voice_notes"]["count"] == 2


@pytest.mark.asyncio
class TestAggregateProjectTalentTotals:
    async def test_talent_ids_deduped_across_collections(self):
        mock_db.submissions.aggregate = _agg([
            {"_id": "proj1", "total_size": 100, "asset_count": 1, "talent_ids": ["t1", "t2"]},
        ])
        mock_db.applications.aggregate = _agg([
            {"_id": "proj1", "total_size": 50, "asset_count": 1, "talent_ids": ["t2", "t3"]},
        ])
        totals = await aggregate_project_talent_totals()
        assert totals["proj1"]["total_size"] == 150
        assert totals["proj1"]["asset_count"] == 2
        assert totals["proj1"]["talent_ids"] == {"t1", "t2", "t3"}


@pytest.mark.asyncio
class TestSharedAssetProtection:
    async def test_unshared_public_id_returns_zero(self):
        mock_db.submissions.count_documents = AsyncMock(return_value=0)
        mock_db.applications.count_documents = AsyncMock(return_value=0)
        mock_db.talents.count_documents = AsyncMock(return_value=0)
        count = await count_other_references("pid_1", "submission", "sub_1")
        assert count == 0

    async def test_shared_with_global_talent_profile_detected(self):
        mock_db.submissions.count_documents = AsyncMock(return_value=0)
        mock_db.applications.count_documents = AsyncMock(return_value=0)
        mock_db.talents.count_documents = AsyncMock(return_value=1)
        count = await count_other_references("pid_shared", "submission", "sub_1")
        assert count == 1

    async def test_excludes_the_submission_being_deleted_from(self):
        """A submission always references its own media; that self-reference
        must not count as 'shared elsewhere'."""
        mock_db.submissions.count_documents = AsyncMock(return_value=0)  # excluded via $ne
        mock_db.applications.count_documents = AsyncMock(return_value=0)
        mock_db.talents.count_documents = AsyncMock(return_value=0)
        count = await count_other_references("pid_1", "submission", "sub_1")
        assert count == 0
        # Verify the exclusion clause was actually passed through
        call_kwargs = mock_db.submissions.count_documents.call_args[0][0]
        assert call_kwargs["id"] == {"$ne": "sub_1"}


@pytest.mark.asyncio
class TestDeleteOneMediaItem:
    async def test_unshared_media_is_physically_destroyed(self):
        mock_db.submissions.count_documents = AsyncMock(return_value=0)
        mock_db.applications.count_documents = AsyncMock(return_value=0)
        mock_db.talents.count_documents = AsyncMock(return_value=0)
        mock_db.submissions.update_one = AsyncMock()
        mock_parent_coll = mock_db.submissions

        media_item = {"id": "m1", "public_id": "pid_x", "category": "image", "resource_type": "image", "url": "https://res.cloudinary.com/x/image/upload/v1/talentgram/submissions/sub_1/pid_x.jpg"}
        with patch("routers.cloudinary_admin.cleanup_media_storage", new=AsyncMock()) as mock_cleanup, \
             patch("routers.cloudinary_admin.log_storage_action", new=AsyncMock()), \
             patch("cloudinary.api.resource", side_effect=cloudinary.exceptions.NotFound("gone")):
            result = await delete_one_media_item(mock_parent_coll, "sub_1", media_item, "admin_1")

        mock_cleanup.assert_awaited_once()
        assert result["physically_deleted"] is True
        assert result["shared_refs_remaining"] == 0
        mock_parent_coll.update_one.assert_awaited_once()
        pull_arg = mock_parent_coll.update_one.call_args[0][1]
        assert pull_arg == {"$pull": {"media": {"id": "m1"}}}

    async def test_destroy_not_confirmed_reports_false(self):
        """The exact bug found via live testing: cleanup_media_storage()
        never raises even when Cloudinary's destroy() silently no-ops
        (e.g. mismatched public_id). physically_deleted must reflect a real
        post-delete Cloudinary lookup, not just "we attempted it"."""
        mock_db.submissions.count_documents = AsyncMock(return_value=0)
        mock_db.applications.count_documents = AsyncMock(return_value=0)
        mock_db.talents.count_documents = AsyncMock(return_value=0)
        mock_db.submissions.update_one = AsyncMock()
        mock_parent_coll = mock_db.submissions

        media_item = {"id": "m4", "public_id": "pid_y", "category": "image", "resource_type": "image", "url": "https://res.cloudinary.com/x/image/upload/v1/talentgram/submissions/sub_1/pid_y.jpg"}
        with patch("routers.cloudinary_admin.cleanup_media_storage", new=AsyncMock()), \
             patch("routers.cloudinary_admin.log_storage_action", new=AsyncMock()), \
             patch("cloudinary.api.resource", return_value={"bytes": 123}):  # still exists!
            result = await delete_one_media_item(mock_parent_coll, "sub_1", media_item, "admin_1")

        assert result["physically_deleted"] is False

    async def test_shared_media_only_pulls_local_reference(self):
        """Shared/global-asset protection: when the public_id is referenced
        by the talent's global profile, the Cloudinary asset must NOT be
        destroyed — only the local submission reference is removed."""
        mock_db.submissions.count_documents = AsyncMock(return_value=0)
        mock_db.applications.count_documents = AsyncMock(return_value=0)
        mock_db.talents.count_documents = AsyncMock(return_value=1)  # shared with global talent
        mock_db.submissions.update_one = AsyncMock()
        mock_parent_coll = mock_db.submissions

        media_item = {"id": "m2", "public_id": "pid_shared", "category": "indian", "resource_type": "image"}
        with patch("routers.cloudinary_admin.cleanup_media_storage", new=AsyncMock()) as mock_cleanup, \
             patch("routers.cloudinary_admin.log_storage_action", new=AsyncMock()):
            result = await delete_one_media_item(mock_parent_coll, "sub_1", media_item, "admin_1")

        mock_cleanup.assert_not_awaited()
        assert result["physically_deleted"] is False
        assert result["shared_refs_remaining"] == 1
        mock_parent_coll.update_one.assert_awaited_once()

    async def test_audition_take_skips_reference_scan(self):
        """Audition takes are architecturally never reusable/global, so the
        reference-count scan is skipped entirely for them (perf + always-safe)."""
        mock_db.submissions.count_documents = AsyncMock()
        mock_db.applications.count_documents = AsyncMock()
        mock_db.talents.count_documents = AsyncMock()
        mock_db.submissions.update_one = AsyncMock()
        mock_parent_coll = mock_db.submissions

        media_item = {"id": "m3", "public_id": "pid_take", "category": "take", "resource_type": "video", "url": "https://res.cloudinary.com/x/video/upload/v1/talentgram/submissions/sub_1/pid_take.mp4"}
        with patch("routers.cloudinary_admin.cleanup_media_storage", new=AsyncMock()) as mock_cleanup, \
             patch("routers.cloudinary_admin.log_storage_action", new=AsyncMock()), \
             patch("cloudinary.api.resource", side_effect=cloudinary.exceptions.NotFound("gone")):
            result = await delete_one_media_item(mock_parent_coll, "sub_1", media_item, "admin_1")

        mock_db.submissions.count_documents.assert_not_awaited()
        mock_db.talents.count_documents.assert_not_awaited()
        mock_cleanup.assert_awaited_once()
        assert result["physically_deleted"] is True
