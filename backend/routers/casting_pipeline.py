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


class PipelineMoveIn(BaseModel):
    ids: List[str] = Field(default_factory=list)
    stage: str


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

    hydrated = [
        {
            **row,
            # Normalise legacy stages (`sent` → `approved`) at read time so
            # the frontend never sees deprecated values. The underlying
            # document is not rewritten — a future backfill can clean up.
            "stage": _normalise_stage(row.get("stage")) or row.get("stage"),
            **by_id.get(row.get("talent_id"), _EMPTY_MERGE),
        }
        for row in rows
    ]
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
