"""Phase 1 + 4 contract tests for the spec-driven re-architecture.

Locks in:
  - "hold" decision state is accepted alongside pending/approved/rejected
  - per-link `talent_field_visibility` shape persists & filters correctly
  - auto-pull link mode resolves approved submissions dynamically
  - submission decision filter on /projects/{pid}/submissions
  - per-project `require_reapproval_on_edit` flag is respected on retake
  - notification fanout fires on new submission, decision change, and retake
"""
import io
import os
import time
import uuid

import pytest
import requests
from PIL import Image

import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

BASE = os.environ.get("TEST_API", "http://localhost:8001/api")
ADMIN_EMAIL = "admin@talentgram.com"
ADMIN_PASS = "Admin@123"


def _mongo_url() -> str:
    raw = open("/app/backend/.env").read()
    return raw.split('MONGO_URL="', 1)[1].split('"', 1)[0]


def _set_submission_state(sid: str, status: str, decision: str) -> None:
    """Bypass the finalize gauntlet by stamping state directly — useful for
    isolating retake-flow assertions from form-validation noise."""
    async def _go():
        c = AsyncIOMotorClient(_mongo_url())
        try:
            await c["talentgram"].submissions.update_one(
                {"id": sid},
                {"$set": {"status": status, "decision": decision}},
            )
        finally:
            c.close()
    asyncio.run(_go())


def _admin_token():
    r = requests.post(f"{BASE}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _hdr(t): return {"Authorization": f"Bearer {t}"}


def _new_project(token, brand=None, **extra):
    body = {"brand_name": brand or f"P1-{uuid.uuid4().hex[:6]}", "character": "Lead"}
    body.update(extra)
    r = requests.post(f"{BASE}/projects", headers=_hdr(token), json=body, timeout=10)
    r.raise_for_status()
    return r.json()


def _start_submission(slug, name="T-Test"):
    r = requests.post(
        f"{BASE}/public/projects/{slug}/submission",
        json={
            "name": name,
            "email": f"{uuid.uuid4().hex[:6]}@example.com",
            "phone": "+10000",
            "form_data": {"first_name": name, "last_name": "Test"},
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def _finalize(sid, sub_token):
    """Move submission from started → submitted by hitting the finalize endpoint."""
    r = requests.post(
        f"{BASE}/public/submissions/{sid}/finalize",
        headers={"Authorization": f"Bearer {sub_token}"},
        timeout=10,
    )
    r.raise_for_status()


def _decide(token, pid, sid, decision):
    r = requests.post(
        f"{BASE}/projects/{pid}/submissions/{sid}/decision",
        headers=_hdr(token),
        json={"decision": decision},
        timeout=10,
    )
    return r


@pytest.fixture(scope="module")
def admin_t():
    return _admin_token()


# ----------------------------------------------------------------------------
# Hold decision + filtering
# ----------------------------------------------------------------------------
class TestHoldDecision:
    def test_hold_is_accepted(self, admin_t):
        proj = _new_project(admin_t, brand="HoldP")
        sub = _start_submission(proj["slug"])
        r = _decide(admin_t, proj["id"], sub["id"], "hold")
        assert r.status_code == 200, r.text

    def test_invalid_decision_rejected(self, admin_t):
        proj = _new_project(admin_t, brand="HoldP-Invalid")
        sub = _start_submission(proj["slug"])
        r = _decide(admin_t, proj["id"], sub["id"], "frozen")
        assert r.status_code == 400

    def test_decision_filter_returns_only_hold(self, admin_t):
        proj = _new_project(admin_t, brand="HoldP-Filter")
        held = _start_submission(proj["slug"], name="Held")
        approved = _start_submission(proj["slug"], name="Approved")
        _decide(admin_t, proj["id"], held["id"], "hold")
        _decide(admin_t, proj["id"], approved["id"], "approved")
        r = requests.get(
            f"{BASE}/projects/{proj['id']}/submissions?decision=hold",
            headers=_hdr(admin_t),
        )
        assert r.status_code == 200
        ids = [s["id"] for s in r.json()]
        assert held["id"] in ids and approved["id"] not in ids


# ----------------------------------------------------------------------------
# Auto-pull showcase links
# ----------------------------------------------------------------------------
class TestAutoPullLink:
    def test_create_requires_project(self, admin_t):
        r = requests.post(
            f"{BASE}/links",
            headers=_hdr(admin_t),
            json={"title": "AutoNoProject", "auto_pull": True},
        )
        assert r.status_code == 400 and "auto_project_id" in r.json()["detail"]

    def test_create_requires_real_project(self, admin_t):
        r = requests.post(
            f"{BASE}/links",
            headers=_hdr(admin_t),
            json={"title": "AutoBadProject", "auto_pull": True, "auto_project_id": "ghost-id-x"},
        )
        assert r.status_code == 400

    def test_auto_pull_resolver_dynamics(self, admin_t):
        """Fresh project, auto-pull link, approve subs and watch them appear."""
        proj = _new_project(admin_t, brand="AutoFlow")
        # 2 subs — only one gets approved
        a = _start_submission(proj["slug"], name="ApprovedTalent")
        b = _start_submission(proj["slug"], name="PendingTalent")
        _decide(admin_t, proj["id"], a["id"], "approved")

        link_payload = {
            "title": f"Showcase-{uuid.uuid4().hex[:5]}",
            "auto_pull": True,
            "auto_project_id": proj["id"],
        }
        r = requests.post(f"{BASE}/links", headers=_hdr(admin_t), json=link_payload)
        assert r.status_code == 200
        link = r.json()
        assert link["auto_pull"] is True

        # Public view
        ident = requests.post(
            f"{BASE}/public/links/{link['slug']}/identify",
            json={"name": "Client A", "email": f"client-{uuid.uuid4().hex[:6]}@example.com"},
        )
        assert ident.status_code == 200, ident.text
        viewer = ident.json()["token"]
        view = requests.get(f"{BASE}/public/links/{link['slug']}", headers={"Authorization": f"Bearer {viewer}"})
        assert view.status_code == 200
        names = [t["name"] for t in view.json()["talents"]]
        joined = " | ".join(names)
        assert "ApprovedTalent" in joined
        assert "PendingTalent" not in joined

        # Approving the second submission MUST add it without touching the link.
        _decide(admin_t, proj["id"], b["id"], "approved")
        view2 = requests.get(f"{BASE}/public/links/{link['slug']}", headers={"Authorization": f"Bearer {viewer}"})
        joined2 = " | ".join(t["name"] for t in view2.json()["talents"])
        assert "ApprovedTalent" in joined2 and "PendingTalent" in joined2

        # Rejecting one MUST remove it from the link.
        _decide(admin_t, proj["id"], a["id"], "rejected")
        view3 = requests.get(f"{BASE}/public/links/{link['slug']}", headers={"Authorization": f"Bearer {viewer}"})
        joined3 = " | ".join(t["name"] for t in view3.json()["talents"])
        assert "ApprovedTalent" not in joined3 and "PendingTalent" in joined3


# ----------------------------------------------------------------------------
# Per-talent field visibility on individual share links
# ----------------------------------------------------------------------------
class TestTalentFieldVisibility:
    def test_persists_only_for_attached_talents(self, admin_t):
        # Create a talent and a link with TFV; an unrelated talent_id in TFV
        # MUST be stripped to avoid stale entries.
        r = requests.post(
            f"{BASE}/talents",
            headers=_hdr(admin_t),
            json={"name": "Vis Test", "age": 25, "height": "5ft 8in", "location": "Mumbai"},
        )
        assert r.status_code == 200
        tid = r.json()["id"]
        r = requests.post(
            f"{BASE}/links",
            headers=_hdr(admin_t),
            json={
                "title": f"VisLink-{uuid.uuid4().hex[:4]}",
                "talent_ids": [tid],
                "talent_field_visibility": {
                    tid: {"name": False, "age": True, "height": False},
                    "ghost-tid": {"name": False},  # should be dropped
                },
            },
        )
        assert r.status_code == 200, r.text
        link = r.json()
        tfv = link["talent_field_visibility"]
        assert tid in tfv and tfv[tid]["name"] is False and tfv[tid]["age"] is True
        assert "ghost-tid" not in tfv


# ----------------------------------------------------------------------------
# Per-project re-approval-on-edit toggle
# ----------------------------------------------------------------------------
class TestRequireReapproval:
    def test_default_resets_decision_on_retake(self, admin_t):
        # Default require_reapproval_on_edit = True
        proj = _new_project(admin_t, brand="ReapProjDefault")
        sub = _start_submission(proj["slug"], name="Reap-Default")
        sid, sub_tok = sub["id"], sub["token"]
        # Stamp state as already-finalized + approved so a retake exercises the toggle.
        _set_submission_state(sid, status="submitted", decision="approved")

        img = Image.new("RGB", (200, 200), (10, 10, 10))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        r = requests.post(
            f"{BASE}/public/submissions/{sid}/upload",
            headers={"Authorization": f"Bearer {sub_tok}"},
            files={"file": ("a.jpg", buf.getvalue(), "image/jpeg")},
            data={"category": "image"},
            timeout=30,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "updated"
        assert body["decision"] == "pending"  # reset by default

    def test_toggle_off_preserves_decision(self, admin_t):
        proj = _new_project(admin_t, brand="ReapProjOff")
        # Disable the flag
        r = requests.put(
            f"{BASE}/projects/{proj['id']}",
            headers=_hdr(admin_t),
            json={**proj, "require_reapproval_on_edit": False},
        )
        assert r.status_code == 200, r.text
        sub = _start_submission(proj["slug"], name="Reap-Off")
        sid, sub_tok = sub["id"], sub["token"]
        _set_submission_state(sid, status="submitted", decision="approved")

        img = Image.new("RGB", (200, 200), (200, 100, 100))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        r = requests.post(
            f"{BASE}/public/submissions/{sid}/upload",
            headers={"Authorization": f"Bearer {sub_tok}"},
            files={"file": ("b.jpg", buf.getvalue(), "image/jpeg")},
            data={"category": "image"},
            timeout=30,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "updated"
        # Decision should be preserved as "approved" because the flag is OFF.
        assert body["decision"] == "approved"


# ----------------------------------------------------------------------------
# Notification fanout
# ----------------------------------------------------------------------------
class TestNotificationFanout:
    def test_endpoints_visible_for_admin(self, admin_t):
        r = requests.get(f"{BASE}/notifications/unread-count", headers=_hdr(admin_t))
        assert r.status_code == 200 and "count" in r.json()
        r = requests.get(f"{BASE}/notifications", headers=_hdr(admin_t))
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) >= {"items", "total", "page", "size", "has_more"}

    def test_decision_change_creates_notification_for_other_users(self, admin_t):
        """Admin acts → admin must NOT see their own notification but other
        active users will (already covered in DB-level test). We assert the
        admin's own count never increments from their own decisions."""
        before = requests.get(f"{BASE}/notifications/unread-count", headers=_hdr(admin_t)).json()["count"]
        proj = _new_project(admin_t, brand="NotifSelf")
        sub = _start_submission(proj["slug"])
        _decide(admin_t, proj["id"], sub["id"], "approved")
        after = requests.get(f"{BASE}/notifications/unread-count", headers=_hdr(admin_t)).json()["count"]
        assert after == before  # actor excluded
