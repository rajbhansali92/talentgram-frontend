"""Safe Talent Deduplication — Existing-Duplicate Scanner (READ-ONLY / DRY RUN).

Part 14-16 of the Talentgram "Safe Talent Deduplication & Profile Merge" spec.

STRICT CONTRACT — this module NEVER writes to the database:
  - No insert_one / update_one / update_many / delete_one / delete_many /
    replace_one anywhere in this file.
  - No Cloudinary/Stream calls.
  - No status changes, no `merged_into` writes, no `talent_merges` inserts.
  - Every DB call in this file is find / find_one / count_documents /
    aggregate (non-$out/$merge) / list_collection_names.
If a future phase adds a write-capable merge executor, it belongs in a
SEPARATE module (never this one) so this scanner can always be safely run
against production without a code review of "did someone sneak a write in."

Usage (from backend/):
    python -m migrations.talent_dedup_scan                # human-readable report to stdout
    python -m migrations.talent_dedup_scan --json-out FILE  # also write structured JSON
    python -m migrations.talent_dedup_scan --examples 5    # show N representative examples (default 5)

Matching hierarchy (Part 2/3 of the spec — conservative, never merge on name alone):
  Tier 1 (exact, strong):  normalized_email | normalized phone | normalized instagram_handle
  Tier 2 (strong combo):   normalized_name + dob | normalized_name + instagram_handle |
                            normalized_name + phone | normalized_name + normalized_email
  Tier 3 (weak, name-only): normalized_name alone, no other corroborating signal —
                            ALWAYS MANUAL_REVIEW, never eligible for SAFE_AUTO_MERGE,
                            reported as a structurally separate, lower-confidence class.

Classification:
  CONFLICT        — a strong identifier (email/phone/instagram/dob) differs, non-empty,
                     on both sides. Never merge. Never even attempted.
  MANUAL_REVIEW   — plausible duplicate, but not safe to decide automatically: a Tier 3
                     (name-only) match, a group of 3+ talents, or an ambiguous canonical
                     selection (see `_select_canonical`).
  SAFE_AUTO_MERGE — Tier 1/2 match, no conflicts, exactly 2 members, canonical selection
                     unambiguous. Still not executed here — this module only ever
                     calculates what WOULD happen (Part 7).
  UNIQUE          — talent participates in no candidate group at all.

Field precedence (Part 11/4, documented per field — see FIELD_POLICY_NOTES below).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db, REVIEW_FIELDS, AUTO_UPDATE_FIELDS, PRESERVE_FIELDS  # noqa: E402

REPORT_DIR = Path(__file__).parent / "reports"
REPORT_DIR.mkdir(exist_ok=True)

# Fields that are list-valued within AUTO_UPDATE_FIELDS -- these are UNION-merged
# (Part 8), never overwritten, unlike the scalar AUTO_UPDATE_FIELDS.
_LIST_AUTO_UPDATE_FIELDS = {"skills", "work_links", "interested_in", "languages"}
# `location` is excluded from AUTO_UPDATE_FIELDS handling entirely (Part 9/12):
# this scanner NEVER proposes changing a talent's location field, in either
# direction, during a dedup merge. A talent document's `location` field cannot
# be reliably distinguished here from a project-specific value that leaked in
# through an unrelated historical code path (a documented open question, see
# docs/ADR_CANONICAL_PROFILE_OWNERSHIP.md Part 1.2's `location` row) -- the
# only safe thing to do algorithmically is leave it alone and flag a mismatch
# for a human to look at directly, never guess.
_SCALAR_AUTO_UPDATE_FIELDS = (AUTO_UPDATE_FIELDS - _LIST_AUTO_UPDATE_FIELDS) - {"location"}

FIELD_POLICY_NOTES = {
    "name": "REVIEW: canonical's value is NEVER overwritten once set (Part 4's own wording -- "
            "\"keep talent value if equivalent\" -- risk of a typo/nickname silently replacing "
            "a verified name is not worth the aggressive-override policy used for the other "
            "REVIEW_FIELDS). Fills canonical only if canonical's name is currently empty "
            "(never happens in practice -- name is required at creation).",
    "dob": "REVIEW: if canonical/duplicate source types are (admin-or-legacy, submission) -- the "
           "exact scenario Part 4's height example describes -- the submission side's value wins "
           "when provided, else the admin/canonical value is kept. Otherwise (ambiguous which "
           "side is the fresher talent-provided value) falls back to fill-canonical-if-empty-only.",
    "gender": "Same policy as dob.",
    "height": "Same policy as dob (this is Part 4's explicit worked example).",
    "ethnicity": "Same policy as dob.",
    "instagram_handle": "AUTO_UPDATE: duplicate's value wins if non-empty and different -- an "
                         "Instagram handle is expected to change over time, the newer profile's "
                         "value is preferred regardless of source type.",
    "instagram_followers": "AUTO_UPDATE: duplicate's value wins if non-empty and different.",
    "bio": "AUTO_UPDATE: duplicate's value wins if non-empty and different.",
    "phone": "AUTO_UPDATE: duplicate's value wins if non-empty and different.",
    "alternate_contact_number": "AUTO_UPDATE: duplicate's value wins if non-empty and different.",
    "cover_media_id": "AUTO_UPDATE, but media identity does not survive a merge unchanged -- "
                       "recomputed fresh after any media-array change, never copied directly.",
    "needs_location_review": "AUTO_UPDATE: duplicate's value wins if non-empty and different.",
    "location": "PROTECTED -- see module docstring. Never auto-changed by this scanner in "
                "either direction. A mismatch is reported, never resolved.",
    "skills": "UNION: proposed = union(canonical.skills, duplicate.skills), de-duplicated.",
    "work_links": "UNION: proposed = union(canonical.work_links, duplicate.work_links), de-duplicated.",
    "interested_in": "UNION: proposed = union(canonical.interested_in, duplicate.interested_in).",
    "languages": "UNION: proposed = union(canonical.languages, duplicate.languages).",
    "tags": "UNION (Part 8's explicit worked example): proposed = union(canonical.tags, "
            "duplicate.tags), de-duplicated by tag id.",
    "notes": "CONCATENATE: canonical's notes are kept, duplicate's notes (if any, and if "
             "different) are appended with a clear provenance marker -- never silently dropped.",
    "email": "IDENTITY: canonical's email is kept if present; filled from duplicate only if "
             "canonical's is currently empty (Part 17 -- identity linking).",
}
for _f in PRESERVE_FIELDS - {"notes"}:
    FIELD_POLICY_NOTES.setdefault(
        _f, "PRESERVE: admin-only field, canonical's value is kept untouched; duplicate's "
            "value (if different) is never applied automatically.",
    )

# --------------------------------------------------------------------------
# Relationship collections -- every collection this scanner knows carries a
# talent_id-shaped reference, confirmed against the live schema (2026-08-17).
# Each entry: (collection_name, field_name_or_names). A "$in"-array field
# (talent_ids / seen_talent_ids) is counted differently from a scalar field.
# --------------------------------------------------------------------------
_SCALAR_TALENT_REF_COLLECTIONS = [
    ("submissions", "talent_id"),
    ("applications", "talent_id"),
    ("casting_pipeline", "talent_id"),
    ("link_action_history", "talent_id"),
    ("link_events", "talent_id"),
    ("whatsapp_jobs", "talent_id"),
    ("whatsapp_audit_log", "talent_id"),
    ("asset_metadata", "talent_id"),
    ("storage_audit_log", "talent_id"),
]
_ARRAY_TALENT_REF_COLLECTIONS = [
    ("links", "talent_ids"),
    ("client_states", "seen_talent_ids"),
]
# talent_migration_candidates references BOTH a legacy and an authenticated
# talent id under different field names -- counted separately, not folded
# into the generic scalar list above, so a report can distinguish "this
# talent IS a recorded migration candidate" from the other relationship types.


def _norm(v: Any) -> Optional[str]:
    return v.strip().lower() if isinstance(v, str) and v.strip() else None


def _norm_name(v: Any) -> Optional[str]:
    n = _norm(v)
    return " ".join(n.split()) if n else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Step 1 — load + index talents
# --------------------------------------------------------------------------
_TALENT_PROJECTION = {
    "_id": 0, "id": 1, "name": 1, "email": 1, "normalized_email": 1, "phone": 1,
    "instagram_handle": 1, "instagram_followers": 1, "dob": 1, "gender": 1,
    "height": 1, "ethnicity": 1, "bio": 1, "location": 1, "skills": 1,
    "work_links": 1, "interested_in": 1, "languages": 1, "alternate_contact_number": 1,
    "tags": 1, "notes": 1, "status": 1, "source": 1, "media": 1, "created_at": 1,
    "updated_at": 1, "cover_media_id": 1, "needs_location_review": 1,
}


async def _load_talents() -> List[dict]:
    return await db.talents.find({}, _TALENT_PROJECTION).to_list(length=None)


class _UnionFind:
    def __init__(self, ids: List[str]):
        self.parent = {i: i for i in ids}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _build_tier12_groups(talents: List[dict]) -> Tuple[Dict[str, List[str]], Dict[Tuple[str, str], Set[str]]]:
    """Returns (component_id -> [talent_ids], (talent_a,talent_b) -> {matched_field,...}).
    Pair evidence is keyed by a SORTED tuple so lookups don't care about order."""
    by_id = {t["id"]: t for t in talents}
    uf = _UnionFind(list(by_id.keys()))
    pair_evidence: Dict[Tuple[str, str], Set[str]] = defaultdict(set)

    def _record_key_group(key_label: str, buckets: Dict[Any, List[str]]) -> None:
        for _, ids in buckets.items():
            if len(ids) < 2:
                continue
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    a, b = sorted((ids[i], ids[j]))
                    pair_evidence[(a, b)].add(key_label)
                    uf.union(a, b)

    # Tier 1
    by_email: Dict[str, List[str]] = defaultdict(list)
    by_phone: Dict[str, List[str]] = defaultdict(list)
    by_insta: Dict[str, List[str]] = defaultdict(list)
    for t in talents:
        e = _norm(t.get("normalized_email") or t.get("email"))
        p = _norm(t.get("phone"))
        ig = _norm(t.get("instagram_handle"))
        if e:
            by_email[e].append(t["id"])
        if p:
            by_phone[p].append(t["id"])
        if ig:
            by_insta[ig].append(t["id"])
    _record_key_group("email", by_email)
    _record_key_group("phone", by_phone)
    _record_key_group("instagram_handle", by_insta)

    # Tier 2 -- normalized_name combined with a second signal
    by_name_dob: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    by_name_insta: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    by_name_phone: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    by_name_email: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for t in talents:
        name = _norm_name(t.get("name"))
        if not name:
            continue
        dob = _norm(t.get("dob"))
        ig = _norm(t.get("instagram_handle"))
        p = _norm(t.get("phone"))
        e = _norm(t.get("normalized_email") or t.get("email"))
        if dob:
            by_name_dob[(name, dob)].append(t["id"])
        if ig:
            by_name_insta[(name, ig)].append(t["id"])
        if p:
            by_name_phone[(name, p)].append(t["id"])
        if e:
            by_name_email[(name, e)].append(t["id"])
    _record_key_group("name+dob", by_name_dob)
    _record_key_group("name+instagram_handle", by_name_insta)
    _record_key_group("name+phone", by_name_phone)
    _record_key_group("name+email", by_name_email)

    components: Dict[str, List[str]] = defaultdict(list)
    for tid in by_id:
        components[uf.find(tid)].append(tid)
    groups = {root: ids for root, ids in components.items() if len(ids) >= 2}
    return groups, pair_evidence


# A shared exact name alone is O(n^2) risk to itemize pairwise once n grows
# (common names, or -- as observed against this environment's local dev DB --
# QA fixture data reusing generic names like "Test Test" hundreds of times).
# Tier 3 is NEVER eligible for auto-merge regardless of how many people share
# a name, so there is no analytical value in enumerating every pair; a group
# above this size is reported as ONE cluster entry instead.
_TIER3_MAX_GROUP_SIZE = 8


def _build_tier3_groups(talents: List[dict], tier12_groups: Dict[str, List[str]]) -> List[List[str]]:
    """Talents sharing an exact normalized name, grouped as whole clusters
    (not exhaustive pairs). A cluster already fully contained within a single
    Tier 1/2 component is skipped -- it's already reported there, with a
    stronger signal, and doesn't need a redundant weaker entry."""
    tier12_component_of: Dict[str, int] = {}
    for idx, ids in enumerate(tier12_groups.values()):
        for tid in ids:
            tier12_component_of[tid] = idx

    by_name: Dict[str, List[str]] = defaultdict(list)
    for t in talents:
        n = _norm_name(t.get("name"))
        if n:
            by_name[n].append(t["id"])

    groups: List[List[str]] = []
    for _, ids in by_name.items():
        if len(ids) < 2:
            continue
        components = {tier12_component_of.get(tid, f"solo:{tid}") for tid in ids}
        if len(components) == 1 and all(tid in tier12_component_of for tid in ids):
            continue  # fully redundant with an existing, stronger Tier 1/2 group
        groups.append(ids)
    return groups


# --------------------------------------------------------------------------
# Step 2 — conflict detection (Part 2's CRITICAL SAFETY RULE)
# --------------------------------------------------------------------------
def _pairwise_conflict(a: dict, b: dict) -> Optional[str]:
    conflicts = []
    for field, av, bv in (
        ("email", a.get("normalized_email") or a.get("email"), b.get("normalized_email") or b.get("email")),
        ("phone", a.get("phone"), b.get("phone")),
        ("instagram_handle", a.get("instagram_handle"), b.get("instagram_handle")),
        ("dob", a.get("dob"), b.get("dob")),
    ):
        an, bn = _norm(av), _norm(bv)
        if an and bn and an != bn:
            conflicts.append(field)
    return ",".join(conflicts) if conflicts else None


def _group_conflicts(members: List[dict]) -> List[str]:
    found = []
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            c = _pairwise_conflict(members[i], members[j])
            if c:
                found.append(f"{members[i]['id']}<->{members[j]['id']}: {c}")
    return found


# --------------------------------------------------------------------------
# Step 3 — canonical selection (Part 6 -- NEVER assume oldest = canonical)
# --------------------------------------------------------------------------
_COMPLETENESS_FIELDS = [
    "name", "email", "phone", "instagram_handle", "dob", "gender", "height",
    "ethnicity", "bio", "location", "skills", "work_links",
]


def _completeness_score(t: dict) -> int:
    score = 0
    for f in _COMPLETENESS_FIELDS:
        v = t.get(f)
        if isinstance(v, (list, dict)):
            score += 1 if v else 0
        elif v not in (None, ""):
            score += 1
    return score


def _profile_strength(t: dict, submissions: int, media: int) -> float:
    src = ((t.get("source") or {}).get("type")) if isinstance(t.get("source"), dict) else t.get("source")
    established_bonus = 1000 if src != "audition_submission" else 0
    return (
        established_bonus
        + _completeness_score(t) * 10
        + submissions * 5
        + media * 3
        + (1 if t.get("email") else 0)
        + (1 if t.get("phone") else 0)
    )


def _select_canonical(members: List[dict], submission_counts: Dict[str, int], media_counts: Dict[str, int]) -> Tuple[Optional[dict], Optional[dict], bool, str]:
    """Returns (canonical, duplicate, ambiguous, reason). Only meaningful for
    exactly 2 members -- callers must route 3+ member groups to MANUAL_REVIEW
    before calling this."""
    a, b = members[0], members[1]
    score_a = _profile_strength(a, submission_counts.get(a["id"], 0), media_counts.get(a["id"], 0))
    score_b = _profile_strength(b, submission_counts.get(b["id"], 0), media_counts.get(b["id"], 0))
    if score_a == score_b:
        # Final tiebreaker only: older record. Never the FIRST criterion (Part 6).
        a_created = a.get("created_at") or ""
        b_created = b.get("created_at") or ""
        if a_created and b_created and a_created != b_created:
            canonical, duplicate = (a, b) if a_created < b_created else (b, a)
            return canonical, duplicate, False, "scores tied; older record chosen as final tiebreaker"
        return None, None, True, f"scores tied ({score_a:.1f}) and no distinguishing created_at -- cannot pick a canonical automatically"
    margin = abs(score_a - score_b)
    denom = max(score_a, score_b, 1)
    if margin / denom < 0.08:
        return None, None, True, f"scores too close to call ({score_a:.1f} vs {score_b:.1f}, {margin/denom:.1%} margin)"
    canonical, duplicate = (a, b) if score_a > score_b else (b, a)
    return canonical, duplicate, False, f"canonical scored {max(score_a, score_b):.1f} vs duplicate's {min(score_a, score_b):.1f}"


# --------------------------------------------------------------------------
# Step 4 — relationship counting (READ-ONLY count_documents only)
# --------------------------------------------------------------------------
async def _relationship_counts(talent_id: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for coll, field in _SCALAR_TALENT_REF_COLLECTIONS:
        counts[coll] = await db[coll].count_documents({field: talent_id})
    for coll, field in _ARRAY_TALENT_REF_COLLECTIONS:
        counts[coll] = await db[coll].count_documents({field: talent_id})
    counts["talent_migration_candidates_as_legacy"] = await db.talent_migration_candidates.count_documents(
        {"legacy_talent_id": talent_id}
    )
    counts["talent_migration_candidates_as_authenticated"] = await db.talent_migration_candidates.count_documents(
        {"authenticated_talent_id": talent_id}
    )
    return counts


# --------------------------------------------------------------------------
# Step 5 — media analysis (Part 9) -- reusable Library media only; submission/
# application-scoped media is a separate, already-independent collection and
# is NEVER touched by a talent-level merge (Part 7's "reference-not-duplicate"
# rule -- only the submission's `talent_id` pointer would eventually change,
# never its own embedded `media[]`).
# --------------------------------------------------------------------------
def _media_analysis(canonical: dict, duplicate: dict) -> Dict[str, Any]:
    c_media = canonical.get("media") or []
    d_media = duplicate.get("media") or []
    c_ids = {m.get("public_id") or m.get("id") for m in c_media}
    overlap = sum(1 for m in d_media if (m.get("public_id") or m.get("id")) in c_ids)
    by_category: Dict[str, int] = defaultdict(int)
    for m in d_media:
        by_category[m.get("category") or "unknown"] += 1
    return {
        "canonical_media_count": len(c_media),
        "duplicate_media_count": len(d_media),
        "duplicate_media_already_present_on_canonical": overlap,
        "proposed_canonical_media_count_after_merge": len(c_media) + len(d_media) - overlap,
        "duplicate_media_by_category": dict(by_category),
        "note": (
            "Only db.talents.media (the reusable Library) is analysed here. "
            "Submission-scoped and application-scoped media are separate, "
            "self-contained embedded arrays that would be UNTOUCHED by a merge "
            "-- only the parent submission/application's talent_id pointer "
            "would eventually change. No Cloudinary/Stream asset would ever "
            "be duplicated or deleted by this analysis or by a future merge "
            "built on it."
        ),
    }


# --------------------------------------------------------------------------
# Step 6 — proposed field changes (Part 4/11 -- calculated, never applied)
# --------------------------------------------------------------------------
def _is_empty(v: Any) -> bool:
    return v in (None, "", [], {})


def _proposed_field_changes(canonical: dict, duplicate: dict) -> Dict[str, Dict[str, Any]]:
    changes: Dict[str, Dict[str, Any]] = {}
    c_src = ((canonical.get("source") or {}).get("type")) if isinstance(canonical.get("source"), dict) else canonical.get("source")
    d_src = ((duplicate.get("source") or {}).get("type")) if isinstance(duplicate.get("source"), dict) else duplicate.get("source")
    aggressive_review_precedence = (c_src != "audition_submission") and (d_src == "audition_submission")

    for field in REVIEW_FIELDS:
        c_val, d_val = canonical.get(field), duplicate.get(field)
        if field == "name":
            proposed = c_val if not _is_empty(c_val) else d_val
        elif aggressive_review_precedence and not _is_empty(d_val):
            proposed = d_val
        elif _is_empty(c_val) and not _is_empty(d_val):
            proposed = d_val
        else:
            proposed = c_val
        if proposed != c_val:
            changes[field] = {"canonical": c_val, "duplicate": d_val, "proposed": proposed, "policy": FIELD_POLICY_NOTES.get(field)}

    for field in _SCALAR_AUTO_UPDATE_FIELDS:
        c_val, d_val = canonical.get(field), duplicate.get(field)
        proposed = d_val if not _is_empty(d_val) and d_val != c_val else c_val
        if proposed != c_val:
            changes[field] = {"canonical": c_val, "duplicate": d_val, "proposed": proposed, "policy": FIELD_POLICY_NOTES.get(field)}

    for field in _LIST_AUTO_UPDATE_FIELDS:
        c_list = canonical.get(field) or []
        d_list = duplicate.get(field) or []
        union = list(dict.fromkeys(list(c_list) + [x for x in d_list if x not in c_list]))
        if union != c_list:
            changes[field] = {"canonical": c_list, "duplicate": d_list, "proposed": union, "policy": FIELD_POLICY_NOTES.get(field)}

    c_tags = canonical.get("tags") or []
    d_tags = duplicate.get("tags") or []
    c_tag_ids = {t.get("id") for t in c_tags if isinstance(t, dict)}
    union_tags = list(c_tags) + [t for t in d_tags if isinstance(t, dict) and t.get("id") not in c_tag_ids]
    if union_tags != c_tags:
        changes["tags"] = {"canonical": c_tags, "duplicate": d_tags, "proposed": union_tags, "policy": FIELD_POLICY_NOTES["tags"]}

    c_notes, d_notes = (canonical.get("notes") or ""), (duplicate.get("notes") or "")
    if d_notes and d_notes.strip() not in c_notes:
        proposed_notes = (c_notes + ("\n\n" if c_notes else "") + f"[merged from duplicate {duplicate['id']}]: {d_notes}").strip()
        changes["notes"] = {"canonical": c_notes, "duplicate": d_notes, "proposed": proposed_notes, "policy": FIELD_POLICY_NOTES["notes"]}

    if _is_empty(canonical.get("email")) and not _is_empty(duplicate.get("email")):
        changes["email"] = {"canonical": canonical.get("email"), "duplicate": duplicate.get("email"), "proposed": duplicate.get("email"), "policy": FIELD_POLICY_NOTES["email"]}
        changes["normalized_email"] = {"canonical": canonical.get("normalized_email"), "duplicate": duplicate.get("normalized_email"), "proposed": duplicate.get("normalized_email"), "policy": FIELD_POLICY_NOTES["email"]}

    c_loc, d_loc = canonical.get("location"), duplicate.get("location")
    if c_loc != d_loc:
        changes["location"] = {
            "canonical": c_loc, "duplicate": d_loc, "proposed": c_loc,  # NEVER auto-changed
            "policy": FIELD_POLICY_NOTES["location"],
            "flag": "MISMATCH -- not auto-resolved, requires manual confirmation",
        }
    return changes


# --------------------------------------------------------------------------
# Step 7 — assemble one candidate group's full report entry
# --------------------------------------------------------------------------
def _talent_summary(t: dict) -> Dict[str, Any]:
    src = t.get("source")
    src_type = (src or {}).get("type") if isinstance(src, dict) else src
    return {
        "talent_id": t["id"],
        "name": t.get("name"),
        "email": t.get("email"),
        "phone": t.get("phone"),
        "instagram_handle": t.get("instagram_handle"),
        "source": src_type,
        "status": t.get("status"),
        "created_at": t.get("created_at"),
    }


async def _build_group_report(
    tier: str,
    member_ids: List[str],
    by_id: Dict[str, dict],
    matched_by: Set[str],
) -> Dict[str, Any]:
    members = [by_id[i] for i in member_ids]

    if tier == "3":
        # Tier 3 is NEVER eligible for auto-merge regardless of conflict
        # status (the spec draws no CONFLICT/MANUAL_REVIEW distinction within
        # Tier 3 -- a name match alone was never going to be auto-merged
        # either way), so the O(n^2) pairwise conflict scan is skipped here.
        # Large clusters (common names, or QA-fixture noise) are truncated
        # for readability rather than dumped in full.
        truncated = len(members) > _TIER3_MAX_GROUP_SIZE
        shown = members[:_TIER3_MAX_GROUP_SIZE]
        entry: Dict[str, Any] = {
            "tier": tier,
            "members": [_talent_summary(m) for m in shown],
            "member_count": len(members),
            "truncated": truncated,
            "matched_by": sorted(matched_by),
            "conflicts": [],
            "classification": "MANUAL_REVIEW",
            "reason": (
                f"Tier 3 (name-only) match across {len(members)} talents -- never eligible "
                "for auto-merge regardless of confidence. Likely a common name or reused "
                "test-fixture data if the count is large; a genuine duplicate if small."
            ),
        }
        return entry

    conflicts = _group_conflicts(members)
    entry = {
        "tier": tier,
        "members": [_talent_summary(m) for m in members],
        "matched_by": sorted(matched_by),
        "conflicts": conflicts,
    }

    if conflicts:
        entry["classification"] = "CONFLICT"
        entry["reason"] = "conflicting strong identifier(s) between at least one pair -- never auto-merge"
        return entry

    if len(members) > 2:
        entry["classification"] = "MANUAL_REVIEW"
        entry["reason"] = f"group has {len(members)} members -- 3+-way clusters are always routed to manual review"
        return entry

    rel_counts = {m["id"]: await _relationship_counts(m["id"]) for m in members}
    submission_counts = {tid: c["submissions"] for tid, c in rel_counts.items()}
    media_counts = {tid: len(by_id[tid].get("media") or []) for tid in member_ids}

    canonical, duplicate, ambiguous, reason = _select_canonical(members, submission_counts, media_counts)
    entry["canonical_selection_reason"] = reason
    if ambiguous:
        entry["classification"] = "MANUAL_REVIEW"
        entry["reason"] = f"ambiguous canonical selection: {reason}"
        return entry

    entry["classification"] = "SAFE_AUTO_MERGE"
    entry["canonical"] = _talent_summary(canonical)
    entry["duplicate"] = _talent_summary(duplicate)
    entry["relationship_counts"] = {
        "canonical": rel_counts[canonical["id"]],
        "duplicate": rel_counts[duplicate["id"]],
        "proposed_total_submissions": rel_counts[canonical["id"]]["submissions"] + rel_counts[duplicate["id"]]["submissions"],
    }
    entry["media"] = _media_analysis(canonical, duplicate)
    entry["proposed_field_changes"] = _proposed_field_changes(canonical, duplicate)
    entry["proposed_status_change"] = {
        "duplicate_talent_id": duplicate["id"],
        "duplicate_status": "MERGED",
        "duplicate_merged_into": canonical["id"],
        "canonical_talent_id": canonical["id"],
        "canonical_status": "unchanged",
    }
    entry["proposed_merge_audit_record_shape"] = {
        "source_talent_id": duplicate["id"],
        "canonical_talent_id": canonical["id"],
        "matched_by": sorted(matched_by),
        "classification": "SAFE_AUTO_MERGE",
        "field_changes": list(entry["proposed_field_changes"].keys()),
        "relationship_counts": entry["relationship_counts"],
        "timestamp": None,
        "migration_version": "talent_dedup_scan_v1",
        "operator": None,
    }
    return entry


# --------------------------------------------------------------------------
# Step 8 — top-level scan
# --------------------------------------------------------------------------
async def run_scan(*, example_limit: int = 5) -> Dict[str, Any]:
    talents = await _load_talents()
    by_id = {t["id"]: t for t in talents}
    total = len(talents)

    tier12_groups, pair_evidence = _build_tier12_groups(talents)
    tier3_groups = _build_tier3_groups(talents, tier12_groups)

    group_reports: List[Dict[str, Any]] = []
    grouped_talent_ids: Set[str] = set()

    for member_ids in tier12_groups.values():
        matched_by: Set[str] = set()
        for i in range(len(member_ids)):
            for j in range(i + 1, len(member_ids)):
                pair = tuple(sorted((member_ids[i], member_ids[j])))
                matched_by |= pair_evidence.get(pair, set())
        report = await _build_group_report("1/2", member_ids, by_id, matched_by)
        group_reports.append(report)
        grouped_talent_ids.update(member_ids)

    for member_ids in tier3_groups:
        report = await _build_group_report("3", member_ids, by_id, {"name"})
        group_reports.append(report)
        grouped_talent_ids.update(member_ids)

    unique_count = total - len(grouped_talent_ids)

    counts_by_class = defaultdict(int)
    for g in group_reports:
        counts_by_class[g["classification"]] += 1

    submissions_affected = 0
    media_affected = 0
    for g in group_reports:
        if g["classification"] == "SAFE_AUTO_MERGE":
            submissions_affected += g["relationship_counts"]["duplicate"]["submissions"]
            media_affected += g["media"]["duplicate_media_count"]

    group_reports.sort(key=lambda g: {"CONFLICT": 0, "SAFE_AUTO_MERGE": 1, "MANUAL_REVIEW": 2}.get(g["classification"], 3))

    summary = {
        "generated_at": _now_iso(),
        "total_talents_scanned": total,
        "unique_talents": unique_count,
        "candidate_groups": len(group_reports),
        "safe_auto_merge": counts_by_class["SAFE_AUTO_MERGE"],
        "manual_review": counts_by_class["MANUAL_REVIEW"],
        "conflict": counts_by_class["CONFLICT"],
        "potential_submissions_affected": submissions_affected,
        "potential_media_affected": media_affected,
        "mode": "READ_ONLY_DRY_RUN -- no writes of any kind were performed",
    }
    return {
        "summary": summary,
        "groups": group_reports,
        "examples": group_reports[:example_limit],
        "field_policy_notes": FIELD_POLICY_NOTES,
    }


# --------------------------------------------------------------------------
# Human-readable text report
# --------------------------------------------------------------------------
def _fmt_talent_block(label: str, t: Dict[str, Any], indent: str = "  ") -> List[str]:
    lines = [f"{indent}{label}:"]
    for k in ("talent_id", "name", "email", "phone", "instagram_handle", "source", "status", "created_at"):
        lines.append(f"{indent}  {k}: {t.get(k)}")
    return lines


def render_text_report(result: Dict[str, Any], example_limit: int) -> str:
    s = result["summary"]
    lines = [
        "EXISTING TALENT DEDUPLICATION DRY RUN",
        "=" * 60,
        f"Generated: {s['generated_at']}",
        f"Mode: {s['mode']}",
        "",
        f"Total talents scanned: {s['total_talents_scanned']}",
        f"Unique (no candidate group): {s['unique_talents']}",
        "",
        f"Potential duplicate groups: {s['candidate_groups']}",
        f"  SAFE_AUTO_MERGE: {s['safe_auto_merge']}",
        f"  MANUAL_REVIEW:   {s['manual_review']}",
        f"  CONFLICT:        {s['conflict']}",
        "",
        f"Potential submissions affected: {s['potential_submissions_affected']}",
        f"Potential media affected: {s['potential_media_affected']}",
        "",
        "-" * 60,
        f"Representative examples (first {example_limit}, ranked CONFLICT > SAFE_AUTO_MERGE > MANUAL_REVIEW):",
        "-" * 60,
    ]
    for g in result["examples"]:
        lines.append("")
        lines.append(f"[{g['classification']}] tier={g['tier']} matched_by={','.join(g['matched_by']) or '-'}")
        if g["classification"] == "SAFE_AUTO_MERGE":
            lines += _fmt_talent_block("Canonical candidate", g["canonical"])
            lines += _fmt_talent_block("Duplicate candidate", g["duplicate"])
            lines.append(f"  Canonical selection: {g['canonical_selection_reason']}")
            rc = g["relationship_counts"]
            lines.append(f"  Submissions: canonical={rc['canonical']['submissions']} duplicate={rc['duplicate']['submissions']} proposed_total={rc['proposed_total_submissions']}")
            m = g["media"]
            lines.append(f"  Media: canonical={m['canonical_media_count']} duplicate={m['duplicate_media_count']} overlap={m['duplicate_media_already_present_on_canonical']} proposed_total={m['proposed_canonical_media_count_after_merge']}")
            lines.append(f"  Proposed field changes: {', '.join(g['proposed_field_changes'].keys()) or '(none)'}")
            for field, ch in g["proposed_field_changes"].items():
                flag = f"  [{ch['flag']}]" if "flag" in ch else ""
                lines.append(f"    {field}: canonical={ch['canonical']!r} duplicate={ch['duplicate']!r} -> proposed={ch['proposed']!r}{flag}")
            lines.append(f"  Status change: duplicate {g['duplicate']['talent_id']} -> MERGED, merged_into={g['canonical']['talent_id']}")
        else:
            if g.get("truncated"):
                lines.append(f"  ({g['member_count']} members total, showing first {len(g['members'])})")
            for m in g["members"]:
                lines += _fmt_talent_block("Member", m)
            lines.append(f"  Reason: {g['reason']}")
            if g["conflicts"]:
                lines.append(f"  Conflicts: {'; '.join(g['conflicts'])}")
    lines.append("")
    lines.append("=" * 60)
    lines.append("NO DATA WAS MODIFIED. This is a read-only dry run.")
    return "\n".join(lines)


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Safe Talent Deduplication -- read-only duplicate scanner")
    parser.add_argument("--json-out", type=str, default=None, help="Also write the full structured report to this JSON file")
    parser.add_argument("--examples", type=int, default=5, help="Number of representative examples to print (default 5)")
    args = parser.parse_args()

    result = await run_scan(example_limit=args.examples)
    print(render_text_report(result, args.examples))

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.write_text(json.dumps(result, indent=2, default=str))
        print(f"\nFull structured report written to {out_path}")


if __name__ == "__main__":
    asyncio.run(_main())
