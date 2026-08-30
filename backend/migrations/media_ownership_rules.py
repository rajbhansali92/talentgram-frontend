"""Pure media-ownership classification rules (Cloudinary rearchitecture, P3).

This module has **no** imports from ``core``, and does **no** database access,
Cloudinary calls, environment reads, filesystem writes, network calls, or
side-effecting logging. Importing it is free of side effects.

``classify_item()`` is a pure function:
  * same inputs  ->  same output  (pass ``now=`` to pin the timestamp stamp;
    ``migration_version`` is a constant, so with ``now`` fixed the result is
    fully deterministic),
  * it never mutates any argument.

It decides ownership from **application state only** (the media ``category`` and
the parent document / resolved references). It never parses the Cloudinary
folder path — see ``folder_disagrees()`` for the separate, after-the-fact
"folder said X but the DB said Y" *report* (folder location can never override
authoritative ownership).

Used by:
  * ``backend/migrations/p3_media_ownership.py`` — the one-time backfill.
  * (future) P6 deletion / retention logic.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

VERSION = "p3-v1"

# Audition takes — the only project-owned category. Everything else is
# talent-owned (the talent owns the canonical asset even when a submission
# holds a value-copy).
TAKE_CATEGORIES = {"take", "take_1", "take_2", "take_3"}

# Non-standard categories seen only on ``scope=whatsapp_media_assignment``
# items — normalized, and flagged ``confidence: "medium"``.
CATEGORY_NORMALIZE = {"photos": "image", "intro": "intro_video"}

CATEGORY_TO_NORMALIZED = {
    "portfolio": "portfolio", "image": "portfolio", "photos": "portfolio",
    "additional_portfolio": "portfolio", "profiles": "portfolio", "selfie": "portfolio",
    "full_length": "portfolio", "side_profile": "portfolio", "ethnic": "portfolio",
    "headshot": "portfolio", "headshots": "portfolio", "profile_image": "portfolio",
    "indian": "indian", "western": "western",
    "video": "intro_video", "intro_video": "intro_video", "intro": "intro_video",
    "portfolio_video": "intro_video", "portfolio_videos": "intro_video",
    "take": "take", "take_1": "take", "take_2": "take", "take_3": "take",
}

# ``how_tid`` values (from the migration's ``_resolve_talent_id``) that indicate
# the talent_id was resolved indirectly rather than read straight off the
# item/parent — these demote ``confidence`` from "high" to "medium".
MEDIUM_CONFIDENCE_RESOLUTIONS = {"source_submission", "source_submission_email", "talent_email"}
# Back-compat alias for the pre-refactor private name.
_MEDIUM_CONFIDENCE_RESOLUTIONS = MEDIUM_CONFIDENCE_RESOLUTIONS

# owner_type values classify_item may assign.
OWNER_TYPE_TALENT = "talent"
OWNER_TYPE_PROJECT_SUBMISSION = "project_submission"


def classify_item(coll, parent, item, talent_id, how_tid,
                  pid_owner_count, pid_norm_cats, *, now=None):
    """Return the ``ownership`` sub-document for one media item. PURE.

    Args:
      coll: ``"talents"`` | ``"submissions"`` | ``"applications"`` — which
        collection the ``parent`` document lives in.
      parent: the parent document (reads ``id``, ``project_id`` only; the
        talent_id is resolved by the caller and passed in as ``talent_id``).
      item: the media item (reads ``category``, ``public_id``,
        ``resource_type``, ``content_type``, ``submission_id``, ``asset_id``,
        ``format``, ``size``/``bytes``, ``source_talent_media_id``).
      talent_id: the resolved talent id, or ``None`` (resolved by the caller).
      how_tid: string describing HOW ``talent_id`` was resolved — only used to
        set ``confidence`` (see ``MEDIUM_CONFIDENCE_RESOLUTIONS``).
      pid_owner_count: ``Counter`` mapping ``public_id`` -> number of media
        items referencing it (for ``is_shared_copy``).
      pid_norm_cats: mapping ``public_id`` -> ``set`` of normalized categories
        seen for that public_id (for the conflicting-category check).
      now: optional ISO-8601 string to use for the ``migrated_at`` stamp;
        when omitted a live UTC timestamp is used. Pass a fixed value for
        deterministic tests. Does not affect any classification.

    Returns:
      dict — the ``ownership`` sub-document. ``owner_type`` is one of
      ``"talent"`` / ``"project_submission"`` / ``None``. It is ``None`` iff
      ``conflict`` is non-``None`` — the caller must treat those as UNKNOWN and
      must not write an owner for them.

    Precedence (authoritative, derived from application state — folder is
    never consulted):
      1. Pre-conditions that force a conflict, checked in this order:
         a. an audition-take ``category`` on a non-``submissions`` document,
         b. an unrecognised ``category``,
         c. the same ``public_id`` carrying >1 normalized category.
      2. Otherwise ``category`` decides ``owner_type``:
         * take* -> ``project_submission`` (owner_id = the submission id);
           if the submission has no ``project_id`` -> conflict.
         * anything else -> ``talent`` (owner_id = the resolved talent_id);
           if no talent_id resolves -> conflict.
      Reference availability (talent_id / project_id) can only DEMOTE a
      classification to a conflict — it never changes ``owner_type``, and it
      is never inferred from unrelated fields.
    """
    cat = item.get("category")
    norm_cat = CATEGORY_TO_NORMALIZED.get(cat)
    pid = item.get("public_id")
    rtype = item.get("resource_type") or (
        "video" if (item.get("content_type") or "").startswith("video/") else "image"
    )
    is_take = cat in TAKE_CATEGORIES

    conflict = None
    owner_type = owner_id = owner_source = None
    confidence = "high"

    if is_take and coll != "submissions":
        conflict = f"take-category item on {coll} (only submissions may own audition takes)"
    elif norm_cat is None:
        conflict = f"unrecognised category {cat!r}"
    elif len(pid_norm_cats.get(pid, set())) > 1:
        conflict = f"public_id {pid} carries conflicting normalized categories {sorted(pid_norm_cats[pid])}"

    if conflict is None:
        if is_take:
            owner_type = OWNER_TYPE_PROJECT_SUBMISSION
            owner_id = parent.get("id")
            owner_source = "category:take"
            if not parent.get("project_id"):
                conflict = "take item on a submission with no project_id"
        else:
            owner_type = OWNER_TYPE_TALENT
            owner_id = talent_id
            owner_source = "category:global(normalized)" if cat in CATEGORY_NORMALIZE else "category:global"
            if not talent_id:
                conflict = "talent-owned item with no resolvable talent_id"
            elif how_tid in MEDIUM_CONFIDENCE_RESOLUTIONS:
                confidence = "medium"
    if cat in CATEGORY_NORMALIZE and confidence == "high":
        confidence = "medium"

    if conflict is not None:
        owner_type = owner_id = owner_source = None

    is_shared = bool(item.get("source_talent_media_id")) or (bool(pid) and pid_owner_count.get(pid, 0) > 1)

    return {
        "owner_type": owner_type,
        "owner_id": owner_id,
        "talent_id": talent_id,
        "project_id": parent.get("project_id") if owner_type == OWNER_TYPE_PROJECT_SUBMISSION else None,
        "submission_id": parent.get("id") if coll == "submissions" else item.get("submission_id"),
        "application_id": parent.get("id") if coll == "applications" else None,
        "media_type": "video" if rtype == "video" else "image",
        "media_category_normalized": norm_cat,
        "cloudinary": {
            "public_id": pid,
            "asset_id": item.get("asset_id"),   # null-safe; a later pass can enrich from Cloudinary
            "resource_type": rtype,
            "format": item.get("format"),
            "bytes": item.get("size") or item.get("bytes"),
        },
        "is_shared_copy": is_shared,
        "source_talent_media_id": item.get("source_talent_media_id"),
        "owner_source": owner_source,
        "confidence": confidence if conflict is None else None,
        "conflict": conflict,
        "migrated_at": now or datetime.now(timezone.utc).isoformat(),
        "migration_version": VERSION,
    }


def folder_disagrees(ownership):
    """Report (do not enforce) a Cloudinary-folder vs authoritative-DB-owner
    mismatch. ``classify_item`` never reads the folder path; this exists only so
    the migration can COUNT how often folder location would have given a
    different answer than the DB (it always defers to the DB).
    """
    pid = (ownership.get("cloudinary") or {}).get("public_id") or ""
    if not pid.startswith("talentgram/") or pid.count("/") < 1:
        return None
    folder_kind = pid.split("/")[1]
    ot = ownership.get("owner_type")
    if ot == OWNER_TYPE_TALENT and folder_kind in ("projects", "admin_media", "submissions", "applications") \
            and "/talents/" not in pid:
        return {"public_id": pid, "db_owner": OWNER_TYPE_TALENT, "folder": folder_kind}
    if ot == OWNER_TYPE_PROJECT_SUBMISSION and folder_kind == "talents":
        return {"public_id": pid, "db_owner": OWNER_TYPE_PROJECT_SUBMISSION, "folder": "talents"}
    return None
