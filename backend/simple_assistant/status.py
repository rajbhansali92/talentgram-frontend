"""WhatsApp delivery-status inspection for Simple Assistant (Phase 6).

READ-ONLY. Nothing here sends, retries, replies, or mutates any WhatsApp
job/batch/worker/session state. It reads the EXISTING engine's own
collections:

  * whatsapp_agent_audit_log  — the Phase 4A/5 `sa_action` rows that tie a
      (talent, project, plan_id) to the `batch_ids` a send created.
  * whatsapp_jobs             — the worker's own per-job status
      (pending / sending / sent [+verification_status] / failed / skipped)
      and its own `sent_at` / `error_message` fields.
  * whatsapp_batches          — batch-level rollup (created_at, status).

No status value is invented — the vocabulary is the worker's.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from simple_assistant.readonly_db import RDB

logger = logging.getLogger(__name__)

AGENT_ID = "simple-assistant"

# ---- command detection --------------------------------------------------
_STATUS_RE = re.compile(
    r"\b(?:"
    r"status\s+of\b|"
    r"what\s+happened\s+to\b|"
    r"(?:get|got|was|were|been|actually)\s+(?:it\s+)?sent\b|"
    r"go(?:ne)?\s+through\b|went\s+through\b|"
    r"delivery\s+status\b|"
    r"whatsapp\s+(?:status|activity|sends?|messages?|deliver)\b|"
    r"(?:messages?|sends?)\s+(?:that\s+are\s+|are\s+)?pending\b|"
    r"pending\s+(?:whatsapp\s+)?(?:messages?|sends?)\b|"
    r"check\s+(?:the\s+)?(?:latest\s+)?(?:casting\s+)?send\b|"
    r"did\b[^?]*\b(?:message|send|audition|casting|whatsapp)\b[^?]*\b(?:go|sent|deliver|through|arrive)\b"
    r")",
    re.I,
)
_INBOUND_RE = re.compile(
    r"\b(?:"
    r"did\b[^?]*\brepl(?:y|ied)\b|did\b[^?]*\brespond\b|"
    r"what\s+did\b[^?]*\b(?:say|said|reply|replied|respond|write|wrote)\b|"
    r"(?:'s|his|her|their)\s+(?:latest\s+|last\s+)?repl(?:y|ies)\b|"
    r"any\s+repl(?:y|ies)\b|has\b[^?]*\breplied\b|"
    r"show\b[^?]*\brepl(?:y|ies)\b|latest\s+repl(?:y|ies)\b"
    r")",
    re.I,
)
_RETRY_RE = re.compile(
    r"\bretry\b|\btry\s+(?:it\s+|that\s+)?again\b|\bre-?send\s+(?:it|that)\b|"
    r"\bsend\s+(?:it|that)\s+again\b|\bresend\s+it\b",
    re.I,
)
_UNANSWERED_RE = re.compile(r"\bunanswered\b|\bwithout\s+(?:a\s+)?repl(?:y|ies)\b|\bno\s+reply\b|\bawaiting\s+repl", re.I)


_LEAD_STRIP_RE = re.compile(
    r"^\s*(?:can\s+you|could\s+you|please|pls|hey|so|ok|okay|"
    r"did|do|does|has|have|is|was|were|will|what\s+happened\s+to|"
    r"what's|what\s+is|what\s+was|whats|what|check|show\s+me|show|tell\s+me|give\s+me|"
    r"any|the|a|an|latest|recent|whatsapp|me|status\s+of|status)\b[\s,]*",
    re.I,
)
_POSSESSIVE_RE = re.compile(r"\b([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,2})'s\b")
# "for <Name>" names the talent — but NOT "replies/messages/sends for <Project>"
_FOR_TALENT_RE = re.compile(
    r"(?<!reply )(?<!replies )(?<!send )(?<!sends )(?<!message )(?<!messages )(?<!updates )"
    r"\bfor\s+([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,2})\b"
)
# "Did Ahana reply" / "has Ahana replied" / "what did Ahana say"
_INBOUND_SUBJECT_RE = re.compile(
    r"\b(?:did|has|have|when\s+did|what\s+did)\s+"
    r"([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,2})\s+"
    r"(?:reply|repli|respond|say|said|write|wrote|message|text)",
    re.I,
)
_PROJECT_RE = re.compile(
    r"\b(?:for|to|on|about|of|from)\s+(?:the\s+)?(?P<p>[A-Z][\w.'&\- ]{1,40}?)"
    r"(?:\s+(?:casting|whatsapp|send|sends|message|messages|submission|group|project|activity|reply|replies))?"
    r"\s*(?:[.?!,]|$)",
    re.I,
)
_PROJECT_TAIL_RE = re.compile(
    r"\b(?:the\s+)?(?P<p>[A-Z][\w.'&\- ]{1,40}?)\s+(?:casting\s+)?"
    r"(?:whatsapp\s+)?(?:send|sends|message|messages|casting)\b",
    re.I,
)
_STOP = {"the", "her", "his", "their", "a", "an", "whatsapp", "casting", "latest", "recent"}


def parse_status(message: str):
    """→ (talent_text | None, project_text | None, is_inbound: bool)"""
    raw = (message or "").strip()
    msg = raw
    for _ in range(6):                       # peel leading question/command words
        stripped = _LEAD_STRIP_RE.sub("", msg, count=1)
        if stripped == msg:
            break
        msg = stripped
    inbound = is_inbound_query(raw)
    talent = None
    subj = _INBOUND_SUBJECT_RE.search(raw)
    if subj and subj.group(1).strip().lower() not in _STOP:
        talent = subj.group(1).strip()
    poss = _POSSESSIVE_RE.search(msg)
    if poss and poss.group(1).strip().lower() not in _STOP:
        if not talent:
            talent = poss.group(1).strip()
        msg = (msg[:poss.start()] + " " + msg[poss.end():]).strip()  # drop "<talent>'s"
    if not talent:
        ft = _FOR_TALENT_RE.search(msg)
        if ft and ft.group(1).strip().lower() not in _STOP:
            talent = ft.group(1).strip()

    project = None
    mt = _PROJECT_TAIL_RE.search(msg)
    if mt:
        cand = mt.group("p").strip()
        if cand.lower() not in _STOP and cand.lower() != (talent or "").lower():
            project = cand
    if not project:
        for m in _PROJECT_RE.finditer(msg):
            cand = m.group("p").strip()
            if cand and cand.lower() not in _STOP and cand.lower() != (talent or "").lower():
                project = cand
    return talent, project, inbound


def is_status_query(message: str) -> bool:
    m = message or ""
    return bool(_STATUS_RE.search(m) or _INBOUND_RE.search(m))


def is_inbound_query(message: str) -> bool:
    m = message or ""
    return bool(_INBOUND_RE.search(m) or (_UNANSWERED_RE.search(m) and re.search(r"\brepl", m, re.I)))


def wants_unanswered(message: str) -> bool:
    return bool(_UNANSWERED_RE.search(message or ""))


def _mask_phone(p: Optional[str]) -> str:
    d = "".join(ch for ch in (p or "") if ch.isdigit())
    if len(d) < 6:
        return p or "unknown number"
    return f"+{d[:2]} {d[2:5]}XXX{'X' * max(0, len(d) - 10)} {d[-2:]}"


# ---- inbound-message card rendering (Phase 7) --------------------------
_TALENT_RES_COPY = {
    "resolved": "Talent",
    "unresolved": "Talent could not be identified from the sender's number.",
    "ambiguous": "Sender's number matches more than one talent record.",
}
_PROJECT_RES_COPY = {
    "casting_group": "Project (from the casting group)",
    "recent_outbound": "Project (from a recent casting send)",
    "ambiguous_project": "Project could not be determined reliably.",
    "unresolved": "No project context.",
    "none": "No project context.",
}


async def _label_for_talent(tid: Optional[str]) -> Optional[str]:
    if not tid:
        return None
    d = await RDB.talents.find_one({"id": tid}, {"_id": 0, "name": 1})
    return (d or {}).get("name")


async def _label_for_project(pid: Optional[str]) -> Optional[str]:
    if not pid:
        return None
    d = await RDB.projects.find_one({"id": pid}, {"_id": 0, "brand_name": 1})
    return (d or {}).get("brand_name")


async def build_inbound_card(msg: dict) -> dict:
    tlabel = await _label_for_talent(msg.get("talent_id"))
    plabel = await _label_for_project(msg.get("project_id"))
    cand_labels = []
    for pid in (msg.get("project_candidates") or [])[:5]:
        lbl = await _label_for_project(pid)
        cand_labels.append({"id": pid, "label": lbl or pid})
    return {
        "inbound_id": msg.get("id"),
        "talent": ({"id": msg["talent_id"], "label": tlabel} if msg.get("talent_id") and tlabel else None),
        "talent_resolution": msg.get("talent_resolution"),
        "sender_phone_masked": _mask_phone(msg.get("sender_phone")),
        "sender_name": msg.get("sender_name"),
        "project": ({"id": msg["project_id"], "label": plabel} if msg.get("project_id") and plabel else None),
        "project_resolution": msg.get("project_resolution"),
        "project_candidates": cand_labels,
        "group_name": msg.get("group_name"),
        "received_at": msg.get("received_at"),
        "message_text": msg.get("message_text") or "",     # verbatim (STEP 13)
        "source": "WhatsApp",
    }


def inbound_list_rows(cards: List[dict]) -> List[dict]:
    rows = []
    for c in cards:
        ts = c.get("received_at")
        rows.append({
            "when": ts.strftime("%b %d, %H:%M") if hasattr(ts, "strftime") else str(ts),
            "talent": (c["talent"]["label"] if c.get("talent") else "Unresolved"),
            "project": (c["project"]["label"] if c.get("project") else "—"),
            "message": (c.get("message_text") or "")[:120],
        })
    return rows


def is_retry_request(message: str) -> bool:
    return bool(_RETRY_RE.search(message or ""))


# ---- canonical status vocabulary (the worker's own) --------------------
_VOCAB = ("queued", "pending", "sending", "sent", "verified", "failed", "skipped")
# worst-first — the overall status of a multi-file send is its least-complete part
_OVERALL_ORDER = ("failed", "sending", "queued", "sent", "verified", "skipped")

_STATUS_COPY = {
    "queued": "Queued with the WhatsApp Engine — not confirmed as sent yet.",
    "pending": "Queued with the WhatsApp Engine — not confirmed as sent yet.",
    "sending": "Currently being processed by the WhatsApp worker.",
    "sent": "WhatsApp reports the message as sent.",
    "verified": "The WhatsApp worker reports the message as sent and verified.",
    "failed": "The send failed.",
    "skipped": "The send was skipped (the batch was cancelled).",
    "unknown": "The WhatsApp Engine hasn't reported a status for this yet.",
}


def _job_status(job: dict) -> str:
    s = (job.get("status") or "").lower()
    if s == "sent" and (job.get("verification_status") or "").lower() == "verified":
        return "verified"
    if s in ("pending", "dry_run_preview", "dry_run_complete"):
        return "queued"
    if s in _VOCAB:
        return s
    return s or "unknown"


def _overall(statuses: List[str]) -> str:
    present = set(statuses)
    for k in _OVERALL_ORDER:
        if k in present:
            return k
    return "unknown"


# ---- send-record lookup (talent + project → the sends) -----------------
async def find_sends(*, talent_id: Optional[str], project_id: Optional[str], limit: int = 12) -> List[dict]:
    """Every Simple-Assistant WhatsApp send that actually queued a batch,
    newest first, from the existing audit trail. Scoped by whichever of
    (talent_id, project_id) is known."""
    q: Dict[str, Any] = {"agent_id": AGENT_ID, "sa_action.action_type": "send_media"}
    if talent_id:
        q["sa_action.talent_id"] = talent_id
    if project_id:
        q["sa_action.project_id"] = project_id
    rows = await RDB.whatsapp_agent_audit_log.find(q, {"_id": 0}).sort("timestamp", -1).to_list(80)
    sends = [r for r in rows if (r.get("sa_action") or {}).get("batch_ids")]
    return sends[:limit]


def _send_summary(row: dict) -> dict:
    sa = row.get("sa_action") or {}
    labels = [o.get("label") for o in (sa.get("outcomes") or []) if o.get("label")]
    return {
        "plan_id": sa.get("plan_id"),
        "timestamp": row.get("timestamp"),
        "talent_id": sa.get("talent_id"),
        "talent_label": sa.get("talent_label"),
        "project_id": sa.get("project_id"),
        "project_label": sa.get("project_label"),
        "destination": sa.get("destination"),
        "destination_type": sa.get("destination_type"),
        "batch_ids": sa.get("batch_ids") or [],
        "media_labels": labels,
    }


async def build_status_card(row: dict) -> dict:
    """Resolve one send (a signed/verified audit row) to a live status card,
    reading whatsapp_jobs + whatsapp_batches — the worker's own state."""
    s = _send_summary(row)
    batch_ids = s["batch_ids"]
    jobs = await RDB.whatsapp_jobs.find(
        {"batch_id": {"$in": batch_ids}},
        {"_id": 0, "id": 1, "batch_id": 1, "status": 1, "verification_status": 1,
         "sent_at": 1, "created_at": 1, "error_message": 1, "attempt_count": 1,
         "message_body": 1, "destination": 1, "talent_id": 1},
    ).to_list(200)

    job_views = []
    for j in jobs:
        st = _job_status(j)
        job_views.append({
            "status": st,
            "sent_at": j.get("sent_at"),
            "created_at": j.get("created_at"),
            "error_message": j.get("error_message"),
            "attempts": j.get("attempt_count") or 0,
            "message_body": j.get("message_body") or "",
        })

    statuses = [jv["status"] for jv in job_views]
    overall = _overall(statuses) if statuses else "unknown"

    # per-media status: match the send's media labels to the job that carried them
    media = []
    for label in s["media_labels"]:
        jv = next((x for x in job_views if label and label in x["message_body"]), None)
        media.append({"label": label, "status": jv["status"] if jv else overall})

    failures = [jv for jv in job_views if jv["status"] == "failed"]
    queued_at = min((jv["created_at"] for jv in job_views if jv["created_at"]), default=None)
    sent_at = max((jv["sent_at"] for jv in job_views if jv["sent_at"]), default=None)

    return {
        "plan_id": s["plan_id"],
        "talent": {"id": s["talent_id"], "label": s["talent_label"]},
        "project": {"id": s["project_id"], "label": s["project_label"]},
        "destination": s["destination"],
        "destination_type": s["destination_type"],
        "queued_by_assistant_at": s["timestamp"],
        "media": media,
        "overall": overall,
        "overall_copy": _STATUS_COPY.get(overall, _STATUS_COPY["unknown"]),
        "jobs": [{k: v for k, v in jv.items() if k != "message_body"} for jv in job_views],
        "job_count": len(job_views),
        "queued_at": queued_at,
        "sent_at": sent_at,
        "failure_reason": (failures[0]["error_message"] if failures else None),
        "failed_count": len(failures),
    }


def send_options(sends: List[dict]) -> List[dict]:
    """Clarification options for 'which send'."""
    out = []
    for i, row in enumerate(sends, 1):
        s = _send_summary(row)
        ts = s["timestamp"]
        when = ts.strftime("%b %d, %H:%M") if hasattr(ts, "strftime") else str(ts)
        media = ", ".join(s["media_labels"]) or "media"
        out.append({"index": i, "id": s["plan_id"], "plan_id": s["plan_id"],
                    "label": f"{when} — {media}"})
    return out


async def recent_activity_rows(sends: List[dict]) -> List[dict]:
    """Compact list for 'show me the <project> sends' / 'latest activity for <talent>'."""
    rows = []
    for row in sends:
        card = await build_status_card(row)
        ts = card["queued_by_assistant_at"]
        rows.append({
            "when": ts.strftime("%b %d, %H:%M") if hasattr(ts, "strftime") else str(ts),
            "talent": card["talent"]["label"],
            "project": card["project"]["label"],
            "destination": card["destination"],
            "media": ", ".join(m["label"] for m in card["media"] if m["label"]),
            "status": card["overall"],
        })
    return rows


# ---- inbound (STEP 15) ------------------------------------------------
# Inspection result: the existing WhatsApp infrastructure does NOT persist a
# reliable, talent+project-linked inbound message history that Simple
# Assistant can query. Inbound text only reaches `whatsapp_agent_audit_log`
# for groups the Agent Registry actively monitors, carries no talent_id /
# project_id, and the inbound listener is itself optional. Per Phase 6
# STEP 15 we do NOT build a new inbound architecture — we report the limit.
INBOUND_UNAVAILABLE = (
    "The current WhatsApp infrastructure doesn't expose reliable inbound-message "
    "history to Simple Assistant yet. Inbound replies are only captured for casting "
    "groups the WhatsApp Agent actively monitors and aren't linked to a specific "
    "talent and project, so I can't reliably show you a talent's reply. "
    'I can show delivery status for outbound sends — try "Did Ahana\'s Google AI send go through?"'
)
