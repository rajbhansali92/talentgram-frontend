"""Authoritative, ownership-aware media lifecycle & deletion-safety service
(Cloudinary rearchitecture, P6).

ONE place decides whether a media asset may be physically deleted. Every delete
path — talents.py, projects.py, submissions.py, applications.py,
cloudinary_admin.py, links.py, core.py's cleanup helpers — must route through
this module instead of re-implementing its own rules.

Deletion is decided from the **MongoDB ownership + reference graph**, never from
the Cloudinary folder string:

    media[i].ownership (P3)  ->  owner_type / owner_id / project_id / submission_id
            +
    live reference scan across talents / submissions / applications / links
            ->  can_delete() decision

Core guarantees
---------------
* GLOBAL_TALENT_MEDIA (intro video, portfolio / indian / western images) belongs
  to the talent. Removing the talent from a project, or deleting a project, or
  deleting one submission, never makes it deletable. It is only ever eligible
  when the talent itself is legitimately hard-deleted AND nothing else refers to
  the asset.
* PROJECT_AUDITION_MEDIA (takes) belongs to project+submission. Deleting the
  submission / project marks it PENDING_DELETION; it becomes eligible only after
  the configured retention period elapses and every reference is gone.
* UNKNOWN or CONFLICTING ownership  ->  PROTECT. Always.
* A shared-by-value asset (same public_id on >1 owner doc, or referenced by a
  live client-review link) is PROTECTED while any reference remains.

P6 production safety
--------------------
Physical Cloudinary deletion is gated behind the env flag
``MEDIA_LIFECYCLE_PHYSICAL_DELETE`` (default OFF). While OFF — the state during
the P6 rollout — ``delete_if_safe`` only ever advances an asset to
PENDING_DELETION; it never calls Cloudinary. P8/P9 build the controlled purge.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("talentgram")

# --------------------------------------------------------------------------
# Lifecycle states  (stored on media[i].lifecycle.state; absent == ACTIVE)
# --------------------------------------------------------------------------
STATE_ACTIVE = "active"
STATE_PENDING_DELETION = "pending_deletion"
STATE_DELETED = "deleted"

# --------------------------------------------------------------------------
# Ownership (mirrors backend/migrations/media_ownership_rules.py — P3)
# --------------------------------------------------------------------------
OWNER_TALENT = "talent"
OWNER_PROJECT_SUBMISSION = "project_submission"

TAKE_CATEGORIES = {"take", "take_1", "take_2", "take_3"}

# --------------------------------------------------------------------------
# Retention policy
# --------------------------------------------------------------------------
RETENTION_CONFIG_KEY = "audition_retention_days"
DEFAULT_AUDITION_RETENTION_DAYS = 30
RETENTION_IMMEDIATE = 0
RETENTION_INDEFINITE = -1
_VALID_RETENTION = {0, 30, 90, -1}


def _physical_delete_enabled() -> bool:
    return os.environ.get("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def resolve_retention_days(raw: Any) -> int:
    """Coerce a configured retention value to a safe integer.

    Allowed: 0 (immediate), 30, 90, -1 (indefinite). Anything else — a missing
    value, a non-int, a stray 45, a negative that isn't -1 — resolves to
    INDEFINITE (never purge). "Safe" here means "err towards keeping the asset".
    """
    if raw is None:
        return DEFAULT_AUDITION_RETENTION_DAYS
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning("media_lifecycle: non-integer retention %r -> INDEFINITE", raw)
        return RETENTION_INDEFINITE
    if val in _VALID_RETENTION:
        return val
    logger.warning("media_lifecycle: out-of-policy retention %r -> INDEFINITE", raw)
    return RETENTION_INDEFINITE


async def get_retention_days(db) -> int:
    """Read the configured audition retention from db.app_config, falling back
    to the 30-day default when the row is absent."""
    try:
        row = await db.app_config.find_one({"key": RETENTION_CONFIG_KEY}, {"_id": 0, "value": 1})
    except Exception as e:
        logger.warning("media_lifecycle: retention config read failed (%s) -> default", e)
        return DEFAULT_AUDITION_RETENTION_DAYS
    if not row or "value" not in row:
        return DEFAULT_AUDITION_RETENTION_DAYS
    return resolve_retention_days(row.get("value"))


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------
@dataclass
class OwnerInfo:
    owner_type: Optional[str]           # "talent" | "project_submission" | None (unknown)
    owner_id: Optional[str] = None
    talent_id: Optional[str] = None
    project_id: Optional[str] = None
    submission_id: Optional[str] = None
    application_id: Optional[str] = None
    is_shared_copy: bool = False
    conflict: Optional[str] = None
    source: str = "ownership"           # where the classification came from

    @property
    def is_known(self) -> bool:
        return self.owner_type in (OWNER_TALENT, OWNER_PROJECT_SUBMISSION) and not self.conflict

    @property
    def is_global_talent_media(self) -> bool:
        return self.owner_type == OWNER_TALENT and not self.conflict

    @property
    def is_project_audition_media(self) -> bool:
        return self.owner_type == OWNER_PROJECT_SUBMISSION and not self.conflict


@dataclass
class Dependency:
    kind: str            # "talent" | "submission" | "application" | "review_link" | "lineage"
    id: Optional[str]
    detail: str
    protects: bool
    status: Optional[str] = None


@dataclass
class LifecycleDecision:
    deletable: bool
    state: str                       # recommended lifecycle state
    reason: str
    owner: OwnerInfo
    dependencies: List[Dependency] = field(default_factory=list)
    retention_days: Optional[int] = None
    eligible_at: Optional[str] = None
    has_backing_asset: bool = True

    def explain(self) -> Dict[str, Any]:
        return {
            "deletable": self.deletable,
            "state": self.state,
            "reason": self.reason,
            "owner": {
                "owner_type": self.owner.owner_type,
                "owner_id": self.owner.owner_id,
                "talent_id": self.owner.talent_id,
                "project_id": self.owner.project_id,
                "submission_id": self.owner.submission_id,
                "is_shared_copy": self.owner.is_shared_copy,
                "conflict": self.owner.conflict,
            },
            "dependencies": [
                {"kind": d.kind, "id": d.id, "detail": d.detail, "protects": d.protects, "status": d.status}
                for d in self.dependencies
            ],
            "retention_days": self.retention_days,
            "eligible_at": self.eligible_at,
            "has_backing_asset": self.has_backing_asset,
        }


@dataclass
class DeletionContext:
    """What the caller is doing — narrows which protections may be lifted."""
    actor: Optional[str] = None
    # a talent is being legitimately hard-deleted (value == that talent_id, and
    # the caller has already confirmed zero blockers via talent_hard_delete_blockers)
    talent_hard_delete: Optional[str] = None
    # a project / submission is being deleted (the audition media it owns may go PENDING)
    project_deletion: Optional[str] = None
    submission_deletion: Optional[str] = None
    # the asset was just uploaded in this same request and is being rejected
    # (validation failure) — brand new, nothing can reference it yet
    just_uploaded_reject: bool = False
    # exclude this (collection, parent_id) pair from the reference scan — the
    # record the caller is removing the media from / deleting
    exclude_collection: Optional[str] = None
    exclude_parent_id: Optional[str] = None
    now: Optional[datetime] = None


def _now(ctx: Optional[DeletionContext] = None) -> datetime:
    if ctx and ctx.now:
        return ctx.now
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Ownership classification  (authoritative field: media[i].ownership — P3)
# --------------------------------------------------------------------------
def classify_owner(media: Dict[str, Any]) -> OwnerInfo:
    """Read the P3 ownership sub-document. NEVER infers from the Cloudinary
    folder. A missing sub-document, a null owner_type, or a set `conflict` all
    yield an UNKNOWN OwnerInfo (owner_type=None) which the gate treats as
    PROTECT.
    """
    own = media.get("ownership")
    if isinstance(own, dict) and (own.get("owner_type") or own.get("conflict")):
        return OwnerInfo(
            owner_type=own.get("owner_type"),
            owner_id=own.get("owner_id"),
            talent_id=own.get("talent_id"),
            project_id=own.get("project_id"),
            submission_id=own.get("submission_id"),
            application_id=own.get("application_id"),
            is_shared_copy=bool(own.get("is_shared_copy")),
            conflict=own.get("conflict"),
            source="ownership",
        )
    # No P3 sub-document (pre-migration item, or a doc created after the backfill
    # without the enrich pass). Fall back to the SAME application-state signal
    # P3 itself uses — category — but only for the unambiguous take case; every
    # other un-migrated item is UNKNOWN and therefore protected.
    cat = (media.get("category") or "").strip().lower()
    if cat in TAKE_CATEGORIES:
        return OwnerInfo(
            owner_type=OWNER_PROJECT_SUBMISSION,
            owner_id=media.get("submission_id"),
            talent_id=media.get("talent_id"),
            project_id=media.get("project_id"),
            submission_id=media.get("submission_id"),
            is_shared_copy=bool(media.get("source_talent_media_id")),
            source="category:take (no ownership sub-doc)",
        )
    return OwnerInfo(owner_type=None, conflict="no_ownership_metadata", source="missing")


# --------------------------------------------------------------------------
# Reference graph
# --------------------------------------------------------------------------
def _asset_keys(media: Dict[str, Any]) -> Dict[str, Any]:
    return {"public_id": media.get("public_id"), "stream_uid": media.get("stream_uid")}


def _key_or(public_id: Optional[str], stream_uid: Optional[str], prefix: str = "media") -> Optional[Dict[str, Any]]:
    ors = []
    if public_id:
        ors.append({f"{prefix}.public_id": public_id})
    if stream_uid:
        ors.append({f"{prefix}.stream_uid": stream_uid})
    if not ors:
        return None
    return {"$or": ors} if len(ors) > 1 else ors[0]


async def get_dependencies(
    db,
    media: Dict[str, Any],
    *,
    ctx: Optional[DeletionContext] = None,
) -> List[Dependency]:
    """Every active / historical reference to this asset's backing storage
    object, across all owner collections + client-review links + copy-by-value
    lineage. `ctx.exclude_*` drops the one record the caller is deleting from.
    """
    public_id = media.get("public_id")
    stream_uid = media.get("stream_uid")
    media_id = media.get("id")
    deps: List[Dependency] = []
    if not public_id and not stream_uid:
        return deps

    q = _key_or(public_id, stream_uid, "media")
    excl_coll = ctx.exclude_collection if ctx else None
    excl_pid = ctx.exclude_parent_id if ctx else None

    # ---- talents.media -----------------------------------------------------
    async for t in db.talents.find(q, {"_id": 0, "id": 1, "name": 1}):
        if excl_coll == "talents" and t.get("id") == excl_pid:
            continue
        deps.append(Dependency(
            kind="talent", id=t.get("id"),
            detail=f"talent global library: {t.get('name') or t.get('id')}",
            protects=True, status="active",
        ))

    # ---- submissions.media ----------------------------------------------------
    async for s in db.submissions.find(q, {"_id": 0, "id": 1, "project_id": 1, "lifecycle_state": 1, "deleted_at": 1}):
        if excl_coll == "submissions" and s.get("id") == excl_pid:
            continue
        soft_deleted = bool(s.get("deleted_at")) or s.get("lifecycle_state") == STATE_DELETED
        deps.append(Dependency(
            kind="submission", id=s.get("id"),
            detail=("soft-deleted submission (retention clock running)" if soft_deleted
                    else "live historical submission"),
            protects=not soft_deleted,     # a still-live submission protects; a soft-deleted one is handled by retention
            status="soft_deleted" if soft_deleted else "active",
        ))

    # ---- applications.media -------------------------------------------------
    async for a in db.applications.find(q, {"_id": 0, "id": 1, "status": 1}):
        if excl_coll == "applications" and a.get("id") == excl_pid:
            continue
        deps.append(Dependency(
            kind="application", id=a.get("id"),
            detail=f"application {a.get('id')}", protects=True, status=a.get("status"),
        ))

    # ---- client-review links ---------------------------------------------------
    # A link that surfaces a talent / submission which carries this media renders
    # it live — destroying the asset breaks the link. Include the asset's own
    # owner ids (from P3) so the scan still works when the caller excluded the
    # owning record from the dependency count.
    own = classify_owner(media)
    talent_ids = {d.id for d in deps if d.kind == "talent"}
    submission_ids = {d.id for d in deps if d.kind == "submission" and d.status == "active"}
    if own.talent_id and not (excl_coll == "talents" and own.talent_id == excl_pid):
        talent_ids.add(own.talent_id)
    if own.submission_id and not (excl_coll == "submissions" and own.submission_id == excl_pid):
        submission_ids.add(own.submission_id)
    if talent_ids or submission_ids:
        link_q = {"$or": []}
        if talent_ids:
            link_q["$or"].append({"talent_ids": {"$in": list(talent_ids)}})
        if submission_ids:
            link_q["$or"].append({"submission_ids": {"$in": list(submission_ids)}})
        async for lk in db.links.find(link_q, {"_id": 0, "slug": 1, "status": 1, "is_active": 1}):
            active = (lk.get("status") in (None, "active")) and (lk.get("is_active") in (None, True))
            deps.append(Dependency(
                kind="review_link", id=lk.get("slug"),
                detail=f"client review link '{lk.get('slug')}'",
                protects=active, status="active" if active else "inactive",
            ))

    # ---- copy-by-value lineage -------------------------------------------------
    if media_id:
        lineage_q = {"$or": [
            {"media.source_talent_media_id": media_id},
            {"media.source_submission_media_id": media_id},
            {"media.source_application_media_id": media_id},
        ]}
        for coll_name, coll in (("submissions", db.submissions), ("talents", db.talents), ("applications", db.applications)):
            async for d in coll.find(lineage_q, {"_id": 0, "id": 1}):
                if excl_coll == coll_name and d.get("id") == excl_pid:
                    continue
                deps.append(Dependency(
                    kind="lineage", id=d.get("id"),
                    detail=f"copy-by-value descendant in {coll_name}", protects=True, status="active",
                ))

    return deps


# --------------------------------------------------------------------------
# The safety gate
# --------------------------------------------------------------------------
async def can_delete(
    db,
    media: Dict[str, Any],
    *,
    ctx: Optional[DeletionContext] = None,
    retention_days: Optional[int] = None,
) -> LifecycleDecision:
    """Decide whether this media asset's backing storage object may be
    physically destroyed. Any uncertainty -> not deletable (PROTECTED)."""
    ctx = ctx or DeletionContext()
    owner = classify_owner(media)
    public_id = media.get("public_id")
    stream_uid = media.get("stream_uid")
    lifecycle = media.get("lifecycle") or {}
    state = lifecycle.get("state") or STATE_ACTIVE

    # already gone
    if state == STATE_DELETED:
        return LifecycleDecision(
            deletable=False, state=STATE_DELETED,
            reason="asset already marked deleted", owner=owner,
        )

    # nothing to physically delete — the caller may safely drop the DB reference
    if not public_id and not stream_uid:
        return LifecycleDecision(
            deletable=False, state=STATE_ACTIVE,
            reason="no backing storage asset (nothing to delete)",
            owner=owner, has_backing_asset=False,
        )

    # unknown / conflicting ownership -> PROTECT
    if not owner.is_known:
        return LifecycleDecision(
            deletable=False, state=STATE_ACTIVE,
            reason=f"ownership unknown or conflicting ({owner.conflict or 'no metadata'})",
            owner=owner,
        )

    deps = await get_dependencies(db, media, ctx=ctx)
    protecting = [d for d in deps if d.protects]

    if protecting:
        return LifecycleDecision(
            deletable=False, state=STATE_ACTIVE,
            reason=f"{len(protecting)} active/historical reference(s) protect this asset",
            owner=owner, dependencies=deps,
        )

    if retention_days is None:
        retention_days = await get_retention_days(db)

    # ---- GLOBAL TALENT MEDIA -------------------------------------------------
    if owner.is_global_talent_media:
        if ctx.talent_hard_delete and ctx.talent_hard_delete == owner.talent_id:
            # talent legitimately hard-deleted AND nothing references the asset.
            # Still not an instant purge unless retention is 0 — global media
            # gets the same PENDING window so an accidental hard-delete is
            # recoverable.
            if retention_days == RETENTION_IMMEDIATE:
                return LifecycleDecision(
                    deletable=True, state=STATE_DELETED,
                    reason="talent hard-deleted, no references, retention=immediate",
                    owner=owner, dependencies=deps, retention_days=retention_days,
                )
            return _pending_decision(media, owner, deps, retention_days, ctx,
                                     "talent hard-deleted; global media enters retention window")
        return LifecycleDecision(
            deletable=False, state=STATE_ACTIVE,
            reason="global talent media — only deletable when the owning talent is hard-deleted",
            owner=owner, dependencies=deps,
        )

    # ---- PROJECT AUDITION MEDIA -------------------------------------------------
    if owner.is_project_audition_media:
        if ctx.just_uploaded_reject:
            return LifecycleDecision(
                deletable=True, state=STATE_DELETED,
                reason="audition asset rejected on upload (brand new, no references)",
                owner=owner, dependencies=deps, retention_days=retention_days,
            )
        if retention_days == RETENTION_INDEFINITE:
            return _pending_decision(media, owner, deps, retention_days, ctx,
                                     "audition media pending; retention is indefinite (never auto-purged)")
        if retention_days == RETENTION_IMMEDIATE:
            return LifecycleDecision(
                deletable=True, state=STATE_DELETED,
                reason="audition media, no references, retention=immediate",
                owner=owner, dependencies=deps, retention_days=retention_days,
            )
        # retention 30 / 90 — needs a PENDING marker whose clock has elapsed
        marked_at = lifecycle.get("marked_at")
        if state != STATE_PENDING_DELETION or not marked_at:
            return _pending_decision(media, owner, deps, retention_days, ctx,
                                     "audition media marked pending; retention clock started")
        eligible_at = _parse_iso(marked_at) + timedelta(days=retention_days)
        if _now(ctx) >= eligible_at:
            return LifecycleDecision(
                deletable=True, state=STATE_DELETED,
                reason="audition media, retention elapsed, no references",
                owner=owner, dependencies=deps, retention_days=retention_days,
                eligible_at=eligible_at.isoformat(),
            )
        return LifecycleDecision(
            deletable=False, state=STATE_PENDING_DELETION,
            reason="audition media pending — retention period not yet elapsed",
            owner=owner, dependencies=deps, retention_days=retention_days,
            eligible_at=eligible_at.isoformat(),
        )

    # ---- fallthrough -> PROTECT ----------------------------------------------
    return LifecycleDecision(
        deletable=False, state=STATE_ACTIVE,
        reason="no rule authorises deletion", owner=owner, dependencies=deps,
    )


def _pending_decision(media, owner, deps, retention_days, ctx, reason) -> LifecycleDecision:
    now = _now(ctx)
    eligible = None
    if retention_days not in (RETENTION_INDEFINITE,):
        eligible = (now + timedelta(days=max(retention_days, 0))).isoformat()
    return LifecycleDecision(
        deletable=False, state=STATE_PENDING_DELETION, reason=reason,
        owner=owner, dependencies=deps, retention_days=retention_days, eligible_at=eligible,
    )


def _parse_iso(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# State transitions
# --------------------------------------------------------------------------
async def mark_pending_deletion(
    db,
    collection_name: str,
    parent_id: str,
    media_id: str,
    *,
    reason: str,
    retention_days: int,
    actor: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Advance one media item to PENDING_DELETION. Idempotent — a no-op if it is
    already PENDING or DELETED (the original marked_at / retention clock is
    preserved)."""
    coll = getattr(db, collection_name)
    parent = await coll.find_one({"id": parent_id}, {"_id": 0, "media": 1})
    if not parent:
        return {"ok": False, "reason": "parent not found"}
    item = next((m for m in (parent.get("media") or []) if m.get("id") == media_id), None)
    if not item:
        return {"ok": False, "reason": "media not found"}
    existing = (item.get("lifecycle") or {}).get("state")
    if existing in (STATE_PENDING_DELETION, STATE_DELETED):
        return {"ok": True, "unchanged": True, "state": existing, "lifecycle": item.get("lifecycle")}

    ts = (now or datetime.now(timezone.utc))
    eligible_at = None if retention_days == RETENTION_INDEFINITE else (
        ts + timedelta(days=max(retention_days, 0))
    ).isoformat()
    lifecycle = {
        "state": STATE_PENDING_DELETION,
        "marked_at": ts.isoformat(),
        "eligible_at": eligible_at,
        "retention_days": retention_days,
        "reason": reason,
        "marked_by": actor,
    }
    await coll.update_one(
        {"id": parent_id, "media.id": media_id},
        {"$set": {"media.$.lifecycle": lifecycle}},
    )
    return {"ok": True, "unchanged": False, "state": STATE_PENDING_DELETION, "lifecycle": lifecycle}


async def _mark_deleted(db, collection_name: Optional[str], parent_id: Optional[str],
                        media_id: Optional[str], actor: Optional[str]) -> None:
    if not (collection_name and parent_id and media_id):
        return
    coll = getattr(db, collection_name)
    await coll.update_one(
        {"id": parent_id, "media.id": media_id},
        {"$set": {"media.$.lifecycle": {
            "state": STATE_DELETED,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "deleted_by": actor,
        }}},
    )


async def delete_if_safe(
    db,
    media: Dict[str, Any],
    *,
    ctx: Optional[DeletionContext] = None,
    collection_name: Optional[str] = None,
    parent_id: Optional[str] = None,
    destroyer=None,
    retention_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Run the safety gate, then act on its verdict:

      * already DELETED           -> no-op ("already_deleted"); never double-destroys
      * deletable + physical ON   -> call `destroyer(media)`, set state DELETED
      * deletable + physical OFF  -> record intent, set state PENDING ("would_delete")
      * not deletable, audition   -> mark PENDING_DELETION ("marked_pending")
      * not deletable, otherwise  -> leave ACTIVE ("protected")

    `destroyer` defaults to core.safe_cleanup_media_storage's physical half; tests
    inject a mock. Physical deletion additionally requires the
    MEDIA_LIFECYCLE_PHYSICAL_DELETE env flag (OFF during the P6 rollout).
    """
    ctx = ctx or DeletionContext()
    decision = await can_delete(db, media, ctx=ctx, retention_days=retention_days)
    mid = media.get("id")

    if decision.state == STATE_DELETED and not decision.deletable:
        return {"outcome": "already_deleted", "decision": decision.explain()}

    if not decision.has_backing_asset:
        return {"outcome": "no_asset", "decision": decision.explain()}

    if decision.deletable:
        # Failed-upload rollback: the asset was created in this same request and
        # never persisted to any owner document. Destroying it completes an
        # aborted transaction rather than deleting "existing" media, so it is
        # NOT gated by the P6 physical-delete flag.
        if ctx.just_uploaded_reject:
            if destroyer is not None:
                try:
                    r = destroyer(media)
                    if hasattr(r, "__await__"):
                        await r
                except Exception as e:  # pragma: no cover
                    logger.warning("media_lifecycle: reject-rollback destroyer raised: %s", e)
            return {"outcome": "deleted", "decision": decision.explain()}
        if not _physical_delete_enabled():
            if collection_name and parent_id and mid:
                await mark_pending_deletion(
                    db, collection_name, parent_id, mid,
                    reason=decision.reason,
                    retention_days=decision.retention_days if decision.retention_days is not None else 0,
                    actor=ctx.actor, now=ctx.now,
                )
            logger.info("media_lifecycle: WOULD delete %s (%s) — physical delete disabled",
                        media.get("public_id") or media.get("stream_uid"), decision.reason)
            return {"outcome": "would_delete", "decision": decision.explain()}

        if destroyer is not None:
            try:
                res = destroyer(media)
                if hasattr(res, "__await__"):
                    await res
            except Exception as e:  # pragma: no cover - destroyer is best-effort
                logger.warning("media_lifecycle: destroyer raised for %s: %s",
                               media.get("public_id"), e)
        await _mark_deleted(db, collection_name, parent_id, mid, ctx.actor)
        return {"outcome": "deleted", "decision": decision.explain()}

    # not deletable
    if decision.owner.is_project_audition_media and collection_name and parent_id and mid:
        marked = await mark_pending_deletion(
            db, collection_name, parent_id, mid,
            reason=decision.reason,
            retention_days=decision.retention_days if decision.retention_days is not None else (
                await get_retention_days(db)
            ),
            actor=ctx.actor, now=ctx.now,
        )
        return {"outcome": "marked_pending", "marked": marked, "decision": decision.explain()}

    return {"outcome": "protected", "decision": decision.explain()}


# --------------------------------------------------------------------------
# Pending-deletion ledger  (db.pending_media_deletions)
#
# When the owning Mongo record is itself removed (project / submission delete),
# there is no media[i] left to carry a `lifecycle` sub-document — so the
# retention intent is recorded here instead. P8/P9's controlled purge reads this
# ledger; nothing in P6 acts on it.
# --------------------------------------------------------------------------
LEDGER = "pending_media_deletions"


async def enqueue_pending_deletion(
    db,
    media: Dict[str, Any],
    *,
    owner: Optional[OwnerInfo] = None,
    reason: str,
    retention_days: int,
    actor: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Idempotently record that this asset's backing storage object is a
    candidate for eventual purge. Keyed by public_id / stream_uid. NEVER deletes
    anything."""
    public_id = media.get("public_id")
    stream_uid = media.get("stream_uid")
    if not public_id and not stream_uid:
        return {"ok": False, "reason": "no backing asset"}
    owner = owner or classify_owner(media)
    ts = now or datetime.now(timezone.utc)
    key = {"public_id": public_id} if public_id else {"stream_uid": stream_uid}
    existing = await db[LEDGER].find_one(key, {"_id": 0, "marked_at": 1})
    if existing:
        return {"ok": True, "unchanged": True}
    eligible_at = None if retention_days == RETENTION_INDEFINITE else (
        ts + timedelta(days=max(retention_days, 0))
    ).isoformat()
    await db[LEDGER].insert_one({
        **key,
        "resource_type": media.get("resource_type"),
        "url": media.get("url"),
        "owner_type": owner.owner_type,
        "owner_id": owner.owner_id,
        "talent_id": owner.talent_id,
        "project_id": owner.project_id,
        "submission_id": owner.submission_id,
        "application_id": owner.application_id,
        "is_shared_copy": owner.is_shared_copy,
        "conflict": owner.conflict,
        "state": STATE_PENDING_DELETION,
        "reason": reason,
        "retention_days": retention_days,
        "marked_at": ts.isoformat(),
        "eligible_at": eligible_at,
        "marked_by": actor,
    })
    return {"ok": True, "unchanged": False, "eligible_at": eligible_at}


async def record_owner_teardown(
    db,
    media_items: List[Dict[str, Any]],
    *,
    context_kind: str,           # "project" | "submission"
    context_id: str,
    actor: Optional[str] = None,
    retention_days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Called when a project or submission is torn down. For each media item:

      * GLOBAL talent media           -> untouched, not enqueued (it lives on
        the talent and in other records)
      * PROJECT audition media        -> enqueued in the ledger with retention
      * UNKNOWN / conflicting          -> enqueued as PROTECTED-unknown (so P8
        surfaces it) but flagged so the purge never auto-acts on it
      * shared-by-value / still referenced elsewhere -> skipped (another record
        keeps it alive)
    """
    if retention_days is None:
        retention_days = await get_retention_days(db)
    summary = {"audition_enqueued": 0, "global_skipped": 0, "unknown_enqueued": 0,
              "still_referenced_skipped": 0, "no_asset": 0}
    ctx = DeletionContext(
        now=now, actor=actor,
        exclude_collection="submissions" if context_kind == "submission" else None,
        exclude_parent_id=context_id if context_kind == "submission" else None,
        **({"project_deletion": context_id} if context_kind == "project" else
           {"submission_deletion": context_id}),
    )
    for m in media_items or []:
        if not (m.get("public_id") or m.get("stream_uid")):
            summary["no_asset"] += 1
            continue
        owner = classify_owner(m)
        if owner.is_global_talent_media:
            summary["global_skipped"] += 1
            continue
        deps = await get_dependencies(db, m, ctx=ctx)
        if any(d.protects for d in deps):
            summary["still_referenced_skipped"] += 1
            continue
        if not owner.is_known:
            await enqueue_pending_deletion(
                db, m, owner=owner,
                reason=f"{context_kind} {context_id} deleted; ownership unknown — PROTECTED pending review",
                retention_days=RETENTION_INDEFINITE, actor=actor, now=now)
            summary["unknown_enqueued"] += 1
            continue
        await enqueue_pending_deletion(
            db, m, owner=owner,
            reason=f"{context_kind} {context_id} deleted; audition media enters retention",
            retention_days=retention_days, actor=actor, now=now)
        summary["audition_enqueued"] += 1
    return summary


# --------------------------------------------------------------------------
# Talent hard-delete dependency check
# --------------------------------------------------------------------------
async def talent_hard_delete_blockers(db, talent_id: str) -> List[Dict[str, Any]]:
    """Every reason a talent may NOT be hard-deleted. Empty list == safe to
    hard-delete (archive is still the normal path). Never raises — a failed
    probe is reported as a blocker so the caller errs towards ARCHIVE."""
    blockers: List[Dict[str, Any]] = []
    talent = await db.talents.find_one({"id": talent_id}, {"_id": 0, "email": 1, "normalized_email": 1, "media": 1, "name": 1})
    if not talent:
        return [{"kind": "not_found", "detail": "talent does not exist"}]

    emails = [e for e in {talent.get("email"), talent.get("normalized_email")} if e]

    async def _count(coll, q, label, kind):
        try:
            n = await coll.count_documents(q)
            if n:
                blockers.append({"kind": kind, "count": n, "detail": f"{n} {label}"})
        except Exception as e:
            blockers.append({"kind": kind, "detail": f"could not verify {label} ({e}) — blocking to be safe"})

    # submissions (historical record + active project participation)
    sub_q = {"talent_id": talent_id}
    await _count(db.submissions, {**sub_q, "$or": [{"deleted_at": {"$exists": False}}, {"deleted_at": None}]},
                "project submission(s) — historical record", "submissions")

    # applications tied to this talent's email
    if emails:
        await _count(db.applications, {"talent_email": {"$in": emails}}, "application(s)", "applications")

    # client review links referencing this talent
    await _count(db.links, {"talent_ids": talent_id}, "client review link(s)", "review_links")

    # casting pipeline entries
    await _count(db.casting_pipeline, {"talent_id": talent_id}, "casting pipeline entry(ies)", "casting_pipeline")

    # global media still owned
    media = talent.get("media") or []
    owned = [m for m in media if classify_owner(m).is_global_talent_media or classify_owner(m).owner_type is None]
    if owned:
        blockers.append({
            "kind": "global_media",
            "count": len(owned),
            "detail": f"{len(owned)} global media item(s) still owned by this talent",
        })

    return blockers
