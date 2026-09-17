"""Phase 8 — READ-ONLY deterministic intelligence over the canonical inbound
WhatsApp history (backend/inbound_messages.py, Phase 7).

For an inbound message this layer determines, conservatively:
  1. who sent it            (Phase 7 resolution — never re-guessed here)
  2. which project it's about (Phase 7 resolution — never re-guessed here)
  3. what it literally says  (verbatim, preserved)
  4. a coarse communication topic  (deterministic keyword classifier)
  5. simple extracted signals      (question? date? availability? interest?)
  6. what authoritative project info already exists for that topic
  7. answerability + a DISPLAY-ONLY proposed response

Hard boundaries (enforced by having no such imports / calls here):
  * NO LLM / model / embedding / vector store
  * NO outbound send, batch, job, Playwright, WhatsApp Engine
  * NO pipeline / submission / talent / project / Cloudinary mutation
  * NO automatic reply, forward, negotiation, reminder

The interpretation is METADATA. The original message stays authoritative.
Feature-flagged: SA_INBOUND_INTELLIGENCE_ENABLED (default OFF).
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

# ---- feature flag -----------------------------------------------------
def is_enabled() -> bool:
    return os.environ.get("SA_INBOUND_INTELLIGENCE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


# ---- command detection (which SA queries want analysis, not a plain card) --
_INTEL_RE = re.compile(
    r"\b(?:"
    r"why\s+did\b[^?]*\b(?:message|text|reach\s+out|write|contact)|"
    r"what\s+(?:is|was|are)\b[^?]*\bask(?:ing)?\b|what\s+did\b[^?]*\bask\b|"
    r"analy[sz]e\b|analysis\s+of\b|break\s+down\b[^?]*\bmessage\b|"
    r"did\b[^?]*\bask\s+about\b|does\b[^?]*\bhave\s+a\s+question\b|"
    r"what\s+(?:is|was)\b[^?]*\babout\b[^?]*\bmessage\b|"
    r"latest\s+questions?\b|unanswered\s+questions?\b|"
    r"what\s+.*\bquestions?\s+from\b"
    r")",
    re.I,
)
_INTEL_SUBJECT_RE = re.compile(
    r"(?i:\b(?:why\s+did|what\s+is|what\s+was|what\s+are|what\s+did|what's|does|did|analy[sz]e|show\s+me\s+what))\s+"
    r"([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,2})(?:'s)?\b",
)
_TOPIC_HINT_RE = re.compile(
    r"\bask(?:ing|ed)?\s+about\s+(?:the\s+)?(?P<t>budget|shoot\s*date|date|location|usage|"
    r"payment|commission|audition|submission|project|availability)\b", re.I,
)


_DRAFT_RE = re.compile(
    r"\b(?:draft|write|compose|suggest|prepare|generate)\b[^?]*\b(?:a\s+)?(?:reply|response|message)\b|"
    r"\breply\s+to\s+[A-Z][\w'\-]+|\breply\s+to\b[^?]*\b(?:message|whatsapp|latest|question)\b|"
    r"\bhow\s+(?:should|do)\s+i\s+(?:respond|reply)\b|"
    r"\bwhat\s+should\s+i\s+(?:tell|say\s+to|reply\s+to|send)\b|"
    r"\bdraft\s+(?:a\s+)?(?:reply|response)\s+to\b",
    re.I,
)


# Phase 12 — read-only conversation-history inspection ("show me X's recent
# conversation about Y", "what has X said about Y", "recent messages before X's
# latest question"). Deterministic, bounded, talent + project scoped.
_CONVO_RE = re.compile(
    r"\b(?:recent\s+conversation|conversation\s+(?:with|history)|"
    r"conversation\s+about|what\s+(?:has|have|did)\b[^?]*\bsaid\s+about\b|"
    r"recent\s+messages\b[^?]*\b(?:before|about|with|from)\b|"
    r"message\s+(?:history|thread)\b|show\s+(?:me\s+)?the\s+(?:recent\s+)?(?:messages|conversation|thread)\b|"
    r"what'?s?\s+been\s+said\b)",
    re.I,
)


def is_conversation_query(message: str) -> bool:
    """A request to SEE the recent back-and-forth — not to analyse one
    message and not to draft a reply."""
    m = message or ""
    if _DRAFT_RE.search(m):
        return False
    return bool(_CONVO_RE.search(m))


def is_intelligence_query(message: str) -> bool:
    return bool(_INTEL_RE.search(message or "") or _DRAFT_RE.search(message or ""))


def is_draft_request(message: str) -> bool:
    m = message or ""
    # "Did Ahana reply to Google AI?" is a Phase-7 question, not a draft order.
    from simple_assistant.status import _INBOUND_RE
    if _INBOUND_RE.search(m):
        return False
    return bool(_DRAFT_RE.search(m))


_DRAFT_SUBJECT_RE = re.compile(
    r"(?i:\b(?:reply|respond|response|message)\s+to|\btell|\bsay\s+to|\bfor)\s+"
    r"([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,2})(?:'s)?\b",
)
# Phase 12 — "conversation with Ahana", "what has Ahana said", "messages from Ahana"
_CONVO_SUBJECT_RE = re.compile(
    r"(?i:\b(?:conversation|messages?|thread|chat)\s+(?:with|from)|\bwhat\s+(?:has|have|did))\s+"
    r"([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,2})(?:'s)?\b",
)


def parse_intelligence(message: str) -> Tuple[Optional[str], Optional[str]]:
    """→ (talent_text | None, topic_hint | None). Project/talent resolution
    itself is delegated to status.parse_status by the caller."""
    m = message or ""
    talent = None
    s = _INTEL_SUBJECT_RE.search(m) or _DRAFT_SUBJECT_RE.search(m) or _CONVO_SUBJECT_RE.search(m)
    if s and s.group(1).strip().lower() not in ("the", "her", "his", "their", "a", "an", "this", "that"):
        talent = s.group(1).strip()
    hint = None
    th = _TOPIC_HINT_RE.search(m)
    if th:
        raw = th.group("t").lower().replace(" ", "").replace("shoot", "shoot_" if "date" in th.group("t").lower() else "")
        hint = {"shootdate": "shoot_date", "date": "shoot_date", "shoot_date": "shoot_date"}.get(raw, raw)
    return talent, hint


# ---- topic classification (deterministic, conservative) -------------
TOPICS = (
    "availability", "budget", "shoot_date", "location", "usage", "payment", "commission",
    "project_details", "submission", "audition", "interest", "not_interested",
    "question", "confirmation", "unknown",
)

# (regex, topic, confidence). Order = priority; first strong match wins the topic.
_RULES: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"\bnot\s+interested\b|\bhave\s+to\s+pass\b|\bnot\s+suitable\b|\bwon'?t\s+(?:be\s+able\s+to\s+)?do\b|\bhave\s+to\s+decline\b|\bcan'?t\s+do\s+this\s+one\b", re.I), "not_interested", "high"),
    (re.compile(r"\b(?:very\s+)?interested\b|\bi\s+want\s+to\s+do\s+(?:it|this)\b|\bcount\s+me\s+in\b|\bi'?m\s+in\b|\bi'?d\s+love\s+to\b|\bkeen\s+to\s+do\b", re.I), "interest", "high"),
    (re.compile(r"\bpayment\s+(?:schedule|terms|date|cycle)\b|\bwhen\s+(?:will\s+i|do\s+i)\s+(?:be\s+paid|get\s+paid)\b|\binvoice\b|\bpay(?:ment)?\s+(?:after|within)\b", re.I), "payment", "high"),
    (re.compile(r"\bcommission\b|\byour\s+cut\b|\bagency\s+(?:fee|cut|percentage)\b", re.I), "commission", "high"),
    (re.compile(r"\bbudget\b|\bhow\s+much\s+(?:is\s+the\s+pay|does\s+it\s+pay|will\s+i\s+(?:get|make))\b|\bwhat'?s?\s+the\s+(?:pay|fee|rate)\b|\bday\s+rate\b|\bper\s+day\s+rate\b", re.I), "budget", "high"),
    (re.compile(r"\bshoot\s*date\b|\bwhat\s+date\b|\bwhich\s+date\b|\bwhen\s+(?:is|do\s+we\s+shoot|'?s)\s+the\s+shoot\b|\bshoot\s+day\b|\bdates?\s+of\s+the\s+shoot\b|\bis\s+the\s+shoot\s+on\b|\bshoot\s+(?:is\s+)?on\s+the\b|\banother\s+(?:shoot\s+)?date\b", re.I), "shoot_date", "high"),
    (re.compile(r"\bnot\s+available\b|\bunavailable\b|\bnot\s+(?:be\s+)?free\b|\bcan'?t\s+make\b|\bcannot\s+make\b|\bcan'?t\s+do\s+the\b|\bcan'?t\s+do\s+(?:on\s+)?(?:the\s+)?\d", re.I), "availability", "high"),
    (re.compile(r"\bavailable\b|\bi'?m\s+free\b|\bi\s+am\s+free\b|\bfree\s+on\b|\bcan\s+i\s+do\s+(?:friday|monday|tuesday|wednesday|thursday|saturday|sunday|the\s+\d)", re.I), "availability", "medium"),
    (re.compile(r"\bwhere\b[^.?!]{0,30}\bshoot\b|\bshoot\b[^.?!]{0,30}\bwhere\b|\bshoot\s+location\b|\blocation\s+of\s+the\s+shoot\b|\bwhich\s+city\b|\bwhere\s+(?:will\s+it\s+be|do\s+we\s+shoot|is\s+it)\b|\bvenue\b", re.I), "location", "high"),
    (re.compile(r"\busage\b|\bwhere\s+will\s+it\s+be\s+used\b|\bwhich\s+platforms?\b|\bmedia\s+rights?\b|\bhow\s+long\s+is\s+the\s+usage\b|\bshelf\s+life\b", re.I), "usage", "high"),
    (re.compile(r"\bi(?:'?ll| will)\s+(?:submit|send)\b|\bsubmitting\b|\bsent\s+my\s+(?:audition|tape|self.?tape)\b|\buploaded\s+(?:my|it|the)\b|\bhere'?s?\s+my\s+(?:audition|tape)\b|\bsending\s+(?:my|the)\s+(?:tape|audition)\b", re.I), "submission", "high"),
    (re.compile(r"\bself.?tape\b|\baudition\b|\bwhat\s+(?:lines|scene)\b|\bwhich\s+lines\b|\bwhat\s+should\s+i\s+perform\b|\bscript\b|\bsides\b", re.I), "audition", "medium"),
    (re.compile(r"\btell\s+me\s+more\b|\bmore\s+(?:details|info)\b|\bwhat'?s?\s+the\s+project\b|\bwhat\s+is\s+this\s+for\b|\bbrief\b|\bmore\s+about\s+the\s+(?:project|role|brand)\b", re.I), "project_details", "medium"),
    (re.compile(r"^\s*(?:ok(?:ay)?|k)\b|\bthank\s*(?:s|\s*you)\b|\bgot\s+it\b|\bnoted\b|\bsounds\s+good\b|\bconfirmed\b|\bwill\s+do\b|\bperfect\b|\bgreat,?\s*thanks\b", re.I), "confirmation", "medium"),
]

_NEGOTIATION_RE = re.compile(
    r"\b(?:increase|raise|bump\s+up|bump|revisit|reconsider|push\s+up|improve)\b[^.?!]*"
    r"\b(?:budget|rate|fee|pay|price|amount|number|offer)\b|"
    r"\b(?:budget|rate|fee|pay)\b[^.?!]*\b(?:increase|higher|go\s+up|more|negotiab)\b|"
    r"\b(?:any\s+(?:chance|room|scope)|room\s+to\s+move)\b[^.?!]*"
    r"\b(?:budget|rate|fee|pay|price|₹|rs\.?|inr)\b|"
    r"\bnegotia(?:te|ting|tion|ble)\b", re.I,
)


def _primary_intent(intents: List[dict]) -> dict:
    """Phase 8/9 compat: the single `topic` points at the first ACTIONABLE
    intent — a data question or a negotiation — else the first intent."""
    return (next((i for i in intents if i["topic"] in _DATA_TOPICS), None)
            or next((i for i in intents if i["topic"] == "negotiation"), None)
            or intents[0])


def classify_topic(text: str) -> Tuple[str, str]:
    """→ (primary topic, confidence). Kept for Phase 8/9 back-compat."""
    p = _primary_intent(classify_intents(text))
    return p["topic"], p["confidence"]


# data topics that can carry a project fact (an INFORMATIONAL question)
_DATA_TOPICS = frozenset({"budget", "shoot_date", "usage", "commission", "project_details", "audition",
                          "payment", "location"})


def classify_intents(text: str) -> List[Dict[str, Any]]:
    """Phase 10 — deterministic MULTI-intent. → ordered, de-duplicated list of
    {topic, confidence, order}. Conservative: a topic appears only when its
    own keyword rule actually fires; never forced. Order = first occurrence
    in the text where determinable."""
    t = text or ""
    seen: Dict[str, Dict[str, Any]] = {}
    for rx, topic, conf in _RULES:
        m = rx.search(t)
        if not m:
            continue
        if topic not in seen:
            seen[topic] = {"topic": topic, "confidence": conf, "_pos": m.start()}
        elif m.start() < seen[topic]["_pos"]:
            seen[topic]["_pos"] = m.start()

    sig = extract_signals(t)
    # a date token beside a "shoot" mention is a shoot-date question even if
    # phrased loosely ("is the shoot on the 15th?")
    if sig.get("dates") and re.search(r"\bshoot\b", t, re.I) and "shoot_date" not in seen:
        seen["shoot_date"] = {"topic": "shoot_date", "confidence": "high",
                              "_pos": t.lower().find("shoot")}
    if sig.get("negotiation_request") and "negotiation" not in seen:
        mm = _NEGOTIATION_RE.search(t)
        seen["negotiation"] = {"topic": "negotiation", "confidence": "high",
                               "_pos": mm.start() if mm else len(t)}
    # a bare interest/availability signal with no data question of its own
    if sig.get("interest_signal") == "interested" and "interest" not in seen:
        seen["interest"] = {"topic": "interest", "confidence": "high", "_pos": 0}
    if sig.get("interest_signal") == "not_interested" and "not_interested" not in seen:
        seen["not_interested"] = {"topic": "not_interested", "confidence": "high", "_pos": 0}
    if sig.get("availability_signal") and "availability" not in seen:
        seen["availability"] = {"topic": "availability", "confidence": "high", "_pos": 0}

    if not seen:
        if _is_question(t):
            return [{"topic": "question", "confidence": "low", "order": 0}]
        return [{"topic": "unknown", "confidence": "low", "order": 0}]

    ordered = sorted(seen.values(), key=lambda d: d["_pos"])
    return [{"topic": d["topic"], "confidence": d["confidence"], "order": i}
            for i, d in enumerate(ordered)]


# ---- extraction (deterministic, conservative) ---------------------
_Q_START_RE = re.compile(
    r"^\s*(?:what|when|where|who|why|how|can|could|would|will|is|are|do|does|did|should|may|any\s+chance)\b", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DAY_ORD_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.I)
_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")[a-z]*\b", re.I)
_MONTH_DAY_RE = re.compile(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\s+(\d{1,2})\b", re.I)
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?\b")
_RELATIVE_RE = re.compile(
    r"\b(today|tomorrow|tonight|day\s+after\s+tomorrow|this\s+weekend|next\s+week|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)


def _is_question(text: str) -> bool:
    t = (text or "").strip()
    return ("?" in t) or bool(_Q_START_RE.match(t))


def extract_date(text: str) -> Dict[str, Any]:
    """→ {raw, resolution: 'resolved'|'relative'|'ambiguous'|'none', value}."""
    t = text or ""
    absolute: List[str] = []
    for m in _DAY_MONTH_RE.finditer(t):
        absolute.append(m.group(0))
    for m in _MONTH_DAY_RE.finditer(t):
        absolute.append(m.group(0))
    for m in _NUMERIC_DATE_RE.finditer(t):
        absolute.append(m.group(0))
    # bare "15th" only counts as a date-ish token when no fuller form exists
    ords = [m.group(0) for m in _DAY_ORD_RE.finditer(t)]
    relatives = [m.group(0) for m in _RELATIVE_RE.finditer(t)]

    tokens = absolute or ords
    if len(tokens) == 1 and not relatives and absolute:
        return {"raw": tokens[0], "resolution": "resolved", "value": tokens[0]}
    if len(tokens) == 1 and not relatives:   # a bare ordinal like "15th"
        return {"raw": tokens[0], "resolution": "ambiguous", "value": None}
    if not tokens and len(relatives) == 1:
        return {"raw": relatives[0], "resolution": "relative", "value": None}
    if not tokens and not relatives:
        return {"raw": None, "resolution": "none", "value": None}
    return {"raw": ", ".join(tokens + relatives), "resolution": "ambiguous", "value": None}


_UNAVAIL_RE = re.compile(r"\bnot\s+available\b|\bunavailable\b|\bcan'?t\s+make\b|\bcannot\s+make\b|\bcan'?t\s+do\b|\bbusy\b|\bnot\s+(?:be\s+)?free\b|\bwon'?t\s+be\s+able\b", re.I)
_AVAIL_RE = re.compile(r"\bi'?m\s+free\b|\bi\s+am\s+free\b|(?<!not )(?<!be )\bavailable\b|\bfree\s+on\b|\bi\s+can\s+do\s+(?:it|the|this)\b", re.I)
_INT_YES_RE = re.compile(r"\b(?:very\s+)?interested\b|\bi\s+want\s+to\s+do\b|\bcount\s+me\s+in\b|\bi'?m\s+in\b|\bkeen\b", re.I)
_INT_NO_RE = re.compile(r"\bnot\s+interested\b|\bhave\s+to\s+pass\b|\bnot\s+suitable\b|\bdecline\b|\bcan'?t\s+do\s+this\s+one\b", re.I)


def extract_dates(text: str) -> List[Dict[str, Any]]:
    """Phase 10 — one entry PER distinct date token. A bare ordinal stays
    ambiguous; a relative token stays relative; nothing is resolved to a
    calendar date (Phase 8 rule preserved)."""
    t = text or ""
    out: List[Dict[str, Any]] = []
    for rx in (_DAY_MONTH_RE, _MONTH_DAY_RE, _NUMERIC_DATE_RE):
        for m in rx.finditer(t):
            out.append({"raw": m.group(0), "resolution": "resolved", "value": m.group(0)})
    if not out:  # only fall back to bare ordinals when there's no fuller form
        for m in _DAY_ORD_RE.finditer(t):
            out.append({"raw": m.group(0), "resolution": "ambiguous", "value": None})
    for m in _RELATIVE_RE.finditer(t):
        out.append({"raw": m.group(0), "resolution": "relative", "value": None})
    return out


def extract_signals(text: str) -> Dict[str, Any]:
    t = text or ""
    av = None
    if _UNAVAIL_RE.search(t) and not _AVAIL_RE.search(t):
        av = "unavailable"
    elif _AVAIL_RE.search(t) and not _UNAVAIL_RE.search(t):
        av = "available"
    it = None
    if _INT_NO_RE.search(t):
        it = "not_interested"
    elif _INT_YES_RE.search(t):
        it = "interested"
    return {
        "is_question": _is_question(t),
        "date": extract_date(t),          # compat — whole-message (multiple → ambiguous)
        "dates": extract_dates(t),        # Phase 10 — per-token
        "availability_signal": av,
        "interest_signal": it,
        "negotiation_request": bool(_NEGOTIATION_RE.search(t)),
    }


# ---- project knowledge (existing authoritative fields only) --------
# topic -> (project field, human label, response template)
_TOPIC_FIELDS = {
    "budget": ("budget_per_day", "budget", "The budget for {project} is {value}."),
    "shoot_date": ("shoot_dates", "shoot date", "The shoot is scheduled for {value}."),
    "usage": ("medium_usage", "usage", "The usage for this project is {value}."),
    "commission": ("commission_percent", "commission", "The commission for this project is {value}."),
    "project_details": ("additional_details", "project details", "Project details: {value}"),
    "audition": ("character", "audition brief", "For the audition: {value}"),
}
# topics for which Talentgram has NO canonical project field
_NO_FIELD_TOPICS = {"payment", "location"}
_LABELS = {
    "budget": "budget", "shoot_date": "shoot date", "usage": "usage", "commission": "commission",
    "payment": "payment terms", "location": "shoot location", "project_details": "project details",
    "audition": "audition brief",
}


def project_knowledge(project: Optional[dict], topic: str) -> Dict[str, Any]:
    label = _LABELS.get(topic)
    # availability is a talent STATEMENT — the relevant existing fact to
    # cross-check it against is the shoot date (never a mutation).
    if topic == "availability":
        raw = (project or {}).get("shoot_dates")
        val = str(raw).strip() if raw not in (None, "", []) else None
        return {"field": "shoot_dates" if val else None, "value": val, "available": val is not None,
                "hidden_from_talent": False, "label": "shoot date", "no_canonical_field": False}
    if topic in _NO_FIELD_TOPICS:
        return {"field": None, "value": None, "available": False,
                "hidden_from_talent": False, "label": label, "no_canonical_field": True}
    spec = _TOPIC_FIELDS.get(topic)
    if not spec or not project:
        return {"field": None, "value": None, "available": False,
                "hidden_from_talent": False, "label": label, "no_canonical_field": not spec}
    field, label2, _tmpl = spec
    raw = project.get(field)
    val = str(raw).strip() if raw not in (None, "", []) else None
    hidden = bool(project.get("hide_budget_from_talent")) if topic == "budget" else False
    return {"field": field, "value": val, "available": val is not None,
            "hidden_from_talent": hidden, "label": label or label2, "no_canonical_field": False}


def project_knowledge_multi(project: Optional[dict], intents: List[dict]) -> Dict[str, Any]:
    """Phase 10 — retrieve ONLY the canonical fact for each detected DATA
    intent (a question that maps to a project field). Never the whole
    project document, never an unrelated field. `availability` is a talent
    STATEMENT, so its shoot-date cross-check is kept separate from `facts`.
    → {facts: {label: value}, missing: [label], hidden: [label],
       availability_crosscheck: {label,value}|None, per_topic: {...}}"""
    facts: Dict[str, str] = {}
    missing: List[str] = []
    hidden: List[str] = []
    per_topic: Dict[str, Any] = {}
    crosscheck: Optional[Dict[str, Any]] = None
    negotiating = any(i["topic"] == "negotiation" for i in intents)
    for it in intents:
        topic = it["topic"]
        if topic == "budget" and negotiating:
            # a budget question that IS a negotiation — the amount is NEVER
            # disclosed or proposed; a human decides. Do not surface a value.
            k = project_knowledge(project, "budget")
            per_topic["budget"] = k
            continue
        if topic == "availability":
            k = project_knowledge(project, "availability")
            per_topic["availability"] = k
            if k.get("available") and k.get("value"):
                crosscheck = {"label": k.get("label"), "value": str(k["value"])}
            continue
        if topic not in _DATA_TOPICS:
            continue
        k = project_knowledge(project, topic)
        per_topic[topic] = k
        lbl = k.get("label") or topic
        if topic == "budget" and k.get("hidden_from_talent"):
            if lbl not in hidden:
                hidden.append(lbl)
            continue
        if k.get("available") and k.get("value"):
            facts.setdefault(lbl, str(k["value"]))
        else:
            if lbl not in missing:
                missing.append(lbl)
    return {"facts": facts, "missing": missing, "hidden": hidden,
            "availability_crosscheck": crosscheck, "per_topic": per_topic}


# ---- answerability + proposed response ---------------------------
def _answerability(topic, signals, knowledge, resolution_ambiguous) -> str:
    if resolution_ambiguous:
        return "ambiguous"
    if signals.get("negotiation_request"):
        return "partially_answerable"          # data exists, but the decision doesn't
    if topic in ("confirmation", "not_interested", "interest", "submission"):
        return "answerable"                    # nothing to look up; a human decides next step
    if topic == "availability":
        return "partially_answerable" if knowledge.get("available") else "insufficient_information"
    if topic in _TOPIC_FIELDS:
        return "answerable" if knowledge.get("available") else "insufficient_information"
    if topic in _NO_FIELD_TOPICS:
        return "insufficient_information"
    if topic in ("question", "unknown"):
        return "insufficient_information"
    return "insufficient_information"


def _proposed_response(topic, signals, knowledge, project_label) -> Tuple[Optional[str], Optional[str]]:
    """→ (proposed_response | None, next_step | None). DISPLAY ONLY."""
    if signals.get("negotiation_request"):
        return None, "Requires admin decision — no counteroffer was made."
    if topic == "confirmation":
        return None, "No response appears to be needed."
    if topic in ("not_interested", "interest"):
        return None, "No pipeline change was made — awaiting your decision."
    if topic == "availability":
        if knowledge.get("available"):
            d = signals.get("date") or {}
            hint = f' The talent mentioned "{d["raw"]}".' if d.get("raw") else ""
            return (f"The shoot is scheduled for {knowledge['value']}.{hint}",
                    "Talent has stated an availability signal — no pipeline change was made.")
        return None, "No shoot date on file to check this against."
    spec = _TOPIC_FIELDS.get(topic)
    if spec and knowledge.get("available"):
        if topic == "budget" and knowledge.get("hidden_from_talent"):
            return None, "A budget is set but is marked hidden from talents — admin decision."
        tmpl = spec[2]
        return tmpl.format(project=project_label or "this project", value=knowledge["value"]), None
    # unavailable / no canonical field
    if knowledge.get("label"):
        return f"I don't have the {knowledge['label']} for this project yet.", None
    return None, None


def _answerability_multi(intents, signals, km, ambiguous) -> str:
    if ambiguous:
        return "ambiguous"
    topics = {i["topic"] for i in intents}
    data = [i for i in intents if i["topic"] in _DATA_TOPICS]
    n_have = len(km["facts"])
    n_missing = len(km["missing"]) + len(km["hidden"])
    if "negotiation" in topics:
        return "partially_answerable"     # data exists; the decision needs a human
    if data:
        if n_missing == 0 and n_have > 0:
            base = "answerable"
        elif n_have > 0:
            base = "partially_answerable"
        else:
            base = "insufficient_information"
    elif "availability" in topics and not (topics - {"availability"}):
        # Phase 8 rule: an availability statement is partially answerable only
        # if there's a shoot date on file to check it against.
        base = "partially_answerable" if km.get("availability_crosscheck") else "insufficient_information"
    else:
        base = "answerable"       # only acknowledgement/interest/etc. intents
    if signals.get("negotiation_request") and base == "answerable":
        base = "partially_answerable"
    return base


def _proposed_response_multi(intents, signals, km, project_label) -> Tuple[Optional[str], Optional[str]]:
    """Deterministic composed response over multiple intents. DISPLAY ONLY."""
    topics = {i["topic"] for i in intents}
    parts: List[str] = []
    next_steps: List[str] = []

    if "interest" in topics:
        parts.append("Thanks for your interest.")
    if "not_interested" in topics:
        parts.append("Thanks for letting us know.")
        next_steps.append("No pipeline change was made — awaiting your decision.")
    if "availability" in topics:
        parts.append("Thanks for the update on your availability.")
        next_steps.append("Talent stated an availability signal — no pipeline change was made.")

    order = {"budget": 0, "shoot_date": 1, "usage": 2, "commission": 3, "project_details": 4, "audition": 5}
    for lbl, val in sorted(km["facts"].items(), key=lambda kv: order.get(kv[0].replace(" ", "_"), 9)):
        tmpl = next((s[2] for s in _TOPIC_FIELDS.values() if s[1] == lbl), None)
        parts.append(tmpl.format(project=project_label or "this project", value=val) if tmpl
                     else f"The {lbl} is {val}.")
    for lbl in km["missing"]:
        parts.append(f"I don't have the {lbl} for this project yet.")
    if km["hidden"]:
        next_steps.append("Budget is hidden from talents — admin decision.")
        if km["facts"] or km["missing"]:      # only add the clause alongside real facts
            parts.append("I don't have budget details I can share at this stage.")
    if "negotiation" in topics:
        next_steps.append("Requires admin decision — no counteroffer was made.")
        if km["facts"] or km["missing"]:
            parts.append("I'll check with the team regarding the budget and get back to you.")

    # Phase 8/9 discipline: a message with NO deterministically-answerable
    # data fact (pure negotiation / hidden budget / bare signal) gets no
    # auto-proposed text — the AI draft handles wording, a human decides.
    if not km["facts"] and not km["missing"]:
        return None, ("; ".join(dict.fromkeys(next_steps)) or None)

    text = " ".join(parts).strip() or None
    return text, ("; ".join(dict.fromkeys(next_steps)) or None)


# ---- orchestrator ----------------------------------------------
def analyze(inbound: dict, project: Optional[dict]) -> Dict[str, Any]:
    """Deterministic analysis of ONE canonical inbound message. Pure —
    reads its two inputs, writes nothing, calls no model."""
    text = inbound.get("message_text") or ""
    talent_res = inbound.get("talent_resolution")
    project_res = inbound.get("project_resolution")
    ambiguous = (talent_res == "ambiguous") or (project_res == "ambiguous_project")

    signals = extract_signals(text)
    intents = classify_intents(text)

    # a negotiation request always pairs with the budget intent (for the fact
    # lookup) — the "negotiation" marker itself is kept for mode/validation.
    if signals["negotiation_request"]:
        topics_now = {i["topic"] for i in intents}
        if "budget" not in topics_now:
            intents.append({"topic": "budget", "confidence": "high", "order": len(intents)})

    primary = _primary_intent(intents)
    primary_topic = primary["topic"]
    primary_conf = primary["confidence"]

    proj_for_facts = project if not ambiguous else None
    km = project_knowledge_multi(proj_for_facts, intents)

    # Phase 8 compat: `project_knowledge` = the first DATA intent's, else primary
    first_data = next((i["topic"] for i in intents if i["topic"] in _DATA_TOPICS
                       or i["topic"] == "availability"), primary_topic)
    knowledge = project_knowledge(proj_for_facts, first_data)

    answerability = _answerability_multi(intents, signals, km, ambiguous)
    if ambiguous:
        proposed, next_step = None, None
    else:
        proposed, next_step = _proposed_response_multi(
            intents, signals, km, (project or {}).get("brand_name"))

    any_ambiguous_date = any(d["resolution"] == "ambiguous" for d in signals["dates"])
    needs_review = (
        any(i["confidence"] != "high" for i in intents)
        or any_ambiguous_date
        or answerability in ("ambiguous", "partially_answerable")
        or "negotiation" in {i["topic"] for i in intents}
    )

    return {
        # 5 — the original message stays authoritative
        "inbound_id": inbound.get("id"),
        "message_id": inbound.get("message_id"),
        "message_text": text,
        "received_at": inbound.get("received_at"),
        "talent_id": inbound.get("talent_id"),
        "talent_resolution": talent_res,
        "project_id": inbound.get("project_id"),
        "project_resolution": project_res,
        "project_candidates": inbound.get("project_candidates") or [],
        "group_name": inbound.get("group_name"),
        # Phase 10 — the authoritative multi-intent list
        "intents": intents,
        # Phase 8/9 back-compat — the primary (first) intent
        "topic": primary_topic,
        "topic_confidence": primary_conf,
        "signals": signals,
        "project_knowledge": knowledge,
        # Phase 10 — the multi-fact set (only detected-intent fields)
        "project_knowledge_multi": km,
        # 8 / 9
        "answerability": answerability,
        "proposed_response": proposed,
        "next_step": next_step,
        "needs_review": needs_review,
        # invariants
        "pipeline_mutation": "none",
        "no_message_sent": True,
    }
