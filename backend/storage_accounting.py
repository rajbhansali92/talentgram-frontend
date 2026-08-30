"""Corrected storage accounting model (Cloudinary rearchitecture, P7).

The old Storage Console derived "storage" almost entirely from
``submissions.media[].size`` + ``applications.media[].size`` and reported ~0.02 GB
while Cloudinary actually holds ~19.5 GB. Two bugs compounded:

  * ``talents.media[]`` was never counted for storage,
  * MongoDB's stored ``size`` fields (upload-time byte counts of *originals*, and
    only where populated) were presented AS "Cloudinary storage" — they are not.
    They exclude every derived asset, every orphan, and every legacy row with a
    null ``size``.

This module keeps the two sources strictly separate and always labelled:

  A. CLOUDINARY ACTUAL STORAGE  — authoritative: ``cloudinary.api.usage()``
  B. APPLICATION MEDIA REFERENCES — MongoDB ``media[].size`` sums, labelled
     "APPLICATION REFERENCE SIZE", never "Cloudinary storage"
  C. GLOBAL TALENT MEDIA         — from P3 ``media[i].ownership.owner_type``
  D. PROJECT AUDITION MEDIA      — from P3 ownership
  E. DERIVED ASSETS              — from ``usage().derived_resources`` (count; bytes
     are not broken out by Cloudinary's usage API)
  F. ORPHANED ASSETS             — only from the last full inventory scan (cached);
     never computed inline (12k+ object listing is too expensive per page load)
  G. UNKNOWN / UNRESOLVED        — P3 ``ownership.conflict`` set, or no ownership

Everything here is READ-ONLY. No Cloudinary write call, no ``destroy``, no
deletion, no cleanup. The module never triggers the full inventory scan — it only
reads whatever the last explicit ``GET /health`` rescan cached.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("talentgram")

# db.storage_metrics_cache rows are keyed by these:
CACHE_KEY_USAGE = "cloudinary_usage"
CACHE_KEY_FULL_SCAN = "full_inventory_scan"

CLOUDINARY_USAGE_TTL_SECONDS = 300       # 5 min — one cheap Admin API call
FULL_SCAN_TTL_SECONDS = 24 * 3600        # a full object listing is a manual rescan

MEDIA_COLLECTIONS = ("talents", "submissions", "applications")

OWNER_TALENT = "talent"
OWNER_PROJECT_SUBMISSION = "project_submission"

STATE_ACTIVE = "active"
STATE_PENDING_DELETION = "pending_deletion"
STATE_DELETED = "deleted"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(iso: Optional[str], now: Optional[datetime] = None) -> Optional[int]:
    if not iso:
        return None
    now = now or _now()
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return int((now - dt).total_seconds())
    except Exception:
        return None


# --------------------------------------------------------------------------
# A / E — Cloudinary authoritative usage (cached)
# --------------------------------------------------------------------------
async def get_cloudinary_usage(
    db,
    *,
    usage_fetcher,
    force_refresh: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return the parsed ``cloudinary.api.usage()`` payload, served from
    ``db.storage_metrics_cache`` when it is younger than the TTL. ``usage_fetcher``
    is a zero-arg callable returning the raw usage dict (injected so tests never
    hit Cloudinary). One Admin API call at most, and only on a cache miss.
    """
    now = now or _now()
    cached = None
    try:
        cached = await db.storage_metrics_cache.find_one({"key": CACHE_KEY_USAGE}, {"_id": 0})
    except Exception as e:
        logger.warning("storage_accounting: usage cache read failed: %s", e)

    age = _age_seconds((cached or {}).get("fetched_at"), now)
    if cached and not force_refresh and age is not None and age < CLOUDINARY_USAGE_TTL_SECONDS:
        return {**_shape_usage(cached.get("value") or {}),
                "fetched_at": cached.get("fetched_at"),
                "age_seconds": age, "stale": False, "from_cache": True}

    raw: Dict[str, Any] = {}
    err = None
    try:
        raw = usage_fetcher() or {}
    except Exception as e:  # pragma: no cover - fetcher is defensive
        err = str(e)
        logger.warning("storage_accounting: live usage fetch failed: %s", e)

    if not raw:
        # fall back to whatever stale cache we have rather than zeros
        if cached:
            return {**_shape_usage(cached.get("value") or {}),
                    "fetched_at": cached.get("fetched_at"),
                    "age_seconds": age, "stale": True, "from_cache": True, "error": err}
        return {**_shape_usage({}), "fetched_at": None, "age_seconds": None,
                "stale": True, "from_cache": False, "error": err or "no usage data"}

    fetched_at = now.isoformat()
    try:
        await db.storage_metrics_cache.update_one(
            {"key": CACHE_KEY_USAGE},
            {"$set": {"key": CACHE_KEY_USAGE, "value": raw, "fetched_at": fetched_at}},
            upsert=True,
        )
    except Exception as e:
        logger.warning("storage_accounting: usage cache write failed: %s", e)

    return {**_shape_usage(raw), "fetched_at": fetched_at, "age_seconds": 0,
            "stale": False, "from_cache": False}


def _num(v: Any) -> int:
    if isinstance(v, dict):
        v = v.get("usage")
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _shape_usage(raw: Dict[str, Any]) -> Dict[str, Any]:
    credits = raw.get("credits") or {}
    derived = raw.get("derived_resources")
    resources = raw.get("resources")
    return {
        "source": "cloudinary_usage_api",
        "as_of": raw.get("last_updated"),
        "plan": raw.get("plan"),
        "storage_bytes": _num(raw.get("storage")),
        "objects": _num(raw.get("objects")),
        "original_objects": (resources if isinstance(resources, int) else None),
        "derived_objects": (derived if isinstance(derived, int) else None),
        "bandwidth_bytes": _num(raw.get("bandwidth")),
        "transformations": _num(raw.get("transformations")),
        "requests": _num(raw.get("requests")),
        "credits": {
            "used": credits.get("usage"),
            "limit": credits.get("limit"),
            "used_percent": credits.get("used_percent"),
        },
    }


# --------------------------------------------------------------------------
# B / C / D / G — MongoDB ownership + reference aggregation (no Cloudinary calls)
# --------------------------------------------------------------------------
async def aggregate_ownership(db) -> Dict[str, Any]:
    """Single pass over talents/submissions/applications ``media[]``. Dedupes by
    ``public_id`` so a copy-by-value asset shared across owner docs is counted
    once. Classifies each distinct asset by its P3 ``ownership.owner_type`` and
    its ``lifecycle.state``. Pure MongoDB — no Cloudinary API call.
    """
    # public_id -> {owner_type, lifecycle_state, ref_bytes(min across copies),
    #               ref_count, collections:set}
    assets: Dict[str, Dict[str, Any]] = {}
    per_collection = {c: {"items": 0, "reference_bytes": 0} for c in MEDIA_COLLECTIONS}
    no_public_id = {"items": 0, "reference_bytes": 0}

    for cname in MEDIA_COLLECTIONS:
        coll = getattr(db, cname)
        pipeline = [
            {"$match": {"media": {"$exists": True, "$ne": []}}},
            {"$unwind": "$media"},
            {"$project": {
                "_id": 0,
                "public_id": "$media.public_id",
                "stream_uid": "$media.stream_uid",
                "size": {"$ifNull": ["$media.size", 0]},
                "owner_type": "$media.ownership.owner_type",
                "conflict": "$media.ownership.conflict",
                "lifecycle_state": "$media.lifecycle.state",
                "resource_type": "$media.resource_type",
            }},
        ]
        try:
            rows = await coll.aggregate(pipeline).to_list(length=500000)
        except Exception as e:
            logger.error("storage_accounting: ownership aggregate failed for %s: %s", cname, e)
            rows = []
        for r in rows:
            sz = int(r.get("size") or 0)
            per_collection[cname]["items"] += 1
            per_collection[cname]["reference_bytes"] += sz
            pid = r.get("public_id") or r.get("stream_uid")
            if not pid:
                no_public_id["items"] += 1
                no_public_id["reference_bytes"] += sz
                continue
            a = assets.get(pid)
            if a is None:
                assets[pid] = {
                    "owner_type": r.get("owner_type"),
                    "conflict": r.get("conflict"),
                    "lifecycle_state": r.get("lifecycle_state") or STATE_ACTIVE,
                    "ref_bytes": sz,
                    "ref_count": 1,
                }
            else:
                a["ref_count"] += 1
                # keep the largest non-zero size seen (best available estimate)
                if sz > a["ref_bytes"]:
                    a["ref_bytes"] = sz
                # a known owner_type wins over null; a conflict always wins
                if r.get("conflict"):
                    a["conflict"] = r["conflict"]
                if not a["owner_type"] and r.get("owner_type"):
                    a["owner_type"] = r["owner_type"]
                # pending/deleted lifecycle on any copy propagates
                st = r.get("lifecycle_state")
                if st in (STATE_PENDING_DELETION, STATE_DELETED):
                    a["lifecycle_state"] = st

    def bucket():
        return {"distinct_assets": 0, "reference_bytes": 0}

    ownership = {
        "global_talent_media": bucket(),
        "project_audition_media": bucket(),
        "unknown_or_conflicting": bucket(),
    }
    lifecycle = {
        "active": bucket(), "pending_deletion": bucket(),
        "deleted": bucket(), "protected": bucket(),
    }
    shared = 0
    for pid, a in assets.items():
        if a["ref_count"] > 1:
            shared += 1
        if a.get("conflict") or not a.get("owner_type"):
            key = "unknown_or_conflicting"
        elif a["owner_type"] == OWNER_TALENT:
            key = "global_talent_media"
        elif a["owner_type"] == OWNER_PROJECT_SUBMISSION:
            key = "project_audition_media"
        else:
            key = "unknown_or_conflicting"
        ownership[key]["distinct_assets"] += 1
        ownership[key]["reference_bytes"] += a["ref_bytes"]

        st = a["lifecycle_state"]
        lkey = st if st in lifecycle else "active"
        # "protected" = ownership unknown/conflicting AND still active
        if key == "unknown_or_conflicting" and st == STATE_ACTIVE:
            lkey = "protected"
        lifecycle[lkey]["distinct_assets"] += 1
        lifecycle[lkey]["reference_bytes"] += a["ref_bytes"]

    raw_ref_bytes = sum(v["reference_bytes"] for v in per_collection.values()) + no_public_id["reference_bytes"]
    # deduped: one size per distinct backing asset (shared copy-by-value counted once)
    deduped_ref_bytes = sum(a["ref_bytes"] for a in assets.values()) + no_public_id["reference_bytes"]
    sized = sum(1 for a in assets.values() if a["ref_bytes"] > 0)

    return {
        "application_references": {
            "source": "mongodb_media_size_fields",
            "label": "APPLICATION REFERENCE SIZE — NOT Cloudinary storage",
            "note": ("Sum of media[].size (upload-time byte counts of ORIGINALS, "
                     "only where populated). Excludes every derived asset, every "
                     "orphan, and legacy rows with a null size."),
            "per_collection": per_collection,
            "items_without_backing_asset": no_public_id,
            "reference_bytes": deduped_ref_bytes,
            "reference_bytes_raw_with_shared_copies": raw_ref_bytes,
            "distinct_backing_assets": len(assets),
            "distinct_backing_assets_with_known_size": sized,
            "shared_backing_assets": shared,
        },
        "ownership": ownership,
        "lifecycle": lifecycle,
    }


# --------------------------------------------------------------------------
# Lifecycle — pending-deletion ledger (assets whose owner doc was removed)
# --------------------------------------------------------------------------
async def aggregate_ledger(db, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or _now()
    out = {"total": 0, "eligible_for_cleanup": 0, "waiting_on_retention": 0,
           "indefinite": 0, "unknown_protected": 0}
    try:
        rows = await db.pending_media_deletions.find({}, {"_id": 0}).to_list(length=200000)
    except Exception as e:
        logger.warning("storage_accounting: ledger read failed: %s", e)
        rows = []
    for r in rows:
        out["total"] += 1
        if r.get("conflict") or not r.get("owner_type"):
            out["unknown_protected"] += 1
            continue
        eligible_at = r.get("eligible_at")
        if eligible_at is None:
            out["indefinite"] += 1
            continue
        age = _age_seconds(eligible_at, now)
        if age is not None and age >= 0:
            out["eligible_for_cleanup"] += 1
        else:
            out["waiting_on_retention"] += 1
    return out


# --------------------------------------------------------------------------
# F — orphans (from the last cached full scan only)
# --------------------------------------------------------------------------
async def get_cached_scan(db, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or _now()
    try:
        row = await db.storage_metrics_cache.find_one({"key": CACHE_KEY_FULL_SCAN}, {"_id": 0})
    except Exception as e:
        logger.warning("storage_accounting: scan cache read failed: %s", e)
        row = None
    if not row:
        return {"available": False, "status": "never_run", "last_scan_at": None,
                "age_seconds": None}
    age = _age_seconds(row.get("fetched_at"), now)
    status = "fresh" if (age is not None and age < FULL_SCAN_TTL_SECONDS) else "stale"
    v = row.get("value") or {}
    return {
        "available": True,
        "status": status,
        "last_scan_at": row.get("fetched_at"),
        "age_seconds": age,
        "cloudinary_unreferenced_objects": v.get("orphaned_count"),
        "cloudinary_unreferenced_bytes": v.get("orphaned_bytes"),
        "broken_references": v.get("broken_count"),
    }


async def save_scan_result(db, result: Dict[str, Any], *, now: Optional[datetime] = None) -> None:
    now = now or _now()
    try:
        await db.storage_metrics_cache.update_one(
            {"key": CACHE_KEY_FULL_SCAN},
            {"$set": {"key": CACHE_KEY_FULL_SCAN, "value": result, "fetched_at": now.isoformat()}},
            upsert=True,
        )
    except Exception as e:
        logger.warning("storage_accounting: scan cache write failed: %s", e)


# --------------------------------------------------------------------------
# Assembler
# --------------------------------------------------------------------------
async def build_accounting(
    db,
    *,
    usage_fetcher,
    force_usage_refresh: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or _now()
    cloudinary = await get_cloudinary_usage(
        db, usage_fetcher=usage_fetcher, force_refresh=force_usage_refresh, now=now)
    mongo = await aggregate_ownership(db)
    ledger = await aggregate_ledger(db, now=now)
    scan = await get_cached_scan(db, now=now)

    cld_bytes = cloudinary.get("storage_bytes") or 0
    app_ref_bytes = mongo["application_references"]["reference_bytes"]

    return {
        "generated_at": now.isoformat(),
        # A + E
        "cloudinary": cloudinary,
        # B
        "application_references": mongo["application_references"],
        # C / D / G
        "ownership": mongo["ownership"],
        # lifecycle (media[] states + ledger)
        "lifecycle": {**mongo["lifecycle"], "ledger": ledger},
        # F
        "orphan_scan": scan,
        # honest reconciliation between the two sources
        "reconciliation": {
            "cloudinary_actual_storage_bytes": cld_bytes,
            "application_reference_bytes": app_ref_bytes,
            "unaccounted_bytes": max(0, cld_bytes - app_ref_bytes),
            "explanation": ("Cloudinary actual storage minus MongoDB reference "
                            "size. The gap is derived assets + orphaned originals "
                            "+ legacy references with a null size. A precise "
                            "per-object split needs a full inventory scan "
                            "(GET /health, cached)."),
        },
        "freshness": {
            "cloudinary_usage": {
                "fetched_at": cloudinary.get("fetched_at"),
                "age_seconds": cloudinary.get("age_seconds"),
                "ttl_seconds": CLOUDINARY_USAGE_TTL_SECONDS,
                "stale": cloudinary.get("stale"),
            },
            "full_inventory_scan": {
                "last_scan_at": scan.get("last_scan_at"),
                "age_seconds": scan.get("age_seconds"),
                "ttl_seconds": FULL_SCAN_TTL_SECONDS,
                "status": scan.get("status"),
            },
            "mongodb_aggregation": {"computed_at": now.isoformat(), "live": True},
        },
        "notes": [
            "Cloudinary figures are authoritative for bytes/objects/derived/bandwidth/credits.",
            "MongoDB figures are APPLICATION REFERENCE SIZE — not Cloudinary storage.",
            "Ownership uses the P3 media[i].ownership sub-document, never the folder path.",
            "This endpoint is read-only and cannot trigger deletion or a full scan.",
        ],
    }
