"""Shared data contracts for the WhatsApp Agent Platform.

Every domain module (agents/modules/crm.py, and future projects-agent /
casting-agent / etc.) builds its behaviour purely out of these types and
registers an `AgentDefinition` with `agents.registry`. Nothing in here
knows about any specific domain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple


@dataclass
class ValidationResult:
    """Result of validating/cleaning one raw field value."""
    ok: bool
    value: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ExecContext:
    """Context passed to an intent's executor when the user approves."""
    agent_id: str
    group_name: str
    sender_phone: str
    sender_name: Optional[str] = None
    conversation_id: Optional[str] = None


@dataclass
class ExecResult:
    """Result of actually executing an approved intent (the DB write)."""
    ok: bool
    message: str  # WhatsApp reply text
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None  # short machine-readable code, e.g. "duplicate_phone"
    # Set True by an auto_confirm intent's executor when `message` is a
    # clarification question rather than a completed result — the domain
    # module has already stashed whatever state it needs (e.g. in
    # session_context) to interpret the next reply via parse_edits_async.
    # Tells the dispatcher to keep the conversation alive (step="editing")
    # instead of clearing it, the same continuation mechanism casting.move
    # already gets via build_confirmation. Default False preserves every
    # existing auto_confirm executor's behaviour unchanged.
    needs_clarification: bool = False


@dataclass
class FieldSpec:
    """One field an intent needs collected before it can execute.

    `validate` cleans + validates a single raw text value (already
    stripped of the field label if the user used `Key = value` syntax).
    It must be a pure function with no side effects — the generic engine
    calls it both while collecting fields turn-by-turn and while applying
    edits during the confirm/edit loop.
    """
    key: str
    label: str  # shown in the confirmation summary, e.g. "Name"
    question: str  # asked when this field is still missing, e.g. "What's the name?"
    validate: Callable[[str], ValidationResult]
    aliases: List[str] = field(default_factory=list)  # extra names accepted in "Key = value" edits
    # Optional fields never block confirmation and are omitted from the
    # summary when empty — the generic engine's missing-field prompt only
    # ever fires for required=True fields (2026-07-21 understanding-layer
    # upgrade: a CRM contact needs name/phone/role, but company/email/etc.
    # are enrichment only, never demanded).
    required: bool = True


@dataclass
class IntentDefinition:
    """One command an agent understands, e.g. crm.create_contact."""
    intent_id: str
    triggers: List[str]  # lowercase trigger words/phrases that open this intent
    fields: List[FieldSpec]
    executor: Callable[[Dict[str, str], ExecContext], Awaitable[ExecResult]]
    summary_title: str = "I understood:"
    # Optional overrides (2026-07-21 understanding-layer upgrade) — the
    # generic engine (parser.py/dispatcher.py) still owns the conversation
    # state machine and confirmation flow unchanged; a domain module may
    # supply smarter, domain-aware text understanding by overriding just
    # these two extraction steps instead of parser.py's generic
    # line-position-based ones:
    #   extract_fields(text) -> {field_key: raw_value} for the message that
    #     opens a fresh conversation (replaces parser.extract_initial_fields).
    #   parse_edits(text, fields) -> {field_key: raw_value} for a message
    #     sent during the "editing" step (replaces parser.parse_edit_instructions,
    #     which only understands "Key = value" syntax).
    # None means "use the generic engine's default" — every other existing
    # or future intent is unaffected unless it opts in.
    extract_fields: Optional[Callable[[str], Dict[str, str]]] = None
    parse_edits: Optional[Callable[[str, List[FieldSpec]], Dict[str, str]]] = None
    # When True, an intent with no missing required fields executes
    # immediately (no confirm/edit/cancel gate) — for read-only query
    # intents where there is nothing to approve. Default False preserves
    # every existing intent's behaviour unchanged.
    auto_confirm: bool = False
    # Overrides confirmation.build_confirmation_message for intents whose
    # confirmation text needs data the generic (sync, DB-free) validate/
    # extract_fields hooks can't reach — e.g. resolving a raw talent
    # selector against the current session's DB-backed listing. None means
    # "use the generic renderer" (every existing intent's behaviour).
    build_confirmation: Optional[Callable[[Dict[str, str], "ExecContext"], Awaitable[str]]] = None
    # Async counterpart to `parse_edits` — for an "editing"-step reply that
    # needs DB/session access to interpret (e.g. "2" picking option 2 from
    # a disambiguation list the domain module stored in session, rather
    # than "Key = value" syntax). Signature: (raw_text, collected_so_far,
    # fields, ctx) -> {field_key: raw_value}. Takes priority over the sync
    # `parse_edits` when set; None (the default) means "use parse_edits or
    # the generic Key=value parser" — every existing intent is unaffected.
    parse_edits_async: Optional[
        Callable[[str, Dict[str, str], List[FieldSpec], "ExecContext"], Awaitable[Dict[str, str]]]
    ] = None
    # Checked right before the engine would otherwise commit to the
    # "confirming" step (both at the fresh-trigger completion point and
    # _collect_or_advance's tail) — lets a message opt OUT of the approval
    # gate for THIS turn (e.g. a trailing "and confirm"), without making
    # auto_confirm a static per-intent flag. Returns None to proceed with
    # the normal confirmation card (the default, and what every existing
    # intent gets since this is None), or an ExecResult to short-circuit
    # straight to it — exactly as if the user had just replied "1". A
    # domain module that returns None here because resolution is still
    # ambiguous can rely on its own pending_disambiguation/"editing"-step
    # continuation firing as normal; since whatever field encoded "the
    # user asked to skip confirmation" persists in `collected` across that
    # continuation the same way any other field does, this hook naturally
    # gets re-checked (and can then succeed) once the ambiguity resolves —
    # no extra state needed for "continue automatically after resolving
    # the ambiguity, without asking for a second approval".
    try_auto_execute: Optional[
        Callable[[Dict[str, str], "ExecContext"], Awaitable[Optional["ExecResult"]]]
    ] = None
    # Interactive Campaign Editing (2026-08-09) — checked FIRST on every
    # message received while a conversation is in the "confirming" step,
    # before the generic approve/edit/cancel parsing. Lets a domain module
    # recognize its own natural-language commands ("Exclude Ahana",
    # "Change template to X", "Preview") directly on top of an already-
    # shown confirmation card, mutate `collected` (via conversation.
    # update_conversation, same collection, no new state store), and
    # return the regenerated confirmation text — all WITHOUT leaving the
    # "confirming" step, so "1"/"approve"/"2"/"edit"/"3"/"cancel" keep
    # working exactly as before, untouched by this hook. Returns None to
    # mean "not one of mine" — dispatcher.py falls through to the existing
    # approve/edit/cancel handling unchanged. None (the default, every
    # existing intent) means this checked branch is a complete no-op.
    handle_confirming_reply: Optional[
        Callable[[str, Dict[str, str], "ExecContext"], Awaitable[Optional[str]]]
    ] = None


@dataclass
class AgentDefinition:
    """One WhatsApp agent — owns exactly one backend module's worth of intents."""
    agent_id: str  # stable internal id, e.g. "crm-agent" — never a group name
    name: str  # human-readable, e.g. "Talentgram CRM"
    module: str  # the backend module this agent is scoped to, e.g. "marketing"
    intents: List[IntentDefinition]
    # Last-resort interpreter for a message that opened no conversation and
    # matched no intent trigger (dispatcher.py calls this ONLY in that
    # exact case — never while a conversation is active, so it can't
    # collide with an in-progress intent's own "reply with a number"
    # continuation, e.g. a pending MOVE disambiguation). Lets an agent
    # interpret a bare reply (e.g. "14") against whatever it already has
    # in session_context (e.g. a just-shown numbered project list) without
    # requiring the user to repeat a trigger word. Returns
    # (intent, collected_fields) to dispatch as if that intent had just
    # been freshly triggered with those fields already extracted, or None
    # to fall through to the normal "unrelated chatter, ignore" behaviour.
    # None (the default) means "no such fallback" — every existing agent
    # (including crm-agent) is unaffected.
    resolve_bare_reply: Optional[
        Callable[[str, "ExecContext"], Awaitable[Optional["Tuple[IntentDefinition, Dict[str, str]]"]]]
    ] = None
    # Concurrent Task Engine (2026-08-05) — when True, dispatcher.py ALSO
    # creates an independent, reply-addressable `agents.tasks` record for
    # every fresh trigger this agent handles, alongside the existing
    # single-slot `conversation.py` record it already creates (unchanged).
    # False (the default) means "no such record" — every existing agent,
    # including crm-agent, is completely unaffected; only an agent that
    # opts in (currently just casting-agent) gets concurrent, WhatsApp-
    # reply-addressable operations. See agents/tasks.py's module docstring
    # for the full design.
    supports_concurrent_tasks: bool = False
    # (2026-08-06) — when set, an unauthorized sender (fails registry.
    # is_sender_allowed) gets THIS text back instead of dispatcher.py's
    # default silent handled=False. None (every existing agent, including
    # crm-agent/casting-agent) means "no reply" — byte-for-byte identical
    # to before this field existed. Does not weaken the allowlist itself
    # in any way — it only changes what an already-rejected sender sees.
    unauthorized_sender_message: Optional[str] = None
    # Static Help Command — the exact text sent back for a bare "help" /
    # "menu" / "commands" / "please help" / "show commands" / "what can
    # you do" message (see parser.is_help_trigger). A plain static string,
    # not generated from `intents` — deliberately simple, per spec. None
    # (the default) means this agent has no help reply configured; every
    # existing agent is unaffected until it sets this.
    help_text: Optional[str] = None


@dataclass
class DispatchResult:
    """Outcome of processing one inbound message."""
    handled: bool  # False => the transport should send nothing back
    reply: Optional[str] = None
    # Concurrent Task Engine — set whenever `reply` is a confirmation/
    # clarification card belonging to a NEW or CONTINUED task, so the
    # transport (whatsapp-worker) can report back which WhatsApp message
    # id that card ends up getting once actually sent (see
    # routers/agents_whatsapp.py's /task-sent endpoint) — that's what
    # makes the task reply-addressable. None for every turn that isn't
    # task-related (the entire CRM flow, and casting-agent's own approve/
    # cancel/completed turns) — existing transport behaviour for those is
    # completely unchanged, since it simply never sees this field set.
    operation_id: Optional[str] = None
