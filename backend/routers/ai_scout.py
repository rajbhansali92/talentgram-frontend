"""AI Scout — Gate 2 router (thin).

    GET  /api/ai-scout/health
    GET  /api/ai-scout/projects/{pid}/criteria        resolve editable scouting criteria
    POST /api/ai-scout/projects/{pid}/run             filter -> rank -> persist a run
    GET  /api/ai-scout/projects/{pid}/runs/latest      cached last run (page reload)
    POST /api/ai-scout/projects/{pid}/select          add human-selected talents to the pipeline

Owns one collection: ``ai_scout_runs`` (a workflow/audit record — the ranked
list, the criteria snapshot, and which talents the admin added). Every real
write is delegated:

    pipeline add  ->  routers.casting_pipeline.add_talents_to_pipeline(pid, ids, "ask_to_test")

The AI never writes to the pipeline. It ranks; the admin selects; the existing
idempotent pipeline function executes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core import _now, current_team_or_admin, db
from ai import client as llm
from ai import scout
from routers import casting_pipeline as pipeline_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai-scout", tags=["AI Scout"])

COLLECTION = "ai_scout_runs"
ASK_TO_TEST = "ask_to_test"

STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_NO_CANDIDATES = "no_candidates"
STATUS_ERROR = "error"

_TIER_RANK = {"top": 0, "strong": 1, "possible": 2}


class RunIn(BaseModel):
    criteria: Optional[Dict[str, Any]] = None
    force: bool = False


class SelectIn(BaseModel):
    run_id: str
    talent_ids: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _project_or_404(pid: str) -> Dict[str, Any]:
    p = await db.projects.find_one({"id": pid}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Project not found")
    return p


def _criteria_hash(crit: Dict[str, Any]) -> str:
    payload = {k: crit.get(k) for k in scout.EMPTY_CRITERIA}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


async def _resolve_criteria(project: Dict[str, Any]) -> Dict[str, Any]:
    """Gate-1 session (best) -> 1 small LLM call -> deterministic text."""
    session = await db.casting_desk_sessions.find_one(
        {"project_id": project["id"], "status": "project_created", "extraction": {"$ne": None}},
        sort=[("created_at", -1)],
    )
    if session and session.get("extraction"):
        crit = scout.criteria_from_gate1_extraction(session["extraction"])
        source = "gate1_session"
    elif llm.is_configured():
        try:
            crit = await scout.extract_criteria_via_llm(project)
            source = "llm"
        except (llm.LLMUnavailable, llm.LLMError) as exc:
            logger.warning("scout criteria LLM extraction failed, falling back: %s", exc)
            crit = _criteria_from_text(project)
            source = "manual"
    else:
        crit = _criteria_from_text(project)
        source = "manual"

    sr = (project.get("submission_requirements") or {}).get("fields", {})
    if (sr.get("competitive_brand") == "required" or project.get("competitive_brand_enabled")) and not crit.get("competitive_brands_note"):
        crit["competitive_brands_note"] = "Competitive-brand / conflict restriction applies (see project)."
    crit["_source"] = source
    return crit


def _criteria_from_text(project: Dict[str, Any]) -> Dict[str, Any]:
    crit = dict(scout.EMPTY_CRITERIA)
    blob = " ".join([project.get("character") or "", project.get("additional_details") or ""])
    crit["character_summary"] = (project.get("character") or blob).strip()[:800]
    low = blob.lower()
    if re.search(r"\bfemale\b|\bwoman\b|\bwomen\b|\bgirl\b", low):
        crit["gender"] = "female"
    elif re.search(r"\bmale\b|\bman\b|\bmen\b|\bboy\b", low):
        crit["gender"] = "male"
    crit["age_min"], crit["age_max"] = scout._parse_age_range(blob)
    return crit


def _public(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    return doc


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.get("/health")
async def health(_u: dict = Depends(current_team_or_admin)):
    return {
        "llm_configured": llm.is_configured(),
        "model": scout.SCOUT_MODEL,
        "max_candidates": scout.MAX_CANDIDATES,
        "batch_size": scout.BATCH_SIZE,
    }


@router.get("/projects/{pid}/criteria")
async def get_criteria(pid: str, _u: dict = Depends(current_team_or_admin)):
    project = await _project_or_404(pid)
    crit = await _resolve_criteria(project)
    source = crit.pop("_source", "manual")
    return {
        "project_id": pid,
        "project_brand": project.get("brand_name"),
        "source": source,
        "criteria": scout.normalise_criteria(crit),
        "categories_available": scout.TALENT_CATEGORIES,
    }


@router.get("/projects/{pid}/runs/latest")
async def latest_run(pid: str, _u: dict = Depends(current_team_or_admin)):
    await _project_or_404(pid)
    doc = await db[COLLECTION].find_one(
        {"project_id": pid, "status": {"$in": [STATUS_COMPLETE, STATUS_NO_CANDIDATES]}},
        sort=[("created_at", -1)],
    )
    if not doc:
        return {"run": None}
    return {"run": await _attach_live_pipeline(_public(doc), pid)}


async def _attach_live_pipeline(run: Dict[str, Any], pid: str) -> Dict[str, Any]:
    """Refresh each result's in_pipeline_stage from the live pipeline so a
    reloaded run reflects talents added since (here or in the kanban)."""
    rows = await db.casting_pipeline.find({"project_id": pid}, {"_id": 0, "talent_id": 1, "stage": 1}).to_list(5000)
    by_id = {r["talent_id"]: pipeline_router._normalise_stage(r.get("stage")) or r.get("stage") for r in rows}
    for r in run.get("results", []):
        r["in_pipeline_stage"] = by_id.get(r["talent_id"])
    return run


@router.post("/projects/{pid}/run")
async def run_scout(pid: str, payload: RunIn, user: dict = Depends(current_team_or_admin)):
    project = await _project_or_404(pid)

    if payload.criteria:
        crit = scout.normalise_criteria(payload.criteria)
        source = "manual"
    else:
        resolved = await _resolve_criteria(project)
        source = resolved.pop("_source", "manual")
        crit = scout.normalise_criteria(resolved)

    chash = _criteria_hash(crit)

    # cache: reuse the last complete run with identical criteria
    if not payload.force:
        cached = await db[COLLECTION].find_one(
            {"project_id": pid, "criteria_hash": chash, "status": {"$in": [STATUS_COMPLETE, STATUS_NO_CANDIDATES]}},
            sort=[("created_at", -1)],
        )
        if cached:
            return {"run": await _attach_live_pipeline(_public(cached), pid), "cached": True}

    run_id = str(uuid.uuid4())
    now = _now()
    base_doc = {
        "id": run_id, "project_id": pid,
        "created_by": user["id"], "created_by_email": user.get("email"),
        "created_at": now, "updated_at": now,
        "status": STATUS_RUNNING, "model": scout.SCOUT_MODEL,
        "criteria": crit, "criteria_source": source, "criteria_hash": chash,
        "scanned_count": 0, "candidate_count": 0, "truncated": False,
        "results": [], "tier_counts": {"top": 0, "strong": 0, "possible": 0},
        "selections": [], "selected_talent_ids": [], "error": None,
    }
    await db[COLLECTION].insert_one(dict(base_doc))

    try:
        run = await _execute_run(run_id, project, crit)
    except (llm.LLMUnavailable, llm.LLMError) as exc:
        code = 503 if isinstance(exc, llm.LLMUnavailable) else 502
        await db[COLLECTION].update_one({"id": run_id}, {"$set": {
            "status": STATUS_ERROR, "error": str(exc), "updated_at": _now(),
        }})
        raise HTTPException(code, f"AI ranking failed: {exc}")
    except Exception as exc:  # pragma: no cover
        logger.exception("scout run crashed")
        await db[COLLECTION].update_one({"id": run_id}, {"$set": {
            "status": STATUS_ERROR, "error": f"{exc}", "updated_at": _now(),
        }})
        raise HTTPException(500, "Scout run failed unexpectedly")

    return {"run": run, "cached": False}


async def _execute_run(run_id: str, project: Dict[str, Any], crit: Dict[str, Any]) -> Dict[str, Any]:
    query = scout.build_candidate_query(crit)
    active = {"status": {"$nin": ["DRAFT", "ARCHIVED", "MERGED"]}}
    scanned_count = await db.talents.count_documents(active)
    pool_count = await db.talents.count_documents(query)

    fetch_cap = scout.MAX_CANDIDATES + 100
    candidates = await db.talents.find(query, scout.CANDIDATE_PROJECTION).to_list(fetch_cap)

    truncated = False
    if len(candidates) > scout.MAX_CANDIDATES:
        candidates.sort(key=scout.profile_confidence, reverse=True)
        candidates = candidates[: scout.MAX_CANDIDATES]
        truncated = True

    if not candidates:
        doc = {
            "status": STATUS_NO_CANDIDATES, "updated_at": _now(),
            "scanned_count": scanned_count, "candidate_count": 0,
            "truncated": False, "results": [], "tier_counts": {"top": 0, "strong": 0, "possible": 0},
        }
        await db[COLLECTION].update_one({"id": run_id}, {"$set": doc})
        return _public(await db[COLLECTION].find_one({"id": run_id}))

    project_ctx = {
        "brand_name": project.get("brand_name"),
        "medium_usage": project.get("medium_usage"),
        "character_summary": crit.get("character_summary"),
        "categories": crit.get("categories"),
        "competitive_brands_note": crit.get("competitive_brands_note"),
    }
    ai_rows = await scout.rank_candidates(project_ctx, candidates)

    pipeline_rows = await db.casting_pipeline.find(
        {"project_id": project["id"]}, {"_id": 0, "talent_id": 1, "stage": 1}
    ).to_list(5000)
    in_pipeline = {r["talent_id"]: pipeline_router._normalise_stage(r.get("stage")) or r.get("stage") for r in pipeline_rows}

    results: List[Dict[str, Any]] = []
    for t in candidates:
        res = scout.assemble_result(t, crit, ai_rows.get(t.get("id")))
        res["tier"] = scout.tier(res)
        res["in_pipeline_stage"] = in_pipeline.get(t.get("id"))
        results.append(res)

    results.sort(key=lambda r: (
        _TIER_RANK.get(r["tier"], 3),
        -(r["overall"] if r["overall"] is not None else -1),
        -(r["confidence"] or 0),
    ))

    tier_counts = {"top": 0, "strong": 0, "possible": 0}
    for r in results:
        tier_counts[r["tier"]] = tier_counts.get(r["tier"], 0) + 1

    doc = {
        "status": STATUS_COMPLETE, "updated_at": _now(),
        "scanned_count": scanned_count, "candidate_count": len(results),
        "pool_count": pool_count, "truncated": truncated,
        "results": results, "tier_counts": tier_counts,
    }
    await db[COLLECTION].update_one({"id": run_id}, {"$set": doc})
    return _public(await db[COLLECTION].find_one({"id": run_id}))


@router.post("/projects/{pid}/select")
async def select_talents(pid: str, payload: SelectIn, user: dict = Depends(current_team_or_admin)):
    project = await _project_or_404(pid)
    run = await db[COLLECTION].find_one({"id": payload.run_id, "project_id": pid})
    if not run:
        raise HTTPException(404, "Scout run not found")
    if run["status"] != STATUS_COMPLETE:
        raise HTTPException(409, "This scout run has no results to select from")

    ranked_ids = {r["talent_id"] for r in run.get("results", [])}
    ids = [t for t in dict.fromkeys(payload.talent_ids) if t in ranked_ids]
    if not ids:
        raise HTTPException(400, "Select at least one talent from the scout results")

    # --- existing idempotent pipeline write ---
    result = await pipeline_router.add_talents_to_pipeline(pid, ids, ASK_TO_TEST)

    added_ids = [d.get("talent_id") for d in result.get("added_docs", [])]
    skipped_ids = list(result.get("skipped_ids", []))

    selection = {
        "at": _now(), "by": user.get("email") or user["id"],
        "requested": ids, "added": added_ids, "skipped_already_present": skipped_ids,
    }
    await db[COLLECTION].update_one(
        {"id": payload.run_id},
        {
            "$push": {"selections": selection},
            "$addToSet": {"selected_talent_ids": {"$each": ids}},
            "$set": {"updated_at": _now()},
        },
    )

    # live stage map for the affected talents so the UI can flip them to "Already in..."
    rows = await db.casting_pipeline.find(
        {"project_id": pid, "talent_id": {"$in": ids}}, {"_id": 0, "talent_id": 1, "stage": 1}
    ).to_list(len(ids))
    stage_map = {r["talent_id"]: pipeline_router._normalise_stage(r.get("stage")) or r.get("stage") for r in rows}

    return {
        "added": result["added"],
        "skipped": result["skipped"],
        "already_in_pipeline": skipped_ids,
        "stage_map": stage_map,
        "stage": ASK_TO_TEST,
    }
