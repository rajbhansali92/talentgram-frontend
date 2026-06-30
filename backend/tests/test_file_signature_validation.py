import sys
from pathlib import Path
import os
import pytest
from unittest.mock import MagicMock, patch

# Setup environment variables needed by core.py
os.environ["MONGO_URL"] = "mongodb://localhost:27017"
os.environ["DB_NAME"] = "test"
os.environ["JWT_SECRET"] = "dummy"
os.environ["ADMIN_EMAIL"] = "admin@talentgram.co"
os.environ["ADMIN_PASSWORD"] = "password"
os.environ["CLOUDINARY_CLOUD_NAME"] = "dummy"
os.environ["CLOUDINARY_API_KEY"] = "dummy"
os.environ["CLOUDINARY_API_SECRET"] = "dummy"

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import cloudinary_upload
from fastapi import HTTPException

# We patch cloudinary.uploader.upload so we don't hit the actual API
@patch("cloudinary.uploader.upload")
def test_file_signature_validation(mock_upload):
    # Mock return value from cloudinary upload
    mock_upload.return_value = {
        "secure_url": "https://res.cloudinary.com/dummy/image/upload/v1/test",
        "public_id": "test",
        "resource_type": "image",
        "format": "heic",
        "bytes": 1000
    }

    # Helper function to run cloudinary_upload and return result or exception
    def run_upload(data: bytes, content_type: str):
        try:
            return cloudinary_upload(
                data=data,
                folder="talentgram/submissions/test",
                public_id="test-media",
                resource_type="auto",
                content_type=content_type
            )
        except HTTPException as e:
            return e

    # 1. HEIC file accepted (ftypheic)
    heic_data = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"
    res = run_upload(heic_data, "image/heic")
    assert not isinstance(res, HTTPException), f"HEIC upload failed: {res}"
    assert res["resource_type"] == "image"

    # 2. HEIF file accepted (ftypheif)
    heif_data = b"\x00\x00\x00\x18ftypheif\x00\x00\x00\x00"
    res = run_upload(heif_data, "image/heif")
    assert not isinstance(res, HTTPException), f"HEIF upload failed: {res}"

    # 3. MIF1 file accepted (ftypmif1 -> image/heic)
    mif1_data = b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00"
    res = run_upload(mif1_data, "image/heic")
    assert not isinstance(res, HTTPException), f"MIF1 upload failed: {res}"

    # 4. MP4 still accepted
    mp4_data = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"
    mock_upload.return_value["resource_type"] = "video"
    res = run_upload(mp4_data, "video/mp4")
    assert not isinstance(res, HTTPException), f"MP4 upload failed: {res}"

    # 5. MOV still accepted
    mov_data = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00"
    res = run_upload(mov_data, "video/quicktime")
    assert not isinstance(res, HTTPException), f"MOV upload failed: {res}"

    # 6. JPEG still accepted
    jpeg_data = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    mock_upload.return_value["resource_type"] = "image"
    res = run_upload(jpeg_data, "image/jpeg")
    assert not isinstance(res, HTTPException), f"JPEG upload failed: {res}"

    # 7. Malicious file spoofing rejected (signature says image/jpeg but header says video/mp4)
    res = run_upload(jpeg_data, "video/mp4")
    assert isinstance(res, HTTPException)
    assert res.status_code == 400
    assert "MIME type header does not match detected file signature" in res.detail

    # 8. Unallowed signature rejected
    bad_data = b"malicious content here"
    res = run_upload(bad_data, "image/jpeg")
    assert isinstance(res, HTTPException)
    assert res.status_code == 400
    assert "Invalid file signature" in res.detail

    # 9. WebM signature accepted
    webm_data = b"\x1a\x45\xdf\xa3\x01\x02\x03"
    mock_upload.return_value["resource_type"] = "video"
    res = run_upload(webm_data, "video/webm")
    assert not isinstance(res, HTTPException), f"WebM upload failed: {res}"

    # 10. Ogg signature accepted
    ogg_data = b"OggS\x01\x02\x03"
    res = run_upload(ogg_data, "audio/ogg")
    assert not isinstance(res, HTTPException), f"Ogg upload failed: {res}"

