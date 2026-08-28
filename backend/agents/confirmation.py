"""Confirmation Engine — builds the "did I get this right?" message.

Nothing is ever written to the database until the user replies 1 (Approve)
to this exact message. Purely presentational + generic: it only knows
about `IntentDefinition.fields` (label + collected value), never about
what those fields mean.
"""
from __future__ import annotations

from typing import Dict

from agents.models import IntentDefinition


def build_confirmation_message(intent: IntentDefinition, collected: Dict[str, str]) -> str:
    lines = [intent.summary_title, ""]
    for f in intent.fields:
        value = (collected.get(f.key, "") or "").strip()
        if not value:
            # Optional fields (required=False) that were never mentioned
            # are omitted entirely rather than shown as "—" — the summary
            # should only list what was actually understood.
            if not f.required:
                continue
            value = "—"
        lines.append(f"{f.label}:")
        lines.append(value)
        lines.append("")
    lines.append("Reply:")
    lines.append("1 → Approve")
    lines.append("2 → Edit")
    lines.append("3 → Cancel")
    return "\n".join(lines)


EDIT_PROMPT = (
    "Tell me what to change.\n"
    "Example:\n"
    "Role = Casting Director"
)


def build_generic_edit_prompt(intent: IntentDefinition, collected: Dict[str, str]) -> str:
    """Guided Edit Prompts (2026-08-28) — the DEFAULT edit prompt for any
    intent that doesn't supply its own build_edit_prompt hook (currently
    just crm-agent's create_contact; casting-agent's ADD/MOVE/SHARE/SEND
    all override this with something more specific to what they resolved —
    see agents/modules/casting_pipeline.py). Reuses the exact same
    intent.fields/collected data build_confirmation_message already reads
    — no new resolution, just framed as "here's what you can edit" instead
    of "here's what I'm about to do". A field whose FieldSpec.question is
    empty is the established "hidden, never shown" convention (see
    AUTO_CONFIRM_FIELD/PLAN_FIELD/SEND_FORM_EDIT_FIELD) and is skipped
    here exactly as build_confirmation_message already skips it."""
    visible = [f for f in intent.fields if f.question]
    lines = ["EDITING", "", "Current:"]
    any_shown = False
    for f in visible:
        value = (collected.get(f.key, "") or "").strip()
        if not value:
            continue
        lines.append(f"{f.label}: {value}")
        any_shown = True
    if not any_shown:
        lines.pop()  # drop the now-empty "Current:" header
    lines += ["", "Tell me what you want to change."]
    example_field = visible[0] if visible else None
    if example_field:
        lines += ["", "Example:", f"{example_field.label} = <new value>"]
    lines += ["", "Nothing will be executed until you confirm."]
    return "\n".join(lines)


CANCELLED_MESSAGE = (
    "CANCELLED\n\n"
    "Nothing from the pending action was executed or sent.\n\n"
    "You can start a new command whenever you're ready."
)

UNRECOGNIZED_CONFIRMATION_REPLY = (
    "Sorry, I didn't understand that.\n"
    "Reply 1 to Approve, 2 to Edit, or 3 to Cancel."
)

UNRECOGNIZED_EDIT_REPLY = (
    "I couldn't understand that.\n"
    "Try: Role = Casting Director"
)
