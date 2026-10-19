"""Personal Assistant — natural phrasings of `update <talent> <media/field>` (2026-10-20).

Root cause being guarded: `parse_update` only understood "<Name>'s <item>" (a possessive) and "<item> of <Name>"; "update Kashvi Kalani introduction
video" (no apostrophe), "update Kashvi intro" (`intro` was not an alias) and "Indian portfolio pictures" (left a stray "pictures") never reached the
update flow, and in a mixed command the talent written WITHOUT a possessive was silently dropped. These tests pin the fix and that everything after
recognition (resolution, file request, approval, upload, read-back) is the unchanged existing workflow.

Same harness as test_personal_assistant_update.py (real FastAPI app + local dev Mongo, throwaway Qzx talents, Cloudinary mocked, nothing sent)."""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import pytest
import pytest_asyncio

from core import _now, db
from personal_assistant import update_items as U
from personal_assistant.parser import parse_message
from test_personal_assistant import (  # noqa: F401  (fixtures + helpers of the existing suite)
    BASE, _add_rows, _isolate_each_test, _jobs, _stage, _talent, alice, anon, bob, cleanup, say, world,
    _real_db_for_this_module,
)
from test_personal_assistant_update import (  # noqa: F401  (the same helpers + storage mock as the update suite)
    FakeStorage, JPEG, MP4, _confirm, _doc, _manifest, _tasks, _upload, storage,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")


def groups(text):
    return [(g["names"], [(i["key"], i["raw"]) for i in g["items"]]) for g in U.parse_update(text)]


# ============================================================================ the exact phrasings from the report
@pytest.mark.parametrize("text, names, key", [
    ("update Kashvi Kalani introduction video", ["Kashvi Kalani"], "video"),
    ("update Kashvi Kalani's introduction video", ["Kashvi Kalani"], "video"),
    ("update Kashvi introduction video", ["Kashvi"], "video"),
    ("update Kashvi Kalani's intro", ["Kashvi Kalani"], "video"),
    ("update Kashvi intro video", ["Kashvi"], "video"),
    ("Update Kashvi's intro video", ["Kashvi"], "video"),
    ("update Kashvi Kalani Indian pictures", ["Kashvi Kalani"], "indian"),
    ("update Kashvi's Western pictures", ["Kashvi"], "western"),
    ("update Kashvi additional portfolio", ["Kashvi"], "portfolio"),
    ("Update Kashvi Western photos", ["Kashvi"], "western"),
    ("Update Kashvi Kalani's Indian pictures", ["Kashvi Kalani"], "indian"),
    ("update the introduction video of Kashvi Kalani", ["Kashvi Kalani"], "video"),
    ("UPDATE KASHVI KALANI INTRO VIDEO.", ["KASHVI KALANI"], "video"),
    ("please update Kashvi intro video please", ["Kashvi"], "video"),
])
def test_every_phrasing_reaches_the_same_intended_update(text, names, key):
    assert groups(text) == [(names, [(key, None)])], text


@pytest.mark.parametrize("phrase, key", [
    ("intro", "video"), ("introduction", "video"), ("intro video", "video"), ("introduction video", "video"), ("introductory video", "video"),
    ("video", "video"), ("Indian pictures", "indian"), ("Indian photos", "indian"), ("Indian images", "indian"), ("Indian portfolio pictures", "indian"),
    ("Indian look pictures", "indian"), ("Indian look", "indian"), ("Western pictures", "western"), ("Western photos", "western"),
    ("Western portfolio pictures", "western"), ("Western look", "western"), ("additional portfolio", "portfolio"), ("portfolio pictures", "portfolio"),
    ("portfolio", "portfolio"), ("additional pictures", "portfolio"), ("extra photos", "portfolio"),
])
def test_media_category_aliases(phrase, key):
    assert groups(f"update Priya Sharma {phrase}") == [(["Priya Sharma"], [(key, None)])], phrase
    assert groups(f"update Priya Sharma's {phrase}") == [(["Priya Sharma"], [(key, None)])], phrase


def test_category_words_are_never_part_of_the_talent_name():
    for text in ("update Kashvi Kalani introduction video", "update Kashvi Kalani Indian portfolio pictures", "update Kashvi additional portfolio",
                 "update Kashvi Kalani's Western photos", "update Kashvi Kalani intro"):
        (names, _items), = groups(text)
        assert not any(w in " ".join(names).lower() for w in ("intro", "video", "indian", "western", "portfolio", "picture", "photo", "additional")), text


# ============================================================================ several talents / categories in one command
def test_multiple_talents_keep_their_own_categories_whatever_the_wording():
    assert groups("Update Kashvi Kalani introduction video and Priya Sharma's Western pictures") == [(["Kashvi Kalani"], [("video", None)]), (["Priya Sharma"], [("western", None)])]
    assert groups("Update Kashvi intro video and Priya Western photos") == [(["Kashvi"], [("video", None)]), (["Priya"], [("western", None)])]
    assert groups("Update Priya Western photos and Kashvi intro video") == [(["Priya"], [("western", None)]), (["Kashvi"], [("video", None)])]
    assert groups("Update Kashvi Kalani Indian pictures, Western pictures and additional portfolio") == [(["Kashvi Kalani"], [("indian", None), ("western", None), ("portfolio", None)])]
    assert groups("Update Kashvi and Priya intro video") == [(["Kashvi", "Priya"], [("video", None)])]
    assert groups("update Kashvi intro video and age") == [(["Kashvi"], [("video", None), ("age", None)])]


def test_a_talent_written_without_a_possessive_is_never_silently_dropped():
    """Regression: 'Update Kashvi Kalani introduction video and Priya Sharma's Western pictures' used to plan ONLY Priya's picture."""
    got = groups("Update Kashvi Kalani introduction video and Priya Sharma's Western pictures")
    assert [g[0] for g in got] == [["Kashvi Kalani"], ["Priya Sharma"]]


def test_field_updates_keep_working_in_every_form():
    assert groups("update Kashvi age 24") == [(["Kashvi"], [("age", "24")])]
    assert groups("update Kashvi Kalani age to 24 and location to Mumbai") == [(["Kashvi Kalani"], [("age", "24"), ("location", "Mumbai")])]
    assert groups("Update Kashvi's age to 24 and location to Mumbai and Priya Sharma's age to 30") == [(["Kashvi"], [("age", "24"), ("location", "Mumbai")]), (["Priya Sharma"], [("age", "30")])]
    assert groups("update Kashvi Kalani WhatsApp group name to Say Age and Height") == [(["Kashvi Kalani"], [("whatsapp_group_name", "Say Age and Height")])]
    assert groups('update Kashvi WhatsApp group name to "Say Age and Height" and age to 24') == [(["Kashvi"], [("whatsapp_group_name", "Say Age and Height"), ("age", "24")])]


# ============================================================================ fail closed
@pytest.mark.parametrize("text", [
    "update Kashvi", "update intro video", "update Kashvi intro video banana split", "update Kashvi location Mumbai", "update", "update 24 intro",
])
def test_anything_not_fully_understood_is_not_guessed_into_an_update(text):
    assert U.parse_update(text) == [], text


def test_the_parser_hands_run_on_updates_to_the_same_planner_path():
    segs = parse_message("update Kashvi Kalani introduction video send the casting call to Vikram Sharma")
    assert [s.intent for s in segs] == ["update", "send"] and segs[0].update_groups[0]["names"] == ["Kashvi Kalani"]
    assert [s.intent for s in parse_message("change disha to approved in amazon now")] == ["move"]


# ============================================================================ planning + the unchanged workflow
@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def k(world):
    r = {}
    r["kashvi"] = await _talent("Qzx Kashvix Kalani", media=[{"id": "iv1", "category": "video", "url": "https://res.example.invalid/iv1", "public_id": "zzz-test/iv1",
                                                            "resource_type": "video", "original_filename": "kashvi-old.mp4", "size": 5, "created_at": _now()}])
    r["other"] = await _talent("Qzx Priyax Sharma")
    r["tw1"] = await _talent("Qzx Meeraz Shah")
    r["tw2"] = await _talent("Qzx Meeraz Iyer")                                        # a SECOND "Meeraz" → the short name is ambiguous
    return r


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _restore(k):
    snap = {n: await db.talents.find_one({"id": i}, {"_id": 0}) for n, i in k.items()}
    yield
    for doc in snap.values():
        if doc:
            await db.talents.replace_one({"id": doc["id"]}, doc)


@pytest.mark.parametrize("text", [
    "update Qzx Kashvix Kalani introduction video", "update Qzx Kashvix Kalani's introduction video", "update Qzx Kashvix introduction video",
    "update Qzx Kashvix Kalani's intro", "update Qzx Kashvix intro video", "UPDATE QZX KASHVIX KALANI INTRO",
])
async def test_each_phrasing_asks_for_the_file_and_uploads_nothing(anon, alice, k, storage, text):
    _o, ts = await _tasks(anon, alice, text)
    assert len(ts) == 1 and ts[0]["kind"] == "update" and ts[0]["status"] == "needs_clarification"
    t = ts[0]
    assert t["params"]["talents"][0]["id"] == k["kashvi"] and [m["category"] for m in t["params"]["updates"]["media"]] == ["video"]
    assert t["slots"][0]["kind"] == "files" and t["slots"][0]["category"] == "video"
    assert storage.calls == [] and (await _doc(k["kashvi"]))["media"][0]["id"] == "iv1"            # recognising the command changed and uploaded nothing


async def test_pictures_and_portfolio_phrasings_map_to_the_right_categories(anon, alice, k):
    for text, cats in (("update Qzx Kashvix Kalani Indian pictures", ["indian"]), ("update Qzx Kashvix Western photos", ["western"]),
                       ("update Qzx Kashvix additional portfolio", ["portfolio"]), ("update Qzx Kashvix Kalani Indian portfolio pictures and Western pictures", ["indian", "western"])):
        _o, (t,) = await _tasks(anon, alice, text)
        assert t["params"]["talents"][0]["id"] == k["kashvi"] and [m["category"] for m in t["params"]["updates"]["media"]] == cats, text
        assert [s["category"] for s in t["slots"] if s["kind"] == "files"] == cats


async def test_a_short_name_resolves_only_when_it_is_unique_otherwise_cards(anon, alice, k):
    _o, (t,) = await _tasks(anon, alice, "update Qzx Meeraz intro video")
    assert t["status"] == "needs_clarification" and t["slots"][0]["kind"] == "talent" and {o["id"] for o in t["slots"][0]["options"]} == {k["tw1"], k["tw2"]}
    assert not any(o.get("best") for o in t["slots"][0]["options"])
    _o, (u,) = await _tasks(anon, alice, "update Qzx Kashvix intro video")                         # unique first name → direct
    assert u["params"]["talents"][0]["id"] == k["kashvi"]


async def test_a_typo_or_sound_alike_is_a_card_never_selected(anon, alice, k, storage):
    for text in ("update Qzx Kashvy Kalani intro video", "update Qzx Kashvi Kalanee introduction video"):
        _o, (t,) = await _tasks(anon, alice, text)
        assert t["status"] == "needs_clarification" and t["slots"][0]["kind"] == "talent", text
        assert not t["params"]["talents"][0].get("id") and storage.calls == []


async def test_two_talents_with_different_categories_keep_their_own_files(anon, alice, k, storage):
    _o, (a, b) = await _tasks(anon, alice, "Update Qzx Kashvix Kalani introduction video and Qzx Priyax Sharma's Western pictures")
    assert (a["params"]["talents"][0]["id"], [m["category"] for m in a["params"]["updates"]["media"]]) == (k["kashvi"], ["video"])
    assert (b["params"]["talents"][0]["id"], [m["category"] for m in b["params"]["updates"]["media"]]) == (k["other"], ["western"])
    a = (await _manifest(anon, alice, a, "video", [("kashvi.mp4", MP4, "video/mp4")])).json()["task"]
    b = (await _manifest(anon, alice, b, "western", [("priya.jpg", JPEG, "image/jpeg")])).json()["task"]
    ra, rb = await _confirm(anon, alice, a), await _confirm(anon, alice, b)
    assert (await _upload(anon, alice, ra, "video", "priya.jpg", JPEG, "image/jpeg")).status_code == 400          # Priya's file can't go to Kashvi's task
    assert (await _upload(anon, alice, ra, "video", "kashvi.mp4", MP4, "video/mp4")).status_code == 200
    assert (await _upload(anon, alice, rb, "western", "priya.jpg", JPEG, "image/jpeg")).status_code == 200
    assert [m["original_filename"] for m in (await _doc(k["kashvi"]))["media"] if m["category"] == "video"] == ["kashvi.mp4"]
    assert [m["original_filename"] for m in (await _doc(k["other"]))["media"] if m["category"] == "western"] == ["priya.jpg"]


async def test_the_existing_approval_upload_and_verification_flow_is_unchanged_for_a_new_phrasing(anon, alice, k, storage):
    _o, (t,) = await _tasks(anon, alice, "update Qzx Kashvix Kalani introduction video")             # the phrasing that used to fail
    task = (await _manifest(anon, alice, t, "video", [("intro.mp4", MP4, "video/mp4")])).json()["task"]
    assert task["status"] == "awaiting_confirmation" and task["fingerprint"] and any("REPLACES kashvi-old.mp4 with intro.mp4" in l for l in task["summary"])
    assert storage.calls == []                                                                         # approval first
    run = await _confirm(anon, alice, task)
    assert run["status"] == "running" and storage.calls == []                                          # approved, but nothing uploaded until a file arrives
    done = (await _upload(anon, alice, run, "video", "intro.mp4", MP4, "video/mp4")).json()["task"]
    assert done["status"] == "completed" and done["outcomes"][0]["detail"] == "intro.mp4" and done["outcomes"][0]["status"] == "uploaded"
    assert [m["original_filename"] for m in (await _doc(k["kashvi"]))["media"] if m["category"] == "video"] == ["intro.mp4"]      # replaced, read back


async def test_an_ordinary_field_update_in_the_new_wording_still_needs_approval(anon, alice, k):
    _o, (t,) = await _tasks(anon, alice, "update Qzx Kashvix Kalani age 24")
    assert t["status"] == "awaiting_confirmation" and any(l.startswith("Age:") and l.endswith("→ 24") for l in t["summary"])
    done = await _confirm(anon, alice, t)
    assert done["status"] == "completed" and (await _doc(k["kashvi"]))["age"] == 24


async def test_ambiguous_or_unsupported_categories_fail_safely_without_asking_for_a_talent_first(anon, alice, k, storage):
    _o, (t,) = await _tasks(anon, alice, "update Qzx Kashvix Kalani pictures")                         # which pictures? three categories exist
    assert t["status"] == "failed" and "Which pictures?" in t["error"] and "Indian look" in t["error"] and not t["slots"]
    _o, (e,) = await _tasks(anon, alice, "update Qzx Kashvix Kalani email")
    assert e["status"] == "failed" and "profile page" in e["error"]
    _o, (g,) = await _tasks(anon, alice, "update the pipeline status")                                  # nothing supported: no "which talent?" question first
    assert g["status"] == "failed" and not g["slots"]
    out = await say(anon, alice, "update Qzx Kashvix Kalani intro video banana split")
    assert out["tasks"] == [] and out["unrecognised"] and storage.calls == []
