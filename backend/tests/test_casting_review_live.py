"""Live-API integration tests for the casting review overhaul:
 - resume submissions (same email → resumed=true)
 - renamable takes (category='take' with label)
 - PATCH/DELETE media behaviour
 - retest flow: re-upload after finalize flips status=updated, decision=pending
 - public/links client payload: order takes→intro→images, competitive_brand,
   custom_answers (bool + per-question dict)
Runs against REACT_APP_BACKEND_URL (preview env). Cleans up seeded data.
"""
import io
import os
import uuid

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback for when the backend is tested standalone
    BASE_URL = "https://casting-deck-pro.preview.emergentagent.com"

ADMIN = {"email": "admin@talentgram.com", "password": "Admin@123"}

TINY_JPG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707"
    "07090908"
    + "0a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c303132"
    + "31191f3539332f38273132ffdb0043010909090c0b0c180d0d18321e1e32323232323232"
    "32323232323232323232323232323232323232323232323232323232323232323232323232"
    + "ffc00011080001000103012200021101031101ffc4001500010100000000000000000000"
    "00000000000000ffc4001f1000030003020302040305050404000000010002030004050611"
    "072131124151ff"
)

TINY_MP4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 64


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json=ADMIN, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def project(admin_headers):
    body = {
        "brand_name": f"TEST_Casting_{uuid.uuid4().hex[:6]}",
        "character": "Lead Actor",
        "competitive_brand_enabled": True,
        "custom_questions": [
            {"question": "Passport?"},
            {"question": "Bike?"},
        ],
    }
    r = requests.post(f"{BASE_URL}/api/projects", json=body, headers=admin_headers, timeout=30)
    assert r.status_code in (200, 201), r.text
    proj = r.json()
    yield proj
    # teardown
    try:
        requests.delete(f"{BASE_URL}/api/projects/{proj['id']}", headers=admin_headers, timeout=30)
    except Exception:
        pass


def _start_submission(slug, email, name="Alice Tester"):
    r = requests.post(
        f"{BASE_URL}/api/public/projects/{slug}/submission",
        json={"name": name, "email": email},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _upload(sid, token, category, file_bytes, filename, content_type, label=None):
    data = {"category": category}
    if label is not None:
        data["label"] = label
    files = {"file": (filename, io.BytesIO(file_bytes), content_type)}
    r = requests.post(
        f"{BASE_URL}/api/public/submissions/{sid}/upload",
        headers={"Authorization": f"Bearer {token}"},
        data=data, files=files, timeout=60,
    )
    return r


# --------- resume / start ---------
def test_start_new_then_resume_same_email(project):
    slug = project["slug"]
    email = f"test_{uuid.uuid4().hex[:6]}@ex.com"
    a = _start_submission(slug, email)
    assert a["resumed"] is False
    assert a["status"] == "draft"
    b = _start_submission(slug, email)
    assert b["resumed"] is True
    assert b["id"] == a["id"]


# --------- takes upload, label, PATCH ---------
def test_take_upload_with_label_and_without(project):
    slug = project["slug"]
    email = f"test_{uuid.uuid4().hex[:6]}@ex.com"
    start = _start_submission(slug, email)
    sid, tok = start["id"], start["token"]

    r1 = _upload(sid, tok, "take", TINY_MP4, "t.mp4", "video/mp4", label="Scene 1")
    assert r1.status_code == 200, r1.text
    media_after = r1.json()["media"]
    takes = [m for m in media_after if m["category"] == "take"]
    assert takes and takes[-1]["label"] == "Scene 1"

    r2 = _upload(sid, tok, "take", TINY_MP4, "t.mp4", "video/mp4", label=None)
    assert r2.status_code == 200
    takes = [m for m in r2.json()["media"] if m["category"] == "take"]
    assert takes[-1]["label"] == "Take 2"


def test_patch_take_label_rules(project):
    slug = project["slug"]
    email = f"test_{uuid.uuid4().hex[:6]}@ex.com"
    start = _start_submission(slug, email)
    sid, tok = start["id"], start["token"]
    r = _upload(sid, tok, "take", TINY_MP4, "t.mp4", "video/mp4", label="Old")
    take_id = [m for m in r.json()["media"] if m["category"] == "take"][-1]["id"]

    # rename
    rn = requests.patch(
        f"{BASE_URL}/api/public/submissions/{sid}/media/{take_id}",
        headers={"Authorization": f"Bearer {tok}"},
        json={"label": "New name"}, timeout=30,
    )
    assert rn.status_code == 200
    media = rn.json()["media"]
    assert [m for m in media if m["id"] == take_id][0]["label"] == "New name"

    # empty label → 400
    rn2 = requests.patch(
        f"{BASE_URL}/api/public/submissions/{sid}/media/{take_id}",
        headers={"Authorization": f"Bearer {tok}"},
        json={"label": "   "}, timeout=30,
    )
    assert rn2.status_code == 400

    # non-take (upload an intro video and try to patch it)
    ri = _upload(sid, tok, "intro_video", TINY_MP4, "v.mp4", "video/mp4")
    intro_id = [m for m in ri.json()["media"] if m["category"] == "intro_video"][0]["id"]
    rn3 = requests.patch(
        f"{BASE_URL}/api/public/submissions/{sid}/media/{intro_id}",
        headers={"Authorization": f"Bearer {tok}"},
        json={"label": "hi"}, timeout=30,
    )
    assert rn3.status_code == 400


def test_max_5_takes(project):
    slug = project["slug"]
    email = f"test_{uuid.uuid4().hex[:6]}@ex.com"
    start = _start_submission(slug, email)
    sid, tok = start["id"], start["token"]
    for i in range(5):
        r = _upload(sid, tok, "take", TINY_MP4, "t.mp4", "video/mp4", label=f"T{i}")
        assert r.status_code == 200, f"take {i} failed: {r.text}"
    r6 = _upload(sid, tok, "take", TINY_MP4, "t.mp4", "video/mp4", label="T6")
    assert r6.status_code == 400
    assert "5" in r6.text


# --------- retest / re-upload flow ---------
def _finalize_ready(sid, tok):
    """Fill mandatory fields + required media to pass finalize."""
    # form
    requests.put(
        f"{BASE_URL}/api/public/submissions/{sid}",
        headers={"Authorization": f"Bearer {tok}"},
        json={"form_data": {
            "first_name": "A", "last_name": "B",
            "height": "5'8\"", "location": "Mumbai",
            "availability": {"status": "yes"},
            "budget": {"status": "accept"},
        }}, timeout=30,
    )
    # 1 take + 1 intro + 5 images
    _upload(sid, tok, "take", TINY_MP4, "t.mp4", "video/mp4", label="S1")
    _upload(sid, tok, "intro_video", TINY_MP4, "v.mp4", "video/mp4")
    for _ in range(5):
        _upload(sid, tok, "image", TINY_JPG, "p.jpg", "image/jpeg")


def test_retest_flow_reupload_flips_status(project, admin_headers):
    slug = project["slug"]
    email = f"test_{uuid.uuid4().hex[:6]}@ex.com"
    start = _start_submission(slug, email)
    sid, tok = start["id"], start["token"]
    _finalize_ready(sid, tok)
    fz = requests.post(f"{BASE_URL}/api/public/submissions/{sid}/finalize",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    assert fz.status_code == 200, fz.text
    assert fz.json()["status"] == "submitted"
    assert fz.json()["resubmitted"] is False

    # admin approves
    requests.post(
        f"{BASE_URL}/api/projects/{project['id']}/submissions/{sid}/decision",
        json={"decision": "approved"}, headers=admin_headers, timeout=30,
    )

    # re-upload after finalize → status=updated, decision=pending
    r2 = _upload(sid, tok, "take", TINY_MP4, "t.mp4", "video/mp4", label="Redo")
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "updated"
    assert body["decision"] == "pending"

    # re-finalize → resubmitted=true, status=updated
    fz2 = requests.post(f"{BASE_URL}/api/public/submissions/{sid}/finalize",
                        headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    assert fz2.status_code == 200
    assert fz2.json()["status"] == "updated"
    assert fz2.json()["resubmitted"] is True


def test_delete_after_finalize_flips_status(project, admin_headers):
    slug = project["slug"]
    email = f"test_{uuid.uuid4().hex[:6]}@ex.com"
    start = _start_submission(slug, email)
    sid, tok = start["id"], start["token"]
    _finalize_ready(sid, tok)
    requests.post(f"{BASE_URL}/api/public/submissions/{sid}/finalize",
                  headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    requests.post(
        f"{BASE_URL}/api/projects/{project['id']}/submissions/{sid}/decision",
        json={"decision": "approved"}, headers=admin_headers, timeout=30,
    )
    # fetch media
    cur = requests.get(f"{BASE_URL}/api/public/submissions/{sid}",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=30).json()
    take = next(m for m in cur["media"] if m["category"] == "take")
    rd = requests.delete(
        f"{BASE_URL}/api/public/submissions/{sid}/media/{take['id']}",
        headers={"Authorization": f"Bearer {tok}"}, timeout=30,
    )
    assert rd.status_code == 200
    after = requests.get(f"{BASE_URL}/api/public/submissions/{sid}",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=30).json()
    assert after["status"] == "updated"
    assert after["decision"] == "pending"


# --------- public/links payload ---------
def _make_link(admin_headers, project_id, submission_ids, field_visibility_updates=None,
               form_data_updates=None):
    # Apply visibility / form_data updates per submission
    for sid in submission_ids:
        patch = {}
        if field_visibility_updates is not None:
            patch["field_visibility"] = field_visibility_updates
        if form_data_updates is not None:
            patch["form_data"] = form_data_updates
        if patch:
            r = requests.put(
                f"{BASE_URL}/api/projects/{project_id}/submissions/{sid}",
                json=patch, headers=admin_headers, timeout=30,
            )
            assert r.status_code == 200, r.text
    body = {
        "title": f"TEST_Link_{uuid.uuid4().hex[:6]}",
        "brand_name": "TEST",
        "submission_ids": submission_ids,
        "visibility": {"portfolio": True, "intro_video": True, "takes": True},
        "is_public": True,
    }
    r = requests.post(f"{BASE_URL}/api/links", json=body, headers=admin_headers, timeout=30)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _identify_and_fetch(slug, email="viewer@ex.com", name="Viewer"):
    ri = requests.post(f"{BASE_URL}/api/public/links/{slug}/identify",
                       json={"name": name, "email": email}, timeout=30)
    assert ri.status_code == 200, ri.text
    vtok = ri.json()["token"]
    rg = requests.get(f"{BASE_URL}/api/public/links/{slug}",
                      headers={"Authorization": f"Bearer {vtok}"}, timeout=30)
    assert rg.status_code == 200, rg.text
    return rg.json()


def _prep_submission_for_link(project, admin_headers, form_data_extra=None):
    """Create+finalize+approve a submission; return sid."""
    email = f"test_{uuid.uuid4().hex[:6]}@ex.com"
    start = _start_submission(project["slug"], email)
    sid, tok = start["id"], start["token"]
    form = {
        "first_name": "A", "last_name": "B",
        "height": "5'8\"", "location": "Mumbai",
        "availability": {"status": "yes"},
        "budget": {"status": "accept"},
    }
    if form_data_extra:
        form.update(form_data_extra)
    requests.put(
        f"{BASE_URL}/api/public/submissions/{sid}",
        headers={"Authorization": f"Bearer {tok}"},
        json={"form_data": form}, timeout=30,
    )
    _upload(sid, tok, "take", TINY_MP4, "t.mp4", "video/mp4", label="Scene 1")
    _upload(sid, tok, "intro_video", TINY_MP4, "v.mp4", "video/mp4")
    for _ in range(5):
        _upload(sid, tok, "image", TINY_JPG, "p.jpg", "image/jpeg")
    requests.post(f"{BASE_URL}/api/public/submissions/{sid}/finalize",
                  headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    return sid


def test_public_link_order_and_take_category(project, admin_headers):
    sid = _prep_submission_for_link(project, admin_headers)
    link = _make_link(admin_headers, project["id"], [sid])
    payload = _identify_and_fetch(link["slug"])
    tal = payload["talents"][0]
    cats = [m["category"] for m in tal["media"]]
    # Must not expose legacy slot names
    assert "take_1" not in cats and "take_2" not in cats and "take_3" not in cats
    # Order: takes → video → portfolio
    first_take = next((i for i, c in enumerate(cats) if c == "take"), -1)
    first_video = next((i for i, c in enumerate(cats) if c == "video"), -1)
    first_image = next((i for i, c in enumerate(cats) if c == "portfolio"), -1)
    assert 0 <= first_take < first_video < first_image, cats
    # Label carried through
    take = next(m for m in tal["media"] if m["category"] == "take")
    assert take.get("label") == "Scene 1"


def test_public_link_competitive_brand_gating(project, admin_headers):
    sid = _prep_submission_for_link(
        project, admin_headers,
        form_data_extra={"competitive_brand": "ACME Corp"},
    )
    # OFF by default
    link_off = _make_link(admin_headers, project["id"], [sid])
    p1 = _identify_and_fetch(link_off["slug"])
    assert "competitive_brand" not in p1["talents"][0]
    # ON
    link_on = _make_link(
        admin_headers, project["id"], [sid],
        field_visibility_updates={"competitive_brand": True},
    )
    p2 = _identify_and_fetch(link_on["slug"])
    assert p2["talents"][0].get("competitive_brand") == "ACME Corp"


def test_public_link_custom_answers_modes(project, admin_headers):
    sid = _prep_submission_for_link(
        project, admin_headers,
        form_data_extra={"custom_answers": {"Passport?": "Yes", "Bike?": "No"}},
    )
    # bool True: both included
    link_all = _make_link(
        admin_headers, project["id"], [sid],
        field_visibility_updates={"custom_answers": True},
    )
    pa = _identify_and_fetch(link_all["slug"])
    ans = pa["talents"][0].get("custom_answers") or []
    qs = {a["question"]: a["answer"] for a in ans}
    assert qs == {"Passport?": "Yes", "Bike?": "No"}
    # per-question dict: only Bike?
    link_one = _make_link(
        admin_headers, project["id"], [sid],
        field_visibility_updates={"custom_answers": {"Passport?": False, "Bike?": True}},
    )
    pb = _identify_and_fetch(link_one["slug"])
    ans2 = pb["talents"][0].get("custom_answers") or []
    assert [a["question"] for a in ans2] == ["Bike?"]
    # bool False: absent
    link_none = _make_link(
        admin_headers, project["id"], [sid],
        field_visibility_updates={"custom_answers": False},
    )
    pc = _identify_and_fetch(link_none["slug"])
    assert "custom_answers" not in pc["talents"][0]


def test_legacy_take_category_renders_as_take_with_label(project, admin_headers):
    """Simulate a legacy submission (take_1/2/3 in DB) via admin direct edit of form,
    but we can't inject raw media via API — instead, verify the public endpoint
    already normalizes properly by checking category values."""
    sid = _prep_submission_for_link(project, admin_headers)
    link = _make_link(admin_headers, project["id"], [sid])
    payload = _identify_and_fetch(link["slug"])
    tal = payload["talents"][0]
    for m in tal["media"]:
        assert m["category"] in ("take", "video", "portfolio"), m["category"]
