"""End-to-end backend API tests for Talentgram Portfolio Link Engine."""
import io
import os
import uuid
import pytest
import requests
from dotenv import load_dotenv

# Load /app/backend/.env so secrets stay out of source.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://casting-deck-pro.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")


# ---------------- Fixtures ----------------
@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=30)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    data = r.json()
    assert "token" in data and data.get("admin", {}).get("email") == ADMIN_EMAIL
    return data["token"]


@pytest.fixture(scope="session")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="session")
def tiny_png_bytes():
    # 1x1 red PNG
    return bytes.fromhex(
        "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C489"
        "0000000D49444154789C63F80F040000FFFF03000006000557BFABD40000000049454E44AE426082"
    )


# ---------------- Auth ----------------
class TestAuth:
    def test_login_success(self):
        r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 200
        d = r.json()
        assert "token" in d
        assert d["admin"]["email"] == ADMIN_EMAIL

    def test_login_invalid(self):
        r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})
        assert r.status_code == 401

    def test_me_requires_auth(self):
        r = requests.get(f"{API}/auth/me")
        assert r.status_code in (401, 403)

    def test_me_with_token(self, admin_headers):
        r = requests.get(f"{API}/auth/me", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["email"] == ADMIN_EMAIL


# ---------------- Talents CRUD ----------------
class TestTalentCRUD:
    def test_full_talent_crud(self, admin_headers):
        # create
        payload = {
            "name": "TEST_Talent_Alpha", "age": 25, "height": "5'8\"",
            "location": "Mumbai", "ethnicity": "Indian",
            "instagram_handle": "alpha", "instagram_followers": "10k",
            "bio": "Talented", "work_links": ["https://example.com/a"],
        }
        r = requests.post(f"{API}/talents", json=payload, headers=admin_headers)
        assert r.status_code == 200, r.text
        t = r.json()
        assert t["name"] == payload["name"]
        assert t["id"]
        tid = t["id"]

        # list
        r = requests.get(f"{API}/talents", headers=admin_headers)
        assert r.status_code == 200
        assert any(x["id"] == tid for x in r.json())

        # get
        r = requests.get(f"{API}/talents/{tid}", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["name"] == payload["name"]

        # update
        upd = {**payload, "name": "TEST_Talent_Alpha_Updated", "age": 26}
        r = requests.put(f"{API}/talents/{tid}", json=upd, headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["name"] == "TEST_Talent_Alpha_Updated"

        # verify persisted
        r = requests.get(f"{API}/talents/{tid}", headers=admin_headers)
        assert r.json()["age"] == 26

        # delete
        r = requests.delete(f"{API}/talents/{tid}", headers=admin_headers)
        assert r.status_code == 200
        r = requests.get(f"{API}/talents/{tid}", headers=admin_headers)
        assert r.status_code == 404


# ---------------- Media Upload ----------------
class TestMediaUpload:
    def test_media_upload_sets_cover_and_delete(self, admin_headers, tiny_png_bytes):
        r = requests.post(f"{API}/talents", json={"name": "TEST_Media_Talent"}, headers=admin_headers)
        tid = r.json()["id"]

        files = {"file": ("a.png", io.BytesIO(tiny_png_bytes), "image/png")}
        data = {"category": "indian"}
        r = requests.post(f"{API}/talents/{tid}/media", files=files, data=data, headers=admin_headers)
        assert r.status_code == 200, r.text
        t = r.json()
        assert len(t["media"]) == 1
        assert t["cover_media_id"] == t["media"][0]["id"]
        media_id = t["media"][0]["id"]
        storage_path = t["media"][0]["storage_path"]

        # Invalid category
        r = requests.post(
            f"{API}/talents/{tid}/media",
            files={"file": ("b.png", io.BytesIO(tiny_png_bytes), "image/png")},
            data={"category": "bogus"}, headers=admin_headers,
        )
        assert r.status_code == 400

        # file serve
        r = requests.get(f"{API}/files/{storage_path}")
        assert r.status_code == 200
        assert len(r.content) > 0

        # set cover explicitly to same
        r = requests.post(f"{API}/talents/{tid}/cover/{media_id}", headers=admin_headers)
        assert r.status_code == 200

        # delete media
        r = requests.delete(f"{API}/talents/{tid}/media/{media_id}", headers=admin_headers)
        assert r.status_code == 200

        # cleanup
        requests.delete(f"{API}/talents/{tid}", headers=admin_headers)


# ---------------- Links ----------------
class TestLinks:
    def test_link_crud_and_duplicate(self, admin_headers):
        # seed two talents
        t1 = requests.post(f"{API}/talents", json={"name": "TEST_LinkT1"}, headers=admin_headers).json()
        t2 = requests.post(f"{API}/talents", json={"name": "TEST_LinkT2"}, headers=admin_headers).json()

        payload = {
            "title": "Talentgram x TestBrand",
            "brand_name": "TestBrand",
            "talent_ids": [t1["id"], t2["id"]],
            "visibility": {"download": False},
        }
        r = requests.post(f"{API}/links", json=payload, headers=admin_headers)
        assert r.status_code == 200, r.text
        link = r.json()
        assert link["slug"].startswith("talentgram-x-testbrand-")
        assert link["visibility"]["portfolio"] is True
        assert link["visibility"]["download"] is False
        lid = link["id"]

        # list
        r = requests.get(f"{API}/links", headers=admin_headers)
        assert r.status_code == 200
        assert any(l["id"] == lid for l in r.json())

        # get
        r = requests.get(f"{API}/links/{lid}", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["view_count"] == 0

        # update visibility
        upd = {**payload, "visibility": {**payload["visibility"], "download": True, "age": False}}
        r = requests.put(f"{API}/links/{lid}", json=upd, headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["visibility"]["download"] is True
        assert r.json()["visibility"]["age"] is False

        # duplicate
        r = requests.post(f"{API}/links/{lid}/duplicate", headers=admin_headers)
        assert r.status_code == 200
        dup = r.json()
        assert dup["id"] != lid
        assert dup["slug"] != link["slug"]
        assert dup["talent_ids"] == payload["talent_ids"]

        # cleanup
        requests.delete(f"{API}/links/{dup['id']}", headers=admin_headers)
        requests.delete(f"{API}/links/{lid}", headers=admin_headers)
        requests.delete(f"{API}/talents/{t1['id']}", headers=admin_headers)
        requests.delete(f"{API}/talents/{t2['id']}", headers=admin_headers)


# ---------------- Public viewer flow + E2E ----------------
class TestE2EFlow:
    def test_full_flow(self, admin_headers, tiny_png_bytes):
        # create 2 talents
        t1 = requests.post(f"{API}/talents", json={"name": "TEST_E2E_T1", "age": 24}, headers=admin_headers).json()
        t2 = requests.post(f"{API}/talents", json={"name": "TEST_E2E_T2", "age": 28}, headers=admin_headers).json()

        # upload image for t1
        r = requests.post(
            f"{API}/talents/{t1['id']}/media",
            files={"file": ("c.png", io.BytesIO(tiny_png_bytes), "image/png")},
            data={"category": "portfolio"},
            headers=admin_headers,
        )
        assert r.status_code == 200
        media_id = r.json()["media"][0]["id"]

        # create link
        r = requests.post(f"{API}/links", json={
            "title": "Talentgram x E2EBrand",
            "brand_name": "E2EBrand",
            "talent_ids": [t1["id"], t2["id"]],
        }, headers=admin_headers)
        assert r.status_code == 200
        link = r.json()
        slug = link["slug"]
        lid = link["id"]

        # public: fetch without identity => 401
        r = requests.get(f"{API}/public/links/{slug}")
        assert r.status_code == 401

        # identify
        viewer_email = f"test_{uuid.uuid4().hex[:6]}@example.com"
        r = requests.post(f"{API}/public/links/{slug}/identify",
                          json={"name": "Test Viewer", "email": viewer_email})
        assert r.status_code == 200
        vtoken = r.json()["token"]
        vheaders = {"Authorization": f"Bearer {vtoken}"}

        # fetch link with viewer
        r = requests.get(f"{API}/public/links/{slug}", headers=vheaders)
        assert r.status_code == 200
        body = r.json()
        assert len(body["talents"]) == 2
        assert body["viewer"]["email"] == viewer_email
        # order preservation
        assert [t["id"] for t in body["talents"]] == [t1["id"], t2["id"]]

        # action: shortlist t1 with comment
        r = requests.post(f"{API}/public/links/{slug}/action",
                          json={"talent_id": t1["id"], "action": "shortlist", "comment": "Perfect fit"},
                          headers=vheaders)
        assert r.status_code == 200

        # action: interested t2
        r = requests.post(f"{API}/public/links/{slug}/action",
                          json={"talent_id": t2["id"], "action": "interested"},
                          headers=vheaders)
        assert r.status_code == 200

        # clearing action keeps comment
        r = requests.post(f"{API}/public/links/{slug}/action",
                          json={"talent_id": t1["id"], "action": None},
                          headers=vheaders)
        assert r.status_code == 200

        # re-fetch link -> actions persisted
        r = requests.get(f"{API}/public/links/{slug}", headers=vheaders)
        acts = r.json()["actions"]
        t1_act = next((a for a in acts if a["talent_id"] == t1["id"]), None)
        assert t1_act is not None
        assert t1_act["comment"] == "Perfect fit"
        assert t1_act["action"] is None  # cleared

        # download log: should 403 initially (download=False by default)
        r = requests.post(f"{API}/public/links/{slug}/download-log",
                          json={"talent_id": t1["id"], "media_id": media_id},
                          headers=vheaders)
        assert r.status_code == 403

        # enable download visibility
        vis = {**link["visibility"], "download": True}
        r = requests.put(f"{API}/links/{lid}", json={
            "title": link["title"], "brand_name": link["brand_name"],
            "talent_ids": link["talent_ids"], "visibility": vis,
        }, headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["visibility"]["download"] is True

        # now download log succeeds
        r = requests.post(f"{API}/public/links/{slug}/download-log",
                          json={"talent_id": t1["id"], "media_id": media_id},
                          headers=vheaders)
        assert r.status_code == 200

        # admin results
        r = requests.get(f"{API}/links/{lid}/results", headers=admin_headers)
        assert r.status_code == 200
        res = r.json()
        assert res["view_count"] >= 1
        assert res["unique_viewers"] >= 1
        assert len(res["downloads"]) >= 1
        # summary per talent
        summary_by_tid = {s["talent_id"]: s for s in res["summary"]}
        # t2 was marked interested
        assert summary_by_tid[t2["id"]]["interested"] == 1
        # t1's comment should appear
        t1_comments = summary_by_tid[t1["id"]]["comments"]
        assert any(c["comment"] == "Perfect fit" for c in t1_comments)

        # aggregated list counts
        r = requests.get(f"{API}/links", headers=admin_headers)
        link_listing = next(l for l in r.json() if l["id"] == lid)
        assert link_listing["view_count"] >= 1
        assert link_listing["unique_viewers"] >= 1

        # cleanup cascades: delete link removes views/actions/downloads
        requests.delete(f"{API}/links/{lid}", headers=admin_headers)
        r = requests.get(f"{API}/links/{lid}", headers=admin_headers)
        assert r.status_code == 404

        # cleanup talents
        requests.delete(f"{API}/talents/{t1['id']}", headers=admin_headers)
        requests.delete(f"{API}/talents/{t2['id']}", headers=admin_headers)
