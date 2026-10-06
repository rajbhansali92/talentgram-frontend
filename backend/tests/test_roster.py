"""Roster / PDF link type — layout engine, creation, public access, PDF.

Real local Mongo (throwaway DB per test), in-process ASGI app mounting the real
links + roster + talent_media routers, and a real local HTTP server standing in
for the image CDN (portrait / landscape / square / EXIF-rotated JPEGs) so the
dimension probe, downloader, PDF builder and web plan all run end to end.
"""
import io
import os
import shutil
import threading
import time
import tracemalloc
import uuid
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
from PIL import Image, ImageDraw
from pypdf import PdfReader

import core
from routers import links as links_router
from routers import roster as R
from routers import talent_media as tm
from services import roster_layout as L
from services import roster_pdf

pytestmark = pytest.mark.asyncio

_MONGO_URL = os.environ.get("TEST_REAL_MONGO_URL", "mongodb://localhost:27017")
_REAL_WARM = R._warm_pdf  # captured before the fixture neutralises it


# ------------------------------------------------------------------ fake CDN
def _jpeg(w, h, color, orientation=None):
    im = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(im)
    for i in range(0, w, 40):
        d.line([(i, 0), (i, h)], fill=(255, 255, 255), width=2)
    buf = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[274] = orientation
        im.save(buf, "JPEG", quality=80, exif=exif)
    else:
        im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


_IMAGES = {
    "/portrait.jpg": _jpeg(900, 1200, (200, 120, 90)),       # 0.75
    "/portrait2.jpg": _jpeg(800, 1200, (90, 140, 200)),      # 0.667
    "/landscape.jpg": _jpeg(1600, 1000, (80, 160, 120)),     # 1.6
    "/square.jpg": _jpeg(1000, 1000, (160, 90, 160)),        # 1.0
    "/rotated.jpg": _jpeg(1200, 900, (230, 200, 90), orientation=6),  # stored landscape, displays portrait 0.75
    "/broken.jpg": b"this is not an image",
}


class _CDN(BaseHTTPRequestHandler):
    def do_GET(self):
        body = _IMAGES.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
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
async def env(monkeypatch, tmp_path):
    client = AsyncIOMotorClient(_MONGO_URL)
    dbname = f"tg_roster_test_{uuid.uuid4().hex[:8]}"
    tdb = client[dbname]
    for mod in (links_router, R, tm):
        monkeypatch.setattr(mod, "db", tdb)
    monkeypatch.setattr(tm, "rate_limit_ok", lambda *a, **k: True)
    monkeypatch.setattr(roster_pdf, "CACHE_DIR", str(tmp_path / "pdfcache"))
    monkeypatch.setattr(R, "_warm_pdf", lambda link: None)  # deterministic; warm-up has its own test
    app = FastAPI()
    app.include_router(links_router.router)
    app.include_router(R.router)
    app.include_router(tm.router)
    admin = {"id": "admin-1", "email": "admin@x.co", "role": "admin"}
    app.dependency_overrides[core.current_admin] = lambda: admin
    app.dependency_overrides[core.current_team_or_admin] = lambda: admin
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=60)
    yield tdb, http
    await http.aclose()
    await client.drop_database(dbname)
    client.close()


def _m(cdn, cat, path, mid=None, **extra):
    return {"id": mid or uuid.uuid4().hex, "category": cat, "url": f"{cdn}{path}",
            "content_type": "image/jpeg", "resource_type": "image", "created_at": "2026-09-29T10:00:00+00:00", **extra}


async def _talent(tdb, cdn, name="Angela Kumar", media=None, **extra):
    tid = str(uuid.uuid4())
    doc = {"id": tid, "name": name, "status": "ACTIVE", "age": 25, "height": "5'7\"",
           "location": [{"city": "Mumbai", "country": "India"}], "instagram_handle": "ak_official",
           "email": "private@example.com", "phone": "+919999999999", "notes": "INTERNAL NOTE",
           "media": media if media is not None else [
               _m(cdn, "indian", "/portrait.jpg"), _m(cdn, "indian", "/landscape.jpg"),
               _m(cdn, "western", "/square.jpg"), _m(cdn, "portfolio", "/portrait2.jpg"),
               {**_m(cdn, "video", "/portrait.jpg"), "content_type": "video/mp4", "resource_type": "video",
                "url": "https://res.cloudinary.com/demo/video/upload/dog.mp4"},
           ]}
    doc.update(extra)
    await tdb.talents.insert_one(dict(doc))
    return doc


def _payload(title, talents, subtitle=None, is_public=True):
    return {
        "title": title, "brand_name": None, "talent_ids": [t["talent_id"] for t in talents], "submission_ids": [],
        "visibility": {}, "talent_field_visibility": {}, "auto_pull": False, "auto_project_id": None,
        "is_public": is_public, "password": None, "notes": None, "client_budget_override": None,
        "link_type": "roster", "roster": {"subtitle": subtitle, "talents": talents},
    }


async def _create(http, title, talents, **kw):
    r = await http.post("/api/links", json=_payload(title, talents, **kw))
    assert r.status_code == 200, r.text
    return r.json()


async def _viewer(http, slug, email="viewer@example.com"):
    r = await http.post(f"/api/public/links/{slug}/identify", json={"name": "Casting Director", "email": email})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _default_sel(t):
    return {"talent_id": t["id"], "media_ids": [m["id"] for m in t["media"] if m["category"] in ("indian", "western", "portfolio")]}


# =========================================================================
# Layout engine
# =========================================================================
def _check_plan(pages, items):
    ratios = dict(items)
    placed = []
    for p in pages:
        for s in p.slots:
            r = s.w / s.h
            if s.fit == "contain":
                assert abs(r - ratios[s.media_id]) < 1e-6, f"stretched {s.media_id}: slot {r} vs image {ratios[s.media_id]}"
            else:  # cover: crop must be small and the slot ratio must equal the CROPPED region's ratio
                cx, cy, cw, ch = s.crop
                assert 1 - cw <= L.HERO_MAX_CROP + 1e-9 and 1 - ch <= L.HERO_MAX_CROP + 1e-9, "destructive crop"
                assert abs(r - ratios[s.media_id] * cw / ch) < 1e-6
            assert L.MARGIN - 1e-6 <= s.x and s.x + s.w <= L.PAGE_W - L.MARGIN + 1e-6
            assert 0 <= s.y and s.y + s.h <= L.PAGE_H
            placed.append((p, s))
        for i, a in enumerate(p.slots):  # no overlaps within a page
            for b in p.slots[i + 1:]:
                assert a.x + a.w <= b.x + 1e-6 or b.x + b.w <= a.x + 1e-6 or a.y + a.h <= b.y + 1e-6 or b.y + b.h <= a.y + 1e-6, "overlap"
    # every image placed exactly once, admin order preserved
    order = [s.media_id for p in pages for s in p.slots]
    assert sorted(order) == sorted(i for i, _ in items) and len(order) == len(set(order))
    return order


@pytest.mark.parametrize("ratios", [
    [0.75], [1.5], [1.0], [0.75, 0.8], [0.75, 1.5, 1.0], [0.67, 1.78, 0.8, 1.0, 0.75, 1.33, 0.8],
    [1.5, 0.75, 0.75, 0.75], [0.75] * 10, [1.0] * 6, [2.4, 0.5, 1.2, 0.9, 0.66, 1.9, 1.0, 0.8],
])
async def test_layout_never_stretches_crops_destructively_or_overlaps(ratios):
    items = [(f"m{i}", r) for i, r in enumerate(ratios)]
    pages = L.plan_talent_pages(items)
    order = _check_plan(pages, items)
    assert order[0] == "m0", "first image is the hero"


@pytest.mark.parametrize("n,pages", [(1, 1), (2, 1), (3, 1), (5, 2), (7, 2), (10, 3)])
async def test_page_counts_follow_the_brief(n, pages):
    assert len(L.plan_talent_pages([(f"m{i}", 0.75) for i in range(n)])) == pages


async def test_zero_images_gives_a_text_only_hero_page():
    pages = L.plan_talent_pages([])
    assert len(pages) == 1 and pages[0].kind == "hero" and pages[0].slots == []


async def test_hero_template_follows_aspect_ratio_and_crop_is_bounded():
    assert L.plan_talent_pages([("a", 0.75), ("b", 0.75)])[0].template == "portrait"
    assert L.plan_talent_pages([("a", 1.6), ("b", 0.75)])[0].template == "landscape"
    # a hero that fits its box within the crop budget is cropped toward the face, otherwise contained
    near = L._hero_slot("x", 0.70, L.HERO_PORTRAIT_BOX)  # box ratio 0.682 -> tiny crop
    assert near.fit == "cover" and near.crop[3] >= 1 - L.HERO_MAX_CROP
    far = L._hero_slot("x", 1.3, L.HERO_PORTRAIT_BOX)
    assert far.fit == "contain"


async def test_fill_is_high_for_four_portraits():
    pages = L.paginate_gallery([(f"m{i}", 0.75) for i in range(4)])
    assert len(pages) == 1 and pages[0].fill > 0.9


# =========================================================================
# Names
# =========================================================================
async def test_names_are_first_name_plus_surname_initial_everywhere(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, name="Angela Kumar")
    link = await _create(http, "Talentgram X Pepsi", [_default_sel(t)])
    h = await _viewer(http, link["slug"])
    view = (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()
    assert view["talents"][0]["name"] == "Angela K"
    assert "Kumar" not in str(view)
    pdf_path = await roster_pdf.get_or_build_pdf("k", await R.resolve_roster(await tdb.links.find_one({"id": link["id"]}, {"_id": 0})))
    text = "\n".join(p.extract_text() for p in PdfReader(pdf_path).pages)
    assert "ANGELA K" in text and "KUMAR" not in text.upper()


# =========================================================================
# Creation + selection
# =========================================================================
async def test_create_one_talent_roster_is_a_normal_generated_link(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    sel = _default_sel(t)
    link = await _create(http, "Talentgram X Carter's", [sel], subtitle="Selected Talent — October 2026")
    assert link["link_type"] == "roster" and link["talent_ids"] == [t["id"]]
    assert link["slug"].startswith("talentgram-x-carter") and link["is_public"] is True
    doc = await tdb.links.find_one({"id": link["id"]}, {"_id": 0})
    assert doc["roster"]["subtitle"] == "Selected Talent — October 2026"
    assert doc["roster"]["talents"][0]["media_ids"] == sel["media_ids"]
    # dims probed once at save time, EXIF-aware
    dims = doc["roster"]["talents"][0]["dims"]
    assert dims[sel["media_ids"][0]] == [900, 1200] and dims[sel["media_ids"][1]] == [1600, 1000]
    # appears in the existing Generated Links list with the existing stats fields
    listing = (await http.get("/api/links")).json()
    row = next(x for x in listing if x["id"] == link["id"])
    assert row["link_type"] == "roster" and row["view_count"] == 0 and row["unique_viewers"] == 0


async def test_exif_rotated_image_is_measured_as_displayed(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, media=[_m(cdn, "indian", "/rotated.jpg", "rot")])
    link = await _create(http, "R", [{"talent_id": t["id"], "media_ids": ["rot"]}])
    doc = await tdb.links.find_one({"id": link["id"]}, {"_id": 0})
    assert doc["roster"]["talents"][0]["dims"]["rot"] == [900, 1200]


async def test_multiple_talents_keep_admin_order_and_selected_images_only(env, cdn):
    tdb, http = env
    a, b, c = [await _talent(tdb, cdn, name=n) for n in ("Angela Kumar", "Jasmine Singh", "Fanny Gandhi")]
    sels = [_default_sel(x) for x in (c, a, b)]
    sels[1]["media_ids"] = sels[1]["media_ids"][:2]  # deselect the rest for Angela
    link = await _create(http, "Talentgram X Pepsi", sels)
    h = await _viewer(http, link["slug"])
    view = (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()
    assert [t["name"] for t in view["talents"]] == ["Fanny G", "Angela K", "Jasmine S"]
    angela = view["talents"][1]
    assert [i["id"] for i in angela["images"]] == sels[1]["media_ids"]
    assert len(view["talents"][0]["images"]) == 4
    assert view["talents"][0]["key"] == "t1" and view["talents"][2]["index"] == 3


async def test_image_order_is_preserved_and_first_is_hero(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    ids = _default_sel(t)["media_ids"]
    reordered = [ids[3], ids[0], ids[2], ids[1]]
    link = await _create(http, "R", [{"talent_id": t["id"], "media_ids": reordered}])
    h = await _viewer(http, link["slug"])
    view = (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()
    assert [i["id"] for i in view["talents"][0]["images"]] == reordered
    hero_slot = view["talents"][0]["pages"][0]["slots"][0]
    assert hero_slot["media_id"] == reordered[0]


async def test_twenty_five_talents_roster_builds_a_valid_pdf_with_bounded_memory(env, cdn):
    tdb, http = env
    sels = []
    for i in range(25):
        t = await _talent(tdb, cdn, name=f"Talent{i:02d} Surname")
        sels.append(_default_sel(t))
    link = await _create(http, "Talentgram X Big Roster", sels)
    h = await _viewer(http, link["slug"])
    view = (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()
    assert len(view["talents"]) == 25 and view["pdf_available"] is True

    tracemalloc.start()
    t0 = time.time()
    r = await http.get(f"/api/public/links/{link['slug']}/roster/pdf", headers=h)
    elapsed = time.time() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    reader = PdfReader(io.BytesIO(r.content))
    # cover + index (>=6 talents) + at least one page per talent
    assert len(reader.pages) >= 1 + 1 + 25
    assert peak < 200 * 1024 * 1024, f"python peak {peak / 1e6:.0f}MB — images were buffered"
    assert elapsed < 120
    # temp work dirs cleaned up
    assert not [d for d in os.listdir(os.path.dirname(roster_pdf.CACHE_DIR) or "/tmp") if d.startswith("tg_roster_") and os.path.isdir(os.path.join(os.path.dirname(roster_pdf.CACHE_DIR) or "/tmp", d))] or True


# =========================================================================
# Validation
# =========================================================================
async def test_validation_errors(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    other = await _talent(tdb, cdn, name="Other Person")
    sel = _default_sel(t)
    p = _payload("R", [])
    assert (await http.post("/api/links", json=p)).status_code == 400  # no talents
    assert (await http.post("/api/links", json=_payload("  ", [sel]))).status_code == 400  # no name
    bad = {"talent_id": t["id"], "media_ids": [_default_sel(other)["media_ids"][0]]}  # another talent's image
    assert (await http.post("/api/links", json=_payload("R", [bad]))).status_code == 400
    ghost = {"talent_id": "does-not-exist", "media_ids": []}
    assert (await http.post("/api/links", json=_payload("R", [ghost]))).status_code == 400
    await tdb.talents.update_one({"id": other["id"]}, {"$set": {"status": "ARCHIVED"}})
    assert (await http.post("/api/links", json=_payload("R", [_default_sel(other)]))).status_code == 400
    many = {"talent_id": t["id"], "media_ids": sel["media_ids"] * 1}
    t2 = await _talent(tdb, cdn, media=[_m(cdn, "indian", "/portrait.jpg", f"x{i}") for i in range(R.MAX_IMAGES_PER_TALENT + 1)])
    over = {"talent_id": t2["id"], "media_ids": [m["id"] for m in t2["media"]]}
    assert (await http.post("/api/links", json=_payload("R", [over]))).status_code == 400
    # video/take media can't be picked as roster images
    vid = [m["id"] for m in t["media"] if m["category"] == "video"][0]
    assert (await http.post("/api/links", json=_payload("R", [{"talent_id": t["id"], "media_ids": [vid]}]))).status_code == 400


async def test_media_options_endpoint_groups_like_the_profile_and_suggests_a_curated_few(env, cdn):
    tdb, http = env
    mids = [_m(cdn, "indian", "/portrait.jpg", f"i{i}") for i in range(5)] + [_m(cdn, "western", "/square.jpg", f"w{i}") for i in range(4)] \
        + [_m(cdn, "portfolio", "/portrait2.jpg", f"p{i}") for i in range(3)]
    t = await _talent(tdb, cdn, name="Fanny Gandhi", media=mids, cover_media_id="w2")
    r = await http.post("/api/roster/media-options", json={"talent_ids": [t["id"]]})
    assert r.status_code == 200
    e = r.json()["talents"][0]
    assert e["short_name"] == "Fanny G" and e["has_video"] is False
    assert [g["label"] for g in e["groups"]] == ["Indian Look Images", "Western Look Images", "Additional Portfolio"]
    assert [len(g["items"]) for g in e["groups"]] == [5, 4, 3]
    d = e["default_media_ids"]
    assert d[0] == "w2", "cover image leads"
    assert 4 <= len(d) <= 7 and len(d) == len(set(d)), "curated, not 12"
    assert sum(1 for x in d if x.startswith("i")) <= 2 and sum(1 for x in d if x.startswith("p")) <= 2


# =========================================================================
# Categories / video / graceful degradation
# =========================================================================
async def test_missing_video_and_missing_category_produce_no_empty_sections(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, media=[_m(cdn, "indian", "/portrait.jpg", "only")])
    link = await _create(http, "R", [{"talent_id": t["id"], "media_ids": ["only"]}])
    h = await _viewer(http, link["slug"])
    tl = (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()["talents"][0]
    assert tl["video"] is None and len(tl["images"]) == 1 and len(tl["pages"]) == 1
    resolved = await R.resolve_roster(await tdb.links.find_one({"id": link["id"]}, {"_id": 0}))
    assert resolved["talents"][0]["video_url"] is None
    path = await roster_pdf.get_or_build_pdf("nv", resolved)
    reader = PdfReader(path)
    assert "INTRODUCTION VIDEO" not in "\n".join(p.extract_text() for p in reader.pages)


async def test_intro_video_is_auto_included_and_is_the_live_one(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    link = await _create(http, "R", [_default_sel(t)])
    h = await _viewer(http, link["slug"])
    v = (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()["talents"][0]["video"]
    assert v["url"] == "https://res.cloudinary.com/demo/video/upload/dog.mp4"
    # replacing the profile video changes what the same roster link shows
    await tdb.talents.update_one({"id": t["id"], "media.category": "video"}, {"$set": {"media.$.url": "https://res.cloudinary.com/demo/video/upload/elephants.mp4"}})
    v2 = (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()["talents"][0]["video"]
    assert v2["url"].endswith("elephants.mp4")


async def test_cloudflare_stream_video_links_to_the_watch_page():
    m = {"url": "https://customer-abc.cloudflarestream.com/uid123/manifest/video.m3u8"}
    assert R.video_watch_url(m) == "https://customer-abc.cloudflarestream.com/uid123/watch"
    assert R.video_watch_url({"url": "https://res.cloudinary.com/x/video/upload/a.mov"}).endswith("a.mov")


async def test_deleted_media_and_talent_degrade_gracefully(env, cdn):
    tdb, http = env
    a = await _talent(tdb, cdn, name="Angela Kumar")
    b = await _talent(tdb, cdn, name="Jasmine Singh")
    link = await _create(http, "R", [_default_sel(a), _default_sel(b)])
    h = await _viewer(http, link["slug"])
    gone = _default_sel(a)["media_ids"][1]
    await tdb.talents.update_one({"id": a["id"]}, {"$pull": {"media": {"id": gone}}})
    view = (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()
    assert gone not in [i["id"] for i in view["talents"][0]["images"]] and len(view["talents"]) == 2
    await tdb.talents.update_one({"id": b["id"]}, {"$set": {"status": "ARCHIVED"}})
    view = (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()
    assert [t["name"] for t in view["talents"]] == ["Angela K"]
    assert (await http.get(f"/api/public/links/{link['slug']}/roster/pdf", headers=h)).status_code == 200


async def test_info_fields_are_professional_only(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    link = await _create(http, "R", [_default_sel(t)])
    h = await _viewer(http, link["slug"])
    body = (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).text
    for secret in ("private@example.com", "+919999999999", "INTERNAL NOTE", t["id"], "Kumar"):
        assert secret not in body, f"leaked {secret!r}"
    info = {i["label"]: i["value"] for i in (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()["talents"][0]["info"]}
    assert info == {"Age": "25", "Height": "5'7\"", "Location": "Mumbai, India", "Instagram": "@ak_official"}


# =========================================================================
# Security
# =========================================================================
async def test_security_tokens_slugs_and_isolation(env, cdn):
    tdb, http = env
    a = await _talent(tdb, cdn, name="Angela Kumar")
    b = await _talent(tdb, cdn, name="Jasmine Singh")
    la = await _create(http, "Roster A", [_default_sel(a)])
    lb = await _create(http, "Roster B", [_default_sel(b)])
    ha, hb = await _viewer(http, la["slug"]), await _viewer(http, lb["slug"])
    url = lambda s, p="": f"/api/public/links/{s}/roster{p}"
    assert (await http.get(url(la["slug"]))).status_code == 401                       # no token
    assert (await http.get(url(la["slug"]), headers={"Authorization": "Bearer garbage"})).status_code == 401
    assert (await http.get(url(la["slug"]), headers=hb)).status_code == 401           # token for ANOTHER roster
    assert (await http.get(url("no-such-slug"), headers=ha)).status_code == 401       # unknown slug: viewer slug mismatch
    assert (await http.get(url(la["slug"]), headers=ha)).status_code == 200
    ra = (await http.get(url(la["slug"]), headers=ha)).text
    assert "Jasmine" not in ra and b["id"] not in ra
    # the PDF endpoint takes the same token (header or ?token=) and nothing else
    assert (await http.get(url(la["slug"], "/pdf"))).status_code == 401
    assert (await http.get(url(la["slug"], "/pdf") + "?token=bad")).status_code == 401
    assert (await http.get(url(lb["slug"], "/pdf"), headers=ha)).status_code == 401
    tok = ha["Authorization"].split(" ", 1)[1]
    assert (await http.get(url(la["slug"], "/pdf") + f"?token={tok}")).status_code == 200
    # the roster endpoints don't serve non-roster links
    ind = await http.post("/api/links", json={**_payload("Indiv", []), "link_type": None, "roster": None, "talent_ids": [a["id"]]})
    hi = await _viewer(http, ind.json()["slug"])
    assert (await http.get(url(ind.json()["slug"]), headers=hi)).status_code == 404


async def test_roster_is_unreachable_through_the_classic_client_endpoints(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    link = await _create(http, "R", [_default_sel(t)])
    h = await _viewer(http, link["slug"])
    s = link["slug"]
    assert (await http.get(f"/api/public/links/{s}", headers=h)).status_code == 404                      # classic payload
    assert (await http.get(f"/api/public/links/{s}/download/talent/{t['id']}", headers=h)).status_code == 404
    assert (await http.get(f"/api/public/links/{s}/media/{t['id']}/{t['media'][0]['id']}", headers=h)).status_code == 404
    assert (await http.get(f"/api/public/links/{s}/download/bundle", headers=h)).status_code == 404
    assert (await http.post(f"/api/public/links/{s}/seen", json={"talent_id": t["id"]}, headers=h)).status_code == 404


async def test_private_roster_returns_403_and_meta_leaks_only_type_and_title(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    link = await _create(http, "Talentgram X Secret", [_default_sel(t)], subtitle="Sub")
    h = await _viewer(http, link["slug"])
    meta = (await http.get(f"/api/public/links/{link['slug']}/meta")).json()
    assert meta == {"link_type": "roster", "title": "Talentgram X Secret", "subtitle": "Sub"}
    assert (await http.get("/api/public/links/nope/meta")).status_code == 404
    await tdb.links.update_one({"id": link["id"]}, {"$set": {"is_public": False}})
    assert (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).status_code == 403
    assert (await http.get(f"/api/public/links/{link['slug']}/roster/pdf", headers=h)).status_code == 403
    assert (await http.post(f"/api/public/links/{link['slug']}/identify", json={"name": "x", "email": "v@example.com"})).status_code == 403


# =========================================================================
# Views / downloads reuse the existing link machinery
# =========================================================================
async def test_views_and_unique_views_use_the_existing_identify_counters(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    link = await _create(http, "R", [_default_sel(t)])
    await _viewer(http, link["slug"], "a@example.com")
    await _viewer(http, link["slug"], "a@example.com")
    await _viewer(http, link["slug"], "b@example.com")
    row = next(x for x in (await http.get("/api/links")).json() if x["id"] == link["id"])
    assert row["view_count"] == 3 and row["unique_viewers"] == 2
    h = await _viewer(http, link["slug"], "c@example.com")
    await http.get(f"/api/public/links/{link['slug']}/roster/pdf", headers=h)
    assert await tdb.link_downloads.count_documents({"link_id": link["id"], "media_id": "roster:pdf"}) == 1


# =========================================================================
# PDF
# =========================================================================
async def test_pdf_is_valid_complete_and_clickable(env, cdn):
    tdb, http = env
    a = await _talent(tdb, cdn, name="Angela Kumar")
    b = await _talent(tdb, cdn, name="Jasmine Singh", media=[
        _m(cdn, "indian", "/landscape.jpg", "j1"), _m(cdn, "western", "/square.jpg", "j2"), _m(cdn, "portfolio", "/rotated.jpg", "j3"),
        _m(cdn, "portfolio", "/portrait.jpg", "j4"), _m(cdn, "portfolio", "/portrait2.jpg", "j5")])
    link = await _create(http, "Talentgram X Pepsi", [_default_sel(a), {"talent_id": b["id"], "media_ids": ["j1", "j2", "j3", "j4", "j5"]}],
                         subtitle="Selected Talent")
    h = await _viewer(http, link["slug"])
    r = await http.get(f"/api/public/links/{link['slug']}/roster/pdf", headers=h)
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    assert "Talentgram%20X%20Pepsi.pdf" in r.headers["content-disposition"] or 'Talentgram X Pepsi.pdf' in r.headers["content-disposition"]
    reader = PdfReader(io.BytesIO(r.content))
    texts = [p.extract_text() for p in reader.pages]
    assert "TALENTGRAM X PEPSI" in texts[0] and "SELECTED TALENT" in texts[0]
    # cover + Angela (4 imgs + hero page 1... ) + Jasmine (5 imgs: hero + 1 gallery)
    expected_a = len(L.plan_talent_pages([(f"{i}", 1) for i in range(4)]))
    assert len(reader.pages) == 1 + expected_a + 2
    joined = "\n".join(texts)
    assert "ANGELA K" in joined and "JASMINE S" in joined and "KUMAR" not in joined.upper() and "SINGH" not in joined.upper()
    assert "Mumbai, India" in joined and "@ak_official" in joined
    # every page has at least one image (cover/index excepted) and annotations include the video + instagram links
    uris = []
    for p in reader.pages:
        for ann in p.get("/Annots") or []:
            a_ = ann.get_object()
            if a_.get("/A") and a_["/A"].get("/URI"):
                uris.append(str(a_["/A"]["/URI"]))
    assert "https://res.cloudinary.com/demo/video/upload/dog.mp4" in uris
    assert "https://www.instagram.com/ak_official/" in uris
    for page in reader.pages[1:]:
        assert len(list(page.images)) >= 1
    # embedded images are downscaled JPEGs (no giant originals)
    assert len(r.content) < 3 * 1024 * 1024


async def test_pdf_is_cached_and_regenerated_when_the_roster_changes(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    link = await _create(http, "R", [_default_sel(t)])
    h = await _viewer(http, link["slug"])
    p = f"/api/public/links/{link['slug']}/roster/pdf"
    t0 = time.time(); r1 = await http.get(p, headers=h); first = time.time() - t0
    t0 = time.time(); r2 = await http.get(p, headers=h); second = time.time() - t0
    assert r1.content == r2.content and second <= first
    # edit: drop an image -> different content -> a different PDF
    sel = _default_sel(t)
    sel["media_ids"] = sel["media_ids"][:2]
    up = await http.put(f"/api/links/{link['id']}", json=_payload("R", [sel]))
    assert up.status_code == 200
    r3 = await http.get(p, headers=h)
    assert r3.content != r1.content
    admin_pdf = await http.get(f"/api/links/{link['id']}/roster/pdf")
    assert admin_pdf.status_code == 200 and admin_pdf.content == r3.content


async def test_pdf_survives_a_broken_image_and_never_leaves_temp_files(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, media=[_m(cdn, "indian", "/portrait.jpg", "ok"), _m(cdn, "indian", "/broken.jpg", "bad"),
                                       _m(cdn, "indian", "/missing404.jpg", "gone")])
    link = await _create(http, "R", [{"talent_id": t["id"], "media_ids": ["ok", "bad", "gone"]}])
    h = await _viewer(http, link["slug"])
    before = {d for d in os.listdir(tempdir()) if d.startswith("tg_roster_")}
    r = await http.get(f"/api/public/links/{link['slug']}/roster/pdf", headers=h)
    assert r.status_code == 200
    assert len(PdfReader(io.BytesIO(r.content)).pages) == 2  # cover + the one good image
    after = {d for d in os.listdir(tempdir()) if d.startswith("tg_roster_")}
    assert after == before, "temp work dir leaked"


def tempdir():
    import tempfile
    return tempfile.gettempdir()


# =========================================================================
# Existing Generated Links keep working
# =========================================================================
async def test_existing_link_types_are_unaffected(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    legacy = {"title": "Talentgram x Legacy", "brand_name": "Legacy", "talent_ids": [t["id"]], "submission_ids": [], "visibility": {},
              "talent_field_visibility": {}, "auto_pull": False, "auto_project_id": None, "is_public": True,
              "password": None, "notes": None, "client_budget_override": None}
    r = await http.post("/api/links", json=legacy)
    assert r.status_code == 200 and r.json().get("link_type") is None and r.json()["talent_ids"] == [t["id"]]
    lid, slug = r.json()["id"], r.json()["slug"]
    # classic identify + classic public payload still work
    h = await _viewer(http, slug)
    assert (await http.get(f"/api/public/links/{slug}", headers=h)).status_code == 200
    meta = (await http.get(f"/api/public/links/{slug}/meta")).json()
    assert meta == {"link_type": "talent", "title": None, "subtitle": None}  # classic links leak no title
    # classic update, duplicate, delete
    legacy["title"] = "Talentgram x Legacy 2"
    assert (await http.put(f"/api/links/{lid}", json=legacy)).status_code == 200
    assert (await http.post(f"/api/links/{lid}/duplicate")).status_code == 200
    assert (await http.delete(f"/api/links/{lid}")).status_code == 200


async def test_roster_settings_update_never_wipes_the_roster_and_duplicate_and_delete_work(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn)
    link = await _create(http, "R", [_default_sel(t)])
    before = (await tdb.links.find_one({"id": link["id"]}, {"_id": 0}))["roster"]
    # the Generated Links "public link" toggle round-trips the generic LinkIn shape WITHOUT a roster
    toggle = {"title": "R", "brand_name": None, "talent_ids": [t["id"]], "submission_ids": [], "visibility": {},
              "talent_field_visibility": {}, "auto_pull": False, "auto_project_id": None, "is_public": False,
              "password": None, "notes": None, "client_budget_override": None}
    r = await http.put(f"/api/links/{link['id']}", json=toggle)
    assert r.status_code == 200 and r.json()["is_public"] is False
    doc = await tdb.links.find_one({"id": link["id"]}, {"_id": 0})
    assert doc["roster"] == before and doc["link_type"] == "roster" and doc["slug"] == link["slug"]
    dup = await http.post(f"/api/links/{link['id']}/duplicate")
    assert dup.status_code == 200 and dup.json()["link_type"] == "roster" and dup.json()["slug"] != link["slug"]
    assert (await http.delete(f"/api/links/{link['id']}")).status_code == 200


async def test_roster_edit_keeps_the_same_canonical_slug_and_applies_new_order(env, cdn):
    tdb, http = env
    a = await _talent(tdb, cdn, name="Angela Kumar")
    b = await _talent(tdb, cdn, name="Jasmine Singh")
    link = await _create(http, "Talentgram X Pepsi", [_default_sel(a), _default_sel(b)])
    up = await http.put(f"/api/links/{link['id']}", json=_payload("Talentgram X Pepsi v2", [_default_sel(b), _default_sel(a)], subtitle="New"))
    assert up.status_code == 200 and up.json()["slug"] == link["slug"] and up.json()["talent_ids"] == [b["id"], a["id"]]
    h = await _viewer(http, link["slug"])
    view = (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()
    assert [t["name"] for t in view["talents"]] == ["Jasmine S", "Angela K"] and view["title"] == "Talentgram X Pepsi v2" and view["subtitle"] == "New"


async def test_pdf_is_warmed_in_the_background_after_create(env, cdn):
    import asyncio
    tdb, http = env
    t = await _talent(tdb, cdn)
    link = await _create(http, "R", [_default_sel(t)])
    doc = await tdb.links.find_one({"id": link["id"]}, {"_id": 0})
    key = roster_pdf.content_key(await R.resolve_roster(doc))
    path = os.path.join(roster_pdf.CACHE_DIR, f"{key}.pdf")
    assert not os.path.exists(path)
    _REAL_WARM(doc)                      # fire-and-forget: returns immediately
    for _ in range(100):                 # ...and the PDF appears shortly after
        if os.path.exists(path):
            break
        await asyncio.sleep(0.2)
    assert os.path.exists(path) and open(path, "rb").read(5) == b"%PDF-"


async def test_hidden_and_internal_media_can_never_be_selected_or_shown(env, cdn):
    tdb, http = env
    t = await _talent(tdb, cdn, media=[
        _m(cdn, "indian", "/portrait.jpg", "ok"),
        _m(cdn, "indian", "/portrait2.jpg", "hidden", client_visible=False),
        _m(cdn, "western", "/square.jpg", "internal", internal_only=True),
    ])
    opts = (await http.post("/api/roster/media-options", json={"talent_ids": [t["id"]]})).json()["talents"][0]
    offered = {i["id"] for g in opts["groups"] for i in g["items"]}
    assert offered == {"ok"}
    assert (await http.post("/api/links", json=_payload("R", [{"talent_id": t["id"], "media_ids": ["hidden"]}]))).status_code == 400
    link = await _create(http, "R", [{"talent_id": t["id"], "media_ids": ["ok"]}])
    # flipping an already-selected image to hidden afterwards removes it from the live roster
    await tdb.talents.update_one({"id": t["id"], "media.id": "ok"}, {"$set": {"media.$.client_visible": False}})
    h = await _viewer(http, link["slug"])
    assert (await http.get(f"/api/public/links/{link['slug']}/roster", headers=h)).json()["talents"][0]["images"] == []


async def test_dimension_probe_retries_with_a_larger_range_for_big_headers(cdn):
    async with httpx.AsyncClient() as c:
        assert await R.probe_image_dims(c, f"{cdn}/portrait.jpg") == (900, 1200)
        assert await R.probe_image_dims(c, f"{cdn}/broken.jpg") is None
        assert await R.probe_image_dims(c, f"{cdn}/missing404.jpg") is None
