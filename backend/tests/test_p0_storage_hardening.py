import os
import sys
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException

# Add backend directory to sys.path
sys.path.insert(0, os.path.abspath("backend"))

import core
# Mock database global
mock_db = MagicMock()
core.db = mock_db

from core import check_cloudinary_health, check_r2_health, upload_and_track_asset
from routers.cloudinary_admin import assert_providers_healthy

@pytest.mark.asyncio
async def test_p0_2_assert_providers_healthy():
    """Verify that assert_providers_healthy checks health and aborts on outages."""
    # Temporarily set R2 endpoint to activate the health gate
    with patch("core.R2_ENDPOINT_URL", "http://localhost"):
        # Cloudinary down
        with patch("routers.cloudinary_admin.check_cloudinary_health", return_value=False), \
             patch("routers.cloudinary_admin.check_r2_health", return_value=True):
            with pytest.raises(HTTPException) as exc_info:
                await assert_providers_healthy()
            assert exc_info.value.status_code == 503
            assert "Cloudinary is currently unreachable" in exc_info.value.detail

        # R2 down
        with patch("routers.cloudinary_admin.check_cloudinary_health", return_value=True), \
             patch("routers.cloudinary_admin.check_r2_health", return_value=False):
            with pytest.raises(HTTPException) as exc_info:
                await assert_providers_healthy()
            assert exc_info.value.status_code == 503
            assert "Cloudflare R2 is currently unreachable" in exc_info.value.detail

        # Both up
        with patch("routers.cloudinary_admin.check_cloudinary_health", return_value=True), \
             patch("routers.cloudinary_admin.check_r2_health", return_value=True):
            # Should not raise exception
            await assert_providers_healthy()

@pytest.mark.asyncio
async def test_p0_1_transactional_upload_failure():
    """Verify that a failed upload does not delete anything, keeping previous assets safe."""
    mock_db.talents.find_one = AsyncMock(return_value={"id": "t1", "name": "Test"})
    mock_db.asset_metadata.update_one = AsyncMock()

    with patch("core.cloudinary_upload", side_effect=ValueError("Upload Error")), \
         patch("core.cleanup_media_storage") as mock_cleanup:
        
        with pytest.raises(ValueError):
            await upload_and_track_asset(
                data=b"dummy",
                resource_type="image",
                content_type="image/jpeg",
                asset_type="profile_image",
                talent_id="t1",
                submission_id="s1",
                project_id="p1"
            )
        
        # Cleanup should not be called since we didn't succeed
        mock_cleanup.assert_not_called()

def test_safe_get_usage():
    """Verify that safe_get_usage extracts values correctly from various types."""
    from routers.cloudinary_admin import safe_get_usage
    
    # Dict cases
    assert safe_get_usage({"usage": 100, "limit": 200}, 50) == (100, 200)
    assert safe_get_usage({"usage": "150", "limit": "300"}, 50) == (150, 300)
    assert safe_get_usage({"usage": None, "limit": None}, 50) == (0, 50)
    assert safe_get_usage({"usage": "invalid", "limit": "invalid"}, 50) == (0, 50)
    
    # Int/float cases
    assert safe_get_usage(500, 100) == (500, 100)
    assert safe_get_usage(123.45, 100) == (123, 100)
    
    # None/invalid cases
    assert safe_get_usage(None, 80) == (0, 80)
    assert safe_get_usage("some_string", 80) == (0, 80)

@pytest.mark.asyncio
async def test_get_storage_analytics_fallback():
    """Verify that get_storage_analytics loads even if Cloudinary and R2 are unavailable."""
    from routers.cloudinary_admin import get_storage_analytics
    
    mock_db.asset_metadata.aggregate = MagicMock()
    # Mock cursors returning empty lists to simulate empty datasets
    mock_cursor = AsyncMock()
    mock_cursor.to_list.return_value = []
    mock_db.asset_metadata.aggregate.return_value = mock_cursor
    
    mock_db.feedback.count_documents = AsyncMock(return_value=0)
    
    with patch("routers.cloudinary_admin.fetch_cloudinary_usage_sync", return_value={}), \
         patch("routers.cloudinary_admin.get_r2_client", return_value=None):
        
        res = await get_storage_analytics(admin={"id": "test_admin"})
        assert res["total_storage"] == 0
        assert res["providers"]["cloudinary"]["status"] == "unavailable"
        assert res["providers"]["cloudflare_r2"]["status"] == "disabled"
        assert res["total_object_count"] == 0
