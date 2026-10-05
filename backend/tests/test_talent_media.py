"""Talent Media Download Link — backend tests (A–K from the brief).

Real local Mongo (isolated throwaway DB, dropped per test) + an in-process
ASGI app mounting only the new router, + a real local HTTP server standing in
for the media CDN so the streaming / ZIP paths run end to end with real httpx.
"""
import asyncio
import io
import os
import threading
import tracemalloc
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "password")

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient

import core
from routers import submissions as sub_router
from routers import talent_media as tm

pytestmark = pytest.mark.asyncio

_MONGO_URL = os.environ.get("TEST_REAL_MONGO_URL", "mongodb://localhost:27017")

# ---- fake CDN ---------------------------------------------------------------
_FILES = {
    "/i1.jpg": b"INDIAN-1" * 50, "/i2.jpg": b"INDIAN-2" * 50,
    "/w1.jpg": b"WESTERN-1" * 50, "/p1.png": b"PORT-1" * 50,
    "/v1.mp4": b"VIDEO" * 5000,
}


class _CDN(BaseHTTPRequestHandler):
    def do_GET(self):
        body = _FILES.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def cdn():
    srv = HTTPServer(("127.0.0.1", 0), _CDN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# ---- app + db fixtures --------------------------------------------------------
@pytest_asyncio.fixture
async def env(monkeypatch):
    client = AsyncIOMotorClient(_MONGO_URL)
    dbname = f"tg_talent_media_test_{uuid.uuid4().hex[:8]}"
    tdb = client[dbname]
    monkeypatch.setattr(tm, "db", tdb)
    monkeypatch.setattr(sub_router, "db", tdb)
    monkeypatch.setattr(tm, "_indexes_ready", False)
    # neutralise the per-IP limiter between tests
    monkeypatch.setattr(tm, "rate_limit_ok", lambda *a, **k: True)
    app = FastAPI()
    app.include_router(tm.router)
    app.dependency_overrides[core.current_team_or_admin] = lambda: {"email": "admin@x.co", "role": "admin"}
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    yield tdb, http
    await http.aclose()
    await client.drop_database(dbname)
    client.close()


def _media(cdn, cat, path, mid=None, **extra):
    return {"id": mid or uuid.uuid4().hex, "category": cat, "url": f"{cdn}{path}",
            "content_type": "video/mp4" if cat == "video" else "image/jpeg",
            "resource_type": "video" if cat == "video" else "image",
            "created_at": "2026-09-29T10:00:00+00:00", **extra}


async def _talent(tdb, cdn, name="Jasmine Singh", **extra):
    doc = {
        "id": str(uuid.uuid4()), "name": name, "status": "ACTIVE",
        "media": [
            _media(cdn, "indian", "/i1.jpg", "m-i1"), _media(cdn, "indian", "/i2.jpg", "m-i2"),
            _media(cdn, "western", "/w1.jpg", "m-w1"),
            _media(cdn, "portfolio", "/p1.png", "m-p1"),
            _media(cdn, "video", "/v1.mp4", "m-v1", provider="direct"),
        ],
    }
    doc.update(extra)
    await tdb.talents.insert_one(dict(doc))
    return doc


async def _link(http, tdb, tid, enabled=True):
    r = await http.patch(f"/api/talents/{tid}/media-download", json={"enabled": enabled})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# A — link generation
async def test_a_enabled_talent_gets_valid_opaque_token(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    token = await _link(http, tdb, t["id"], True)
    assert len(token) >= 30 and t["id"] not in token
    r = await http.get(f"/api/public/talent-media/{token}")
    assert r.status_code == 200
    body = r.json()
    assert body["talent"]["name"] == "Jasmine S"
    assert body["total"] == 5
    assert "id" not in body["talent"] and "email" not in r.text and "Singh" not in r.text


# B / C / D — toggle semantics, enforced server-side on every request
async def test_b_c_d_toggle_off_kills_existing_link_and_on_revives_same_link(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    token = await _link(http, tdb, t["id"], True)
    assert (await http.get(f"/api/public/talent-media/{token}")).status_code == 200

    assert await _link(http, tdb, t["id"], False) == token  # toggle never rotates the token
    assert (await http.get(f"/api/public/talent-media/{token}")).status_code == 404
    mid = (await tdb.talents.find_one({"id": t["id"]}))["media"][0]["id"]
    assert (await http.get(f"/api/public/talent-media/{token}/file/{mid}")).status_code == 404
    assert (await http.get(f"/api/public/talent-media/{token}/download-all")).status_code == 404

    assert await _link(http, tdb, t["id"], True) == token  # same link
    assert (await http.get(f"/api/public/talent-media/{token}")).status_code == 200


async def test_missing_flag_defaults_to_on(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)  # no media_download_enabled field at all
    token = await tm.ensure_token(t["id"])
    assert (await http.get(f"/api/public/talent-media/{token}")).status_code == 200


# E — isolation
async def test_e_token_only_exposes_its_own_talent(env, cdn):
    tdb, http = env
    a = await _talent(tdb, cdn, name="Angela Kumar")
    b = await _talent(tdb, cdn, name="Fanny Gandhi")
    b["media"][0]["id"] = "b-only"
    await tdb.talents.update_one({"id": b["id"]}, {"$set": {"media.0.id": "b-only"}})
    ta, tb = await _link(http, tdb, a["id"]), await _link(http, tdb, b["id"])
    assert ta != tb
    ra = (await http.get(f"/api/public/talent-media/{ta}")).json()
    assert ra["talent"]["name"] == "Angela K"
    ids_a = [i["id"] for s in ra["sections"] for i in s["items"]]
    assert "b-only" not in ids_a
    # A's token cannot fetch B's media id
    assert (await http.get(f"/api/public/talent-media/{ta}/file/b-only")).status_code == 404
    assert (await http.get(f"/api/public/talent-media/{tb}/file/b-only")).status_code == 200


# F — invalid tokens
@pytest.mark.parametrize("bad", ["x", "A" * 32, "../../etc/passwd", "a" * 100, "%00" * 10])
async def test_f_invalid_token_404(env, bad):
    _, http = env
    assert (await http.get(f"/api/public/talent-media/{bad}")).status_code in (404, 405)


# G — live data, no snapshot
async def test_g_same_link_reflects_profile_changes(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    token = await _link(http, tdb, t["id"])
    first = (await http.get(f"/api/public/talent-media/{token}")).json()
    assert [len(s["items"]) for s in first["sections"]] == [2, 1, 1]

    await tdb.talents.update_one({"id": t["id"]}, {"$pull": {"media": {"id": "m-i1"}}})  # remove
    await tdb.talents.update_one({"id": t["id"]}, {"$push": {"media": _media(cdn, "western", "/w1.jpg", "m-w2")}})  # add
    await tdb.talents.update_one({"id": t["id"], "media.id": "m-v1"}, {"$set": {"media.$.id": "m-v2"}})  # replace video
    second = (await http.get(f"/api/public/talent-media/{token}")).json()
    assert [len(s["items"]) for s in second["sections"]] == [1, 2, 1]
    assert second["video"]["id"] == "m-v2"
    assert [i["id"] for i in second["sections"][0]["items"]] == ["m-i2"]


async def test_empty_categories_and_hidden_media(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    await tdb.talents.update_one({"id": t["id"]}, {"$set": {"media": [
        _media(cdn, "indian", "/i1.jpg", "shown"),
        _media(cdn, "indian", "/i2.jpg", "hidden1", client_visible=False),
        _media(cdn, "western", "/w1.jpg", "hidden2", internal_only=True),
    ]}})
    token = await _link(http, tdb, t["id"])
    body = (await http.get(f"/api/public/talent-media/{token}")).json()
    assert [len(s["items"]) for s in body["sections"]] == [1, 0, 0]
    assert body["video"] is None and body["total"] == 1


async def test_archived_talent_link_unavailable(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    token = await _link(http, tdb, t["id"])
    await tdb.talents.update_one({"id": t["id"]}, {"$set": {"status": "ARCHIVED"}})
    assert (await http.get(f"/api/public/talent-media/{token}")).status_code == 404


async def test_toggle_fields_not_writable_via_talent_form_models():
    # PUT /talents/{tid} ($set of TalentIn fields only) can therefore never
    # clobber the token or the flag.
    assert "media_download_enabled" not in core.TalentIn.model_fields
    assert "media_download_token" not in core.TalentIn.model_fields


# ---- notification (H, I, J) --------------------------------------------------
async def _submission(tdb, talent, project_id, **extra):
    sub = {
        "id": str(uuid.uuid4()), "project_id": project_id, "talent_id": talent["id"],
        "submitted_at": "2026-10-04T13:21:15.412447+00:00",
        "form_data": {"first_name": "Jasmine", "last_name": "Singh"},
        "original_form_data": {"first_name": "Jasmine", "last_name": "Singh", "age": 27},
    }
    sub.update(extra)
    return sub


async def _jobs(tdb):
    return await tdb.whatsapp_jobs.find({}, {"_id": 0}).sort("created_at", 1).to_list(50)


async def test_h_new_submission_form_then_exactly_one_media_message(env, cdn):
    tdb, http = env
    pid = str(uuid.uuid4())
    await tdb.projects.insert_one({"id": pid, "brand_name": "Amazon ad"})
    t = await _talent(tdb, cdn)
    sub = await _submission(tdb, t, pid)
    await sub_router._enqueue_internal_whatsapp_notification_task(sub, "NEW SUBMISSION")
    jobs = await _jobs(tdb)
    kinds = [j["template_id"] for j in jobs]
    assert kinds == ["internal_notification", "internal_notification_form", "internal_notification_media_link"]
    media = jobs[-1]
    token = (await tdb.talents.find_one({"id": t["id"]}))["media_download_token"]
    assert media["message_body"] == (
        "Talentgram X Jasmine S\n\nKindly click the link to download pictures and introduction video:\n\n"
        f"{tm.MEDIA_LINK_BASE_URL}/talent-media/{token}"
    )
    assert media["destination"] == jobs[0]["destination"]
    # worker sends in created_at order: media strictly after the form
    assert media["created_at"] > jobs[1]["created_at"]


async def test_i_toggle_off_sends_form_but_no_media_message(env, cdn):
    tdb, http = env
    pid = str(uuid.uuid4())
    await tdb.projects.insert_one({"id": pid, "brand_name": "Amazon ad"})
    t = await _talent(tdb, cdn, media_download_enabled=False)
    sub = await _submission(tdb, t, pid)
    await sub_router._enqueue_internal_whatsapp_notification_task(sub, "NEW SUBMISSION")
    kinds = [j["template_id"] for j in await _jobs(tdb)]
    assert kinds == ["internal_notification", "internal_notification_form"]


async def test_j_same_event_twice_does_not_duplicate_media_message(env, cdn):
    tdb, http = env
    pid = str(uuid.uuid4())
    await tdb.projects.insert_one({"id": pid, "brand_name": "Amazon ad"})
    t = await _talent(tdb, cdn)
    sub = await _submission(tdb, t, pid)
    g = dict(group_name="Talentgram Operations", project_id=pid, project_name="Amazon ad")
    results = await asyncio.gather(*[tm.enqueue_media_link_message(sub, "NEW SUBMISSION", **g) for _ in range(5)])
    assert results.count(True) == 1
    assert len([j for j in await _jobs(tdb) if j["template_id"] == "internal_notification_media_link"]) == 1
    # a genuine later resubmission (new updated_at) is a new event and does send
    sub2 = dict(sub, updated_at="2026-10-05T09:00:00+00:00")
    assert await tm.enqueue_media_link_message(sub2, "SUBMISSION UPDATED", **g) is True
    assert await tm.enqueue_media_link_message(sub2, "SUBMISSION UPDATED", **g) is False


async def test_media_failure_never_breaks_form_notification(env, cdn, monkeypatch):
    tdb, http = env
    pid = str(uuid.uuid4())
    await tdb.projects.insert_one({"id": pid, "brand_name": "Amazon ad"})
    t = await _talent(tdb, cdn)
    sub = await _submission(tdb, t, pid)

    async def boom(*a, **k):
        raise RuntimeError("media link exploded")

    monkeypatch.setattr(tm, "enqueue_media_link_message", boom)
    await sub_router._enqueue_internal_whatsapp_notification_task(sub, "NEW SUBMISSION")
    assert [j["template_id"] for j in await _jobs(tdb)] == ["internal_notification", "internal_notification_form"]


async def test_name_formatting():
    assert tm.short_talent_name("Angela Kumar") == "Angela K"
    assert tm.short_talent_name("Fanny Gandhi") == "Fanny G"
    assert tm.short_talent_name("Jasmine Singh") == "Jasmine S"
    assert tm.short_talent_name("Mary Ann smith") == "Mary Ann S"
    assert tm.short_talent_name("Cher") == "Cher"
    assert tm.short_talent_name("  ") == "Talent"


# ---- K: downloads ------------------------------------------------------------
async def test_k_individual_image_and_video_download(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    token = await _link(http, tdb, t["id"])
    r = await http.get(f"/api/public/talent-media/{token}/file/m-i2")
    assert r.status_code == 200 and r.content == _FILES["/i2.jpg"]
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;") and "Jasmine S - Indian Look Images - image-02.jpg" in cd
    assert "Singh" not in cd
    v = await http.get(f"/api/public/talent-media/{token}/file/m-v1")
    assert v.status_code == 200 and v.content == _FILES["/v1.mp4"]
    assert "introduction-video.mp4" in v.headers["content-disposition"]
    p = await http.get(f"/api/public/talent-media/{token}/file/m-p1")
    assert p.content == _FILES["/p1.png"] and p.headers["content-disposition"].count(".png") >= 1


async def test_k_download_all_zip_complete_structure_no_dupes(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    token = await _link(http, tdb, t["id"])
    r = await http.get(f"/api/public/talent-media/{token}/download-all")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "Jasmine S.zip" in r.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.testzip() is None
    names = zf.namelist()
    assert sorted(names) == sorted([
        "Jasmine S/Indian Look Images/image-01.jpg", "Jasmine S/Indian Look Images/image-02.jpg",
        "Jasmine S/Western Look Images/image-01.jpg", "Jasmine S/Additional Portfolio/image-01.png",
        "Jasmine S/Introduction Video/introduction-video.mp4",
    ])
    assert len(names) == len(set(names))
    assert zf.read("Jasmine S/Indian Look Images/image-02.jpg") == _FILES["/i2.jpg"]
    assert zf.read("Jasmine S/Introduction Video/introduction-video.mp4") == _FILES["/v1.mp4"]
    assert tm._zip_slots._value == tm._ZIP_MAX_CONCURRENT  # slot released


async def test_k_download_all_reflects_live_media(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    token = await _link(http, tdb, t["id"])
    await tdb.talents.update_one({"id": t["id"]}, {"$pull": {"media": {"category": "video"}}})
    zf = zipfile.ZipFile(io.BytesIO((await http.get(f"/api/public/talent-media/{token}/download-all")).content))
    assert not any("Introduction Video" in n for n in zf.namelist())


async def test_download_all_unreachable_upstream_is_clean_error_not_corrupt_zip(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    await tdb.talents.update_one({"id": t["id"]}, {"$set": {"media": [_media(cdn, "indian", "/nope.jpg", "gone")]}})
    token = await _link(http, tdb, t["id"])
    with pytest.raises(Exception):
        # entry fails mid-stream => the response is aborted (no EOCD ever sent)
        r = await http.get(f"/api/public/talent-media/{token}/download-all")
        zipfile.ZipFile(io.BytesIO(r.content))
        raise AssertionError("a failed entry must not produce a valid-looking archive")
    assert tm._zip_slots._value == tm._ZIP_MAX_CONCURRENT


async def test_download_all_busy_returns_503(env, cdn, monkeypatch):
    tdb, http = env
    t = await _talent(tdb, cdn)
    token = await _link(http, tdb, t["id"])
    monkeypatch.setattr(tm, "_zip_slots", asyncio.Semaphore(0))
    r = await http.get(f"/api/public/talent-media/{token}/download-all")
    assert r.status_code == 503 and r.headers.get("retry-after")


async def test_stream_zip_is_constant_memory():
    """A 60 MB entry must stream in <=64 KiB pieces with a tiny peak — never
    buffered. (The legacy client ZIP buffers everything; this must not.)"""
    total = 60 * 1024 * 1024

    def factory():
        async def gen():
            sent = 0
            piece = b"\0" * tm._CHUNK
            while sent < total:
                yield piece
                sent += len(piece)
        return gen

    tracemalloc.start()
    biggest, got = 0, 0
    sink = io.BytesIO()
    async for chunk in tm.stream_zip([("a/big.bin", factory())]):
        biggest = max(biggest, len(chunk))
        got += len(chunk)
        if got < 4096:  # keep only the head to prove the format; not the body
            sink.write(chunk)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert biggest <= tm._CHUNK
    assert got > total
    assert peak < 8 * 1024 * 1024, f"peak {peak} bytes — ZIP was buffered"


async def test_stream_zip_roundtrips_with_stdlib_reader():
    def mk(b):
        async def gen():
            for i in range(0, len(b), 7):
                yield b[i:i + 7]
        return lambda: gen()
    parts = [("Ü ñame/a.txt", b"hello" * 100), ("d/b.bin", bytes(range(256)) * 30), ("empty.txt", b"")]
    buf = b"".join([c async for c in tm.stream_zip([(n, mk(d)) for n, d in parts])])
    zf = zipfile.ZipFile(io.BytesIO(buf))
    assert zf.testzip() is None
    assert {n: zf.read(n) for n in zf.namelist()} == dict(parts)
