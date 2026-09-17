"""Incoming-audition / submission-preparation RESOLUTION (Phase 4B).

Resolves a "X has sent her test" / "prepare X's P submission" / "attach X's
intro to P" command into a fully server-resolved `SubmissionPlan` for
preview. Attaching happens in submission_execute.py behind an HMAC-signed
plan + explicit confirm, through the canonical submission service
`routers.submissions.attach_existing_talent_media_to_submission`.

Reused (read-only) resolution:
  * agents.modules.media_assignment.resolve_authoritative_talent_for_upload
      — duplicate-record tie-break: which talent record owns THIS project's submission
  * routers.submissions.build_prefill_media
      — the ONE canonical "what's in this talent's library" builder
  * simple_assistant.comm._resolve_submission  — (project, authoritative talent) -> submission
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from core import (
    MAX_SUBMISSION_TAKES,
    MAX_SUBMISSION_VIDEO_BYTES,
    compute_age,
    video_poster_url,
)
from routers.submissions import build_prefill_media
from simple_assistant import comm
from simple_assistant.comm import _INTRO_RE, _TAKE_N_RE, _AUDITION_ALL_RE, _clean_frag
from simple_assistant.readonly_db import RDB
from simple_assistant.submission_plan import ATTACH, INGEST, INSPECT, SubMedia, SubmissionPlan

logger = logging.getLogger(__name__)

_TAKE_CATEGORIES = {"take", "take_1", "take_2", "take_3"}
_PORTFOLIO_CATEGORIES = {"image", "indian", "western", "portfolio", "additional_portfolio"}
_LEGACY_TAKE_LABEL = {"take_1": "Take 1", "take_2": "Take 2", "take_3": "Take 3"}

_INSPECT_RE = re.compile(
    r"\b(?:has\s+sent|sent\s+(?:her|his|their|the|an?)\s+(?:test|audition|tape|files?|submission)|"
    r"check\b.+\bfiles?\b|prepare\b.+\bsubmission\b|prepare\b.+\b(?:for|audition)\b|"
    r"sent\s+(?:her|his|their)\s+test|inspect\b.+\bsubmission\b|review\b.+\bsubmission\b)\b",
    re.I,
)
_ATTACH_RE = re.compile(r"\battach\b", re.I)
_UPLOAD_RE = re.compile(r"\bupload(?:ed|ing|s)?\b", re.I)
_SENT_RE = re.compile(r"\b(?:has\s+sent|sent)\b", re.I)
# strip "this as " / "this audition for " / "it as " fillers that sit between
# the upload verb and the talent, e.g. "Upload this as Ahana's Take 2".
_INGEST_FILLER_RE = re.compile(
    r"^(?:this\s+audition|this|it|the\s+(?:file|video|audition|clip))\s+(?:as|for)\s+", re.I
)
_BARE_NAME_RE = re.compile(r"^[A-Za-z][\w.\-]*(?:\s+[A-Za-z][\w.\-]*){0,2}$")
_PORTFOLIO_TERM_RE = re.compile(r"\b(?:portfolio(?:\s+(?:image|photo|picture)s?)?|photos?|pictures?|looks?|polaroids?)\b", re.I)

_POSSESSIVE_RE = re.compile(r"^\s*(.+?)'s\b", re.I)
_LEAD_VERB_RE = re.compile(
    r"^\s*(?:can you\s+|please\s+)*(?:attach|prepare|check|inspect|review|get|show\s+me|upload)\s+", re.I
)
_FOR_PROJECT_RE = re.compile(
    r"\b(?:for|to|in|on)\s+(?:the\s+|her\s+|his\s+|their\s+)?(?P<proj>.+?)"
    r"(?:\s+(?:submission|project|casting|audition))?\s*(?:[.?!]|,\s|$)",
    re.I,
)
_PREPARE_PROJECT_RE = re.compile(r"\b(?P<proj>.+?)\s+submission\b", re.I)


def is_submission_command(message: str) -> bool:
    m = message or ""
    to_casting = re.search(r"\bcasting\s+(?:group|team)\b", m, re.I)
    return bool(
        _INSPECT_RE.search(m)
        or (_ATTACH_RE.search(m) and not to_casting)
        or (
            _UPLOAD_RE.search(m)
            and not to_casting
            and re.search(r"\b(?:audition|take|tape|intro|submission|clip|video|footage)\b", m, re.I)
        )
    )


class ParsedSub:
    __slots__ = ("kind", "talent_text", "project_text", "media_terms")

    def __init__(self):
        self.kind = INSPECT
        self.talent_text: Optional[str] = None
        self.project_text: Optional[str] = None
        self.media_terms: List[Dict[str, Any]] = []


def parse_submission(message: str) -> Optional[ParsedSub]:
    msg = (message or "").strip()
    if not is_submission_command(msg):
        return None
    p = ParsedSub()
    if _UPLOAD_RE.search(msg):
        p.kind = INGEST
    elif _ATTACH_RE.search(msg):
        p.kind = ATTACH
    else:
        p.kind = INSPECT

    body = _LEAD_VERB_RE.sub("", msg).strip()
    body = _INGEST_FILLER_RE.sub("", body).strip()   # "this as " / "this audition for "

    poss = _POSSESSIVE_RE.match(body)
    if poss:
        p.talent_text = _clean_frag(poss.group(1))
        body = body[poss.end():].strip()   # drop "<talent>'s " so project parsing is clean
    else:
        # "Ahana has sent her test" / "Ahana sent her audition for Google AI"
        m = re.match(r"^\s*([A-Za-z][\w.\- ]{1,40}?)\s+(?:has\s+sent|sent)\b", msg, re.I)
        if m:
            p.talent_text = _clean_frag(m.group(1))
        elif _BARE_NAME_RE.match(body):
            # "Prepare this audition for Ahana" → body is now just "Ahana"
            p.talent_text = _clean_frag(body)
            body = ""

    # media terms
    take_ns = [int(m.group(1)) for m in _TAKE_N_RE.finditer(msg)]
    for n in take_ns:
        p.media_terms.append({"kind": "take", "n": n})
    if _INTRO_RE.search(msg):
        p.media_terms.append({"kind": "intro"})
    if _PORTFOLIO_TERM_RE.search(msg):
        p.media_terms.append({"kind": "portfolio_all"})
    # "audition"/"audition takes"/"files" (with no specific take number) → all takes
    if not take_ns and not any(t["kind"] == "portfolio_all" for t in p.media_terms) and (
        _AUDITION_ALL_RE.search(msg) or re.search(r"\bfiles?\b", msg, re.I)
    ):
        p.media_terms.append({"kind": "audition_all"})

    # project
    pm = _PREPARE_PROJECT_RE.search(body)
    if pm:
        p.project_text = _clean_frag(pm.group("proj")) or None
    if not p.project_text:
        last = None
        for m in _FOR_PROJECT_RE.finditer(msg):
            c = _clean_frag(m.group("proj"))
            if c and c.lower() not in {"the", "her", "him", "their", "a", "test", "audition", "files", "submission"}:
                last = c
        p.project_text = last
    return p


# --------------------------------------------------------------------------
# Media mapping
# --------------------------------------------------------------------------
def _existing_media(sub: dict) -> Tuple[List[SubMedia], List[SubMedia], List[SubMedia]]:
    """→ (intro[], takes[], portfolio[]) as they sit on the submission NOW."""
    intro, takes, portfolio = [], [], []
    for m in sub.get("media") or []:
        if not isinstance(m, dict) or not m.get("url"):
            continue
        cat = (m.get("category") or "").lower()
        if cat == "intro_video":
            intro.append(_sm(m, "Intro Video", "video"))
        elif cat in _TAKE_CATEGORIES:
            label = (m.get("label") or "").strip() or _LEGACY_TAKE_LABEL.get(cat, "Take")
            takes.append(_sm(m, label, "video"))
        elif cat in _PORTFOLIO_CATEGORIES:
            portfolio.append(_sm(m, m.get("label") or cat.title(), "image"))
    takes.sort(key=lambda x: x.label)
    return intro, takes, portfolio


def _sm(m: dict, label: str, kind: str) -> SubMedia:
    return SubMedia(
        category=(m.get("category") or "").lower(),
        label=(m.get("label") or label),
        kind=kind,
        submission_media_id=m.get("id"),
        url=m.get("url"),
        poster_url=m.get("poster_url") or video_poster_url(m.get("public_id")) or video_poster_url(m.get("url")),
    )


def _available_from_library(library: List[dict], attached_source_ids: set) -> List[SubMedia]:
    out: List[SubMedia] = []
    for m in library:
        mid = m.get("id")
        if not mid or mid in attached_source_ids or not (m.get("url") or m.get("public_id")):
            continue
        cat = (m.get("category") or "").lower()
        if cat in ("video", "intro_video"):
            out.append(SubMedia(category="intro_video", label="Intro Video", kind="video",
                                source_id=mid, url=m.get("url"),
                                poster_url=m.get("poster_url") or video_poster_url(m.get("public_id"))))
        elif cat in _PORTFOLIO_CATEGORIES:
            out.append(SubMedia(category="image", label=m.get("label") or "Portfolio image", kind="image",
                                source_id=mid, url=m.get("url")))
    return out


_FORM_FIELDS = [
    ("name", ("first_name", "last_name", "name")),
    ("age", ("age",)),
    ("height", ("height",)),
    ("location", ("location", "current_location", "city")),
    ("budget", ("budget", "budget_expectation", "expected_budget")),
    ("availability", ("availability", "available")),
    ("instagram", ("instagram_handle", "instagram")),
]


def _form_view(sub: dict) -> Tuple[bool, Dict[str, Any]]:
    fd = sub.get("form_data") or {}
    if not fd:
        return False, {}
    out: Dict[str, Any] = {}
    for label, keys in _FORM_FIELDS:
        if label == "name":
            fn = fd.get("first_name") or ""
            ln = fd.get("last_name") or ""
            v = (f"{fn} {ln}".strip()) or fd.get("name")
        else:
            v = next((fd.get(k) for k in keys if fd.get(k) not in (None, "", [])), None)
        if v not in (None, "", []):
            out[label] = v
    dob = fd.get("dob")
    if "age" not in out and dob:
        a = compute_age(dob)
        if a:
            out["age"] = a
    return True, out


# --------------------------------------------------------------------------
# Plan builder
# --------------------------------------------------------------------------
class Resolved:
    def __init__(self, *, plan=None, error=None, clarification=None):
        self.plan = plan
        self.error = error
        self.clarification = clarification


_ALLOWED_VIDEO_CT = ("video/",)
_ALLOWED_VIDEO_EXT = (".mp4", ".mov", ".avi", ".webm", ".mkv", ".3gp")


def _is_video_meta(fm: Dict[str, Any]) -> bool:
    ct = str(fm.get("content_type") or "").lower()
    fn = str(fm.get("filename") or "").lower()
    return ct.startswith(_ALLOWED_VIDEO_CT) or fn.endswith(_ALLOWED_VIDEO_EXT)


def _ingest_target(parsed: ParsedSub, takes: List[SubMedia]) -> Tuple[str, str]:
    """Resolve the ONE destination slot for a single incoming file.
    intro wins over an explicit take number wins over 'next take'."""
    terms = parsed.media_terms or []
    if any(t["kind"] == "intro" for t in terms):
        return "intro_video", "Intro Video"
    take_terms = [t for t in terms if t["kind"] == "take"]
    if take_terms:
        n = take_terms[0]["n"]
        if 1 <= n <= 3:
            return f"take_{n}", f"Take {n}"
        return "take", f"Take {n}"
    return "take", f"Take {len(takes) + 1}"


async def build_plan(
    *, parsed: ParsedSub, talent: Dict[str, str], project_id: Optional[str],
    project_label: Optional[str], talent_candidate_ids: List[str],
    file_meta: Optional[Dict[str, Any]] = None,
) -> Resolved:
    tlabel = talent["label"]
    if not project_id:
        return Resolved(error="Which project is this submission for?")

    sub, auth_tid, sub_err = await comm._resolve_submission(project_id, talent_candidate_ids or [talent["id"]])
    if sub_err == "ambiguous_submission":
        return Resolved(error=(
            f"More than one talent record named {tlabel} has a {project_label} submission — "
            "please resolve the duplicate records first."
        ))
    if not sub:
        return Resolved(error=f"I couldn't find a {project_label} submission for {tlabel}.")

    # authoritative talent record for the library lookup
    auth_talent = await RDB.talents.find_one(
        {"id": auth_tid}, {"_id": 0, "id": 1, "name": 1, "email": 1, "media": 1}
    )
    library = []
    if auth_talent:
        library = await build_prefill_media(auth_talent, email=auth_talent.get("email"))

    intro, takes, portfolio = _existing_media(sub)
    attached_src = {m.get("source_talent_media_id") for m in (sub.get("media") or []) if m.get("source_talent_media_id")}
    available = _available_from_library(library, attached_src)
    form_found, form = _form_view(sub)

    existing_all = intro + takes + portfolio
    missing: List[str] = []
    reqs = (sub.get("submission_requirements") or {})
    if not intro and not any(a.category == "intro_video" for a in available):
        missing.append("Introduction video")
    if not takes:
        missing.append("Audition takes")

    status = sub.get("status") or sub.get("decision") or "pending"

    plan = SubmissionPlan(
        action_type=parsed.kind,
        talent={"id": auth_tid or talent["id"], "label": tlabel},
        project={"id": project_id, "label": project_label},
        submission_id=sub.get("id"),
        submission_status=status,
        form_found=form_found, form=form,
        existing_media=existing_all,
        available_media=available,
        missing=missing,
    )

    if parsed.kind == INSPECT:
        plan.requires_confirmation = False
        plan.executable = False
        return Resolved(plan=plan)

    # ---- INGEST: a genuinely NEW incoming file → canonical upload pipeline ----
    if parsed.kind == INGEST:
        if not file_meta:
            return Resolved(error=(
                "I don't have a file attached to this command. "
                "Attach the audition video first, then ask me again."
            ))
        target_cat, target_label = _ingest_target(parsed, takes)
        replaces = bool(
            (target_cat == "intro_video" and intro)
            or (target_cat in ("take_1", "take_2", "take_3")
                and any(m.category == target_cat for m in takes))
        )
        size = int(file_meta.get("size") or 0)
        sha = file_meta.get("sha256")
        warnings: List[str] = []
        ok = True

        if not _is_video_meta(file_meta):
            warnings.append(
                f'"{file_meta.get("filename") or "that file"}" doesn\'t look like a video — '
                "upload MP4, MOV, or WEBM."
            )
            ok = False
        if size and size > MAX_SUBMISSION_VIDEO_BYTES:
            cap = MAX_SUBMISSION_VIDEO_BYTES // (1024 * 1024)
            warnings.append(
                f"That file is {size // (1024 * 1024)} MB — the submission video limit is {cap} MB."
            )
            ok = False
        if target_cat == "take" and len(takes) >= MAX_SUBMISSION_TAKES:
            warnings.append(
                f"This submission already has {len(takes)} audition takes "
                f"({MAX_SUBMISSION_TAKES} max) — delete one before adding another."
            )
            ok = False
        if sha:
            dup = next(
                (m for m in (sub.get("media") or []) if m.get("content_sha256") == sha), None
            )
            if dup:
                warnings.append(
                    f"This exact file is already on the submission as "
                    f'"{dup.get("label") or dup.get("category")}" — nothing to upload again.'
                )
                ok = False
        if replaces:
            warnings.append(f"This will replace the current {target_label} on the submission.")

        plan.upload = {
            "filename": file_meta.get("filename"),
            "size": size or None,
            "content_type": file_meta.get("content_type"),
            "sha256": sha,
            "target_category": target_cat,
            "target_label": target_label,
            "replaces": replaces,
        }
        plan.warnings = warnings
        plan.executable = ok
        plan.requires_confirmation = ok
        return Resolved(plan=plan)

    # ---- ATTACH: resolve requested terms against AVAILABLE library media ----
    if not parsed.media_terms:
        return Resolved(error="Which media should I attach — the intro, or portfolio images?")

    proposed: List[SubMedia] = []
    already: List[str] = []
    unresolvable: List[str] = []

    for term in parsed.media_terms:
        if term["kind"] == "intro":
            on_sub = next((m for m in intro), None)
            avail = next((a for a in available if a.category == "intro_video"), None)
            if avail:
                proposed.append(avail)
            elif on_sub:
                already.append("Intro Video")
            else:
                unresolvable.append("Introduction video")
        elif term["kind"] == "take":
            n = term["n"]
            on_sub = next((m for m in takes if re.search(rf"\b{n}\b", m.label)), None)
            if on_sub:
                already.append(on_sub.label)
            else:
                unresolvable.append(f"Audition Take {n}")
        elif term["kind"] == "audition_all":
            if takes:
                already.extend(m.label for m in takes)
            else:
                unresolvable.append("Audition takes")
        elif term["kind"] == "portfolio_all":
            avail_imgs = [a for a in available if a.category == "image"]
            if avail_imgs:
                proposed.extend(avail_imgs)
            elif portfolio:
                already.extend(m.label for m in portfolio)
            else:
                unresolvable.append("Portfolio images")

    warnings: List[str] = []
    if already:
        warnings.append("Already on the submission: " + ", ".join(already) + " — will not be re-attached.")
    for u in unresolvable:
        if u.startswith("Audition"):
            warnings.append(
                f"{u}: audition takes are uploaded straight to a project submission and are not in "
                f"{tlabel}'s profile library, so there's nothing to attach."
            )
        else:
            warnings.append(f"{u}: not found in {tlabel}'s Talent Profile.")

    plan.proposed = proposed
    plan.warnings = warnings
    plan.executable = bool(proposed)
    plan.requires_confirmation = bool(proposed)
    if not proposed:
        # nothing to do — surface why, no confirm button
        return Resolved(plan=plan)
    return Resolved(plan=plan)
