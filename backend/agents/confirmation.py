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

CANCELLED_MESSAGE = "Cancelled.\nNothing saved."

UNRECOGNIZED_CONFIRMATION_REPLY = (
    "Sorry, I didn't understand that.\n"
    "Reply 1 to Approve, 2 to Edit, or 3 to Cancel."
)

UNRECOGNIZED_EDIT_REPLY = (
    "I couldn't understand that.\n"
    "Try: Role = Casting Director"
)
