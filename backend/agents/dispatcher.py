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
import time
from typing import Optional

from agents import audit, conversation, registry, request_scope, session_context
from agents.confirmation import (
    CANCELLED_MESSAGE,
    EDIT_PROMPT,
    UNRECOGNIZED_CONFIRMATION_REPLY,
    UNRECOGNIZED_EDIT_REPLY,
    build_confirmation_message,
)
from agents.models import DispatchResult, ExecContext
from agents.parser import (
    VOICE_CONFIDENCE_THRESHOLD,
    clean_voice_transcript,
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


class _BlankOnMissing(dict):
    """Renders a missing {placeholder} as empty string instead of raising —
    a question template referencing a not-yet-collected field degrades to
    slightly-generic phrasing rather than crashing the conversation."""
    def __missing__(self, key):
        return ""


def _render_question(template: str, collected: dict) -> str:
    """A domain module's FieldSpec.question may reference already-collected
    values (e.g. "What's {name}'s phone number?") to sound like a real
    conversation instead of a form. Purely generic string substitution —
    this engine still has no idea what "name" means."""
    try:
        return " ".join(template.format_map(_BlankOnMissing(collected)).split())
    except Exception:
        return template


async def _render_confirmation(intent, collected: dict, ctx: ExecContext) -> str:
    """Renders the "did I get this right?" message. Delegates to the
    intent's own `build_confirmation` hook when it supplies one (needed for
    confirmations that must resolve data the sync, DB-free validate/
    extract_fields hooks can't reach, e.g. a talent selector against the
    current session's live listing); otherwise the generic, domain-agnostic
    renderer — unchanged behaviour for every intent that doesn't opt in."""
    if intent.build_confirmation:
        return await intent.build_confirmation(collected, ctx)
    return build_confirmation_message(intent, collected)


async def _collect_or_advance(
    agent, intent, conv: dict, text: str, *, sender_name: Optional[str] = None
) -> DispatchResult:
    """Handle one turn while the conversation is in "collecting" or
    "editing" step. Returns the reply; caller is responsible for the
    audit log entry."""
    collected = dict(conv.get("collected") or {})
    phone = conv["phone"]

    if conv["step"] == "editing":
        if intent.parse_edits_async:
            edit_ctx = ExecContext(
                agent_id=agent.agent_id,
                group_name=conv.get("group_name") or "",
                sender_phone=phone,
                sender_name=sender_name,
                conversation_id=str(conv.get("_id") or ""),
            )
            edits = await intent.parse_edits_async(text, collected, intent.fields, edit_ctx)
        else:
            edit_parser = intent.parse_edits or parse_edit_instructions
            edits = edit_parser(text, intent.fields)
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
            agent.agent_id, phone, collected=collected, step="collecting"
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
                agent.agent_id, phone, collected=collected
            )

    still_missing = next_missing_field(intent, collected)
    if still_missing:
        await conversation.update_conversation(
            agent.agent_id, phone, collected=collected, step="collecting"
        )
        return DispatchResult(handled=True, reply=_render_question(still_missing.question, collected))

    ctx = ExecContext(
        agent_id=agent.agent_id,
        group_name=conv.get("group_name") or "",
        sender_phone=phone,
        sender_name=sender_name,
        conversation_id=str(conv.get("_id") or ""),
    )
    if intent.auto_confirm:
        exec_result = await intent.executor(collected, ctx)
        if exec_result.needs_clarification:
            # The executor's reply is a clarification question, not a
            # completed result — it has already stashed whatever it needs
            # (e.g. in session_context) to interpret the next free-text
            # reply via parse_edits_async. Keep the conversation alive in
            # "editing" step instead of clearing it, same continuation
            # mechanism casting.move gets via build_confirmation.
            await conversation.update_conversation(agent.agent_id, phone, step="editing")
        else:
            await conversation.clear_conversation(agent.agent_id, phone)
        return DispatchResult(handled=True, reply=exec_result.message)

    if intent.try_auto_execute:
        auto_result = await intent.try_auto_execute(collected, ctx)
        if auto_result is not None:
            await conversation.clear_conversation(agent.agent_id, phone)
            return DispatchResult(handled=True, reply=auto_result.message)

    await conversation.update_conversation(
        agent.agent_id, phone, collected=collected, step="confirming"
    )
    return DispatchResult(
        handled=True, reply=await _render_confirmation(intent, collected, ctx)
    )


async def handle_inbound_message(
    *,
    group_name: str,
    sender_phone: str,
    text: str,
    sender_name: Optional[str] = None,
    sender_is_group_member: Optional[bool] = None,
    # Voice transport interface — no speech-to-text engine is wired up yet
    # (see whatsapp-worker/inbound.py's voice-note detection), but the
    # conversation engine already supports both ends of it: pass a
    # transcript's confidence score once one exists and a low-confidence
    # transcript is held for an explicit "I heard: ... Is that correct?"
    # before touching any intent's NLU; pass media_type="voice_note" (with
    # no `text`) for a voice note that couldn't be transcribed at all, so
    # the user gets a clear reply instead of the message being silently
    # dropped.
    transcript_confidence: Optional[float] = None,
    media_type: Optional[str] = None,
) -> DispatchResult:
    phone = _normalize_sender(sender_phone)
    raw_message = text or ""
    # Used for trigger detection, field extraction, and confirmation/edit
    # parsing — a speech-to-text transcript's filler words ("hey", "um",
    # "please") and stutters ("move move X") are stripped here so every
    # matcher downstream sees text that behaves like clean typed input.
    # raw_message itself is left untouched and is what's stored in the
    # audit log — the audit trail always shows exactly what was received.
    working_message = clean_voice_transcript(raw_message)

    # Coarse, always-on timing for the backend-side portion of a turn —
    # real production latency (railway logs on the worker) showed backend
    # request time as roughly half the total. request_scope gives a real
    # per-stage breakdown (auth/mongo/fuzzy/db_write — see
    # casting_pipeline.py) logged alongside this total in the `finally`
    # block below, instead of another guess-and-check profiling pass.
    t0 = time.monotonic()
    dispatched_agent_id: Optional[str] = None
    request_scope.reset()

    try:
        with request_scope.stage("auth"):
            resolved = await registry.resolve_agent_for_group(group_name)
            if not resolved:
                # Messages from groups no agent owns are silently ignored.
                return DispatchResult(handled=False)
            agent, config = resolved
            dispatched_agent_id = agent.agent_id

            sender_allowed = registry.is_sender_allowed(
                config, phone, is_group_member=sender_is_group_member
            )
        if not sender_allowed:
            await audit.log_turn(
                agent_id=agent.agent_id,
                group_name=group_name,
                sender_phone=phone,
                raw_message=raw_message,
                error="sender_not_allowlisted",
            )
            return DispatchResult(handled=False)

        # --- Voice transport interface (see this function's docstring-ish
        # param comments above and parser.VOICE_CONFIDENCE_THRESHOLD) ---
        if media_type == "voice_note" and not raw_message.strip():
            # No transcript at all — no STT engine exists yet. Tell the
            # user plainly rather than silently dropping the message (the
            # old behaviour: whatsapp-worker used to just skip a textless
            # bubble). Touches no conversation/session state, so any
            # in-flight operation survives untouched.
            await audit.log_turn(
                agent_id=agent.agent_id, group_name=group_name, sender_phone=phone,
                raw_message=raw_message, error="voice_note_unsupported",
            )
            return DispatchResult(
                handled=True,
                reply="I can't listen to voice notes yet — please type your message instead.",
            )

        voice_session = await session_context.get_session(agent.agent_id, phone)
        pending_transcript = (voice_session or {}).get("pending_voice_transcript")
        if pending_transcript:
            action = parse_confirmation_reply(working_message)
            if action == "approve":
                await session_context.update_session(
                    agent.agent_id, phone,
                    pending_voice_transcript=None, pending_voice_confidence=None,
                )
                # Substitute the now-confirmed transcript as THIS turn's
                # message and fall through to normal processing below — it
                # composes correctly whether or not another conversation is
                # already in flight (fresh-trigger-always-restarts, or fed
                # into whatever step that conversation is in).
                raw_message = pending_transcript
                working_message = clean_voice_transcript(raw_message)
            elif action in ("cancel", "edit"):
                await session_context.update_session(
                    agent.agent_id, phone,
                    pending_voice_transcript=None, pending_voice_confidence=None,
                )
                await audit.log_turn(
                    agent_id=agent.agent_id, group_name=group_name, sender_phone=phone,
                    raw_message=raw_message, confirmation_action="voice_transcript_rejected",
                )
                return DispatchResult(handled=True, reply="No problem — please type your message instead.")
            else:
                return DispatchResult(
                    handled=True,
                    reply=(
                        f'Please reply 1 for Yes or 2 for No.\n\nI heard:\n\n"{pending_transcript}"'
                        f"\n\nIs that correct?\n\n1. Yes\n2. No"
                    ),
                )
        elif transcript_confidence is not None and transcript_confidence < VOICE_CONFIDENCE_THRESHOLD:
            await session_context.update_session(
                agent.agent_id, phone,
                pending_voice_transcript=raw_message, pending_voice_confidence=transcript_confidence,
            )
            await audit.log_turn(
                agent_id=agent.agent_id, group_name=group_name, sender_phone=phone,
                raw_message=raw_message, confirmation_action="voice_transcript_pending",
            )
            return DispatchResult(
                handled=True,
                reply=f'I heard:\n\n"{raw_message}"\n\nIs that correct?\n\n1. Yes\n2. No',
            )

        conv = await conversation.get_conversation(agent.agent_id, phone)
        if conv and conversation.is_expired(conv):
            await conversation.clear_conversation(agent.agent_id, phone)
            conv = None

        # A fresh trigger always restarts, even mid-conversation.
        fresh_intent = detect_trigger(agent, working_message)

        if conv is None or fresh_intent is not None:
            intent = fresh_intent or (
                registry.get_intent(agent, conv["intent_id"]) if conv else None
            )
            bare_reply_resolution = None
            if intent is None and conv is None and agent.resolve_bare_reply:
                # Truly fresh (no conversation, no trigger match) — give the
                # agent one last chance to interpret this against whatever
                # it already has in session_context (e.g. "14" against a
                # just-shown numbered project list). Never reached while a
                # conversation is active, so this can't collide with an
                # in-progress intent's own "reply with a number" handling.
                bare_ctx = ExecContext(
                    agent_id=agent.agent_id, group_name=group_name,
                    sender_phone=phone, sender_name=sender_name,
                )
                bare_reply_resolution = await agent.resolve_bare_reply(working_message, bare_ctx)
                if bare_reply_resolution is not None:
                    intent, _ = bare_reply_resolution

            if intent is None:
                # No active conversation and this message doesn't open one
                # — unrelated chatter in the group, ignore.
                return DispatchResult(handled=False)

            if bare_reply_resolution is not None:
                _, initial_raw = bare_reply_resolution
            else:
                extractor = intent.extract_fields or (lambda t: extract_initial_fields(intent, t))
                initial_raw = extractor(working_message)
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
                question = _render_question(missing.question, collected)
                reply = ("\n\n".join(initial_errors) + "\n\n" + question) if initial_errors else question
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

            ctx = ExecContext(
                agent_id=agent.agent_id,
                group_name=group_name,
                sender_phone=phone,
                sender_name=sender_name,
                conversation_id=str(conv.get("_id") or ""),
            )
            if intent.auto_confirm:
                # Nothing to approve — reply immediately and don't leave a
                # lingering "confirming" conversation behind, UNLESS the
                # executor is asking a clarification question (see
                # ExecResult.needs_clarification) — then keep it alive in
                # "editing" step so the next free-text reply can continue
                # it via parse_edits_async, same as casting.move.
                exec_result = await intent.executor(collected, ctx)
                if exec_result.needs_clarification:
                    await conversation.update_conversation(agent.agent_id, phone, step="editing")
                else:
                    await conversation.clear_conversation(agent.agent_id, phone)
                await audit.log_turn(
                    agent_id=agent.agent_id,
                    group_name=group_name,
                    sender_phone=phone,
                    raw_message=raw_message,
                    conversation_id=str(conv.get("_id") or ""),
                    parsed_intent=intent.intent_id,
                    parsed_fields=collected,
                    validation_errors=initial_errors or None,
                    confirmation_action="auto",
                    execution_result=exec_result.message,
                    error=exec_result.error,
                )
                return DispatchResult(handled=True, reply=exec_result.message)

            if intent.try_auto_execute:
                auto_result = await intent.try_auto_execute(collected, ctx)
                if auto_result is not None:
                    await conversation.clear_conversation(agent.agent_id, phone)
                    await audit.log_turn(
                        agent_id=agent.agent_id,
                        group_name=group_name,
                        sender_phone=phone,
                        raw_message=raw_message,
                        conversation_id=str(conv.get("_id") or ""),
                        parsed_intent=intent.intent_id,
                        parsed_fields=collected,
                        validation_errors=initial_errors or None,
                        confirmation_action="and_confirm",
                        execution_result=auto_result.message,
                        error=auto_result.error,
                    )
                    return DispatchResult(handled=True, reply=auto_result.message)

            await conversation.update_conversation(
                agent.agent_id, phone, collected=collected, step="confirming"
            )
            reply = await _render_confirmation(intent, collected, ctx)
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
            action = parse_confirmation_reply(working_message)
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
        result = await _collect_or_advance(
            agent, intent, conv, working_message, sender_name=sender_name
        )
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
    finally:
        dispatch_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "dispatch_timing agent=%s group=%r dispatch_ms=%d stages=%s",
            dispatched_agent_id, group_name, dispatch_ms, request_scope.get_timings(),
        )
