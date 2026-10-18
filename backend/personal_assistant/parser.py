"""Personal Assistant — deterministic command parser (no LLM, no network, no DB).

One message in → an ordered list of `Segment`s out. A segment is ONE business action with the raw phrases the
user typed for its talents / projects / template / stage / recipient; turning phrases into real ids is the
resolver's job (resolve.py), executing is the executors' (executors.py). This file only decides WHAT was asked.

Multi-command messages are split on sentence ends and on connectors that introduce a new verb
("…Amazon Now and send them the Casting Call. Also add Neha to Pepsi…"). A plain "and" between names
("Angela, Riya and Neha") never splits. A pronoun ("them", "her") in a later segment points at the talents of
the previous one — the planner turns that into a task dependency.

Stage vocabulary is the existing casting-pipeline NLU's (shorthand, fuzzy, live PIPELINE_STAGE_ORDER).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from agents.modules import casting_pipeline_nlu as nlu
from routers.casting_pipeline import PIPELINE_STAGE_ORDER

INTENTS = ("lookup", "add", "send", "move", "list", "share", "offer_send", "unknown")


@dataclass
class Segment:
    text: str
    intent: str = "unknown"
    talents: List[str] = field(default_factory=list)      # name phrases as typed
    talent_ref: Optional[str] = None                      # "selected" | "previous"
    project_text: Optional[str] = None                    # raw project clause ("Amazon and Pepsi")
    project_ref: Optional[str] = None                     # "current"
    template: Optional[str] = None                        # raw template phrase
    stage: Optional[str] = None                           # canonical stage key
    stage_text: Optional[str] = None                      # stage phrase that did not resolve cleanly
    stage_filter: Optional[str] = None                    # "selected talents in <stage>"
    lookup: Optional[str] = None                          # profile|media_link|media|history|projects|submissions|tested|find
    recipient: Optional[dict] = None                      # {"kind": number|project_group|casting_director, "value": ...}
    worker_hint: Optional[str] = None                     # "worker 1" | "worker 2" as typed


# ---------------------------------------------------------------------------------------------------------
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")
_VERBS = r"(?:send|add|move|share|show|find|mark|put|ask|forward|get|give|list|include)"
_CONNECT = re.compile(
    r"\s*,?\s*\b(?:and\s+then|and\s+also|then|also|after\s+that|afterwards|plus)\b\s*,?\s*"
    r"|\s*,\s*(?=" + _VERBS + r"\b)"
    r"|\s+and\s+(?=" + _VERBS + r"\b)"
    r"|\s*,?\s*\band\s+(?=(?:tell\s+me|which\s+projects?|what\s+projects?|has\s+(?:she|he|they)\b|her\s|his\s|their\s))"
    r"|\s*,\s*(?=tell\s+me\b)",
    re.I,
)
_LEAD = re.compile(r"^(?:(?:hey|hi|ok|okay|so|please|pls|kindly|can\s+you|could\s+you|would\s+you|i\s+want\s+you\s+to|i'd\s+like\s+you\s+to)\b[\s,]*)+", re.I)

_WORKER = re.compile(r"\b(?:(?:via|using|from|through|with|on)\s+)?worker\s*(?:no\.?\s*)?(1|2|one|two)\b", re.I)
_PHONE = re.compile(r"(\+?\d[\d\s\-()]{7,}\d)")
_SELECTED = re.compile(
    r"\b(?:these|those|the\s+selected|selected|my\s+selection|the\s+selection|this\s+selection)"
    r"(?:\s+(?:selected|\w+))?\s+(?:talents?|people|girls|boys|models|actors|ones)\b"
    r"|\b(?:these|those|the\s+selected(?:\s+ones)?|selected|my\s+selection|the\s+selection)\b", re.I)
_PRONOUN = re.compile(r"^(?:them|her|him|they|all\s+of\s+them|both(?:\s+of\s+them)?)$", re.I)
_FILLER_WORDS = {"the", "a", "an", "of", "please", "all", "talent", "talents", "profile", "profiles"}
# Command words that a loose name pattern can swallow from the front of "Show me Angela" / "Send Angela".
_LEAD_VERBS = {"show", "me", "get", "open", "find", "give", "pull", "up", "send", "share", "forward", "look", "lookup",
               "search", "for", "who", "is", "locate", "see", "display", "tell", "about"}
_CURRENT_PROJECT = re.compile(r"\b(?:the\s+)?(?:current|this|that|same)\s+project\b|\bhere\b", re.I)
_NUMBER_WORDS = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"

_TEMPLATE_CLEAN = re.compile(r"\b(?:the|a|an|template|message|whatsapp)\b", re.I)


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip(" \t,.;:!?"))


def _strip_possessive(s: str) -> str:
    return re.sub(r"(?:'s|’s|s')\s*$", "", s.strip(), flags=re.I) if re.search(r"(?:'|’)s?\s*$", s.strip()) else s.strip()


def split_names(raw: str) -> List[str]:
    """"Angela, Riya and Neha" → ["Angela", "Riya", "Neha"]. Possessives and filler words are dropped."""
    out: List[str] = []
    for part in re.split(r"\s*(?:,|&|\band\b|\bplus\b)\s*", raw or "", flags=re.I):
        part = _strip_possessive(_norm_space(part))
        words = [w for w in part.split() if w.lower() not in _FILLER_WORDS]
        while len(words) > 1 and words[0].lower() in _LEAD_VERBS:
            words = words[1:]
        if words and not (len(words) == 1 and words[0].lower() in _LEAD_VERBS):
            out.append(" ".join(words))
    return out


_OTHER = re.compile(r"\b(?:these|those|the)\s+(?:other|different|remaining|additional|new)\b", re.I)


def _who(raw: str, seg: Segment) -> None:
    """Fill talents / talent_ref from the 'who' clause of a command."""
    raw = _norm_space(raw)
    if not raw:
        return
    if _OTHER.search(raw):                 # "these OTHER talents" is not the selection on screen: ask who
        seg.talent_ref = "ask"
        return
    if _PRONOUN.match(raw):
        seg.talent_ref = "previous"
        return
    m = _SELECTED.search(raw)
    if m:
        seg.talent_ref = "selected"
        tail = raw[m.end():]
        stage_in = re.search(r"\bin\s+(.+)$", tail, re.I)
        if stage_in:
            key, _amb, _rest = nlu.extract_stage_phrase(stage_in.group(1), PIPELINE_STAGE_ORDER)
            if key:
                seg.stage_filter = key
        return
    cleaned = re.sub(rf"^(?:{_NUMBER_WORDS}\s+)?(?:new\s+)?talents?\b\s*", "", raw, flags=re.I)
    seg.talents = split_names(cleaned or raw)


def _project(raw: Optional[str], seg: Segment) -> None:
    raw = re.sub(r"^(?:(?:in|for|of|on)\s+)+", "", _norm_space(raw or ""), flags=re.I)
    if not raw:
        return
    if _CURRENT_PROJECT.fullmatch(raw) or re.fullmatch(r"(?:the\s+)?(?:current|this)(?:\s+project)?", raw, re.I):
        seg.project_ref = "current"
        return
    seg.project_text = raw


def _worker(text: str, seg: Segment) -> str:
    m = _WORKER.search(text)
    if m:
        seg.worker_hint = "worker " + {"one": "1", "two": "2"}.get(m.group(1).lower(), m.group(1))
        text = text[:m.start()] + " " + text[m.end():]
    return text


def _template(raw: str) -> Optional[str]:
    t = _norm_space(_TEMPLATE_CLEAN.sub(" ", raw or ""))
    return t or None


# --- intent detection --------------------------------------------------------------------------------------
_SHARE_OBJ = re.compile(r"\b(?:profile|media\s*link|media|portfolio|audition|photos?|pictures?|video|intro(?:duction)?\s+video)\b", re.I)
_RECIPIENT_GROUP = re.compile(r"\b(?:the\s+|this\s+)?(?:project|casting|whatsapp)\s+group\b", re.I)
_RECIPIENT_DIRECTOR = re.compile(r"\b(?:the\s+)?casting\s+director\b", re.I)


_ROLES = {
    "casting director": "casting_director", "casting assistant": "casting_assistant", "line producer": "line_producer",
    "creative producer": "creative_producer", "brand manager": "brand_manager", "marketing manager": "marketing_manager",
    "production house": "production_house", "producer": "producer",
}
_ROLE_RE = re.compile(r"\b(?:the\s+)?(" + "|".join(sorted(_ROLES, key=len, reverse=True)) + r")\b", re.I)


def _recipient(text: str) -> Optional[dict]:
    m = _PHONE.search(text)
    if m:
        return {"kind": "number", "value": re.sub(r"[^\d+]", "", m.group(1))}
    if _RECIPIENT_GROUP.search(text):
        return {"kind": "project_group", "value": None}
    tail = re.search(r"\b(?:to|with)\s+(?P<r>.+?)\s*$", re.sub(r"\s+for\s+.+$", "", text, flags=re.I), re.I)
    if not tail:
        return None
    r = tail.group("r").strip(" .,")
    role = _ROLE_RE.search(r)
    if role and len(r.split()) <= 4:
        return {"kind": "role", "value": _ROLES[role.group(1).lower()], "text": role.group(1).lower()}
    if re.fullmatch(r"(?:this|the|that)\s+\w*\s*(?:number|group|contact)?|them|him|her|it", r, re.I) or not re.search(r"[A-Za-z]", r):
        return None
    return {"kind": "named", "value": r}


_NAME = r"[A-Za-z][A-Za-z.'’\-]*(?:\s+[A-Za-z][A-Za-z.'’\-]*){0,3}"


def _parse_share(text: str, seg: Segment) -> None:
    seg.intent = "share"
    seg.recipient = _recipient(text)
    pm = re.search(r"\s+for\s+(?P<p>.+?)\s*$", text, re.I)
    if pm and not re.search(r"\bfor\s+(?:him|her|them|it)\b", pm.group(0), re.I):
        _project(pm.group("p"), seg)
    body = text
    # "share Angela's media link" / "send Angela Kumar's audition to ..." / "share the profile of Angela"
    m = re.search(rf"(?P<n>{_NAME})(?:'|’)s\s+(?:profile|media\s*link|media|portfolio|audition|photos?|video)", body, re.I)
    if not m:
        m = re.search(rf"(?:profile|media\s*link|media|portfolio|audition)\s+(?:of|for)\s+(?P<n>{_NAME})", body, re.I)
    if m:
        # "…media link of Angela with the casting director": the name stops at the preposition that starts the recipient
        seg.talents = split_names(re.split(r"\s+(?:with|to|for|via|at)\b", m.group("n"), maxsplit=1, flags=re.I)[0])
    elif re.search(r"\b(?:her|his|their)\s+(?:profile|audition|media\s*link|photos?)\b", body, re.I):
        seg.talent_ref = "previous"     # "send her media link to …" — the talent just discussed
    elif re.search(r"\b(?:this|the|that)\s+(?:profile|audition|media\s*link)\b|\bit\b|\bthem\b", body, re.I):
        seg.talent_ref = "selected"     # "send this profile to …" — the one on screen / selected
    seg.lookup = "media_link" if re.search(r"media\s*link|media", body, re.I) else ("audition" if re.search(r"audition", body, re.I) else "profile")


_PRON = r"(?:she|he|they|her|him|them|their|his)"


def _parse_pronoun_lookup(text: str, seg: Segment) -> bool:
    """"…which projects she has tested for" / "has he tested" / "her submission history" — about the talent of
    the previous action in the same message."""
    low = text.lower()
    kind = None
    if re.search(r"\b(?:which|what)\s+projects?\b.*\btested\b|\bhas\s+" + _PRON + r"\s+(?:ever\s+)?(?:tested|auditioned)\b|\bhave\s+they\s+tested\b", low):
        kind = "tested"
    elif re.search(r"\b(?:which|what)\s+projects?\b.*\b" + _PRON + r"\b|\bprojects?\s+(?:is|has|was)\s+" + _PRON + r"\b", low):
        kind = "projects"
    elif re.search(r"\b(?:her|his|their)\s+submission\s+history\b", low):
        kind = "submissions"
    elif re.search(r"\b(?:her|his|their)\s+media\s*link\b", low):
        kind = "media_link"
    elif re.search(r"\b(?:her|his|their)\s+(?:photos?|videos?)\b", low):
        kind = "media"
    elif re.search(r"\b(?:her|his|their)\s+profile\b", low):
        kind = "profile"
    if not kind:
        return False
    seg.intent, seg.lookup, seg.talent_ref = "lookup", kind, "previous"
    return True


def _parse_lookup(text: str, seg: Segment) -> bool:
    low = text.lower()
    names: Optional[str] = None
    kind: Optional[str] = None
    if _parse_pronoun_lookup(text, seg):
        return True
    m = re.search(rf"\bhas\s+(?P<n>{_NAME}?)\s+(?:ever\s+)?(?:tested|auditioned|been\s+tested)", text, re.I)
    if m:
        names, kind = m.group("n"), "tested"
    if not names:
        m = re.search(rf"\bwhich\s+projects?\s+(?:has|have|is|was|did)\s+(?P<n>{_NAME}?)\s+(?:been\s+)?(?:added|in|part|submitted|worked)", text, re.I)
        if m:
            names, kind = m.group("n"), "projects"
    if not names:
        m = re.search(rf"\bsubmission\s+history\s+(?:of|for)\s+(?P<n>{_NAME})", text, re.I) or re.search(
            rf"(?P<n>{_NAME})(?:'|’)s\s+submission\s+history", text, re.I)
        if m:
            names, kind = m.group("n"), "submissions"
    if not names:
        m = re.search(rf"(?P<n>{_NAME})(?:'|’)s\s+(?P<what>profile|media\s*link|media|photos?(?:\s+and\s+intro\w*\s+video)?|videos?|intro\w*\s+video|history)", text, re.I)
        if m:
            names = m.group("n")
            what = m.group("what").lower()
            kind = "media_link" if what.startswith("media link") else "media" if what.startswith(("media", "photo", "video", "intro")) else "profile"
    if not names:
        m = re.search(rf"\b(?P<what>profile|media\s*link|photos?|videos?)\s+(?:of|for)\s+(?P<n>{_NAME})", text, re.I)
        if m:
            names = m.group("n")
            what = m.group("what").lower()
            kind = "media_link" if what.startswith("media link") else "media" if what.startswith(("photo", "video")) else "profile"
    if not names:
        m = re.match(rf"\s*(?:find|search\s+for|look\s*up|locate|who\s+is)\s+(?P<n>{_NAME})\s*$", text, re.I)
        if m:
            names, kind = m.group("n"), "find"
    if not names:
        m = re.match(rf"\s*(?:show|get|open|pull\s+up|give)(?:\s+me)?\s+(?P<n>{_NAME})\s*$", text, re.I)
        if m and not re.match(r"(?:everyone|all|the|my|me)\b", m.group("n"), re.I):
            names, kind = m.group("n"), "profile"
    if not names:
        return False
    seg.intent, seg.lookup = "lookup", kind
    seg.talents = split_names(names)
    return True


def parse_segment(text: str) -> Segment:
    seg = Segment(text=_norm_space(text))
    t = _worker(_LEAD.sub("", seg.text), seg)
    t = _norm_space(t)
    low = t.lower()
    if not t:
        return seg

    # ask whether … (follow-up question about sending — attaches to the previous add)
    if re.match(r"(?:and\s+)?(?:ask|check|prompt|remind)\b.*\b(?:send|casting|message|template)\b", low) or re.match(r"(?:and\s+)?(?:ask\s+)?(?:me\s+)?(?:whether|if)\b.*\b(?:send|casting)", low):
        seg.intent = "offer_send"
        return seg

    # share / forward  (also "send <profile|media link|audition> to …")
    if re.match(r"(?:share|forward)\b", low) or (re.match(r"send\b", low) and _SHARE_OBJ.search(t) and re.search(r"\b(?:to|with)\b", low)
                                                 and not re.search(r"\b(?:casting\s+call|template)\b", low)):
        _parse_share(t, seg)
        return seg

    # send a template
    if re.match(r"send\b", low):
        seg.intent = "send"
        rest = _norm_space(t[4:])
        # pronoun form: "send them the Casting Call [for Amazon]"
        m = re.match(rf"(?P<who>them|her|him|these(?:\s+\w+){{0,3}}|those(?:\s+\w+){{0,3}}|the\s+selected\s+\w+|selected\s+\w+)\s+(?:the\s+)?(?P<tpl>.+?)(?:\s+for\s+(?P<proj>.+))?$", rest, re.I)
        if m and (_PRONOUN.match(m.group("who")) or _SELECTED.search(m.group("who"))):
            _who(m.group("who"), seg)
            seg.template = _template(m.group("tpl"))
            _project(m.group("proj"), seg)
            return seg
        # "<template> [for <project>] to <who> [for <project>]"
        m = re.match(r"(?P<tpl>.+?)(?:\s+for\s+(?P<p1>.+?))?\s+to\s+(?P<who>.+?)(?:\s+for\s+(?P<p2>.+))?$", rest, re.I)
        if m:
            seg.template = _template(m.group("tpl"))
            _who(m.group("who"), seg)
            _project(m.group("p1") or m.group("p2"), seg)
            return seg
        # "send the casting call for Amazon" (talents = selection / current context)
        m = re.match(r"(?P<tpl>.+?)\s+for\s+(?P<proj>.+)$", rest, re.I)
        if m:
            seg.template = _template(m.group("tpl"))
            seg.talent_ref = "selected"
            _project(m.group("proj"), seg)
            return seg
        seg.template = _template(rest)
        seg.talent_ref = "selected"
        return seg

    # add to project(s)
    m = re.match(r"(?:add|include)\s+(?P<who>.+?)\s+(?:to|into|in)\s+(?P<where>.+)$", t, re.I)
    if m:
        seg.intent = "add"
        _who(m.group("who"), seg)
        _project(m.group("where"), seg)
        return seg

    # list a stage ("show me everyone in Ask to Test", "which talents are awaiting a test")
    if re.search(r"\bawaiting\s+(?:a\s+)?test|\bwaiting\s+(?:for\s+)?(?:a\s+)?test|\bpending\s+test", low):
        seg.intent, seg.stage = "list", "ask_to_test"
        _project(_after_prep(t), seg)
        return seg

    # move
    m = re.match(r"(?:move|shift|put|change)\s+(?P<who>.+?)\s+(?:to|into)\s+(?P<rest>.+)$", t, re.I)
    if m:
        seg.intent = "move"
        _who(m.group("who"), seg)
        _stage_and_project(m.group("rest"), seg)
        return seg
    m = re.match(r"mark\s+(?P<who>.+?)\s+(?:as\s+)?(?P<rest>(?:reached\s*out|follow[\s-]?up|approved|shortlisted|locked|hold|rejected|not\s+available|not\s+interested|already\s+tested|ask\s+to\s+test)\b.*)$", t, re.I)
    if m:
        seg.intent = "move"
        _who(m.group("who"), seg)
        _stage_and_project(m.group("rest"), seg)
        return seg

    # list everyone in a stage ("show me everyone in Ask to Test", "who is in hold in Amazon")
    if re.search(r"\b(?:everyone|everybody|all\s+(?:the\s+)?talents?|who(?:'s|\s+is|\s+are)?|which\s+talents?|talents?)\b", low) and re.search(r"\bin\b|\bat\b", low):
        key, _amb, rest = nlu.extract_stage_phrase(t, PIPELINE_STAGE_ORDER)
        if key and not re.search(r"(?:'|’)s\b", t):
            seg.intent, seg.stage = "list", key
            _project(_after_prep(rest), seg)
            return seg

    # lookups that read history / profile
    if _parse_lookup(t, seg):
        return seg
    return seg


def _after_prep(text: str) -> Optional[str]:
    m = re.search(r"\b(?:in|for|of|on)\s+(?!the\s+pipeline\b)(?P<p>.+?)\s*(?:project|pipeline)?\s*$", text, re.I)
    return m.group("p") if m else None


def _stage_and_project(rest: str, seg: Segment) -> None:
    key, amb, remaining = nlu.extract_stage_phrase(rest, PIPELINE_STAGE_ORDER)
    if key:
        seg.stage = key
    else:
        first = re.split(r"\s+(?:in|for|on)\s+", rest, maxsplit=1)[0]
        m = nlu.match_stage_phrase(first, PIPELINE_STAGE_ORDER)
        seg.stage = m.key
        seg.stage_text = None if m.key else _norm_space(first)
        remaining = rest[len(first):]
    _project(_after_prep(remaining) or (remaining if re.search(r"\b(?:current|this)\s+project\b", remaining, re.I) else None), seg)


def parse_message(message: str) -> List[Segment]:
    """Split a message into ordered segments and parse each. Empty input → []."""
    text = (message or "").strip()
    if not text:
        return []
    out: List[Segment] = []
    for sentence in _SENTENCE.split(text):
        for piece in _CONNECT.split(sentence):
            piece = (piece or "").strip()
            if piece and re.search(r"[A-Za-z0-9]", piece):
                out.append(parse_segment(piece))
    return out
