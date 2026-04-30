"""
Iteration 10 — Diagnostic-only health audit (read-only).
Touches public + admin API surface. Does NOT mutate seed/prod data unless explicitly creating TEST_ data and noted.
"""
import os
import time
import uuid
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://casting-deck-pro.preview.emergentagent.com").rstrip("/")
API = f"{BASE}/api"
SLUG = "autoflow-c66e26"
KNOWN_EMAIL = "test_afd08d@ex.com"
ADMIN_EMAIL = "admin@talentgram.com"
ADMIN_PASS = "Admin@123"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=15)
    if r.status_code == 200:
        tok = r.json().get("access_token") or r.json().get("token")
        if tok:
            s.headers["Authorization"] = f"Bearer {tok}"
    assert r.status_code == 200, f"admin login failed {r.status_code}: {r.text[:300]}"
    return s


# ---------- Backend API health smoke (no 500s) ----------
def test_health_public_project():
    r = requests.get(f"{API}/public/projects/{SLUG}", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert "slug" in j or "id" in j

def test_health_public_prefill_known():
    r = requests.get(f"{API}/public/prefill", params={"email": KNOWN_EMAIL}, timeout=15)
    assert r.status_code == 200
    j = r.json()
    # security: must NOT leak forbidden fields
    forbidden = {"media", "bio", "gender", "work_links", "ethnicity", "source"}
    assert not (forbidden & set(j.keys())), f"Forbidden fields leaked: {forbidden & set(j.keys())}"

def test_health_public_prefill_empty():
    r = requests.get(f"{API}/public/prefill", params={"email": ""}, timeout=15)
    assert r.status_code == 200, f"empty email returned {r.status_code}: {r.text[:200]}"
    assert r.json() == {} or r.json() == {"data": {}}

def test_health_public_prefill_invalid():
    r = requests.get(f"{API}/public/prefill", params={"email": "notanemail"}, timeout=15)
    assert r.status_code == 200, f"invalid email returned {r.status_code}: {r.text[:200]}"

def test_health_public_prefill_uppercase_normalized():
    r = requests.get(f"{API}/public/prefill", params={"email": KNOWN_EMAIL.upper()}, timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert j, f"Uppercase email did not match seeded talent — case normalization missing? body={j}"

def test_health_admin_talents(admin_session):
    r = admin_session.get(f"{API}/talents", params={"size": 50}, timeout=15)
    assert r.status_code == 200, r.text[:300]
    j = r.json()
    assert isinstance(j, dict) or isinstance(j, list)

def test_health_admin_projects(admin_session):
    r = admin_session.get(f"{API}/projects", timeout=15)
    assert r.status_code == 200, r.text[:300]

def test_health_admin_applications(admin_session):
    r = admin_session.get(f"{API}/applications", timeout=15)
    assert r.status_code == 200, r.text[:300]

def test_health_admin_links(admin_session):
    r = admin_session.get(f"{API}/links", timeout=15)
    assert r.status_code == 200, r.text[:300]

def test_health_admin_feedback(admin_session):
    r = admin_session.get(f"{API}/admin/feedback", timeout=15)
    assert r.status_code == 200, r.text[:300]

def test_health_admin_notifications(admin_session):
    r = admin_session.get(f"{API}/notifications", timeout=15)
    assert r.status_code == 200, r.text[:300]


# ---------- Error handling ----------
def test_err_apply_invalid_json():
    r = requests.post(f"{API}/public/apply",
                      data="not-json",
                      headers={"Content-Type": "application/json"}, timeout=15)
    assert r.status_code in (400, 422), f"expected 4xx got {r.status_code}: {r.text[:200]}"
    assert r.status_code < 500

def test_err_submission_empty_payload():
    r = requests.post(f"{API}/public/projects/{SLUG}/submission", json={}, timeout=15)
    assert 400 <= r.status_code < 500, f"expected 4xx got {r.status_code}: {r.text[:200]}"

def test_err_post_talents_missing_required(admin_session):
    r = admin_session.post(f"{API}/talents", json={}, timeout=15)
    assert 400 <= r.status_code < 500, f"expected 4xx got {r.status_code}: {r.text[:200]}"


# ---------- Flow C: project submission resume (idempotent) ----------
def test_flow_c_submission_resume():
    email = f"flowc_{uuid.uuid4().hex[:8]}@ex.com"
    r1 = requests.post(f"{API}/public/projects/{SLUG}/submission",
                       json={"email": email, "name": "TEST_C Resume"}, timeout=20)
    assert r1.status_code in (200, 201), r1.text[:300]
    j1 = r1.json()
    sid1 = j1.get("submission_id") or j1.get("id") or (j1.get("submission") or {}).get("id")
    assert sid1, f"No submission id in {j1}"
    # second post -> resumed:true
    r2 = requests.post(f"{API}/public/projects/{SLUG}/submission",
                       json={"email": email, "name": "TEST_C Resume"}, timeout=20)
    assert r2.status_code in (200, 201), r2.text[:300]
    j2 = r2.json()
    sid2 = j2.get("submission_id") or j2.get("id") or (j2.get("submission") or {}).get("id")
    assert sid2 == sid1, f"Submission re-created instead of resumed: {sid1} vs {sid2}"
    assert j2.get("resumed") is True, f"resumed flag missing in resume response: {j2}"


# ---------- Flow B prefill known + unknown silent ----------
def test_flow_b_prefill_unknown_silent():
    r = requests.get(f"{API}/public/prefill",
                     params={"email": f"nobody_{uuid.uuid4().hex[:6]}@ex.com"}, timeout=15)
    assert r.status_code == 200
    assert r.json() in ({}, {"data": {}}), r.json()


# ---------- Flow E: admin manual talent dedup by email ----------
def test_flow_e_admin_create_duplicate_email_upserts(admin_session):
    # Use a TEST_ email that we'll create twice
    email = f"test_dup_{uuid.uuid4().hex[:6]}@ex.com"
    payload = {"name": "TEST_DupA X", "email": email}
    r1 = admin_session.post(f"{API}/talents", json=payload, timeout=15)
    assert r1.status_code in (200, 201), r1.text[:300]
    id1 = r1.json().get("id") or r1.json().get("_id")
    # second create with same email -> should NOT duplicate
    payload2 = {"name": "TEST_DupB Y", "email": email}
    r2 = admin_session.post(f"{API}/talents", json=payload2, timeout=15)
    body2 = r2.json() if r2.headers.get("content-type", "").startswith("application/json") else {}
    # accept upsert/merge OR explicit 409 — flag exact behavior in report
    if r2.status_code in (200, 201):
        id2 = body2.get("id") or body2.get("_id")
        assert id2 == id1, f"DUPLICATE TALENT CREATED: {id1} vs {id2} — upsert/merge missing"
    else:
        assert r2.status_code == 409, f"Expected upsert or 409, got {r2.status_code}: {r2.text[:200]}"


# ---------- Security: prefill projection allowlist ----------
def test_security_prefill_no_pii_leak():
    r = requests.get(f"{API}/public/prefill", params={"email": KNOWN_EMAIL}, timeout=15)
    j = r.json()
    forbidden = ["media", "bio", "gender", "work_links", "ethnicity", "source", "headshots", "portfolio"]
    leaked = [k for k in forbidden if k in j]
    assert not leaked, f"PII LEAK: {leaked} in prefill response keys={list(j.keys())}"


# ---------- Security: rate limiter on prefill (21st in 60s -> 429) ----------
def test_security_prefill_rate_limit():
    headers = {"X-Forwarded-For": f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"}
    statuses = []
    for _ in range(22):
        r = requests.get(f"{API}/public/prefill",
                         params={"email": "ratelimit@ex.com"}, headers=headers, timeout=10)
        statuses.append(r.status_code)
    assert 429 in statuses, f"No 429 after 22 calls; got {statuses}"


# ---------- Public link projection strips admin-only fields ----------
def test_security_public_link_projection(admin_session):
    # find a real link slug
    rl = admin_session.get(f"{API}/links", timeout=15).json()
    items = rl.get("items") if isinstance(rl, dict) else rl
    if not items:
        pytest.skip("no links to validate projection")
    slug = items[0].get("slug")
    if not slug:
        pytest.skip("link missing slug")
    r = requests.get(f"{API}/public/links/{slug}", timeout=15)
    if r.status_code != 200:
        pytest.skip(f"public link returned {r.status_code}")
    keys = set(r.json().keys()) if isinstance(r.json(), dict) else set()
    leaked = keys & {"notes", "password", "created_by", "talent_ids", "submission_ids"}
    assert not leaked, f"Admin-only fields leaked on public link: {leaked}"


# ---------- Performance ----------
@pytest.mark.parametrize("path,params", [
    ("/talents", {"size": 50}),
    ("/projects", {}),
    ("/links", {}),
])
def test_perf_admin_endpoints(admin_session, path, params):
    t0 = time.time()
    r = admin_session.get(f"{API}{path}", params=params, timeout=15)
    dt = time.time() - t0
    assert r.status_code == 200
    assert dt < 3.0, f"{path} slow: {dt:.2f}s"
    print(f"PERF {path} {dt*1000:.0f}ms")

def test_perf_prefill_speed():
    t0 = time.time()
    r = requests.get(f"{API}/public/prefill", params={"email": KNOWN_EMAIL}, timeout=15)
    dt = time.time() - t0
    assert r.status_code == 200
    print(f"PERF prefill {dt*1000:.0f}ms")
    assert dt < 2.0


# ---------- Pagination ----------
def test_pagination_talents(admin_session):
    r1 = admin_session.get(f"{API}/talents", params={"page": 0, "size": 50}, timeout=15)
    r2 = admin_session.get(f"{API}/talents", params={"page": 0, "size": 200}, timeout=15)
    assert r1.status_code == 200 and r2.status_code == 200
    j1, j2 = r1.json(), r2.json()
    items1 = j1.get("items", j1) if isinstance(j1, dict) else j1
    items2 = j2.get("items", j2) if isinstance(j2, dict) else j2
    assert len(items1) <= 50
    assert len(items2) <= 200
    assert len(items2) >= len(items1)
