"""Manual Talent Merge — write-capable executor for admin-initiated,
two-record merges from the Global Talents roster.

Deliberately a SEPARATE module from `migrations/talent_dedup_scan.py` (that
module's own docstring: "If a future phase adds a write-capable merge
executor, it belongs in a SEPARATE module (never this one)"). Reuses the
scanner's read-only relationship-counting/collection-map helpers, but field
precedence here is INTENTIONALLY DIFFERENT from both the scanner's
scored-canonical logic and `core.py`'s real-time `_auto_link_migration_candidate`:

  Real-time auto-link (core.py):  talent's fresh self-report wins over an
                                   older admin value for REVIEW_FIELDS.
  This module (manual merge):     the ADMIN-CHOSEN canonical record wins
                                   whenever it has a value, full stop — the
                                   admin's explicit choice of "which profile
                                   is authoritative" already settles the
                                   question Part 1's logic exists to answer
                                   automatically. Only fills a field from the
                                   non-canonical side when canonical is empty.

No MongoDB replica-set transactions are used anywhere else in this codebase
(`grep -rn "start_session\\|start_transaction"` across backend/ is empty)
and none are assumed available here. Safety instead comes from:
  1. An atomic `find_one_and_update` claim on the duplicate record BEFORE
     any other write — this is the sole point of no return, and it is what
     makes double-clicks/concurrent-admin-merges/network-retries safe.
  2. A `talent_merges` operation record created immediately after the claim
     succeeds, with `status` "in_progress" -> "completed"/"failed" — every
     subsequent write in `_apply_merge_steps` is itself idempotent (matching
     on the OLD id, which naturally stops matching after the first
     successful pass), so a retry of the exact same (canonical, duplicate)
     pair safely RESUMES rather than re-applying or corrupting anything.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core import (
    db, _now, compute_age, update_talent_cover_cache,
    REVIEW_FIELDS, PRESERVE_FIELDS,
)
from migrations.talent_dedup_scan import (
    _SCALAR_TALENT_REF_COLLECTIONS, _ARRAY_TALENT_REF_COLLECTIONS,
    _SCALAR_AUTO_UPDATE_FIELDS, _LIST_AUTO_UPDATE_FIELDS,
    _relationship_counts, _is_empty, _norm as _normalize_identity_value,
    _completeness_score,
)

logger = logging.getLogger("talentgram")

_MERGE_STALE_IN_PROGRESS_SECONDS = 120

# Identity fields surfaced as conflicts for admin visibility (Part 22: an
# explicit two-record selection is NOT gated by these — the admin's
# selection is the authority — but they must never be silently invisible).
_DISPLAY_CONFLICT_FIELDS = [
    "name", "dob", "email", "phone", "instagram_handle", "whatsapp_group_name",
]


class MergeError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def _parse_iso(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Field precedence — canonical wins if non-empty, else filled from the other
# side. Mirrors the scanner's field classification (REVIEW_FIELDS, scalar
# AUTO_UPDATE_FIELDS excluding location, list AUTO_UPDATE_FIELDS as union,
# tags as union, notes concatenated, PRESERVE_FIELDS fill-if-empty-only,
# location NEVER auto-merged) but with the simpler, uniform
# canonical-wins-if-present rule throughout (Part 6 of this task's spec) —
# no aggressive override case, because the admin's canonical choice already
# IS the authority decision Part 1's aggressive-override existed to infer
# automatically.
# --------------------------------------------------------------------------
def _identity_conflicts(canonical: dict, other: dict) -> List[Dict[str, Any]]:
    conflicts = []
    for field in _DISPLAY_CONFLICT_FIELDS:
        a, b = canonical.get(field), other.get(field)
        an, bn = _normalize_identity_value(a), _normalize_identity_value(b)
        if an and bn and an != bn:
            conflicts.append({"field": field, "canonical": a, "other": b})
    return conflicts


def _field_changes(canonical: dict, other: dict) -> Dict[str, Dict[str, Any]]:
    changes: Dict[str, Dict[str, Any]] = {}

    for field in REVIEW_FIELDS | _SCALAR_AUTO_UPDATE_FIELDS:
        c_val, o_val = canonical.get(field), other.get(field)
        proposed = c_val if not _is_empty(c_val) else o_val
        if proposed != c_val:
            changes[field] = {"canonical": c_val, "other": o_val, "proposed": proposed}

    if "dob" in changes:
        age = compute_age(changes["dob"]["proposed"])
        if age is not None:
            changes["age"] = {"canonical": canonical.get("age"), "other": other.get("age"), "proposed": age}

    for field in _LIST_AUTO_UPDATE_FIELDS:
        c_list, o_list = canonical.get(field) or [], other.get(field) or []
        union = list(dict.fromkeys(list(c_list) + [x for x in o_list if x not in c_list]))
        if union != c_list:
            changes[field] = {"canonical": c_list, "other": o_list, "proposed": union}

    c_tags, o_tags = canonical.get("tags") or [], other.get("tags") or []
    c_tag_ids = {t.get("id") for t in c_tags if isinstance(t, dict)}
    union_tags = list(c_tags) + [t for t in o_tags if isinstance(t, dict) and t.get("id") not in c_tag_ids]
    if union_tags != c_tags:
        changes["tags"] = {"canonical": c_tags, "other": o_tags, "proposed": union_tags}

    c_notes, o_notes = (canonical.get("notes") or ""), (other.get("notes") or "")
    if o_notes and o_notes.strip() not in c_notes:
        proposed_notes = (c_notes + ("\n\n" if c_notes else "") + f"[merged from {other['id']}]: {o_notes}").strip()
        changes["notes"] = {"canonical": c_notes, "other": o_notes, "proposed": proposed_notes}

    # PRESERVE_FIELDS (admin-only, fill-if-empty only — Part 13's WhatsApp
    # caution applies here: whatsapp_group_name is NEVER overwritten if
    # canonical already has one, only filled if canonical has none).
    for field in PRESERVE_FIELDS - {"status", "tags", "notes"}:
        c_val, o_val = canonical.get(field), other.get(field)
        if _is_empty(c_val) and not _is_empty(o_val):
            changes[field] = {"canonical": c_val, "other": o_val, "proposed": o_val}

    if _is_empty(canonical.get("email")) and not _is_empty(other.get("email")):
        changes["email"] = {"canonical": canonical.get("email"), "other": other.get("email"), "proposed": other.get("email")}
        changes["normalized_email"] = {
            "canonical": canonical.get("normalized_email"), "other": other.get("normalized_email"),
            "proposed": other.get("normalized_email") or other.get("email"),
        }

    # location is deliberately NEVER included here — Part 11: project-
    # specific submission answers must never become a global profile update,
    # and this function has no way to prove a talent doc's own `location`
    # field is genuinely global rather than leaked project data, so the
    # canonical's location is always left completely untouched.

    return changes


def _media_plan(canonical: dict, other: dict) -> Dict[str, Any]:
    c_media, o_media = canonical.get("media") or [], other.get("media") or []
    c_keys = {m.get("public_id") or m.get("id") for m in c_media}
    overlap = [m for m in o_media if (m.get("public_id") or m.get("id")) in c_keys]
    new_from_other = [m for m in o_media if (m.get("public_id") or m.get("id")) not in c_keys]
    proposed_media = c_media + new_from_other
    return {
        "canonical_count": len(c_media),
        "other_count": len(o_media),
        "overlap_count": len(overlap),
        "proposed_count": len(proposed_media),
        "proposed_media": proposed_media,
    }


async def _build_plan(canonical: dict, other: dict) -> Dict[str, Any]:
    field_changes = _field_changes(canonical, other)
    media = _media_plan(canonical, other)
    conflicts = _identity_conflicts(canonical, other)
    rel_canonical = await _relationship_counts(canonical["id"])
    rel_other = await _relationship_counts(other["id"])
    proposed_submissions = rel_canonical["submissions"] + rel_other["submissions"]
    tags_merged = len(field_changes.get("tags", {}).get("proposed", [])) - len(canonical.get("tags") or [])
    return {
        "field_changes": field_changes,
        "media": media,
        "conflicts": conflicts,
        "relationship_counts": {"canonical": rel_canonical, "other": rel_other},
        "proposed_submissions_total": proposed_submissions,
        "tags_merged_count": max(tags_merged, 0),
    }


# --------------------------------------------------------------------------
# Preview (read-only) — safe to call as many times as the UI needs.
# --------------------------------------------------------------------------
async def build_merge_preview(talent_a_id: str, talent_b_id: str, canonical_id: Optional[str] = None) -> Dict[str, Any]:
    if talent_a_id == talent_b_id:
        raise MergeError(400, "Cannot merge a talent with itself")

    talent_a = await db.talents.find_one({"id": talent_a_id}, {"_id": 0})
    talent_b = await db.talents.find_one({"id": talent_b_id}, {"_id": 0})
    if not talent_a:
        raise MergeError(404, f"Talent {talent_a_id} not found")
    if not talent_b:
        raise MergeError(404, f"Talent {talent_b_id} not found")

    rel_a = await _relationship_counts(talent_a_id)
    rel_b = await _relationship_counts(talent_b_id)
    score_a = _completeness_score(talent_a) * 10 + rel_a["submissions"] * 5 + len(talent_a.get("media") or []) * 3
    score_b = _completeness_score(talent_b) * 10 + rel_b["submissions"] * 5 + len(talent_b.get("media") or []) * 3
    src_a = ((talent_a.get("source") or {}).get("type")) if isinstance(talent_a.get("source"), dict) else talent_a.get("source")
    src_b = ((talent_b.get("source") or {}).get("type")) if isinstance(talent_b.get("source"), dict) else talent_b.get("source")
    score_a += 1000 if src_a != "audition_submission" else 0
    score_b += 1000 if src_b != "audition_submission" else 0
    recommended_id = talent_a_id if score_a >= score_b else talent_b_id
    recommendation_reason = (
        "more established profile" if abs(score_a - score_b) > 50
        else "similar completeness — review both before choosing"
    )

    result: Dict[str, Any] = {
        "talent_a": {**talent_a, "relationship_counts": rel_a, "media_count": len(talent_a.get("media") or [])},
        "talent_b": {**talent_b, "relationship_counts": rel_b, "media_count": len(talent_b.get("media") or [])},
        "recommended_canonical_id": recommended_id,
        "recommendation_reason": recommendation_reason,
        "identity_conflicts": _identity_conflicts(talent_a, talent_b),
        "either_already_merged": {
            talent_a_id: talent_a.get("status") == "MERGED",
            talent_b_id: talent_b.get("status") == "MERGED",
        },
    }

    if canonical_id:
        if canonical_id not in (talent_a_id, talent_b_id):
            raise MergeError(400, "canonical_id must be one of the two selected talents")
        canonical = talent_a if canonical_id == talent_a_id else talent_b
        other = talent_b if canonical_id == talent_a_id else talent_a
        plan = await _build_plan(canonical, other)
        result["merge_plan"] = {
            "canonical_talent_id": canonical["id"],
            "duplicate_talent_id": other["id"],
            **plan,
        }

    return result


# --------------------------------------------------------------------------
# Execute — the write path.
# --------------------------------------------------------------------------
def _operation_summary(op_doc: dict, *, resumed: bool, already_done: bool) -> Dict[str, Any]:
    return {
        "ok": True,
        "canonical_talent_id": op_doc["canonical_talent_id"],
        "duplicate_talent_id": op_doc["source_talent_id"],
        "already_merged": already_done,
        "resumed": resumed,
        "submissions_preserved": op_doc["relationship_counts"]["canonical"]["submissions"] + op_doc["relationship_counts"]["other"]["submissions"],
        "media_preserved": op_doc["media"]["proposed_count"],
        "tags_merged": op_doc["tags_merged_count"],
        "fields_updated": [f for f in op_doc["field_changes"].keys() if f not in ("age",)],
        "operation_id": op_doc["id"],
    }


async def _apply_merge_steps(op_doc: dict) -> None:
    """Every write below is idempotent — matches on the OLD (duplicate) id,
    which stops matching after the first successful pass, so re-running this
    exact function for the same operation is always safe (Part 16/19)."""
    canonical_id = op_doc["canonical_talent_id"]
    duplicate_id = op_doc["source_talent_id"]

    field_set = {k: v["proposed"] for k, v in op_doc["field_changes"].items()}
    field_set["media"] = op_doc["media"]["proposed_media"]
    field_set["updated_at"] = _now()

    if "email" in field_set:
        # Canonical is adopting the duplicate's email (Part 17 identity
        # linking, its own value was empty). `email`/`normalized_email`
        # carry a partial UNIQUE index -- if the duplicate still holds this
        # exact value when canonical's write lands, that write fails with
        # E11000, even though the duplicate is destined to lose the field
        # anyway. Clear it off the duplicate FIRST (excluding it from the
        # partial index, since the index only applies where the field is a
        # string), then apply it to canonical. Idempotent: on a resumed
        # retry the duplicate's email is already cleared, so this is a no-op.
        await db.talents.update_one(
            {"id": duplicate_id}, {"$unset": {"email": "", "normalized_email": ""}}
        )
    await db.talents.update_one({"id": canonical_id}, {"$set": field_set})
    await update_talent_cover_cache(canonical_id)

    # Plain scalar reassignment -- safe blanket update, these are all either
    # append-only event logs or single-owner references with no compound
    # natural key that could collide.
    for coll, field in _SCALAR_TALENT_REF_COLLECTIONS:
        await db[coll].update_many({field: duplicate_id}, {"$set": {field: canonical_id}})
    await db.feedback.update_many({"talent_id": duplicate_id}, {"$set": {"talent_id": canonical_id}})
    await db.whatsapp_jobs.update_many(
        {"recipient_id": duplicate_id, "recipient_kind": "talent"},
        {"$set": {"recipient_id": canonical_id}},
    )
    await db.talent_migration_candidates.update_many(
        {"legacy_talent_id": duplicate_id}, {"$set": {"legacy_talent_id": canonical_id}}
    )
    await db.talent_migration_candidates.update_many(
        {"authenticated_talent_id": duplicate_id}, {"$set": {"authenticated_talent_id": canonical_id}}
    )

    # Array-valued relationships.
    for coll, field in _ARRAY_TALENT_REF_COLLECTIONS:
        await db[coll].update_many({field: duplicate_id}, {"$addToSet": {field: canonical_id}})
        await db[coll].update_many({field: duplicate_id}, {"$pull": {field: duplicate_id}})

    # links.talent_field_visibility -- dict keyed by talent_id, needs a
    # per-document read+merge (canonical's own per-field settings win on a
    # key conflict, duplicate's fill in anything canonical didn't set).
    async for link in db.links.find(
        {f"talent_field_visibility.{duplicate_id}": {"$exists": True}},
        {"_id": 0, "id": 1, "talent_field_visibility": 1},
    ):
        tfv = dict(link.get("talent_field_visibility") or {})
        dup_entry = tfv.pop(duplicate_id, None)
        if dup_entry:
            tfv[canonical_id] = {**dup_entry, **(tfv.get(canonical_id) or {})}
            await db.links.update_one({"id": link["id"]}, {"$set": {"talent_field_visibility": tfv}})

    # link_actions -- natural key is (link_id, viewer_email, talent_id); a
    # blanket reassign could silently collide with an existing canonical row
    # for the same (link, viewer) pair. Merge comments/voice_notes instead
    # of guessing which side's action/comment should win.
    async for dup_action in db.link_actions.find({"talent_id": duplicate_id}, {"_id": 0}):
        canon_filter = {"link_id": dup_action["link_id"], "viewer_email": dup_action["viewer_email"], "talent_id": canonical_id}
        canon_action = await db.link_actions.find_one(canon_filter, {"_id": 0})
        if not canon_action:
            await db.link_actions.update_one(
                {"id": dup_action["id"]}, {"$set": {"talent_id": canonical_id}}
            )
            continue
        merged_comments = (canon_action.get("comments") or []) + (dup_action.get("comments") or [])
        merged_voice_notes = (canon_action.get("voice_notes") or []) + (dup_action.get("voice_notes") or [])
        await db.link_actions.update_one(
            {"id": canon_action["id"]},
            {"$set": {"comments": merged_comments, "voice_notes": merged_voice_notes, "updated_at": _now()}},
        )
        await db.link_actions.delete_one({"id": dup_action["id"]})

    # casting_pipeline -- natural key is (project_id, talent_id), "inserted
    # at most once" per pair. If canonical already has a row for the same
    # project (both talents were submitted to it before being recognized as
    # the same person), there is no safe way to auto-decide which `stage`
    # should win -- skip and record the collision for manual follow-up
    # rather than guess (Part 19: preserve or flag, never guess).
    skipped_pipeline_collisions = []
    async for dup_pipe in db.casting_pipeline.find({"talent_id": duplicate_id}, {"_id": 0}):
        canon_pipe = await db.casting_pipeline.find_one(
            {"project_id": dup_pipe["project_id"], "talent_id": canonical_id}, {"_id": 0}
        )
        if canon_pipe:
            skipped_pipeline_collisions.append({
                "project_id": dup_pipe["project_id"],
                "canonical_stage": canon_pipe.get("stage"),
                "duplicate_stage": dup_pipe.get("stage"),
            })
            continue
        await db.casting_pipeline.update_one(
            {"id": dup_pipe["id"]}, {"$set": {"talent_id": canonical_id}}
        )
    if skipped_pipeline_collisions:
        await db.talent_merges.update_one(
            {"id": op_doc["id"]}, {"$set": {"skipped_pipeline_collisions": skipped_pipeline_collisions}}
        )


async def execute_merge(canonical_id: str, duplicate_id: str, *, operator: str) -> Dict[str, Any]:
    if canonical_id == duplicate_id:
        raise MergeError(400, "canonical_talent_id and duplicate_talent_id must be different")

    canonical = await db.talents.find_one({"id": canonical_id}, {"_id": 0})
    if not canonical:
        raise MergeError(404, f"Talent {canonical_id} not found")
    if canonical.get("status") == "MERGED":
        raise MergeError(409, "The chosen canonical talent is itself already MERGED into another record")

    duplicate = await db.talents.find_one({"id": duplicate_id}, {"_id": 0})
    if not duplicate:
        raise MergeError(404, f"Talent {duplicate_id} not found")

    if duplicate.get("status") == "MERGED":
        if duplicate.get("merged_into") != canonical_id:
            raise MergeError(
                409,
                f"Talent {duplicate_id} is already merged into a different talent "
                f"({duplicate.get('merged_into')}) -- cannot also merge it into {canonical_id}",
            )
        op_id = duplicate.get("merge_operation_id")
        existing_op = await db.talent_merges.find_one({"id": op_id}, {"_id": 0}) if op_id else None
        if not existing_op:
            # Narrow, genuine race window: the atomic claim on `duplicate`
            # (below, in the first-attempt branch) and the audit record's
            # insert are two SEPARATE writes -- no Mongo transaction covers
            # both (this codebase has none anywhere, see module docstring).
            # A concurrent caller can observe status=="MERGED" a few
            # milliseconds before the winning caller's own insert_one has
            # landed. Short retry before concluding genuine reconciliation
            # is needed, rather than failing a request that would have
            # succeeded a moment later.
            for _ in range(10):
                await asyncio.sleep(0.05)
                existing_op = await db.talent_merges.find_one({"id": op_id}, {"_id": 0}) if op_id else None
                if existing_op:
                    break
        if not existing_op:
            raise MergeError(500, "Talent is marked MERGED but its merge operation record is missing -- manual reconciliation required")
        if existing_op.get("status") == "completed":
            return _operation_summary(existing_op, resumed=False, already_done=True)
        if existing_op.get("status") == "in_progress":
            started = _parse_iso(existing_op.get("timestamp")) or datetime.now(timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - started).total_seconds()
            if age_seconds < _MERGE_STALE_IN_PROGRESS_SECONDS:
                raise MergeError(409, "A merge for this pair is already in progress -- try again shortly")
        # Stale in-progress or previously failed -- safe to resume, every
        # step in _apply_merge_steps is idempotent.
        logger.warning(
            "execute_merge: resuming operation %s (status=%s) canonical=%s duplicate=%s",
            existing_op["id"], existing_op.get("status"), canonical_id, duplicate_id,
        )
        try:
            await _apply_merge_steps(existing_op)
        except Exception as e:
            await db.talent_merges.update_one(
                {"id": existing_op["id"]}, {"$set": {"status": "failed", "error": str(e), "failed_at": _now()}}
            )
            raise MergeError(500, f"Merge resume failed: {e}") from e
        await db.talent_merges.update_one(
            {"id": existing_op["id"]}, {"$set": {"status": "completed", "completed_at": _now()}}
        )
        existing_op["status"] = "completed"
        return _operation_summary(existing_op, resumed=True, already_done=False)

    # First attempt -- atomically claim the duplicate. This is the sole
    # point of no return; everything before this is pure reads.
    op_id = str(uuid.uuid4())
    now = _now()
    claim = await db.talents.find_one_and_update(
        {"id": duplicate_id, "status": {"$ne": "MERGED"}},
        {"$set": {
            "status": "MERGED", "merged_into": canonical_id, "merged_at": now,
            "merge_operation_id": op_id, "updated_at": now,
        }},
    )
    if not claim:
        # Lost a race to a concurrent request for the exact same pair --
        # the other request already flipped duplicate.status; re-enter
        # through the "already MERGED" branch above to get a consistent
        # idempotent/in-progress response instead of racing further.
        return await execute_merge(canonical_id, duplicate_id, operator=operator)

    plan = await _build_plan(canonical, duplicate)
    op_doc = {
        "id": op_id,
        "source_talent_id": duplicate_id,
        "canonical_talent_id": canonical_id,
        "merge_reason": "manual_admin_merge",
        "matched_by": ["manual_admin_selection"],
        "field_changes": plan["field_changes"],
        "relationship_counts": plan["relationship_counts"],
        "media": plan["media"],
        "conflicts": plan["conflicts"],
        "tags_merged_count": plan["tags_merged_count"],
        "operator": operator,
        "status": "in_progress",
        "timestamp": now,
        "migration_version": "manual_merge_v1",
    }
    await db.talent_merges.insert_one(op_doc)
    logger.info(
        "execute_merge: claimed duplicate=%s -> canonical=%s operator=%s operation_id=%s",
        duplicate_id, canonical_id, operator, op_id,
    )

    try:
        await _apply_merge_steps(op_doc)
    except Exception as e:
        logger.error(
            "execute_merge: FAILED mid-merge -- operation_id=%s canonical=%s duplicate=%s error=%s. "
            "Duplicate is already claimed (status=MERGED); retrying this exact request will safely "
            "resume the remaining steps (every step is idempotent).",
            op_id, canonical_id, duplicate_id, e,
        )
        await db.talent_merges.update_one(
            {"id": op_id}, {"$set": {"status": "failed", "error": str(e), "failed_at": _now()}}
        )
        raise MergeError(500, f"Merge failed partway through and has been recorded for resume/reconciliation: {e}") from e

    await db.talent_merges.update_one({"id": op_id}, {"$set": {"status": "completed", "completed_at": _now()}})
    op_doc["status"] = "completed"
    logger.info("execute_merge: completed operation_id=%s canonical=%s duplicate=%s", op_id, canonical_id, duplicate_id)
    return _operation_summary(op_doc, resumed=False, already_done=False)
