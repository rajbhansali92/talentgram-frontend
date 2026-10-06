"""Global Talent list — Introduction Video / image counters.

The list API never sends media[] (see _LIST_PROJECTION) so the row's counters used to be
computed from nothing and every talent showed video = 0. The API now computes
`video_count` / `image_count` inside the list pipelines. These tests drive the REAL list
endpoint (every sort branch) against a real Mongo and compare with the Talent Preview's own
source (GET /talents/{id}).
"""
import os
import uuid

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
from routers import talents as talents_router

pytestmark = pytest.mark.asyncio
_MONGO_URL = os.environ.get("TEST_REAL_MONGO_URL", "mongodb://localhost:27017")

# every code path of GET /talents that builds a page: default, collated name sort,
# computed sort (followers/completeness), null-safe sort (height/age)
SORTS = [None, "name_asc", "created_asc", "followers_desc", "completeness_desc", "height_asc", "age_desc"]


def _m(cat, n=1, **extra):
    return [{"id": uuid.uuid4().hex, "category": cat, "url": f"https://res.cloudinary.com/x/{cat}/{uuid.uuid4().hex}.jpg",
             "resource_type": "video" if cat == "video" else "image",
             "content_type": "video/mp4" if cat == "video" else "image/jpeg", **extra} for _ in range(n)]


FIXTURES = {
    "with_video": _m("indian", 3) + _m("western", 3) + _m("video"),     # Sana-like: 6 images + 1 intro video
    "no_video": _m("indian", 2),                                          # images, no intro video
    "no_media": [],
    "video_only": _m("video"),
    "images_and_video": _m("western", 10) + _m("indian", 6) + _m("video"),  # Nikki-like
    "image_named_like_video": _m("portfolio", 2, content_type="video/quicktime", original_filename="clip.mov"),  # NOT an intro video
}


@pytest_asyncio.fixture
async def env(monkeypatch):
    client = AsyncIOMotorClient(_MONGO_URL)
    dbname = f"tg_list_counts_{uuid.uuid4().hex[:8]}"
    tdb = client[dbname]
    monkeypatch.setattr(talents_router, "db", tdb)
    ids = {}
    for i, (name, media) in enumerate(FIXTURES.items()):
        tid = f"t-{name}"
        ids[name] = tid
        await tdb.talents.insert_one({
            "id": tid, "name": f"Zed {name} {i}", "status": "ACTIVE", "created_at": f"2026-09-0{i + 1}T00:00:00+00:00",
            "media": media, "media_count": len(media), "height_inches": 60 + i, "dob": f"199{i}-01-01",
        })
    app = FastAPI()
    app.include_router(talents_router.router)
    app.dependency_overrides[core.current_team_or_admin] = lambda: {"id": "a", "email": "a@x.co", "role": "admin"}
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=60)
    yield tdb, http, ids
    await http.aclose()
    await client.drop_database(dbname)
    client.close()


async def _list(http, sort_by=None):
    params = {"page": 0, "size": 50}
    if sort_by:
        params["sort_by"] = sort_by
    r = await http.get("/api/talents", params=params)
    assert r.status_code == 200, r.text
    return {t["id"]: t for t in r.json()["items"]}


@pytest.mark.parametrize("sort_by", SORTS)
async def test_list_rows_carry_correct_video_and_image_counts_on_every_sort_branch(env, sort_by):
    tdb, http, ids = env
    rows = await _list(http, sort_by)
    assert set(ids.values()) <= set(rows), "every fixture talent is listed"
    expect = {  # (image_count, video_count)
        "with_video": (6, 1),               # 1. WITH intro video -> video 1 (image count = images only)
        "no_video": (2, 0),                 # 3. images, no video -> image count right, video 0
        "no_media": (0, 0),                 # 6. no video must not show a false count
        "video_only": (0, 1),
        "images_and_video": (16, 1),        # 4. images AND video -> images unchanged, video 1
        "image_named_like_video": (2, 0),   # a video-typed file that is not the Introduction Video is not counted
    }
    for name, (img, vid) in expect.items():
        row = rows[ids[name]]
        assert (row["image_count"], row["video_count"]) == (img, vid), (sort_by, name, row["image_count"], row["video_count"])


async def test_image_plus_video_always_adds_up_to_total_media(env):
    tdb, http, ids = env
    for row in (await _list(http)).values():
        assert row["image_count"] + row["video_count"] == row["media_count"] or row["id"] == ids["image_named_like_video"]
    # (the one exception is the deliberately mis-typed fixture: both its files are non-intro media)
    r = (await _list(http))[ids["image_named_like_video"]]
    assert r["image_count"] == r["media_count"] == 2 and r["video_count"] == 0


async def test_list_payload_stays_light_media_array_is_still_not_sent(env):
    tdb, http, ids = env
    for sort_by in SORTS:
        for row in (await _list(http, sort_by)).values():
            assert "media" not in row or row["media"] in (None, []), "media[] must not reach the list payload"
            for private in ("created_by", "notes", "whatsapp_group_name"):
                assert private not in row
    # stored total is untouched (still the grid badge / header total)
    rows = await _list(http)
    assert rows[ids["with_video"]]["media_count"] == 7


async def test_list_and_preview_use_the_same_canonical_introduction_video_field(env):
    """5. Preview plays the media item with category 'video' from GET /talents/{id}; the list's
    video_count must be derived from exactly that, for every talent."""
    tdb, http, ids = env
    rows = await _list(http)
    for name, tid in ids.items():
        preview = (await http.get(f"/api/talents/{tid}")).json()
        preview_videos = [m for m in preview["media"] if m.get("category") == "video"]
        assert rows[tid]["video_count"] == (1 if preview_videos else 0), name
        assert rows[tid]["image_count"] == len([m for m in preview["media"] if m.get("category") != "video"]), name


async def test_a_talent_with_several_video_category_items_still_reads_as_one_intro_video(env):
    tdb, http, ids = env
    await tdb.talents.update_one({"id": ids["with_video"]}, {"$push": {"media": {"$each": _m("video")}}})
    row = (await _list(http))[ids["with_video"]]
    assert row["video_count"] == 1   # "has an Introduction Video", never 2


async def test_talent_without_a_media_field_at_all_is_safe(env):
    tdb, http, ids = env
    await tdb.talents.insert_one({"id": "t-nomediafield", "name": "Zed nomedia", "status": "ACTIVE", "created_at": "2026-09-09T00:00:00+00:00"})
    for sort_by in SORTS:
        row = (await _list(http, sort_by))["t-nomediafield"]
        assert (row["image_count"], row["video_count"]) == (0, 0)


async def test_filters_and_pagination_still_work_on_the_new_pipeline(env):
    tdb, http, ids = env
    r = (await http.get("/api/talents", params={"page": 0, "size": 2, "sort_by": "name_asc"})).json()
    assert len(r["items"]) == 2 and r["total"] == len(FIXTURES) and r["has_more"] is True
    names = [t["name"] for t in r["items"]]
    assert names == sorted(names, key=str.lower)
    r2 = (await http.get("/api/talents", params={"page": 1, "size": 2, "sort_by": "name_asc"})).json()
    assert not set(t["id"] for t in r["items"]) & set(t["id"] for t in r2["items"])
    q = (await http.get("/api/talents", params={"q": "with_video", "page": 0, "size": 10})).json()
    assert [t["id"] for t in q["items"]] == [ids["with_video"]] and q["items"][0]["video_count"] == 1
