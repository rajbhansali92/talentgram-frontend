"""Personal Assistant — the `update` command (profile fields + media), end to end through the real FastAPI app and real local dev Mongo.

Throwaway Qzx talents only. Cloudinary is MOCKED (routers.talents.upload_and_track_asset / delete_talent_media_item are replaced), so nothing is
uploaded anywhere and no real media is touched; nothing is sent to WhatsApp. Fields go through the REAL PUT /talents/{id} function and media
through the REAL POST /talents/{id}/media function (only the storage call inside it is mocked).

  1 parsing & planning     3 media: manifest → approval → upload     5 safety: authorization, stale plans, double clicks
  2 fields                 4 failures and partial success             6 regressions"""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import asyncio
import uuid

import pytest
import pytest_asyncio

from core import _now, db
from personal_assistant import update_items as U
from personal_assistant.parser import parse_message
from test_personal_assistant import (  # noqa: F401  (fixtures + helpers of the existing suite)
    BASE, _add_rows, _isolate_each_test, _jobs, _project, _stage, _talent, _user, alice, anon, bob, cleanup, say, world, team,
    _real_db_for_this_module,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 60


class FakeStorage:
    def __init__(self):
        self.calls, self.fail_next, self.deleted = [], 0, []

    async def upload(self, data, **kw):
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("storage unavailable")
        self.calls.append({"size": len(data), "asset_type": kw.get("asset_type"), "talent_id": kw.get("talent_id")})
        pid = f"zzz-test/{uuid.uuid4().hex}"
        rt = kw.get("resource_type")
        return {"url": f"https://res.example.invalid/{pid}", "public_id": pid, "resource_type": rt, "bytes": len(data), "format": "mp4" if rt == "video" else "jpg",
                "video_codec": "h264" if rt == "video" else None, "duration": 3.0 if rt == "video" else None}


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def storage(monkeypatch):
    fake = FakeStorage()
    import routers.talents as rt
    monkeypatch.setattr(rt, "upload_and_track_asset", fake.upload)

    async def fake_delete(tid, mid):
        fake.deleted.append(mid)
        await db.talents.update_one({"id": tid}, {"$pull": {"media": {"id": mid}}})
    monkeypatch.setattr(rt, "delete_talent_media_item", fake_delete)
    yield fake


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def u(world):
    r = {}
    r["angela"] = await _talent("Qzx Angela Updx", age=22, height="5'6\"", location=[{"city": "Delhi", "country": "India"}], whatsapp_group_name="OLD GROUP NAME",
                                instagram_handle="qzx_old", phone="911234500321", bio="Original bio", skills=["Dance"],
                                media=[{"id": "v-old", "category": "video", "url": "https://res.example.invalid/v-old", "public_id": "zzz-test/v-old", "resource_type": "video",
                                        "original_filename": "old-intro.mp4", "size": 10, "created_at": _now()},
                                       {"id": "i1", "category": "indian", "url": "https://res.example.invalid/i1", "public_id": "zzz-test/i1", "resource_type": "image",
                                        "original_filename": "i1.jpg", "size": 10, "created_at": _now()}])
    r["priya"] = await _talent("Qzx Priya Updx", age=30, location=[{"city": "Pune", "country": "India"}])
    r["dob"] = await _talent("Qzx Dobbie Updx", age=40, dob="1985-03-04")
    r["sou1"] = await _talent("Qzx Soumya Updx", age=21)
    r["sou2"] = await _talent("Qzx Soumya Updx", age=33, phone="911234500322")
    r["w1"] = world["w1"]
    return r


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _reset(u):
    snap = {k: await db.talents.find_one({"id": v}, {"_id": 0}) for k, v in u.items() if k != "w1"}
    yield
    for k, doc in snap.items():
        if doc:
            await db.talents.replace_one({"id": doc["id"]}, doc)
    await db.profile_audits.delete_many({"talent_id": {"$in": [v for k, v in u.items() if k != "w1"]}})


async def _tasks(anon, who, text):
    out = await say(anon, who, text)
    return out, out["tasks"]


async def _confirm(anon, who, task, expect=200):
    r = await anon.post(f"{BASE}/tasks/{task['id']}/confirm", headers=who["headers"], json={"fingerprint": task["fingerprint"]})
    assert r.status_code == expect, r.text
    return r.json()["task"] if expect == 200 else r


async def _get(anon, who, tid):
    return (await anon.get(f"{BASE}/tasks/{tid}", headers=who["headers"])).json()["task"]


async def _manifest(anon, who, task, category, files):
    r = await anon.post(f"{BASE}/tasks/{task['id']}/clarify", headers=who["headers"],
                        json={"slot": f"files:{category}", "files": [{"name": n, "size": len(b), "type": t} for n, b, t in files]})
    return r


async def _upload(anon, who, task, category, name, data, ctype):
    return await anon.post(f"{BASE}/tasks/{task['id']}/media", headers=who["headers"],
                           data={"category": category, "key": f"{name}|{len(data)}"}, files={"file": (name, data, ctype)})


async def _doc(tid):
    return await db.talents.find_one({"id": tid}, {"_id": 0})


# =========================================================================== 1. parsing and planning
def test_the_briefs_examples_parse_into_per_talent_groups():
    def g(text):
        return [(x["names"], [(i["key"], i["raw"]) for i in x["items"]]) for x in U.parse_update(text)]
    assert g("Update Angela Kumar's introduction video.") == [(["Angela Kumar"], [("video", None)])]
    assert g("Update Angela Kumar's Indian pictures and Western pictures.") == [(["Angela Kumar"], [("indian", None), ("western", None)])]
    assert g("Update Angela Kumar's introduction video and additional portfolio.") == [(["Angela Kumar"], [("video", None), ("portfolio", None)])]
    assert g("Update Angela Kumar's age to 24.") == [(["Angela Kumar"], [("age", "24")])]
    assert g("Update Angela Kumar's height to 5 feet 7 inches.") == [(["Angela Kumar"], [("height", "5 feet 7 inches")])]
    assert g("Update Angela Kumar's location to Mumbai.") == [(["Angela Kumar"], [("location", "Mumbai")])]
    assert g("Update Angela Kumar's introduction video and age.") == [(["Angela Kumar"], [("video", None), ("age", None)])]
    assert g("Update Angela Kumar and Priya Sharma's locations to Mumbai.") == [(["Angela Kumar", "Priya Sharma"], [("location", "Mumbai")])]
    assert g("Update Angela Kumar's introduction video and Priya Sharma's Western pictures.") == [(["Angela Kumar"], [("video", None)]), (["Priya Sharma"], [("western", None)])]
    assert g("Update Angela Kumar's age to 24 and location to Mumbai") == [(["Angela Kumar"], [("age", "24"), ("location", "Mumbai")])]
    assert g("update the introduction video of Angela Kumar") == [(["Angela Kumar"], [("video", None)])]


def test_a_pasted_group_name_is_kept_exactly_and_may_contain_item_words():
    raw = "Mallika  Dang X Talentgram (Age & Height)"
    got = U.parse_update(f"Update Angela Kumar's WhatsApp group name to {raw}")[0]["items"][0]
    assert got["key"] == "whatsapp_group_name" and got["raw"] == raw                        # inner double space, case, punctuation, the word "Age" — all kept
    q = U.parse_update('Update Angela Kumar\'s WhatsApp group name to "Say Age and Height" and age to 24')[0]["items"]
    assert [(i["key"], i["raw"]) for i in q] == [("whatsapp_group_name", "Say Age and Height"), ("age", "24")]
    assert U.parse_value("whatsapp_group_name", raw)[0] == raw


def test_values_are_read_only_in_the_fields_own_format_and_never_guessed():
    assert U.parse_value("age", "24")[0] == 24 and U.parse_value("height", "5 feet 7 inches")[0] == "5'7\"" and U.parse_value("height", "5'7")[0] == "5'7\""
    assert U.parse_value("height", "170 cm")[0] == "5'7\"" and "rounded" in U.parse_value("height", "170 cm")[2]
    assert U.parse_value("location", "Mumbai")[0] == [{"city": "Mumbai", "country": "India"}] and "India assumed" in U.parse_value("location", "Mumbai")[2]
    assert U.parse_value("location", "Dubai, UAE")[0] == [{"city": "Dubai", "country": "UAE"}]
    assert U.parse_value("instagram_handle", "https://www.instagram.com/some.one/")[0] == "some.one"
    # spoken numbers are read deterministically (2026-10-20): "twenty four" is 24, "five feet seven" is 5'7" — anything not a whole number word is still refused
    assert U.parse_value("age", "twenty four")[0] == 24 and U.parse_value("height", "five feet seven")[0] == "5'7\""
    for key, bad in (("age", "twenty four hundred"), ("age", "150"), ("height", "tall"), ("height", "9 feet 2"), ("height", "one seventy"), ("instagram_handle", "not a handle!"), ("instagram_followers", "lots")):
        with pytest.raises(U.BadValue):
            U.parse_value(key, bad)


async def test_each_clause_becomes_its_own_task_for_its_own_talent(anon, alice, u):
    out, ts = await _tasks(anon, alice, "Update Qzx Angela Updx's introduction video and Qzx Priya Updx's Western pictures")
    assert [t["kind"] for t in ts] == ["update", "update"]
    a, p = ts
    assert a["params"]["talents"][0]["id"] == u["angela"] and [m["category"] for m in a["params"]["updates"]["media"]] == ["video"]
    assert p["params"]["talents"][0]["id"] == u["priya"] and [m["category"] for m in p["params"]["updates"]["media"]] == ["western"]
    assert a["status"] == p["status"] == "needs_clarification" and a["slots"][0]["kind"] == "files" and a["slots"][0]["category"] == "video"


async def test_two_talents_sharing_one_value_become_two_independent_tasks(anon, alice, u):
    out, ts = await _tasks(anon, alice, "Update Qzx Angela Updx and Qzx Priya Updx's locations to Mumbai")
    assert len(ts) == 2 and {t["params"]["talents"][0]["id"] for t in ts} == {u["angela"], u["priya"]}
    assert all(t["status"] == "awaiting_confirmation" for t in ts)
    assert any("Delhi, India → Mumbai, India" in l for l in ts[0]["summary"] + ts[1]["summary"])


async def test_ambiguous_and_sound_alike_names_are_cards_and_nothing_is_written(anon, alice, u):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Soumya Updx's age to 25")
    assert t["status"] == "needs_clarification" and t["slots"][0]["kind"] == "talent" and {o["id"] for o in t["slots"][0]["options"]} == {u["sou1"], u["sou2"]}
    _o, (s,) = await _tasks(anon, alice, "Update Qzx Priya Updz's age to 25")                          # a mis-heard / mis-typed name
    assert s["status"] == "needs_clarification" and not s["params"]["talents"][0].get("id")
    assert (await _doc(u["priya"]))["age"] == 30 and (await _doc(u["sou1"]))["age"] == 21
    r = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "talent:0", "choice_id": u["sou2"]})
    picked = r.json()["task"]
    assert picked["status"] == "awaiting_confirmation" and picked["params"]["talents"][0]["id"] == u["sou2"] and (await _doc(u["sou2"]))["age"] == 33   # identity only


async def test_an_item_without_a_value_asks_for_it_and_an_unsupported_one_is_reported_not_guessed(anon, alice, u):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's age")
    assert t["status"] == "needs_clarification" and t["slots"][0]["key"] == "value:age"
    _o, (e,) = await _tasks(anon, alice, "Update Qzx Angela Updx's email to someone@example.com")
    assert e["status"] == "failed" and "profile page" in e["error"]
    _o, (m,) = await _tasks(anon, alice, "Update Qzx Angela Updx's age to 25 and email to someone@example.com")
    assert m["status"] == "awaiting_confirmation" and any("can't be changed from here" in l for l in m["summary"]) and (await _doc(u["angela"]))["email"] != "someone@example.com"


# =========================================================================== 2. fields
async def test_age_and_location_update_only_those_fields_and_everything_else_survives(anon, alice, u):
    before = await _doc(u["angela"])
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's age to 24 and location to Mumbai")
    assert t["status"] == "awaiting_confirmation" and t["summary"][0] == "Talent: Qzx Angela Updx"
    assert "Age: 22 → 24" in t["summary"] and any(l.startswith("Location: Delhi, India → Mumbai, India") for l in t["summary"])
    assert await _doc(u["angela"]) == before                                                            # planning wrote nothing
    done = await _confirm(anon, alice, t)
    assert done["status"] == "completed"
    after = await _doc(u["angela"])
    assert after["age"] == 24 and after["location"] == [{"city": "Mumbai", "country": "India"}]
    for k in ("name", "email", "phone", "bio", "skills", "instagram_handle", "whatsapp_group_name", "height", "media", "tags"):      # (the cover cache refresh is the existing save's own side effect)
        assert after.get(k) == before.get(k), k                                                         # a partial update never blanks the rest of the profile
    assert [o["status"] for o in done["outcomes"]] == ["updated", "updated"] and done["outcomes"][0]["detail"] == "24"
    assert await db.profile_audits.count_documents({"talent_id": u["angela"], "source": "admin_edit"}) >= 1


async def test_height_group_name_and_instagram_use_the_existing_formats_and_exact_values(anon, alice, u):
    exact = "Mallika  Dang X Talentgram"
    _o, (t,) = await _tasks(anon, alice, f"Update Qzx Angela Updx's height to 5 feet 7 inches and instagram handle to @new.handle")
    assert (await _confirm(anon, alice, t))["status"] == "completed"
    a = await _doc(u["angela"])
    assert a["height"] == "5'7\"" and a["height_inches"] == 67.0 and a["instagram_handle"] == "new.handle"
    _o, (g,) = await _tasks(anon, alice, f"Update Qzx Angela Updx's WhatsApp group name to {exact}")
    assert any(exact in l for l in g["summary"])
    assert (await _confirm(anon, alice, g))["status"] == "completed"
    assert (await _doc(u["angela"]))["whatsapp_group_name"] == exact                                        # byte for byte


async def test_age_is_refused_when_the_profile_calculates_it_from_a_date_of_birth(anon, alice, u):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Dobbie Updx's age to 25")
    assert t["status"] == "failed" and "date of birth" in t["error"]
    assert (await _doc(u["dob"]))["age"] == 40


async def test_an_invalid_value_asks_again_instead_of_writing(anon, alice, u):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's age to 250")
    assert t["status"] == "needs_clarification" and "between 0 and 120" in t["slots"][0]["prompt"]
    r = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "value:age", "text": "26"})
    assert r.json()["task"]["status"] == "awaiting_confirmation" and (await _doc(u["angela"]))["age"] == 22


async def test_a_field_that_changed_after_review_is_not_overwritten(anon, alice, u):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's age to 24")
    await db.talents.update_one({"id": u["angela"]}, {"$set": {"age": 23, "updated_at": _now()}})        # someone edited it meanwhile
    r = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"fingerprint": t["fingerprint"]})
    assert r.status_code == 409 and r.json()["detail"]["changed"]                                       # the approval no longer matches what was shown
    revised = r.json()["detail"]["tasks"][0]
    assert revised["fingerprint"] != t["fingerprint"] and any("Age: 23 → 24" in l for l in revised["summary"]) and (await _doc(u["angela"]))["age"] == 23
    # an old client that sends NO fingerprint still cannot overwrite a value that changed after the plan: the executor checks it itself
    _o, (t2,) = await _tasks(anon, alice, "Update Qzx Angela Updx's age to 29")
    await db.talents.update_one({"id": u["angela"]}, {"$set": {"age": 27}})
    legacy = await anon.post(f"{BASE}/tasks/{t2['id']}/confirm", headers=alice["headers"], json={})
    done = legacy.json()["task"]
    assert done["status"] == "failed" and "changed since you reviewed" in done["outcomes"][0]["reason"] and (await _doc(u["angela"]))["age"] == 27


async def test_a_save_failure_is_reported_as_failure_never_as_success(anon, alice, u, monkeypatch):
    import routers.talents as rt
    from fastapi import HTTPException

    async def boom(tid, payload, admin):
        raise HTTPException(409, "Another talent already has this email")
    monkeypatch.setattr(rt, "update_talent", boom)
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's age to 24")
    done = await _confirm(anon, alice, t)
    assert done["status"] == "failed" and done["outcomes"][0]["status"] == "failed" and "already has this email" in done["outcomes"][0]["reason"]
    assert (await _doc(u["angela"]))["age"] == 22


async def test_success_is_reported_only_after_the_value_is_read_back_from_the_profile(anon, alice, u, monkeypatch):
    import routers.talents as rt

    async def silently_does_nothing(tid, payload, admin):          # the write "succeeds" but nothing is persisted
        return {}
    monkeypatch.setattr(rt, "update_talent", silently_does_nothing)
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's age to 24")
    done = await _confirm(anon, alice, t)
    assert done["status"] == "failed" and "not saved" in done["outcomes"][0]["reason"] and (await _doc(u["angela"]))["age"] == 22


# =========================================================================== 3. media
async def test_a_video_goes_manifest_then_approval_then_upload_and_replaces_the_old_one(anon, alice, u, storage):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's introduction video")
    assert t["slots"][0]["accept"].startswith(".mp4") and t["slots"][0]["multiple"] is False
    r = await _manifest(anon, alice, t, "video", [("new-intro.mp4", MP4, "video/mp4")])
    task = r.json()["task"]
    assert task["status"] == "awaiting_confirmation"
    assert any("REPLACES old-intro.mp4 with new-intro.mp4" in l for l in task["summary"])               # the operation and the exact file are shown before approval
    assert storage.calls == []                                                                           # nothing uploaded before approval
    run = await _confirm(anon, alice, task)
    assert run["status"] == "running" and run["params"]["updates"]["media"][0]["files"][0]["state"] == "pending"
    up = await _upload(anon, alice, run, "video", "new-intro.mp4", MP4, "video/mp4")
    assert up.status_code == 200
    done = up.json()["task"]
    assert done["status"] == "completed" and done["outcomes"][0]["status"] == "uploaded" and done["outcomes"][0]["detail"] == "new-intro.mp4"
    media = (await _doc(u["angela"]))["media"]
    vids = [m for m in media if m["category"] == "video"]
    assert len(vids) == 1 and vids[0]["original_filename"] == "new-intro.mp4" and "v-old" in storage.deleted      # replaced, exactly one video left
    assert [m["id"] for m in media if m["category"] == "indian"] == ["i1"]                                # other categories untouched
    assert storage.calls[0]["talent_id"] == u["angela"] and storage.calls[0]["asset_type"] == "portfolio_video"


async def test_pictures_are_added_never_replaced_and_every_filename_is_reported(anon, alice, u, storage):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's Indian pictures and Western pictures")
    assert [s["key"] for s in t["slots"]] == ["files:indian", "files:western"]
    await _manifest(anon, alice, t, "indian", [("a.jpg", JPEG, "image/jpeg"), ("b.png", PNG, "image/png")])
    task = (await _manifest(anon, alice, t, "western", [("w.jpg", JPEG, "image/jpeg")])).json()["task"]
    assert any("Indian look pictures: adds 2 files (a.jpg, b.png) to 1 existing — nothing is removed" in l for l in task["summary"])
    run = await _confirm(anon, alice, task)
    for cat, name, data, ct in (("indian", "a.jpg", JPEG, "image/jpeg"), ("indian", "b.png", PNG, "image/png"), ("western", "w.jpg", JPEG, "image/jpeg")):
        assert (await _upload(anon, alice, run, cat, name, data, ct)).status_code == 200
    final = await _get(anon, alice, run["id"])
    assert final["status"] == "completed" and sorted(o["detail"] for o in final["outcomes"]) == ["a.jpg", "b.png", "w.jpg"]
    media = (await _doc(u["angela"]))["media"]
    assert sorted(m["original_filename"] for m in media if m["category"] == "indian") == ["a.jpg", "b.png", "i1.jpg"]      # added next to the existing one
    assert [m["original_filename"] for m in media if m["category"] == "western"] == ["w.jpg"]
    assert not storage.deleted


async def test_files_are_mapped_to_the_right_talent_and_category_only(anon, alice, u, storage):
    _o, (a, p) = await _tasks(anon, alice, "Update Qzx Angela Updx's introduction video and Qzx Priya Updx's Western pictures")
    a = (await _manifest(anon, alice, a, "video", [("angela.mp4", MP4, "video/mp4")])).json()["task"]
    p = (await _manifest(anon, alice, p, "western", [("priya.jpg", JPEG, "image/jpeg")])).json()["task"]
    ra, rp = await _confirm(anon, alice, a), await _confirm(anon, alice, p)
    wrong = await _upload(anon, alice, ra, "video", "priya.jpg", JPEG, "image/jpeg")                     # Priya's file offered to Angela's task
    assert wrong.status_code == 400
    wrong2 = await _upload(anon, alice, rp, "indian", "priya.jpg", JPEG, "image/jpeg")                   # right file, category that was never approved
    assert wrong2.status_code == 400
    assert (await _upload(anon, alice, ra, "video", "angela.mp4", MP4, "video/mp4")).status_code == 200
    assert (await _upload(anon, alice, rp, "western", "priya.jpg", JPEG, "image/jpeg")).status_code == 200
    assert [m["original_filename"] for m in (await _doc(u["angela"]))["media"] if m["category"] == "video"] == ["angela.mp4"]
    assert [m["original_filename"] for m in (await _doc(u["priya"]))["media"] if m["category"] == "western"] == ["priya.jpg"]
    assert not [m for m in (await _doc(u["priya"]))["media"] if m["category"] == "video"]


async def test_file_validation_uses_the_admin_pages_own_rules(anon, alice, u):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's introduction video")
    for name, data, ct, why in (("doc.pdf", b"%PDF-" + b"0" * 20, "application/pdf", "format"), ("clip.webm", MP4, "video/webm", "format"), ("empty.mp4", b"", "video/mp4", "empty")):
        r = await _manifest(anon, alice, t, "video", [(name, data, ct)])
        assert r.status_code == 400, why
    r = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "files:video", "files": [{"name": "huge.mp4", "size": 300 * 1024 * 1024, "type": "video/mp4"}]})
    assert r.status_code == 400 and "200 MB" in r.text
    r = await _manifest(anon, alice, t, "video", [("a.mp4", MP4, "video/mp4"), ("b.mp4", MP4 + b"1", "video/mp4")])
    assert r.status_code == 400 and "single file" in r.text
    _o, (i,) = await _tasks(anon, alice, "Update Qzx Angela Updx's Indian pictures")
    r = await anon.post(f"{BASE}/tasks/{i['id']}/clarify", headers=alice["headers"], json={"slot": "files:indian", "files": [{"name": "big.jpg", "size": 21 * 1024 * 1024, "type": "image/jpeg"}]})
    assert r.status_code == 400 and "20 MB" in r.text


# =========================================================================== 4. failures and partial success
async def test_an_upload_failure_leaves_the_profile_untouched_and_a_retry_completes_it(anon, alice, u, storage):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's Indian pictures")
    task = (await _manifest(anon, alice, t, "indian", [("a.jpg", JPEG, "image/jpeg"), ("b.jpg", JPEG + b"1", "image/jpeg")])).json()["task"]
    run = await _confirm(anon, alice, task)
    storage.fail_next = 1
    first = await _upload(anon, alice, run, "indian", "a.jpg", JPEG, "image/jpeg")
    assert first.status_code == 200 and first.json()["task"]["status"] == "running"                       # one file still waiting
    assert first.json()["task"]["outcomes"][0]["status"] == "failed" and "nothing was added" in first.json()["task"]["outcomes"][0]["reason"]
    assert [m["original_filename"] for m in (await _doc(u["angela"]))["media"] if m["category"] == "indian"] == ["i1.jpg"]
    second = (await _upload(anon, alice, run, "indian", "b.jpg", JPEG + b"1", "image/jpeg")).json()["task"]
    assert second["status"] == "partial" and sorted(o["status"] for o in second["outcomes"]) == ["failed", "uploaded"]
    retry = (await _upload(anon, alice, run, "indian", "a.jpg", JPEG, "image/jpeg")).json()["task"]        # the failed one can be retried
    assert retry["status"] == "completed" and sorted(o["detail"] for o in retry["outcomes"]) == ["a.jpg", "b.jpg"]
    assert sorted(m["original_filename"] for m in (await _doc(u["angela"]))["media"] if m["category"] == "indian") == ["a.jpg", "b.jpg", "i1.jpg"]


async def test_a_renamed_file_whose_bytes_are_not_what_it_claims_is_rejected(anon, alice, u, storage):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's introduction video")
    fake = b"MZ" + b"\x00" * 60                                                                          # an executable renamed .mp4
    task = (await _manifest(anon, alice, t, "video", [("fake.mp4", fake, "video/mp4")])).json()["task"]
    run = await _confirm(anon, alice, task)
    done = (await _upload(anon, alice, run, "video", "fake.mp4", fake, "video/mp4")).json()["task"]
    assert done["status"] == "failed" and "don't match" in done["outcomes"][0]["reason"] and storage.calls == []
    assert [m["id"] for m in (await _doc(u["angela"]))["media"] if m["category"] == "video"] == ["v-old"]


async def test_a_mismatched_upload_is_refused_before_anything_is_stored(anon, alice, u, storage):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's introduction video")
    task = (await _manifest(anon, alice, t, "video", [("v.mp4", MP4, "video/mp4")])).json()["task"]
    run = await _confirm(anon, alice, task)
    r = await anon.post(f"{BASE}/tasks/{t['id']}/media", headers=alice["headers"], data={"category": "video", "key": f"v.mp4|{len(MP4)}"}, files={"file": ("v.mp4", MP4 + b"extra", "video/mp4")})
    assert r.status_code == 400 and storage.calls == []                                                   # different size than approved
    assert (await _get(anon, alice, run["id"]))["status"] == "running"


async def test_fields_and_media_in_one_plan_report_each_item_and_partial_success(anon, alice, u, storage):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's age to 24 and Indian pictures")
    task = (await _manifest(anon, alice, t, "indian", [("a.jpg", JPEG, "image/jpeg")])).json()["task"]
    run = await _confirm(anon, alice, task)
    assert run["status"] == "running" and (await _doc(u["angela"]))["age"] == 24                          # the field was written + verified at approval
    storage.fail_next = 1
    done = (await _upload(anon, alice, run, "indian", "a.jpg", JPEG, "image/jpeg")).json()["task"]
    assert done["status"] == "partial"
    assert [(o["name"].split(" — ")[1], o["status"]) for o in done["outcomes"]] == [("Age", "updated"), ("Indian look pictures", "failed")]


async def test_a_failure_for_one_talent_never_touches_another(anon, alice, u):
    out, (a, p) = await _tasks(anon, alice, "Update Qzx Angela Updx and Qzx Priya Updx's locations to Mumbai")
    await db.talents.update_one({"id": u["priya"]}, {"$set": {"status": "ARCHIVED"}})                      # Priya becomes unavailable after the plan
    ra = await _confirm(anon, alice, a)
    rp = await anon.post(f"{BASE}/tasks/{p['id']}/confirm", headers=alice["headers"], json={})
    assert ra["status"] == "completed" and (await _doc(u["angela"]))["location"] == [{"city": "Mumbai", "country": "India"}]
    assert rp.json()["task"]["status"] == "failed" and (await _doc(u["priya"]))["location"] == [{"city": "Pune", "country": "India"}]


# =========================================================================== 5. safety
async def test_cancel_writes_nothing_and_a_double_click_writes_once(anon, alice, u, storage):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's age to 24")
    c = await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])
    assert c.json()["task"]["status"] == "cancelled" and (await _doc(u["angela"]))["age"] == 22
    _o, (t2,) = await _tasks(anon, alice, "Update Qzx Angela Updx's location to Goa")
    rs = await asyncio.gather(*[anon.post(f"{BASE}/tasks/{t2['id']}/confirm", headers=alice["headers"], json={"fingerprint": t2["fingerprint"]}) for _ in range(6)])
    assert all(r.status_code == 200 for r in rs)
    assert await db.assistant_audit.count_documents({"task_id": t2["id"], "action": "execute"}) == 1
    assert await db.profile_audits.count_documents({"talent_id": u["angela"], "new_values.location": {"$exists": True}}) == 1       # one real write, one audit row


async def test_the_same_file_uploaded_twice_at_once_is_stored_once(anon, alice, u, storage):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's Indian pictures")
    task = (await _manifest(anon, alice, t, "indian", [("a.jpg", JPEG, "image/jpeg")])).json()["task"]
    run = await _confirm(anon, alice, task)
    rs = await asyncio.gather(*[_upload(anon, alice, run, "indian", "a.jpg", JPEG, "image/jpeg") for _ in range(4)])
    assert sorted(r.status_code for r in rs).count(200) == 1 and all(r.status_code in (200, 409) for r in rs)
    assert len(storage.calls) == 1 and [m["original_filename"] for m in (await _doc(u["angela"]))["media"] if m["original_filename"] == "a.jpg"] == ["a.jpg"]
    again = await _upload(anon, alice, run, "indian", "a.jpg", JPEG, "image/jpeg")                          # after it is done, a repeat is refused too
    assert again.status_code == 409 and len(storage.calls) == 1


async def test_uploads_need_an_approved_plan_and_the_owners_session(anon, alice, bob, u, storage):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's Indian pictures")
    task = (await _manifest(anon, alice, t, "indian", [("a.jpg", JPEG, "image/jpeg")])).json()["task"]
    early = await _upload(anon, alice, task, "indian", "a.jpg", JPEG, "image/jpeg")                          # not approved yet
    assert early.status_code == 409 and storage.calls == []
    run = await _confirm(anon, alice, task)
    assert (await _upload(anon, bob, run, "indian", "a.jpg", JPEG, "image/jpeg")).status_code == 404       # another user's task
    assert (await anon.post(f"{BASE}/tasks/{t['id']}/media", data={"category": "indian", "key": "x"}, files={"file": ("a.jpg", JPEG, "image/jpeg")})).status_code in (401, 403)
    assert storage.calls == []


async def test_only_an_admin_can_change_the_whatsapp_group_name_and_it_is_reported_not_silently_dropped(anon, alice, team, u, monkeypatch):
    monkeypatch.setenv("ASSISTANT_ALLOWED_USERS", team["email"].lower())
    _o, (t,) = await _tasks(anon, team, "Update Qzx Angela Updx's WhatsApp group name to Some New Group")
    done = await _confirm(anon, team, t)
    assert done["status"] == "failed" and "Only an admin" in done["outcomes"][0]["reason"]
    assert (await _doc(u["angela"]))["whatsapp_group_name"] == "OLD GROUP NAME"


async def test_the_approval_card_never_exposes_storage_urls_or_private_fields(anon, alice, u):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's age to 24")
    blob = str(t)
    assert "res.example.invalid" not in blob and "911234500321" not in blob and "@example.com" not in blob


# =========================================================================== 6. regressions
async def test_a_plan_with_an_update_keeps_the_other_commands_their_dependencies_and_fingerprints(anon, alice, u, world):
    await _add_rows(world["amazon"], u["priya"], stage="ask_to_test")
    out = await say(anon, alice, "Update Qzx Angela Updx's age to 24, move Qzx Priya Updx to Approved in Streaming Now")
    kinds = [t["kind"] for t in out["tasks"]]
    assert kinds == ["update", "move"] and all(t["fingerprint"] for t in out["tasks"])
    body = {"fingerprints": {t["id"]: t["fingerprint"] for t in out["tasks"]}}
    r = await anon.post(f"{BASE}/commands/{out['command_id']}/confirm-all", headers=alice["headers"], json=body)
    assert r.status_code == 200
    assert (await _doc(u["angela"]))["age"] == 24 and await _stage(world["amazon"], u["priya"]) == "approved"


def test_change_still_means_a_pipeline_move_and_update_never_swallows_other_commands():
    assert parse_message("change disha to approved in amazon now")[0].intent == "move"
    segs = parse_message("add sahal to amazon now update angela kumar's age to 24 send the casting call to vikram")
    assert [s.intent for s in segs] == ["add", "update", "send"]
    assert [s.intent for s in parse_message("update the pipeline please")] == ["unknown"]


async def test_the_update_task_cannot_be_retried_into_a_second_write(anon, alice, u):
    _o, (t,) = await _tasks(anon, alice, "Update Qzx Angela Updx's age to 24")
    done = await _confirm(anon, alice, t)
    r = await anon.post(f"{BASE}/tasks/{done['id']}/retry", headers=alice["headers"])
    assert r.status_code == 409
