"""Shared data contracts for the WhatsApp Agent Platform.

Every domain module (agents/modules/crm.py, and future projects-agent /
casting-agent / etc.) builds its behaviour purely out of these types and
registers an `AgentDefinition` with `agents.registry`. Nothing in here
knows about any specific domain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


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


@dataclass
class IntentDefinition:
    """One command an agent understands, e.g. crm.create_contact."""
    intent_id: str
    triggers: List[str]  # lowercase trigger words/phrases that open this intent
    fields: List[FieldSpec]
    executor: Callable[[Dict[str, str], ExecContext], Awaitable[ExecResult]]
    summary_title: str = "I understood:"


@dataclass
class AgentDefinition:
    """One WhatsApp agent — owns exactly one backend module's worth of intents."""
    agent_id: str  # stable internal id, e.g. "crm-agent" — never a group name
    name: str  # human-readable, e.g. "Talentgram CRM"
    module: str  # the backend module this agent is scoped to, e.g. "marketing"
    intents: List[IntentDefinition]


@dataclass
class DispatchResult:
    """Outcome of processing one inbound message."""
    handled: bool  # False => the transport should send nothing back
    reply: Optional[str] = None
