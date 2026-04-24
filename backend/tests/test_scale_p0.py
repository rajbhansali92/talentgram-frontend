"""P0 Scalability contract tests.

Locks in the invariants added in the scale push:
  - Pagination is BACKWARD COMPATIBLE — raw array when `?page` absent, envelope when present
  - Submission uploads reject payloads larger than MAX_SUBMISSION_VIDEO_BYTES
  - Image uploads generate a 1600px JPEG `resized_storage_path`

Tests use the live HTTP server via `requests` (same pattern as the other
`*_live.py` suites).
"""
import io
import os
import time
import uuid

import pytest
import requests
from PIL import Image

BASE = os.environ.get("TEST_API", "http://localhost:8001/api")
ADMIN_EMAIL = "admin@talentgram.com"
ADMIN_PASS = "Admin@123"


def _admin_token():
    r = requests.post(f"{BASE}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_h():
    return {"Authorization": f"Bearer {_admin_token()}"}


# ----------------------------------------------------------------------------
# Pagination back-compat
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/talents", "/projects", "/links", "/applications", "/submissions/approved"])
def test_pagination_backward_compatible(admin_h, path):
    # No query → raw list
    r = requests.get(f"{BASE}{path}", headers=admin_h, timeout=10)
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text}"
    assert isinstance(r.json(), list), f"{path} should return a raw list when ?page is absent"

    # With page/size → envelope
    r = requests.get(f"{BASE}{path}?page=0&size=5", headers=admin_h, timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    assert set(body.keys()) >= {"items", "total", "page", "size", "has_more"}
    assert isinstance(body["items"], list)
    assert body["page"] == 0
    assert body["size"] == 5


# ----------------------------------------------------------------------------
# Submission upload size cap + image resize
# ----------------------------------------------------------------------------
def _open_project(admin_h):
    """Create a fresh project and a submission, return (project, sid, sub_token)."""
    brand = f"ScaleP0-{uuid.uuid4().hex[:6]}"
    r = requests.post(
        f"{BASE}/projects",
        headers=admin_h,
        json={"brand_name": brand, "character": "test"},
        timeout=10,
    )
    r.raise_for_status()
    project = r.json()

    r = requests.post(
        f"{BASE}/public/projects/{project['slug']}/submission",
        json={
            "name": "Test Candidate",
            "email": f"scale-{uuid.uuid4().hex[:6]}@example.com",
            "phone": "+910000000000",
            "form_data": {"first_name": "Test", "last_name": "Candidate"},
        },
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    return project, body["id"], body["token"]


def test_image_upload_generates_resized_variant(admin_h):
    project, sid, tok = _open_project(admin_h)
    # Generate a 2400px test PNG so the resize pipeline actually kicks in
    img = Image.new("RGB", (2400, 3200), (120, 80, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    r = requests.post(
        f"{BASE}/public/submissions/{sid}/upload",
        headers={"Authorization": f"Bearer {tok}"},
        files={"file": ("headshot.png", buf.getvalue(), "image/png")},
        data={"category": "image"},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    sub = r.json()
    uploaded = [m for m in sub["media"] if m["category"] == "image"]
    assert uploaded, "expected at least one image in media"
    m = uploaded[-1]
    assert m.get("resized_storage_path"), "portfolio images should carry a 1600px JPEG copy"
    assert m["resized_storage_path"] != m["storage_path"]


def test_submission_upload_rejects_oversized_video(admin_h):
    """Oversize video upload must 400 with the size-limit message."""
    project, sid, tok = _open_project(admin_h)
    # 151 MB of zeros — bigger than the 150 MB cap, smaller than any safety net
    oversize = b"\x00" * (151 * 1024 * 1024)
    r = requests.post(
        f"{BASE}/public/submissions/{sid}/upload",
        headers={"Authorization": f"Bearer {tok}"},
        files={"file": ("big.mp4", oversize, "video/mp4")},
        data={"category": "intro_video"},
        timeout=180,
    )
    assert r.status_code == 400, r.text
    assert "too large" in r.json().get("detail", "").lower()
