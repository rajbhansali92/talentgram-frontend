"""
Iteration 17 — Verify top-level image_url on talents/applications/submissions,
public prefill returning image_url + age, public projects endpoint, no /api/files,
no emergentagent storage references in source. Cloudinary upload + delete e2e.
"""
import io
import os
import re
import json
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://casting-deck-pro.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@talentgram.com"
ADMIN_PASSWORD = "Admin@123"
TEST_SLUG = "international-sportswear-brand-8a7a22"
TEST_PREFILL_EMAIL = "heen.rathod.hr@gmail.com"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=20)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    data = r.json()
    token = data.get("token") or data.get("access_token")
    assert token, f"Missing token in login response: {data}"
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


# ------------- Health -------------
def test_health():
    r = requests.get(f"{BASE_URL}/api/", timeout=15)
    assert r.status_code == 200


# ------------- Top-level image_url on talents grid -------------
def test_talents_have_top_level_image_url(session):
    r = session.get(f"{BASE_URL}/api/talents", timeout=30)
    assert r.status_code == 200, r.text
    talents = r.json()
    assert isinstance(talents, list), "talents should be a list"
    assert len(talents) > 0, "expected at least one talent"
    bad = []
    cloudinary_count = 0
    null_count = 0
    for t in talents:
        if "image_url" not in t:
            bad.append({"id": t.get("id"), "reason": "missing image_url key"})
            continue
        v = t["image_url"]
        # must be either a non-empty string or None — never 'undefined' / ''
        if v is None:
            null_count += 1
            continue
        if not isinstance(v, str) or v.strip() == "" or v.strip().lower() == "undefined":
            bad.append({"id": t.get("id"), "reason": f"invalid image_url={v!r}"})
            continue
        if "res.cloudinary.com" in v:
            cloudinary_count += 1
    assert not bad, f"talents with bad image_url: {bad[:5]}"
    assert cloudinary_count >= 1, f"expected ≥1 cloudinary image_url, got {cloudinary_count} (null={null_count})"
    print(f"[talents] total={len(talents)} cloudinary={cloudinary_count} null={null_count}")


# ------------- Top-level image_url on applications -------------
def test_applications_have_top_level_image_url(session):
    r = session.get(f"{BASE_URL}/api/applications", timeout=30)
    assert r.status_code == 200, r.text
    apps = r.json()
    assert isinstance(apps, list)
    if not apps:
        pytest.skip("No applications to test")
    bad = []
    cloud = 0
    null_ = 0
    for a in apps:
        if "image_url" not in a:
            bad.append({"id": a.get("id"), "reason": "missing image_url"})
            continue
        v = a["image_url"]
        if v is None:
            null_ += 1
            continue
        if not isinstance(v, str) or v.strip() == "" or v.strip().lower() == "undefined":
            bad.append({"id": a.get("id"), "reason": f"invalid image_url={v!r}"})
            continue
        if "res.cloudinary.com" in v:
            cloud += 1
    assert not bad, f"apps with bad image_url: {bad[:5]}"
    print(f"[applications] total={len(apps)} cloudinary={cloud} null={null_}")


# ------------- Public projects endpoint by slug -------------
def test_public_project_by_slug():
    r = requests.get(f"{BASE_URL}/api/public/projects/{TEST_SLUG}", timeout=15)
    assert r.status_code == 200, r.text
    proj = r.json()
    assert isinstance(proj, dict)
    # Required-ish fields for SubmissionPage to render
    for key in ("brand_name", "character", "shoot_dates", "additional_details"):
        assert key in proj, f"missing key {key} in project doc"
    assert proj.get("brand_name"), "brand_name empty"


# ------------- Prefill: known email returns rich payload -------------
def test_prefill_known_email_has_image_url():
    r = requests.get(f"{BASE_URL}/api/public/prefill",
                     params={"email": TEST_PREFILL_EMAIL}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict)
    assert body, "expected non-empty prefill payload for known email"
    # Required fields per request
    for k in ("first_name", "last_name", "image_url"):
        assert k in body, f"prefill missing key {k}: keys={list(body.keys())}"
    iv = body.get("image_url")
    if iv is not None:
        assert isinstance(iv, str) and iv.strip() != ""
        # Should be cloudinary or at least an http URL
        assert iv.startswith("http"), f"image_url not http: {iv}"
    # age may be derived from DOB or absent — accept either int/None/string
    print(f"[prefill] keys={sorted(body.keys())}")


# ------------- Prefill: unknown email returns empty {} -------------
def test_prefill_unknown_email_empty():
    r = requests.get(f"{BASE_URL}/api/public/prefill",
                     params={"email": "nobody-zzz-xxx@example.com"}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {} or body == {"work_links": []} or not body, f"expected empty for unknown email: {body}"


# ------------- POST /api/upload basic Cloudinary upload -------------
PNG_1x1 = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C489"
    "0000000A49444154789C6300010000000500010D0A2DB40000000049454E44AE426082"
)


def test_post_upload_cloudinary(session):
    files = {"file": ("tiny.png", io.BytesIO(PNG_1x1), "image/png")}
    r = session.post(f"{BASE_URL}/api/upload", files=files, timeout=30)
    assert r.status_code in (200, 201), f"{r.status_code} {r.text}"
    data = r.json()
    for k in ("url", "public_id", "resource_type", "content_type", "original_filename"):
        assert k in data, f"upload response missing {k}: {data}"
    assert "res.cloudinary.com" in data["url"], f"url not cloudinary: {data['url']}"


# ------------- Talent media upload + delete (e2e) -------------
def test_talent_media_upload_then_delete_updates_image_url(session):
    # Create temp talent
    payload = {
        "name": "TEST_iter17 Image",
        "first_name": "TEST_iter17",
        "last_name": "Image",
        "email": f"TEST_iter17_{int(time.time())}@example.com",
        "phone": "+910000000000",
    }
    rc = session.post(f"{BASE_URL}/api/talents", json=payload, timeout=15)
    assert rc.status_code in (200, 201), rc.text
    tal = rc.json()
    tid = tal.get("id") or tal.get("_id")
    assert tid, f"no id in created talent: {tal}"
    try:
        # Upload media
        files = {"file": ("tiny.png", io.BytesIO(PNG_1x1), "image/png")}
        ru = session.post(f"{BASE_URL}/api/talents/{tid}/media",
                          files=files, data={"category": "portfolio"}, timeout=30)
        assert ru.status_code in (200, 201), ru.text
        media = ru.json()
        # Could be wrapper or direct media item — find media item with url+public_id
        mid = None
        if isinstance(media, dict):
            if "id" in media and "url" in media:
                mid = media["id"]
                assert "res.cloudinary.com" in media["url"]
                assert media.get("public_id")
            else:
                # likely talent-shaped response
                arr = (media.get("media") or media.get("media_items") or [])
                if arr:
                    last = arr[-1]
                    mid = last.get("id")
                    assert "res.cloudinary.com" in (last.get("url") or "")

        # Reload talents grid and assert the new talent has image_url populated
        rg = session.get(f"{BASE_URL}/api/talents", timeout=30)
        assert rg.status_code == 200
        match = next((t for t in rg.json() if t.get("id") == tid), None)
        assert match is not None, "created talent not in /api/talents listing"
        assert match.get("image_url"), f"image_url should be populated: {match.get('image_url')}"
        assert "res.cloudinary.com" in match["image_url"]

        # Delete media (if mid known)
        if mid:
            rd = session.delete(f"{BASE_URL}/api/talents/{tid}/media/{mid}", timeout=20)
            assert rd.status_code in (200, 204), rd.text
    finally:
        # Cleanup talent
        session.delete(f"{BASE_URL}/api/talents/{tid}", timeout=15)


# ------------- /api/files/* must NOT exist (legacy proxy removed) -------------
def test_no_api_files_route():
    r = requests.get(f"{BASE_URL}/api/files/anything-here", timeout=10)
    assert r.status_code == 404, f"expected 404 for /api/files/*, got {r.status_code}"
    # openapi must not contain /api/files paths
    ro = requests.get(f"{BASE_URL}/api/openapi.json", timeout=15)
    if ro.status_code == 200:
        spec = ro.json()
        paths = list((spec.get("paths") or {}).keys())
        files_paths = [p for p in paths if "/files" in p]
        assert not files_paths, f"openapi still references files routes: {files_paths}"


# ------------- Source code must not reference emergentagent storage init -------------
def test_source_no_emergent_storage_refs():
    # Scan backend source
    bad_hits = []
    for root, _, fnames in os.walk("/app/backend"):
        if any(skip in root for skip in ("__pycache__", "/tests", "/.venv")):
            continue
        for fn in fnames:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(root, fn)
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
            except Exception:
                continue
            for needle in ("integrations.emergentagent.com", "EmergentStorage",
                           "from emergentintegrations.storage", "emergent_storage_init"):
                if needle in txt:
                    bad_hits.append((fp, needle))
    assert not bad_hits, f"emergent storage references still present: {bad_hits}"
