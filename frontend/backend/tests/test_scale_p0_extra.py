"""Additional P0 scale coverage beyond test_scale_p0.py:
  - project-scoped submissions pagination back-compat
  - submission image oversize → 400
  - applications upload oversize video → 400
  - applications image upload gains resized_storage_path
"""
import io
import os
import uuid

import pytest
import requests
from PIL import Image

BASE = os.environ.get("TEST_API", "http://localhost:8001/api")
ADMIN = {"email": "admin@talentgram.com", "password": "Admin@123"}


@pytest.fixture(scope="module")
def admin_h():
    r = requests.post(f"{BASE}/auth/login", json=ADMIN, timeout=10)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def project(admin_h):
    brand = f"ScaleP0x-{uuid.uuid4().hex[:6]}"
    r = requests.post(f"{BASE}/projects", headers=admin_h,
                      json={"brand_name": brand, "character": "test"}, timeout=10)
    r.raise_for_status()
    return r.json()


def _open_sub(project):
    r = requests.post(
        f"{BASE}/public/projects/{project['slug']}/submission",
        json={"name": "Test", "email": f"scalex-{uuid.uuid4().hex[:6]}@ex.com",
              "phone": "+910000000000",
              "form_data": {"first_name": "A", "last_name": "B"}},
        timeout=10,
    )
    r.raise_for_status()
    b = r.json()
    return b["id"], b["token"]


def test_project_submissions_pagination_backcompat(admin_h, project):
    pid = project["id"]
    r = requests.get(f"{BASE}/projects/{pid}/submissions", headers=admin_h, timeout=10)
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    r = requests.get(f"{BASE}/projects/{pid}/submissions?page=0&size=3",
                     headers=admin_h, timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    assert set(body.keys()) >= {"items", "total", "page", "size", "has_more"}
    assert body["page"] == 0 and body["size"] == 3


def test_submission_image_oversize_rejected(project):
    sid, tok = _open_sub(project)
    # 26 MB of bytes (cap is 25 MB per spec)
    blob = b"\xff" * (26 * 1024 * 1024)
    r = requests.post(
        f"{BASE}/public/submissions/{sid}/upload",
        headers={"Authorization": f"Bearer {tok}"},
        files={"file": ("big.jpg", blob, "image/jpeg")},
        data={"category": "image"},
        timeout=120,
    )
    assert r.status_code == 400, r.text
    assert "too large" in r.json().get("detail", "").lower()


def _open_app():
    r = requests.post(
        f"{BASE}/public/apply",
        json={"first_name": "T", "last_name": "X",
              "email": f"app-{uuid.uuid4().hex[:6]}@ex.com",
              "phone": "+911111111111"},
        timeout=10,
    )
    r.raise_for_status()
    b = r.json()
    return b["id"], b["token"]


def test_applications_upload_oversize_video_rejected():
    aid, tok = _open_app()

    blob = b"\x00" * (151 * 1024 * 1024)
    r = requests.post(
        f"{BASE}/public/apply/{aid}/upload",
        headers={"Authorization": f"Bearer {tok}"},
        files={"file": ("big.mp4", blob, "video/mp4")},
        data={"category": "intro_video"},
        timeout=180,
    )
    assert r.status_code == 400, r.text
    assert "too large" in r.json().get("detail", "").lower()


def test_applications_image_upload_has_resized_variant():
    aid, tok = _open_app()

    import random
    img = Image.new("RGB", (2400, 1800))
    px = img.load()
    for y in range(0, 1800, 4):
        for x in range(0, 2400, 4):
            px[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    r = requests.post(
        f"{BASE}/public/apply/{aid}/upload",
        headers={"Authorization": f"Bearer {tok}"},
        files={"file": ("pic.png", buf.getvalue(), "image/png")},
        data={"category": "image"},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    app = r.json()
    imgs = [m for m in app.get("media", []) if m.get("category") == "image"]
    assert imgs
    m = imgs[-1]
    assert m.get("resized_storage_path")
    assert m["resized_storage_path"] != m["storage_path"]
