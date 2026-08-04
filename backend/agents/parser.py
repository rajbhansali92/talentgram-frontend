"""Command Parser — generic, domain-agnostic text handling.

Nothing in this file knows about "name/phone/role" or any other domain
field. It only knows about the shapes of messages a WhatsApp agent
conversation can receive:
  - a fresh message that might open a new intent ("Save\\nRaj Mehta\\n...")
  - a bare reply to "what's the X?" while collecting fields
  - a confirmation menu reply (1 / 2 / 3, or approve/edit/cancel words)
  - an edit instruction ("Role = Casting Director")

Tolerant of extra whitespace, blank lines, and casing throughout, per the
"tolerate formatting variations" requirement.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from agents.models import AgentDefinition, FieldSpec, IntentDefinition


def _clean_lines(text: str) -> List[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


# Voice-transcript hygiene: common leading filler ("hey", "please", "can
# you", "um") — possibly several chained together — and trailing filler
# ("...please", "...thanks"), plus an immediately-repeated word ("move
# move Sarah" -> "move Sarah", a common speech-to-text stutter artifact).
# Generic (no domain vocabulary), so it lives here rather than in any one
# agent's module — every agent's trigger detection and field extraction
# benefits identically, the same as detect_trigger's glued-digit rule.
_LEADING_FILLER_RE = re.compile(
    r"^\s*(?:(?:hey|hi|ok(?:ay)?|please|um+|uh+|so|can you|could you|would you)[\s,]+)+",
    re.IGNORECASE,
)
_TRAILING_FILLER_RE = re.compile(r"[\s,]+(?:please|thanks|thank you)\.?\s*$", re.IGNORECASE)
_REPEATED_WORD_RE = re.compile(r"\b(\w+)(\s+\1\b)+", re.IGNORECASE)


def clean_voice_transcript(text: str) -> str:
    """Best-effort speech-to-text cleanup, applied once at the top of
    handle_inbound_message to the text used for trigger detection and
    field extraction — NOT to what's stored as the audit log's raw_message
    (that stays verbatim, so the audit trail always shows exactly what was
    received). Missing punctuation and inconsistent capitalization need no
    special handling here: every trigger/field/confirmation matcher in
    this platform is already case- and punctuation-tolerant on its own."""
    working = text or ""
    working = _LEADING_FILLER_RE.sub("", working)
    working = _TRAILING_FILLER_RE.sub("", working)
    working = _REPEATED_WORD_RE.sub(r"\1", working)
    return working.strip()


def detect_trigger(agent: AgentDefinition, text: str) -> Optional[IntentDefinition]:
    """Does this message open a new intent? Matches if the first line
    starts with (case-insensitively) one of the intent's trigger phrases,
    longest trigger first so "new contact" wins over a hypothetical
    shorter "new"."""
    lines = _clean_lines(text)
    if not lines:
        return None
    first = lines[0].lower()
    best: Optional[tuple] = None  # (trigger_len, intent)
    for intent in agent.intents:
        for trig in intent.triggers:
            t = trig.lower().strip()
            matches = (
                first == t
                or first.startswith(t + " ")
                or first.startswith(t + ":")
                # "P5" / "T12"-style shorthand: the trigger literal glued
                # directly to a number, no separator. Generic (benefits any
                # agent's short trigger, not just one domain), and safe —
                # it only fires when the message IS the trigger word
                # immediately followed by digits, not a substring of
                # unrelated text (e.g. "add7" would need to literally start
                # with "add" then a digit; "addressbook" doesn't match
                # since 'r' isn't a digit).
                or (first.startswith(t) and len(first) > len(t) and first[len(t)].isdigit())
            )
            if matches:
                if best is None or len(t) > best[0]:
                    best = (len(t), intent)
    return best[1] if best else None


def extract_initial_fields(intent: IntentDefinition, text: str) -> Dict[str, str]:
    """Pull as many raw field values as possible out of the message that
    opened this intent, in field order. Handles all three example shapes:

      Save                       Add Raj Mehta              New Contact
      Raj Mehta                  9876543210                 Rahul Shah
      9876543210                 Casting Director            9999999999
      Brand Manager                                          Agency Producer

    i.e. the trigger may be its own line, or prefixed onto the first data
    line ("Add Raj Mehta") — either way, whatever data lines remain are
    assigned to fields in declared order. Returns only raw (unvalidated)
    strings; the caller runs each through its FieldSpec.validate.
    """
    lines = _clean_lines(text)
    if not lines:
        return {}

    first = lines[0]
    first_lower = first.lower()
    matched_trigger = None
    for trig in intent.triggers:
        t = trig.lower().strip()
        if first_lower == t:
            matched_trigger = t
            lines = lines[1:]
            break
        if first_lower.startswith(t + " ") or first_lower.startswith(t + ":"):
            matched_trigger = t
            remainder = first[len(trig):].lstrip(" :").strip()
            lines = ([remainder] if remainder else []) + lines[1:]
            break

    result: Dict[str, str] = {}
    for field, raw in zip(intent.fields, lines):
        result[field.key] = raw
    return result


_CONFIRM_APPROVE = {
    "1", "approve", "yes", "y", "confirm", "ok", "okay",
    "go ahead", "proceed", "do it",
}
_CONFIRM_EDIT = {"2", "edit", "change"}
_CONFIRM_CANCEL = {"3", "cancel", "no", "n", "stop"}


def parse_confirmation_reply(text: str) -> Optional[str]:
    """Returns "approve" / "edit" / "cancel", or None if unrecognized.
    Tolerant of trailing punctuation a voice transcript might (or might
    not) include — "Go ahead." / "Yes!" match the same as "Go ahead" /
    "Yes" — purely additive: nothing that matched before stops matching."""
    norm = (text or "").strip().rstrip(".!?").strip().lower()
    if norm in _CONFIRM_APPROVE:
        return "approve"
    if norm in _CONFIRM_EDIT:
        return "edit"
    if norm in _CONFIRM_CANCEL:
        return "cancel"
    return None


_EDIT_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z \-]*?)\s*(?:=|:|-)\s*(.+)$")


def parse_edit_instructions(text: str, fields: List[FieldSpec]) -> Dict[str, str]:
    """Parse one or more "Key = value" / "Key: value" / "Key - value"
    lines against the intent's field labels/aliases (case-insensitive).
    Unrecognized lines are silently skipped — the dispatcher reports back
    if nothing at all was understood."""
    label_map: Dict[str, str] = {}
    for f in fields:
        label_map[f.label.strip().lower()] = f.key
        label_map[f.key.strip().lower()] = f.key
        for alias in f.aliases:
            label_map[alias.strip().lower()] = f.key

    out: Dict[str, str] = {}
    for line in _clean_lines(text):
        m = _EDIT_LINE_RE.match(line)
        if not m:
            continue
        raw_key = m.group(1).strip().lower()
        raw_value = m.group(2).strip()
        field_key = label_map.get(raw_key)
        if field_key and raw_value:
            out[field_key] = raw_value
    return out


def next_missing_field(intent: IntentDefinition, collected: Dict[str, str]) -> Optional[FieldSpec]:
    for field in intent.fields:
        if not field.required:
            continue
        if not (collected.get(field.key) or "").strip():
            return field
    return None
