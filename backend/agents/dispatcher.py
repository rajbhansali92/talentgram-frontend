"""Command Dispatcher — the single entry point for every inbound WhatsApp
agent message, regardless of which transport delivered it (simulated
webhook today; a real WhatsApp Web listener or Cloud API webhook later —
see docs/claude/whatsapp-agent-platform.md for the transport seam).

Pipeline: Command Parser → Intent Engine → Confirmation Layer →
Action Executor → Audit Log, exactly as specified. This module is the
only place those five stages are wired together; every stage above it
(registry, conversation, parser, confirmation) is domain-agnostic, and
every stage below it (agents/modules/*) knows nothing about WhatsApp.
"""
from __future__ import annotations

import logging
from typing import Optional

from agents import audit, conversation, registry
from agents.confirmation import (
    CANCELLED_MESSAGE,
    EDIT_PROMPT,
    UNRECOGNIZED_CONFIRMATION_REPLY,
    UNRECOGNIZED_EDIT_REPLY,
    build_confirmation_message,
)
from agents.models import DispatchResult, ExecContext
from agents.parser import (
    detect_trigger,
    extract_initial_fields,
    next_missing_field,
    parse_confirmation_reply,
    parse_edit_instructions,
)

logger = logging.getLogger(__name__)


def _normalize_sender(raw: str) -> str:
    """Local normalization for the *sender identity* (allowlist matching),
    kept intentionally simple/strict — this is a security check, not a
    user-facing field, so it does not share the CRM module's lenient
    phone-field validator."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return digits


async def _collect_or_advance(agent, intent, conv: dict, text: str) -> DispatchResult:
    """Handle one turn while the conversation is in "collecting" or
    "editing" step. Returns the reply; caller is responsible for the
    audit log entry."""
    collected = dict(conv.get("collected") or {})

    if conv["step"] == "editing":
        edits = parse_edit_instructions(text, intent.fields)
        if not edits:
            return DispatchResult(handled=True, reply=UNRECOGNIZED_EDIT_REPLY)
        for key, raw_value in edits.items():
            field = next((f for f in intent.fields if f.key == key), None)
            if not field:
                continue
            result = field.validate(raw_value)
            if not result.ok:
                return DispatchResult(handled=True, reply=result.error)
            collected[key] = result.value
        await conversation.update_conversation(
            agent.agent_id, conv["phone"], collected=collected, step="collecting"
        )
    else:
        # "collecting": this message answers the question for the next
        # missing field.
        missing = next_missing_field(intent, collected)
        if missing:
            result = missing.validate(text.strip())
            if not result.ok:
                return DispatchResult(handled=True, reply=result.error)
            collected[missing.key] = result.value
            await conversation.update_conversation(
                agent.agent_id, conv["phone"], collected=collected
            )

    still_missing = next_missing_field(intent, collected)
    if still_missing:
        await conversation.update_conversation(
            agent.agent_id, conv["phone"], collected=collected, step="collecting"
        )
        return DispatchResult(handled=True, reply=still_missing.question)

    await conversation.update_conversation(
        agent.agent_id, conv["phone"], collected=collected, step="confirming"
    )
    return DispatchResult(
        handled=True, reply=build_confirmation_message(intent, collected)
    )


async def handle_inbound_message(
    *,
    group_name: str,
    sender_phone: str,
    text: str,
    sender_name: Optional[str] = None,
    sender_is_group_member: Optional[bool] = None,
) -> DispatchResult:
    phone = _normalize_sender(sender_phone)
    raw_message = text or ""

    try:
        resolved = await registry.resolve_agent_for_group(group_name)
        if not resolved:
            # Messages from groups no agent owns are silently ignored.
            return DispatchResult(handled=False)
        agent, config = resolved

        if not registry.is_sender_allowed(
            config, phone, is_group_member=sender_is_group_member
        ):
            await audit.log_turn(
                agent_id=agent.agent_id,
                group_name=group_name,
                sender_phone=phone,
                raw_message=raw_message,
                error="sender_not_allowlisted",
            )
            return DispatchResult(handled=False)

        conv = await conversation.get_conversation(agent.agent_id, phone)
        if conv and conversation.is_expired(conv):
            await conversation.clear_conversation(agent.agent_id, phone)
            conv = None

        # A fresh trigger always restarts, even mid-conversation.
        fresh_intent = detect_trigger(agent, raw_message)

        if conv is None or fresh_intent is not None:
            intent = fresh_intent or (
                registry.get_intent(agent, conv["intent_id"]) if conv else None
            )
            if intent is None:
                # No active conversation and this message doesn't open one
                # — unrelated chatter in the group, ignore.
                return DispatchResult(handled=False)

            initial_raw = extract_initial_fields(intent, raw_message)
            collected: dict = {}
            initial_errors: list = []
            for field in intent.fields:
                raw_value = initial_raw.get(field.key)
                if not raw_value:
                    continue
                result = field.validate(raw_value)
                if result.ok:
                    collected[field.key] = result.value
                else:
                    # Invalid initial values are treated as not-yet-collected
                    # (not fatal) — but we tell the user exactly what was
                    # wrong with what they sent, then still ask for it via
                    # the normal missing-field flow below, rather than
                    # silently discarding it as if it had never been sent.
                    initial_errors.append(result.error)

            conv = await conversation.start_conversation(
                agent_id=agent.agent_id,
                phone=phone,
                group_name=group_name,
                intent_id=intent.intent_id,
                collected=collected,
            )
            # Fields already extracted from `raw_message` above — just
            # check what (if anything) is still missing and reply
            # accordingly, rather than routing through _collect_or_advance
            # (which is for turns that *answer* a pending question, not
            # the message that opens the conversation).
            missing = next_missing_field(intent, collected)
            if missing:
                reply = ("\n\n".join(initial_errors) + "\n\n" + missing.question) if initial_errors else missing.question
            else:
                await conversation.update_conversation(
                    agent.agent_id, phone, collected=collected, step="confirming"
                )
                reply = build_confirmation_message(intent, collected)
            await audit.log_turn(
                agent_id=agent.agent_id,
                group_name=group_name,
                sender_phone=phone,
                raw_message=raw_message,
                conversation_id=str(conv.get("_id") or ""),
                parsed_intent=intent.intent_id,
                parsed_fields=collected,
                validation_errors=initial_errors or None,
            )
            return DispatchResult(handled=True, reply=reply)

        # Existing, non-expired conversation, no fresh trigger in this message.
        intent = registry.get_intent(agent, conv["intent_id"])
        if intent is None:
            await conversation.clear_conversation(agent.agent_id, phone)
            return DispatchResult(handled=False)

        if conv["step"] == "confirming":
            action = parse_confirmation_reply(raw_message)
            if action == "approve":
                ctx = ExecContext(
                    agent_id=agent.agent_id,
                    group_name=group_name,
                    sender_phone=phone,
                    sender_name=sender_name,
                    conversation_id=str(conv.get("_id") or ""),
                )
                exec_result = await intent.executor(conv.get("collected") or {}, ctx)
                await conversation.clear_conversation(agent.agent_id, phone)
                await audit.log_turn(
                    agent_id=agent.agent_id,
                    group_name=group_name,
                    sender_phone=phone,
                    raw_message=raw_message,
                    conversation_id=str(conv.get("_id") or ""),
                    parsed_intent=intent.intent_id,
                    parsed_fields=conv.get("collected"),
                    confirmation_action="approve",
                    execution_result=exec_result.message,
                    error=exec_result.error,
                )
                return DispatchResult(handled=True, reply=exec_result.message)

            if action == "edit":
                await conversation.update_conversation(
                    agent.agent_id, phone, step="editing"
                )
                await audit.log_turn(
                    agent_id=agent.agent_id,
                    group_name=group_name,
                    sender_phone=phone,
                    raw_message=raw_message,
                    conversation_id=str(conv.get("_id") or ""),
                    parsed_intent=intent.intent_id,
                    confirmation_action="edit",
                )
                return DispatchResult(handled=True, reply=EDIT_PROMPT)

            if action == "cancel":
                await conversation.clear_conversation(agent.agent_id, phone)
                await audit.log_turn(
                    agent_id=agent.agent_id,
                    group_name=group_name,
                    sender_phone=phone,
                    raw_message=raw_message,
                    conversation_id=str(conv.get("_id") or ""),
                    parsed_intent=intent.intent_id,
                    confirmation_action="cancel",
                )
                return DispatchResult(handled=True, reply=CANCELLED_MESSAGE)

            await audit.log_turn(
                agent_id=agent.agent_id,
                group_name=group_name,
                sender_phone=phone,
                raw_message=raw_message,
                conversation_id=str(conv.get("_id") or ""),
                parsed_intent=intent.intent_id,
                validation_errors=["unrecognized_confirmation_reply"],
            )
            return DispatchResult(handled=True, reply=UNRECOGNIZED_CONFIRMATION_REPLY)

        # step in ("collecting", "editing")
        result = await _collect_or_advance(agent, intent, conv, raw_message)
        await audit.log_turn(
            agent_id=agent.agent_id,
            group_name=group_name,
            sender_phone=phone,
            raw_message=raw_message,
            conversation_id=str(conv.get("_id") or ""),
            parsed_intent=intent.intent_id,
        )
        return result

    except Exception as exc:  # graceful failure — never a raw 500 to the transport
        logger.exception("whatsapp agent dispatch failed")
        try:
            await audit.log_turn(
                agent_id=None,
                group_name=group_name,
                sender_phone=phone,
                raw_message=raw_message,
                error=str(exc),
            )
        except Exception:
            pass
        return DispatchResult(
            handled=True,
            reply="Something went wrong on our end. Please try again in a moment.",
        )
