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
