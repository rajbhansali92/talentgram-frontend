"""
Phase 3 v37j — UI-only fix regression test.

This iteration is strictly UI-side, but the backend MUST round-trip the new
visibility keys (indian_images, western_images) through link.visibility
unchanged. We also re-verify the canonical fixture data and admin regression
endpoints required by the review request.

Fixtures verified live against the public preview backend:
  - link slug:   talentgram-x-comfort-9339a4
  - project:     Comfort  (id=cd3d9ac1-9a70-4c24-9af6-9f2beb163b22)
  - shivani:     submission_id=133b702c-06b0-4afa-887d-3c3293ffe0a4
                 -> media cats: image=4, indian=3, western=1, intro_video=1
  - angela:      submission_id=b50884fd-af45-4f8d-9520-6d8b602a754f (empty media)
"""

import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://casting-deck-pro.preview.emergentagent.com").rstrip("/")
LINK_SLUG = "talentgram-x-comfort-9339a4"
LINK_ID = "e12e0864-d11f-4943-af11-ad3d7ef29e0b"
SHIVANI_SUB_ID = "133b702c-06b0-4afa-887d-3c3293ffe0a4"
ANGELA_SUB_ID = "b50884fd-af45-4f8d-9520-6d8b602a754f"
COMFORT_PID = "cd3d9ac1-9a70-4c24-9af6-9f2beb163b22"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "admin@talentgram.com", "password": "Admin@123"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def viewer_token():
    r = requests.post(
        f"{BASE_URL}/api/public/links/{LINK_SLUG}/identify",
        json={"name": "Phase3 v37j Tester", "email": "test_v37j@example.com"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    tok = r.json().get("token")
    assert tok
    return tok


# ---------- admin regression ----------

class TestAdminRegression:
    """Phase 3 v37j requires admin endpoints all 200."""

    def test_login_admin(self, admin_token):
        assert isinstance(admin_token, str) and len(admin_token) > 20

    def test_talents_list(self, admin_headers):
        r = requests.get(f"{BASE_URL}/api/talents", headers=admin_headers, timeout=15)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_projects_list(self, admin_headers):
        r = requests.get(f"{BASE_URL}/api/projects", headers=admin_headers, timeout=15)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_links_list(self, admin_headers):
        r = requests.get(f"{BASE_URL}/api/links", headers=admin_headers, timeout=15)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_applications_list(self, admin_headers):
        r = requests.get(f"{BASE_URL}/api/applications", headers=admin_headers, timeout=15)
        assert r.status_code == 200

    def test_submissions_approved(self, admin_headers):
        r = requests.get(f"{BASE_URL}/api/submissions/approved", headers=admin_headers, timeout=15)
        assert r.status_code == 200


# ---------- canonical fixture data ----------

class TestCanonicalFixture:
    """Confirm Shivani / Angela media counts match expectations."""

    def test_comfort_project_subs(self, admin_headers):
        r = requests.get(
            f"{BASE_URL}/api/projects/{COMFORT_PID}/submissions",
            headers=admin_headers,
            timeout=15,
        )
        assert r.status_code == 200
        subs = r.json()
        sids = {s["id"]: s for s in subs}
        assert SHIVANI_SUB_ID in sids
        assert ANGELA_SUB_ID in sids

        shiv = sids[SHIVANI_SUB_ID]
        cats = {}
        for m in shiv.get("media") or []:
            cats[m.get("category")] = cats.get(m.get("category"), 0) + 1
        assert cats.get("image") == 4, f"expected 4 portfolio (category=image), got {cats}"
        assert cats.get("indian") == 3, f"expected 3 indian, got {cats}"
        assert cats.get("western") == 1, f"expected 1 western, got {cats}"

        ang = sids[ANGELA_SUB_ID]
        ang_cats = {}
        for m in ang.get("media") or []:
            ang_cats[m.get("category")] = ang_cats.get(m.get("category"), 0) + 1
        assert ang_cats.get("indian", 0) == 0
        assert ang_cats.get("western", 0) == 0
        assert ang_cats.get("image", 0) == 0


# ---------- visibility round-trip ----------

class TestLinkVisibilityRoundTrip:
    """Backend must accept arbitrary visibility keys without filtering them."""

    def test_default_state_intact(self, admin_headers):
        r = requests.get(f"{BASE_URL}/api/links", headers=admin_headers, timeout=15)
        assert r.status_code == 200
        link = next(l for l in r.json() if l["slug"] == LINK_SLUG)
        assert link["id"] == LINK_ID

    def test_round_trip_indian_western_keys(self, admin_headers):
        # GET full current link so we can issue a complete PUT
        rg = requests.get(f"{BASE_URL}/api/links/{LINK_ID}", headers=admin_headers, timeout=15)
        assert rg.status_code == 200, rg.text
        link = rg.json()
        original_vis = dict(link.get("visibility") or {})

        # Build PUT body that mirrors LinkIn fields used by the UI.
        def _put_body(vis):
            return {
                "title": link.get("title") or "Talentgram x Comfort",
                "talent_ids": link.get("talent_ids") or [],
                "submission_ids": link.get("submission_ids") or [],
                "auto_pull": bool(link.get("auto_pull")),
                "auto_project_id": link.get("auto_project_id"),
                "talent_field_visibility": link.get("talent_field_visibility") or {},
                "visibility": vis,
                "client_budget_override": link.get("client_budget_override"),
                "expires_at": link.get("expires_at"),
                "password": None,
            }

        # PUT with new visibility keys (indian_images=False)
        new_vis = {**original_vis, "indian_images": False, "western_images": True, "portfolio": True}
        put = requests.put(
            f"{BASE_URL}/api/links/{LINK_ID}",
            headers=admin_headers,
            json=_put_body(new_vis),
            timeout=15,
        )
        assert put.status_code == 200, put.text

        r2 = requests.get(f"{BASE_URL}/api/links/{LINK_ID}", headers=admin_headers, timeout=15)
        v = (r2.json() or {}).get("visibility") or {}
        assert v.get("indian_images") is False, f"indian_images not persisted: {v}"
        assert v.get("western_images") is True
        assert v.get("portfolio") is True

        # Restore canonical (all true) so future iterations see clean fixture
        restore_vis = {**original_vis, "indian_images": True, "western_images": True, "portfolio": True}
        rr = requests.put(
            f"{BASE_URL}/api/links/{LINK_ID}",
            headers=admin_headers,
            json=_put_body(restore_vis),
            timeout=15,
        )
        assert rr.status_code == 200, rr.text

        r3 = requests.get(f"{BASE_URL}/api/links/{LINK_ID}", headers=admin_headers, timeout=15)
        v3 = (r3.json() or {}).get("visibility") or {}
        assert v3.get("indian_images") is True
        assert v3.get("western_images") is True
        assert v3.get("portfolio") is True

        # Confirm restored
        r3 = requests.get(f"{BASE_URL}/api/links", headers=admin_headers, timeout=15)
        link3 = next(l for l in r3.json() if l["slug"] == LINK_SLUG)
        v3 = link3.get("visibility") or {}
        assert v3.get("indian_images") is True
        assert v3.get("western_images") is True
        assert v3.get("portfolio") is True


# ---------- public link payload (driving ClientView gating) ----------

class TestPublicLinkPayload:
    """Make sure ClientView receives 8 portfolio-cat images on Shivani by default."""

    def test_public_payload_shivani_8_images(self, viewer_token):
        r = requests.get(
            f"{BASE_URL}/api/public/links/{LINK_SLUG}",
            headers={"Authorization": f"Bearer {viewer_token}"},
            timeout=15,
        )
        assert r.status_code == 200
        d = r.json()
        talents = d.get("talents") or []
        shivs = [t for t in talents if t.get("id") == SHIVANI_SUB_ID or "shivani" in (t.get("name") or "").lower()]
        assert shivs, "Shivani not found in public link payload"
        media = shivs[0].get("media") or []
        cats = {}
        for m in media:
            cats[m.get("category")] = cats.get(m.get("category"), 0) + 1
        # Public payload uses normalised cats: portfolio/indian/western/video/take
        assert cats.get("portfolio") == 4
        assert cats.get("indian") == 3
        assert cats.get("western") == 1
        portfolio_total = (
            cats.get("portfolio", 0) + cats.get("indian", 0) + cats.get("western", 0)
        )
        assert portfolio_total == 8
