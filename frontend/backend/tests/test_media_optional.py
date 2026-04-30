"""Phase 1 v37c — media optionality tests.

Covers:
- /submit finalize succeeds with ZERO media (form_data complete)
- /submit finalize 400s on missing form fields (height/location/availability)
- /apply finalize 400 if no image; succeeds with exactly 1 image; no video required
"""
import io
import os
import time
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://casting-deck-pro.preview.emergentagent.com").rstrip("/")


@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    return s


@pytest.fixture(scope="session")
def slug(api):
    """Find an existing project slug to test against."""
    # Login as admin to fetch projects
    r = api.post(f"{BASE_URL}/api/auth/login", json={
        "email": "admin@talentgram.com",
        "password": "Admin@123",
    })
    token = r.json().get("token")
    rp = api.get(f"{BASE_URL}/api/projects", headers={"Authorization": f"Bearer {token}"})
    items = rp.json() if isinstance(rp.json(), list) else rp.json().get("items", [])
    assert items, "No projects exist on backend; cannot test submission flow"
    return items[0]["slug"]


SLUG = None  # populated lazily via fixture


def _u(name):
    return f"TEST_{name}_{uuid.uuid4().hex[:8]}@example.com"


# ---------- /submit (audition) ----------
class TestSubmitFinalizeZeroMedia:
    def _start(self, api, email, slug):
        r = api.post(f"{BASE_URL}/api/public/projects/{slug}/submission", json={
            "name": "Test Talent",
            "email": email,
            "phone": "+919999999999",
        })
        assert r.status_code == 200, r.text
        return r.json()

    def _put_form(self, api, sid, token, form_data):
        r = api.put(
            f"{BASE_URL}/api/public/submissions/{sid}",
            headers={"Authorization": f"Bearer {token}"},
            json={"form_data": form_data},
        )
        assert r.status_code == 200, r.text
        return r.json()

    def test_finalize_zero_media_full_form_succeeds(self, api, slug):
        email = _u("submit_zero")
        started = self._start(api, email, slug)
        sid, token = started["id"], started["token"]
        self._put_form(api, sid, token, {
            "first_name": "Zoey",
            "last_name": "Zero",
            "height": "5'7\"",
            "location": "Mumbai",
            "availability": {"status": "yes", "note": ""},
            "budget": {"status": "accept", "value": ""},
        })
        r = api.post(
            f"{BASE_URL}/api/public/submissions/{sid}/finalize",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, f"Expected 200 with zero media, got {r.status_code}: {r.text}"
        data = r.json()
        assert data.get("ok") is True
        assert data.get("status") == "submitted"

    def test_finalize_missing_height_400(self, api, slug):
        email = _u("submit_noheight")
        started = self._start(api, email, slug)
        sid, token = started["id"], started["token"]
        self._put_form(api, sid, token, {
            "first_name": "No",
            "last_name": "Height",
            "location": "Delhi",
            "availability": {"status": "yes", "note": ""},
            "budget": {"status": "accept", "value": ""},
        })
        r = api.post(
            f"{BASE_URL}/api/public/submissions/{sid}/finalize",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400, r.text
        assert "height" in r.text.lower()

    def test_finalize_missing_location_400(self, api, slug):
        email = _u("submit_noloc")
        started = self._start(api, email, slug)
        sid, token = started["id"], started["token"]
        self._put_form(api, sid, token, {
            "first_name": "No",
            "last_name": "Loc",
            "height": "5'8\"",
            "availability": {"status": "yes", "note": ""},
            "budget": {"status": "accept", "value": ""},
        })
        r = api.post(
            f"{BASE_URL}/api/public/submissions/{sid}/finalize",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400, r.text
        assert "location" in r.text.lower()

    def test_finalize_missing_availability_400(self, api, slug):
        email = _u("submit_noavail")
        started = self._start(api, email, slug)
        sid, token = started["id"], started["token"]
        self._put_form(api, sid, token, {
            "first_name": "No",
            "last_name": "Avail",
            "height": "5'8\"",
            "location": "Pune",
            "budget": {"status": "accept", "value": ""},
        })
        r = api.post(
            f"{BASE_URL}/api/public/submissions/{sid}/finalize",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400, r.text
        assert "availab" in r.text.lower()


# ---------- /apply (open application) ----------
class TestApplyFinalizeOneImage:
    def _png_bytes(self):
        # Minimal valid PNG (1x1 pixel)
        return bytes.fromhex(
            "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4"
            "890000000D49444154789C6300010000000500010D0A2DB40000000049454E44AE426082"
        )

    def _start(self, api, email):
        r = api.post(f"{BASE_URL}/api/public/apply", json={
            "email": email,
            "first_name": "Apply",
            "last_name": "Tester",
            "phone": "+918888888888",
        })
        assert r.status_code == 200, r.text
        return r.json()

    def _put_form(self, api, aid, token, form_data):
        r = api.put(
            f"{BASE_URL}/api/public/apply/{aid}",
            headers={"Authorization": f"Bearer {token}"},
            json={"form_data": form_data},
        )
        assert r.status_code == 200, r.text
        return r.json()

    def test_finalize_no_media_400_image_required(self, api):
        email = _u("apply_nomedia")
        started = self._start(api, email)
        aid, token = started["id"], started["token"]
        self._put_form(api, aid, token, {
            "first_name": "Apply",
            "last_name": "Tester",
            "dob": "1995-05-05",
            "height": "5'7\"",
            "location": "Mumbai",
            "gender": "Female",
        })
        r = api.post(
            f"{BASE_URL}/api/public/apply/{aid}/finalize",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400, r.text
        assert "profile" in r.text.lower() or "headshot" in r.text.lower() or "image" in r.text.lower()

    def test_finalize_one_image_no_video_succeeds(self, api):
        email = _u("apply_oneimg")
        started = self._start(api, email)
        aid, token = started["id"], started["token"]
        self._put_form(api, aid, token, {
            "first_name": "Apply",
            "last_name": "Tester",
            "dob": "1995-05-05",
            "height": "5'7\"",
            "location": "Mumbai",
            "gender": "Female",
        })
        # upload exactly 1 image
        files = {"file": ("test.png", io.BytesIO(self._png_bytes()), "image/png")}
        data = {"category": "image"}
        ru = api.post(
            f"{BASE_URL}/api/public/apply/{aid}/upload",
            headers={"Authorization": f"Bearer {token}"},
            files=files, data=data,
        )
        assert ru.status_code == 200, ru.text
        # finalize without intro_video
        r = api.post(
            f"{BASE_URL}/api/public/apply/{aid}/finalize",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, f"Expected 200 with 1 image and no video, got {r.status_code}: {r.text}"
        assert r.json().get("ok") is True


# ---------- Admin regression ----------
class TestAdminRegression:
    def test_admin_login_and_lists(self, api):
        r = api.post(f"{BASE_URL}/api/auth/login", json={
            "email": "admin@talentgram.com",
            "password": "Admin@123",
        })
        assert r.status_code == 200, r.text
        token = r.json().get("token") or r.json().get("access_token")
        # Some auth schemes set httpOnly cookie; try Bearer if token exists
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        # projects
        rp = api.get(f"{BASE_URL}/api/projects", headers=headers)
        assert rp.status_code == 200, rp.text
        assert isinstance(rp.json(), (list, dict))

        # applications
        ra = api.get(f"{BASE_URL}/api/applications", headers=headers)
        assert ra.status_code == 200, ra.text

    def test_prefill_endpoint(self, api):
        r = api.get(f"{BASE_URL}/api/public/prefill", params={"email": "nonexistent_TEST@example.com"})
        assert r.status_code == 200
        assert r.json() == {} or isinstance(r.json(), dict)
