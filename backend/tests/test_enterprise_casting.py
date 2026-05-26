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
    generate_submission_snapshot,
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
# Immutable Client Package Snapshots
# --------------------------------------------------------------------------
def test_snapshot_freezes_shape():
    sub = _base_submission()
    snapshot = generate_submission_snapshot(sub, "recruiter@agency.com")
    
    assert snapshot["name"] == "Audrey Hepburn"
    assert snapshot["age"] == 29
    assert len(snapshot["media"]) == 2
    assert snapshot["snapshot_meta"]["author_email"] == "recruiter@agency.com"
    assert "generated_at" in snapshot["snapshot_meta"]

def test_submission_to_client_shape_bypasses_live_data_using_snapshot():
    sub = _base_submission()
    snapshot = generate_submission_snapshot(sub, "recruiter@agency.com")
    
    # We mutate the live submission doc significantly (simulate subsequent admin overrides)
    sub["client_package_snapshot"] = snapshot
    sub["form_data"]["first_name"] = "Different Name"
    sub["form_data"]["age"] = 99
    sub["media"] = []  # Empty out active media
    
    # Resolving client shape must STILL yield the original frozen snapshot details
    shape = _submission_to_client_shape(sub)
    assert shape["name"] == "Audrey Hepburn"
    assert shape["age"] == 29
    assert len(shape["media"]) == 2
    assert shape["snapshot_meta"]["author_email"] == "recruiter@agency.com"
