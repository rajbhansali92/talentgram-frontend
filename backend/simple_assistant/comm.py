"""Communication + media RESOLUTION for Simple Assistant (Phase 4A).

Turns a parsed "send / upload" command into an exact, fully server-resolved
`CommunicationPlan` for preview. Nothing here sends anything — external
send lives in comm_execute.py behind an HMAC-signed plan + explicit
confirm.

Reused (read-only) resolution:
  * agents.modules.casting_pipeline_nlu       — name/project fuzzy matching
  * agents.modules.media_assignment.resolve_authoritative_talent_for_upload
      — which duplicate talent record actually owns THIS project's submission
  * routers.whatsapp._resolve_destination     — the app's own group-vs-number rule
  * simple_assistant.commands._active_projects / _match_project / _global_talent_candidates
  * simple_assistant.links_adapter            — existing generated profile link lookup
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from agents.modules import casting_pipeline_nlu as nlu
from agents.modules.media_assignment import resolve_authoritative_talent_for_upload
from routers.whatsapp import _resolve_destination
from simple_assistant import links_adapter
from simple_assistant.comm_plan import (
    ATTACH_MEDIA,
    DEST_GROUP,
    DEST_NUMBER,
    DEST_SUBMISSION,
    SEND_MEDIA,
    SEND_PROFILE_LINK,
    SEND_PROJECT_INFO,
    CommunicationPlan,
    MediaRef,
)
from simple_assistant.readonly_db import RDB

logger = logging.getLogger(__name__)

_TAKE_CATEGORIES = {"take", "take_1", "take_2", "take_3"}
_INTRO_CATEGORIES = {"intro_video"}
_LEGACY_TAKE_LABEL = {"take_1": "Take 1", "take_2": "Take 2", "take_3": "Take 3"}


# --------------------------------------------------------------------------
# Command parsing (deterministic)
# --------------------------------------------------------------------------
_SEND_RE = re.compile(r"\b(?:send|share|forward)\b", re.I)
_UPLOAD_RE = re.compile(r"\b(?:upload|attach|add)\b.+\bsubmission\b", re.I)
_LEAD_VERB_RE = re.compile(r"^\s*(?:can you\s+|could you\s+|please\s+|pls\s+)*(?:send|share|forward|upload|attach|add)\s+", re.I)
_BULK_RE = re.compile(
    r"\b(?:selected\s+talents?|all\s+(?:the\s+)?talents?|every\s+talent|the\s+shortlist|"
    r"multiple\s+talents?|these\s+talents?)\b",
    re.I,
)
_PROFILE_RE = re.compile(r"\bprofile\b", re.I)
_PROJECT_INFO_RE = re.compile(
    r"\b(?:project|requirements?|brief|details|casting\s+call)\b", re.I
)
_TAKE_N_RE = re.compile(r"\b(?:audition\s+)?takes?\s*#?\s*(\d+)\b", re.I)
_AUDITION_ALL_RE = re.compile(r"\baudition(?:\s+takes?)?\b", re.I)
_INTRO_RE = re.compile(r"\b(?:intro(?:duction)?)(?:\s+video)?\b", re.I)
# "Send X's Google AI submission to the casting group" — the whole submission
# (intro + every take). Only a SEND verb; ATTACH already owns "upload ... submission".
_SUBMISSION_ALL_RE = re.compile(r"\b(?:whole\s+|entire\s+|full\s+|complete\s+)?submission\b", re.I)
# "the audition I just uploaded" / "the latest take" / "the one I just sent"
_LATEST_RE = re.compile(
    r"\b(?:just\s+(?:uploaded|sent|added|shared)|i\s+just\s+(?:uploaded|sent|added)|"
    r"(?:the\s+)?(?:latest|most\s+recent|newest|last)\s+(?:audition|take|clip|video|file|one|upload))\b",
    re.I,
)
# no possessive → "... for <Name> ..." names the talent
_FOR_TALENT_RE = re.compile(r"\bfor\s+([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,2})\b")

# "... 's ..." -> talent name is everything before the first possessive.
_POSSESSIVE_RE = re.compile(r"^\s*(.+?)'s\b", re.I)
# "... to <X> casting|production [group|team]" / "... to the casting group"
_CASTING_DEST_RE = re.compile(
    r"\bto\s+(?:the\s+)?(?P<proj>.+?)\s+(?:casting|production)\s*(?:group|team|chat|desk)?\s*(?:[.?!]|$)",
    re.I,
)
_CASTING_DEST_BARE_RE = re.compile(r"\bto\s+(?:the\s+)?casting\s+(?:group|team)\b", re.I)
# "<talent> the <project> project|details|requirements|brief"
_TALENT_INFO_RE = re.compile(
    r"^\s*(?P<talent>.+?)\s+(?:the\s+)?(?P<proj>.+?)\s+(?:project|details|requirements?|brief)\b", re.I
)
# fallback "for|in|on|from|to <project> (submission|project|casting|...)"
_PROJECT_SCOPE_RE = re.compile(
    r"\b(?:for|in|on|from|to)\s+(?:the\s+|her\s+|his\s+|their\s+)?(?P<proj>.+?)"
    r"(?:\s+(?:casting|production|submission|project|group|team|chat))?\s*(?:[.?!]|,\s|$)",
    re.I,
)


def is_comm_command(message: str) -> bool:
    return bool(_SEND_RE.search(message or "") or _UPLOAD_RE.search(message or ""))


class ParsedComm:
    __slots__ = ("kind", "talent_text", "project_text", "want_profile", "want_project_info",
                 "media_terms", "dest_kind", "bulk")

    def __init__(self):
        self.kind = None
        self.talent_text = None
        self.project_text = None
        self.want_profile = False
        self.want_project_info = False
        self.media_terms: List[Dict[str, Any]] = []   # [{"kind":"intro"}|{"kind":"take","n":2}|{"kind":"audition_all"}]
        self.dest_kind = None                          # "casting" | "talent" | "submission"
        self.bulk = False


def parsed_to_dict(p: "ParsedComm") -> Dict[str, Any]:
    return {k: getattr(p, k) for k in ParsedComm.__slots__}


def parsed_from_dict(d: Dict[str, Any]) -> "ParsedComm":
    p = ParsedComm()
    for k in ParsedComm.__slots__:
        setattr(p, k, d.get(k))
    p.media_terms = d.get("media_terms") or []
    return p


_STOP = {"the", "her", "him", "his", "their", "them", "casting", "group", "team",
         "profile", "project", "a", "an", "and", "to", "for", "with"}


def parse_comm(message: str) -> Optional[ParsedComm]:
    msg = (message or "").strip()
    p = ParsedComm()

    if _BULK_RE.search(msg):
        p.bulk = True
        return p

    if _UPLOAD_RE.search(msg):
        p.kind = ATTACH_MEDIA
        p.dest_kind = "submission"
    elif _SEND_RE.search(msg):
        pass
    else:
        return None

    body = _LEAD_VERB_RE.sub("", msg).strip()  # drop "Send " / "Upload " etc.

    # ---- media / content terms ----
    for m in _TAKE_N_RE.finditer(msg):
        p.media_terms.append({"kind": "take", "n": int(m.group(1))})
    if _INTRO_RE.search(msg):
        p.media_terms.append({"kind": "intro"})
    if not p.media_terms and _LATEST_RE.search(msg):
        p.media_terms.append({"kind": "latest_take"})
    if not p.media_terms and p.kind != ATTACH_MEDIA and _SUBMISSION_ALL_RE.search(msg):
        p.media_terms.append({"kind": "submission_all"})
    if not p.media_terms and _AUDITION_ALL_RE.search(msg):
        p.media_terms.append({"kind": "audition_all"})
    if _PROFILE_RE.search(msg):
        p.want_profile = True

    # ---- destination + project ----
    cd = _CASTING_DEST_RE.search(msg)
    if cd:
        p.dest_kind = p.dest_kind or "casting"
        p.project_text = _clean_frag(cd.group("proj"))
    elif _CASTING_DEST_BARE_RE.search(msg):
        p.dest_kind = p.dest_kind or "casting"

    # ---- talent ----
    poss = _POSSESSIVE_RE.match(body)
    if poss:
        p.talent_text = _clean_frag(poss.group(1))
        # "<talent>'s <Project> submission ..." — the project sits between the
        # possessive and the word "submission" (same shape Phase 4B parses).
        rest = body[poss.end():].strip()
        sm = re.match(r"(?P<proj>.+?)\s+submission\b", rest, re.I)
        if sm and not p.project_text:
            cand = _clean_frag(sm.group("proj"))
            if cand and cand.lower() not in _STOP:
                p.project_text = cand

    # no possessive → "... for <Name> ..." (e.g. "the audition I just uploaded for Ahana")
    if not p.talent_text:
        ft = _FOR_TALENT_RE.search(msg)
        if ft:
            cand = _clean_frag(ft.group(1))
            if cand and cand.lower() not in _STOP and (not p.project_text or cand.lower() != p.project_text.lower()):
                p.talent_text = cand

    # "<talent> the <project> project/details" — no possessive, talent's own dest
    if not p.talent_text and p.dest_kind not in ("casting", "submission"):
        ti = _TALENT_INFO_RE.match(body)
        if ti and _PROJECT_INFO_RE.search(body):
            p.talent_text = _clean_frag(ti.group("talent"))
            p.project_text = _clean_frag(ti.group("proj"))
            p.dest_kind = "talent"
            p.want_project_info = True

    # project fallback
    if not p.project_text:
        last = None
        _tl = (p.talent_text or "").lower()
        for m in _PROJECT_SCOPE_RE.finditer(msg):
            c = _clean_frag(m.group("proj"))
            if c and c.lower() not in _STOP and c.lower() != _tl:
                last = c
        p.project_text = last

    if p.kind is None:
        if p.want_project_info:
            p.kind = SEND_PROJECT_INFO
        elif p.want_profile:
            p.kind = SEND_PROFILE_LINK
        else:
            p.kind = SEND_MEDIA
    return p


def _clean_frag(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = re.sub(
        r"\b(?:intro(?:duction)?(?:\s+video)?|audition\s+takes?|takes?\s*#?\s*\d+|"
        r"profile|project|details|requirements?|brief|casting\s+call|and|the|her|his|their)\b",
        " ", s, flags=re.I,
    )
    s = re.sub(r"[.?!,;:'\"]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -")
    return s or None


# --------------------------------------------------------------------------
# Resolution helpers
# --------------------------------------------------------------------------
async def _resolve_submission(project_id: str, candidate_ids: List[str]):
    """→ (submission_doc | None, authoritative_talent_id | None, error_str | None)"""
    if len(candidate_ids) == 1:
        sub = await RDB.submissions.find_one(
            {"project_id": project_id, "talent_id": candidate_ids[0]}, {"_id": 0}
        )
        return sub, candidate_ids[0], (None if sub else "no_submission")
    auth = await resolve_authoritative_talent_for_upload(project_id, candidate_ids)
    if not auth.ok:
        return None, None, auth.error
    sub = await RDB.submissions.find_one(
        {"project_id": project_id, "talent_id": auth.talent_id}, {"_id": 0}
    )
    return sub, auth.talent_id, (None if sub else "no_submission")


def _media_of(submission: dict, term: Dict[str, Any]) -> Tuple[Optional[MediaRef], Optional[str]]:
    """Resolve ONE requested media term against the submission's canonical
    media list. Returns (MediaRef, None) or (None, missing_label)."""
    items = [m for m in (submission.get("media") or []) if isinstance(m, dict) and m.get("url")]
    kind = term["kind"]

    if kind == "intro":
        hit = next((m for m in items if (m.get("category") or "").lower() in _INTRO_CATEGORIES), None)
        return (_mref(hit, "Intro Video", "video"), None) if hit else (None, "Intro Video")

    takes = [m for m in items if (m.get("category") or "").lower() in _TAKE_CATEGORIES]
    takes.sort(key=lambda m: (str(m.get("category")), str(m.get("created_at") or "")))

    if kind == "latest_take":
        if not takes:
            return None, "the audition you just uploaded"
        newest = max(takes, key=lambda m: str(m.get("created_at") or ""))
        return _mref(newest, newest.get("label") or "Latest audition take", "video"), None

    if kind == "take":
        n = term["n"]
        # 1) legacy fixed slot
        legacy = next((m for m in takes if (m.get("category") or "").lower() == f"take_{n}"), None)
        if legacy:
            return _mref(legacy, f"Audition Take {n}", "video"), None
        # 2) a `take` whose label carries that number ("Take 2" / "Audition Take 2")
        labelled = next(
            (m for m in takes if re.search(rf"\b{n}\b", str(m.get("label") or ""))
             and (m.get("category") or "").lower() == "take"),
            None,
        )
        if labelled:
            return _mref(labelled, labelled.get("label") or f"Audition Take {n}", "video"), None
        # 3) positional Nth take
        if 1 <= n <= len(takes):
            return _mref(takes[n - 1], (takes[n - 1].get("label") or f"Audition Take {n}"), "video"), None
        return None, f"Audition Take {n}"

    if kind == "audition_all":
        return None, None  # handled by caller (expands to every take)

    return None, term.get("kind")


def _mref(m: dict, label: str, kind: str) -> MediaRef:
    from core import video_poster_url

    return MediaRef(
        media_id=m.get("id"),
        category=(m.get("category") or "").lower(),
        label=(m.get("label") or label),
        url=m.get("url"),
        poster_url=m.get("poster_url") or video_poster_url(m.get("public_id")) or video_poster_url(m.get("url")),
        kind=kind,
        public_id=m.get("public_id"),
    )


async def _casting_group(project_id: str) -> Tuple[Optional[str], Optional[str]]:
    """→ (group_name | None, error). Canonical: project.whatsapp_casting_group_name
    (a single field — there is no data path to two groups for one project)."""
    doc = await RDB.projects.find_one(
        {"id": project_id}, {"_id": 0, "whatsapp_casting_group_name": 1}
    )
    g = ((doc or {}).get("whatsapp_casting_group_name") or "").strip()
    return (g, None) if g else (None, "no_casting_group")


async def _talent_own_destination(talent_id: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """The app's OWN group-vs-number rule. → (destination, destination_type, reason)."""
    t = await RDB.talents.find_one(
        {"id": talent_id}, {"_id": 0, "whatsapp_group_name": 1, "phone": 1, "name": 1}
    ) or {}
    dtype, dest, reason = _resolve_destination(t)
    if dtype == "group":
        return dest, DEST_GROUP, None
    if dtype == "number":
        return dest, DEST_NUMBER, None
    return None, None, reason or "no_whatsapp_destination"


def _project_info_message(project: dict, talent_label: str) -> str:
    bits = [f"*{project.get('brand_name') or 'Project'}*"]
    if project.get("character"):
        bits.append(f"Requirement: {project['character']}")
    if project.get("shoot_dates"):
        bits.append(f"Shoot: {project['shoot_dates']}")
    if project.get("budget_per_day"):
        bits.append(f"Budget: {project['budget_per_day']}")
    if project.get("medium_usage"):
        bits.append(f"Usage: {project['medium_usage']}")
    return f"Hi {talent_label.split()[0]}, sharing a project you may be a fit for:\n\n" + "\n".join(bits)


class Resolved:
    """Outcome of build_plan: exactly one of plan / error / clarification."""
    def __init__(self, *, plan=None, error=None, clarification=None):
        self.plan = plan
        self.error = error
        self.clarification = clarification


async def _resolve_send_destination(
    *, parsed: "ParsedComm", project_id: str, project_label: Optional[str],
    talent_id: str, tlabel: str, forced: Optional[Dict[str, Any]],
):
    """The WhatsApp destination for a SEND_MEDIA / SEND_PROFILE_LINK plan.

    Reuses the app's own rules: ``project.whatsapp_casting_group_name`` for
    the casting group, ``routers.whatsapp._resolve_destination`` (via
    ``_talent_own_destination``) for the talent's own group/number. Never
    invents a destination.

    → (destination, dtype, dlabel, source, clarification|None, error|None)
    """
    if forced:
        return (forced["destination"], forced["destination_type"], forced["destination_label"],
                forced["destination_source"], None, None)

    group, gerr = await _casting_group(project_id)

    if parsed.dest_kind == "casting":
        # the command explicitly said "casting group" — no ambiguity
        if gerr:
            return None, None, None, None, None, f"{project_label} has no WhatsApp casting group configured."
        return group, DEST_GROUP, f"{project_label} Casting", "project_casting_group", None, None

    own_dest, own_dtype, _own_reason = await _talent_own_destination(talent_id)

    # genuine ambiguity: a project casting group AND a distinct talent-own group
    if group and own_dest and own_dtype == DEST_GROUP and own_dest != group:
        clar = {
            "kind": "destination",
            "prompt": f"I found two possible WhatsApp destinations for {tlabel}. Which one should I use?",
            "options": [
                {"index": 1, "id": "project_casting_group", "label": f"{project_label} Casting",
                 "destination": group, "destination_type": DEST_GROUP,
                 "destination_label": f"{project_label} Casting", "destination_source": "project_casting_group"},
                {"index": 2, "id": "talent_own", "label": own_dest,
                 "destination": own_dest, "destination_type": DEST_GROUP,
                 "destination_label": f"{tlabel}'s WhatsApp group", "destination_source": "talent_own"},
            ],
        }
        return None, None, None, None, clar, None

    if group:
        return group, DEST_GROUP, f"{project_label} Casting", "project_casting_group", None, None
    if own_dest:
        dlabel = f"{tlabel}'s WhatsApp group" if own_dtype == DEST_GROUP else f"{tlabel}'s number"
        return own_dest, own_dtype, dlabel, "talent_own", None, None
    return None, None, None, None, None, (
        f"{project_label} has no WhatsApp casting group configured, and {tlabel} "
        "has no saved WhatsApp group or number."
    )


async def build_plan(
    *, parsed: "ParsedComm", talent: Dict[str, str], project_id: Optional[str],
    project_label: Optional[str], talent_candidate_ids: List[str],
    forced_destination: Optional[Dict[str, Any]] = None,
) -> Resolved:
    """Build a CommunicationPlan for an already-resolved talent (+ optional
    project). `talent_candidate_ids` is every id the name matched (for the
    authoritative-submission tie-break)."""
    tlabel = talent["label"]

    # ---- SEND_PROJECT_INFO — talent's OWN destination ----
    if parsed.kind == SEND_PROJECT_INFO:
        if not project_id:
            return Resolved(error="Which project's details should I send?")
        proj = await RDB.projects.find_one({"id": project_id}, {"_id": 0})
        dest, dtype, reason = await _talent_own_destination(talent["id"])
        warnings: List[str] = []
        if not dest:
            if reason and "phone" in reason.lower():
                pass
            return Resolved(error=(
                f"{tlabel} doesn't have a saved WhatsApp group or number."
                if "no whatsapp" in (reason or "").lower() or "no whatsapp_group" in (reason or "").lower()
                else f"{tlabel} doesn't have a saved Talentgram WhatsApp group. I can't send this yet."
            ))
        if dtype == DEST_NUMBER:
            warnings.append(f"{tlabel} has no saved Talentgram WhatsApp group — this would go to their number.")
        plan = CommunicationPlan(
            action_type=SEND_PROJECT_INFO,
            talent={"id": talent["id"], "label": tlabel},
            project={"id": project_id, "label": project_label},
            destination=dest, destination_type=dtype,
            destination_label=(f"{tlabel}'s WhatsApp group" if dtype == DEST_GROUP else f"{tlabel}'s number"),
            message=_project_info_message(proj or {}, tlabel),
            warnings=warnings, executable=True,
        )
        return Resolved(plan=plan)

    # ---- everything else needs a project ----
    if not project_id:
        return Resolved(error="Which project is this for?")

    # ---- SEND_PROFILE_LINK ----
    if parsed.kind == SEND_PROFILE_LINK:
        dest, dtype, dlabel, source, clar, derr = await _resolve_send_destination(
            parsed=parsed, project_id=project_id, project_label=project_label,
            talent_id=talent["id"], tlabel=tlabel, forced=forced_destination,
        )
        if derr:
            return Resolved(error=derr)
        link = await links_adapter.best_profile_link(talent["id"])
        if not link:
            return Resolved(error=(
                f"{tlabel} doesn't currently have a generated profile link. "
                "I can generate one using Talentgram's existing profile-link system, "
                "but generating links isn't enabled in this phase."
            ))
        if clar:
            plan_d = CommunicationPlan(
                action_type=SEND_PROFILE_LINK, talent={"id": talent["id"], "label": tlabel},
                project={"id": project_id, "label": project_label}, profile_link=link,
                message=f"{tlabel} — Talentgram profile\n{link['url']}", executable=False,
            )
            return Resolved(plan=plan_d, clarification=clar)
        plan = CommunicationPlan(
            action_type=SEND_PROFILE_LINK,
            talent={"id": talent["id"], "label": tlabel},
            project={"id": project_id, "label": project_label},
            destination=dest, destination_type=dtype,
            destination_label=dlabel, destination_source=source,
            profile_link=link,
            message=f"{tlabel} — Talentgram profile\n{link['url']}",
            executable=True,
        )
        return Resolved(plan=plan)

    # ---- SEND_MEDIA / ATTACH_MEDIA — need the submission + media ----
    sub, auth_tid, sub_err = await _resolve_submission(project_id, talent_candidate_ids or [talent["id"]])
    if sub_err == "ambiguous_submission":
        return Resolved(error=(
            f"More than one talent record named {tlabel} has a {project_label} submission — "
            "please resolve the duplicate records first."
        ))
    if not sub:
        return Resolved(error=f"I couldn't find a {project_label} submission for {tlabel}.")

    if not parsed.media_terms:
        return Resolved(error="Which media should I send — the intro, or a specific audition take?")

    # intro first, then takes in numeric order (nice preview order)
    ordered = sorted(
        parsed.media_terms,
        key=lambda t: (0, 0) if t["kind"] == "intro" else (1, t.get("n", 99)) if t["kind"] == "take" else (2, 0),
    )

    def _all_takes() -> List[dict]:
        ts = [m for m in (sub.get("media") or []) if isinstance(m, dict)
              and (m.get("category") or "").lower() in _TAKE_CATEGORIES and m.get("url")]
        ts.sort(key=lambda m: (str(m.get("category")), str(m.get("created_at") or "")))
        return ts

    media: List[MediaRef] = []
    missing: List[str] = []
    _seen_ids: set = set()

    def _add(ref: MediaRef):
        if ref.media_id and ref.media_id in _seen_ids:
            return
        _seen_ids.add(ref.media_id)
        media.append(ref)

    for term in ordered:
        if term["kind"] in ("audition_all", "submission_all"):
            if term["kind"] == "submission_all":
                intro = next((m for m in (sub.get("media") or []) if isinstance(m, dict)
                              and (m.get("category") or "").lower() in _INTRO_CATEGORIES and m.get("url")), None)
                if intro:
                    _add(_mref(intro, "Intro Video", "video"))
            takes = _all_takes()
            for i, m in enumerate(takes, 1):
                _add(_mref(m, m.get("label") or f"Audition Take {i}", "video"))
            if not takes and term["kind"] == "audition_all":
                missing.append("Audition takes")
            if term["kind"] == "submission_all" and not media:
                missing.append("submission media")
            continue
        ref, miss = _media_of(sub, term)
        if ref:
            _add(ref)
        elif miss:
            missing.append(miss)

    if parsed.kind == ATTACH_MEDIA:
        plan = CommunicationPlan(
            action_type=ATTACH_MEDIA,
            talent={"id": auth_tid or talent["id"], "label": tlabel},
            project={"id": project_id, "label": project_label},
            submission_id=sub.get("id"),
            destination_type=DEST_SUBMISSION,
            destination_label=f"{project_label} submission",
            media=media,
            warnings=(["Missing: " + ", ".join(missing)] if missing else [])
            + ["Attaching media to a submission isn't enabled in this phase — this is a preview only."],
            executable=False,
        )
        return Resolved(plan=plan)

    # SEND_MEDIA
    if not media and missing:
        return Resolved(error=(
            f"I couldn't find {' or '.join(missing)} for {tlabel}'s {project_label} submission."
        ))

    dest, dtype, dlabel, source, dest_clar, derr = await _resolve_send_destination(
        parsed=parsed, project_id=project_id, project_label=project_label,
        talent_id=auth_tid or talent["id"], tlabel=tlabel, forced=forced_destination,
    )
    if derr:
        return Resolved(error=derr)
    if dest_clar:
        plan_d = CommunicationPlan(
            action_type=SEND_MEDIA, talent={"id": auth_tid or talent["id"], "label": tlabel},
            project={"id": project_id, "label": project_label}, submission_id=sub.get("id"),
            media=media, message=f"{tlabel} — {project_label}", executable=False,
        )
        return Resolved(plan=plan_d, clarification=dest_clar)

    warnings = []
    clarification = None
    if media and missing:
        # partial availability -> clarification, never silent substitution
        clarification = {
            "kind": "media_partial",
            "prompt": f"{', '.join(m.label for m in media)} available, but {', '.join(missing)} missing. "
                      f"Send only what's available?",
            "options": [
                {"index": 1, "id": "send_available", "label": f"Send only {', '.join(m.label for m in media)}"},
                {"index": 2, "id": "cancel", "label": "Cancel"},
            ],
        }

    if dtype == DEST_NUMBER:
        warnings.append(
            f"{project_label} has no WhatsApp casting group — this would go to {tlabel}'s number."
        )

    plan = CommunicationPlan(
        action_type=SEND_MEDIA,
        talent={"id": auth_tid or talent["id"], "label": tlabel},
        project={"id": project_id, "label": project_label},
        submission_id=sub.get("id"),
        destination=dest, destination_type=dtype,
        destination_label=dlabel, destination_source=source,
        media=media,
        message=f"{tlabel} — {project_label}",
        warnings=warnings,
        executable=bool(media) and not clarification,
    )
    return Resolved(plan=plan, clarification=clarification)
