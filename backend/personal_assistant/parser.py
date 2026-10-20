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

import difflib
import re
from dataclasses import dataclass, field
from typing import List, Optional

from agents.modules import casting_pipeline_nlu as nlu
from personal_assistant import update_items
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
    update_groups: List[dict] = field(default_factory=list)  # `update` only: [{"names", "ref", "items"}] — see update_items.parse_update
    correction: bool = False                              # the speaker corrected themselves ("…no wait, Disha"): never acted on, asked again
    stage_suggest: List[str] = field(default_factory=list)  # stages it merely RESEMBLES (spelling / sound): offered as cards, never chosen
    stage_filter: Optional[str] = None                    # "selected talents in <stage>"
    lookup: Optional[str] = None                          # profile|media_link|media|history|projects|submissions|tested|find
    recipient: Optional[dict] = None                      # {"kind": number|project_group|casting_director, "value": ...}
    worker_hint: Optional[str] = None                     # "worker 1" | "worker 2" as typed
    verb_heard: Optional[dict] = None                     # {"said": "chair", "meant": "share"}: the action word only SOUNDS like a command word — asked, never assumed
    alt: Optional["Segment"] = None                       # the reading WITHOUT the verb repair (kept so the planner can prefer it when its names are real)


# ---------------------------------------------------------------------------------------------------------
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")
_VERBS = r"(?:send|add|move|share|show|find|mark|put|ask|forward|get|give|list|include|update)"
_CONNECT = re.compile(
    r"\s*,?\s*\b(?:and\s+then|and\s+also|then|also|after\s+that|afterwards|plus)\b\s*,?\s*"
    r"|\s*,\s*(?=" + _VERBS + r"\b)"
    r"|\s+and\s+(?=" + _VERBS + r"\b)"
    r"|\s*,?\s*\band\s+(?=(?:tell\s+me|which\s+projects?|what\s+projects?|has\s+(?:she|he|they)\b|her\s|his\s|their\s))"
    r"|\s*,\s*(?=tell\s+me\b)",
    re.I,
)
_LEAD = re.compile(r"^(?:(?:hey|hi|ok|okay|so|please|pls|kindly|can\s+you|could\s+you|would\s+you|i\s+want\s+you\s+to|i'd\s+like\s+you\s+to|"
                   r"i'?d\s+like\s+to|i\s+would\s+like\s+to|i\s+want\s+to|i\s+need\s+to|let'?s|go\s+ahead\s+and|just|well|now|alright|right)\b[\s,]*)+", re.I)

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


_WITH_TAIL = re.compile(r"^(?P<head>.+?)\s+with\s+(?P<who>[A-Za-z][A-Za-z.'’\-]*(?:\s+[A-Za-z][A-Za-z.'’\-]*){0,3}(?:\s*(?:,|&|\band\b)\s*[A-Za-z][A-Za-z.'’\-]*(?:\s+[A-Za-z][A-Za-z.'’\-]*){0,2})*)$", re.I)
_NOT_NAMES = {"the", "a", "an", "this", "that", "these", "those", "worker", "template", "whatsapp", "casting", "group"}


def _split_with(text: str) -> tuple:
    """"A and B with Kripa Trivedi" → ("A and B", "Kripa Trivedi"). A tail that starts like a phrase rather than a name
    ("with the casting director") is left where it was."""
    m = _WITH_TAIL.match(_norm_space(text or ""))
    if not m or m.group("who").split()[0].lower() in _NOT_NAMES:
        return text, None
    return m.group("head"), m.group("who")


def _template(raw: str) -> Optional[str]:
    t = _norm_space(_TEMPLATE_CLEAN.sub(" ", raw or ""))
    return t or None


# --- intent detection --------------------------------------------------------------------------------------
_SHARE_OBJ = re.compile(r"\b(?:profile|media\s*(?:link|kit)|media|portfolio|audition|photos?|pictures?|video|intro(?:duction)?\s+video)\b", re.I)
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
    m = re.search(rf"(?P<n>{_NAME})(?:'|’)s\s+(?:profile|media\s*(?:link|kit)|media|portfolio|audition|photos?|video)", body, re.I)
    if not m:
        m = re.search(rf"(?:profile|media\s*(?:link|kit)|media|portfolio|audition)\s+(?:of|for)\s+(?P<n>{_NAME})", body, re.I)
    if not m:
        m = _share_name_without_possessive(body)
    if m:
        # "…media link of Angela with the casting director": the name stops at the preposition that starts the recipient
        seg.talents = split_names(re.split(r"\s+(?:with|to|for|via|at)\b", m.group("n"), maxsplit=1, flags=re.I)[0])
    elif re.search(r"\b(?:her|his|their)\s+(?:profile|audition|media\s*(?:link|kit)|photos?)\b", body, re.I):
        seg.talent_ref = "previous"     # "send her media link to …" — the talent just discussed
    elif re.search(r"\b(?:this|the|that)\s+(?:profile|audition|media\s*(?:link|kit))\b|\bit\b|\bthem\b", body, re.I):
        seg.talent_ref = "selected"     # "send this profile to …" — the one on screen / selected
    seg.lookup = "media_link" if re.search(r"media\s*(?:link|kit)|media", body, re.I) else ("audition" if re.search(r"audition", body, re.I) else "profile")


_OBJ = r"(?:profile|media\s*(?:link|kit)|media|portfolio|audition|photos?|videos?)"
_NOT_A_NAME_START = {"with", "to", "for", "via", "at", "the", "this", "that", "it", "them", "her", "his", "their", "these", "those", "of", "and", "my"}


def _share_name_without_possessive(body: str):
    """Speech engines often drop the "'s": "share shivi rajput profile" / "share shivi rajput media link to the group" / "share profile shivi rajput".
    The name is the words next to the item word; a recipient preposition or a pronoun is never the start of a name."""
    m = re.match(rf"\s*(?:share|forward|send)\s+(?:the\s+)?(?P<n>[A-Za-z][A-Za-z.'’\-]*(?:\s+[A-Za-z][A-Za-z.'’\-]*){{0,3}}?)\s+{_OBJ}\b", body, re.I)
    if m and m.group("n").split()[0].lower() not in _NOT_A_NAME_START and not re.fullmatch(_OBJ, m.group("n"), re.I):
        return m
    m = re.match(rf"\s*(?:share|forward|send)\s+(?:the\s+)?{_OBJ}\s+(?P<n>[A-Za-z][A-Za-z.'’\-]*(?:\s+[A-Za-z][A-Za-z.'’\-]*){{0,3}})", body, re.I)
    if m and m.group("n").split()[0].lower() not in _NOT_A_NAME_START:
        return m
    return None


_PRON = r"(?:she|he|they|her|him|them|their|his)"


def _parse_pronoun_lookup(text: str, seg: Segment) -> bool:
    """"…which projects she has tested for" / "has he tested" / "her submission history" — about the talent of
    the previous action in the same message."""
    low = text.lower()
    kind = None
    # "which projects <she> has tested for" needs a PRONOUN: with a name in that spot ("which projects has Angela tested
    # for") this branch used to claim the sentence and leave it without a talent; the named branch below reads those.
    if re.search(r"\b(?:which|what)\s+projects?\b.*\b" + _PRON + r"\b.*\btested\b|\bhas\s+" + _PRON + r"\s+(?:ever\s+)?(?:tested|auditioned)\b|\bhave\s+they\s+tested\b", low):
        kind = "tested"
    elif re.search(r"\b(?:which|what)\s+projects?\b.*\b" + _PRON + r"\b|\bprojects?\s+(?:is|has|was)\s+" + _PRON + r"\b", low):
        kind = "projects"
    elif re.search(r"\b(?:her|his|their)\s+submission\s+history\b", low):
        kind = "submissions"
    elif re.search(r"\b(?:her|his|their)\s+media\s*(?:link|kit)\b", low):
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
        m = re.search(rf"(?P<n>{_NAME})(?:'|’)s\s+(?P<what>profile|media\s*(?:link|kit)|media|photos?(?:\s+and\s+intro\w*\s+video)?|videos?|intro\w*\s+video|history)", text, re.I)
        if m:
            names = m.group("n")
            what = re.sub(r"\s+", " ", m.group("what").lower())
            kind = "media_link" if re.match(r"media ?(link|kit)", what) else "media" if what.startswith(("media", "photo", "video", "intro")) else "profile"
    if not names:
        m = re.search(rf"\b(?P<what>profile|media\s*(?:link|kit)|photos?|videos?)\s+(?:of|for)\s+(?P<n>{_NAME})", text, re.I)
        if m:
            names = m.group("n")
            what = re.sub(r"\s+", " ", m.group("what").lower())
            kind = "media_link" if re.match(r"media ?(link|kit)", what) else "media" if what.startswith(("photo", "video")) else "profile"
    if not names:
        m = re.match(rf"\s*(?:find|search\s+for|look\s*up|locate|who\s+is)\s+(?P<n>{_NAME})\s*$", text, re.I)
        if m:
            names, kind = m.group("n"), "find"
    if not names:
        m = re.match(rf"\s*(?:show|get|open|pull\s+up|give)(?:\s+me)?\s+(?P<n>{_NAME})\s*$", text, re.I)
        if m and not re.match(r"(?:everyone|all|the|my|me)\b", m.group("n"), re.I):
            names, kind = m.group("n"), "profile"
            tail = re.search(r"\s+media\s*(?:link|kit)$", names, re.I)        # "show angela kumar media kit" — the object, not part of the name
            if tail:
                names, kind = names[: tail.start()], "media_link"
    if not names:
        return False
    seg.intent, seg.lookup = "lookup", kind
    seg.talents = split_names(names)
    return True


_UPDATE_SYNONYM = re.compile(r"^\s*(?:please\s+)?(?:set|change|edit|modify|amend|correct|fix|alter)\s+(?!status\b)", re.I)


def _as_update(text: str) -> str:
    """"set / change / edit … <talent>'s height" is an UPDATE when it names a profile item the update command supports (height, age, location,
    Instagram, WhatsApp group, introduction video, pictures); without such an item ("change Shivi to Approved") it stays what it was — a move."""
    m = _UPDATE_SYNONYM.match(text or "")
    if not m:
        return text
    rest = text[m.end():]
    if any(kind in ("field", "media") for _a, _b, kind, _k in update_items.find_items(rest)):
        return "update " + rest
    return text


def parse_segment(text: str) -> Segment:
    """One clause → one Segment. The clause is read as said; only if that yields no command (or glued the action word into a name) is the action word
    repaired — an inflection ("moves", "sent", "updated", "up date") silently, a SOUND-ALIKE ("shear", "moove", "sand") with a question attached."""
    seg = _parse_core(text)
    fixed = _repair_verb(text, seg)
    return fixed or seg


def _parse_core(text: str) -> Segment:
    seg = Segment(text=_norm_space(text))
    t = _worker(_LEAD.sub("", seg.text), seg)
    t = _norm_space(t)
    low = t.lower()
    if not t:
        return seg
    text = _as_update(text)

    # update <talent>'s <profile items>   (fields / media) — see update_items. The RAW text is parsed so a pasted group name keeps its exact spacing.
    if update_items.is_update(text):
        groups = update_items.parse_update(text)
        if groups:
            seg.intent, seg.update_groups = "update", groups
        return seg

    # "In MK Jewels film 1 move Shivi to Approved" — the project said first
    lead = _project_first(t)
    if lead:
        return lead

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
        # "send Shivi Rajput the casting call [for MK Jewels film 1]" — the named talent first
        m = re.match(r"(?P<who>[A-Za-z][A-Za-z.'’\-]*(?:\s+[A-Za-z][A-Za-z.'’\-]*){0,3}?)\s+the\s+(?P<tpl>.+?)(?:\s+for\s+(?P<proj>.+))?$", rest, re.I)
        if m and m.group("who").split()[0].lower() not in _NOT_A_NAME_START and not _PRONOUN.match(m.group("who")):
            _who(m.group("who"), seg)
            seg.template = _template(m.group("tpl"))
            _project(m.group("proj"), seg)
            return seg
        # "<template> [for <project>] to <who> [for <project>]"
        m = re.match(r"(?P<tpl>.+?)(?:\s+for\s+(?P<p1>.+?))?\s+to\s+(?P<who>.+?)(?:\s+for\s+(?P<p2>.+))?$", rest, re.I)
        if m and _norm_space(m.group("tpl")).lower() in _BARE_PRONOUNS:
            seg.intent = "unknown"                                  # "send it to Embassy One" with nothing it could refer to: say so, never guess
            return seg
        if m:
            seg.template = _template(m.group("tpl"))
            _who(m.group("who"), seg)
            _project(m.group("p1") or m.group("p2"), seg)
            return seg
        # "send the casting call for Amazon" (talents = selection / current context) — or, said the other way round,
        # "send the casting for Amazon and Pepsi with Kripa Trivedi": the names after the LAST "with" are the talents
        m = re.match(r"(?P<tpl>.+?)\s+for\s+(?P<proj>.+)$", rest, re.I)
        if m:
            seg.template = _template(m.group("tpl"))
            proj, who = _split_with(m.group("proj"))
            if who:
                _who(who, seg)
            else:
                seg.talent_ref = "selected"
            _project(proj, seg)
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
    if not m:
        pm = re.match(r"put\s+(?P<who>.+?)\s+in\s+(?P<rest>.+)$", t, re.I)       # "put Shivi in approved in <project>": "in" is a stage only if a stage follows
        if pm and _exact_stage_in(" ".join(pm.group("rest").split()[:3])):
            m = pm
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
            _project(_after_prep(_strip_stage_words(rest, key)), seg)
            return seg

    # lookups that read history / profile
    if _parse_lookup(t, seg):
        return seg
    return seg


# --- speech tolerance for the ACTION word ------------------------------------------------------------------------------------------------
# An inflection of a command word ("moves", "sent", "updated", "up date") is the same word: read as that command. A word that merely SOUNDS like a
# command word ("shear", "chair" → share; "moove" → move; "sand" → send) is only a hypothesis: the clause must then be a complete, well-formed
# command of that kind, the planner must find the names in it in the real data, and the task asks "Did you mean to share?" before anything else.
# Nothing here ever executes anything — every consequential action still waits for the normal approval.
_INFLECT = {"shares": "share", "shared": "share", "sharing": "share", "moves": "move", "moved": "move", "moving": "move", "sends": "send", "sent": "send",
            "sending": "send", "updates": "update", "updated": "update", "updating": "update", "adds": "add", "added": "add", "adding": "add",
            "shows": "show", "showing": "show"}
_SOUNDS = {"shear": "share", "sheer": "share", "sher": "share", "chair": "share", "cher": "share", "shair": "share", "shayre": "share", "shere": "share",
           "moove": "move", "mooved": "move", "muve": "move", "moov": "move", "mood": "move", "sand": "send", "cent": "send", "sned": "send"}
_EXPECT = {"share": {"share"}, "move": {"move"}, "send": {"send", "share"}, "update": {"update"}, "add": {"add"}, "show": {"lookup", "list"}}
_COMMAND_WORDS = set(_EXPECT) | {"find", "mark", "put", "shift", "include", "forward", "list", "get", "give", "change", "set", "edit", "ask", "open"}


def _verb_candidate(word: str) -> Optional[tuple]:
    """→ (canonical verb, tier) where tier 1 = an inflection (safe), 2 = sounds like it (needs a question). None for an ordinary word."""
    w = (word or "").lower()
    if not w or w in _COMMAND_WORDS:
        return None
    if w in _INFLECT:
        return _INFLECT[w], 1
    if w in _SOUNDS:
        return _SOUNDS[w], 2
    if len(w) >= 4:
        from personal_assistant import phonetic
        best = max(((phonetic.similarity(w, v), v) for v in ("share", "move", "send", "update")), default=(0.0, ""))
        if best[0] >= 0.9:
            return best[1], 2
    return None


def _repair_verb(text: str, seg: Segment) -> Optional[Segment]:
    body = _norm_space(_LEAD.sub("", _norm_space(text)))
    if not body or seg.correction:
        return None
    m = re.match(r"up[\s-]+date\b", body, re.I)
    if m:
        word, canon, tier, rest = "update", "update", 1, body[m.end():]
        if seg.intent != "unknown":
            return None
    else:
        wm = re.match(r"[A-Za-z']+", body)
        if not wm:
            return None
        word, rest = wm.group(0), body[wm.end():]
        cand = _verb_candidate(word)
        if not cand:
            return None
        canon, tier = cand
        # only when the clause as said gave no command, or glued the action word onto a name ("chair Shivi Rajput's profile" → a lookup of "chair Shivi Rajput")
        glued = seg.intent == "lookup" and bool(seg.talents) and seg.talents[0].lower().startswith(word.lower() + " ")
        if seg.intent != "unknown" and not glued:
            return None
    fixed = _parse_core(canon + rest)
    if fixed.intent not in _EXPECT[canon] or not (fixed.talents or fixed.talent_ref or fixed.project_text or fixed.update_groups):
        return None
    if fixed.talents and fixed.talents[0].lower().startswith(word.lower() + " "):
        return None
    fixed.text = _norm_space(text)
    if tier == 2:
        fixed.verb_heard = {"said": word.lower(), "meant": canon}
        fixed.alt = seg if seg.intent != "unknown" else None
    return fixed


# --- word order: the project said first ------------------------------------------------------------------------------------------------
_FIRST_VERBS = ("move", "shift", "put", "add", "include", "send", "share", "forward", "mark")


def _project_first(t: str) -> Optional[Segment]:
    """"In MK Jewels film 1 move Shivi to Approved" → the same command with the project last. The project is the words up to the first command word,
    and the rewrite is accepted only if it then reads as a complete command of that kind with exactly that project."""
    m = re.match(r"(?:in|for|on)\s+(?P<body>.+)$", t, re.I)
    if not m:
        return None
    words = m.group("body").split()
    for k in range(1, len(words) - 1):
        verb = words[k].lower()
        if verb not in _FIRST_VERBS:
            continue
        proj, cmd = " ".join(words[:k]), " ".join(words[k:])
        cand = _parse_core(f"{cmd} {'for' if verb == 'send' else 'in'} {proj}")
        if cand.intent in ("move", "add", "send", "share") and cand.project_text and _norm_space(cand.project_text).lower() == proj.lower():
            cand.text = _norm_space(t)
            return cand
    return None


def _strip_stage_words(rest: str, key: Optional[str]) -> str:
    """`extract_stage_phrase` leaves a hyphenated/compound stage name ("Follow-up", "Ask to Test") in the remainder; the project
    clause must not start with the stage's own words, or a project name can never match them."""
    if not key:
        return rest
    words = [w for w in re.split(r"[_\s-]+", key) if w]
    if not words:
        return rest
    return re.sub(r"\b" + r"[\s_-]*".join(map(re.escape, words)) + r"\b", " ", rest, count=1, flags=re.I)


def _after_prep(text: str) -> Optional[str]:
    m = re.search(r"\b(?:in|for|of|on)\s+(?!the\s+pipeline\b)(?P<p>.+?)\s*(?:project|pipeline)?\s*$", text, re.I)
    return m.group("p") if m else None


def _exact_stage(phrase: str) -> Optional[str]:
    """The stage a phrase NAMES — same words ignoring case / hyphen / underscore ("follow-up" = "Follow Up"), or a registered shorthand
    ("approve"). No fuzzy matching: this is what may be applied without asking."""
    registry = nlu.build_stage_registry(PIPELINE_STAGE_ORDER)
    return registry.get(re.sub(r"\s+", " ", re.sub(r"[-_]", " ", (phrase or "").lower())).strip())


def _exact_stage_in(text: str) -> Optional[str]:
    words = re.findall(r"[A-Za-z][A-Za-z']*", text or "")
    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            k = _exact_stage(" ".join(words[i:i + n]))
            if k:
                return k
    return None


def stage_resembles(phrase: str) -> List[str]:
    """Stages a phrase merely looks or sounds like — the nearest first. Used to PROPOSE cards for a stage that was not named exactly."""
    from personal_assistant import phonetic
    registry = nlu.build_stage_registry(PIPELINE_STAGE_ORDER)
    key = re.sub(r"\s+", " ", re.sub(r"[-_]", " ", (phrase or "").lower())).strip()
    out: List[str] = []
    for c in difflib.get_close_matches(key, list(registry.keys()), n=4, cutoff=0.6):
        out.append(registry[c])
    compact = key.replace(" ", "")
    for st in PIPELINE_STAGE_ORDER:
        if phonetic.similarity(compact, st.replace("_", "")) >= phonetic.PHON_MIN:
            out.append(st)
    return list(dict.fromkeys(out))[:3]


def _stage_and_project(rest: str, seg: Segment) -> None:
    key, amb, remaining = nlu.extract_stage_phrase(rest, PIPELINE_STAGE_ORDER)
    if key and _exact_stage_in(rest) != key:
        # found only through a spelling correction: never applied silently — a move into the wrong stage is a real change
        seg.stage_text = _norm_space(" ".join(w for w in rest.split() if w.lower().strip(",.") not in remaining.lower().split())) or _norm_space(rest)
        seg.stage_suggest = list(dict.fromkeys([key] + stage_resembles(seg.stage_text)))[:3]
    elif key:
        seg.stage = key
    else:
        first = re.split(r"\s+(?:in|for|on)\s+", rest, maxsplit=1)[0]
        m = nlu.match_stage_phrase(first, PIPELINE_STAGE_ORDER)
        if m.key and _exact_stage(first) == m.key:
            seg.stage = m.key
        else:
            seg.stage_text = _norm_space(first)
            seg.stage_suggest = list(dict.fromkeys(([m.key] if m.key else []) + stage_resembles(first)))[:3]
        remaining = rest[len(first):]
    _project(_after_prep(remaining) or (remaining if re.search(r"\b(?:current|this)\s+project\b", remaining, re.I) else None), seg)


_BARE_PRONOUNS = {"it", "this", "that", "them", "him", "her", "these", "those"}
_SEND_IT = re.compile(r"^(?:send|share|forward)\s+(?:it|this|that|the\s+link|the\s+media\s*(?:link|kit)|the\s+kit)\s+(?:to|with)\s+(?P<r>.+?)\s*$", re.I)


def _follow_up_share(seg: Segment, prev: Optional[Segment]) -> Optional[Segment]:
    """"Show me Angela's media kit and send it to Embassy One": the second clause has no template and no talent of its own — "it" is
    the link just shown. Without this it was read as a template send to a *talent* called "Embassy One". Only when the previous
    clause was a lookup/share of a profile or media link; anything else is left to the normal rules (and stays unresolved)."""
    if prev is None or prev.intent not in ("lookup", "share") or prev.lookup not in ("media_link", "media", "profile"):
        return None
    m = _SEND_IT.match(seg.text.strip(" .,;"))
    if not m:
        return None
    out = Segment(text=seg.text, intent="share", talent_ref="previous", lookup="media_link" if prev.lookup in ("media_link", "media") else "profile")
    out.recipient = _recipient(f"send it to {m.group('r')}")
    return out


# Spoken hesitations are noise; a spoken CORRECTION is not — "move Doha Kumar, no wait, Disha Kumar" contains two names and no way to
# know which one was meant, so neither is acted on (the clause before the correction is held too) and the user is asked to say it once.
_FILLER = re.compile(r"(?<![\w'])(?:um+|uhm+|uh+|erm+|hmm+)(?![\w'])[,.]?\s*", re.I)
# KNOWN LIMITATION (deliberate, fail-safe): a clause containing a standalone "No" between words ("Say No More", "Project No. 5") is read as a
# self-correction and held — the user is told to say it once — so such a real name cannot be used by voice. Typed commands go through the
# same parser; use a different spelling or the card search for those names.
_CORRECTION = re.compile(
    r"\b(?:no\s*,?\s*wait|wait\s*,?\s*no|i\s+mean(?:t)?|sorry|scratch\s+that|never\s*mind|cancel\s+that|correction|make\s+that|or\s+rather|actually)\b"
    r"|[\u2014\u2013]\s*no\b|--\s*no\b"                                         # "Doha—no, Disha"
    r"|\b\w+[,.]?\s+no[,.]\s+\w+"                                              # "Doha, no, Disha"
    r"|(?<=[A-Za-z])\s+no\s+(?=[A-Za-z])", re.I)                                  # "Doha no Disha" (speech engines drop the punctuation)


_COMMAND_VERB = re.compile(r"\s+(?=(?:add|include|move|shift|put|send|forward|share|show|mark|list|update)\b)", re.I)


def _verb_split(piece: str) -> List[str]:
    """Browser speech recognition usually inserts NO punctuation, so "add Sahal to Toba move Disha to Follow Up in Amazon send the casting
    call to Vikram" reaches us as one run-on clause. Split it before a command verb — but only where BOTH halves are then complete,
    understood commands (a verb inside a name, or a half-finished clause, is never split), leftmost first."""
    words_at = [m.end() for m in _COMMAND_VERB.finditer(piece)]
    for cut in words_at:
        left, right = piece[:cut].strip(), piece[cut:].strip()
        if len(left.split()) < 3 or len(right.split()) < 3:
            continue
        left_seg = parse_segment(left)
        right_ok = parse_segment(right).intent != "unknown" or bool(_SEND_IT.match(right.strip(" .,;")))      # "…send it to X" refers to the clause before it
        if left_seg.intent != "unknown" and right_ok:
            return [left] + _verb_split(right)
    return [piece]


def parse_message(message: str) -> List[Segment]:
    """Split a message into ordered segments and parse each. Empty input → []."""
    text = _FILLER.sub("", (message or "")).strip()
    if not text:
        return []
    out: List[Segment] = []
    pieces = [sub for sentence in _SENTENCE.split(text) for piece in _CONNECT.split(sentence) for sub in _verb_split((piece or "").strip())]
    for piece in pieces:
        piece = (piece or "").strip()
        if piece and re.search(r"[A-Za-z0-9]", piece) and _CORRECTION.search(piece):
            if out and out[-1].intent != "unknown":
                out[-1] = Segment(text=out[-1].text, intent="unknown", correction=True)      # the clause being corrected is held as well
            out.append(Segment(text=piece, intent="unknown", correction=True))
            continue
        if piece and re.search(r"[A-Za-z0-9]", piece):
            seg = parse_segment(piece)
            follow = _follow_up_share(seg, out[-1] if out else None) if seg.intent in ("send", "unknown") else None
            out.append(follow or seg)
    return out
