"""Audit Log — one entry per inbound message processed by any agent.

Modelled on db.otp_audit_logs (existing pattern for security-sensitive,
append-only event trails). Every turn of every conversation writes one
entry here, whether or not it resulted in a database write, so the full
lifecycle of a command (parsed → confirmed → executed, or rejected /
errored at any step) is reconstructable from this collection alone.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core import db

COLLECTION = "whatsapp_agent_audit_log"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def log_turn(
    *,
    agent_id: Optional[str],
    group_name: str,
    sender_phone: str,
    raw_message: str,
    conversation_id: Optional[str] = None,
    parsed_intent: Optional[str] = None,
    parsed_fields: Optional[Dict[str, str]] = None,
    validation_errors: Optional[List[str]] = None,
    confirmation_action: Optional[str] = None,
    execution_result: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    doc = {
        "timestamp": _now(),
        "agent_id": agent_id,
        "group_name": group_name,
        "sender_phone": sender_phone,
        "raw_message": raw_message,
        "conversation_id": conversation_id,
        "parsed_intent": parsed_intent,
        "parsed_fields": parsed_fields,
        "validation_errors": validation_errors,
        "confirmation_action": confirmation_action,
        "execution_result": execution_result,
        "error": error,
    }
    try:
        await db[COLLECTION].insert_one(doc)
    except Exception:
        # Audit logging must never take down the agent's actual response —
        # this is the one place in the platform where a failure is
        # deliberately swallowed rather than surfaced to the user.
        import logging
        logging.getLogger(__name__).exception("failed to write whatsapp agent audit log")
