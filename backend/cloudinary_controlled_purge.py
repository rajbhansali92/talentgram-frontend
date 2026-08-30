"""P9 — CONTROLLED, AUDITED, PER-ASSET Cloudinary derived-asset deletion.

Three layers, deliberately separated so Layers 1–2 are fully testable without any
Cloudinary write:

  LAYER 1  revalidate_candidate()  — fresh per-asset validation against LIVE
           Cloudinary + MongoDB state; any mismatch vs the approved manifest ->
           blocked. Pure logic + injected read-only fetchers.
  LAYER 2  build_purge_manifest() / approve_manifest() / create_batch()
           — immutable manifests, manifest-specific approval (no "approve all"),
           batch-size caps (canary = 10, then <= 50).
  LAYER 3  execute_batch()  — physical deletion. Gated behind ALL of:
             * env flag MEDIA_LIFECYCLE_PHYSICAL_DELETE on
             * a matching approval whose candidate hash still matches
             * dry_run is False
             * every asset re-passes Layer 1 immediately before its delete
           Uses the NARROWEST Cloudinary mechanism — delete_derived_resources
           by exact derived id, one asset per call. NEVER a prefix / folder
           delete. Any anomaly -> raise PurgeAnomaly, the batch stops, nothing
           else is deleted.

It makes it structurally impossible to turn "DELETE_CANDIDATE" into "delete
everything": no path deletes without an immutable, hash-pinned, admin approval
of a specific asset set, and every asset is re-proven safe at delete time.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("talentgram")

# --------------------------------------------------------------------------
CANARY_BATCH_SIZE = 10
MAX_BATCH_SIZE = 50

MANIFESTS_COLL = "purge_manifests"
APPROVALS_COLL = "purge_approvals"
BATCHES_COLL = "purge_batches"
AUDIT_COLL = "purge_audit_log"

# Layer-1 verdicts
PASS = "PASS"
PROTECTED = "PROTECTED"
STALE_MANIFEST = "STALE_MANIFEST"
RETENTION_BLOCKED = "RETENTION_BLOCKED"
REFERENCE_BLOCKED = "REFERENCE_BLOCKED"
OWNERSHIP_BLOCKED = "OWNERSHIP_BLOCKED"
PARENT_BLOCKED = "PARENT_BLOCKED"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
NOT_FOUND = "NOT_FOUND"            # derived asset already gone — nothing to do
UNKNOWN = "UNKNOWN"

_BLOCKING = {PROTECTED, STALE_MANIFEST, RETENTION_BLOCKED, REFERENCE_BLOCKED,
             OWNERSHIP_BLOCKED, PARENT_BLOCKED, IDENTITY_MISMATCH, UNKNOWN}

# classifications from P8.5 that P9 may EVER consider (canary uses a stricter subset)
_ELIGIBLE_CLASSIFICATIONS = {"DELETE_CANDIDATE"}
# hard-excluded regardless of anything else
_NEVER = {"PROTECTED_HISTORICAL_DERIVED", "ACTIVE_DERIVED", "UNKNOWN_DERIVED",
          "LEGACY_DERIVED", "PROTECTED_UNKNOWN", "PROTECTED_CONFLICT",
          "PROTECTED_SHARED", "ACTIVE_GLOBAL_TALENT_MEDIA",
          "ACTIVE_PROJECT_AUDITION_MEDIA", "PENDING_RETENTION", "STALE_METADATA_ONLY"}

# canary must come from this transformation family only (retired, regenerable,
# render-time-only, never persisted): full-res / sized AVIF
_CANARY_FAMILIES = ("f_avif,q_auto", "c_fill,dpr_", "f_avif")
_CANARY_FORBID_TOKENS = ("f_mp4", "fl_attachment", "vc_auto", "fl_sprite")


class PurgeAnomaly(RuntimeError):
    """Raised the instant anything unexpected happens during a batch. The batch
    stops; no further asset is touched."""


def _physical_delete_enabled() -> bool:
    return os.environ.get("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "false").strip().lower() in (
        "1", "true", "yes", "on")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat()


def candidate_hash(candidate_ids: List[str]) -> str:
    """Order-independent hash pinning an exact candidate set to an approval."""
    return hashlib.sha256(
        json.dumps(sorted(str(x) for x in candidate_ids)).encode()
    ).hexdigest()


# ==========================================================================
# LAYER 1 — per-asset revalidation
# ==========================================================================
@dataclass
class RevalidationResult:
    status: str
    reason: str
    candidate_id: Optional[str] = None
    public_id: Optional[str] = None
    derived_id: Optional[str] = None
    checks: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_pass(self) -> bool:
        return self.status == PASS

    def explain(self) -> Dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "candidate_id": self.candidate_id,
                "public_id": self.public_id, "derived_id": self.derived_id, "checks": self.checks}


async def revalidate_candidate(
    db,
    candidate: Dict[str, Any],
    *,
    resource_fetcher: Callable[[str, str], Dict[str, Any]],
    reference_index: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> RevalidationResult:
    """Re-prove, against LIVE state, that this one derived asset is safe to
    delete. `candidate` is a row from the P8.5 inventory / a purge manifest
    (needs: derived_id, public_id (parent), transformation, transformation_family,
    format, bytes, classification, parent_public_id). `resource_fetcher(pid, rt)`
    is a read-only `cloudinary.api.resource(pid, resource_type=rt)` wrapper.

    ANY discrepancy vs the manifest, ANY reference, ANY unproven fact -> blocked.
    """
    now = now or _now()
    cid = candidate.get("candidate_id") or candidate.get("derived_id")
    parent_pid = candidate.get("parent_public_id") or candidate.get("public_id")
    derived_id = candidate.get("derived_id") or candidate.get("id")
    xf = candidate.get("transformation")
    rt = candidate.get("resource_type") or "image"
    cls = candidate.get("classification")
    checks: Dict[str, Any] = {}

    def _r(status, reason):
        return RevalidationResult(status, reason, candidate_id=cid, public_id=parent_pid,
                                  derived_id=derived_id, checks=checks)

    # ---- classification gate (defence in depth) --------------------------
    if cls in _NEVER or cls not in _ELIGIBLE_CLASSIFICATIONS:
        return _r(PROTECTED, f"classification {cls!r} is never P9-eligible")
    if not derived_id or not parent_pid or not xf:
        return _r(IDENTITY_MISMATCH, "candidate is missing derived_id / parent / transformation")

    # ---- ownership (P3 authoritative; folder NEVER) ----------------------
    ridx = reference_index or await _build_reference_index(db)
    owners = ridx["owners"].get(parent_pid) or []
    if not owners and "/" in parent_pid:
        owners = ridx["owners"].get(parent_pid.rsplit("/", 1)[-1]) or []
    checks["parent_owner_docs"] = len(owners)
    if not owners:
        return _r(PARENT_BLOCKED, "parent original has no MongoDB media reference — cannot prove it is safe")
    conflict = next((o["conflict"] for o in owners if o.get("conflict")), None)
    owner_type = next((o["owner_type"] for o in owners if o.get("owner_type")), None)
    checks["owner_type"] = owner_type
    checks["ownership_conflict"] = conflict
    if conflict:
        return _r(OWNERSHIP_BLOCKED, f"parent ownership conflict: {conflict}")
    if not owner_type:
        return _r(OWNERSHIP_BLOCKED, "parent owner_type unknown")
    # manifest agreement on ownership
    if candidate.get("owner_type") and candidate["owner_type"] != owner_type:
        return _r(STALE_MANIFEST, f"ownership changed since manifest ({candidate['owner_type']} -> {owner_type})")

    # ---- parent still an active reference (not soft-deleted) -------------
    live_owner = [o for o in owners if not o.get("doc_soft_deleted")]
    checks["parent_live_refs"] = len(live_owner)
    if not live_owner:
        return _r(PARENT_BLOCKED, "parent original's only references are soft-deleted — resolve with the parent, not here")

    # ---- retention (audition media) -------------------------------------
    if owner_type == "project_submission":
        from media_lifecycle import get_retention_days
        rdays = await get_retention_days(db)
        checks["retention_days"] = rdays
        # a live audition parent -> the parent is in use -> derivative not deletable
        return _r(RETENTION_BLOCKED, "parent is live project audition media — not eligible")

    # ---- repo-wide reference / persisted-URL check ----------------------
    persisted = _persisted_check(candidate, ridx)
    checks["persisted_url_hit"] = persisted
    if persisted:
        return _r(REFERENCE_BLOCKED, "a URL for this exact derivative is persisted in a media field")
    # copy-by-value lineage on the parent
    if parent_pid in ridx.get("source_lineage", set()) or derived_id in ridx.get("source_lineage", set()):
        return _r(REFERENCE_BLOCKED, "parent / derivative appears in copy-by-value lineage")
    # ledger
    if parent_pid in ridx.get("ledger_keys", set()):
        return _r(REFERENCE_BLOCKED, "parent is in the pending_media_deletions ledger")

    # ---- LIVE Cloudinary identity -------------------------------------
    try:
        res = resource_fetcher(parent_pid, rt)
    except _NotFound:
        return _r(PARENT_BLOCKED, "parent original no longer exists on Cloudinary")
    except Exception as e:  # ambiguous — do not delete
        return _r(UNKNOWN, f"could not fetch parent from Cloudinary: {e}")
    if not res or not res.get("public_id"):
        return _r(PARENT_BLOCKED, "Cloudinary returned no parent resource")
    checks["cloudinary_parent_public_id"] = res.get("public_id")
    derived_list = res.get("derived") or []
    match = next((d for d in derived_list if d.get("id") == derived_id), None)
    if match is None:
        # already gone — nothing to delete (idempotent success, not a failure)
        return _r(NOT_FOUND, "derived asset already absent on Cloudinary")
    checks["cloudinary_derived"] = {"id": match.get("id"), "transformation": match.get("transformation"),
                                    "format": match.get("format"), "bytes": match.get("bytes")}
    # identity comparison vs manifest
    m_xf = _norm_xf(xf)
    c_xf = _norm_xf(match.get("transformation"))
    if m_xf and c_xf and m_xf != c_xf:
        return _r(IDENTITY_MISMATCH, f"transformation differs (manifest {m_xf!r} vs live {c_xf!r})")
    if candidate.get("format") and match.get("format") and candidate["format"] != match.get("format"):
        return _r(IDENTITY_MISMATCH, "format differs from manifest")
    mb, cb = candidate.get("bytes"), match.get("bytes")
    if mb and cb and abs(int(mb) - int(cb)) > max(1024, int(mb) * 0.05):
        return _r(STALE_MANIFEST, f"byte size changed since manifest ({mb} -> {cb})")

    return _r(PASS, "retired regenerable derivative; parent original active & referenced; no persisted "
                    "URL; ownership proven (talent); identity matches manifest — safe subject to "
                    "immediate pre-delete recheck")


def _norm_xf(x: Optional[str]) -> Optional[str]:
    if not x:
        return None
    return ",".join(sorted(x.replace("/", ",").split(","))).strip(",").lower()


def _persisted_check(candidate: Dict[str, Any], ridx: Dict[str, Any]) -> bool:
    url = candidate.get("url") or ""
    if url:
        norm = url.replace("http://", "https://")
        import re as _re
        norm = _re.sub(r"/v\d+/", "/v/", norm)
        if norm in ridx.get("norm_url_index", set()):
            return True
    parent = candidate.get("parent_public_id") or candidate.get("public_id")
    fam = candidate.get("transformation_family")
    if parent and fam and (_norm_pid(parent), fam) in ridx.get("persisted_parent_family", set()):
        return True
    return False


def _norm_pid(p: Optional[str]) -> Optional[str]:
    if not p:
        return p
    import re as _re
    p = _re.sub(r"^(v\d+/)+", "", p)
    i = p.find("talentgram/")
    return p[i:] if i >= 0 else p


class _NotFound(Exception):
    pass


async def _build_reference_index(db) -> Dict[str, Any]:
    """Thin wrapper over the P8 engine's reference index + a few P9 extras."""
    import cloudinary_cleanup_manifest as ccm
    import re as _re
    ridx = await ccm.build_reference_index(db)
    ridx["norm_url_index"] = {
        _re.sub(r"/v\d+/", "/v/", u.replace("http://", "https://"))
        for u in ridx["url_index"] if isinstance(u, str)
    }
    pf = set()
    for u in ridx["url_index"]:
        if not isinstance(u, str) or "/upload/" not in u:
            continue
        pp, xf, _ = _parse_full(u)
        if xf:
            pf.add((_norm_pid(pp), _fam(xf)))
    ridx["persisted_parent_family"] = pf
    try:
        led = await db.pending_media_deletions.find({}, {"_id": 0, "public_id": 1, "stream_uid": 1}).to_list(100000)
        ridx["ledger_keys"] = {r.get("public_id") or r.get("stream_uid") for r in led if (r.get("public_id") or r.get("stream_uid"))}
    except Exception:
        ridx["ledger_keys"] = set()
    return ridx


def _parse_full(url: str):
    import re as _re
    SEG = _re.compile(r"(^|,)(w_|h_|c_|q_|f_|dpr_|e_|so_|vc_|b_|ac_|g_|fl_|sp_|br_|pg_|a_|o_|r_|x_|y_|l_|u_|t_)")
    VER = _re.compile(r"^v\d+$")
    if "/upload/" not in url:
        return (None, None, None)
    tail = url.split("/upload/", 1)[1].split("?")[0].split("#")[0]
    segs = tail.split("/")
    xf = []
    while segs and (VER.match(segs[0]) or SEG.search(segs[0])):
        if SEG.search(segs[0]) and not VER.match(segs[0]):
            xf.append(segs[0])
        segs.pop(0)
    return ("/".join(segs).rsplit(".", 1)[0] or None, "/".join(xf) if xf else None, None)


def _fam(xf: Optional[str]) -> str:
    import re as _re
    if xf is None:
        return "(none)"
    if xf.startswith("fl_attachment"):
        return "fl_attachment:* (download)"
    return _re.sub(r"\d+", "N", xf)


# ==========================================================================
# LAYER 2 — manifest / approval / batch
# ==========================================================================
async def build_purge_manifest(
    db,
    source_rows: List[Dict[str, Any]],
    *,
    source_manifest_id: str,
    resource_fetcher: Callable[[str, str], Dict[str, Any]],
    actor: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Layer 2 — run Layer 1 across every candidate and freeze the result into an
    immutable manifest. NO deletion. This IS a Mongo write (the manifest doc) —
    it is a new analysis artifact, never a mutation of media."""
    now = now or _now()
    ridx = await _build_reference_index(db)
    rows: List[Dict[str, Any]] = []
    tally: Dict[str, int] = {}
    passed_bytes = 0
    for c in source_rows:
        rv = await revalidate_candidate(db, c, resource_fetcher=resource_fetcher,
                                        reference_index=ridx, now=now)
        tally[rv.status] = tally.get(rv.status, 0) + 1
        row = {**c, "revalidation": rv.explain()}
        if rv.status == PASS:
            passed_bytes += int(c.get("bytes") or 0)
        rows.append(row)
    passed = [r for r in rows if r["revalidation"]["status"] == PASS]
    manifest_id = "pm_" + hashlib.sha256(
        json.dumps({"src": source_manifest_id, "ts": _iso(now),
                    "ids": sorted(str(r.get("derived_id")) for r in passed)},
                   sort_keys=True).encode()).hexdigest()[:20]
    doc = {
        "manifest_id": manifest_id,
        "source_manifest_id": source_manifest_id,
        "generated_at": _iso(now),
        "generated_by": actor,
        "dry_run": True,
        "physical_delete_enabled_at_generation": _physical_delete_enabled(),
        "summary": {
            "candidates_examined": len(rows),
            "passed_revalidation": len(passed),
            "passed_bytes": passed_bytes,
            "by_verdict": tally,
        },
        "passed_candidate_ids": sorted(str(r.get("derived_id")) for r in passed),
        "passed_candidate_hash": candidate_hash([r.get("derived_id") for r in passed]),
        "rows": rows,
        "canary_preview": select_canary([r for r in rows], CANARY_BATCH_SIZE),
    }
    await db[MANIFESTS_COLL].insert_one({**doc})
    doc.pop("_id", None)
    return doc


async def approve_manifest(
    db,
    manifest_id: str,
    *,
    approved_by: str,
    candidate_ids: List[str],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """An explicit, manifest-specific, hash-pinned approval. There is NO
    'approve all future candidates' — an approval names an exact immutable set
    tied to one manifest."""
    now = now or _now()
    manifest = await db[MANIFESTS_COLL].find_one({"manifest_id": manifest_id}, {"_id": 0})
    if not manifest:
        raise ValueError(f"manifest {manifest_id} not found")
    want = set(str(x) for x in candidate_ids)
    passed = set(manifest["passed_candidate_ids"])
    if not want:
        raise ValueError("approval must name at least one candidate id")
    if not want.issubset(passed):
        raise ValueError("approval names candidate(s) that did not pass revalidation in this manifest")
    approval_id = "ap_" + hashlib.sha256(
        f"{manifest_id}|{approved_by}|{_iso(now)}|{candidate_hash(list(want))}".encode()
    ).hexdigest()[:20]
    doc = {
        "approval_id": approval_id,
        "manifest_id": manifest_id,
        "source_manifest_id": manifest.get("source_manifest_id"),
        "approved_by": approved_by,
        "approved_at": _iso(now),
        "candidate_count": len(want),
        "candidate_bytes": sum(int(r.get("bytes") or 0) for r in manifest["rows"]
                               if str(r.get("derived_id")) in want),
        "candidate_ids": sorted(want),
        "candidate_hash": candidate_hash(list(want)),
        "consumed_batch_ids": [],
    }
    await db[APPROVALS_COLL].insert_one({**doc})
    doc.pop("_id", None)
    return doc


async def create_batch(
    db,
    approval_id: str,
    *,
    size: int = CANARY_BATCH_SIZE,
    canary: bool = True,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Carve a size-capped batch of not-yet-processed candidates from an
    approval. Canary batch is exactly CANARY_BATCH_SIZE; later batches are
    capped at MAX_BATCH_SIZE."""
    now = now or _now()
    approval = await db[APPROVALS_COLL].find_one({"approval_id": approval_id}, {"_id": 0})
    if not approval:
        raise ValueError(f"approval {approval_id} not found")
    manifest = await db[MANIFESTS_COLL].find_one({"manifest_id": approval["manifest_id"]}, {"_id": 0})
    # approval must still match its manifest exactly
    if candidate_hash(approval["candidate_ids"]) != approval["candidate_hash"]:
        raise ValueError("approval candidate hash mismatch — corrupted approval")
    cap = CANARY_BATCH_SIZE if canary else MAX_BATCH_SIZE
    if size > cap:
        raise ValueError(f"batch size {size} exceeds cap {cap}")
    if canary and size != CANARY_BATCH_SIZE:
        raise ValueError(f"canary batch must be exactly {CANARY_BATCH_SIZE}")

    done = set()
    async for b in db[BATCHES_COLL].find({"approval_id": approval_id}, {"_id": 0, "candidate_ids": 1}):
        done.update(b.get("candidate_ids") or [])
    remaining = [c for c in approval["candidate_ids"] if c not in done]
    if not remaining:
        raise ValueError("approval fully consumed — no candidates remain")

    rows_by_id = {str(r.get("derived_id")): r for r in manifest["rows"]}
    chosen = _select_batch_ids(remaining, rows_by_id, size, canary)
    batch_id = "b_" + hashlib.sha256(f"{approval_id}|{_iso(now)}|{','.join(chosen)}".encode()).hexdigest()[:20]
    doc = {
        "batch_id": batch_id,
        "approval_id": approval_id,
        "manifest_id": approval["manifest_id"],
        "created_at": _iso(now),
        "canary": canary,
        "size": len(chosen),
        "candidate_ids": chosen,
        "candidates": [rows_by_id[c] for c in chosen],
        "status": "created",
        "prior_batches": len(done) // max(1, size),
    }
    await db[BATCHES_COLL].insert_one({**doc})
    doc.pop("_id", None)
    return doc


def select_canary(rows: List[Dict[str, Any]], n: int = CANARY_BATCH_SIZE) -> List[Dict[str, Any]]:
    """The safest possible n candidates: retired sized/full-res AVIF derivatives,
    revalidation PASS, active+referenced parent, no persisted URL, ownership
    known. Explicitly excludes f_mp4 / fl_attachment / vc_auto / sprite / any
    non-DELETE_CANDIDATE / any orphan-parent."""
    out = []
    for r in rows:
        if len(out) >= n:
            break
        rv = (r.get("revalidation") or {}).get("status")
        if rv is not None and rv != PASS:
            continue
        if r.get("classification") != "DELETE_CANDIDATE":
            continue
        fam = (r.get("transformation_family") or "")
        xf = (r.get("transformation") or "").lower()
        if any(tok in xf for tok in _CANARY_FORBID_TOKENS):
            continue
        if "avif" not in xf:
            continue
        if not r.get("parent_referenced", True):
            continue
        out.append({k: r.get(k) for k in (
            "candidate_id", "derived_id", "public_id", "parent_public_id", "transformation",
            "transformation_family", "resource_type", "format", "bytes", "url", "classification")})
    return out


def _select_batch_ids(remaining_ids, rows_by_id, size, canary):
    if canary:
        canary_rows = select_canary([rows_by_id[c] for c in remaining_ids if c in rows_by_id],
                                    CANARY_BATCH_SIZE)
        ids = [str(r["derived_id"]) for r in canary_rows]
        if len(ids) < CANARY_BATCH_SIZE:
            raise ValueError(f"cannot form a full {CANARY_BATCH_SIZE}-asset canary from the "
                             f"safest AVIF family — only {len(ids)} qualify")
        return ids[:CANARY_BATCH_SIZE]
    return remaining_ids[:size]


# ==========================================================================
# LAYER 3 — physical deletion (gated, per-asset, exact, audited, anomaly-stop)
# ==========================================================================
@dataclass
class BatchResult:
    batch_id: str
    dry_run: bool
    attempted: int = 0
    deleted: int = 0
    skipped: int = 0
    blocked: int = 0
    stopped: bool = False
    stop_reason: Optional[str] = None
    per_asset: List[Dict[str, Any]] = field(default_factory=list)

    def explain(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in
                ("batch_id", "dry_run", "attempted", "deleted", "skipped", "blocked",
                 "stopped", "stop_reason", "per_asset")}


async def execute_batch(
    db,
    batch_id: str,
    *,
    actor: str,
    dry_run: bool = True,
    resource_fetcher: Callable[[str, str], Dict[str, Any]],
    derived_deleter: Optional[Callable[[List[str]], Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> BatchResult:
    """Layer 3. For each asset: re-run Layer 1 LIVE; if not PASS -> skip + audit;
    if PASS -> (dry_run) audit 'would_delete' OR (real) delete THIS ONE derived
    id via `derived_deleter([id])`, then verify it is gone and the parent
    survives. Any anomaly -> raise PurgeAnomaly (batch stops). Canary stops
    after 10 regardless.

    Real deletion requires ALL of: not dry_run, MEDIA_LIFECYCLE_PHYSICAL_DELETE
    on, a valid unconsumed approval, the manifest's candidate hash unchanged.
    """
    now = now or _now()
    batch = await db[BATCHES_COLL].find_one({"batch_id": batch_id}, {"_id": 0})
    if not batch:
        raise ValueError(f"batch {batch_id} not found")
    approval = await db[APPROVALS_COLL].find_one({"approval_id": batch["approval_id"]}, {"_id": 0})
    manifest = await db[MANIFESTS_COLL].find_one({"manifest_id": batch["manifest_id"]}, {"_id": 0})
    if not approval or not manifest:
        raise ValueError("batch's approval/manifest missing")

    real = (not dry_run)
    if real:
        if not _physical_delete_enabled():
            raise PermissionError("physical deletion requires MEDIA_LIFECYCLE_PHYSICAL_DELETE=on")
        if batch.get("status") in ("executed", "executing"):
            raise ValueError("batch already executed")
        if candidate_hash(approval["candidate_ids"]) != approval["candidate_hash"]:
            raise PurgeAnomaly("approval candidate hash changed since approval")
        if set(batch["candidate_ids"]) - set(approval["candidate_ids"]):
            raise PurgeAnomaly("batch contains ids not in the approval")
        if manifest.get("passed_candidate_hash") and not set(batch["candidate_ids"]).issubset(
                set(manifest["passed_candidate_ids"])):
            raise PurgeAnomaly("batch id no longer in the manifest's passed set")
        await db[BATCHES_COLL].update_one({"batch_id": batch_id}, {"$set": {"status": "executing"}})

    ridx = await _build_reference_index(db)
    res = BatchResult(batch_id=batch_id, dry_run=dry_run)

    for cand in batch["candidates"]:
        if batch.get("canary") and res.deleted >= CANARY_BATCH_SIZE:
            break
        res.attempted += 1
        rv = await revalidate_candidate(db, cand, resource_fetcher=resource_fetcher,
                                        reference_index=ridx, now=now)
        rec = _audit_record(batch, cand, actor, rv, now, dry_run)

        if rv.status == NOT_FOUND:
            rec["deletion_result"] = "already_absent"
            res.skipped += 1
            await _audit(db, rec)
            continue
        if not rv.is_pass:
            rec["deletion_result"] = "blocked"
            res.blocked += 1
            await _audit(db, rec)
            # a blocked asset that the manifest said PASS => manifest went stale => STOP
            if (cand.get("revalidation") or {}).get("status") == PASS or \
               (manifest and str(cand.get("derived_id")) in manifest.get("passed_candidate_ids", [])):
                res.stopped = True
                res.stop_reason = f"asset {cand.get('derived_id')} was PASS in the manifest but is now {rv.status}: {rv.reason}"
                if real:
                    await db[BATCHES_COLL].update_one({"batch_id": batch_id},
                                                      {"$set": {"status": "stopped", "stop_reason": res.stop_reason}})
                return res
            continue

        if dry_run:
            rec["deletion_result"] = "would_delete"
            res.per_asset.append({"derived_id": cand.get("derived_id"), "result": "would_delete"})
            # dry-run performs NO writes at all (not even the audit log)
            continue

        # ---- real deletion — this ONE derived id only --------------------
        try:
            resp = derived_deleter([rv.derived_id])
        except Exception as e:
            rec["deletion_result"] = "error"
            rec["error"] = str(e)[:400]
            await _audit(db, rec)
            res.stopped = True
            res.stop_reason = f"deleter raised for {rv.derived_id}: {e}"
            await db[BATCHES_COLL].update_one({"batch_id": batch_id},
                                              {"$set": {"status": "stopped", "stop_reason": res.stop_reason}})
            return res
        rec["cloudinary_response_summary"] = _summarise_response(resp)
        ok = _response_ok(resp, rv.derived_id)
        if ok == "ambiguous":
            rec["deletion_result"] = "DELETION_STATUS_UNKNOWN"
            await _audit(db, rec)
            res.stopped = True
            res.stop_reason = f"ambiguous Cloudinary response for {rv.derived_id} — stopping, NOT retrying"
            await db[BATCHES_COLL].update_one({"batch_id": batch_id},
                                              {"$set": {"status": "stopped", "stop_reason": res.stop_reason}})
            return res

        # ---- post-delete verification ---------------------------------
        try:
            after = resource_fetcher(rv.public_id, cand.get("resource_type") or "image")
        except _NotFound:
            # the parent original vanished right after we deleted a derivative — a
            # serious anomaly. Log, then raise so the whole run halts hard.
            rec["deletion_result"] = "PARENT_DISAPPEARED"
            await _audit(db, rec)
            await db[BATCHES_COLL].update_one({"batch_id": batch_id},
                                              {"$set": {"status": "stopped",
                                                        "stop_reason": "parent original disappeared after a derived delete"}})
            raise PurgeAnomaly(f"parent original {rv.public_id} disappeared after deleting a derivative")
        except Exception as e:
            rec["deletion_result"] = "deleted_unverified"
            rec["error"] = f"post-delete parent fetch failed: {e}"
            await _audit(db, rec)
            res.stopped = True
            res.stop_reason = "could not verify parent survival after delete"
            await db[BATCHES_COLL].update_one({"batch_id": batch_id},
                                              {"$set": {"status": "stopped", "stop_reason": res.stop_reason}})
            return res
        still_there = [d for d in (after.get("derived") or []) if d.get("id") == rv.derived_id]
        parent_alive = bool(after.get("public_id"))
        if still_there:
            rec["deletion_result"] = "delete_not_effective"
            await _audit(db, rec)
            res.stopped = True
            res.stop_reason = f"derived {rv.derived_id} still present after delete"
            await db[BATCHES_COLL].update_one({"batch_id": batch_id},
                                              {"$set": {"status": "stopped", "stop_reason": res.stop_reason}})
            return res
        if not parent_alive:
            rec["deletion_result"] = "PARENT_DISAPPEARED"
            await _audit(db, rec)
            raise PurgeAnomaly(f"parent original {rv.public_id} disappeared after deleting a derivative")

        rec["deletion_result"] = "deleted"
        res.deleted += 1
        res.per_asset.append({"derived_id": rv.derived_id, "result": "deleted"})
        await _audit(db, rec)

    if real:
        await db[BATCHES_COLL].update_one(
            {"batch_id": batch_id},
            {"$set": {"status": "executed", "executed_at": _iso(now),
                      "deleted": res.deleted, "blocked": res.blocked, "skipped": res.skipped}})
        await db[APPROVALS_COLL].update_one({"approval_id": batch["approval_id"]},
                                            {"$addToSet": {"consumed_batch_ids": batch_id}})
    # canary NEVER auto-continues — caller must review and create the next batch
    return res


# --------------------------------------------------------------------------
def _audit_record(batch, cand, actor, rv: RevalidationResult, now, dry_run) -> Dict[str, Any]:
    return {
        "timestamp": _iso(now),
        "actor": actor,
        "dry_run": dry_run,
        "manifest_id": batch.get("manifest_id"),
        "approval_id": batch.get("approval_id"),
        "batch_id": batch.get("batch_id"),
        "canary": batch.get("canary"),
        "public_id": cand.get("parent_public_id") or cand.get("public_id"),
        "parent_public_id": cand.get("parent_public_id") or cand.get("public_id"),
        "derived_id": cand.get("derived_id"),
        "resource_type": cand.get("resource_type"),
        "format": cand.get("format"),
        "bytes": cand.get("bytes"),
        "transformation": cand.get("transformation"),
        "classification": cand.get("classification"),
        "revalidation_result": rv.explain(),
        "reference_count": rv.checks.get("parent_owner_docs"),
        "ownership": rv.checks.get("owner_type"),
        "retention": rv.checks.get("retention_days"),
        "deletion_result": None,
        "cloudinary_response_summary": None,
        "error": None,
    }


async def _audit(db, record: Dict[str, Any]) -> None:
    """Insert-only immutable audit record. Never contains secrets."""
    if record.get("dry_run"):
        return  # dry-run writes NOTHING
    try:
        await db[AUDIT_COLL].insert_one({**record})
    except Exception as e:  # pragma: no cover
        logger.error("purge audit write failed: %s", e)


def _summarise_response(resp: Any) -> Dict[str, Any]:
    if not isinstance(resp, dict):
        return {"raw": str(resp)[:300]}
    return {k: resp.get(k) for k in ("deleted", "deleted_counts", "partial", "rate_limit_remaining")
            if k in resp}


def _response_ok(resp: Any, derived_id: str) -> str:
    """'ok' | 'ambiguous'. delete_derived_resources returns {"deleted": {id: "deleted"|...}}."""
    if not isinstance(resp, dict):
        return "ambiguous"
    deleted = resp.get("deleted") or {}
    v = deleted.get(derived_id)
    if v in ("deleted", "not_found"):
        return "ok"
    if resp.get("partial"):
        return "ambiguous"
    if deleted and all(x == "deleted" for x in deleted.values()):
        return "ok"
    return "ambiguous"
