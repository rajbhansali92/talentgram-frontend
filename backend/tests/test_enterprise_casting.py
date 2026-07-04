"""Unit and regression tests for the Enterprise Casting Workflow features:
- Immutable client package snapshots
- Lightweight media revision history
- Expanded casting status workflow and history transition logging
- Delivery analytics mapping
"""
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import (
    SUBMISSION_DECISIONS,
    _submission_to_client_shape,
    generate_submission_snapshot,  # deprecated, retained one release
)

def _base_submission(media=None, form_data=None, fv=None):
    return {
        "id": "sub-123",
        "project_id": "p-123",
        "talent_name": "Audrey Hepburn",
        "talent_email": "audrey@classic.com",
        "form_data": form_data or {
            "first_name": "Audrey", "last_name": "Hepburn",
            "age": 29, "height": "5'7\"", "location": "Hollywood",
        },
        "field_visibility": fv or {"age": True, "height": True, "location": True},
        "media": media or [
            {"id": "m1", "category": "image", "url": "https://img.com/1.jpg", "client_visible": True},
            {"id": "m2", "category": "take_1", "url": "https://img.com/2.mp4", "client_visible": True, "label": "Main Audition"},
        ],
    }

# --------------------------------------------------------------------------
# Expanded Statuses
# --------------------------------------------------------------------------
def test_new_statuses_exist_in_submission_decisions():
    assert "ask_to_test" in SUBMISSION_DECISIONS
    assert "shortlisted" in SUBMISSION_DECISIONS
    assert "does_not_work_for_this" in SUBMISSION_DECISIONS
    assert "pending" in SUBMISSION_DECISIONS

# --------------------------------------------------------------------------
# Issue #1/#10 — Always-live client shape (no frozen snapshots).
# The client-facing shape is computed live on every request from the current
# submission, so recruiter visibility/approval changes are reflected everywhere
# immediately. A stale `client_package_snapshot` on the doc is ignored.
# --------------------------------------------------------------------------
def test_client_shape_is_live_from_submission():
    sub = _base_submission()
    shape = _submission_to_client_shape(sub)
    assert shape["name"] == "Audrey Hepburn"
    assert shape["age"] == 29
    assert len(shape["media"]) == 2
    # No frozen-snapshot metadata is attached anymore.
    assert "snapshot_meta" not in shape

def test_client_shape_reflects_live_edits_and_ignores_legacy_snapshot():
    sub = _base_submission()
    # Simulate a legacy frozen snapshot left on the document; it must be ignored.
    sub["client_package_snapshot"] = {"name": "STALE", "age": 1, "media": []}
    # And a subsequent recruiter edit to the live submission.
    sub["form_data"]["first_name"] = "Updated"
    sub["form_data"]["age"] = 40

    shape = _submission_to_client_shape(sub)
    # Live values win — the stale snapshot is never returned.
    assert shape["name"].startswith("Updated")
    assert shape["age"] == 40
    assert len(shape["media"]) == 2

def test_generate_submission_snapshot_deprecated_but_still_callable():
    # Deprecated (Issue #1/#10): retained one release. It must still build a
    # shape for any lingering caller / the dormant /snapshot endpoint, but the
    # renderer no longer reads it (verified above).
    sub = _base_submission()
    snapshot = generate_submission_snapshot(sub, "recruiter@agency.com")
    assert snapshot["name"] == "Audrey Hepburn"
    assert snapshot["snapshot_meta"]["author_email"] == "recruiter@agency.com"


def test_client_shape_hidden_media_never_reaches_client():
    # Per-media client_visible=False (and legacy internal_only) are excluded.
    sub = _base_submission(media=[
        {"id": "m1", "category": "image", "url": "https://img.com/1.jpg", "client_visible": True},
        {"id": "m2", "category": "image", "url": "https://img.com/2.jpg", "client_visible": False},
        {"id": "m3", "category": "image", "url": "https://img.com/3.jpg", "internal_only": True},
    ], fv={"portfolio": True})
    shape = _submission_to_client_shape(sub)
    ids = {m["id"] for m in shape["media"]}
    assert ids == {"m1"}, f"only the client-visible image should reach the client, got {ids}"
