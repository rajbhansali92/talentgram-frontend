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
import re
import time
from typing import Optional

from agents import audit, conversation, disambiguation, registry, request_scope, session_context, tasks
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


# Explicit-operation-id reply tier (priority 2, "future-ready" per the
# Concurrent Task Engine spec) — "CP-20260805-0A1B 1" approves that exact
# operation regardless of which message (if any) it's a WhatsApp reply to.
# Matched BEFORE the existing conversation.py path, same as a reply-to-
# message match; the operation id prefix is stripped and whatever's left
# (often nothing, defaulting to "1"/approve) is parsed as the actual reply.
_OPERATION_ID_RE = re.compile(r"^\s*(CP-\d{8}-[0-9A-Fa-f]{4})\b\s*(.*)$", re.IGNORECASE)

# Unanchored variant for Step 2B's Tier 2b: scans anywhere inside a QUOTED
# card's multi-line text for an embedded operation id (today's templates
# never include one, so this is currently inert — future-proofing only,
# see the routing block below). _OPERATION_ID_RE itself stays anchored to
# ^...$ because it also has to strip a leading id off the user's own single
# -line reply text, which this variant is not used for.
_OPERATION_ID_SCAN_RE = re.compile(r"(CP-\d{8}-[0-9A-Fa-f]{4})", re.IGNORECASE)


async def _advance_task(
    agent, task: dict, text: str, *, group_name: str, phone: str, sender_name: Optional[str],
) -> DispatchResult:
    """Task-engine counterpart of `_collect_or_advance` (+ the "confirming"
    step block inside handle_inbound_message below) — a deliberately
    PARALLEL implementation, not a shared one, so the existing, already-
    working conversation.py single-slot path is never touched by this
    feature (see agents/tasks.py's module docstring). Handles one turn
    already resolved to belong to THIS task (via reply-to-message or an
    explicit operation id) — never reads or writes any OTHER pending
    task, matching "resolve only that operation, never inspect unrelated
    pending tasks"."""
    op_id = task["operation_id"]
    intent = registry.get_intent(agent, task["intent_id"])
    if intent is None:
        await tasks.clear_task(agent.agent_id, op_id)
        return DispatchResult(handled=False)

    collected = dict(task.get("collected") or {})
    status = task.get("status")
    ctx = ExecContext(
        agent_id=agent.agent_id, group_name=group_name, sender_phone=phone,
        sender_name=sender_name, conversation_id=op_id,
    )

    if status == tasks.STATUS_CONFIRMING:
        action = parse_confirmation_reply(text)
        if action == "approve":
            await tasks.update_task(agent.agent_id, op_id, status=tasks.STATUS_EXECUTING)
            exec_result = await intent.executor(collected, ctx)
            await tasks.clear_task(agent.agent_id, op_id)
            return DispatchResult(handled=True, reply=exec_result.message)
        if action == "edit":
            await tasks.update_task(
                agent.agent_id, op_id, status=tasks.STATUS_CLARIFYING, last_message_text=EDIT_PROMPT,
            )
            return DispatchResult(handled=True, reply=EDIT_PROMPT, operation_id=op_id)
        if action == "cancel":
            await tasks.clear_task(agent.agent_id, op_id)
            return DispatchResult(handled=True, reply=CANCELLED_MESSAGE)
        return DispatchResult(handled=True, reply=UNRECOGNIZED_CONFIRMATION_REPLY, operation_id=op_id)

    if status == tasks.STATUS_CLARIFYING:
        if intent.parse_edits_async:
            edits = await intent.parse_edits_async(text, collected, intent.fields, ctx)
        else:
            edit_parser = intent.parse_edits or parse_edit_instructions
            edits = edit_parser(text, intent.fields)
        if not edits:
            return DispatchResult(handled=True, reply=UNRECOGNIZED_EDIT_REPLY, operation_id=op_id)
        for key, raw_value in edits.items():
            field = next((f for f in intent.fields if f.key == key), None)
            if not field:
                continue
            result = field.validate(raw_value)
            if not result.ok:
                return DispatchResult(handled=True, reply=result.error, operation_id=op_id)
            collected[key] = result.value
        await tasks.update_task(agent.agent_id, op_id, collected=collected, status=tasks.STATUS_CREATED)
    else:
        # status == STATUS_CREATED: this message answers the question for
        # the next missing field (mirrors _collect_or_advance's
        # "collecting" branch).
        missing = next_missing_field(intent, collected)
        if missing:
            result = missing.validate(text.strip())
            if not result.ok:
                return DispatchResult(handled=True, reply=result.error, operation_id=op_id)
            collected[missing.key] = result.value
            await tasks.update_task(agent.agent_id, op_id, collected=collected)

    still_missing = next_missing_field(intent, collected)
    if still_missing:
        reply = _render_question(still_missing.question, collected)
        await tasks.update_task(
            agent.agent_id, op_id, collected=collected, status=tasks.STATUS_CREATED,
            last_message_text=reply,
        )
        return DispatchResult(handled=True, reply=reply, operation_id=op_id)

    if intent.auto_confirm:
        exec_result = await intent.executor(collected, ctx)
        if exec_result.needs_clarification:
            await tasks.update_task(
                agent.agent_id, op_id, status=tasks.STATUS_CLARIFYING, collected=collected,
                last_message_text=exec_result.message,
            )
            return DispatchResult(handled=True, reply=exec_result.message, operation_id=op_id)
        await tasks.clear_task(agent.agent_id, op_id)
        return DispatchResult(handled=True, reply=exec_result.message)

    if intent.try_auto_execute:
        auto_result = await intent.try_auto_execute(collected, ctx)
        if auto_result is not None:
            await tasks.clear_task(agent.agent_id, op_id)
            return DispatchResult(handled=True, reply=auto_result.message)

    reply = await _render_confirmation(intent, collected, ctx)
    await tasks.update_task(
        agent.agent_id, op_id, collected=collected, status=tasks.STATUS_CONFIRMING,
        last_message_text=reply,
    )
    return DispatchResult(handled=True, reply=reply, operation_id=op_id)


async def _advance_disambiguation(
    agent, conv: dict, text: str, *, group_name: str, phone: str, sender_name: Optional[str],
) -> DispatchResult:
    """Sprint 1 (2026-08-09) — Shared Interactive Disambiguation Engine.
    Handles one turn while the conversation is in the "disambiguating"
    step (set by a domain module's build_confirmation hook via
    disambiguation.start, see agents/disambiguation.py's module docstring).
    Fully generic — knows nothing about projects/templates/talents/CRM/
    saved lists, only the opaque entity_type/field_key/candidates the
    caller stored. Self-contained (own audit logging), same shape as
    _collect_or_advance/_advance_task."""
    pending = await disambiguation.get_pending(agent.agent_id, phone)
    if pending is None or disambiguation.is_expired(pending):
        if pending:
            await disambiguation.clear(agent.agent_id, phone)
        await conversation.clear_conversation(agent.agent_id, phone)
        await audit.log_turn(
            agent_id=agent.agent_id, group_name=group_name, sender_phone=phone,
            raw_message=text, confirmation_action="disambiguation_expired",
        )
        return DispatchResult(
            handled=True,
            reply="That selection has expired — please send your command again.",
        )

    action = parse_confirmation_reply(text)
    if action == "cancel":
        await disambiguation.clear(agent.agent_id, phone)
        await conversation.clear_conversation(agent.agent_id, phone)
        await audit.log_turn(
            agent_id=agent.agent_id, group_name=group_name, sender_phone=phone,
            raw_message=text, confirmation_action="disambiguation_cancel",
        )
        return DispatchResult(handled=True, reply=CANCELLED_MESSAGE)

    candidates = [disambiguation.Candidate(**c) for c in pending["candidates"]]
    picked = disambiguation.resolve_reply(text, candidates)
    if picked is None:
        await audit.log_turn(
            agent_id=agent.agent_id, group_name=group_name, sender_phone=phone,
            raw_message=text, validation_errors=["unrecognized_disambiguation_reply"],
        )
        prompt = disambiguation.format_prompt(pending["entity_type"], candidates)
        return DispatchResult(handled=True, reply=f"Sorry, I didn't catch that.\n\n{prompt}")

    intent = registry.get_intent(agent, pending["intent_id"])
    if intent is None:
        await disambiguation.clear(agent.agent_id, phone)
        await conversation.clear_conversation(agent.agent_id, phone)
        return DispatchResult(handled=False)

    collected = dict(pending.get("collected") or {})
    collected[pending["field_key"]] = picked.label
    await disambiguation.clear(agent.agent_id, phone)

    still_missing = next_missing_field(intent, collected)
    if still_missing:
        await conversation.update_conversation(
            agent.agent_id, phone, collected=collected, step="collecting"
        )
        reply = _render_question(still_missing.question, collected)
        await audit.log_turn(
            agent_id=agent.agent_id, group_name=group_name, sender_phone=phone,
            raw_message=text, parsed_intent=intent.intent_id, parsed_fields=collected,
            confirmation_action="disambiguation_resolved",
        )
        return DispatchResult(handled=True, reply=reply)

    ctx = ExecContext(
        agent_id=agent.agent_id, group_name=group_name, sender_phone=phone,
        sender_name=sender_name, conversation_id=str(conv.get("_id") or ""),
    )
    await conversation.update_conversation(
        agent.agent_id, phone, collected=collected, step="confirming"
    )
    reply = await _render_confirmation(intent, collected, ctx)
    await audit.log_turn(
        agent_id=agent.agent_id, group_name=group_name, sender_phone=phone,
        raw_message=text, parsed_intent=intent.intent_id, parsed_fields=collected,
        confirmation_action="disambiguation_resolved",
    )
    return DispatchResult(handled=True, reply=reply)


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
    # Concurrent Task Engine (2026-08-05) — the WhatsApp message id this
    # inbound message is a reply TO, if any. None (the default, and what
    # every existing/CRM caller passes) means "not a reply" — the entire
    # routing branch below that reads this is skipped and behaviour is
    # byte-for-byte identical to before this feature existed. Populated by
    # the worker only once it can actually read this from the DOM (see
    # whatsapp-worker/inbound.py) — until then this stays None in
    # production too, same as a test that doesn't pass it.
    replied_to_message_id: Optional[str] = None,
    # Step 2B (2026-08-05) — the INNER TEXT of the reply's "quoted message"
    # block, if this is a WhatsApp reply. Real production DOM samples (see
    # whatsapp-worker/inbound.py's _extract_reply_context) show this text is
    # reliably present on a reply even though a directly-readable message id
    # for the quoted original is NOT — so this is the signal that actually
    # resolves a reply in practice; replied_to_message_id is still tried
    # first whenever it IS available (a stronger, non-text identifier), per
    # "avoid brittle string matching whenever a stronger identifier exists".
    replied_quoted_text: Optional[str] = None,
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
            # Opt-in only (2026-08-06) — None (every existing agent's
            # default) preserves the exact prior behaviour: silent
            # handled=False, nothing sent back. An agent that wants a
            # friendly rejection message instead sets this one string on
            # its AgentDefinition; nothing else about the fail-closed
            # allowlist check above changes for anyone.
            if agent.unauthorized_sender_message:
                return DispatchResult(handled=True, reply=agent.unauthorized_sender_message)
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

        # --- Concurrent Task Engine routing (2026-08-05, Step 2B) — priority
        # order per spec, strongest identifier first:
        #   1. WhatsApp confirmation_message_id (if the reply's quoted block
        #      exposed one — best-effort, see _extract_reply_context; real
        #      samples so far never carry one, but this is tried first
        #      whenever it IS present since it's an exact, non-text match).
        #   2a. Explicit operation id typed directly in the reply text.
        #   2b. Explicit operation id embedded in the QUOTED card's text
        #       (future-proofs richer templates that show "Operation\nCP-
        #       ..."; today's templates don't, so this is currently inert
        #       but costs nothing to check).
        #   3. Quoted confirmation/clarification TEXT match, scoped to this
        #      sender's own pending tasks — the signal that actually
        #      resolves a reply in practice today (see tasks.
        #      find_task_by_quoted_text's docstring for why this, not a
        #      message id, is the reliable fallback here).
        #   4. Falls through to the EXISTING conversation.py / session_
        #      context.py path below, completely unchanged.
        # Only ever reached for agents that opt in (agent.supports_
        # concurrent_tasks) — CRM never creates tasks, so this is always a
        # no-op miss for it. A match here resolves and advances ONLY that
        # one task — no other pending task (this agent's or anyone else's)
        # is ever read or touched. ---
        replied_task: Optional[dict] = None
        if agent.supports_concurrent_tasks:
            if replied_to_message_id:
                replied_task = await tasks.get_task_by_confirmation_message_id(
                    agent.agent_id, replied_to_message_id
                )
                if replied_task and tasks.is_expired(replied_task):
                    await tasks.clear_task(agent.agent_id, replied_task["operation_id"])
                    replied_task = None
            if replied_task is None:
                op_match = _OPERATION_ID_RE.match(working_message)
                if op_match:
                    candidate = await tasks.get_task(agent.agent_id, op_match.group(1).upper())
                    if candidate and not tasks.is_expired(candidate):
                        replied_task = candidate
                        # Strip the operation-id prefix — whatever remains
                        # ("1", "2 = Approved", "") is the actual reply,
                        # same grammar as replying to a card directly.
                        working_message = op_match.group(2).strip() or "1"
            if replied_task is None and replied_quoted_text:
                quoted_op_match = _OPERATION_ID_SCAN_RE.search(replied_quoted_text)
                if quoted_op_match:
                    candidate = await tasks.get_task(agent.agent_id, quoted_op_match.group(1).upper())
                    if candidate and not tasks.is_expired(candidate):
                        replied_task = candidate
                        # The id came from the QUOTED card, not the user's
                        # own reply text — working_message is the user's
                        # actual reply ("1", "cancel", ...) and must NOT be
                        # stripped here.
            if replied_task is None and replied_quoted_text:
                replied_task = await tasks.find_task_by_quoted_text(
                    agent.agent_id, phone, replied_quoted_text
                )
        if replied_task is not None:
            task_result = await _advance_task(
                agent, replied_task, working_message,
                group_name=group_name, phone=phone, sender_name=sender_name,
            )
            await audit.log_turn(
                agent_id=agent.agent_id,
                group_name=group_name,
                sender_phone=phone,
                raw_message=raw_message,
                conversation_id=replied_task["operation_id"],
                parsed_intent=replied_task.get("intent_id"),
                confirmation_action="task_reply",
            )
            return task_result

        conv = await conversation.get_conversation(agent.agent_id, phone)
        if conv and conversation.is_expired(conv):
            await conversation.clear_conversation(agent.agent_id, phone)
            conv = None

        # A fresh trigger always restarts, even mid-conversation.
        fresh_intent = detect_trigger(agent, working_message)

        if conv is None or fresh_intent is not None:
            # (2026-08-09, production-readiness audit) A fresh trigger
            # restarts the conversation even if it interrupts a pending
            # disambiguation — clear that stale choice explicitly rather
            # than leaving it to TTL. Harmless no-op when nothing is
            # pending; never a correctness issue either way (the
            # "disambiguating" branch below is only ever reached when
            # conv["step"] is still "disambiguating", which a fresh
            # trigger always overwrites), just resource hygiene.
            await disambiguation.clear(agent.agent_id, phone)
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
            # Concurrent Task Engine: ALSO create an independent, reply-
            # addressable task record — alongside, not instead of, the
            # conversation.py record just above (which keeps the existing
            # bare-digit-no-reply fallback working exactly as before).
            # None for every agent that hasn't opted in (CRM) — zero
            # effect on it.
            new_task: Optional[dict] = None
            if agent.supports_concurrent_tasks:
                new_task = await tasks.create_task(
                    agent_id=agent.agent_id, phone=phone, group_name=group_name,
                    sender_name=sender_name, intent_id=intent.intent_id,
                    collected=collected, original_text=raw_message,
                    reply_message_id=replied_to_message_id,
                )
            task_op_id = new_task["operation_id"] if new_task else None

            # Fields already extracted from `raw_message` above — just
            # check what (if anything) is still missing and reply
            # accordingly, rather than routing through _collect_or_advance
            # (which is for turns that *answer* a pending question, not
            # the message that opens the conversation).
            missing = next_missing_field(intent, collected)
            if missing:
                question = _render_question(missing.question, collected)
                reply = ("\n\n".join(initial_errors) + "\n\n" + question) if initial_errors else question
                if new_task:
                    await tasks.update_task(agent.agent_id, task_op_id, last_message_text=reply)
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
                return DispatchResult(handled=True, reply=reply, operation_id=task_op_id)

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
                    if new_task:
                        await tasks.update_task(
                            agent.agent_id, task_op_id, status=tasks.STATUS_CLARIFYING,
                            last_message_text=exec_result.message,
                        )
                else:
                    await conversation.clear_conversation(agent.agent_id, phone)
                    if new_task:
                        await tasks.clear_task(agent.agent_id, task_op_id)
                        task_op_id = None
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
                return DispatchResult(handled=True, reply=exec_result.message, operation_id=task_op_id)

            if intent.try_auto_execute:
                auto_result = await intent.try_auto_execute(collected, ctx)
                if auto_result is not None:
                    await conversation.clear_conversation(agent.agent_id, phone)
                    if new_task:
                        await tasks.clear_task(agent.agent_id, task_op_id)
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
            if new_task:
                await tasks.update_task(
                    agent.agent_id, task_op_id, status=tasks.STATUS_CONFIRMING, last_message_text=reply,
                )
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
            return DispatchResult(handled=True, reply=reply, operation_id=task_op_id)

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

        if conv["step"] == "disambiguating":
            return await _advance_disambiguation(
                agent, conv, working_message, group_name=group_name, phone=phone,
                sender_name=sender_name,
            )

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
        stages = request_scope.get_timings()
        logger.info(
            "dispatch_timing agent=%s group=%r dispatch_ms=%d stages=%s",
            dispatched_agent_id, group_name, dispatch_ms, stages,
        )
        # Human-readable companion to the line above (Phase 1 of the
        # latency investigation) — same data, aligned ASCII table instead
        # of a Python dict, for skimming Railway logs by eye. The
        # machine-parseable `stages={...}` line above stays unchanged for
        # grep/diffing.
        logger.info(
            "dispatch_breakdown agent=%s group=%r\n%s",
            dispatched_agent_id, group_name,
            request_scope.format_stage_table(stages, float(dispatch_ms)),
        )
        # Fine-grained op trace + Mongo Summary (2026-08-05 latency sprint,
        # Phase 1) — request_id ties these two lines back to the
        # dispatch_timing/dispatch_breakdown lines above for the same turn.
        logger.info(
            "op_trace agent=%s group=%r request_id=%s\n%s",
            dispatched_agent_id, group_name, request_scope.get_request_id(),
            request_scope.format_op_trace(),
        )
        logger.info(
            "mongo_summary agent=%s group=%r request_id=%s\n%s",
            dispatched_agent_id, group_name, request_scope.get_request_id(),
            request_scope.format_mongo_summary(),
        )
