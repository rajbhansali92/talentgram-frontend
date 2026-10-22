"""Talent media links expire: first valid open starts one 48 h window; never extended; enforced on every endpoint.

Same harness as test_talent_media.py: real local Mongo (throwaway DB per test), the real router in-process, and a
real local HTTP server standing in for the media CDN. Expiry is simulated by moving the stored `expires_at` into the
past (the check compares against the real clock), never by weakening the check.
"""
import asyncio
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
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
_FILES = {"/i1.jpg": b"INDIAN-1" * 50, "/w1.jpg": b"WESTERN-1" * 50, "/v1.mp4": b"VIDEO" * 500}


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


@pytest_asyncio.fixture
async def env(monkeypatch):
    client = AsyncIOMotorClient(_MONGO_URL)
    dbname = f"tg_talent_media_exp_{uuid.uuid4().hex[:8]}"
    tdb = client[dbname]
    monkeypatch.setattr(tm, "db", tdb)
    monkeypatch.setattr(sub_router, "db", tdb)
    monkeypatch.setattr(tm, "_indexes_ready", False)
    monkeypatch.setattr(tm, "rate_limit_ok", lambda *a, **k: True)
    monkeypatch.delenv("TALENT_MEDIA_LINK_TTL_HOURS", raising=False)
    app = FastAPI()
    app.include_router(tm.router)
    app.dependency_overrides[core.current_team_or_admin] = lambda: {"email": "admin@x.co", "role": "admin"}
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    plain_app = FastAPI()                       # same router, NO auth override: admin routes must stay protected
    plain_app.include_router(tm.router)
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=plain_app), base_url="http://t")
    yield tdb, http, anon
    await http.aclose()
    await anon.aclose()
    await client.drop_database(dbname)
    client.close()


def _media(cdn, cat, path, mid):
    return {"id": mid, "category": cat, "url": f"{cdn}{path}", "content_type": "video/mp4" if cat == "video" else "image/jpeg",
            "resource_type": "video" if cat == "video" else "image", "created_at": "2026-09-29T10:00:00+00:00",
            **({"provider": "direct"} if cat == "video" else {})}


async def _talent(tdb, cdn, name="Jasmine Singh", **extra):
    doc = {"id": str(uuid.uuid4()), "name": name, "status": "ACTIVE",
           "media": [_media(cdn, "indian", "/i1.jpg", "m-i1"), _media(cdn, "western", "/w1.jpg", "m-w1"), _media(cdn, "video", "/v1.mp4", "m-v1")]}
    doc.update(extra)
    await tdb.talents.insert_one(dict(doc))
    return doc


async def _link_doc(tdb, token):
    return await tdb.talent_media_links.find_one({"token": token}, {"_id": 0})


async def _expire(tdb, token, ago=timedelta(minutes=1)):
    await tdb.talent_media_links.update_one({"token": token}, {"$set": {"expires_at": (datetime.now(timezone.utc) - ago).isoformat()}})


def _dt(s):
    return datetime.fromisoformat(s)


async def _all_paths(token):
    return [f"/api/public/talent-media/{token}", f"/api/public/talent-media/{token}/file/m-i1",
            f"/api/public/talent-media/{token}/file/m-v1", f"/api/public/talent-media/{token}/download-all"]


# 1 — first open activates and sets the expiry
async def test_first_open_activates_and_sets_48h_expiry(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    assert await _link_doc(tdb, token) is None or (await _link_doc(tdb, token))["activated_at"] is None   # issuing a link does not start the clock
    before = datetime.now(timezone.utc)
    assert (await http.get(f"/api/public/talent-media/{token}")).status_code == 200
    doc = await _link_doc(tdb, token)
    assert doc["activated_at"] and doc["expires_at"]
    assert before <= _dt(doc["activated_at"]) <= datetime.now(timezone.utc)
    assert _dt(doc["expires_at"]) - _dt(doc["activated_at"]) == timedelta(hours=48)


async def test_window_length_is_one_configurable_setting(env, cdn, monkeypatch):
    tdb, http, _ = env
    monkeypatch.setenv("TALENT_MEDIA_LINK_TTL_HOURS", "6")
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    await http.get(f"/api/public/talent-media/{token}")
    doc = await _link_doc(tdb, token)
    assert _dt(doc["expires_at"]) - _dt(doc["activated_at"]) == timedelta(hours=6)
    monkeypatch.setenv("TALENT_MEDIA_LINK_TTL_HOURS", "garbage")
    assert tm.link_ttl() == timedelta(hours=48)


# 2 — repeated opens (page, file, zip) never reset the expiry
async def test_repeated_opens_and_downloads_never_move_the_expiry(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    await http.get(f"/api/public/talent-media/{token}")
    first = await _link_doc(tdb, token)
    for path in await _all_paths(token):
        assert (await http.get(path)).status_code == 200, path
        assert (await http.get(path)).status_code == 200, path
    again = await _link_doc(tdb, token)
    assert (again["activated_at"], again["expires_at"]) == (first["activated_at"], first["expires_at"])


async def test_reopening_the_same_url_after_expiry_does_not_start_a_new_session(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    await http.get(f"/api/public/talent-media/{token}")
    await _expire(tdb, token)
    stamp = (await _link_doc(tdb, token))["expires_at"]
    for _ in range(3):
        assert (await http.get(f"/api/public/talent-media/{token}")).status_code == 410
    assert (await _link_doc(tdb, token))["expires_at"] == stamp


# 3 — simultaneous first opens converge on ONE window
async def test_concurrent_first_opens_cannot_extend_or_recreate_the_window(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    rs = await asyncio.gather(*[http.get(f"/api/public/talent-media/{token}") for _ in range(25)])
    assert [r.status_code for r in rs] == [200] * 25
    assert await tdb.talent_media_links.count_documents({"token": token}) == 1
    doc = await _link_doc(tdb, token)
    assert _dt(doc["expires_at"]) - _dt(doc["activated_at"]) == timedelta(hours=48)
    await asyncio.sleep(0.05)
    await asyncio.gather(*[http.get(f"/api/public/talent-media/{token}") for _ in range(10)])
    assert (await _link_doc(tdb, token)) == doc


async def test_concurrent_activation_writes_exactly_one_window(env, cdn):
    tdb, _, _ = env
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    link = await tm._link_doc(token, t["id"])
    got = await asyncio.gather(*[tm._activate(dict(link)) for _ in range(30)])
    assert len({(g["activated_at"], g["expires_at"]) for g in got}) == 1
    assert await tdb.talent_media_links.count_documents({"token": token}) == 1


# 4 — downloads work inside the window, and one download does not spend the others
async def test_image_video_and_download_all_work_inside_the_window(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    assert (await http.get(f"/api/public/talent-media/{token}")).status_code == 200
    img = await http.get(f"/api/public/talent-media/{token}/file/m-i1")
    assert img.status_code == 200 and img.content == _FILES["/i1.jpg"]
    assert "attachment" in img.headers["content-disposition"] and "image-01.jpg" in img.headers["content-disposition"]
    vid = await http.get(f"/api/public/talent-media/{token}/file/m-v1")
    assert vid.status_code == 200 and vid.content == _FILES["/v1.mp4"] and "introduction-video.mp4" in vid.headers["content-disposition"]
    zp = await http.get(f"/api/public/talent-media/{token}/download-all")
    assert zp.status_code == 200 and zp.headers["content-type"] == "application/zip"
    assert (await http.get(f"/api/public/talent-media/{token}/file/m-i1")).status_code == 200      # still usable after the others


# 5 — every path rejects an expired link on the server
async def test_every_endpoint_rejects_an_expired_link(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    await http.get(f"/api/public/talent-media/{token}")
    await _expire(tdb, token)
    for path in await _all_paths(token):
        r = await http.get(path)
        assert r.status_code == 410, path
        assert r.json() == {"detail": "This media link has expired."}
        assert "Jasmine" not in r.text and token not in r.text and "http" not in r.text      # nothing about the talent, media or URLs


async def test_expired_download_all_does_not_touch_the_cdn_or_a_zip_slot(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    await http.get(f"/api/public/talent-media/{token}")
    await _expire(tdb, token)
    free = tm._zip_slots._value
    assert (await http.get(f"/api/public/talent-media/{token}/download-all")).status_code == 410
    assert tm._zip_slots._value == free


async def test_unparseable_expiry_fails_closed(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    await http.get(f"/api/public/talent-media/{token}")
    await tdb.talent_media_links.update_one({"token": token}, {"$set": {"expires_at": "not-a-date"}})
    assert (await http.get(f"/api/public/talent-media/{token}")).status_code == 410


# 6 — invalid / revoked tokens are indistinguishable and reveal nothing
async def test_invalid_unknown_and_revoked_links_all_give_the_same_404(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    await http.get(f"/api/public/talent-media/{token}")
    assert (await http.patch(f"/api/talents/{t['id']}/media-download", json={"enabled": False})).status_code == 200      # revoke
    bodies = set()
    for path in [f"/api/public/talent-media/{token}", f"/api/public/talent-media/{token}/file/m-i1", f"/api/public/talent-media/{token}/download-all",
                 f"/api/public/talent-media/{'Z' * 32}", "/api/public/talent-media/short"]:
        r = await http.get(path)
        assert r.status_code == 404, path
        bodies.add(r.text)
    assert len(bodies) == 1
    await _expire(tdb, token)                                  # revoked wins over expired: still the plain 404, not "expired"
    assert (await http.get(f"/api/public/talent-media/{token}")).status_code == 404


async def test_a_disabled_link_does_not_start_its_clock(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn, media_download_enabled=False)
    token = await tm.ensure_token(t["id"])
    assert (await http.get(f"/api/public/talent-media/{token}")).status_code == 404
    doc = await _link_doc(tdb, token)
    assert doc is None or doc["activated_at"] is None


# 7 — expiry never touches the media
async def test_expiry_does_not_delete_or_modify_original_media(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    await http.get(f"/api/public/talent-media/{token}")
    snapshot = (await tdb.talents.find_one({"id": t["id"]}, {"_id": 0, "media": 1, "name": 1}))
    await _expire(tdb, token)
    for path in await _all_paths(token):
        await http.get(path)
    assert (await tdb.talents.find_one({"id": t["id"]}, {"_id": 0, "media": 1, "name": 1})) == snapshot


# 8 — a fresh link after expiry (never a revived one)
async def test_a_fresh_link_is_minted_after_expiry_and_the_old_one_stays_dead(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    old = await tm.ensure_token(t["id"])
    await http.get(f"/api/public/talent-media/{old}")
    assert await tm.ensure_token(t["id"]) == old                       # still inside its window: the same link, no churn
    await _expire(tdb, old)

    r = await http.post(f"/api/talents/{t['id']}/media-download/link")
    assert r.status_code == 200
    new = r.json()["url"].rsplit("/", 1)[1]
    assert new != old and len(new) >= 30
    assert (await http.get(f"/api/public/talent-media/{old}")).status_code == 410            # not revived
    assert (await _link_doc(tdb, old))["activated_at"] is not None
    assert (await http.get(f"/api/public/talent-media/{new}")).status_code == 200            # new link, its own full window
    nd = await _link_doc(tdb, new)
    assert _dt(nd["expires_at"]) - _dt(nd["activated_at"]) == timedelta(hours=48)
    assert (await http.post(f"/api/talents/{t['id']}/media-download/link")).json()["url"].endswith(new)      # and it is now the current one


async def test_concurrent_requests_for_a_fresh_link_converge_on_one_token(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    old = await tm.ensure_token(t["id"])
    await http.get(f"/api/public/talent-media/{old}")
    await _expire(tdb, old)
    toks = await asyncio.gather(*[tm.ensure_token(t["id"]) for _ in range(15)])
    assert len(set(toks)) == 1 and toks[0] != old


async def test_toggling_off_and_on_does_not_revive_an_expired_link(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    old = await tm.ensure_token(t["id"])
    await http.get(f"/api/public/talent-media/{old}")
    await _expire(tdb, old)
    await http.patch(f"/api/talents/{t['id']}/media-download", json={"enabled": False})
    await http.patch(f"/api/talents/{t['id']}/media-download", json={"enabled": True})
    assert (await http.get(f"/api/public/talent-media/{old}")).status_code == 410


# links issued before expiry existed (token on the talent doc, no link state yet)
async def test_a_link_sent_before_expiry_existed_activates_on_first_open_then_expires(env, cdn):
    tdb, http, _ = env
    legacy = "LegacyToken" + "a" * 21
    t = await _talent(tdb, cdn, media_download_token=legacy)
    assert await tdb.talent_media_links.count_documents({}) == 0
    assert (await http.get(f"/api/public/talent-media/{legacy}")).status_code == 200
    doc = await _link_doc(tdb, legacy)
    assert doc["talent_id"] == t["id"] and _dt(doc["expires_at"]) - _dt(doc["activated_at"]) == timedelta(hours=48)
    await _expire(tdb, legacy)
    assert (await http.get(f"/api/public/talent-media/{legacy}")).status_code == 410
    assert await tm.ensure_token(t["id"]) != legacy


# 9 — Personal Assistant / callers
async def test_assistant_peek_and_resolve_follow_expiry(env, cdn, monkeypatch):
    from personal_assistant import resolve as R
    tdb, http, _ = env
    monkeypatch.setattr(R, "db", tdb)
    t = await _talent(tdb, cdn)
    assert (await R.peek_media_link(t["id"]))["state"] == "will_create"
    name, url = await tm.resolve_media_link(t["id"])
    assert name == "Jasmine Singh" and url.startswith(tm.MEDIA_LINK_BASE_URL + "/talent-media/")
    pk = await R.peek_media_link(t["id"])
    assert pk == {"state": "ready", "url": url}
    token = url.rsplit("/", 1)[1]
    await http.get(f"/api/public/talent-media/{token}")
    assert (await R.peek_media_link(t["id"]))["url"] == url              # inside its window: same link
    await _expire(tdb, token)
    assert (await R.peek_media_link(t["id"]))["state"] == "will_create"  # expired: the preview no longer promises that URL
    _, fresh = await tm.resolve_media_link(t["id"])
    assert fresh != url
    await tdb.talents.update_one({"id": t["id"]}, {"$set": {"media_download_enabled": False}})
    assert (await tm.resolve_media_link(t["id"]))[1] is None
    assert (await R.peek_media_link(t["id"]))["state"] == "off"


async def test_whatsapp_notification_link_is_fresh_after_expiry(env, cdn):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    old = await tm.ensure_token(t["id"])
    await http.get(f"/api/public/talent-media/{old}")
    await _expire(tdb, old)
    sub = {"id": "s1", "talent_id": t["id"], "submitted_at": "2026-10-22T10:00:00+00:00"}
    assert await tm.enqueue_media_link_message(sub, "NEW SUBMISSION", group_name="G", project_id="p", project_name="P") is True
    job = await tdb.whatsapp_jobs.find_one({}, {"_id": 0})
    assert old not in job["message_body"] and "/talent-media/" in job["message_body"]


# 10 — authorization + token privacy
async def test_admin_link_routes_still_require_auth(env, cdn):
    tdb, _, anon = env
    t = await _talent(tdb, cdn)
    assert (await anon.post(f"/api/talents/{t['id']}/media-download/link")).status_code in (401, 403)
    assert (await anon.patch(f"/api/talents/{t['id']}/media-download", json={"enabled": False})).status_code in (401, 403)
    assert await tdb.talent_media_links.count_documents({}) == 0 and "media_download_token" not in (await tdb.talents.find_one({"id": t["id"]}))


async def test_full_token_is_never_echoed_or_logged_by_the_app(env, cdn, caplog):
    tdb, http, _ = env
    t = await _talent(tdb, cdn)
    token = await tm.ensure_token(t["id"])
    caplog.set_level(logging.DEBUG, logger="talentgram")
    for phase in ("active", "expired"):
        for path in await _all_paths(token):
            assert token not in (await http.get(path)).text
        await _expire(tdb, token)
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("talentgram")]       # the app's own loggers (the test client's httpx log is not the server's)
    assert ours and all(token not in m for m in ours)


async def test_access_log_masks_the_token_in_the_request_path():
    token = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", f"/api/public/talent-media/{token}/file/m-i1", "1.1", 200), None)
    logger = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, tm._MaskMediaTokens) for f in logger.filters)             # installed on the real access logger
    assert all(f.filter(rec) for f in logger.filters)
    out = rec.getMessage()
    assert token not in out and "/talent-media/AbCd…/file/m-i1" in out
    plain = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s"', ("1.2.3.4", "GET", "/api/projects"), None)
    tm._MaskMediaTokens().filter(plain)
    assert plain.getMessage() == '1.2.3.4 - "GET /api/projects"'
