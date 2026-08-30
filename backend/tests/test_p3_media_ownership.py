"""P3 media-ownership migration — classification rules + additive/idempotent contract.

`backend/migrations/p3_media_ownership.py` adds exactly one nested `ownership`
key per media item and nothing else. `classify_item` is pure; these tests
exercise it directly.
"""
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.abspath("backend"))

import core
core.db = None  # migration module does `from core import db`; not used by classify_item

from migrations.p3_media_ownership import (  # noqa: E402
    classify_item, folder_disagrees, VERSION, TAKE_CATEGORIES,
)


def _c(coll, parent, item, talent_id="t1", how="parent.talent_id",
       pid_owner_count=None, pid_norm_cats=None):
    return classify_item(coll, parent, item, talent_id, how,
                         pid_owner_count or Counter(), pid_norm_cats or {})


def test_talent_media_is_owner_type_talent():
    o = _c("talents", {"id": "t1"},
           {"id": "m1", "category": "portfolio", "public_id": "talentgram/talents/t1/portfolio/m1",
            "resource_type": "image", "size": 100},
           talent_id="t1", how="talents_doc")
    assert o["owner_type"] == "talent"
    assert o["owner_id"] == "t1"
    assert o["media_category_normalized"] == "portfolio"
    assert o["media_type"] == "image"
    assert o["conflict"] is None
    assert o["confidence"] == "high"
    assert o["migration_version"] == VERSION


def test_submission_take_is_project_submission():
    o = _c("submissions", {"id": "s1", "project_id": "p1"},
           {"id": "a", "category": "take", "public_id": "talentgram/projects/p1/submissions/s1/a",
            "resource_type": "video", "size": 10})
    assert o["owner_type"] == "project_submission"
    assert o["owner_id"] == "s1"
    assert o["project_id"] == "p1"
    assert o["submission_id"] == "s1"
    assert o["media_category_normalized"] == "take"
    assert o["conflict"] is None


def test_submission_global_category_is_talent_owned():
    o = _c("submissions", {"id": "s1", "project_id": "p1"},
           {"id": "b", "category": "western", "public_id": "talentgram/submissions/s1/b",
            "resource_type": "image", "size": 20},
           talent_id="t9")
    assert o["owner_type"] == "talent"
    assert o["owner_id"] == "t9"
    assert o["media_category_normalized"] == "western"


def test_unresolvable_talent_id_left_unknown_not_guessed():
    o = _c("applications", {"id": "app1", "status": "draft"},
           {"id": "x", "category": "image", "public_id": "talentgram/applications/app1/x",
            "resource_type": "image", "size": 5},
           talent_id=None, how="unresolved")
    assert o["owner_type"] is None
    assert o["owner_id"] is None
    assert o["conflict"] == "talent-owned item with no resolvable talent_id"
    assert o["confidence"] is None


def test_take_on_application_is_a_conflict():
    o = _c("applications", {"id": "app1"},
           {"id": "y", "category": "take_1", "public_id": "talentgram/applications/app1/y",
            "resource_type": "video", "size": 1})
    assert o["owner_type"] is None
    assert "only submissions may own audition takes" in o["conflict"]


def test_unknown_category_is_a_conflict():
    o = _c("submissions", {"id": "s1", "project_id": "p1"},
           {"id": "z", "category": "mystery_cat", "public_id": "talentgram/submissions/s1/z",
            "resource_type": "image", "size": 1})
    assert o["owner_type"] is None
    assert "unrecognised category" in o["conflict"]


def test_nonstandard_categories_normalized_medium_confidence():
    for cat, expect in (("photos", "portfolio"), ("intro", "intro_video")):
        o = _c("submissions", {"id": "s1", "project_id": "p1"},
               {"id": "n", "category": cat, "public_id": "talentgram/projects/p1/submissions/s1/n",
                "resource_type": "image" if cat == "photos" else "video", "size": 1},
               talent_id="t1")
        assert o["owner_type"] == "talent"
        assert o["media_category_normalized"] == expect
        assert o["confidence"] == "medium"
        assert o["owner_source"] == "category:global(normalized)"


def test_shared_copy_flag():
    pid = "talentgram/talents/t1/portfolio/shared"
    counts = Counter({pid: 3})
    o = _c("talents", {"id": "t1"},
           {"id": "m", "category": "portfolio", "public_id": pid, "resource_type": "image", "size": 1},
           talent_id="t1", pid_owner_count=counts)
    assert o["is_shared_copy"] is True

    o2 = _c("submissions", {"id": "s1", "project_id": "p1"},
            {"id": "m2", "category": "portfolio", "public_id": "talentgram/submissions/s1/m2",
             "resource_type": "image", "size": 1, "source_talent_media_id": "lib-1"},
            talent_id="t1")
    assert o2["is_shared_copy"] is True


def test_conflicting_normalized_categories_for_same_public_id():
    pid = "talentgram/submissions/s1/dup"
    o = _c("submissions", {"id": "s1", "project_id": "p1"},
           {"id": "d", "category": "western", "public_id": pid, "resource_type": "image", "size": 1},
           talent_id="t1", pid_norm_cats={pid: {"western", "take"}})
    assert o["owner_type"] is None
    assert "conflicting normalized categories" in o["conflict"]


def test_original_item_fields_are_never_referenced_for_mutation():
    """classify_item only READS the item; the migration spreads {**item, 'ownership': ...}."""
    item = {"id": "m1", "category": "indian", "public_id": "p", "resource_type": "image",
            "size": 42, "url": "https://x", "scope": "submission", "origin": "project"}
    before = dict(item)
    _c("submissions", {"id": "s1", "project_id": "p1"}, item, talent_id="t1")
    assert item == before  # not mutated


def test_folder_vs_db_disagreement_detection():
    talent_owned_in_submission_folder = {
        "owner_type": "talent",
        "cloudinary": {"public_id": "talentgram/submissions/s1/m1"},
    }
    d = folder_disagrees(talent_owned_in_submission_folder)
    assert d and d["db_owner"] == "talent" and d["folder"] == "submissions"

    talent_owned_in_talent_folder = {
        "owner_type": "talent",
        "cloudinary": {"public_id": "talentgram/talents/t1/portfolio/m1"},
    }
    assert folder_disagrees(talent_owned_in_talent_folder) is None
