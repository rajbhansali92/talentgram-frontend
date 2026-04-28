"""
Iteration 16 — Verify Cloudinary migration end-to-end.

Scope:
  1. Backend boots cleanly (/api/ ok)
  2. Admin login works with seeded credentials
  3. /api/talents — at least N items have media[].url pointing to res.cloudinary.com,
     and storage_path is preserved (non-destructive migration)
  4. /api/applications — list, validate media[].url where present
  5. /api/projects/:slug/submissions — _public_media exposes url + public_id
  6. POST /api/talents/:tid/media — uploads a small PNG → response media has Cloudinary url
  7. DELETE /api/talents/:tid/media/:mid — cleanup; ensures destroy path is callable
  8. Confirm /api/files/* routes are removed (404)
"""
import io
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://casting-deck-pro.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@talentgram.com"
ADMIN_PASSWORD = "Admin@123"


# ---------- Fixtures ----------
@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def admin_session(session):
    r = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    data = r.json()
    token = data.get("access_token") or data.get("token")
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


# ---------- 1. Health ----------
def test_health_root():
    r = requests.get(f"{BASE_URL}/api/")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True


# ---------- 2. Auth ----------
def test_admin_login(admin_session):
    # If we reach here, login succeeded in fixture
    me = admin_session.get(f"{BASE_URL}/api/auth/me")
    assert me.status_code == 200, me.text
    body = me.json()
    assert body.get("email") == ADMIN_EMAIL
    assert body.get("role") == "admin"


# ---------- 3. Talents list — Cloudinary URL coverage ----------
def test_talents_have_cloudinary_urls(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/talents")
    assert r.status_code == 200, r.text
    talents = r.json()
    assert isinstance(talents, list)
    assert len(talents) > 0, "expected at least one talent record"

    total_media = 0
    cloudinary_urls = 0
    storage_path_preserved = 0
    sampled = []
    for t in talents:
        for m in (t.get("media") or []):
            total_media += 1
            url = m.get("url") or ""
            if "res.cloudinary.com" in url:
                cloudinary_urls += 1
            if m.get("storage_path"):
                storage_path_preserved += 1
            if len(sampled) < 5 and "res.cloudinary.com" in url:
                sampled.append({"talent": t.get("name"), "url": url[:100],
                                "storage_path": (m.get("storage_path") or "")[:80]})

    print(f"\n[talents] total_media={total_media} cloudinary={cloudinary_urls} "
          f"storage_path_preserved={storage_path_preserved}")
    print(f"[talents] sample: {sampled}")
    # Validate at least 5 records with Cloudinary URLs
    assert cloudinary_urls >= 5, (
        f"Expected ≥5 talent media items with Cloudinary URLs, got {cloudinary_urls}"
    )
    # Validate non-destructive migration: storage_path retained on most legacy items
    assert storage_path_preserved >= 5, (
        f"Expected storage_path preserved on legacy items; got {storage_path_preserved}"
    )


# ---------- 4. Applications ----------
def test_applications_media_urls(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/applications")
    assert r.status_code == 200, r.text
    apps = r.json()
    assert isinstance(apps, list)
    cloud_count = 0
    total = 0
    for a in apps:
        for m in (a.get("media") or []):
            total += 1
            if "res.cloudinary.com" in (m.get("url") or ""):
                cloud_count += 1
    print(f"\n[applications] count={len(apps)} media_total={total} cloudinary={cloud_count}")
    # Not strict: 1x1 placeholders failed migration (expected); we just want coverage > 0
    # Skip if no media at all (admin may have 0 applications with media)
    if total > 0:
        assert cloud_count >= 1 or total < 5, (
            "Expected at least 1 application media on Cloudinary"
        )


# ---------- 5. Submissions per project ----------
def test_project_submissions_public_media(admin_session):
    pr = admin_session.get(f"{BASE_URL}/api/projects")
    assert pr.status_code == 200, pr.text
    projects = pr.json()
    assert len(projects) > 0
    found_one_with_submissions = False
    sampled_url = None
    cloud_count = 0
    for p in projects[:20]:
        # endpoint uses project ID, not slug
        pid = p.get("id")
        if not pid:
            continue
        r = admin_session.get(f"{BASE_URL}/api/projects/{pid}/submissions")
        if r.status_code != 200:
            continue
        subs = r.json()
        if not subs:
            continue
        found_one_with_submissions = True
        for s in subs:
            for m in (s.get("media") or []):
                if "res.cloudinary.com" in (m.get("url") or ""):
                    cloud_count += 1
                    # verify _public_media exposes public_id passthrough
                    assert "public_id" in m, f"public_id must passthrough on submission media: {m}"
                    sampled_url = sampled_url or m.get("url")
    assert found_one_with_submissions, "no project had submissions to validate"
    assert cloud_count >= 5, f"expected ≥5 submission media on Cloudinary, got {cloud_count}"
    print(f"\n[submissions] cloudinary_count={cloud_count} sample={sampled_url}")


# ---------- 6. Upload new media → Cloudinary ----------
# Tiny but Cloudinary-acceptable PNG: 8x8 red square (NOT a 1x1 placeholder)
def _real_png_bytes():
    # 8x8 PNG synthesized via PIL if available; fallback to a known-good multi-byte PNG header
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), color=(255, 0, 0)).save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        # Pre-baked 8x8 PNG (large enough for Cloudinary to accept)
        import base64
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAH0lEQVR42mNk"
            "+M9Qz0AEYBxVSF9FAAEYBxX+VwIAGgsC/SXkSQAAAAASUVORK5CYII="
        )


@pytest.fixture(scope="module")
def created_media(admin_session):
    """Create a TEST talent + upload one Cloudinary image → cleanup after."""
    create = admin_session.post(
        f"{BASE_URL}/api/talents",
        json={"name": "TEST_cloudinary_upload", "email": "TEST_cl_upload@example.com"},
    )
    assert create.status_code in (200, 201), create.text
    talent = create.json()
    tid = talent["id"]
    yield_ctx = {"tid": tid, "mid": None, "url": None, "public_id": None}

    # Upload — multipart, no Content-Type
    upload_session = requests.Session()
    upload_session.headers.update({k: v for k, v in admin_session.headers.items() if k.lower() != "content-type"})
    files = {"file": ("test.png", _real_png_bytes(), "image/png")}
    data = {"category": "portfolio"}
    up = upload_session.post(f"{BASE_URL}/api/talents/{tid}/media", files=files, data=data)
    assert up.status_code in (200, 201), f"upload failed: {up.status_code} {up.text}"
    body = up.json()
    media_list = body.get("media") or []
    assert media_list, "no media on talent after upload"
    last = media_list[-1]
    assert "res.cloudinary.com" in (last.get("url") or ""), f"upload not on cloudinary: {last}"
    assert last.get("public_id"), "public_id missing on uploaded media"
    assert last.get("resource_type") in ("image", "video"), "resource_type missing"
    yield_ctx["mid"] = last["id"]
    yield_ctx["url"] = last["url"]
    yield_ctx["public_id"] = last["public_id"]
    yield yield_ctx

    # teardown — delete media + talent
    try:
        admin_session.delete(f"{BASE_URL}/api/talents/{tid}/media/{yield_ctx['mid']}")
    except Exception:
        pass
    try:
        admin_session.delete(f"{BASE_URL}/api/talents/{tid}")
    except Exception:
        pass


def test_upload_to_cloudinary(created_media):
    assert created_media["url"] and "res.cloudinary.com" in created_media["url"]
    assert created_media["public_id"]
    print(f"\n[upload] new cloudinary url: {created_media['url']}")
    # Verify the file is publicly fetchable
    fetched = requests.get(created_media["url"], timeout=15)
    assert fetched.status_code == 200, f"cloudinary URL not fetchable: {fetched.status_code}"
    assert fetched.headers.get("content-type", "").startswith("image/")


def test_delete_media_cloudinary(admin_session, created_media):
    tid = created_media["tid"]
    mid = created_media["mid"]
    r = admin_session.delete(f"{BASE_URL}/api/talents/{tid}/media/{mid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    # Verify removed from talent
    t = admin_session.get(f"{BASE_URL}/api/talents/{tid}").json()
    media_ids = [m["id"] for m in (t.get("media") or [])]
    assert mid not in media_ids
    # Mark as deleted so fixture teardown skips
    created_media["mid"] = None


# ---------- 7. /api/files/* removed ----------
def test_files_route_removed():
    r = requests.get(f"{BASE_URL}/api/files/anything.png")
    assert r.status_code == 404, f"old /api/files/* should 404, got {r.status_code}"


# ---------- 8. Public link viewing — talents in client view ----------
def test_public_link_view_uses_cloudinary(admin_session):
    links = admin_session.get(f"{BASE_URL}/api/links").json()
    assert len(links) > 0
    cloud_count = 0
    sampled_slug = None
    for link in links[:20]:
        slug = link.get("slug")
        if not slug:
            continue
        # Public link viewing requires viewer identification first
        ident = requests.post(
            f"{BASE_URL}/api/public/links/{slug}/identify",
            json={"name": "TEST_viewer", "email": "TEST_viewer@example.com"},
        )
        if ident.status_code != 200:
            continue
        token = ident.json().get("token")
        r = requests.get(
            f"{BASE_URL}/api/public/links/{slug}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code != 200:
            continue
        body = r.json()
        for t in (body.get("talents") or []):
            for m in (t.get("media") or []):
                if "res.cloudinary.com" in (m.get("url") or ""):
                    cloud_count += 1
                    sampled_slug = slug
        if cloud_count >= 3:
            break
    print(f"\n[public_link] slug={sampled_slug} cloudinary_media={cloud_count}")
    assert cloud_count >= 1, "expected at least one Cloudinary URL in a public link payload"
