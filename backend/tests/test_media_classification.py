"""LAYER 1 — pure media-ownership classification unit tests.

Exercises the real ``classify_item()`` from
``backend/migrations/media_ownership_rules.py`` directly.

NO database. NO Cloudinary. NO migration script. NO network. NO filesystem.
NO ``import core``. NO mocking of any of the above — the classifier is a pure
function and these tests treat it as one.

    input state  ->  classify_item()  ->  classification/result

Contract under test (see the module docstring for the authoritative version):

  signature:
    classify_item(coll, parent, item, talent_id, how_tid,
                  pid_owner_count, pid_norm_cats, *, now=None) -> dict

  owner_type in {"talent", "project_submission", None}
  owner_type is None  <=>  conflict is not None   (UNKNOWN — never guessed)

  precedence (folder path is NEVER consulted):
    1. forced-conflict pre-conditions, in order:
         a. take-category on a non-"submissions" document
         b. unrecognised category
         c. same public_id carrying >1 normalized category
    2. otherwise `category` alone picks owner_type:
         take*  -> project_submission   (owner_id = submission id)
         else   -> talent               (owner_id = resolved talent_id)
       missing project_id / talent_id can only DEMOTE to a conflict; it never
       flips owner_type and is never inferred from unrelated fields.
"""
import copy
import os
import sys
from collections import Counter

import pytest

# `backend/` on the path (matches the repo's existing test convention), resolved
# from this file's location so it is CWD-independent. This is the ONLY setup —
# no core import, no env, no DB, no Cloudinary, no network, no fs I/O.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from migrations.media_ownership_rules import (
    classify_item,
    folder_disagrees,
    VERSION,
    CATEGORY_TO_NORMALIZED,
    CATEGORY_NORMALIZE,
    TAKE_CATEGORIES,
    MEDIUM_CONFIDENCE_RESOLUTIONS,
    OWNER_TYPE_TALENT,
    OWNER_TYPE_PROJECT_SUBMISSION,
)

FIXED_NOW = "2026-01-01T00:00:00+00:00"

# Global (talent-owned) categories = every key that normalizes to something
# other than "take".
GLOBAL_CATEGORIES = sorted(c for c, n in CATEGORY_TO_NORMALIZED.items() if n != "take")


def _classify(coll, parent, item, *, talent_id="t-1", how_tid="parent.talent_id",
              pid_owner_count=None, pid_norm_cats=None, now=FIXED_NOW):
    return classify_item(
        coll, parent, item, talent_id, how_tid,
        pid_owner_count if pid_owner_count is not None else Counter(),
        pid_norm_cats if pid_norm_cats is not None else {},
        now=now,
    )


# ---------------------------------------------------------------------------
# A / C — GLOBAL TALENT MEDIA (clear, unambiguous talent ownership)
# ---------------------------------------------------------------------------
def test_classify_global_talent_media_on_talents_doc():
    parent = {"id": "talent-42"}
    item = {"id": "m1", "category": "portfolio", "public_id": "talentgram/talents/talent-42/portfolio/m1",
            "resource_type": "image", "size": 1234}
    o = _classify("talents", parent, item, talent_id="talent-42", how_tid="talents_doc")

    assert o["owner_type"] == OWNER_TYPE_TALENT
    assert o["owner_id"] == "talent-42"
    assert o["talent_id"] == "talent-42"
    assert o["project_id"] is None
    assert o["application_id"] is None
    assert o["submission_id"] is None
    assert o["media_category_normalized"] == "portfolio"
    assert o["media_type"] == "image"
    assert o["owner_source"] == "category:global"
    assert o["conflict"] is None
    assert o["confidence"] == "high"
    assert o["migration_version"] == VERSION


def test_classify_global_talent_media_carried_on_a_submission():
    """A talent's profile photo living inside a submission's media[] is still
    talent-owned — the canonical asset belongs to the talent."""
    parent = {"id": "sub-9", "project_id": "proj-1", "talent_id": "talent-7"}
    item = {"id": "m2", "category": "western", "public_id": "talentgram/submissions/sub-9/m2",
            "resource_type": "image", "size": 50}
    o = _classify("submissions", parent, item, talent_id="talent-7")

    assert o["owner_type"] == OWNER_TYPE_TALENT
    assert o["owner_id"] == "talent-7"
    assert o["submission_id"] == "sub-9"   # provenance, not ownership
    assert o["project_id"] is None         # only set for project_submission owners
    assert o["media_category_normalized"] == "western"
    assert o["conflict"] is None


# ---------------------------------------------------------------------------
# B / D — PROJECT AUDITION MEDIA (clear, unambiguous project/submission ownership)
# ---------------------------------------------------------------------------
def test_classify_project_audition_take():
    parent = {"id": "sub-100", "project_id": "proj-55", "talent_id": "talent-3"}
    item = {"id": "a1", "category": "take", "resource_type": "video", "size": 999,
            "public_id": "talentgram/projects/proj-55/auditions/talent-3_x/submission_sub-100/take_abc12345"}
    o = _classify("submissions", parent, item, talent_id="talent-3")

    assert o["owner_type"] == OWNER_TYPE_PROJECT_SUBMISSION
    assert o["owner_id"] == "sub-100"
    assert o["project_id"] == "proj-55"
    assert o["submission_id"] == "sub-100"
    assert o["talent_id"] == "talent-3"    # recorded for reference, ownership stays project
    assert o["media_category_normalized"] == "take"
    assert o["media_type"] == "video"
    assert o["owner_source"] == "category:take"
    assert o["conflict"] is None
    assert o["confidence"] == "high"


@pytest.mark.parametrize("take_cat", sorted(TAKE_CATEGORIES))
def test_every_take_category_is_project_submission(take_cat):
    parent = {"id": "s", "project_id": "p"}
    item = {"id": "x", "category": take_cat, "public_id": "talentgram/submissions/s/x", "resource_type": "video"}
    o = _classify("submissions", parent, item)
    assert o["owner_type"] == OWNER_TYPE_PROJECT_SUBMISSION
    assert o["owner_id"] == "s"
    assert o["media_category_normalized"] == "take"


@pytest.mark.parametrize("cat", GLOBAL_CATEGORIES)
def test_every_non_take_category_is_talent_owned(cat):
    parent = {"id": "s", "project_id": "p", "talent_id": "t"}
    coll = "talents" if cat in {"portfolio", "indian", "western", "video"} else "submissions"
    item = {"id": "x", "category": cat, "public_id": "talentgram/x/x", "resource_type": "image"}
    o = _classify(coll, parent, item, talent_id="t")
    assert o["owner_type"] == OWNER_TYPE_TALENT
    assert o["media_category_normalized"] == CATEGORY_TO_NORMALIZED[cat]


# ---------------------------------------------------------------------------
# E — CONFLICT  (UNKNOWN, per the real contract — never guessed)
# ---------------------------------------------------------------------------
def test_take_category_on_application_is_conflict():
    parent = {"id": "app-1"}
    item = {"id": "y", "category": "take_1", "public_id": "talentgram/applications/app-1/y", "resource_type": "video"}
    o = _classify("applications", parent, item)
    assert o["owner_type"] is None
    assert o["owner_id"] is None
    assert o["confidence"] is None
    assert "only submissions may own audition takes" in o["conflict"]


def test_take_category_on_talents_doc_is_conflict():
    parent = {"id": "t-1"}
    item = {"id": "y", "category": "take", "public_id": "talentgram/talents/t-1/y", "resource_type": "video"}
    o = _classify("talents", parent, item)
    assert o["owner_type"] is None
    assert "only submissions may own audition takes" in o["conflict"]


def test_unrecognised_category_is_conflict():
    parent = {"id": "s", "project_id": "p", "talent_id": "t"}
    item = {"id": "z", "category": "totally_made_up", "public_id": "talentgram/submissions/s/z", "resource_type": "image"}
    o = _classify("submissions", parent, item, talent_id="t")
    assert o["owner_type"] is None
    assert "unrecognised category 'totally_made_up'" in o["conflict"]
    assert o["media_category_normalized"] is None


def test_missing_category_is_conflict():
    parent = {"id": "s", "project_id": "p", "talent_id": "t"}
    item = {"id": "z", "public_id": "talentgram/submissions/s/z", "resource_type": "image"}
    o = _classify("submissions", parent, item, talent_id="t")
    assert o["owner_type"] is None
    assert "unrecognised category None" in o["conflict"]


def test_same_public_id_with_conflicting_normalized_categories_is_conflict():
    pid = "talentgram/submissions/s/dup"
    parent = {"id": "s", "project_id": "p", "talent_id": "t"}
    item = {"id": "d", "category": "western", "public_id": pid, "resource_type": "image"}
    o = _classify("submissions", parent, item, talent_id="t",
                  pid_norm_cats={pid: {"western", "take"}})
    assert o["owner_type"] is None
    assert "conflicting normalized categories" in o["conflict"]
    assert "['take', 'western']" in o["conflict"]


def test_portfolio_and_image_are_the_SAME_normalized_category_not_a_conflict():
    """`portfolio` (talent label) and `image` (submission label) both normalize
    to 'portfolio' — a public_id seen under both must NOT be a conflict."""
    pid = "talentgram/submissions/s/x"
    parent = {"id": "s", "project_id": "p", "talent_id": "t"}
    item = {"id": "x", "category": "image", "public_id": pid, "resource_type": "image"}
    o = _classify("submissions", parent, item, talent_id="t",
                  pid_norm_cats={pid: {"portfolio"}})
    assert o["owner_type"] == OWNER_TYPE_TALENT
    assert o["conflict"] is None


# ---------------------------------------------------------------------------
# F — MISSING REFERENCES  (do not infer ownership from unrelated fields)
# ---------------------------------------------------------------------------
def test_missing_talent_reference_is_conflict_not_guessed():
    parent = {"id": "app-1", "status": "draft"}
    item = {"id": "x", "category": "image", "public_id": "talentgram/applications/app-1/x", "resource_type": "image"}
    o = _classify("applications", parent, item, talent_id=None, how_tid="unresolved")
    assert o["owner_type"] is None
    assert o["owner_id"] is None
    assert o["confidence"] is None
    assert o["conflict"] == "talent-owned item with no resolvable talent_id"


def test_missing_project_reference_on_take_is_conflict():
    parent = {"id": "sub-77"}  # no project_id
    item = {"id": "a", "category": "take", "public_id": "talentgram/submissions/sub-77/a", "resource_type": "video"}
    o = _classify("submissions", parent, item, talent_id="t-1")
    assert o["owner_type"] is None
    assert o["conflict"] == "take item on a submission with no project_id"


def test_does_not_infer_talent_ownership_from_source_talent_media_id():
    """`source_talent_media_id` marks a shared copy — it must NOT be used to
    manufacture a talent owner when talent_id is unresolved."""
    parent = {"id": "app-1"}
    item = {"id": "x", "category": "indian", "public_id": "talentgram/applications/app-1/x",
            "resource_type": "image", "source_talent_media_id": "lib-999"}
    o = _classify("applications", parent, item, talent_id=None, how_tid="unresolved")
    assert o["owner_type"] is None
    assert o["conflict"] == "talent-owned item with no resolvable talent_id"
    assert o["is_shared_copy"] is True  # the marker is still recorded, just not trusted for ownership


def test_does_not_infer_project_ownership_from_submission_id_field_on_non_take():
    """A global-category item carries `submission_id` for provenance; that must
    not turn it into a project_submission owner."""
    parent = {"id": "sub-1", "project_id": "proj-1"}
    item = {"id": "x", "category": "western", "public_id": "talentgram/submissions/sub-1/x",
            "resource_type": "image", "submission_id": "sub-1", "project_id": "proj-1"}
    o = _classify("submissions", parent, item, talent_id="t-1")
    assert o["owner_type"] == OWNER_TYPE_TALENT
    assert o["project_id"] is None  # not a project owner


# ---------------------------------------------------------------------------
# G — ABANDONED DRAFT  (the exact condition behind the 11 UNKNOWN P3 records)
# ---------------------------------------------------------------------------
def test_abandoned_draft_application_returns_unknown():
    """Reproduces the 10 QA-artifact + 1 real draft P3 UNKNOWNs: a draft
    application/submission with media but no talent row ever created."""
    parent = {"id": "app-draft", "status": "draft", "talent_email": "p2.race.123@gmail.com"}
    item = {"id": "x", "category": "intro_video", "public_id": "talentgram/applications/app-draft/x",
            "resource_type": "video", "size": 42}
    o = _classify("applications", parent, item, talent_id=None, how_tid="unresolved")
    assert o["owner_type"] is None
    assert o["owner_id"] is None
    assert o["confidence"] is None
    assert o["conflict"] == "talent-owned item with no resolvable talent_id"


def test_abandoned_draft_submission_intro_video_returns_unknown():
    parent = {"id": "sub-draft", "status": "draft", "project_id": "proj-x"}
    item = {"id": "iv", "category": "intro_video", "public_id": "talentgram/submissions/sub-draft/iv",
            "resource_type": "video"}
    o = _classify("submissions", parent, item, talent_id=None, how_tid="unresolved")
    assert o["owner_type"] is None
    assert o["conflict"] == "talent-owned item with no resolvable talent_id"


# ---------------------------------------------------------------------------
# H — FOLDER != OWNERSHIP
# ---------------------------------------------------------------------------
def test_folder_saying_project_does_not_override_talent_ownership():
    """public_id folder = talentgram/projects/... but the DB context (talents
    collection, portfolio category) is authoritative -> talent."""
    parent = {"id": "talent-1"}
    item = {"id": "m", "category": "portfolio", "resource_type": "image",
            "public_id": "talentgram/projects/proj-9/auditions/talent-1_x/submission_s1/some-uuid"}
    o = _classify("talents", parent, item, talent_id="talent-1", how_tid="talents_doc")
    assert o["owner_type"] == OWNER_TYPE_TALENT
    assert o["owner_id"] == "talent-1"
    # the folder mismatch is only REPORTED, never enforced:
    assert folder_disagrees(o) == {
        "public_id": item["public_id"], "db_owner": OWNER_TYPE_TALENT, "folder": "projects",
    }


def test_folder_saying_talent_does_not_override_project_ownership():
    parent = {"id": "sub-1", "project_id": "proj-1"}
    item = {"id": "a", "category": "take", "resource_type": "video",
            "public_id": "talentgram/talents/talent-9_x/some_take"}
    o = _classify("submissions", parent, item, talent_id="talent-9")
    assert o["owner_type"] == OWNER_TYPE_PROJECT_SUBMISSION
    assert o["owner_id"] == "sub-1"
    assert folder_disagrees(o) == {
        "public_id": item["public_id"], "db_owner": OWNER_TYPE_PROJECT_SUBMISSION, "folder": "talents",
    }


def test_classify_item_never_reads_folder_path_for_ownership():
    """Same authoritative inputs, wildly different folder strings -> identical
    ownership decision."""
    parent = {"id": "t-1"}
    base = {"id": "m", "category": "indian", "resource_type": "image"}
    a = _classify("talents", parent, {**base, "public_id": "talentgram/talents/t-1/indian/m"},
                  talent_id="t-1", how_tid="talents_doc")
    b = _classify("talents", parent, {**base, "public_id": "talentgram/projects/p/x/y/z"},
                  talent_id="t-1", how_tid="talents_doc")
    c = _classify("talents", parent, {**base, "public_id": "s3://elsewhere/whatever"},
                  talent_id="t-1", how_tid="talents_doc")
    for o in (a, b, c):
        assert o["owner_type"] == OWNER_TYPE_TALENT
        assert o["owner_id"] == "t-1"
    assert a["owner_source"] == b["owner_source"] == c["owner_source"]


def test_folder_disagrees_returns_none_when_folder_matches_owner():
    o = {"owner_type": OWNER_TYPE_TALENT, "cloudinary": {"public_id": "talentgram/talents/t-1/portfolio/m"}}
    assert folder_disagrees(o) is None


# ---------------------------------------------------------------------------
# I — SHARED GLOBAL MEDIA (referenced by multiple projects -> still talent)
# ---------------------------------------------------------------------------
def test_shared_global_media_is_talent_owned_not_project():
    pid = "talentgram/talents/t-1/portfolio/shared"
    parent = {"id": "sub-1", "project_id": "proj-1", "talent_id": "t-1"}
    item = {"id": "m", "category": "portfolio", "public_id": pid, "resource_type": "image"}
    # same asset referenced by 4 media rows across projects
    o = _classify("submissions", parent, item, talent_id="t-1",
                  pid_owner_count=Counter({pid: 4}))
    assert o["owner_type"] == OWNER_TYPE_TALENT
    assert o["owner_id"] == "t-1"
    assert o["is_shared_copy"] is True
    assert o["project_id"] is None


def test_shared_copy_flag_from_source_talent_media_id():
    parent = {"id": "sub-1", "project_id": "proj-1", "talent_id": "t-1"}
    item = {"id": "m", "category": "portfolio", "public_id": "talentgram/submissions/sub-1/m",
            "resource_type": "image", "source_talent_media_id": "lib-1"}
    o = _classify("submissions", parent, item, talent_id="t-1")
    assert o["is_shared_copy"] is True
    assert o["owner_type"] == OWNER_TYPE_TALENT


def test_not_shared_when_single_reference_and_no_library_link():
    pid = "talentgram/talents/t-1/portfolio/solo"
    parent = {"id": "t-1"}
    item = {"id": "m", "category": "portfolio", "public_id": pid, "resource_type": "image"}
    o = _classify("talents", parent, item, talent_id="t-1", how_tid="talents_doc",
                  pid_owner_count=Counter({pid: 1}))
    assert o["is_shared_copy"] is False


# ---------------------------------------------------------------------------
# J — PROJECT AUDITION DOES NOT BECOME GLOBAL (regression)
# ---------------------------------------------------------------------------
def test_audition_take_stays_project_even_when_talent_id_present():
    parent = {"id": "sub-1", "project_id": "proj-1", "talent_id": "talent-777"}
    item = {"id": "a", "category": "take", "resource_type": "video",
            "public_id": "talentgram/projects/proj-1/auditions/talent-777_x/submission_sub-1/take_deadbeef"}
    o = _classify("submissions", parent, item, talent_id="talent-777", how_tid="parent.talent_id")

    assert o["owner_type"] == OWNER_TYPE_PROJECT_SUBMISSION      # NOT talent
    assert o["owner_id"] == "sub-1"                              # NOT talent-777
    assert o["project_id"] == "proj-1"
    assert o["talent_id"] == "talent-777"                        # recorded, not owning
    assert o["media_category_normalized"] == "take"
    assert o["owner_source"] == "category:take"


@pytest.mark.parametrize("take_cat", sorted(TAKE_CATEGORIES))
def test_all_take_categories_stay_project_with_talent_id_present(take_cat):
    parent = {"id": "s", "project_id": "p", "talent_id": "t"}
    item = {"id": "x", "category": take_cat, "public_id": "talentgram/submissions/s/x", "resource_type": "video"}
    o = _classify("submissions", parent, item, talent_id="t")
    assert o["owner_type"] == OWNER_TYPE_PROJECT_SUBMISSION


# ---------------------------------------------------------------------------
# K — INPUT IMMUTABILITY
# ---------------------------------------------------------------------------
def test_classify_item_does_not_mutate_its_inputs():
    coll = "submissions"
    parent = {"id": "sub-1", "project_id": "proj-1", "talent_id": "t-1",
              "media": [{"id": "m1"}], "nested": {"a": [1, 2, 3]}}
    item = {"id": "m1", "category": "western", "public_id": "talentgram/submissions/sub-1/m1",
            "resource_type": "image", "size": 100, "url": "https://x", "scope": "submission",
            "origin": "project", "source_talent_media_id": None, "tags": ["t"]}
    pid_owner_count = Counter({"talentgram/submissions/sub-1/m1": 2})
    pid_norm_cats = {"talentgram/submissions/sub-1/m1": {"western"}}

    parent_before = copy.deepcopy(parent)
    item_before = copy.deepcopy(item)
    poc_before = copy.deepcopy(pid_owner_count)
    pnc_before = copy.deepcopy(pid_norm_cats)

    classify_item(coll, parent, item, "t-1", "parent.talent_id", pid_owner_count, pid_norm_cats, now=FIXED_NOW)

    assert parent == parent_before
    assert item == item_before
    assert pid_owner_count == poc_before
    assert pid_norm_cats == pnc_before


def test_classify_item_return_value_is_not_an_alias_of_input():
    item = {"id": "m", "category": "portfolio", "public_id": "p", "resource_type": "image"}
    o = classify_item("talents", {"id": "t"}, item, "t", "talents_doc", Counter(), {}, now=FIXED_NOW)
    assert o is not item
    o["owner_type"] = "MUTATED"
    assert item.get("owner_type") is None


# ---------------------------------------------------------------------------
# L — DETERMINISM
# ---------------------------------------------------------------------------
def test_classify_item_is_deterministic():
    args = (
        "submissions",
        {"id": "sub-1", "project_id": "proj-1", "talent_id": "t-1"},
        {"id": "m", "category": "take", "public_id": "talentgram/submissions/sub-1/m", "resource_type": "video"},
        "t-1", "parent.talent_id", Counter({"talentgram/submissions/sub-1/m": 1}), {},
    )
    a = classify_item(*args, now=FIXED_NOW)
    b = classify_item(*args, now=FIXED_NOW)
    assert a == b


def test_only_migrated_at_varies_without_a_pinned_now():
    args = (
        "talents", {"id": "t-1"},
        {"id": "m", "category": "indian", "public_id": "talentgram/talents/t-1/indian/m", "resource_type": "image"},
        "t-1", "talents_doc", Counter(), {},
    )
    a = classify_item(*args)
    b = classify_item(*args)
    a.pop("migrated_at")
    b.pop("migrated_at")
    assert a == b  # everything except the timestamp stamp is identical


# ---------------------------------------------------------------------------
# §5 — PRECEDENCE boundaries
# ---------------------------------------------------------------------------
def test_precedence_take_on_wrong_collection_beats_pid_category_conflict():
    pid = "talentgram/talents/t-1/x"
    o = _classify("talents", {"id": "t-1"},
                  {"id": "x", "category": "take", "public_id": pid, "resource_type": "video"},
                  pid_norm_cats={pid: {"take", "western"}})
    assert "only submissions may own audition takes" in o["conflict"]


def test_precedence_unrecognised_category_beats_pid_category_conflict():
    pid = "talentgram/submissions/s/x"
    o = _classify("submissions", {"id": "s", "project_id": "p", "talent_id": "t"},
                  {"id": "x", "category": "bogus", "public_id": pid, "resource_type": "image"},
                  talent_id="t", pid_norm_cats={pid: {"western", "indian"}})
    assert "unrecognised category 'bogus'" in o["conflict"]


def test_precedence_category_decides_owner_type_before_reference_checks():
    """A take with NO project_id: category still routes it to project_submission
    first; only THEN is it demoted to conflict for the missing project_id. It
    never falls through to 'talent' just because talent_id is present."""
    o = _classify("submissions", {"id": "sub-1"},   # no project_id
                  {"id": "a", "category": "take", "public_id": "talentgram/submissions/sub-1/a", "resource_type": "video"},
                  talent_id="talent-present")
    assert o["owner_type"] is None                       # demoted to conflict
    assert o["conflict"] == "take item on a submission with no project_id"
    assert o["owner_id"] is None                         # NOT "talent-present"


def test_precedence_global_category_with_missing_talent_never_becomes_project():
    o = _classify("submissions", {"id": "sub-1", "project_id": "proj-1"},
                  {"id": "x", "category": "western", "public_id": "talentgram/submissions/sub-1/x", "resource_type": "image"},
                  talent_id=None, how_tid="unresolved")
    assert o["owner_type"] is None
    assert o["conflict"] == "talent-owned item with no resolvable talent_id"
    assert o["project_id"] is None  # never inherits the submission's project


@pytest.mark.parametrize("how_tid,expected_confidence", [
    ("talents_doc", "high"),
    ("item.talent_id", "high"),
    ("parent.talent_id", "high"),
    ("talent_email", "medium"),
    ("source_submission", "medium"),
    ("source_submission_email", "medium"),
])
def test_confidence_reflects_how_the_talent_id_was_resolved(how_tid, expected_confidence):
    coll = "talents" if how_tid == "talents_doc" else "submissions"
    parent = {"id": "x", "project_id": "p", "talent_id": "t"}
    item = {"id": "m", "category": "portfolio", "public_id": "talentgram/x/m", "resource_type": "image"}
    o = _classify(coll, parent, item, talent_id="t", how_tid=how_tid)
    assert o["confidence"] == expected_confidence
    assert o["conflict"] is None


# ---------------------------------------------------------------------------
# category normalization (photos/intro) — medium confidence, still talent-owned
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw_cat,normalized", sorted(CATEGORY_NORMALIZE.items()))
def test_nonstandard_categories_normalized_and_flagged_medium(raw_cat, normalized):
    parent = {"id": "sub-1", "project_id": "proj-1", "talent_id": "t-1"}
    item = {"id": "m", "category": raw_cat, "resource_type": "image" if raw_cat == "photos" else "video",
            "public_id": "talentgram/projects/proj-1/submissions/sub-1/m"}
    o = _classify("submissions", parent, item, talent_id="t-1", how_tid="parent.talent_id")
    assert o["owner_type"] == OWNER_TYPE_TALENT
    assert o["media_category_normalized"] == CATEGORY_TO_NORMALIZED[normalized]
    assert o["owner_source"] == "category:global(normalized)"
    assert o["confidence"] == "medium"     # normalization demotes confidence
    assert o["conflict"] is None


# ---------------------------------------------------------------------------
# output shape / provenance fields
# ---------------------------------------------------------------------------
def test_media_type_from_resource_type_and_content_type():
    p = {"id": "t"}
    assert _classify("talents", p, {"category": "portfolio", "resource_type": "video", "public_id": "x"},
                     talent_id="t")["media_type"] == "video"
    assert _classify("talents", p, {"category": "portfolio", "content_type": "video/mp4", "public_id": "x"},
                     talent_id="t")["media_type"] == "video"
    assert _classify("talents", p, {"category": "portfolio", "public_id": "x"},
                     talent_id="t")["media_type"] == "image"  # default


def test_cloudinary_block_mirrors_the_item_without_transforming_it():
    item = {"id": "m", "category": "portfolio", "public_id": "talentgram/talents/t/portfolio/m",
            "resource_type": "image", "asset_id": "cld-asset-1", "format": "jpg", "size": 4242}
    o = _classify("talents", {"id": "t"}, item, talent_id="t", how_tid="talents_doc")
    assert o["cloudinary"] == {
        "public_id": "talentgram/talents/t/portfolio/m",
        "asset_id": "cld-asset-1",
        "resource_type": "image",
        "format": "jpg",
        "bytes": 4242,
    }


def test_bytes_falls_back_from_size_to_bytes_key():
    o1 = _classify("talents", {"id": "t"},
                   {"category": "portfolio", "public_id": "x", "resource_type": "image", "size": 10}, talent_id="t")
    o2 = _classify("talents", {"id": "t"},
                   {"category": "portfolio", "public_id": "x", "resource_type": "image", "bytes": 20}, talent_id="t")
    assert o1["cloudinary"]["bytes"] == 10
    assert o2["cloudinary"]["bytes"] == 20


# ---------------------------------------------------------------------------
# residual branch coverage
# ---------------------------------------------------------------------------
def test_normalized_category_with_indirect_talent_resolution_stays_medium():
    """normalization demotes to medium; an already-medium confidence (indirect
    talent resolution) must not be 're-processed' or bumped."""
    parent = {"id": "sub-1", "project_id": "proj-1", "talent_id": "t"}
    item = {"id": "m", "category": "photos", "public_id": "talentgram/projects/proj-1/submissions/sub-1/m",
            "resource_type": "image"}
    o = _classify("submissions", parent, item, talent_id="t", how_tid="talent_email")
    assert o["confidence"] == "medium"
    assert o["conflict"] is None


def test_is_shared_copy_false_when_public_id_missing():
    o = _classify("talents", {"id": "t"},
                  {"id": "m", "category": "portfolio", "resource_type": "image"},  # no public_id
                  talent_id="t", how_tid="talents_doc",
                  pid_owner_count=Counter({None: 5}))  # a None key must not count as "shared"
    assert o["is_shared_copy"] is False
    assert o["cloudinary"]["public_id"] is None


def test_folder_disagrees_ignores_non_talentgram_public_ids():
    assert folder_disagrees({"owner_type": OWNER_TYPE_TALENT,
                             "cloudinary": {"public_id": "s3://bucket/whatever"}}) is None
    assert folder_disagrees({"owner_type": OWNER_TYPE_TALENT, "cloudinary": {}}) is None
    assert folder_disagrees({"owner_type": OWNER_TYPE_TALENT,
                             "cloudinary": {"public_id": "talentgram"}}) is None  # no "/" segment


def test_conflict_item_still_reports_provenance_and_shape():
    """A conflict clears owner_* but the sub-doc is still fully shaped (so the
    caller can write it and a human can see the media_type / cloudinary / etc.)."""
    o = _classify("applications", {"id": "app-1"},
                  {"id": "x", "category": "image", "public_id": "talentgram/applications/app-1/x",
                   "resource_type": "image", "asset_id": "cld-1", "size": 9},
                  talent_id=None, how_tid="unresolved")
    assert o["owner_type"] is None and o["owner_id"] is None and o["owner_source"] is None
    assert o["confidence"] is None
    assert o["conflict"]
    # still shaped:
    assert o["application_id"] == "app-1"
    assert o["media_type"] == "image"
    assert o["media_category_normalized"] == "portfolio"
    assert o["cloudinary"]["asset_id"] == "cld-1"
    assert o["migration_version"] == VERSION
