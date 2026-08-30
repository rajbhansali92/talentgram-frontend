"""Media-Assignment mechanism (Phase 1, 2026-08-22) — turns an
`@Gunwanti + mark <project> <role>` WhatsApp reply, made inside a talent's
own group, into an exact attachment on the correct Talentgram submission.

Built on Phase 0's proven mechanism (see the "ticklish-cuddling-willow"
plan): a reply's quoted-media thumbnail hashes byte-identically to its
source message's own thumbnail, and the mention resolves to a stable
WhatsApp LID — never a display name, never a timestamp/position guess.

Module boundary: this file owns interpretation (mark-text parsing, identity
validation, project/role resolution, ambiguity/failure detection,
idempotency, the assignment audit record). It never talks to WhatsApp
directly — that's whatsapp-worker/mark_scan.py's job (open the group, hash
thumbnails, extract mention/mark text, and — once told exactly which
messages are needed — download their bytes). The two sides communicate only
through the `whatsapp_scan_requests` collection's documented shape below,
so either side can be re-deployed independently.

Bounded, on-demand only: a scan request is created once per `upload`
command and claimed once by the worker — there is no continuous polling of
any talent group anywhere in this design.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core import db, normalize_email
from agents.modules import casting_pipeline_nlu as nlu

IDENTITY_COLLECTION = "whatsapp_agent_identity"
SCAN_REQUESTS_COLLECTION = "whatsapp_scan_requests"
ASSIGNMENTS_COLLECTION = "media_assignments"

# The worker never scrolls WhatsApp Web further back than this for a scan —
# "smallest practical history window", not "scan indefinitely" (see plan).
# The source of truth is always the @Gunwanti + mark + hash match, never
# how wide this window happens to be; a mark outside it is a resolution
# failure, not a wider retry.
MAX_SCAN_MESSAGES = 300

# whatsapp_scan_requests.status lifecycle:
#   pending_scan -> (worker) -> scan_done | scan_failed
#   -> (backend orchestrator) -> pending_download | finished (nothing to
#      download, or every mark failed validation)
#   pending_download -> (worker) -> download_done | download_failed
#   -> (backend orchestrator) -> finished
SCAN_STATUS_PENDING = "pending_scan"
SCAN_STATUS_DONE = "scan_done"
SCAN_STATUS_FAILED = "scan_failed"
DOWNLOAD_STATUS_PENDING = "pending_download"
DOWNLOAD_STATUS_DONE = "download_done"
DOWNLOAD_STATUS_FAILED = "download_failed"
STATUS_FINISHED = "finished"

ASSIGN_STATUS_MARKED = "marked"
ASSIGN_STATUS_RESOLVING = "resolving"
ASSIGN_STATUS_UPLOADED = "uploaded"
ASSIGN_STATUS_FAILED = "failed"

MEDIA_ROLES = ("take", "intro", "photos")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_gunwanti_identity() -> Optional[Dict[str, str]]:
    """{"name", "phone", "lid"} — LID is the only field ever compared
    against a mention; phone is reference metadata only (see plan)."""
    return await db[IDENTITY_COLLECTION].find_one({}, {"_id": 0})


# ---------------------------------------------------------------------------
# Authoritative talent resolution for uploads (2026-08-23) — closes a real
# data-integrity gap: name-based talent resolution (casting_pipeline.py's
# _resolve_talent_query_target, used by every other command) identifies
# WHICH WhatsApp source/workflow an "upload" command is about, but it is
# NEVER authoritative for where the media actually lands. Two `talents`
# documents can share an identical name during the admin-manual-add ->
# talent-submits-their-own-email transition period this app documents
# (routers/submissions.py's submission_finalize: an admin-created record
# with no/different email is invisible to the email-based lookup that
# runs when the talent later submits a project form, so a SECOND talent
# record gets created for the same person, keyed by their real email).
# The project's own submission — keyed on (project_id, talent_email) — is
# the one place that unambiguously says which talent record this specific
# project's media belongs to. This function is the single source of
# truth: given a project and the SET of talent_ids a name query matched
# (even a set of one, for the ordinary non-duplicate case), it finds that
# project's submission among them, re-derives the talent from the
# submission's OWN submitted email (never trusting submission.talent_id
# blindly), and requires that to land back on exactly one of the
# candidates — never guessing, never picking by name, always stopping on
# any ambiguity or mismatch.
# ---------------------------------------------------------------------------
@dataclass
class AuthoritativeTalentResolution:
    ok: bool
    talent_id: Optional[str] = None
    talent_label: Optional[str] = None
    submission_id: Optional[str] = None
    email: Optional[str] = None
    error: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None


async def resolve_authoritative_talent_for_upload(
    project_id: str, candidate_talent_ids: List[str],
) -> AuthoritativeTalentResolution:
    """`candidate_talent_ids` is every talent_id a NAME query matched (one
    element in the common case; more than one during the duplicate-record
    transition period). Never returns ok=True without an exact,
    email-verified single match."""
    if not candidate_talent_ids:
        return AuthoritativeTalentResolution(ok=False, error="no_candidates")

    # P7 (soft-delete audit): this is a WRITE-PATH assist — it locates the
    # submission a WhatsApp-delivered audition asset should be attached to. A
    # soft-deleted project/submission has no live workflow; attaching fresh
    # media to it is never correct (it wouldn't render in client links and
    # would be retention-purged), so a soft-deleted submission must read as
    # "not found" here. MUST EXCLUDE — no historical-access case applies.
    from core import active_only
    submissions = await db.submissions.find(
        active_only({"project_id": project_id, "talent_id": {"$in": candidate_talent_ids}}),
        {"_id": 0, "id": 1, "talent_id": 1, "talent_email": 1},
    ).to_list(20)
    if not submissions:
        return AuthoritativeTalentResolution(
            ok=False, error="no_submission_found",
            detail={"project_id": project_id, "candidate_talent_ids": candidate_talent_ids},
        )
    distinct_submission_talent_ids = {s["talent_id"] for s in submissions}
    if len(submissions) > 1 or len(distinct_submission_talent_ids) > 1:
        # Either two DIFFERENT candidate talents each have their own
        # submission for this project (genuinely ambiguous — which one did
        # the employee mean?), or the same talent somehow has more than one
        # submission for this project (shouldn't happen given the
        # (project_id, talent_email) unique index, but never assumed safe
        # to pick either way).
        return AuthoritativeTalentResolution(
            ok=False, error="ambiguous_submission",
            detail={"submissions": submissions},
        )

    submission = submissions[0]
    email = normalize_email(submission.get("talent_email"))
    if not email:
        return AuthoritativeTalentResolution(
            ok=False, error="no_email_on_submission",
            detail={"submission_id": submission["id"]},
        )

    # Same $or shape routers/submissions.py's submission_finalize already
    # uses to look up a talent by submitted email — the established
    # convention for this exact lookup, not a new one invented here.
    email_talents = await db.talents.find(
        {"$or": [{"normalized_email": email}, {"email": email}, {"source.talent_email": email}]},
        {"_id": 0, "id": 1, "name": 1},
    ).to_list(10)
    distinct_ids = {t["id"] for t in email_talents}
    if len(distinct_ids) == 0:
        return AuthoritativeTalentResolution(
            ok=False, error="email_maps_to_no_talent", email=email,
            detail={"submission_id": submission["id"]},
        )
    if len(distinct_ids) > 1:
        return AuthoritativeTalentResolution(
            ok=False, error="email_maps_to_multiple_talents", email=email,
            detail={"submission_id": submission["id"], "talent_ids": sorted(distinct_ids)},
        )
    resolved_talent = email_talents[0]
    resolved_talent_id = resolved_talent["id"]

    if resolved_talent_id != submission["talent_id"]:
        # The submission's own talent_id disagrees with what its own
        # submitted email resolves to — a real data inconsistency, never
        # silently trusted either way.
        return AuthoritativeTalentResolution(
            ok=False, error="submission_talent_mismatch", email=email,
            detail={"submission_talent_id": submission["talent_id"], "email_resolved_talent_id": resolved_talent_id},
        )

    if resolved_talent_id not in candidate_talent_ids:
        # The email-verified talent isn't even among the talents the
        # employee's NAME query matched — a different person than
        # intended. Never silently substituted.
        return AuthoritativeTalentResolution(
            ok=False, error="email_resolved_to_unexpected_talent", email=email,
            detail={"resolved_talent_id": resolved_talent_id, "resolved_talent_name": resolved_talent.get("name"),
                    "candidate_talent_ids": candidate_talent_ids},
        )

    return AuthoritativeTalentResolution(
        ok=True, talent_id=resolved_talent_id, talent_label=resolved_talent.get("name"),
        submission_id=submission["id"], email=email,
    )


async def ensure_indexes() -> None:
    """Called once at backend startup (see server.py) — safe to call
    repeatedly, create_index is idempotent.

    2026-08-23: the original unique key (talent_id, project_id,
    source_message_id) was correct for single-message marks but wrong for
    albums — every tile in an album shares the SAME source_message_id (the
    album's own data-id), so two tiles marked for the same talent+project
    collided on this index; record_assignment's except-fallback would
    silently return tile 1's row when tile 2 was inserted. Added
    source_thumbnail_hash (unique per tile) to the key. Old index dropped
    by name first since Mongo errors on redefining an existing index name
    with different keys."""
    try:
        await db[ASSIGNMENTS_COLLECTION].drop_index("uniq_talent_project_source_message")
    except Exception:
        pass  # never existed, or already dropped — fine either way
    await db[ASSIGNMENTS_COLLECTION].create_index(
        [("talent_id", 1), ("project_id", 1), ("source_message_id", 1), ("source_thumbnail_hash", 1)],
        unique=True, name="uniq_talent_project_source_message_hash",
    )
    await db[SCAN_REQUESTS_COLLECTION].create_index("created_at")


# ---------------------------------------------------------------------------
# Mark-text parsing — "google take 1" / "google intro" / "intro google" /
# "introduction google" / "google introduction" / "google photos", case-
# insensitive, position-independent. The literal word "mark" itself is
# stripped by the caller before this runs (see extract_role_and_project);
# kept as a defensive re-check here too since this function may be called
# directly in tests.
# ---------------------------------------------------------------------------
_MARK_KEYWORD_RE = re.compile(r"\bmark\b", re.IGNORECASE)
_TAKE_RE = re.compile(r"\btake\s*([1-9][0-9]?)\b", re.IGNORECASE)
# A bare "take" with no number at all (2026-08-27) — distinct from _TAKE_RE
# (which requires a digit): the admin marked this as a take, they just
# didn't say which one. Never guessed/defaulted to 1 here; take_number
# stays None all the way through, same as intro/photos already do.
_TAKE_BARE_RE = re.compile(r"\btake\b", re.IGNORECASE)
_INTRO_RE = re.compile(r"\bintro(?:duction)?\b", re.IGNORECASE)
_PHOTOS_RE = re.compile(r"\bphotos?\b", re.IGNORECASE)
# WhatsApp's own DOM renders a message's timestamp text TWICE (once for the
# bubble, once for an accessibility/tooltip label), sometimes prefixed with
# "Edited" — e.g. "...take 1     7:20 am         7:20 am" or "...take 1
# Edited  5:05 am         Edited  5:05 am". mark_text is captured straight
# from that DOM (see mark_scan.py's _mark_text), so this noise ends up
# baked into project_fragment unless stripped — found live 2026-08-25: it
# corrupted fuzzy project resolution (a genuinely unambiguous "Google Test
# 3" became ambiguous against "Google Test" once "7:20 am 7:20 am" was
# appended), silently dropping an otherwise-valid mark as "wrong project".
_TRAILING_TIMESTAMP_RE = re.compile(r"(\s+(?:Edited\s+)?\d{1,2}:\d{2}\s*(?:am|pm))+\s*$", re.IGNORECASE)


@dataclass
class ParsedMark:
    project_fragment: str
    media_role: str  # "take" | "intro" | "photos"
    take_number: Optional[int] = None


def extract_role_and_project(raw_mark_text: str) -> Optional[ParsedMark]:
    """None means "contains 'mark' but not a shape we recognize" (e.g. no
    role keyword at all) — the caller reports this as an unresolvable mark,
    never guesses a role."""
    if not raw_mark_text or not _MARK_KEYWORD_RE.search(raw_mark_text):
        return None
    working = _MARK_KEYWORD_RE.sub(" ", raw_mark_text, count=1)
    # Strip WhatsApp's trailing timestamp DOM noise BEFORE any role/take-
    # number extraction runs (2026-08-27 fix) — _TAKE_RE's \btake\s*(\d+)\b
    # has no bound on how much whitespace it crosses, so a bare "Take"
    # immediately followed by an appended "...     7:26 am         7:26 am"
    # timestamp was matching the "7" from the time as if it were the
    # admin's own take number. The original ordering only stripped this
    # noise AFTER take/intro/photos extraction, which was late enough to
    # protect project_fragment matching (its original purpose) but not
    # early enough to protect take_number itself.
    working = _TRAILING_TIMESTAMP_RE.sub("", working)

    take_m = _TAKE_RE.search(working)
    if take_m:
        media_role, take_number = "take", int(take_m.group(1))
        working = _TAKE_RE.sub(" ", working, count=1)
    elif _TAKE_BARE_RE.search(working):
        media_role, take_number = "take", None
        working = _TAKE_BARE_RE.sub(" ", working, count=1)
    elif _INTRO_RE.search(working):
        media_role, take_number = "intro", None
        working = _INTRO_RE.sub(" ", working, count=1)
    elif _PHOTOS_RE.search(working):
        media_role, take_number = "photos", None
        working = _PHOTOS_RE.sub(" ", working, count=1)
    else:
        return None

    project_fragment = re.sub(r"\s+", " ", working).strip()
    if not project_fragment:
        return None
    return ParsedMark(project_fragment=project_fragment, media_role=media_role, take_number=take_number)


# ---------------------------------------------------------------------------
# Scan-request lifecycle (backend side of the state machine described in
# the module docstring). whatsapp-worker/mark_scan.py claims/writes these
# same documents — the shape here is the contract between the two services.
# ---------------------------------------------------------------------------
async def create_scan_request(
    *, talent_id: str, talent_label: str, project_id: str, project_label: str, group_name: str,
) -> str:
    req_id = str(uuid.uuid4())
    await db[SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id,
        "mode": "scan",
        "status": SCAN_STATUS_PENDING,
        "group_name": group_name,
        "talent_id": talent_id,
        "talent_label": talent_label,
        "project_id": project_id,
        "project_label": project_label,
        "max_messages": MAX_SCAN_MESSAGES,
        "candidates": None,
        "download_targets": None,
        "download_results": None,
        "report": None,
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": None,
    })
    return req_id


async def get_scan_request(request_id: str) -> Optional[Dict[str, Any]]:
    return await db[SCAN_REQUESTS_COLLECTION].find_one({"id": request_id}, {"_id": 0})


# ---------------------------------------------------------------------------
# Candidate validation — turns the worker's raw {lid, mark_text,
# quoted_thumbnail_hash, reply_message_id, resolved_source_message_id}
# candidates into a validated assignment set for ONE requested project.
# Every rule below is a direct restatement of the spec's "SAFETY RULES" —
# nothing here guesses; every branch either resolves cleanly or reports a
# specific, named failure.
# ---------------------------------------------------------------------------
@dataclass
class ValidationOutcome:
    ok: bool
    assignments: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    ambiguous: Optional[Dict[str, Any]] = None  # {"media_role", "take_number", "candidates": [...]}
    unresolved: List[Dict[str, Any]] = dataclass_field(default_factory=list)  # marks with no hash match
    # Batch marks (e.g. "mark google: take 1, take 2, ...") the worker
    # could not deterministically resolve to an album's tiles — a
    # DISTINCT failure category from `unresolved`, never merged into it
    # and never given a chance at the single-mark parser (see
    # validate_candidates' resolution_status short-circuit below): a
    # failed batch's raw text (e.g. "take 1, take 2, take 3, intro")
    # could otherwise be misread as an ordinary single "take 1" mark by
    # extract_role_and_project, which only looks for the FIRST role
    # keyword it finds.
    batch_failures: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    # Project-text safety checks (2026-08-25) — DISTINCT from `ambiguous`
    # above (which is about two marks claiming the SAME slot). These are
    # about a single mark's OWN project-fragment text:
    #   project_mismatch: the fragment confidently matches a REAL project
    #     that is NOT the one the admin explicitly requested — e.g. a
    #     mark meant for a different project, still marked in the same
    #     WhatsApp group. Never silently redirected to the requested
    #     project; always flagged.
    #   project_ambiguous: the fragment is tied between multiple real
    #     projects (could be either) — never guessed, always flagged.
    # A fragment matching NEITHER of the above (no confident match to
    # ANYTHING) is NOT an error — see validate_candidates' own comment —
    # it defaults to the admin-requested project, since the admin already
    # explicitly resolved the target project via the UPLOAD command
    # itself; the mark's job is identifying WHICH media, not re-proving
    # which project.
    project_mismatch: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    project_ambiguous: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    error: Optional[str] = None


def slot_key(
    media_role: str, take_number: Optional[int],
    source_message_id: Optional[str] = None, source_thumbnail_hash: Optional[str] = None,
) -> tuple:
    """The "slot" a piece of media occupies for a talent+project.
    "take"/"intro" have a genuine single slot per (role, take_number) —
    exactly one video should ever occupy "Take 1"; two DIFFERENT source
    tiles both claiming it is real ambiguity, never auto-picked. "photos"
    has no such natural slot: a batch mark (e.g. "mark google photos" on a
    whole photo album) legitimately produces many DISTINCT photos sharing
    role="photos" — including the source identity in the key means each
    photo gets its own slot, so two different photos never collide as
    "ambiguous" with each other, and uploading one never makes another
    look "already uploaded". Used consistently everywhere a slot is
    compared: validate_candidates' ambiguity/dedup logic and the backend
    orchestrator's already-uploaded / still-to-download checks."""
    if media_role == "photos":
        return (media_role, take_number, source_message_id, source_thumbnail_hash)
    return (media_role, take_number)


def validate_candidates(
    candidates: List[Dict[str, Any]],
    *,
    gunwanti_lid: str,
    requested_project_id: str,
    requested_project_label: str,
    projects: List[Dict[str, str]],
    talent_id: str,
    project_id_for_label: Optional[str] = None,
) -> ValidationOutcome:
    """`candidates` is the worker's raw scan result — every reply in the
    bounded window that contains the literal word "mark", REGARDLESS of
    mention validity (the worker stays a dumb I/O layer; this function
    owns the identity check, per the module docstring).

    `gunwanti_lid` (2026-08-25: NO LONGER used to filter candidates — see
    below). MARK is the authoritative media-selection signal; the reply
    ITSELF, quoted to a real media message, establishes source identity.
    Requiring a specific WhatsApp mention was the original Phase-1 design
    but doesn't hold for every real scenario (a talent with no group, a
    direct-chat source, a different admin replying) — an admin mention is
    now optional metadata only (still recorded as mark_target_contact_id
    on the resulting assignment/send record), never a gate. The parameter
    is kept for call-site/signature stability; it is simply unused now."""
    valid_marks: List[Dict[str, Any]] = []
    batch_failures: List[Dict[str, Any]] = []
    project_mismatch: List[Dict[str, Any]] = []
    project_ambiguous: List[Dict[str, Any]] = []
    for c in candidates:
        if c.get("resolution_status") == "BATCH_RESOLUTION_FAILED":
            # A batch mark (e.g. "mark google: take 1, take 2, take 3,
            # intro") the worker could not deterministically resolve to
            # an album's tiles. Regardless of what follows below, this
            # candidate can NEVER become a resolved single-media
            # assignment — that's the actual bug found in production
            # (2026-08-23 E2E): extract_role_and_project only looks for
            # the FIRST role keyword it finds, so a failed batch's raw
            # text was misread as an ordinary single "take 1" mark,
            # creating a bogus assignment with no resolved hash. A
            # best-effort project-relevance check still runs (so a
            # failure for a DIFFERENT project doesn't spuriously block
            # or clutter this one's report) — but its result is used
            # ONLY to filter, never to build an assignment.
            parsed_for_relevance = extract_role_and_project(c.get("mark_text") or "")
            if parsed_for_relevance is not None:
                relevance_match = nlu.resolve_project_by_name(parsed_for_relevance.project_fragment, projects)
                # Deliberately STRICTER than the single-mark admin-
                # authoritative default below (2026-08-26 fix — a real
                # SEND E2E found the exact failure mode this guards
                # against): a batch mark that can never become a
                # resolved assignment is surfaced to the admin as a hard
                # BLOCK ("no upload/send was performed"), not merely
                # excluded from the accepted set. Defaulting an
                # unresolvable batch mark with NO confident project match
                # to "relevant here" turns any WhatsApp group that ever
                # had ONE such stale/ambiguous batch mark into a
                # permanent poison pill, blocking every future unrelated
                # request that happens to scan the same group — proven
                # live: an old "mark google: take 1, take 2, take 3,
                # intro" batch mark (unrelated to either project) blocked
                # a fresh, unrelated SEND for a completely different
                # project. Only a CONFIDENT match to the requested
                # project makes a batch failure relevant here; no match
                # at all is silently ignored, same as before the
                # single-mark leniency existed.
                if not relevance_match.project or relevance_match.project["id"] != requested_project_id:
                    continue  # not confidently for THIS project -> irrelevant, never surfaced as a block
            batch_failures.append(c)
            continue
        parsed = extract_role_and_project(c.get("mark_text") or "")
        if parsed is None:
            continue  # contains "mark" but no recognizable role -> not a candidate at all
        match = nlu.resolve_project_by_name(parsed.project_fragment, projects)
        # Project-text safety rule (2026-08-25 — real production
        # incident: Sharvari Kashid / Tapti AI App (Ananya). The admin's
        # UPLOAD command already explicitly resolved the target project;
        # a mark's job is identifying WHICH media, not re-proving which
        # project via informal WhatsApp shorthand ("Tapti Ai Test" for
        # "Tapti AI App (Ananya)"). Four distinct cases, never guessed:
        #   1. confidently matches the REQUESTED project -> accept.
        #   2. confidently matches a DIFFERENT real project -> reject,
        #      flagged as a mismatch (never silently redirected).
        #   3. tied between multiple real projects -> reject, flagged as
        #      ambiguous (never guessed).
        #   4. matches nothing confidently at all -> defaults to the
        #      admin-requested project (safe: this can only ever resolve
        #      to the ONE project this whole scan is already scoped to,
        #      never to a wrong one — cases 2 and 3 already excluded any
        #      mark that confidently points elsewhere).
        if match.project:
            if match.project["id"] != requested_project_id:
                project_mismatch.append({
                    **c, "media_role": parsed.media_role, "take_number": parsed.take_number,
                    "matched_project_label": match.project["label"],
                })
                continue
            # else: confidently matches the requested project -> accept below.
        elif match.ambiguous:
            project_ambiguous.append({
                **c, "media_role": parsed.media_role, "take_number": parsed.take_number,
                "ambiguous_projects": match.ambiguous,
            })
            continue
        # else: no confident match to anything -> default to the
        # admin-requested project (case 4 above) -> accept below.
        valid_marks.append({
            **c, "media_role": parsed.media_role, "take_number": parsed.take_number,
        })

    if batch_failures:
        # Checked before ambiguous/unresolved, same "stop, don't partially
        # proceed" posture — a batch mark that failed to resolve means the
        # employee's intended assignment for (at least) this reply was
        # never established at all; never silently ignored in favor of
        # whatever other marks happened to resolve fine in the same scan.
        return ValidationOutcome(
            ok=False, batch_failures=batch_failures,
            project_mismatch=project_mismatch, project_ambiguous=project_ambiguous,
        )


    def _key(m: Dict[str, Any]) -> tuple:
        return slot_key(m["media_role"], m["take_number"], m.get("resolved_source_message_id"), m.get("quoted_thumbnail_hash"))

    # Group by slot to detect duplicate marks of the exact same slot —
    # never auto-pick between them.
    by_slot: Dict[tuple, List[Dict[str, Any]]] = {}
    for m in valid_marks:
        by_slot.setdefault(_key(m), []).append(m)

    for key, marks in by_slot.items():
        # Distinct source media resolving to the SAME slot is the
        # ambiguity the spec calls out; the SAME source media marked twice
        # (identical (source_message_id, quoted_thumbnail_hash) — the
        # thumbnail hash matters here too: every tile in an album shares
        # the same source_message_id, so comparing that alone would miss
        # two DIFFERENT tiles both claiming the same take) is harmless
        # idempotent duplication, not ambiguity.
        distinct_sources = {(m.get("resolved_source_message_id"), m.get("quoted_thumbnail_hash")) for m in marks}
        if len(distinct_sources) > 1:
            media_role, take_number = key[0], key[1]
            return ValidationOutcome(
                ok=False,
                ambiguous={
                    "media_role": media_role, "take_number": take_number,
                    "candidates": marks,
                },
                project_mismatch=project_mismatch, project_ambiguous=project_ambiguous,
            )

    unresolved = [m for m in valid_marks if not m.get("resolved_source_message_id")]
    if unresolved:
        return ValidationOutcome(
            ok=False, unresolved=unresolved,
            project_mismatch=project_mismatch, project_ambiguous=project_ambiguous,
        )

    # Dedupe identical-slot repeats (e.g. the same mark sent twice by
    # accident) down to one assignment per slot.
    seen_slots = set()
    assignments = []
    for m in valid_marks:
        key = _key(m)
        if key in seen_slots:
            continue
        seen_slots.add(key)
        assignments.append(m)

    # project_mismatch/project_ambiguous (2026-08-25) are advisory, NEVER
    # blocking — a mark confidently belonging to a different real project,
    # or genuinely ambiguous between two, is excluded from `assignments`
    # above (never uploaded to the wrong/uncertain place) but must not
    # stop OTHER, correctly-resolved marks in the SAME scan from
    # completing normally (a talent's WhatsApp group legitimately
    # accumulates marks for multiple projects over time; an unrelated
    # mismatch elsewhere in the same thread must never block a
    # completely valid upload for the requested project).
    return ValidationOutcome(
        ok=True, assignments=assignments,
        project_mismatch=project_mismatch, project_ambiguous=project_ambiguous,
    )


# ---------------------------------------------------------------------------
# media_assignments bookkeeping — the persistent, auditable record. Unique
# index on (talent_id, project_id, source_message_id) (see ensure_indexes)
# is what makes a second `upload` run idempotent: an insert for an
# already-recorded source message is a harmless duplicate-key no-op, and
# assignment_status="uploaded" rows are reported, never re-uploaded.
# ---------------------------------------------------------------------------
async def already_uploaded(talent_id: str, project_id: str) -> List[Dict[str, Any]]:
    """Reconciliation-based (2026-08-25 fix): an assignment row claiming
    assignment_status="uploaded" is no longer trusted blindly — it must
    ALSO have a matching entry (by source_message_id) in the TARGET
    SUBMISSION's own media[] array, which is the actual source of truth
    an admin/client sees. A row that says "uploaded" but the submission
    has no matching media (the submission was recreated/reset after the
    assignment was recorded, a manual admin edit removed it, or any other
    drift between the two collections) is excluded here — the caller's
    to_download computation then naturally includes it again for a real
    retry instead of the system reporting a false ALREADY COMPLETED /
    UPLOAD COMPLETE while the submission is actually missing the media.
    Never mutates the stale row itself; a future successful (re-)upload
    updates it in place via mark_assignment_status's existing update_one."""
    rows = await db[ASSIGNMENTS_COLLECTION].find(
        {"talent_id": talent_id, "project_id": project_id, "assignment_status": ASSIGN_STATUS_UPLOADED},
        {"_id": 0},
    ).to_list(200)
    if not rows:
        return []
    from core import active_only  # P7: consistent with resolve_authoritative_talent_for_upload
    submission = await db.submissions.find_one(
        active_only({"talent_id": talent_id, "project_id": project_id}), {"_id": 0, "media": 1}
    )
    submitted_source_ids = {
        m.get("source_message_id") for m in ((submission or {}).get("media") or []) if m.get("source_message_id")
    }
    return [r for r in rows if r.get("source_message_id") in submitted_source_ids]


async def record_assignment(
    *, talent_id: str, project_id: str, normalized_project: str, group_name: str,
    group_id: Optional[str], mark: Dict[str, Any], created_by: str,
) -> Dict[str, Any]:
    """Upserts a `marked`-status row keyed on the unique index — safe to
    call repeatedly for the same source message without creating
    duplicates. Returns the (possibly pre-existing) document."""
    doc = {
        "assignment_id": str(uuid.uuid4()),
        "talent_id": talent_id,
        "project_id": project_id,
        "source_group_id": group_id,
        "source_group_name": group_name,
        "source_message_id": mark.get("resolved_source_message_id"),
        "source_media_type": mark.get("source_media_type"),
        "source_thumbnail_hash": mark.get("quoted_thumbnail_hash"),
        "source_sender": mark.get("source_sender"),
        "source_timestamp": mark.get("source_timestamp"),
        "mark_reply_message_id": mark.get("reply_message_id"),
        "mark_reply_text": mark.get("mark_text"),
        "mark_target_phone": mark.get("mention_phone"),
        "mark_target_contact_id": mark.get("mention_lid"),
        "normalized_project": normalized_project,
        "media_role": mark.get("media_role"),
        "take_number": mark.get("take_number"),
        "original_label": mark.get("mark_text"),
        "assignment_status": ASSIGN_STATUS_MARKED,
        "created_at": _now(),
        "created_by": created_by,
    }
    try:
        await db[ASSIGNMENTS_COLLECTION].insert_one(doc)
        return doc
    except Exception:
        # Matches the unique index exactly (talent_id, project_id,
        # source_message_id, source_thumbnail_hash) — for an album,
        # source_message_id alone is shared by every tile, so this MUST
        # include the hash or it can return a different tile's row.
        existing = await db[ASSIGNMENTS_COLLECTION].find_one(
            {
                "talent_id": talent_id, "project_id": project_id,
                "source_message_id": doc["source_message_id"],
                "source_thumbnail_hash": doc["source_thumbnail_hash"],
            },
            {"_id": 0},
        )
        return existing or doc


async def mark_assignment_status(
    talent_id: str, project_id: str, source_message_id: str, source_thumbnail_hash: str, status: str, **extra
) -> None:
    """source_thumbnail_hash is required, not optional — for an album,
    source_message_id alone matches every tile sharing that album; without
    the hash, update_one could silently update the wrong tile's row."""
    await db[ASSIGNMENTS_COLLECTION].update_one(
        {
            "talent_id": talent_id, "project_id": project_id,
            "source_message_id": source_message_id, "source_thumbnail_hash": source_thumbnail_hash,
        },
        {"$set": {"assignment_status": status, **extra}},
    )


def submission_label(media_role: str, take_number: Optional[int]) -> str:
    """The label shown on the SUBMISSION itself, where the project is
    already implicit (unlike role_label, used for cross-project WhatsApp
    chat reports, which prefixes the project name) — "Take 1",
    "Introduction", "Photo" — matching the codebase's own "Take N"
    submission-media-naming convention (routers/submissions.py)."""
    if media_role == "take" and take_number:
        return f"Take {take_number}"
    if media_role == "intro":
        return "Introduction"
    if media_role == "photos":
        return "Photo"
    return media_role.capitalize()


def role_label(media_role: str, take_number: Optional[int], project_label: str) -> str:
    if media_role == "take" and take_number:
        return f"{project_label} Take {take_number}"
    if media_role == "intro":
        return f"{project_label} Intro"
    if media_role == "photos":
        return f"{project_label} Photos"
    return f"{project_label} {media_role}"
