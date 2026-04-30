"""Phase 3 — Per-category image cap (10 each) + indian/western mapping in client view.

Covers:
 - Submission upload: image / indian / western — independent 10-cap each
 - Application upload: image / indian / western — independent 10-cap each
 - ClientView shape: indian/western preserved on the public link payload
 - work_links: form_data.work_links surfaced via talent.work_links
 - Admin login + listing endpoints regression
"""

import io
import os
import uuid

import pytest
import requests

def _load_base_url() -> str:
    if os.environ.get("REACT_APP_BACKEND_URL"):
        return os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
    # Fall back to frontend/.env so the test runs in CI without env injection.
    env_path = "/app/frontend/.env"
    if os.path.exists(env_path):
        with open(env_path) as fh:
            for line in fh:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    return line.split("=", 1)[1].strip().rstrip("/")
    raise RuntimeError("REACT_APP_BACKEND_URL not set")


BASE_URL = _load_base_url()
ADMIN_EMAIL = "admin@talentgram.com"
ADMIN_PASSWORD = "Admin@123"


# Tiny but valid JPEG (~1 KB stays well below MAX_SUBMISSION_IMAGE_BYTES).
_JPEG_HEAD = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b0800010001010111003fffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda000c03010002110311003f00fbfc"
)
_JPEG_TAIL = b"\xff\xd9"
TINY_JPEG = _JPEG_HEAD + b"\x00" * 200 + _JPEG_TAIL


@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=15,
    )
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="session")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="session")
def project_slug(admin_headers):
    r = requests.get(f"{BASE_URL}/api/projects", headers=admin_headers, timeout=15)
    assert r.status_code == 200
    projects = r.json()
    assert projects, "No projects available for testing"
    return projects[0]["slug"]


def _start_submission(slug, email, name="TEST Phase3"):
    r = requests.post(
        f"{BASE_URL}/api/public/projects/{slug}/submission",
        json={"name": name, "email": email, "phone": "9999999999",
              "form_data": {"first_name": "TEST", "last_name": "Phase3"}},
        timeout=15,
    )
    assert r.status_code == 200, f"start failed {r.status_code} {r.text}"
    return r.json()["id"], r.json()["token"]


def _upload_image(sid, token, category, idx=0, route="submissions"):
    files = {"file": (f"img_{idx}.jpg", TINY_JPEG, "image/jpeg")}
    data = {"category": category}
    if route == "submissions":
        url = f"{BASE_URL}/api/public/submissions/{sid}/upload"
    else:
        url = f"{BASE_URL}/api/public/apply/{sid}/upload"
    return requests.post(
        url, headers={"Authorization": f"Bearer {token}"}, data=data, files=files, timeout=20
    )


# ------------------------------------------------------------------------
# Submission per-category cap tests (10 each, independent)
# ------------------------------------------------------------------------
class TestSubmissionPerCategoryCap:
    def test_indian_cap_10_then_400(self, project_slug):
        email = f"TEST_phase3_indian_{uuid.uuid4().hex[:8]}@example.com"
        sid, tok = _start_submission(project_slug, email)
        # 10 successful indian uploads
        for i in range(10):
            r = _upload_image(sid, tok, "indian", idx=i)
            assert r.status_code == 200, f"indian #{i+1} failed: {r.status_code} {r.text}"
        # 11th must 400 with the precise message
        r = _upload_image(sid, tok, "indian", idx=11)
        assert r.status_code == 400
        assert "Indian look image limit reached (10)" in r.text

    def test_western_cap_10_then_400(self, project_slug):
        email = f"TEST_phase3_western_{uuid.uuid4().hex[:8]}@example.com"
        sid, tok = _start_submission(project_slug, email)
        for i in range(10):
            r = _upload_image(sid, tok, "western", idx=i)
            assert r.status_code == 200, f"western #{i+1} failed: {r.status_code} {r.text}"
        r = _upload_image(sid, tok, "western", idx=11)
        assert r.status_code == 400
        assert "Western look image limit reached (10)" in r.text

    def test_image_independent_of_indian_western(self, project_slug):
        """A submission can carry 10 image + 10 indian + 10 western = 30 portfolio images."""
        email = f"TEST_phase3_combo_{uuid.uuid4().hex[:8]}@example.com"
        sid, tok = _start_submission(project_slug, email)
        for cat in ("image", "indian", "western"):
            for i in range(10):
                r = _upload_image(sid, tok, cat, idx=i)
                assert r.status_code == 200, f"{cat} #{i+1} failed: {r.status_code} {r.text}"
        # Each category 11th → 400
        for cat, label in (("image", "Portfolio"), ("indian", "Indian look"), ("western", "Western look")):
            r = _upload_image(sid, tok, cat, idx=99)
            assert r.status_code == 400, f"{cat} 11th expected 400, got {r.status_code}"
            assert f"{label} image limit reached (10)" in r.text


# ------------------------------------------------------------------------
# Application per-category cap tests
# ------------------------------------------------------------------------
class TestApplicationPerCategoryCap:
    def _start_app(self, email):
        r = requests.post(
            f"{BASE_URL}/api/public/apply",
            json={"first_name": "TEST", "last_name": "Phase3",
                  "email": email, "phone": "9999999999"},
            timeout=15,
        )
        assert r.status_code == 200, f"start_app failed {r.status_code} {r.text}"
        return r.json()["id"], r.json()["token"]

    def test_application_indian_cap(self):
        email = f"TEST_phase3_app_indian_{uuid.uuid4().hex[:8]}@example.com"
        aid, tok = self._start_app(email)
        for i in range(10):
            r = _upload_image(aid, tok, "indian", idx=i, route="applications")
            assert r.status_code == 200, f"app indian #{i+1} failed: {r.status_code} {r.text}"
        r = _upload_image(aid, tok, "indian", idx=11, route="applications")
        assert r.status_code == 400
        assert "Indian look image limit reached (10)" in r.text

    def test_application_western_cap(self):
        email = f"TEST_phase3_app_western_{uuid.uuid4().hex[:8]}@example.com"
        aid, tok = self._start_app(email)
        for i in range(10):
            r = _upload_image(aid, tok, "western", idx=i, route="applications")
            assert r.status_code == 200, f"app western #{i+1} failed: {r.status_code} {r.text}"
        r = _upload_image(aid, tok, "western", idx=11, route="applications")
        assert r.status_code == 400
        assert "Western look image limit reached (10)" in r.text

    def test_application_image_independent(self):
        email = f"TEST_phase3_app_combo_{uuid.uuid4().hex[:8]}@example.com"
        aid, tok = self._start_app(email)
        for cat in ("image", "indian", "western"):
            for i in range(10):
                r = _upload_image(aid, tok, cat, idx=i, route="applications")
                assert r.status_code == 200, f"{cat} #{i+1} failed: {r.status_code} {r.text}"


# ------------------------------------------------------------------------
# Client-view shape: indian/western preserved
# ------------------------------------------------------------------------
class TestClientViewIndianWesternMapping:
    def test_existing_link_surfaces_indian_western(self, admin_headers):
        # Find any link that contains a submission with indian/western media.
        # We use the existing seeded link `talentgram-x-comfort-9339a4`.
        slug = "talentgram-x-comfort-9339a4"
        # Identify viewer
        r = requests.post(
            f"{BASE_URL}/api/public/links/{slug}/identify",
            json={"name": "TEST Viewer", "email": f"TEST_viewer_{uuid.uuid4().hex[:8]}@example.com"},
            timeout=15,
        )
        assert r.status_code == 200, f"identify failed: {r.status_code} {r.text}"
        viewer_token = r.json()["token"]

        r = requests.get(
            f"{BASE_URL}/api/public/links/{slug}",
            headers={"Authorization": f"Bearer {viewer_token}"},
            timeout=15,
        )
        assert r.status_code == 200, f"public link fetch failed: {r.status_code} {r.text}"
        payload = r.json()
        talents = payload.get("talents", [])
        assert talents, "no talents returned"

        # Look for at least one talent whose media contains indian or western
        found_indian = False
        found_western = False
        for t in talents:
            for m in t.get("media", []) or []:
                if m.get("category") == "indian":
                    found_indian = True
                if m.get("category") == "western":
                    found_western = True
        assert found_indian, "ClientView payload dropped indian-category media"
        assert found_western, "ClientView payload dropped western-category media"


# ------------------------------------------------------------------------
# work_links surfacing
# ------------------------------------------------------------------------
class TestWorkLinksSurfacing:
    def test_submission_with_work_links_round_trips_via_link(self, project_slug, admin_headers):
        # Create a fresh submission with work_links in form_data, finalize it, approve, attach to a link.
        email = f"TEST_phase3_worklinks_{uuid.uuid4().hex[:8]}@example.com"
        sid, tok = _start_submission(project_slug, email)

        # Fill required form fields + work_links
        form_data = {
            "first_name": "TEST",
            "last_name": "WorkLinks",
            "height": "5'8\"",
            "location": "Mumbai",
            "availability": "Available",
            "budget": {"status": "accept", "value": ""},
            "work_links": ["https://example.com/reel1", "https://example.com/reel2"],
        }
        r = requests.put(
            f"{BASE_URL}/api/public/submissions/{sid}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"form_data": form_data},
            timeout=15,
        )
        assert r.status_code == 200, f"sub update failed: {r.status_code} {r.text}"

        # Add 1 portfolio image so the submission is plausible (media-optional but harmless)
        r = _upload_image(sid, tok, "image", idx=0)
        assert r.status_code == 200

        # Finalize
        r = requests.post(
            f"{BASE_URL}/api/public/submissions/{sid}/finalize",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=15,
        )
        assert r.status_code == 200, f"finalize failed: {r.status_code} {r.text}"

        # Approve via admin decision
        # Need project_id
        proj_r = requests.get(f"{BASE_URL}/api/projects", headers=admin_headers, timeout=15)
        pid = next(p["id"] for p in proj_r.json() if p["slug"] == project_slug)
        r = requests.post(
            f"{BASE_URL}/api/projects/{pid}/submissions/{sid}/decision",
            headers=admin_headers,
            json={"decision": "approved"},
            timeout=15,
        )
        assert r.status_code == 200, f"decision failed: {r.status_code} {r.text}"

        # Create a link bundling this submission. NOTE: backend slugifies title,
        # request `slug` field is ignored. Use response slug for fetch.
        link_title = f"TEST Phase3 work_links {uuid.uuid4().hex[:6]}"
        r = requests.post(
            f"{BASE_URL}/api/links",
            headers=admin_headers,
            json={
                "title": link_title,
                "talent_ids": [],
                "submission_ids": [sid],
                "visibility": {"work_links": True},
            },
            timeout=15,
        )
        assert r.status_code == 200, f"link create failed: {r.status_code} {r.text}"
        slug = r.json()["slug"]

        # Identify and fetch
        r = requests.post(
            f"{BASE_URL}/api/public/links/{slug}/identify",
            json={"name": "TEST WL Viewer", "email": f"TEST_wl_{uuid.uuid4().hex[:6]}@example.com"},
            timeout=15,
        )
        assert r.status_code == 200
        vt = r.json()["token"]
        r = requests.get(
            f"{BASE_URL}/api/public/links/{slug}",
            headers={"Authorization": f"Bearer {vt}"},
            timeout=15,
        )
        assert r.status_code == 200
        talents = r.json().get("talents", [])
        assert talents, "no talents returned for new link"
        # Find our talent and assert work_links present
        target = next((t for t in talents if t.get("submission_id") == sid), talents[0])
        wl = target.get("work_links") or []
        assert "https://example.com/reel1" in wl
        assert "https://example.com/reel2" in wl


# ------------------------------------------------------------------------
# Regression: admin endpoints
# ------------------------------------------------------------------------
class TestAdminRegression:
    def test_admin_login(self):
        r = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            timeout=15,
        )
        assert r.status_code == 200
        assert "token" in r.json()

    @pytest.mark.parametrize("path", [
        "/api/talents",
        "/api/projects",
        "/api/links",
        "/api/applications",
        "/api/submissions/approved",
    ])
    def test_admin_listing_endpoints(self, admin_headers, path):
        r = requests.get(f"{BASE_URL}{path}", headers=admin_headers, timeout=15)
        assert r.status_code == 200, f"{path} → {r.status_code}: {r.text[:200]}"
