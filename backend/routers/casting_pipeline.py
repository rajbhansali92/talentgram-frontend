"""Casting Pipeline — project-scoped kanban router.

Endpoints (all under `/api/projects/{project_id}/pipeline`):

  GET    /                       List rows, hydrated with talent data
  POST   /add                    Bulk add talents to the pipeline (idempotent)
  PATCH  /move                   Bulk move existing rows to a new stage
  DELETE /{entry_id}             Remove a single pipeline row

Collection: ``casting_pipeline``. Document shape:

    {
        "id":          str (uuid4),
        "project_id":  str,
        "talent_id":   str,
        "stage":       str,
        "created_at":  datetime,
        "updated_at":  datetime,
    }

Hydration is done with a single ``$in`` lookup on ``db.talents`` — no N+1
regardless of pipeline size.
"""
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from pymongo.errors import BulkWriteError

from core import (
    _now,
    current_team_or_admin,
    db,
    enrich_talent,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects",
    tags=["Casting Pipeline"],
)


# ---------------------------------------------------------------------------
# Stages — centralised registry.
#
# `PIPELINE_STAGES`    : full set of accepted stage values (validation).
# `PIPELINE_STAGE_ORDER`: canonical render order for the kanban. `pitch` is
#                        intentionally placed last because it is an
#                        independent "sourcing" lane, not part of the
#                        progression flow (no inbound/outbound transitions
#                        from the main funnel).
# `DEFAULT_STAGE`      : where new pipeline rows land on add.
#
# Legacy normalisation: the deprecated `sent` value is folded into
# `approved` at the I/O boundary (read on GET, normalised on POST/PATCH).
# Existing `sent` documents stay in the DB untouched — we rewrite them at
# read time, so a backfill is not required for the feature to ship.
# ---------------------------------------------------------------------------
PIPELINE_STAGE_ORDER = [
    "ask_to_test",
    "approved",
    "hold",
    "shortlisted",
    "already_tested",
    "locked",
    "rejected",
    "not_available",
    "not_interested",
    "pitch",
]

PIPELINE_STAGES = set(PIPELINE_STAGE_ORDER)

# Legacy → canonical alias map. Applied at every I/O boundary.
LEGACY_STAGE_ALIASES = {
    "sent": "approved",
}

DEFAULT_STAGE = "ask_to_test"


def _normalise_stage(raw: Optional[str]) -> Optional[str]:
    """Fold legacy/aliased stage values into their canonical equivalents.

    Returns ``None`` for falsy input so callers can decide whether to fall
    back to ``DEFAULT_STAGE`` (on add) or reject (on move).
    """
    if not raw:
        return None
    s = raw.strip().lower()
    return LEGACY_STAGE_ALIASES.get(s, s)


# ---------------------------------------------------------------------------
# Pydantic input bodies
# ---------------------------------------------------------------------------
class PipelineAddIn(BaseModel):
    talent_ids: List[str] = Field(default_factory=list)
    # Optional stage override for the bulk-add. Defaults to "ask_to_test".
    stage: Optional[str] = None


class BulkProjectAddIn(BaseModel):
    """Cross-project bulk add — the Global Talent Directory's 'Add to
    Project' action. Every (project_id, talent_id) pair is inserted at most
    once; pairs already in the pipeline are skipped, never duplicated."""
    project_ids: List[str] = Field(default_factory=list)
    talent_ids: List[str] = Field(default_factory=list)
    stage: Optional[str] = None


class PipelineMoveIn(BaseModel):
    ids: List[str] = Field(default_factory=list)
    stage: str


class PipelineBulkMoveIn(BaseModel):
    talent_ids: List[str] = Field(default_factory=list)
    stage: str


class PipelineBulkLabelIn(BaseModel):
    talent_ids: List[str] = Field(default_factory=list)
    labels: List[str] = Field(default_factory=list)
    action: str  # "add" or "remove"


class PipelineBulkNoteIn(BaseModel):
    talent_ids: List[str] = Field(default_factory=list)
    note: str


class PipelineBulkDeleteIn(BaseModel):
    talent_ids: List[str] = Field(default_factory=list)


class PipelineBulkExportIn(BaseModel):
    talent_ids: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _talent_merge_fields(t: dict) -> dict:
    """Reduce a talent doc to the five hydration fields the kanban renders."""
    enriched = enrich_talent(t) or {}
    return {
        "talent_name": enriched.get("name"),
        "talent_email": enriched.get("email"),
        "talent_phone": enriched.get("phone"),
        "instagram_handle": enriched.get("instagram_handle"),
        "image_url": enriched.get("image_url"),
    }


_EMPTY_MERGE = {
    "talent_name": None,
    "talent_email": None,
    "talent_phone": None,
    "instagram_handle": None,
    "image_url": None,
}


async def _project_exists(project_id: str) -> bool:
    """Lightweight existence check — used to fail fast on bad project ids
    rather than silently creating orphan pipeline rows."""
    return bool(
        await db.projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    )


# ---------------------------------------------------------------------------
# Submission → pipeline sync
#
# Called from `submissions.set_decision` whenever an admin approves / holds /
# rejects a submission, and from `submissions.submission_finalize` on first
# finalize so that every submitted talent is auto-entered in the pipeline.
#
# Behaviour:
#
#   Auto-SYNC (existing row):
#     • Only mutates rows currently in AUTO_SYNC_OVERWRITABLE_STAGES.
#       Locked, shortlisted, already_tested, pitch are protected —
#       a submission decision must not silently overwrite later curation.
#     • Touches at most ONE row (the (project_id, talent_id) pair).
#
#   Auto-CREATE (no existing row):
#     • When a submission decision maps to a stage AND no pipeline row
#       exists yet, a new row is inserted at the target stage.
#     • Uses a single atomic upsert: the update path handles existing rows
#       (with stage guard), and the insert path creates fresh rows.
#     • Strictly scoped to project_id — no cross-project creation.
#
#   Shared invariants:
#     • Best-effort — errors are swallowed/logged, never blocking the
#       user-facing submission decision response.
#     • Idempotent: re-running the same decision is a no-op (matched_count
#       increments but modified_count stays 0).
# ---------------------------------------------------------------------------
SUBMISSION_DECISION_TO_STAGE = {
    "approved": "approved",
    "hold": "hold",
    "rejected": "rejected",
    "ask_to_test": "ask_to_test",
    "shortlisted": "shortlisted",
    "does_not_work_for_this": "not_available",
}

# Only these stages may be auto-overwritten by a submission decision.
# Adding `follow_up` here when the stage is introduced will make the cleanup
# requirement ("automatically remove the talent from … future follow_up
# stage") work without further changes.
AUTO_SYNC_OVERWRITABLE_STAGES = {
    "ask_to_test",
    "approved",
    "hold",
    "rejected",
    "shortlisted",
    "not_available",
    "follow_up",  # not yet implemented; safe to list pre-emptively
}


async def sync_pipeline_from_submission(
    project_id: Optional[str],
    talent_id: Optional[str],
    decision: Optional[str],
) -> None:
    """Best-effort: upsert the pipeline row for (project_id, talent_id) to
    the stage implied by the submission decision.

    No-op when:
      • project_id or talent_id are missing (submission not yet linked)
      • decision is not in SUBMISSION_DECISION_TO_STAGE (e.g. 'pending')

    Existing-row behaviour:
      Only overwrites the stage when the current stage is in
      AUTO_SYNC_OVERWRITABLE_STAGES. Protected stages (locked, shortlisted,
      already_tested, pitch) are left untouched.

    New-row behaviour:
      If no pipeline row exists for (project_id, talent_id), one is created
      at the target stage. This ensures that a submission decision is always
      reflected in the pipeline — the recruiter never needs to manually add
      a talent they have already reviewed.

    This function NEVER raises — it returns silently and logs at WARNING.
    """
    try:
        if not project_id or not talent_id:
            return
        # Normalize input: strip whitespace and lowercase so "Rejected" /
        # "REJECTED" / "rejected" all resolve correctly. The SUBMISSION_DECISIONS
        # validation on the router already enforces lowercase, but this guard
        # protects any future direct callers or admin tooling.
        decision_key = (decision or "").strip().lower()
        target_stage = SUBMISSION_DECISION_TO_STAGE.get(decision_key)
        if not target_stage:
            return

        now = _now()

        # Step 1: conditional update of EXISTING row.
        # Only touches the row when current stage is overwritable.
        # Protected stages (locked/shortlisted/already_tested/pitch) are
        # deliberately excluded from AUTO_SYNC_OVERWRITABLE_STAGES, so this
        # update silently matches 0 rows for protected entries.
        res = await db.casting_pipeline.update_one(
            {
                "project_id": project_id,
                "talent_id": talent_id,
                "stage": {"$in": list(AUTO_SYNC_OVERWRITABLE_STAGES)},
            },
            {"$set": {"stage": target_stage, "updated_at": now}},
        )
        if res.modified_count:
            logger.info(
                "pipeline.auto-sync.update project=%s talent=%s decision=%s → stage=%s",
                project_id, talent_id, decision_key, target_stage,
            )
            return

        # Step 2: check whether a protected row exists (should not be touched).
        # If so, respect the manual curation and skip creation.
        protected_exists = await db.casting_pipeline.find_one(
            {
                "project_id": project_id,
                "talent_id": talent_id,
            },
            {"_id": 0, "stage": 1},
        )
        if protected_exists:
            # Row exists but is in a protected stage — do not overwrite.
            logger.info(
                "pipeline.auto-sync.protected project=%s talent=%s "
                "current_stage=%s — skipping overwrite",
                project_id, talent_id, protected_exists.get("stage"),
            )
            return

        # Step 3: no row exists — auto-create at the target stage.
        # Uses insert_one rather than upsert to keep the duplicate-key
        # error visible if a concurrent request races us here.
        new_entry = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "talent_id": talent_id,
            "stage": target_stage,
            "created_at": now,
            "updated_at": now,
        }
        try:
            await db.casting_pipeline.insert_one(new_entry)
            logger.info(
                "pipeline.auto-sync.create project=%s talent=%s decision=%s → stage=%s",
                project_id, talent_id, decision_key, target_stage,
            )
        except Exception as insert_exc:
            # DuplicateKeyError: concurrent request won the race — harmless.
            logger.warning(
                "pipeline.auto-sync.create race project=%s talent=%s: %s",
                project_id, talent_id, insert_exc,
            )
    except Exception:
        # Pipeline sync is an enrichment — never break the submission flow.
        logger.exception(
            "pipeline.auto-sync failed project=%s talent=%s decision=%s",
            project_id, talent_id, decision_key,
        )


async def ensure_pipeline_from_finalized_submission(
    project_id: Optional[str],
    talent_id: Optional[str],
) -> None:
    """Best-effort: auto-create a pipeline entry at ask_to_test when a
    submission is first finalized and no pipeline row yet exists.

    This is the entry point for submission → pipeline auto-creation at the
    moment of first finalize. Subsequent decision changes are handled by
    sync_pipeline_from_submission. Protected stages are respected: if the
    talent is already in a curated stage the entry is left untouched.

    This function NEVER raises.
    """
    try:
        if not project_id or not talent_id:
            return

        now = _now()

        # Check whether a row already exists for this (project, talent) pair.
        existing = await db.casting_pipeline.find_one(
            {"project_id": project_id, "talent_id": talent_id},
            {"_id": 0, "id": 1},
        )
        if existing:
            # Row already present — do not touch. The recruiter may have
            # manually placed this talent before they submitted.
            return

        new_entry = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "talent_id": talent_id,
            "stage": DEFAULT_STAGE,   # ask_to_test
            "created_at": now,
            "updated_at": now,
        }
        try:
            await db.casting_pipeline.insert_one(new_entry)
            logger.info(
                "pipeline.auto-create.finalize project=%s talent=%s → stage=%s",
                project_id, talent_id, DEFAULT_STAGE,
            )
        except Exception as insert_exc:
            logger.warning(
                "pipeline.auto-create.finalize race project=%s talent=%s: %s",
                project_id, talent_id, insert_exc,
            )
    except Exception:
        logger.exception(
            "pipeline.auto-create.finalize failed project=%s talent=%s",
            project_id, talent_id,
        )


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id}/pipeline
# ---------------------------------------------------------------------------
@router.get("/{project_id}/pipeline")
async def list_pipeline(
    project_id: str,
    _admin: dict = Depends(current_team_or_admin),
):
    """Return every pipeline row for this project, hydrated with talent
    data in ONE additional `$in` query — no N+1.

    Response envelope is intentionally fixed to ``{success, data}`` so the
    frontend can rely on it identically to ``/talents/search`` and
    ``/talents/bulk``.
    """
    rows = await db.casting_pipeline.find(
        {"project_id": project_id},
        {"_id": 0},
    ).sort("created_at", 1).to_list(5000)

    if not rows:
        return {"success": True, "data": []}

    talent_ids = list({r.get("talent_id") for r in rows if r.get("talent_id")})

    by_id: dict = {}
    if talent_ids:
        talents_cursor = db.talents.find(
            {"id": {"$in": talent_ids}},
            {
                "_id": 0,
                "id": 1,
                "name": 1,
                "email": 1,
                "phone": 1,
                "instagram_handle": 1,
                "cover_media_id": 1,
                "media": 1,
            },
        )
        for t in await talents_cursor.to_list(len(talent_ids)):
            tid = t.get("id")
            if tid:
                by_id[tid] = _talent_merge_fields(t)

    # Submission-aware follow_up computation (PATCH 3C).
    #
    # `follow_up` is a virtual, read-only lane — NOT a stored DB stage. A
    # row is "in follow_up" when:
    #     stage == ask_to_test  AND  talent has no submission for this project
    #
    # One query covers all rows. `talent_id` is intentionally chosen as the
    # match key (not email) because pipeline rows and submissions are
    # already linked-by-talent_id once a talent reaches a project.
    submitted_talent_ids: set = set()
    if talent_ids:
        sub_cursor = db.submissions.find(
            {"project_id": project_id, "talent_id": {"$in": talent_ids}},
            {"_id": 0, "talent_id": 1},
        )
        async for s in sub_cursor:
            tid = s.get("talent_id")
            if tid:
                submitted_talent_ids.add(tid)

    hydrated = []
    for row in rows:
        canonical_stage = _normalise_stage(row.get("stage")) or row.get("stage")
        tid = row.get("talent_id")
        is_follow_up = (
            canonical_stage == "ask_to_test"
            and bool(tid)
            and tid not in submitted_talent_ids
        )
        hydrated.append({
            **row,
            # Normalise legacy stages (`sent` → `approved`) at read time so
            # the frontend never sees deprecated values. The underlying
            # document is not rewritten — a future backfill can clean up.
            "stage": canonical_stage,
            "is_follow_up": is_follow_up,
            **by_id.get(tid, _EMPTY_MERGE),
        })
    return {"success": True, "data": hydrated}


# ---------------------------------------------------------------------------
# POST /api/projects/{project_id}/pipeline/add
# ---------------------------------------------------------------------------
@router.post("/{project_id}/pipeline/add", status_code=status.HTTP_201_CREATED)
async def add_to_pipeline(
    project_id: str,
    payload: PipelineAddIn,
    _admin: dict = Depends(current_team_or_admin),
):
    """Bulk-add talents to the pipeline. Idempotent: any (project_id,
    talent_id) pair that already exists is silently skipped, so the same
    add request can be retried without producing duplicates.

    Returns ``{added: N, skipped: N}`` so the UI can render a precise toast.
    """
    if not await _project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    # Normalise + dedup input ids in one pass.
    raw_ids = payload.talent_ids or []
    talent_ids = []
    seen = set()
    for raw in raw_ids:
        if not isinstance(raw, str):
            continue
        cid = raw.strip()
        if cid and cid not in seen:
            seen.add(cid)
            talent_ids.append(cid)

    if not talent_ids:
        return {"success": True, "added": 0, "skipped": 0, "data": []}

    # Stage normalisation: accept legacy aliases (e.g. `sent` → `approved`),
    # validate against the canonical registry, fall back to default if
    # caller omits the field. Reject explicitly unknown stages with 400
    # so a typo doesn't silently create rows in the default lane.
    if payload.stage is None:
        stage = DEFAULT_STAGE
    else:
        normalised = _normalise_stage(payload.stage)
        if normalised not in PIPELINE_STAGES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid stage. Must be one of: {PIPELINE_STAGE_ORDER}",
            )
        stage = normalised

    # One round-trip to find existing pairs — cheaper than per-id `update_one`
    # with upsert because the duplicate set is usually small.
    existing_cursor = db.casting_pipeline.find(
        {"project_id": project_id, "talent_id": {"$in": talent_ids}},
        {"_id": 0, "talent_id": 1},
    )
    existing_ids = {
        d["talent_id"] for d in await existing_cursor.to_list(len(talent_ids))
    }

    now = _now()
    new_docs = [
        {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "talent_id": tid,
            "stage": stage,
            "created_at": now,
            "updated_at": now,
        }
        for tid in talent_ids
        if tid not in existing_ids
    ]

    if new_docs:
        await db.casting_pipeline.insert_many(new_docs)
        # Strip Mongo's mutated _id before returning so the response is JSON-safe.
        for d in new_docs:
            d.pop("_id", None)

    return {
        "success": True,
        "added": len(new_docs),
        "skipped": len(existing_ids),
        "data": new_docs,
    }


# ---------------------------------------------------------------------------
# POST /api/projects/bulk-add-talents
#
# Cross-project counterpart to /{project_id}/pipeline/add — the Global
# Talent Directory's bulk "Add to Project" action. Adds many talents into
# many projects' pipelines in ONE request: one query to validate the
# projects, one to validate the talents, one to find every existing
# (project_id, talent_id) pair across all selected projects, and one
# insert_many for everything that's new. No per-(project, talent) API calls,
# no N+1 inserts.
# ---------------------------------------------------------------------------
@router.post("/bulk-add-talents", status_code=status.HTTP_201_CREATED)
async def bulk_add_talents_to_projects(
    payload: BulkProjectAddIn,
    admin: dict = Depends(current_team_or_admin),
):
    """Bulk-add talents into multiple ACTIVE projects' pipelines.

    Idempotent per (project_id, talent_id): a pair already in the pipeline
    is silently skipped, never duplicated (backed by the casting_pipeline
    unique index as a DB-level safety net against races on top of the
    application-level pre-check below). Rejects the whole request if any
    selected project doesn't exist or isn't `ongoing` — the picker only ever
    offers active projects, so this should only fire on a stale selection.
    """
    # Normalise + dedup ids (mirrors add_to_pipeline's handling).
    def _clean_ids(raw_list: List[str]) -> List[str]:
        seen: set = set()
        out: List[str] = []
        for raw in raw_list or []:
            if not isinstance(raw, str):
                continue
            cid = raw.strip()
            if cid and cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out

    project_ids = _clean_ids(payload.project_ids)
    talent_ids = _clean_ids(payload.talent_ids)

    if not project_ids:
        raise HTTPException(status_code=400, detail="Select at least one project")
    if not talent_ids:
        raise HTTPException(status_code=400, detail="Select at least one talent")

    # Validate projects: must exist AND be `ongoing` (the only status that
    # accepts new pipeline entries — hold/complete/locked are all rejected).
    projects = await db.projects.find(
        {"id": {"$in": project_ids}},
        {"_id": 0, "id": 1, "brand_name": 1, "status": 1},
    ).to_list(len(project_ids))
    projects_by_id = {p["id"]: p for p in projects}

    missing = [pid for pid in project_ids if pid not in projects_by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"Project(s) not found: {missing}")

    inactive = [projects_by_id[pid] for pid in project_ids if projects_by_id[pid].get("status") != "ongoing"]
    if inactive:
        names = [p.get("brand_name") or p["id"] for p in inactive]
        raise HTTPException(
            status_code=400,
            detail=f"Cannot add talents to inactive project(s): {', '.join(names)}",
        )

    # Validate talents: drop any id that doesn't correspond to a real talent
    # (defensive — the picker only ever sends ids for talents it just
    # rendered, so this should be a no-op in normal use).
    valid_talent_docs = await db.talents.find(
        {"id": {"$in": talent_ids}}, {"_id": 0, "id": 1}
    ).to_list(len(talent_ids))
    valid_talent_ids = {t["id"] for t in valid_talent_docs}
    talent_ids = [tid for tid in talent_ids if tid in valid_talent_ids]
    if not talent_ids:
        raise HTTPException(status_code=400, detail="No valid talents to add")

    # Stage: explicit override (validated) or the configured pipeline
    # default — never hardcoded here.
    if payload.stage is None:
        stage = DEFAULT_STAGE
    else:
        normalised = _normalise_stage(payload.stage)
        if normalised not in PIPELINE_STAGES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid stage. Must be one of: {PIPELINE_STAGE_ORDER}",
            )
        stage = normalised

    # One query for every existing (project_id, talent_id) pair across ALL
    # selected projects — avoids a per-project existence check.
    existing_pairs = {
        (d["project_id"], d["talent_id"])
        for d in await db.casting_pipeline.find(
            {"project_id": {"$in": project_ids}, "talent_id": {"$in": talent_ids}},
            {"_id": 0, "project_id": 1, "talent_id": 1},
        ).to_list(len(project_ids) * len(talent_ids))
    }

    now = _now()
    new_docs = []
    per_project_added: dict = {pid: 0 for pid in project_ids}
    per_project_skipped: dict = {pid: 0 for pid in project_ids}
    for pid in project_ids:
        for tid in talent_ids:
            if (pid, tid) in existing_pairs:
                per_project_skipped[pid] += 1
                continue
            per_project_added[pid] += 1
            new_docs.append({
                "id": str(uuid.uuid4()),
                "project_id": pid,
                "talent_id": tid,
                "stage": stage,
                "created_at": now,
                "updated_at": now,
            })

    inserted = 0
    if new_docs:
        try:
            result = await db.casting_pipeline.insert_many(new_docs, ordered=False)
            inserted = len(result.inserted_ids)
        except BulkWriteError as bwe:
            # The unique (project_id, talent_id) index caught a race our
            # pre-check missed (e.g. a concurrent add of the same pair).
            # Whatever DID land is reported accurately via nInserted; the
            # rest are effectively "skipped" (already there by the time the
            # write landed) rather than a hard failure.
            inserted = bwe.details.get("nInserted", 0)
            logger.warning(
                "bulk_add_talents_to_projects: %d/%d inserts raced with a duplicate pair",
                len(new_docs) - inserted, len(new_docs),
            )

    total_skipped = (len(project_ids) * len(talent_ids)) - inserted

    return {
        "success": True,
        "added": inserted,
        "skipped": total_skipped,
        "project_count": len(project_ids),
        "talent_count": len(talent_ids),
        "projects": [
            {
                "project_id": pid,
                "brand_name": projects_by_id[pid].get("brand_name"),
                "added": per_project_added[pid],
                "skipped": per_project_skipped[pid],
            }
            for pid in project_ids
        ],
    }


# ---------------------------------------------------------------------------
# PATCH /api/projects/{project_id}/pipeline/move
# ---------------------------------------------------------------------------
@router.patch("/{project_id}/pipeline/move")
async def move_pipeline(
    project_id: str,
    payload: PipelineMoveIn,
    _admin: dict = Depends(current_team_or_admin),
):
    """Bulk-update the ``stage`` of one or more pipeline rows.

    Always scoped to ``project_id`` in the WHERE clause — a payload crafted
    to reference rows from another project simply won't match and returns
    ``moved: 0``.
    """
    # Normalise legacy aliases (`sent` → `approved`) before validating, so
    # frontends transitioning during the rollout keep working.
    target_stage = _normalise_stage(payload.stage)
    if target_stage not in PIPELINE_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid stage. Must be one of: {PIPELINE_STAGE_ORDER}",
        )

    ids = [i for i in (payload.ids or []) if isinstance(i, str) and i.strip()]
    if not ids:
        return {"success": True, "moved": 0}

    res = await db.casting_pipeline.update_many(
        {"project_id": project_id, "id": {"$in": ids}},
        {"$set": {"stage": target_stage, "updated_at": _now()}},
    )
    logger.info(
        "pipeline.move project=%s stage=%s requested=%d matched=%d modified=%d",
        project_id, target_stage, len(ids), res.matched_count, res.modified_count,
    )
    return {
        "success": True,
        "moved": res.modified_count,
        "matched": res.matched_count,
    }


# ---------------------------------------------------------------------------
# DELETE /api/projects/{project_id}/pipeline/{entry_id}
# ---------------------------------------------------------------------------
@router.delete("/{project_id}/pipeline/{entry_id}")
async def delete_pipeline_entry(
    project_id: str,
    entry_id: str,
    _admin: dict = Depends(current_team_or_admin),
):
    """Remove a single pipeline row. Returns 404 if the row doesn't belong
    to this project (or doesn't exist) so the admin gets a clear signal
    instead of a silent no-op."""
    res = await db.casting_pipeline.delete_one(
        {"project_id": project_id, "id": entry_id}
    )
    if not res.deleted_count:
        raise HTTPException(status_code=404, detail="Pipeline entry not found")
    return {"success": True, "deleted": entry_id}


# ---------------------------------------------------------------------------
# Bulk Actions Endpoints
# ---------------------------------------------------------------------------

@router.post("/{project_id}/pipeline/bulk-move")
async def bulk_move_pipeline(
    project_id: str,
    payload: PipelineBulkMoveIn,
    _admin: dict = Depends(current_team_or_admin),
):
    """Bulk move selected talents by talent_ids inside the project to a target stage."""
    target_stage = _normalise_stage(payload.stage)
    if target_stage not in PIPELINE_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid stage. Must be one of: {PIPELINE_STAGE_ORDER}",
        )
    talent_ids = [tid.strip() for tid in payload.talent_ids if isinstance(tid, str) and tid.strip()]
    if not talent_ids:
        return {"success": True, "moved": 0, "matched": 0}

    res = await db.casting_pipeline.update_many(
        {"project_id": project_id, "talent_id": {"$in": talent_ids}},
        {"$set": {"stage": target_stage, "updated_at": _now()}},
    )
    logger.info(
        "pipeline.bulk-move project=%s stage=%s requested=%d matched=%d modified=%d",
        project_id, target_stage, len(talent_ids), res.matched_count, res.modified_count,
    )
    return {
        "success": True,
        "moved": res.modified_count,
        "matched": res.matched_count,
    }


@router.post("/{project_id}/pipeline/bulk-label")
async def bulk_label_pipeline(
    project_id: str,
    payload: PipelineBulkLabelIn,
    _admin: dict = Depends(current_team_or_admin),
):
    """Bulk add/remove labels (tags) on talents' profiles directly."""
    talent_ids = [tid.strip() for tid in payload.talent_ids if isinstance(tid, str) and tid.strip()]
    if not talent_ids or not payload.labels:
        return {"success": True, "updated": 0}

    updated_count = 0
    if payload.action == "add":
        for label in payload.labels:
            label = label.strip()
            if not label:
                continue
            normalized = label.lower().strip()
            tag_doc = await db.tags.find_one({"normalized_name": normalized})
            if not tag_doc:
                tag_id = str(uuid.uuid4())
                tag_doc = {
                    "id": tag_id,
                    "name": label,
                    "normalized_name": normalized,
                    "created_at": _now(),
                }
                await db.tags.insert_one(tag_doc)
            else:
                tag_id = tag_doc["id"]
            
            tag_obj = {"id": tag_id, "name": label}
            
            res = await db.talents.update_many(
                {"id": {"$in": talent_ids}, "tags.id": {"$ne": tag_id}},
                {"$push": {"tags": tag_obj}}
            )
            updated_count += res.modified_count
            
    elif payload.action == "remove":
        for label in payload.labels:
            normalized = label.lower().strip()
            tag_doc = await db.tags.find_one({"normalized_name": normalized})
            if tag_doc:
                tag_id = tag_doc["id"]
                res = await db.talents.update_many(
                    {"id": {"$in": talent_ids}},
                    {"$pull": {"tags": {"id": tag_id}}}
                )
                updated_count += res.modified_count
                
    return {"success": True, "updated": updated_count}


@router.post("/{project_id}/pipeline/bulk-note")
async def bulk_note_pipeline(
    project_id: str,
    payload: PipelineBulkNoteIn,
    _admin: dict = Depends(current_team_or_admin),
):
    """Bulk append an internal note to the talents' documents."""
    talent_ids = [tid.strip() for tid in payload.talent_ids if isinstance(tid, str) and tid.strip()]
    note = payload.note.strip()
    if not talent_ids or not note:
        return {"success": True, "updated": 0}

    updated_count = 0
    cursor = db.talents.find({"id": {"$in": talent_ids}}, {"id": 1, "notes": 1})
    async for t in cursor:
        existing = t.get("notes") or ""
        if existing:
            new_note = f"{existing}\n\n{note}"
        else:
            new_note = note
            
        res = await db.talents.update_one(
            {"id": t["id"]},
            {"$set": {"notes": new_note, "updated_at": _now()}}
        )
        updated_count += res.modified_count
        
    return {"success": True, "updated": updated_count}


@router.post("/{project_id}/pipeline/bulk-delete")
async def bulk_delete_pipeline(
    project_id: str,
    payload: PipelineBulkDeleteIn,
    _admin: dict = Depends(current_team_or_admin),
):
    """Bulk remove selected pipeline entries from the project."""
    talent_ids = [tid.strip() for tid in payload.talent_ids if isinstance(tid, str) and tid.strip()]
    if not talent_ids:
        return {"success": True, "deleted": 0}

    res = await db.casting_pipeline.delete_many(
        {"project_id": project_id, "talent_id": {"$in": talent_ids}}
    )
    return {"success": True, "deleted": res.deleted_count}


@router.post("/{project_id}/pipeline/bulk-export")
async def bulk_export_pipeline(
    project_id: str,
    payload: PipelineBulkExportIn,
    _admin: dict = Depends(current_team_or_admin),
):
    """Bulk export selected talents' profile details."""
    talent_ids = [tid.strip() for tid in payload.talent_ids if isinstance(tid, str) and tid.strip()]
    if not talent_ids:
        return {"success": True, "talents": []}

    talents_cursor = db.talents.find(
        {"id": {"$in": talent_ids}},
        {
            "_id": 0,
            "id": 1,
            "name": 1,
            "email": 1,
            "phone": 1,
            "gender": 1,
            "height": 1,
            "location": 1,
            "skills": 1,
            "tags": 1,
        }
    )
    talents_list = await talents_cursor.to_list(len(talent_ids))
    return {"success": True, "talents": talents_list}
