"""Live-API integration tests: "Ongoing Project Talents" — the WhatsApp
Campaigns talent-first daily follow-up list (source_type ONGOING_PIPELINE).

Covers exactly the guarantees the feature promises: a talent pending on
several ongoing projects appears ONCE with all of them consolidated; a
talent with a mixed status (pending/submitted/completed across different
projects) only shows the still-pending one; a project that isn't ongoing
never appears; "Last Reminder" only counts an actually-successful
("sent") WhatsApp job, never a failed/pending one; recipient routing
matches _resolve_destination (group > number > unavailable) exactly; and
sending to N selected talents produces exactly N WhatsApp jobs — one
consolidated message per talent, never one per project.

Runs against a live local backend + local Mongo (same convention as the
other focused test files added this session).
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
import pymongo

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8000").rstrip("/")
API = f"{BASE_URL}/api"
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "talentgram")
ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "changeme123")


@pytest.fixture(scope="module")
def db():
    client = pymongo.MongoClient(MONGO_URL)
    return client[DB_NAME]


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=30)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    return r.json()["token"]


def _mk_project(db, *, status="ongoing", name="ZZZ_TEST OPT Project"):
    pid = str(uuid.uuid4())
    slug = f"zzz-test-opt-{uuid.uuid4().hex[:10]}"
    db.projects.insert_one({"id": pid, "slug": slug, "brand_name": name, "status": status})
    return pid, slug


def _mk_talent(db, *, name="ZZZ_TEST OPT Talent", phone="", group=""):
    tid = str(uuid.uuid4())
    db.talents.insert_one({
        "id": tid, "name": name,
        "email": f"zzz-opt-{uuid.uuid4().hex[:8]}@example.com",
        "phone": phone, "whatsapp_group_name": group,
    })
    return tid


def _mk_pipeline_row(db, project_id, talent_id, stage="ask_to_test"):
    db.casting_pipeline.insert_one({
        "id": str(uuid.uuid4()), "project_id": project_id, "talent_id": talent_id,
        "stage": stage, "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    })


def _mk_submission(db, project_id, talent_id):
    db.submissions.insert_one({
        "id": str(uuid.uuid4()), "project_id": project_id, "talent_id": talent_id,
        "status": "submitted",
    })


@pytest.fixture()
def cleanup(db):
    ids = {"projects": [], "talents": [], "pipeline": [], "submissions": [], "jobs": []}
    yield ids
    if ids["projects"]:
        db.projects.delete_many({"id": {"$in": ids["projects"]}})
        db.casting_pipeline.delete_many({"project_id": {"$in": ids["projects"]}})
        db.submissions.delete_many({"project_id": {"$in": ids["projects"]}})
    if ids["talents"]:
        db.talents.delete_many({"id": {"$in": ids["talents"]}})
        db.whatsapp_jobs.delete_many({"talent_id": {"$in": ids["talents"]}})


def _get_talents(admin_token):
    r = requests.get(f"{API}/whatsapp/ongoing-pipeline-talents",
                      headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
    assert r.status_code == 200, r.text
    return {t["talent_id"]: t for t in r.json()["talents"]}


def test_multi_project_talent_appears_once_consolidated(db, admin_token, cleanup):
    p1, _ = _mk_project(db, name="ZZZ_TEST OPT Nike")
    p2, _ = _mk_project(db, name="ZZZ_TEST OPT Amazon")
    p3, _ = _mk_project(db, name="ZZZ_TEST OPT Nykaa")
    cleanup["projects"] += [p1, p2, p3]
    tid = _mk_talent(db, name="ZZZ_TEST OPT Multi", phone="+911234500001")
    cleanup["talents"].append(tid)
    for pid in (p1, p2, p3):
        _mk_pipeline_row(db, pid, tid)  # ask_to_test, no submission -> pending on all 3

    by_id = _get_talents(admin_token)
    assert tid in by_id
    row = by_id[tid]
    assert len(row["pending_projects"]) == 3
    names = {p["project_name"] for p in row["pending_projects"]}
    assert names == {"ZZZ_TEST OPT Nike", "ZZZ_TEST OPT Amazon", "ZZZ_TEST OPT Nykaa"}
    assert row["destination_type"] == "number"
    assert row["destination"] == "+911234500001"
    # Consolidated message contains all three, each bolded with its own link.
    for pid, name in ((p1, "Nike"), (p2, "Amazon"), (p3, "Nykaa")):
        assert f"*ZZZ_TEST OPT {name}*" in row["message"]


def test_message_matches_the_exact_requested_structure(db, admin_token, cleanup):
    """2026-09 template change — bold "N - *Project*" heading, URL on its
    own "project details - {link}" line (never a bare indented line that a
    markdown-numbered-list-aware client would auto-number as its own
    "N+1." item — that double-numbering bug is exactly what this format
    exists to avoid), blank line between entries, exact closing wording,
    single-asterisk (WhatsApp-valid) bold — not the old "N. *Project*"
    format."""
    p1, slug1 = _mk_project(db, name="ZZZ_TEST OPT Structure A")
    p2, slug2 = _mk_project(db, name="ZZZ_TEST OPT Structure B")
    cleanup["projects"] += [p1, p2]
    tid = _mk_talent(db, name="Dia Structure", phone="+911234500009")
    cleanup["talents"].append(tid)
    _mk_pipeline_row(db, p1, tid)
    _mk_pipeline_row(db, p2, tid)

    row = _get_talents(admin_token)[tid]
    msg = row["message"]
    link1 = f"https://submit.talentgramagency.com/submit/{slug1}"
    link2 = f"https://submit.talentgramagency.com/submit/{slug2}"

    expected = (
        "Hi Dia,\n"
        "\n"
        "Just checking in regarding your pending Talentgram projects:\n"
        "\n"
        "1 - *ZZZ_TEST OPT Structure A*\n"
        f"project details - {link1}\n"
        "\n"
        "2 - *ZZZ_TEST OPT Structure B*\n"
        f"project details - {link2}\n"
        "\n"
        "Could you please let us know *which project(s) you'll be submitting for*? "
        "This will help us keep the project status updated from our end.\n"
        "\n"
        "If you're sending a submission, please complete it at the earliest.\n"
        "\n"
        "— Talentgram"
    )
    assert msg == expected
    # No markdown-style anchor links — a bare, tappable URL, matching every
    # other message this WhatsApp engine already sends.
    assert "](" not in msg
    assert "**" not in msg  # every bold marker is single-asterisk, WhatsApp-valid
    # The exact bug being fixed: no "N." markdown-list-style project line
    # anywhere (that's what a WhatsApp/markdown client auto-renumbers).
    assert "1." not in msg
    assert "2." not in msg


def test_mixed_status_only_pending_project_shown(db, admin_token, cleanup):
    p_pending, _ = _mk_project(db, name="ZZZ_TEST OPT Pending")
    p_submitted, _ = _mk_project(db, name="ZZZ_TEST OPT Submitted")
    p_locked, _ = _mk_project(db, name="ZZZ_TEST OPT Locked")
    cleanup["projects"] += [p_pending, p_submitted, p_locked]
    tid = _mk_talent(db, name="ZZZ_TEST OPT Mixed", phone="+911234500002")
    cleanup["talents"].append(tid)
    _mk_pipeline_row(db, p_pending, tid, stage="ask_to_test")           # pending
    _mk_pipeline_row(db, p_submitted, tid, stage="ask_to_test")
    _mk_submission(db, p_submitted, tid)                                # ask_to_test + submitted -> NOT pending
    _mk_pipeline_row(db, p_locked, tid, stage="locked")                 # terminal stage -> NOT pending

    by_id = _get_talents(admin_token)
    assert tid in by_id
    names = {p["project_name"] for p in by_id[tid]["pending_projects"]}
    assert names == {"ZZZ_TEST OPT Pending"}


def test_non_ongoing_project_excluded(db, admin_token, cleanup):
    pid, _ = _mk_project(db, status="completed", name="ZZZ_TEST OPT Wrapped")
    cleanup["projects"].append(pid)
    tid = _mk_talent(db, name="ZZZ_TEST OPT NonOngoing", phone="+911234500003")
    cleanup["talents"].append(tid)
    _mk_pipeline_row(db, pid, tid)

    by_id = _get_talents(admin_token)
    assert tid not in by_id


def test_explicit_follow_up_stage_counts_as_pending(db, admin_token, cleanup):
    pid, _ = _mk_project(db, name="ZZZ_TEST OPT ReachedOut")
    cleanup["projects"].append(pid)
    tid = _mk_talent(db, name="ZZZ_TEST OPT FollowUp", phone="+911234500004")
    cleanup["talents"].append(tid)
    _mk_pipeline_row(db, pid, tid, stage="follow_up")

    by_id = _get_talents(admin_token)
    assert tid in by_id
    assert len(by_id[tid]["pending_projects"]) == 1


def test_last_reminder_never_and_successful(db, admin_token, cleanup):
    pid, _ = _mk_project(db, name="ZZZ_TEST OPT Reminder")
    cleanup["projects"].append(pid)
    tid = _mk_talent(db, name="ZZZ_TEST OPT NeverThenSent", phone="+911234500005")
    cleanup["talents"].append(tid)
    _mk_pipeline_row(db, pid, tid)

    # Before any job: Never.
    by_id = _get_talents(admin_token)
    assert by_id[tid]["last_reminder_at"] is None

    # A FAILED job must not count as a successful reminder.
    db.whatsapp_jobs.insert_one({
        "id": str(uuid.uuid4()), "talent_id": tid, "status": "failed",
        "sent_at": None, "created_at": datetime.now(timezone.utc).isoformat(),
    })
    by_id = _get_talents(admin_token)
    assert by_id[tid]["last_reminder_at"] is None

    # A SENT job must be picked up as the last successful reminder.
    sent_iso = datetime.now(timezone.utc).isoformat()
    db.whatsapp_jobs.insert_one({
        "id": str(uuid.uuid4()), "talent_id": tid, "status": "sent",
        "sent_at": sent_iso, "created_at": sent_iso,
    })
    by_id = _get_talents(admin_token)
    assert by_id[tid]["last_reminder_at"] == sent_iso


def test_recipient_group_takes_priority_over_number(db, admin_token, cleanup):
    pid, _ = _mk_project(db, name="ZZZ_TEST OPT Recipient")
    cleanup["projects"].append(pid)
    tid = _mk_talent(db, name="ZZZ_TEST OPT GroupTalent", phone="+911234500006", group="ZZZ Test Group x Talentgram")
    cleanup["talents"].append(tid)
    _mk_pipeline_row(db, pid, tid)

    by_id = _get_talents(admin_token)
    row = by_id[tid]
    assert row["destination_type"] == "group"
    assert row["destination"] == "ZZZ Test Group x Talentgram"


def test_no_recipient_is_unavailable(db, admin_token, cleanup):
    pid, _ = _mk_project(db, name="ZZZ_TEST OPT NoRecipient")
    cleanup["projects"].append(pid)
    tid = _mk_talent(db, name="ZZZ_TEST OPT Unreachable")  # no phone, no group
    cleanup["talents"].append(tid)
    _mk_pipeline_row(db, pid, tid)

    by_id = _get_talents(admin_token)
    row = by_id[tid]
    assert row["destination_type"] == ""
    assert row["message"] is None  # never build a message for an unreachable talent


def test_batch_send_is_one_job_per_talent_not_per_project(db, admin_token, cleanup):
    """The core rule: N selected talents -> N jobs, regardless of how many
    pending projects any of them has."""
    p1, _ = _mk_project(db, name="ZZZ_TEST OPT BatchA")
    p2, _ = _mk_project(db, name="ZZZ_TEST OPT BatchB")
    cleanup["projects"] += [p1, p2]

    t_multi = _mk_talent(db, name="ZZZ_TEST OPT BatchMulti", phone="+911234500007")
    t_single = _mk_talent(db, name="ZZZ_TEST OPT BatchSingle", phone="+911234500008")
    cleanup["talents"] += [t_multi, t_single]
    _mk_pipeline_row(db, p1, t_multi)
    _mk_pipeline_row(db, p2, t_multi)
    _mk_pipeline_row(db, p1, t_single)

    templates = requests.get(f"{API}/whatsapp/templates",
                              headers={"Authorization": f"Bearer {admin_token}"}, timeout=30).json()
    custom_template_id = next(t["id"] for t in templates if t.get("slug") == "custom")

    r = requests.post(
        f"{API}/whatsapp/batches",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "source_type": "ONGOING_PIPELINE",
            "source_params": {"talent_ids": [t_multi, t_single]},
            "template_id": custom_template_id,
            "is_dry_run": True,
        },
        timeout=30,
    )
    assert r.status_code == 201, r.text
    data = r.json()
    jobs = data["jobs"]
    assert len(jobs) == 2  # exactly one job per talent, not 3 (2 projects + 1 project)

    by_talent = {j["talent_id"]: j for j in jobs}
    assert "ZZZ_TEST OPT BatchA" in by_talent[t_multi]["message_body"]
    assert "ZZZ_TEST OPT BatchB" in by_talent[t_multi]["message_body"]
    assert "ZZZ_TEST OPT BatchA" in by_talent[t_single]["message_body"]
    assert "ZZZ_TEST OPT BatchB" not in by_talent[t_single]["message_body"]

    # dry run — nothing should have been persisted; clean up defensively anyway.
    db.whatsapp_batches.delete_one({"id": data["batch"]["id"]})
    db.whatsapp_jobs.delete_many({"batch_id": data["batch"]["id"]})


def test_ineligible_talent_silently_dropped_at_send_time(db, admin_token, cleanup):
    """"Do not send if the talent no longer has an eligible pending
    project" — asking to send to a talent with zero pending projects
    (or a nonexistent one) must resolve to zero recipients, not an error
    that blocks the rest of the batch, and not a send to someone stale."""
    tid = str(uuid.uuid4())  # never added to any pipeline at all
    r = requests.post(
        f"{API}/whatsapp/resolve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"source_type": "ONGOING_PIPELINE", "source_params": {"talent_ids": [tid]}},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    assert r.json()["recipients"] == []
