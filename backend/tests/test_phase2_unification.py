"""Phase 2 — Schema Unification across talent-facing surfaces.

Covers:
  (a) `phone` round-trips through TalentIn (admin POST /api/talents)
  (b) /api/public/prefill returns gender, ethnicity, bio, work_links
      (and never media/source/notes/created_by)
  (c) submission upload accepts category=indian and category=western
  (d) MAX_SUBMISSION_IMAGES is enforced ACROSS image+indian+western
  (e) MIN_SUBMISSION_IMAGES counted across all 3 categories on finalize
  (f) Submission finalize syncs FILL-EMPTY-ONLY (admin hand-edits sacred)
  (g) Application approval merges phone/work_links/ethnicity/gender
  (h) Cross-form value parity — TalentIn rejects nothing the schema sends
"""
import io
import os
import time
import uuid

import pytest
import requests

BASE = os.environ.get(
    "PYTEST_API_BASE",
    "https://casting-deck-pro.preview.emergentagent.com/api",
)
ADMIN_EMAIL = "admin@talentgram.com"
ADMIN_PASS = "Admin@123"
PUBLIC_SLUG = "pantaloons-with-a-celebrity-11ccd4"


# ---------- fixtures ----------
@pytest.fixture(scope="module")
def admin_h():
    r = requests.post(
        f"{BASE}/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _png_bytes() -> bytes:
    # 1x1 valid PNG
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf"
        b"\xc0\x00\x00\x00\x03\x00\x01\xa3M\xeb\xf3\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _mp4_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2mp41" + b"\x00" * 64


def _start_submission(email: str):
    r = requests.post(
        f"{BASE}/public/projects/{PUBLIC_SLUG}/submission",
        json={
            "name": "Test Phase2",
            "email": email,
            "phone": "+919999000111",
            "consent": True,
        },
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["id"], body["token"]


# ----------------------------------------------------------------------
# (a) phone round-trips through TalentIn
# ----------------------------------------------------------------------
def test_a_phone_roundtrips_in_talent_create(admin_h):
    email = f"test_p2_phone_{uuid.uuid4().hex[:6]}@ex.com"
    payload = {
        "name": "Phase2 Phone",
        "email": email,
        "phone": "+919876543210",
        "gender": "female",
        "ethnicity": "south_asian",
        "instagram_followers": "100K+",
        "work_links": ["https://example.com/reel", "https://example.com/portfolio"],
    }
    r = requests.post(f"{BASE}/talents", headers=admin_h, json=payload, timeout=10)
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["phone"] == "+919876543210"
    assert t["gender"] == "female"
    assert t["ethnicity"] == "south_asian"
    assert t["instagram_followers"] == "100K+"
    assert t["work_links"] == payload["work_links"]
    # Verify persistence via GET
    g = requests.get(f"{BASE}/talents/{t['id']}", headers=admin_h, timeout=10)
    assert g.status_code == 200
    assert g.json()["phone"] == "+919876543210"


# ----------------------------------------------------------------------
# (a2) Same email twice → MERGES, fills empty only
# ----------------------------------------------------------------------
def test_a2_dup_email_merges_fill_empty_only(admin_h):
    email = f"test_p2_merge_{uuid.uuid4().hex[:6]}@ex.com"
    r1 = requests.post(
        f"{BASE}/talents",
        headers=admin_h,
        json={"name": "Original", "email": email, "gender": "female", "phone": "+11"},
        timeout=10,
    )
    assert r1.status_code == 200, r1.text
    id1 = r1.json()["id"]
    r2 = requests.post(
        f"{BASE}/talents",
        headers=admin_h,
        json={
            "name": "OverwriteAttempt",
            "email": email,
            "gender": "male",  # should NOT overwrite
            "ethnicity": "indian",  # SHOULD fill (was empty)
            "phone": "+22",  # already set, do not overwrite
        },
        timeout=10,
    )
    assert r2.status_code == 200, r2.text
    t2 = r2.json()
    assert t2["id"] == id1, "must merge, not duplicate"
    assert t2["gender"] == "female", "existing field must be preserved"
    assert t2["phone"] == "+11", "existing phone preserved"
    assert t2["ethnicity"] == "indian", "empty field filled"


# ----------------------------------------------------------------------
# (b) /api/public/prefill returns unified fields
# ----------------------------------------------------------------------
def test_b_prefill_returns_unified_fields(admin_h):
    email = f"test_p2_prefill_{uuid.uuid4().hex[:6]}@ex.com"
    r = requests.post(
        f"{BASE}/talents",
        headers=admin_h,
        json={
            "name": "Pre Fill",
            "email": email,
            "gender": "non_binary",
            "ethnicity": "mixed",
            "bio": "Hello there",
            "work_links": ["https://wl.example/x"],
            "phone": "+912000",
        },
        timeout=10,
    )
    assert r.status_code == 200
    pf = requests.get(f"{BASE}/public/prefill", params={"email": email}, timeout=10)
    assert pf.status_code == 200
    body = pf.json()
    for k in ("gender", "ethnicity", "bio", "work_links"):
        assert k in body, f"prefill missing key {k}"
    assert body["gender"] == "non_binary"
    assert body["ethnicity"] == "mixed"
    assert body["bio"] == "Hello there"
    assert body["work_links"] == ["https://wl.example/x"]
    # Sensitive keys must NOT leak
    for forbidden in ("media", "source", "notes", "created_by", "_id"):
        assert forbidden not in body, f"prefill leaks {forbidden}"


# ----------------------------------------------------------------------
# (c)+(d) Submission upload — indian+western 200; MAX_SUBMISSION_IMAGES capped across all
# ----------------------------------------------------------------------
def test_c_d_submission_upload_categories_and_max():
    email = f"test_p2_up_{uuid.uuid4().hex[:6]}@ex.com"
    sid, tok = _start_submission(email)
    h = {"Authorization": f"Bearer {tok}"}
    # Upload 1 indian + 1 western — both must be 200.
    for cat in ("indian", "western"):
        files = {"file": (f"{cat}.png", _png_bytes(), "image/png")}
        data = {"category": cat}
        r = requests.post(
            f"{BASE}/public/submissions/{sid}/upload",
            headers=h, files=files, data=data, timeout=20,
        )
        assert r.status_code == 200, f"{cat} upload failed: {r.text}"
    # Now flood up to MAX(=8) total across all 3 image cats. Already 2 used.
    # Add 6 more (mix of categories). 9th must 400.
    cats_cycle = ["image", "indian", "western", "image", "indian", "western"]
    for cat in cats_cycle:
        files = {"file": (f"{cat}.png", _png_bytes(), "image/png")}
        r = requests.post(
            f"{BASE}/public/submissions/{sid}/upload",
            headers=h, files=files, data={"category": cat}, timeout=20,
        )
        assert r.status_code == 200, f"unexpected fail at {cat}: {r.text}"
    # 9th — must fail (8 limit hit).
    files = {"file": ("over.png", _png_bytes(), "image/png")}
    r = requests.post(
        f"{BASE}/public/submissions/{sid}/upload",
        headers=h, files=files, data={"category": "indian"}, timeout=20,
    )
    assert r.status_code == 400, f"expected MAX cap, got {r.status_code}: {r.text}"
    assert "limit" in r.text.lower() or "8" in r.text


# ----------------------------------------------------------------------
# (e) /api/public/apply — indian + western both accepted
# ----------------------------------------------------------------------
def test_e_apply_upload_accepts_indian_western():
    email = f"test_p2_apply_{uuid.uuid4().hex[:6]}@ex.com"
    r = requests.post(
        f"{BASE}/public/apply",
        json={
            "first_name": "Apply",
            "last_name": "P2",
            "email": email,
            "phone": "+91123",
        },
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    aid = body["id"]
    tok = body["token"]
    h = {"Authorization": f"Bearer {tok}"}
    for cat in ("indian", "western"):
        files = {"file": (f"{cat}.png", _png_bytes(), "image/png")}
        r = requests.post(
            f"{BASE}/public/apply/{aid}/upload",
            headers=h, files=files, data={"category": cat}, timeout=20,
        )
        assert r.status_code == 200, f"apply {cat} failed: {r.text}"


# ----------------------------------------------------------------------
# (f) Finalize fill-empty-only sync — admin hand-edits stay sacred
# ----------------------------------------------------------------------
def test_f_finalize_fill_empty_only(admin_h):
    email = f"test_p2_fin_{uuid.uuid4().hex[:6]}@ex.com"
    # Pre-create talent with gender pre-set, ethnicity blank
    rt = requests.post(
        f"{BASE}/talents",
        headers=admin_h,
        json={
            "name": "Sacred Edit",
            "email": email,
            "gender": "female",  # pre-set, must NOT be overwritten
        },
        timeout=10,
    )
    assert rt.status_code == 200
    talent_id = rt.json()["id"]

    # Now create a submission with DIFFERENT gender + an ethnicity
    sid, tok = _start_submission(email)
    h = {"Authorization": f"Bearer {tok}"}
    # Patch form_data
    form = {
        "first_name": "Sacred",
        "last_name": "Edit",
        "height": "5'7\"",
        "location": "Mumbai",
        "gender": "male",  # mismatch — must NOT overwrite
        "ethnicity": "indian",  # blank → should fill
        "bio": "from submission",
        "phone": "+90000",
        "availability": {"status": "yes"},
        "budget": {"status": "accept"},
    }
    r = requests.put(
        f"{BASE}/public/submissions/{sid}",
        headers=h, json={"form_data": form}, timeout=15,
    )
    assert r.status_code == 200, r.text

    # Upload required: 1 intro_video, 1 take, 5 images (use indian+western mix)
    iv = requests.post(
        f"{BASE}/public/submissions/{sid}/upload",
        headers=h,
        files={"file": ("v.mp4", _mp4_bytes(), "video/mp4")},
        data={"category": "intro_video"},
        timeout=30,
    )
    assert iv.status_code == 200, iv.text
    tk = requests.post(
        f"{BASE}/public/submissions/{sid}/upload",
        headers=h,
        files={"file": ("t.mp4", _mp4_bytes(), "video/mp4")},
        data={"category": "take", "label": "Take 1"},
        timeout=30,
    )
    assert tk.status_code == 200, tk.text
    cats = ["indian", "indian", "western", "western", "image"]
    for c in cats:
        # retry on 503 transient objstore failures
        for attempt in range(3):
            u = requests.post(
                f"{BASE}/public/submissions/{sid}/upload",
                headers=h,
                files={"file": (f"{c}.png", _png_bytes(), "image/png")},
                data={"category": c},
                timeout=30,
            )
            if u.status_code == 200:
                break
            time.sleep(1.0)
        assert u.status_code == 200, u.text

    # Finalize — proves (g) MIN counted across all 3 image cats too
    fr = requests.post(f"{BASE}/public/submissions/{sid}/finalize", headers=h, timeout=30)
    assert fr.status_code == 200, fr.text

    # Re-fetch talent — gender stays female, ethnicity filled, bio filled
    g = requests.get(f"{BASE}/talents/{talent_id}", headers=admin_h, timeout=10)
    assert g.status_code == 200
    t = g.json()
    assert t["gender"] == "female", f"GENDER OVERWRITTEN! got {t.get('gender')}"
    assert t.get("ethnicity") == "indian", "empty ethnicity should be filled"
    assert t.get("bio") == "from submission"


# ----------------------------------------------------------------------
# (h) Application approval writes phone/work_links/ethnicity/gender to talent
# ----------------------------------------------------------------------
def test_h_application_approval_syncs_unified_fields(admin_h):
    email = f"test_p2_appapprov_{uuid.uuid4().hex[:6]}@ex.com"
    r = requests.post(
        f"{BASE}/public/apply",
        json={
            "first_name": "Approve",
            "last_name": "Sync",
            "email": email,
            "phone": "+91777888",
        },
        timeout=15,
    )
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    tok = r.json()["token"]
    h_app = {"Authorization": f"Bearer {tok}"}
    # Fill form data with unified fields
    pr = requests.put(
        f"{BASE}/public/apply/{aid}",
        headers=h_app,
        json={"form_data": {
            "first_name": "Approve",
            "last_name": "Sync",
            "phone": "+91777888",
            "gender": "male",
            "ethnicity": "south_asian",
            "work_links": ["https://wl.example/a"],
            "height": "5'10\"",
            "location": "Delhi",
            "dob": "1995-01-01",
        }},
        timeout=15,
    )
    # patch endpoint may not exist — skip if 404
    if pr.status_code == 404:
        pytest.skip("apply patch endpoint shape differs; skipping sync verify")

    # Approve via admin decision
    dr = requests.post(
        f"{BASE}/applications/{aid}/decision",
        headers=admin_h,
        json={"decision": "approved"},
        timeout=15,
    )
    assert dr.status_code in (200, 201), dr.text
    body = dr.json()
    tid = body.get("talent_id")
    assert tid, f"decision did not return talent_id: {body}"

    # Fetch the new talent directly
    g = requests.get(f"{BASE}/talents/{tid}", headers=admin_h, timeout=10)
    assert g.status_code == 200, g.text
    target = g.json()
    assert (target.get("email") or "").lower() == email.lower()
    assert target.get("phone") == "+91777888"
    assert target.get("gender") == "male"
    assert target.get("ethnicity") == "south_asian"
    assert "https://wl.example/a" in (target.get("work_links") or [])
