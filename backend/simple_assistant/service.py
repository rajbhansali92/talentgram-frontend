"""Read-only aggregation for the Simple Assistant Command Centre.

Every function here performs ONLY ``find`` / ``aggregate`` reads. There is
no code path from this module to any insert/update/delete, to Cloudinary,
to WhatsApp, or to an LLM.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core import active_only, compute_age, db, enrich_talent, video_poster_url
from routers.casting_pipeline import (
    PIPELINE_STAGE_ORDER,
    _normalise_stage,
    get_stage_counts,
)

logger = logging.getLogger(__name__)

# Human labels for the canonical pipeline stages. Keys are a strict subset
# of routers.casting_pipeline.PIPELINE_STAGE_ORDER (imported live above) —
# no stage is invented here. "follow_up" is the virtual "Reached Out" lane,
# same definition routers.casting_pipeline.list_pipeline uses.
STAGE_LABELS: Dict[str, str] = {
    "ask_to_test": "Asked to test",
    "approved": "Sent to casting",
    "hold": "On hold",
    "shortlisted": "Shortlisted",
    "already_tested": "Already tested",
    "locked": "Locked",
    "rejected": "Rejected",
    "not_available": "Not available",
    "not_interested": "Not interested",
    "pitch": "Pitch",
    "follow_up": "Follow-up",
}

# Order talent cards surface in — most decision-relevant first.
_STAGE_SORT: Dict[str, int] = {
    "locked": 0,
    "shortlisted": 1,
    "already_tested": 2,
    "approved": 3,
    "ask_to_test": 4,
    "hold": 5,
    "follow_up": 6,
    "pitch": 7,
    "not_available": 8,
    "not_interested": 9,
    "rejected": 10,
}

_ACTIVE_PROJECT_STATUSES = ("ongoing", "hold", "locked")
_MAX_PROJECTS = 50
_MAX_TALENT_CARDS = 24
_MAX_TAKES = 6

_TAKE_CATEGORIES = {"take", "take_1", "take_2", "take_3"}
_LEGACY_TAKE_LABEL = {"take_1": "Take 1", "take_2": "Take 2", "take_3": "Take 3"}


def _greeting_now() -> str:
    """Time-of-day greeting in the agency's local timezone (IST)."""
    try:
        from zoneinfo import ZoneInfo

        hour = datetime.now(ZoneInfo("Asia/Kolkata")).hour
    except Exception:  # pragma: no cover - zoneinfo always present on 3.9+
        hour = (datetime.now(timezone.utc).hour + 5) % 24
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def _first_name(user: Dict[str, Any]) -> str:
    raw = (user.get("name") or user.get("first_name") or "").strip()
    if raw:
        return raw.split()[0]
    email = (user.get("email") or "").strip()
    if email:
        return email.split("@")[0].split(".")[0].capitalize()
    return "there"


def _project_image(project: dict) -> Optional[str]:
    for m in project.get("materials") or []:
        if not isinstance(m, dict):
            continue
        rt = (m.get("resource_type") or "").lower()
        ct = (m.get("content_type") or "").lower()
        url = m.get("url")
        if url and (rt == "image" or ct.startswith("image/")):
            return url
    return None


def _media_entry(item: dict) -> Optional[dict]:
    """Map ONE submission media item to a playable descriptor. Read-only —
    no transform, no signing, no upload. Returns None when unusable."""
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    if not url:
        return None
    cat = (item.get("category") or "").lower()
    if cat == "intro_video":
        label = "Introduction"
    elif cat in _TAKE_CATEGORIES:
        label = (item.get("label") or "").strip() or _LEGACY_TAKE_LABEL.get(cat, "Take")
    else:
        return None
    poster = item.get("poster_url") or video_poster_url(item.get("public_id")) or video_poster_url(url)
    return {
        "id": item.get("id"),
        "label": label,
        "url": url,
        "poster_url": poster,
        "category": cat,
    }


def _talent_media(sub: Optional[dict]) -> Dict[str, Any]:
    intro = None
    takes: List[dict] = []
    for item in (sub or {}).get("media") or []:
        entry = _media_entry(item)
        if not entry:
            continue
        if entry["category"] == "intro_video":
            if intro is None:
                intro = entry
        else:
            takes.append(entry)
    takes.sort(key=lambda e: (e["category"], (e["label"] or "")))
    return {"intro_video": intro, "takes": takes[:_MAX_TAKES]}


async def _project_overview(project: dict, *, want_cards: bool) -> dict:
    pid = project.get("id")

    rows = await db.casting_pipeline.find(
        {"project_id": pid}, {"_id": 0, "talent_id": 1, "stage": 1}
    ).to_list(5000)
    talent_ids = list({r.get("talent_id") for r in rows if r.get("talent_id")})

    stage_counts_task = get_stage_counts(pid)

    talent_docs: List[dict] = []
    submissions: List[dict] = []
    if talent_ids:
        talent_docs, submissions, stage_counts = await asyncio.gather(
            db.talents.find(
                {"id": {"$in": talent_ids}},
                {
                    "_id": 0,
                    "id": 1,
                    "name": 1,
                    "dob": 1,
                    "age": 1,
                    "height": 1,
                    "gender": 1,
                    "media": 1,
                    "cover_media_id": 1,
                    "cover_url": 1,
                    "cover_thumbnail_url": 1,
                },
            ).to_list(len(talent_ids)),
            db.submissions.find(
                active_only({"project_id": pid, "talent_id": {"$in": talent_ids}}),
                {"_id": 0, "talent_id": 1, "status": 1, "media": 1, "submitted_at": 1},
            ).to_list(None),
            stage_counts_task,
        )
    else:
        stage_counts = await stage_counts_task

    sub_by_talent: Dict[str, dict] = {}
    for s in submissions:
        tid = s.get("talent_id")
        if tid and tid not in sub_by_talent:
            sub_by_talent[tid] = s
    submitted_talent_ids = set(sub_by_talent.keys())

    talent_by_id = {t.get("id"): t for t in talent_docs if t.get("id")}

    # follow_up: virtual lane — identical predicate to
    # routers.casting_pipeline.list_pipeline (kept in sync by intent).
    follow_up_count = 0
    tests_received = 0
    cards: List[dict] = []
    for r in rows:
        tid = r.get("talent_id")
        stage = _normalise_stage(r.get("stage")) or r.get("stage")
        is_follow_up = stage == "follow_up" or (
            stage == "ask_to_test" and bool(tid) and tid not in submitted_talent_ids
        )
        if is_follow_up:
            follow_up_count += 1

        sub = sub_by_talent.get(tid)
        media = _talent_media(sub) if sub else {"intro_video": None, "takes": []}
        has_test = bool(media["intro_video"] or media["takes"])
        if has_test:
            tests_received += 1

        if not want_cards:
            continue
        t = talent_by_id.get(tid)
        if not t:
            continue
        enriched = enrich_talent(dict(t)) or {}
        age = enriched.get("age")
        if age is None:
            age = compute_age(t.get("dob")) or t.get("age")
        cards.append(
            {
                "talent_id": tid,
                "name": t.get("name") or "Unknown",
                "age": age,
                "height": t.get("height") or None,
                "gender": t.get("gender") or None,
                "photo_url": enriched.get("cover_thumbnail_url")
                or enriched.get("image_url")
                or t.get("cover_url"),
                "stage": stage,
                "stage_label": STAGE_LABELS.get(stage, stage.replace("_", " ").title()),
                "is_follow_up": is_follow_up,
                "test_status": "received" if has_test else "awaiting",
                "media": media,
                "_sort": (
                    _STAGE_SORT.get(stage, 99),
                    0 if has_test else 1,
                    (t.get("name") or "").lower(),
                ),
            }
        )

    cards.sort(key=lambda c: c.pop("_sort"))
    truncated = len(cards) > _MAX_TALENT_CARDS

    stage_summary = [
        {"key": k, "label": STAGE_LABELS.get(k, k), "count": stage_counts.get(k, 0)}
        for k in PIPELINE_STAGE_ORDER
        if stage_counts.get(k, 0) > 0
    ]

    return {
        "id": pid,
        "name": project.get("brand_name") or "Untitled project",
        "client": project.get("brand_name") or None,
        "character": project.get("character") or None,
        "requirement": project.get("additional_details") or project.get("character") or None,
        "status": project.get("status") or "ongoing",
        "shoot_dates": project.get("shoot_dates") or None,
        "budget": project.get("budget_per_day") or None,
        "medium_usage": project.get("medium_usage") or None,
        "director": project.get("director") or None,
        "production_house": project.get("production_house") or None,
        "image_url": _project_image(project),
        "pipeline_total": len(rows),
        "stage_counts": stage_counts,
        "stage_summary": stage_summary,
        "tests_received": tests_received,
        "follow_up_count": follow_up_count,
        "talents": cards[:_MAX_TALENT_CARDS],
        "talents_truncated": truncated,
        "talent_total": len(rows),
    }


async def build_overview(user: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the full Command Centre payload from existing data only."""
    projects = await db.projects.find(
        {"status": {"$in": list(_ACTIVE_PROJECT_STATUSES)}},
        {
            "_id": 0,
            "id": 1,
            "brand_name": 1,
            "character": 1,
            "additional_details": 1,
            "status": 1,
            "shoot_dates": 1,
            "budget_per_day": 1,
            "medium_usage": 1,
            "director": 1,
            "production_house": 1,
            "materials": 1,
            "created_at": 1,
        },
    ).sort("created_at", -1).to_list(_MAX_PROJECTS)

    overviews = await asyncio.gather(
        *(_project_overview(p, want_cards=True) for p in projects)
    )

    talents_in_pipeline = sum(o["pipeline_total"] for o in overviews)
    tests_received = sum(o["tests_received"] for o in overviews)
    follow_ups = sum(o["follow_up_count"] for o in overviews)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "greeting": _greeting_now(),
        "user": {"first_name": _first_name(user)},
        "summary": {
            "active_projects": len(overviews),
            "talents_in_pipeline": talents_in_pipeline,
            "tests_received": tests_received,
            "follow_ups": follow_ups,
        },
        "stage_vocabulary": [
            {"key": k, "label": STAGE_LABELS.get(k, k)} for k in PIPELINE_STAGE_ORDER
        ],
        "projects": overviews,
    }
