"""P8 — READ-ONLY Cloudinary cleanup-analysis engine (DRY-RUN ONLY).

Answers "what could we safely delete?" — and produces a per-asset manifest —
**without deleting anything**. It is structurally incapable of physical deletion:

  * it imports no Cloudinary uploader module and calls none of Cloudinary's
    asset-removal APIs (the test walks the module AST to assert this),
  * it performs **zero MongoDB writes** — every DB call in this module is
    ``find`` / ``aggregate`` / ``count_documents`` (the test asserts this too),
  * it makes **zero Cloudinary calls** — the object inventory is passed IN by
    the caller (from P7's cached ``/health`` scan or a fresh read-only listing);
    the engine never holds a Cloudinary handle at all.

Deletion eligibility requires ALL of:
    KNOWN OWNERSHIP  +  NO PROTECTED REFERENCES  +  RETENTION EXPIRED
  + NO DEPENDENCIES  +  SAFE LIFECYCLE STATE     +  KNOWN ASSET RELATIONSHIP
Any one unknown  ->  PROTECTED.

"Orphan" is NEVER sufficient for deletion. An asset unreferenced in MongoDB, or
whose parent document was deleted, or that sits in an old folder, or that is a
derived asset, or that is months old, is PROTECTED unless every condition above
holds AND a repo-wide search for its identifiers (public_id / secure_url /
poster_url / thumbnail_url / original_url / asset_id) finds nothing.

The manifest is a SNAPSHOT (`generated_at`, `source_*_time`, `manifest_id`). It
is NOT permanently authoritative — P9 must re-validate every asset immediately
before any deletion.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("talentgram")

# NOTE: this module deliberately imports NOTHING from cloudinary and holds no
# Cloudinary handle. The physical object inventory is passed into build_manifest()
# by the caller. The caller is responsible for obtaining it read-only (P7's
# cached /health scan, or a fresh cloudinary.api.resources listing) and for any
# caching of that list.

MEDIA_COLLECTIONS = ("talents", "submissions", "applications")

# ---- primary classifications (exactly one per asset) ----------------------
ACTIVE_GLOBAL_TALENT_MEDIA = "ACTIVE_GLOBAL_TALENT_MEDIA"
ACTIVE_PROJECT_AUDITION_MEDIA = "ACTIVE_PROJECT_AUDITION_MEDIA"
ACTIVE_DERIVED_ASSET = "ACTIVE_DERIVED_ASSET"
PROTECTED_HISTORICAL = "PROTECTED_HISTORICAL"
PROTECTED_SHARED = "PROTECTED_SHARED"
PROTECTED_UNKNOWN = "PROTECTED_UNKNOWN"
PROTECTED_CONFLICT = "PROTECTED_CONFLICT"
PENDING_RETENTION = "PENDING_RETENTION"
SAFE_ORPHAN = "SAFE_ORPHAN"
LEGACY_DERIVED_CANDIDATE = "LEGACY_DERIVED_CANDIDATE"
STALE_METADATA_ONLY = "STALE_METADATA_ONLY"

# ---- proposed actions ----------------------------------------------------
KEEP = "KEEP"
PROTECT = "PROTECT"
WAIT_FOR_RETENTION = "WAIT_FOR_RETENTION"
REVIEW = "REVIEW"
DELETE_ELIGIBLE = "DELETE_ELIGIBLE"   # NOT "delete now" — see module docstring

_ACTION_FOR = {
    ACTIVE_GLOBAL_TALENT_MEDIA: KEEP,
    ACTIVE_PROJECT_AUDITION_MEDIA: KEEP,
    ACTIVE_DERIVED_ASSET: KEEP,
    PROTECTED_HISTORICAL: PROTECT,
    PROTECTED_SHARED: PROTECT,
    PROTECTED_UNKNOWN: PROTECT,
    PROTECTED_CONFLICT: PROTECT,
    PENDING_RETENTION: WAIT_FOR_RETENTION,
    SAFE_ORPHAN: DELETE_ELIGIBLE,
    LEGACY_DERIVED_CANDIDATE: REVIEW,
    STALE_METADATA_ONLY: REVIEW,
}

_TRANSFORM_SEG_RE = re.compile(
    r"(^|,)(w_|h_|c_|q_|f_|dpr_|e_|so_|vc_|b_|ac_|g_|fl_|sp_|br_|pg_|a_|o_|r_|x_|y_|l_|u_)"
)
_VERSION_RE = re.compile(r"^v\d+$")


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
# Cloudinary delivery-URL / public_id parsing (pure)
# --------------------------------------------------------------------------
def parse_cloudinary_url(url: str) -> Dict[str, Optional[str]]:
    """From a Cloudinary delivery URL, recover: the bare parent public_id, the
    transformation segment (if any), and the resource_type. Non-Cloudinary or
    unparseable -> all None. NEVER consulted for ownership — only for the
    derived-parent relationship."""
    out: Dict[str, Optional[str]] = {"parent_public_id": None, "transformation": None,
                                     "resource_type": None}
    if not url or "res.cloudinary.com" not in url or "/upload/" not in url:
        return out
    try:
        after_host = url.split("res.cloudinary.com/", 1)[1]
        # <cloud>/<resource_type>/upload/<...>
        parts = after_host.split("/upload/", 1)
        head = parts[0].split("/")
        if len(head) >= 2:
            out["resource_type"] = head[1]
        tail = parts[1].split("?")[0].split("#")[0]
        segs = tail.split("/")
        transform = None
        while segs and (_VERSION_RE.match(segs[0]) or _TRANSFORM_SEG_RE.search(segs[0])):
            if _TRANSFORM_SEG_RE.search(segs[0]):
                transform = segs[0] if transform is None else transform
            segs.pop(0)
        pid = "/".join(segs).rsplit(".", 1)[0]
        out["parent_public_id"] = pid or None
        out["transformation"] = transform
    except Exception:
        pass
    return out


def is_derived_delivery_url(url: str) -> bool:
    return bool(parse_cloudinary_url(url).get("transformation"))


def _full_public_id_from_url(url: Optional[str]) -> Optional[str]:
    """The FULL Cloudinary public_id (folder + leaf, no version, no transform,
    no extension) recovered from a delivery URL. This is what
    ``cloudinary.api.resources()`` reports as ``public_id`` — and it is often
    NOT what ``media[].public_id`` stores (legacy sign endpoints stored a bare
    leaf id). Used to join the two."""
    if not url or "res.cloudinary.com" not in url or "/upload/" not in url:
        return None
    try:
        tail = url.split("/upload/", 1)[1].split("?")[0].split("#")[0]
        segs = tail.split("/")
        while segs and (_VERSION_RE.match(segs[0]) or _TRANSFORM_SEG_RE.search(segs[0])):
            segs.pop(0)
        pid = "/".join(segs).rsplit(".", 1)[0]
        return pid or None
    except Exception:
        return None


# --------------------------------------------------------------------------
# MongoDB reference index (one pass, no per-asset queries)
# --------------------------------------------------------------------------
async def build_reference_index(db) -> Dict[str, Any]:
    """One streamed pass over talents/submissions/applications media + links +
    asset_metadata. Returns maps keyed by public_id:

      owners[pid]        -> list of {collection, doc_id, media_id, category,
                                     owner_type, conflict, lifecycle_state,
                                     is_shared_copy, talent_id, project_id,
                                     submission_id, application_id, url, bytes}
      url_index          -> set of every Cloudinary identifier string seen in
                            ANY media field (url / poster_url / thumbnail_url /
                            original_url / public_id / asset_id / stream_uid) —
                            for the legacy-URL safety search
      source_lineage[mid]-> True if some doc has source_*_media_id == mid
      link_talent_ids    -> set of talent_ids surfaced by an ACTIVE review link
      link_submission_ids-> set of submission_ids surfaced by an ACTIVE link
      soft_deleted_subs  -> {submission_id: deleted_at_iso}
      soft_deleted_projs -> {project_id: deleted_at_iso}
      metadata_pids      -> {public_id: asset_metadata row (minimal)}
    """
    owners: Dict[str, List[Dict[str, Any]]] = {}
    url_index: set = set()
    source_lineage: set = set()
    soft_deleted_subs: Dict[str, Optional[str]] = {}
    soft_deleted_projs: Dict[str, Optional[str]] = {}

    persisted_derived_variants: set = set()   # (parent_public_id, transformation)

    def _index_urls(m: Dict[str, Any]):
        for k in ("url", "poster_url", "thumbnail_url", "original_url",
                  "compat_delivery_url", "video_url", "secure_url"):
            v = m.get(k)
            if isinstance(v, str) and v:
                url_index.add(v)
                p = parse_cloudinary_url(v)
                if p.get("transformation") and p.get("parent_public_id"):
                    persisted_derived_variants.add((p["parent_public_id"], p["transformation"]))
        for k in ("public_id", "asset_id", "stream_uid"):
            v = m.get(k)
            if isinstance(v, str) and v:
                url_index.add(v)

    for cname in MEDIA_COLLECTIONS:
        coll = getattr(db, cname)
        proj = {"_id": 0, "id": 1, "media": 1}
        if cname == "submissions":
            proj.update({"project_id": 1, "lifecycle_state": 1, "deleted_at": 1})
        async for doc in coll.find({"media": {"$exists": True, "$ne": []}}, proj):
            doc_soft_deleted = bool(doc.get("deleted_at")) or doc.get("lifecycle_state") == "deleted"
            if cname == "submissions" and doc_soft_deleted:
                soft_deleted_subs[doc["id"]] = doc.get("deleted_at")
            for m in doc.get("media") or []:
                # Only URLs persisted in a LIVE (non-soft-deleted) document count
                # as a protecting reference — a URL in a soft-deleted doc is
                # being torn down alongside the asset.
                if not doc_soft_deleted:
                    _index_urls(m)
                for sk in ("source_talent_media_id", "source_submission_media_id",
                           "source_application_media_id"):
                    if m.get(sk):
                        source_lineage.add(m[sk])
                # Index under BOTH the stored public_id AND the full public_id
                # recovered from the delivery URL. `submission_sign_upload`
                # historically stored a BARE id (just the media UUID) while
                # Cloudinary created the asset at `{folder}/{uuid}` — so the
                # Cloudinary inventory's public_id only matches the URL-derived
                # form. (Documented: cloudinary_admin.resolve_full_public_id.)
                keys = set()
                if m.get("public_id"):
                    keys.add(m["public_id"])
                if m.get("stream_uid"):
                    keys.add(m["stream_uid"])
                full = _full_public_id_from_url(m.get("url"))
                if full:
                    keys.add(full)
                if not keys:
                    continue
                own = m.get("ownership") or {}
                _owner_entry = {
                    "collection": cname, "doc_id": doc["id"], "media_id": m.get("id"),
                    "category": m.get("category"),
                    "owner_type": own.get("owner_type"), "conflict": own.get("conflict"),
                    "is_shared_copy": bool(own.get("is_shared_copy")),
                    "lifecycle_state": (m.get("lifecycle") or {}).get("state") or "active",
                    "talent_id": own.get("talent_id") or m.get("talent_id"),
                    "project_id": own.get("project_id") or doc.get("project_id"),
                    "submission_id": own.get("submission_id") or (doc["id"] if cname == "submissions" else None),
                    "application_id": own.get("application_id") or (doc["id"] if cname == "applications" else None),
                    "doc_soft_deleted": doc_soft_deleted,
                    "url": m.get("url"),
                    "bytes": m.get("size") or (m.get("ownership") or {}).get("cloudinary", {}).get("bytes"),
                    "resource_type": m.get("resource_type"),
                    "format": (m.get("ownership") or {}).get("cloudinary", {}).get("format"),
                    "created_at": m.get("created_at"),
                }
                for _k in keys:
                    owners.setdefault(_k, []).append(_owner_entry)

    # soft-deleted projects
    try:
        async for p in db.projects.find(
            {"$or": [{"lifecycle_state": "deleted"}, {"deleted_at": {"$exists": True, "$ne": None}}]},
            {"_id": 0, "id": 1, "deleted_at": 1},
        ):
            soft_deleted_projs[p["id"]] = p.get("deleted_at")
    except Exception as e:
        logger.warning("cleanup_manifest: soft-deleted projects read failed: %s", e)

    # active review links
    link_talent_ids: set = set()
    link_submission_ids: set = set()
    try:
        async for lk in db.links.find({}, {"_id": 0, "talent_ids": 1, "submission_ids": 1,
                                           "status": 1, "is_active": 1}):
            active = (lk.get("status") in (None, "active")) and (lk.get("is_active") in (None, True))
            if not active:
                continue
            link_talent_ids.update(lk.get("talent_ids") or [])
            link_submission_ids.update(lk.get("submission_ids") or [])
    except Exception as e:
        logger.warning("cleanup_manifest: links read failed: %s", e)

    metadata_pids: Dict[str, Any] = {}
    try:
        async for md in db.asset_metadata.find({}, {"_id": 0, "public_id": 1, "asset_type": 1,
                                                    "status": 1, "created_at": 1}):
            if md.get("public_id"):
                metadata_pids[md["public_id"]] = md
    except Exception as e:
        logger.warning("cleanup_manifest: asset_metadata read failed: %s", e)

    return {
        "owners": owners,
        "url_index": url_index,
        "persisted_derived_variants": persisted_derived_variants,
        "source_lineage": source_lineage,
        "link_talent_ids": link_talent_ids,
        "link_submission_ids": link_submission_ids,
        "soft_deleted_subs": soft_deleted_subs,
        "soft_deleted_projs": soft_deleted_projs,
        "metadata_pids": metadata_pids,
    }


async def build_ledger_index(db) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        async for r in db.pending_media_deletions.find({}, {"_id": 0}):
            key = r.get("public_id") or r.get("stream_uid")
            if key:
                out[key] = r
    except Exception as e:
        logger.warning("cleanup_manifest: ledger read failed: %s", e)
    return out


# --------------------------------------------------------------------------
# Legacy-URL safety search (the P5-interaction guard)
# --------------------------------------------------------------------------
def has_persisted_url_reference(public_id: str, url_index: set) -> bool:
    """True if ANY stored Cloudinary identifier string in ANY media field
    contains this public_id — including a derived delivery URL that carries a
    transformation segment in front of it (P5 deliberately did NOT rewrite
    legacy stored URLs, so an old client link / historical submission may still
    point straight at a derived asset)."""
    if not public_id:
        return False
    for s in url_index:
        if public_id in s:
            return True
    return False


# --------------------------------------------------------------------------
# Per-asset classifier (pure, given the pre-built indexes)
# --------------------------------------------------------------------------
def classify_asset(
    obj: Dict[str, Any],
    *,
    refs: Dict[str, Any],
    ledger: Dict[str, Dict[str, Any]],
    retention_days: int,
    known_public_ids: set,
    now: datetime,
) -> Dict[str, Any]:
    """Classify one Cloudinary physical object. PURE — all lookups are in the
    dicts/sets passed in. Returns a full manifest row."""
    pid = obj.get("public_id")
    url = obj.get("url") or ""
    parsed = parse_cloudinary_url(url)
    _xf = obj.get("transformation") or parsed.get("transformation")
    owner_rows = refs["owners"].get(pid, [])
    ledger_row = ledger.get(pid)
    md_row = refs["metadata_pids"].get(pid)

    # base row
    row: Dict[str, Any] = {
        "asset_id": obj.get("asset_id"),
        "public_id": pid,
        "resource_type": obj.get("resource_type") or parsed.get("resource_type"),
        "type": obj.get("type") or "upload",
        "format": obj.get("format"),
        "bytes": obj.get("bytes") or 0,
        "created_at": obj.get("created_at"),
        "folder": (pid.rsplit("/", 1)[0] if pid and "/" in pid else ""),
        "owner_type": None, "owner_id": None, "talent_id": None,
        "project_id": None, "submission_id": None, "application_id": None,
        "lifecycle_state": "active",
        "reference_count": len(owner_rows),
        "references": [
            {"collection": r["collection"], "doc_id": r["doc_id"], "media_id": r["media_id"],
             "category": r["category"], "soft_deleted": r["doc_soft_deleted"]}
            for r in owner_rows
        ],
        "retention_policy": ("indefinite" if retention_days == -1 else
                             ("immediate" if retention_days == 0 else f"{retention_days}d")),
        "eligible_at": None,
        "retention_remaining_seconds": None,
        "derived_parent": None,
        "derived_transformation": obj.get("transformation") or parsed.get("transformation"),
        "confidence": "high",
    }

    # ---- derived-asset relationship ---------------------------------------
    # A row is a DERIVED asset when the inventory tags it as one
    # (obj["type"] == "derived", Cloudinary's own marker, or an explicit
    # obj["derived_of"]) OR its delivery URL carries a transformation segment in
    # front of a DIFFERENT public_id. Originals (the common case) fall through.
    explicit_parent = obj.get("derived_of") or obj.get("parent_public_id")
    is_derived = bool(explicit_parent) or (
        obj.get("type") == "derived" and parsed.get("parent_public_id")
    ) or (
        bool(parsed.get("transformation")) and parsed.get("parent_public_id")
        and parsed.get("parent_public_id") != pid
    )
    parent_pid = explicit_parent or (parsed.get("parent_public_id") if is_derived else None)
    if is_derived:
        row["derived_parent"] = parent_pid
        parent_known = parent_pid in known_public_ids
        parent_referenced = bool(refs["owners"].get(parent_pid))
        if not parent_known:
            return _finish(row, PROTECTED_UNKNOWN,
                           f"Derived asset (transform '{_xf}') whose parent "
                           f"'{parent_pid}' is not in the current Cloudinary inventory — relationship "
                           f"unknown, cannot reason about safety.")
        _variant_persisted = (parent_pid, _xf) in refs.get("persisted_derived_variants", set()) \
            or has_persisted_url_reference(pid, refs["url_index"])
        if _variant_persisted:
            return _finish(row, PROTECTED_HISTORICAL,
                           f"Derived asset (transform '{_xf}' of '{parent_pid}') whose exact "
                           f"transformation URL is still persisted in a live media field (client "
                           f"link / historical submission). P5 does not rewrite legacy stored URLs "
                           f"— deleting this would break a live reference.")
        if parent_referenced:
            return _finish(row, LEGACY_DERIVED_CANDIDATE,
                           f"Derived asset (transform '{_xf}') of a still-live "
                           f"parent '{parent_pid}'. No persisted URL references this exact derivative "
                           f"and it is regenerable on demand — REVIEW before removal (a rare older "
                           f"cached client view could still request it).", confidence="medium")
        # parent exists but is itself unreferenced -> treat as orphan chain, still REVIEW
        return _finish(row, LEGACY_DERIVED_CANDIDATE,
                       f"Derived asset of parent '{parent_pid}', which is itself unreferenced. "
                       f"No persisted URL points here. REVIEW alongside the parent.",
                       confidence="medium")

    # ---- ownership from the reference index ------------------------------
    if not owner_rows:
        # physically present, zero MongoDB media references
        if md_row is not None:
            row["confidence"] = "medium"
            return _finish(row, STALE_METADATA_ONLY,
                           f"Present in asset_metadata (type={md_row.get('asset_type')}, "
                           f"status={md_row.get('status')}) but no media[] item references it. "
                           f"REVIEW — the metadata row may be stale, or the media ref was lost.")
        if has_persisted_url_reference(pid, refs["url_index"]):
            return _finish(row, PROTECTED_HISTORICAL,
                           "No media[] item owns this public_id, but a URL containing it is still "
                           "persisted in a media field. PROTECTED — a stored reference exists.")
        # genuine orphan, ownership unknown
        return _finish(row, PROTECTED_UNKNOWN,
                       "Physically present, zero MongoDB references, ownership cannot be "
                       "established from any authoritative record. PROTECTED — 'orphan' is never "
                       "sufficient for deletion.", confidence="low")

    # dedupe owner_type / conflict across all copies
    conflict = next((r["conflict"] for r in owner_rows if r.get("conflict")), None)
    owner_type = next((r["owner_type"] for r in owner_rows if r.get("owner_type")), None)
    primary = owner_rows[0]
    row.update({
        "owner_type": owner_type, "owner_id": None,
        "talent_id": next((r["talent_id"] for r in owner_rows if r.get("talent_id")), None),
        "project_id": next((r["project_id"] for r in owner_rows if r.get("project_id")), None),
        "submission_id": next((r["submission_id"] for r in owner_rows if r.get("submission_id")), None),
        "application_id": next((r["application_id"] for r in owner_rows if r.get("application_id")), None),
        "lifecycle_state": next((r["lifecycle_state"] for r in owner_rows
                                 if r["lifecycle_state"] in ("deleted", "pending_deletion")),
                                owner_rows[0]["lifecycle_state"]),
    })

    if conflict:
        return _finish(row, PROTECTED_CONFLICT,
                       f"P3 ownership has an unresolved conflict ({conflict}). PROTECTED — never "
                       f"deleted while ownership is ambiguous.")
    if not owner_type:
        return _finish(row, PROTECTED_UNKNOWN,
                       "Referenced by a media item that carries no resolved P3 owner_type. "
                       "PROTECTED.", confidence="low")

    # ---- shared / historical protections --------------------------------
    distinct_docs = {(r["collection"], r["doc_id"]) for r in owner_rows}
    if len(distinct_docs) > 1 or any(r["is_shared_copy"] for r in owner_rows):
        return _finish(row, PROTECTED_SHARED,
                       f"Backing object shared by {len(distinct_docs)} owner document(s) "
                       f"(copy-by-value). PROTECTED while any reference remains.")

    # active (non-soft-deleted) owner document -> live reference
    live_rows = [r for r in owner_rows if not r["doc_soft_deleted"]]
    if live_rows:
        # client-review-link protection
        tids = {r["talent_id"] for r in owner_rows if r["talent_id"]}
        sids = {r["submission_id"] for r in owner_rows if r["submission_id"]}
        if tids & refs["link_talent_ids"] or sids & refs["link_submission_ids"]:
            return _finish(row, PROTECTED_HISTORICAL,
                           "Surfaced by an ACTIVE client-review link. PROTECTED.")
        if owner_type == "talent":
            return _finish(row, ACTIVE_GLOBAL_TALENT_MEDIA,
                           "Live global talent media (referenced by a non-deleted "
                           f"{live_rows[0]['collection']} record). KEEP.")
        return _finish(row, ACTIVE_PROJECT_AUDITION_MEDIA,
                       "Live project audition media (referenced by a non-deleted submission). KEEP.")

    # ---- only soft-deleted owner(s) remain — retention path -------------
    if owner_type == "talent":
        # global talent media is only ever eligible when the talent is hard-deleted;
        # a soft-deleted submission copy does not make it deletable
        if has_persisted_url_reference(pid, refs["url_index"]):
            return _finish(row, PROTECTED_HISTORICAL,
                           "Global talent media whose only live-doc reference is gone, but a URL "
                           "containing it is still persisted. PROTECTED.")
        return _finish(row, PROTECTED_HISTORICAL,
                       "Global talent media referenced only by soft-deleted document(s). The "
                       "canonical asset stays with the talent — not eligible via submission "
                       "teardown. PROTECTED.")

    # project audition media, all owner docs soft-deleted -> retention
    if retention_days == -1:
        return _finish(row, PENDING_RETENTION,
                       "Project audition media; owning submission/project soft-deleted; retention "
                       "policy is INDEFINITE — never auto-eligible. WAIT.")
    # find the most recent teardown timestamp (ledger eligible_at, or doc deleted_at + retention)
    eligible_at = None
    if ledger_row and ledger_row.get("eligible_at"):
        eligible_at = _parse(ledger_row["eligible_at"])
    else:
        del_times = []
        for r in owner_rows:
            dt = refs["soft_deleted_subs"].get(r["submission_id"]) if r["submission_id"] else None
            dt = dt or (refs["soft_deleted_projs"].get(r["project_id"]) if r["project_id"] else None)
            if dt:
                del_times.append(_parse(dt))
        base = max(del_times) if del_times else None
        if base and retention_days >= 0:
            eligible_at = base + timedelta(days=retention_days)
    if eligible_at is None:
        return _finish(row, PENDING_RETENTION,
                       "Project audition media with soft-deleted owner(s) but no resolvable "
                       "teardown timestamp — cannot prove retention elapsed. WAIT.",
                       confidence="medium")
    row["eligible_at"] = eligible_at.isoformat()
    remaining = (eligible_at - now).total_seconds()
    row["retention_remaining_seconds"] = int(remaining)
    if remaining > 0:
        return _finish(row, PENDING_RETENTION,
                       f"Project audition media owned by {row['submission_id']}. Owner soft-deleted; "
                       f"{int(remaining/86400)}d of the {retention_days}d retention remain. WAIT.")
    if has_persisted_url_reference(pid, refs["url_index"]):
        return _finish(row, PROTECTED_HISTORICAL,
                       "Retention elapsed, but a URL containing this public_id is still persisted "
                       "in a media field. PROTECTED — a stored reference exists.")
    return _finish(
        row, SAFE_ORPHAN,
        f"Project audition media owned by submission {row['submission_id']}"
        f"{(' (project ' + row['project_id'] + ')') if row['project_id'] else ''}. "
        f"Owning submission/project was soft-deleted "
        f"{_days_ago(eligible_at - timedelta(days=retention_days), now)} days ago; "
        f"{retention_days}-day retention expired {int(-remaining/86400)} days ago. "
        f"No remaining submission / application / talent-library / active-client-link reference, "
        f"and no persisted URL contains this public_id. Safe to delete subject to P9's final "
        f"pre-delete re-check.")


def _finish(row: Dict[str, Any], classification: str, reason: str,
            *, confidence: Optional[str] = None) -> Dict[str, Any]:
    row["classification"] = classification
    row["proposed_action"] = _ACTION_FOR[classification]
    row["reason"] = reason
    if confidence:
        row["confidence"] = confidence
    return row


def _parse(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return _now()


def _days_ago(dt: datetime, now: datetime) -> int:
    return max(0, int((now - dt).total_seconds() / 86400))


def _reconcile(total_bytes, referenced_bytes, originals, derived, usage):
    """Cloudinary total = originals + derived. This engine's inventory is
    ORIGINALS (cloudinary.api.resources does not enumerate derived assets); the
    derived COUNT comes from usage() when supplied. Per-object derived
    classification needs a separate opt-in deep scan (one resource(derived=True)
    call per original) — out of scope for a page-load manifest."""
    u = usage or {}
    usage_derived = None
    dr = u.get("derived_resources")
    if isinstance(dr, int):
        usage_derived = dr
    usage_storage = None
    st = u.get("storage")
    if isinstance(st, dict):
        usage_storage = st.get("usage")
    elif isinstance(st, (int, float)):
        usage_storage = int(st)
    return {
        "manifest_inventory": "originals_only (cloudinary.api.resources)",
        "original_objects_scanned": len(originals),
        "derived_objects_in_manifest": len(derived),
        "derived_objects_reported_by_usage_api": usage_derived,
        "derived_note": ("Derived assets are not individually enumerated here — "
                         "cloudinary.api.resources() lists originals only. Their aggregate "
                         "count comes from the usage API; per-object classification would need "
                         "a per-original derived scan (out of scope for this manifest)."),
        "manifest_original_bytes": total_bytes,
        "cloudinary_usage_api_total_storage_bytes": usage_storage,
        "distinct_referenced_original_bytes": referenced_bytes,
        "unreferenced_or_unknown_original_bytes": max(0, total_bytes - referenced_bytes),
        "gap_vs_usage_api_bytes": (max(0, (usage_storage or 0) - total_bytes)
                                   if usage_storage else None),
        "gap_explanation": ("usage-API storage minus manifest original bytes ≈ the bytes held "
                            "by the ~derived_objects_reported_by_usage_api derived assets."),
        "note": "Shared backing objects counted once in distinct_referenced_original_bytes.",
    }


# --------------------------------------------------------------------------
# Manifest assembler
# --------------------------------------------------------------------------
async def build_manifest(
    db,
    *,
    objects: List[Dict[str, Any]],
    inventory_fetched_at: Optional[str] = None,
    inventory_from_cache: bool = False,
    inventory_stale: bool = False,
    cloudinary_usage: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Produce the full read-only cleanup manifest.

    ``objects`` is the Cloudinary physical inventory the CALLER already fetched
    (each item: ``{public_id, asset_id?, bytes, resource_type, type?, format?,
    url, created_at?}``). This engine makes NO Cloudinary call and NO MongoDB
    write. Deterministic for a fixed snapshot — rows are sorted by public_id and
    the ``manifest_id`` is a hash of (inventory time, object count, per-row
    (public_id, classification))."""
    from media_lifecycle import get_retention_days

    now = now or _now()
    mongo_snapshot_time = now.isoformat()
    refs = await build_reference_index(db)
    ledger = await build_ledger_index(db)
    retention_days = await get_retention_days(db)
    inv = {"fetched_at": inventory_fetched_at, "from_cache": inventory_from_cache,
           "stale": inventory_stale}

    known_public_ids = {o.get("public_id") for o in objects if o.get("public_id")}

    rows: List[Dict[str, Any]] = []
    for obj in sorted(objects, key=lambda o: o.get("public_id") or ""):
        rows.append(classify_asset(
            obj, refs=refs, ledger=ledger, retention_days=retention_days,
            known_public_ids=known_public_ids, now=now,
        ))

    # counts + reconciliation
    by_class: Dict[str, int] = {}
    by_action: Dict[str, int] = {}
    bytes_by_class: Dict[str, int] = {}
    delete_eligible_bytes = 0
    referenced_bytes = 0
    counted_pids: set = set()
    for r in rows:
        by_class[r["classification"]] = by_class.get(r["classification"], 0) + 1
        by_action[r["proposed_action"]] = by_action.get(r["proposed_action"], 0) + 1
        bytes_by_class[r["classification"]] = bytes_by_class.get(r["classification"], 0) + (r["bytes"] or 0)
        if r["proposed_action"] == DELETE_ELIGIBLE:
            delete_eligible_bytes += r["bytes"] or 0
        if r["reference_count"] > 0 and r["public_id"] not in counted_pids:
            referenced_bytes += r["bytes"] or 0
            counted_pids.add(r["public_id"])

    total_bytes = sum((o.get("bytes") or 0) for o in objects)
    originals = [o for o in objects if not is_derived_delivery_url(o.get("url") or "")]
    derived = [o for o in objects if is_derived_delivery_url(o.get("url") or "")]

    manifest_id = hashlib.sha256(
        json.dumps({
            "inv": inv.get("fetched_at"),
            "n": len(objects),
            "rows": [(r["public_id"], r["classification"]) for r in rows],
        }, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    every_delete_eligible_has_reason = all(
        bool(r.get("reason")) and "orphan" != (r.get("reason") or "").strip().lower()
        for r in rows if r["proposed_action"] == DELETE_ELIGIBLE
    )

    return {
        "manifest_id": manifest_id,
        "generated_at": now.isoformat(),
        "source_cloudinary_inventory_time": inv.get("fetched_at"),
        "source_cloudinary_inventory_from_cache": inv.get("from_cache"),
        "source_cloudinary_inventory_stale": inv.get("stale"),
        "source_mongo_snapshot_time": mongo_snapshot_time,
        "retention_policy_days": retention_days,
        "dry_run": True,
        "read_only": True,
        "not_authoritative_note": (
            "This manifest is a SNAPSHOT. DELETE_ELIGIBLE means 'could be safely deleted IF an "
            "administrator later approves AND P9's fresh per-asset re-check passes' — it is NOT "
            "an instruction to delete. P9 re-validates every asset immediately before deletion."
        ),
        "totals": {
            "objects_scanned": len(objects),
            "total_bytes": total_bytes,
            "original_objects": len(originals),
            "derived_objects": len(derived),
            "distinct_referenced_bytes": referenced_bytes,
            "delete_eligible_objects": by_action.get(DELETE_ELIGIBLE, 0),
            "delete_eligible_bytes": delete_eligible_bytes,
        },
        "by_classification": by_class,
        "by_proposed_action": by_action,
        "bytes_by_classification": bytes_by_class,
        "reconciliation": _reconcile(total_bytes, referenced_bytes, originals, derived,
                                     cloudinary_usage),
        "integrity": {
            "every_delete_eligible_row_has_explanation": every_delete_eligible_has_reason,
            "cloudinary_writes": 0,
            "mongodb_writes": 0,
        },
        "rows": rows,
    }
